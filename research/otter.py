#!/usr/bin/env python3
"""Upload a reporter's audio to Otter.ai and post the transcript back to Slack.

Dispatched by the /otter slash command (Cloudflare Worker -> otter.yml). The reporter picked
the file + account in a Slack modal; this script does the work a newsroom editor used to do by
hand: download the file, log into the right Otter account, upload it, wait for the transcript,
and drop it back in Slack.

env (from otter.yml):
  SLACK_BOT_TOKEN                       bot token (download file, post results)
  OTTER_FILE_ID, OTTER_ACCOUNT          what to upload and where (jr|sr|conf)
  OTTER_CHANNEL, OTTER_USER             where /otter was run / who ran it
  OTTER_{JR,SR,CONF}_EMAIL/_PASSWORD    the three Otter logins

Otter has no public upload API, so this rides the unofficial library (gmchad/otterai-api):
login is email+password (no 2FA), and upload_speech() doesn't return the new speech id, so we
diff the speech list to find it. Everything Otter-specific is tolerant + logged, because the
first live run is the only way to confirm Otter hasn't renamed a field.
"""
import logging
import os
import tempfile
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("otter")

try:
    from otterai import OtterAI
except Exception:                      # pragma: no cover
    try:
        from otterai.otterai import OtterAI
    except Exception:
        OtterAI = None

TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
API = "https://slack.com/api/"

ACCOUNTS = {
    "jr":   ("Jr reporters",  "OTTER_JR_EMAIL",   "OTTER_JR_PASSWORD"),
    "sr":   ("Sr reporters",  "OTTER_SR_EMAIL",   "OTTER_SR_PASSWORD"),
    "conf": ("Confidential",  "OTTER_CONF_EMAIL", "OTTER_CONF_PASSWORD"),
}
CONTENT_TYPES = {
    "mp3": "audio/mpeg", "mpga": "audio/mpeg", "mpeg": "audio/mpeg", "m4a": "audio/mp4",
    "mp4": "audio/mp4", "m4v": "audio/mp4", "aac": "audio/aac", "wav": "audio/wav",
    "flac": "audio/flac", "ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/opus",
    "wma": "audio/x-ms-wma", "aiff": "audio/aiff", "aif": "audio/aiff",
    "mov": "video/quicktime", "webm": "video/webm",
}
TRANSCRIPT_FORMAT = "docx"
POLL_INTERVAL = int(os.environ.get("OTTER_POLL_INTERVAL", "20"))
POLL_TIMEOUT = int(os.environ.get("OTTER_POLL_TIMEOUT", "2400"))


# --------------------------------------------------------------------------- #
# Slack helpers
# --------------------------------------------------------------------------- #
def _slack(method, **payload):
    r = requests.post(API + method, headers={"Authorization": "Bearer " + TOKEN},
                      json=payload, timeout=60)
    d = r.json()
    if not d.get("ok"):
        log.warning("slack %s -> %s", method, d.get("error"))
    return d


def file_info(file_id):
    r = requests.get(API + "files.info", headers={"Authorization": "Bearer " + TOKEN},
                     params={"file": file_id}, timeout=60)
    return r.json()


def download(url):
    r = requests.get(url, headers={"Authorization": "Bearer " + TOKEN}, timeout=600)
    r.raise_for_status()
    return r.content


def open_dm(user):
    return (_slack("conversations.open", users=user).get("channel") or {}).get("id")


def start_status(channel, user, text):
    """Post the first status message; fall back to DMing the user if the channel isn't
    reachable (bot not a member). Returns (channel_id, ts) to update + thread under."""
    if channel:
        d = _slack("chat.postMessage", channel=channel, text=text)
        if d.get("ok"):
            return channel, d["ts"]
        log.warning("cannot post to channel %s (%s) — DMing the user", channel, d.get("error"))
    dm = open_dm(user)
    d = _slack("chat.postMessage", channel=dm, text=text)
    return dm, d.get("ts")


