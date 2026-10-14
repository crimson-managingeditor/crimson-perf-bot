"""Smoke tests for the routing + upload flow, using fakes for Slack and Otter so no
network, credentials, or the unofficial library are needed. Run: python tests/test_flow.py
(or `pytest`)."""
import os
import sys

# Configure three accounts + a confidential allowlist BEFORE importing config/app.
os.environ.update({
    "OTTER_JR_EMAIL": "jr@x", "OTTER_JR_PASSWORD": "jrpw",
    "OTTER_SR_EMAIL": "sr@x", "OTTER_SR_PASSWORD": "srpw",
    "OTTER_CONF_EMAIL": "conf@x", "OTTER_CONF_PASSWORD": "confpw",
    "CONF_ALLOWLIST": "UALLOWED",
    "SLACK_BOT_TOKEN": "xoxb-test",
})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import config


class FakeSlack:
    def __init__(self, filename="interview.mp3"):
        self.filename = filename
        self.updates = []        # (ts, text)
        self.uploaded_files = [] # dict of kwargs
        self.messages = []       # (thread_ts, text)
        self.ephemerals = []

    def files_info(self, file):
        return {"file": {"id": file, "name": self.filename,
                         "url_private_download": f"https://files.slack/{file}"}}

    def chat_update(self, channel, ts, text, blocks=None):
        self.updates.append((ts, text))

    def files_upload_v2(self, **kw):
        self.uploaded_files.append(kw)

    def chat_postMessage(self, channel, thread_ts, text):
        self.messages.append((thread_ts, text))

    def chat_postEphemeral(self, channel, user, text):
        self.ephemerals.append((user, text))


class FakeUploader:
    """Records what the Otter layer was asked to do. Captures the last instance."""
    last = None

    def __init__(self, email, password):
        self.email, self.password = email, password
        self.uploaded = None
        FakeUploader.last = self

    def login(self):
        return self

    def upload(self, path, content_type):
        assert os.path.exists(path), "temp audio file should exist during upload"
        with open(path, "rb") as fh:
            self.uploaded = {"bytes": fh.read(), "content_type": content_type, "path": path}
        return "OTID123"

    def wait_for_transcript(self, otid, timeout, interval):
        return {"otid": otid, "process_finished": True}

    def download(self, otid, fmt):
        return b"TRANSCRIPT-DOCX-BYTES"


def run(account_key, clicker, slack=None, fail=False):
    slack = slack or FakeSlack()
    app.make_uploader = (FailUploader if fail else FakeUploader)
    app.process_upload(
        client=slack, channel="C1", thread_ts="100.0", status_ts="101.0",
        account_key=account_key, file_id="F1", clicker=clicker,
        downloader=lambda url: b"FAKE-AUDIO-BYTES",
    )
    return slack


class FailUploader(FakeUploader):
    def upload(self, path, content_type):
        from otter_client import OtterError
        raise OtterError("Otter rejected the upload (status 403)")


# --------------------------------------------------------------------------- #
def test_audio_detection():
    assert app.is_audio_file({"name": "a.mp3", "filetype": "mp3"})
    assert app.is_audio_file({"name": "x", "mimetype": "audio/mpeg"})
    assert app.is_audio_file({"name": "clip.m4a"})
    assert not app.is_audio_file({"name": "notes.pdf", "filetype": "pdf"})
    assert not app.is_audio_file({"name": "photo.png", "mimetype": "image/png"})


def test_buttons_cover_all_accounts():
    blocks = app.account_buttons("F1", "interview.mp3", "U1")
    labels = [e["text"]["text"] for e in blocks[1]["elements"]]
    assert labels == ["Jr reporters", "Sr reporters", "Confidential"]
    conf = [e for e in blocks[1]["elements"] if e["action_id"] == "pick:conf"][0]
    assert conf["style"] == "danger"


def test_conf_gate():
    assert config.conf_allowed("UALLOWED")
    assert not config.conf_allowed("USTRANGER")


def test_modal_has_file_input_and_all_accounts():
    modal = app.build_modal("C123")
    assert modal["callback_id"] == "otter_modal"
    assert modal["private_metadata"] == "C123"
    elems = {b["block_id"]: b["element"]["type"] for b in modal["blocks"]}
    assert elems["otter_file"] == "file_input"
    assert elems["otter_acct"] == "static_select"
    opts = [o["value"] for b in modal["blocks"] if b["block_id"] == "otter_acct"
            for o in b["element"]["options"]]
    assert opts == ["jr", "sr", "conf"]


def test_parse_modal_extracts_file_and_account():
    view = {"state": {"values": {
        "otter_file": {"file": {"files": [{"id": "F9", "name": "a.mp3"}]}},
        "otter_acct": {"acct": {"selected_option": {"value": "conf"}}},
    }}}
    assert app.parse_modal(view) == ("F9", "conf")
    # empty submission -> (None, None), handled as a validation error upstream
    assert app.parse_modal({"state": {"values": {}}}) == (None, None)


def test_happy_path_posts_transcript_and_uses_right_account():
    slack = run("sr", "UANYONE")
    assert FakeUploader.last.email == "sr@x"                         # routed to Sr account
    assert FakeUploader.last.uploaded["bytes"] == b"FAKE-AUDIO-BYTES"
    assert FakeUploader.last.uploaded["content_type"] == "audio/mpeg"  # mp3 mime
    assert slack.uploaded_files, "transcript should be posted back"
    assert slack.uploaded_files[0]["filename"] == "interview.docx"
    assert slack.uploaded_files[0]["thread_ts"] == "100.0"
    assert any("✅" in t for _, t in slack.updates)
    # audio temp file must be gone afterwards
    assert not os.path.exists(FakeUploader.last.uploaded["path"]), "temp audio must be deleted"


def test_confidential_suppresses_link_but_still_transcribes():
    slack = run("conf", "UALLOWED")
    assert FakeUploader.last.email == "conf@x"
    assert slack.uploaded_files, "confidential transcript still returned to the same thread"
    # CONF_POST_LINK defaults false -> no deep link echoed
    assert not any("otter.ai/u/" in t for _, t in slack.messages)


def test_failure_tells_reporter_to_do_it_manually_and_cleans_up():
    slack = run("jr", "UANYONE", fail=True)
    assert any("manually" in t for _, t in slack.updates)
    assert not slack.uploaded_files
    assert not os.path.exists(FakeUploader.last.uploaded["path"] if FakeUploader.last.uploaded else "/nonexistent")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
