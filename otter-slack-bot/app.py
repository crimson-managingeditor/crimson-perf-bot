"""Slack bot: a reporter drops an audio file in Slack, taps Jr / Sr / Confidential,
and the bot uploads it to the matching Otter account on their behalf — then posts the
finished transcript back into the thread. Runs over Socket Mode (no public URL needed).

Flow:
  message-with-audio  ->  post account buttons in-thread
  button click        ->  ack fast, then (in a worker thread) download from Slack,
                          upload to Otter, poll for the transcript, post it back
The audio file is always deleted from this host once it has reached Otter.
"""
import json
import logging
import os
import re
import tempfile
import threading

import requests

import config
from otter_client import OtterError, OtterUploader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("otterbot")

# Injection points so the worker is testable without real Slack/Otter.
make_uploader = OtterUploader


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested directly)
# --------------------------------------------------------------------------- #
def is_audio_file(f: dict) -> bool:
    ftype = (f.get("filetype") or "").lower()
    name = (f.get("name") or f.get("title") or "").lower()
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    mt = (f.get("mimetype") or "").lower()
    return (ext in config.AUDIO_EXTS or ftype in config.AUDIO_EXTS
            or mt.startswith("audio/") or mt.startswith("video/"))


def account_buttons(file_id: str, filename: str, uploader: str) -> list:
    value = json.dumps({"f": file_id, "u": uploader})
    elements = []
    for key, acct in config.ACCOUNTS.items():
        btn = {
            "type": "button",
            "action_id": f"pick:{key}",
            "text": {"type": "plain_text", "text": acct["label"]},
            "value": value,
        }
        if key == "conf":
            btn["style"] = "danger"
        elements.append(btn)
    return [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"🎙️ *{filename}* — which Otter account?"}},
        {"type": "actions", "elements": elements},
    ]


def _status_blocks(text: str) -> list:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


# --------------------------------------------------------------------------- #
# /otter slash command -> a modal with a file picker + account dropdown.
# (Slash commands can't carry a file, so the file comes via the modal.)
# --------------------------------------------------------------------------- #
def build_modal(channel_id: str) -> dict:
    options = [{"text": {"type": "plain_text", "text": acct["label"]}, "value": key}
               for key, acct in config.ACCOUNTS.items()]
    return {
        "type": "modal",
        "callback_id": "otter_modal",
        "private_metadata": channel_id or "",     # remember where to post results
        "title": {"type": "plain_text", "text": "Upload to Otter"},
        "submit": {"type": "plain_text", "text": "Upload"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "input", "block_id": "otter_file",
             "label": {"type": "plain_text", "text": "Audio file"},
             "element": {"type": "file_input", "action_id": "file", "max_files": 1}},
            {"type": "input", "block_id": "otter_acct",
             "label": {"type": "plain_text", "text": "Which Otter account?"},
             "element": {"type": "static_select", "action_id": "acct",
                         "placeholder": {"type": "plain_text", "text": "Choose one"},
                         "options": options}},
        ],
    }


def parse_modal(view: dict):
    """Return (file_id, account_key) from a submitted otter_modal view."""
    vals = (view.get("state") or {}).get("values") or {}
    files = ((vals.get("otter_file") or {}).get("file") or {}).get("files") or []
    file_id = files[0]["id"] if files else None
    selected = ((vals.get("otter_acct") or {}).get("acct") or {}).get("selected_option") or {}
    return file_id, selected.get("value")


def start_status(client, channel: str, user: str, text: str):
    """Post the first status message, falling back to a DM if the channel isn't reachable
    (e.g. the bot isn't a member). Returns (channel_id, message_ts) to update + thread under."""
    if channel:
        try:
            r = client.chat_postMessage(channel=channel, text=text)
            return channel, r["ts"]
        except Exception as e:
            log.warning("cannot post to %s (%s) — DMing the user instead", channel, e)
    dm = client.conversations_open(users=user)["channel"]["id"]
    r = client.chat_postMessage(channel=dm, text=text)
    return dm, r["ts"]


