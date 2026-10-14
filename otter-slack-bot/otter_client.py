"""Thin, defensive wrapper around the unofficial Otter.ai API (gmchad/otterai-api).

Everything that could break when Otter changes their site is isolated here, so the
Slack layer only ever sees a clean interface: login() -> upload() -> wait_for_transcript()
-> download(). Responses from the library are the raw {'status', 'data'} dicts documented
in its source; we parse them tolerantly and log the shapes we see, because the first live
run against a real account is the only way to confirm Otter hasn't changed a key on us.

Key quirk baked in here: upload_speech() does NOT return the new speech's id, so upload()
snapshots the speech list, uploads, then polls for the new otid to appear.
"""
import logging
import os
import tempfile
import time

log = logging.getLogger("otterbot.otter")

try:                                   # packaging varies between versions of the lib
    from otterai import OtterAI
except Exception:                      # pragma: no cover - import path fallback
    try:
        from otterai.otterai import OtterAI
    except Exception:
        OtterAI = None                 # not installed (e.g. during tests); login() will raise


class OtterError(Exception):
    """Anything that stops us getting the file into Otter. Surfaced to the reporter."""


def _status_ok(resp) -> bool:
    try:
        return 200 <= int(resp.get("status", 0)) < 300
    except Exception:
        return False


def _data(resp) -> dict:
    d = resp.get("data") if isinstance(resp, dict) else None
    return d if isinstance(d, dict) else {}


def _speech_list(resp) -> list:
    """get_speeches() nests the array under a key that has moved between versions."""
    d = _data(resp)
    for key in ("speeches", "list", "results"):
        if isinstance(d.get(key), list):
            return d[key]
    # sometimes the array is one level deeper
    inner = d.get("data")
    if isinstance(inner, dict) and isinstance(inner.get("speeches"), list):
        return inner["speeches"]
    return []


def _otid(speech: dict):
    for key in ("otid", "speech_id", "id"):
        if speech.get(key):
            return speech[key]
    return None


def _looks_done(speech: dict) -> bool:
    """Best-effort 'transcript is ready' check across the flags Otter has used."""
    for key in ("process_finished", "finished", "is_processed", "processed"):
        if speech.get(key) is True:
            return True
    # fall back to structural signals that a transcript exists
    if isinstance(speech.get("transcripts"), list) and speech["transcripts"]:
        return True
    for key in ("word_count", "duration"):
        try:
            if float(speech.get(key) or 0) > 0 and speech.get("process_finished") is not False:
                return True
        except Exception:
            pass
    return False


class OtterUploader:
    def __init__(self, email: str, password: str):
        self._email = email
        self._password = password
        self._client = None
        self._logged_keys = False     # log speech-dict shape once, for first-run tuning

    def login(self):
        if OtterAI is None:
            raise OtterError("otterai library is not installed on this host")
        client = OtterAI()
        try:
            resp = client.login(self._email, self._password)
        except Exception as e:
            raise OtterError(f"login call failed: {e}") from e
        if not _status_ok(resp):
            raise OtterError(f"login rejected (status {resp.get('status')!r}) — "
                             "check the email/password and that 2FA is off on this account")
        self._client = client
        return self

    def _require(self):
        if self._client is None:
            raise OtterError("not logged in")
        return self._client

    def _snapshot_otids(self) -> set:
        try:
            return {_otid(s) for s in _speech_list(self._require().get_speeches()) if _otid(s)}
        except Exception as e:
            log.warning("could not snapshot existing speeches: %s", e)
            return set()

    def upload(self, file_path: str, content_type: str) -> str:
        """Upload a local audio file and return the new speech's otid."""
        client = self._require()
        before = self._snapshot_otids()
        try:
            resp = client.upload_speech(file_path, content_type)
        except Exception as e:
            raise OtterError(f"upload call failed: {e}") from e
        if not _status_ok(resp):
            raise OtterError(f"Otter rejected the upload (status {resp.get('status')!r})")

        # some versions return the otid directly on finish_speech_upload
        direct = _otid(_data(resp)) or _otid(_data(resp).get("speech", {}) if isinstance(_data(resp).get("speech"), dict) else {})
        if direct:
            return direct

        # otherwise wait for a brand-new speech to show up in the list
        deadline = time.time() + 90
        while time.time() < deadline:
            time.sleep(4)
            now = _speech_list(client.get_speeches())
            fresh = [s for s in now if _otid(s) and _otid(s) not in before]
            if fresh:
                fresh.sort(key=lambda s: s.get("created_at") or s.get("start_time") or 0, reverse=True)
                return _otid(fresh[0])
        raise OtterError("file uploaded but the new transcript did not appear in the account")

    def wait_for_transcript(self, otid: str, timeout: int, interval: int):
        client = self._require()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                speech = _data(client.get_speech(otid))
                speech = speech.get("speech", speech) if isinstance(speech.get("speech"), dict) else speech
            except Exception as e:
                log.warning("get_speech failed, will retry: %s", e)
                time.sleep(interval)
                continue
            if not self._logged_keys:
                log.info("otter speech keys: %s", sorted(speech.keys()))
                self._logged_keys = True
            if _looks_done(speech):
                return speech
            time.sleep(interval)
        return None

    def download(self, otid: str, fmt: str = "docx") -> bytes | None:
        """Return the finished transcript as bytes in the requested single format."""
        client = self._require()
        base = os.path.join(tempfile.gettempdir(), f"otter_{otid}")
        try:
            resp = client.download_speech(otid, name=base, fileformat=fmt)
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