def update_status(channel, ts, text):
    if channel and ts:
        _slack("chat.update", channel=channel, ts=ts, text=text)


def upload_transcript(channel, thread_ts, filename, content, comment):
    """Post a file into Slack via the current external-upload flow (files.upload is sunset)."""
    g = requests.get(API + "files.getUploadURLExternal",
                     headers={"Authorization": "Bearer " + TOKEN},
                     params={"filename": filename, "length": len(content)}, timeout=60).json()
    if not g.get("ok"):
        log.warning("getUploadURLExternal -> %s", g.get("error"))
        return False
    put = requests.post(g["upload_url"], files={"file": (filename, content)}, timeout=600)
    if put.status_code != 200:
        log.warning("upload POST -> %s", put.status_code)
        return False
    args = {"files": [{"id": g["file_id"], "title": filename}],
            "channel_id": channel, "initial_comment": comment}
    if thread_ts:
        args["thread_ts"] = thread_ts
    return _slack("files.completeUploadExternal", **args).get("ok", False)


# --------------------------------------------------------------------------- #
# Otter (tolerant wrapper over the unofficial library)
# --------------------------------------------------------------------------- #
class OtterError(Exception):
    pass


def _ok(resp):
    try:
        return 200 <= int(resp.get("status", 0)) < 300
    except Exception:
        return False


def _data(resp):
    d = resp.get("data") if isinstance(resp, dict) else None
    return d if isinstance(d, dict) else {}


def _speech_list(resp):
    d = _data(resp)
    for k in ("speeches", "list", "results"):
        if isinstance(d.get(k), list):
            return d[k]
    inner = d.get("data")
    if isinstance(inner, dict) and isinstance(inner.get("speeches"), list):
        return inner["speeches"]
    return []


def _otid(speech):
    for k in ("otid", "speech_id", "id"):
        if speech.get(k):
            return speech[k]
    return None


def _looks_done(speech):
    for k in ("process_finished", "finished", "is_processed", "processed"):
        if speech.get(k) is True:
            return True
    if isinstance(speech.get("transcripts"), list) and speech["transcripts"]:
        return True
    return False


class Otter:
    def __init__(self, email, password):
        self.email, self.password, self.c = email, password, None
        self._logged = False

    def login(self):
        if OtterAI is None:
            raise OtterError("otterai library not installed")
        c = OtterAI()
        try:
            resp = c.login(self.email, self.password)
        except Exception as e:
            raise OtterError(f"login failed: {e}") from e
        if not _ok(resp):
            raise OtterError(f"login rejected (status {resp.get('status')!r}) — "
                             "check the email/password and that 2FA is off")
        self.c = c
        return self

    def _ids(self):
        try:
            return {_otid(s) for s in _speech_list(self.c.get_speeches()) if _otid(s)}
        except Exception as e:
            log.warning("snapshot speeches failed: %s", e)
            return set()

    def upload(self, path, content_type):
        before = self._ids()
        try:
            resp = self.c.upload_speech(path, content_type)
        except Exception as e:
            raise OtterError(f"upload failed: {e}") from e
        if not _ok(resp):
            raise OtterError(f"Otter rejected the upload (status {resp.get('status')!r})")
        direct = _otid(_data(resp))
        if direct:
            return direct
        deadline = time.time() + 90
        while time.time() < deadline:
            time.sleep(4)
            fresh = [s for s in _speech_list(self.c.get_speeches())
                     if _otid(s) and _otid(s) not in before]
            if fresh:
                fresh.sort(key=lambda s: s.get("created_at") or s.get("start_time") or 0, reverse=True)
                return _otid(fresh[0])
        raise OtterError("file uploaded but the new transcript did not appear")

    def wait_for_transcript(self, otid):
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            try:
                sp = _data(self.c.get_speech(otid))
                sp = sp.get("speech", sp) if isinstance(sp.get("speech"), dict) else sp
            except Exception as e:
                log.warning("get_speech retry: %s", e)
                time.sleep(POLL_INTERVAL)
                continue
            if not self._logged:
                log.info("otter speech keys: %s", sorted(sp.keys()))
                self._logged = True
            if _looks_done(sp):
                return sp
            time.sleep(POLL_INTERVAL)
        return None

    def download(self, otid, fmt=TRANSCRIPT_FORMAT):
        base = os.path.join(tempfile.gettempdir(), f"otter_{otid}")
        try:
            resp = self.c.download_speech(otid, name=base, fileformat=fmt)
        except Exception as e:
            log.warning("download_speech failed: %s", e)
            return None
        path = _data(resp).get("filename")
        if path and os.path.exists(path):
            try:
                with open(path, "rb") as fh:
                    return fh.read()
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        return None


