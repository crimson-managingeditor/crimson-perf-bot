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
import json
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
# Otter now requires an `appid` on its API calls; the web client sends "otter-web".
APPID = os.environ.get("OTTER_APPID", "otter-web")


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

    def _api_get(self, path, **params):
        """GET an Otter API endpoint on the logged-in session, with appid attached."""
        params.setdefault("appid", APPID)
        r = self.c._session.get(self.c.API_BASE_URL + path, params=params, timeout=120)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {}

    def upload(self, path, content_type):
        # Reimplements the library's upload with appid on every call (Otter returns
        # {"message":"missing appid","code":10} otherwise). Uses the logged-in session.
        import xml.etree.ElementTree as ET
        from requests_toolbelt.multipart.encoder import MultipartEncoder
        sess, uid = self.c._session, self.c._userid
        api, s3base = self.c.API_BASE_URL, self.c.S3_BASE_URL
        before = self._ids()
        size = os.path.getsize(path) if os.path.exists(path) else -1
        log.info("upload start: content_type=%s bytes=%s existing_speeches=%d",
                 content_type, size, len(before))

        # 1. presigned S3 form fields
        pr = sess.get(api + "speech_upload_params", params={"userid": uid, "appid": APPID}, timeout=120)
        if pr.status_code != 200:
            log.warning("speech_upload_params %s: %s", pr.status_code, pr.text[:300])
            raise OtterError(f"Otter rejected the upload prep (status {pr.status_code})")
        fields = dict(pr.json()["data"])
        fields["success_action_status"] = str(fields.get("success_action_status", 201))
        fields.pop("form_action", None)

        # 2. POST the audio to S3
        with open(path, "rb") as fh:
            fields["file"] = (os.path.basename(path), fh, content_type)
            mpe = MultipartEncoder(fields=fields)
            s3r = requests.post(s3base + "speech-upload-prod", data=mpe,
                                headers={"Content-Type": mpe.content_type}, timeout=600)
        if s3r.status_code != 201:
            log.warning("S3 upload %s: %s", s3r.status_code, s3r.text[:300])
            raise OtterError(f"storage upload failed (status {s3r.status_code})")
        root = ET.fromstring(s3r.text)
        bucket, key = root[1].text, root[2].text

        # 3. finish the upload
        fr = sess.get(api + "finish_speech_upload",
                      params={"bucket": bucket, "key": key, "language": "en",
                              "country": "us", "userid": uid, "appid": APPID}, timeout=120)
        if fr.status_code != 200:
            log.warning("finish_speech_upload %s: %s", fr.status_code, fr.text[:300])
            raise OtterError(f"Otter rejected the upload (status {fr.status_code})")
        try:
            fj = fr.json()
        except Exception:
            fj = {}
        otid = _otid(fj) or _otid((fj.get("speech") or {}) if isinstance(fj, dict) else {})
        if otid:
            return otid
        # otid not in the finish response -> find the new speech by diffing the list
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
            code, j = self._api_get("speech", userid=self.c._userid, otid=otid)
            sp = j.get("speech", j) if isinstance(j, dict) else {}
            if not isinstance(sp, dict):
                sp = {}
            if not self._logged:
                log.info("otter speech keys: %s (http %s)", sorted(sp.keys()), code)
                self._logged = True
            if _looks_done(sp):
                return sp
            time.sleep(POLL_INTERVAL)
        return None

    def download(self, otid, fmt=TRANSCRIPT_FORMAT):
        sess, uid = self.c._session, self.c._userid
        csrf = (self.c._cookies or {}).get("csrftoken") or sess.cookies.get("csrftoken")
        headers = {"referer": "https://otter.ai/"}
        if csrf:
            headers["x-csrftoken"] = csrf
        try:
            r = sess.post(self.c.API_BASE_URL + "bulk_export",
                          params={"userid": uid, "appid": APPID}, headers=headers,
                          data={"formats": fmt, "speech_otid_list": [otid]}, timeout=180)
        except Exception as e:
            log.warning("bulk_export error: %s", e)
            return None
        if not r.ok:
            log.warning("bulk_export %s: %s", r.status_code, r.text[:200])
            return None
        return r.content


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