def run_modal_upload(client, channel, user, account_key, file_id):
    label = config.ACCOUNTS[account_key]["label"]
    target, ts = start_status(client, channel, user, f"⏳ Routing to *{label}* Otter…")
    process_upload(client, target, ts, ts, account_key, file_id, user)


# --------------------------------------------------------------------------- #
# The heavy lifting: download from Slack -> upload to Otter -> post transcript
# --------------------------------------------------------------------------- #
def download_slack_file(url: str) -> bytes:
    r = requests.get(url, headers={"Authorization": f"Bearer {config.SLACK_BOT_TOKEN}"}, timeout=120)
    r.raise_for_status()
    return r.content


def process_upload(client, channel, thread_ts, status_ts, account_key, file_id, clicker,
                   downloader=download_slack_file):
    acct = config.ACCOUNTS[account_key]
    label = acct["label"]
    tmp_path = None

    def status(text):
        try:
            client.chat_update(channel=channel, ts=status_ts, text=text, blocks=_status_blocks(text))
        except Exception as e:
            log.warning("status update failed: %s", e)

    try:
        info = client.files_info(file=file_id)["file"]
        filename = info.get("name") or f"{file_id}.audio"
        dl_url = info.get("url_private_download") or info.get("url_private")
        if not dl_url:
            raise OtterError("Slack did not return a download URL for that file")

        status(f"⏳ Sending *{filename}* to *{label}* Otter…")
        audio = downloader(dl_url)

        fd, tmp_path = tempfile.mkstemp(suffix="_" + re.sub(r"[^A-Za-z0-9._-]", "_", filename))
        with os.fdopen(fd, "wb") as fh:
            fh.write(audio)

        up = make_uploader(acct["email"], acct["password"]).login()
        otid = up.upload(tmp_path, config.content_type_for(filename))

        # Core job done — the file is in Otter. Everything below is a best-effort bonus.
        status(f"✅ Uploaded to *{label}* Otter — transcribing now…")
        # don't log a confidential source's filename
        log.info("uploaded file=%s account=%s otid=%s",
                 "<redacted>" if account_key == "conf" else filename, account_key, otid)

        posted_something = False
        if config.post_transcript(account_key):
            speech = up.wait_for_transcript(otid, config.POLL_TIMEOUT, config.POLL_INTERVAL)
            if speech is not None:
                content = up.download(otid, config.TRANSCRIPT_FORMAT)
                if content:
                    base = filename.rsplit(".", 1)[0]
                    client.files_upload_v2(
                        channel=channel, thread_ts=thread_ts,
                        filename=f"{base}.{config.TRANSCRIPT_FORMAT}",
                        content=content,
                        initial_comment=f"📝 Transcript ready — *{label}* Otter",
                    )
                    posted_something = True

        if config.post_link(account_key):
            link = config.OTTER_SPEECH_URL.format(otid=otid)
            client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                    text=f"🔗 Otter link: {link}")
            posted_something = True

        if posted_something:
            status(f"✅ Done — *{filename}* → *{label}* Otter.")
        else:
            status(f"✅ Uploaded *{filename}* to *{label}* Otter. "
                   "It'll finish transcribing there shortly.")

    except OtterError as e:
        log.warning("otter upload failed: %s", e)
        status(f"⚠️ Couldn't upload to *{label}* Otter: {e}\n"
               "The file is untouched — please upload it manually this time.")
    except Exception as e:                              # pragma: no cover - defensive
        log.exception("unexpected failure in process_upload")
        status(f"⚠️ Something went wrong uploading to *{label}* Otter. "
               "Please upload it manually this time.")
    finally:
        if tmp_path and os.path.exists(tmp_path):        # never leave source audio on the host
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Slack wiring
# --------------------------------------------------------------------------- #
def build_app():
    from slack_bolt import App
    app = App(token=config.SLACK_BOT_TOKEN)

    @app.event("message")
    def on_message(event, client, logger):
        # ignore our own messages, edits, deletes, etc. — only fresh posts with files
        if event.get("bot_id") or event.get("subtype") not in (None, "file_share"):
            return
        files = event.get("files") or []
        audio = [f for f in files if is_audio_file(f)]
        if not audio:
            return
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        uploader = event.get("user", "")
        for f in audio:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f"Where should “{f.get('name', 'this audio')}” go?",
                blocks=account_buttons(f["id"], f.get("name") or f.get("title") or "audio", uploader),
            )

    @app.action(re.compile(r"^pick:(jr|sr|conf)$"))
    def on_pick(ack, body, action, client, logger):
        ack()
        account_key = action["action_id"].split(":", 1)[1]
        if account_key not in config.ACCOUNTS:
            return
        payload = json.loads(action.get("value") or "{}")
        file_id = payload.get("f")
        clicker = body["user"]["id"]
        channel = body["container"]["channel_id"]
        status_ts = body["container"]["message_ts"]
        msg = body.get("message", {})
        thread_ts = msg.get("thread_ts") or status_ts

        if account_key == "conf" and not config.conf_allowed(clicker):
            client.chat_postEphemeral(
                channel=channel, user=clicker,
                text="🔒 The *Confidential* Otter account isn't enabled for you. "
                     "Ask an admin to add your Slack ID to CONF_ALLOWLIST.")
            return

        client.chat_update(channel=channel, ts=status_ts,
                           text="Working…",
                           blocks=_status_blocks(f"⏳ Routing to *{config.ACCOUNTS[account_key]['label']}* Otter…"))
        threading.Thread(
            target=process_upload,
            args=(client, channel, thread_ts, status_ts, account_key, file_id, clicker),
            daemon=True,
        ).start()

    @app.command("/otter")
    def on_command(ack, body, client, logger):
        ack()
        if not config.ACCOUNTS:
            client.chat_postEphemeral(channel=body["channel_id"], user=body["user_id"],
                                      text="No Otter accounts are configured yet.")
            return
        client.views_open(trigger_id=body["trigger_id"], view=build_modal(body["channel_id"]))

    @app.view("otter_modal")
    def on_modal_submit(ack, body, view, client, logger):
        file_id, account_key = parse_modal(view)
        user = body["user"]["id"]
        errors = {}
        if not file_id:
            errors["otter_file"] = "Attach an audio file."
        if account_key not in config.ACCOUNTS:
            errors["otter_acct"] = "Pick an account."
        elif account_key == "conf" and not config.conf_allowed(user):
            errors["otter_acct"] = "You're not allowed to use the Confidential account."
        if errors:
            ack(response_action="errors", errors=errors)
            return
        ack()   # close the modal; work happens off-thread
        channel = view.get("private_metadata") or ""
        threading.Thread(
            target=run_modal_upload,
            args=(client, channel, user, account_key, file_id),
            daemon=True,
        ).start()

    return app


def main():
    missing = [v for v in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN") if not os.environ.get(v)]
    if missing:
        raise SystemExit(f"missing env: {', '.join(missing)}")
    if not config.ACCOUNTS:
        raise SystemExit("no Otter accounts configured — set at least OTTER_JR_EMAIL/PASSWORD")
    if "conf" in config.ACCOUNTS and not config.CONF_ALLOWLIST and config.CONF_REQUIRE_ALLOWLIST:
        log.warning("confidential account configured but CONF_ALLOWLIST is empty — "
                    "nobody can use it until you set CONF_ALLOWLIST")

    from slack_bolt.adapter.socket_mode import SocketModeHandler
    app = build_app()
    log.info("otter-slack-bot starting (accounts: %s)", ", ".join(config.ACCOUNTS))
    SocketModeHandler(app, config.SLACK_APP_TOKEN).start()


if __name__ == "__main__":
    main()