def content_type_for(name):
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return CONTENT_TYPES.get(ext, "audio/mp4")


# --------------------------------------------------------------------------- #
def main():
    account = os.environ.get("OTTER_ACCOUNT", "")
    file_id = os.environ.get("OTTER_FILE_ID", "")
    channel = os.environ.get("OTTER_CHANNEL", "")
    user = os.environ.get("OTTER_USER", "")
    if account not in ACCOUNTS:
        log.error("unknown account %r", account)
        return
    label, e_env, p_env = ACCOUNTS[account]
    email, password = os.environ.get(e_env), os.environ.get(p_env)

    # Confidential source audio is delivered privately to the uploader, never into a channel.
    if account == "conf":
        channel = ""
    tgt, ts = start_status(channel, user, f"⏳ Uploading to *{label}* Otter…")

    if not email or not password:
        update_status(tgt, ts, f"⚠️ The *{label}* Otter account isn't configured yet "
                               f"(missing {e_env}/{p_env}).")
        return

    tmp = None
    try:
        info = (file_info(file_id) or {}).get("file") or {}
        filename = info.get("name") or f"{file_id}.audio"
        dl = info.get("url_private_download") or info.get("url_private")
        if not dl:
            raise OtterError("Slack returned no download URL for that file")

        audio = download(dl)
        fd, tmp = tempfile.mkstemp(suffix="_" + filename.replace("/", "_"))
        with os.fdopen(fd, "wb") as fh:
            fh.write(audio)

        otter = Otter(email, password).login()
        otid = otter.upload(tmp, content_type_for(filename))
        log.info("uploaded account=%s otid=%s file=%s", account, otid,
                 "<redacted>" if account == "conf" else filename)
        update_status(tgt, ts, f"✅ Uploaded to *{label}* Otter — transcribing now…")

        posted = False
        speech = otter.wait_for_transcript(otid)
        if speech is not None:
            content = otter.download(otid)
            if content:
                base = filename.rsplit(".", 1)[0]
                posted = upload_transcript(tgt, ts, f"{base}.{TRANSCRIPT_FORMAT}", content,
                                           f"📝 Transcript ready — *{label}* Otter")
        if account != "conf":
            _slack("chat.postMessage", channel=tgt, thread_ts=ts,
                   text=f"🔗 In Otter: https://otter.ai/u/{otid}")

        update_status(tgt, ts, f"✅ Done — *{filename}* → *{label}* Otter."
                      if posted else
                      f"✅ Uploaded *{filename}* to *{label}* Otter. It'll finish transcribing there shortly.")

    except OtterError as e:
        log.warning("otter failed: %s", e)
        update_status(tgt, ts, f"⚠️ Couldn't upload to *{label}* Otter: {e}\n"
                               "Nothing was changed — please upload it manually this time.")
    except Exception as e:
        log.exception("unexpected failure")
        update_status(tgt, ts, f"⚠️ Something went wrong uploading to *{label}* Otter. "
                               "Please upload it manually this time.")
    finally:
        if tmp and os.path.exists(tmp):      # never leave source audio on the runner
            try:
                os.remove(tmp)
            except OSError:
                pass


if __name__ == "__main__":
    main()
