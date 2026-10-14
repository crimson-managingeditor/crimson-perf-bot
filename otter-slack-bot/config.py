"""All configuration comes from environment variables so nothing sensitive lives in the repo.
See .env.example for the full list. Only SLACK_* and at least one OTTER_* account are required."""
import os

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")   # xapp- token, for Socket Mode

# --- the three Otter accounts -------------------------------------------------
# key -> {label shown on the Slack button, email, password}. An account with no
# email configured is simply hidden (so you can start with one and add the rest).
_RAW_ACCOUNTS = {
    "jr":   ("Jr reporters",  "OTTER_JR_EMAIL",   "OTTER_JR_PASSWORD"),
    "sr":   ("Sr reporters",  "OTTER_SR_EMAIL",   "OTTER_SR_PASSWORD"),
    "conf": ("Confidential",  "OTTER_CONF_EMAIL", "OTTER_CONF_PASSWORD"),
}
ACCOUNTS = {
    key: {"label": label, "email": os.environ.get(e_env, ""), "password": os.environ.get(p_env, "")}
    for key, (label, e_env, p_env) in _RAW_ACCOUNTS.items()
    if os.environ.get(e_env)
}

# --- confidential guardrails --------------------------------------------------
# Only these Slack user IDs may send audio to the confidential account.
CONF_ALLOWLIST = {u.strip() for u in os.environ.get("CONF_ALLOWLIST", "").split(",") if u.strip()}
# When true (default), an empty allowlist means NOBODY can use the confidential
# account yet — safer than defaulting it open for source recordings.
CONF_REQUIRE_ALLOWLIST = os.environ.get("CONF_REQUIRE_ALLOWLIST", "true").lower() != "false"

def conf_allowed(slack_user_id: str) -> bool:
    if CONF_ALLOWLIST:
        return slack_user_id in CONF_ALLOWLIST
    return not CONF_REQUIRE_ALLOWLIST

# --- behaviour ----------------------------------------------------------------
# After upload, fetch the finished transcript and post it back into the thread.
POST_TRANSCRIPT = os.environ.get("POST_TRANSCRIPT", "true").lower() != "false"
# Same, but specifically for the confidential account (transcript lands in the
# same thread the reporter already posted the audio to — same visibility).
CONF_POST_TRANSCRIPT = os.environ.get("CONF_POST_TRANSCRIPT", "true").lower() != "false"
# Echo the Otter account deep-link into Slack. Off for confidential by default.
CONF_POST_LINK = os.environ.get("CONF_POST_LINK", "false").lower() == "true"

TRANSCRIPT_FORMAT = os.environ.get("TRANSCRIPT_FORMAT", "docx")   # docx | txt | pdf | srt
POLL_INTERVAL = int(os.environ.get("OTTER_POLL_INTERVAL", "20"))   # seconds between checks
POLL_TIMEOUT = int(os.environ.get("OTTER_POLL_TIMEOUT", "2400"))   # give up after 40 min
OTTER_SPEECH_URL = "https://otter.ai/u/{otid}"

# Audio/video extensions we'll offer to upload. Otter accepts a/v files.
AUDIO_EXTS = {
    "mp3", "m4a", "wav", "aac", "ogg", "oga", "opus", "flac", "wma", "aiff", "aif",
    "mp4", "mov", "m4v", "webm", "mpeg", "mpga",
}
_CONTENT_TYPES = {
    "mp3": "audio/mpeg", "mpga": "audio/mpeg", "mpeg": "audio/mpeg",
    "m4a": "audio/mp4", "mp4": "audio/mp4", "m4v": "audio/mp4", "aac": "audio/aac",
    "wav": "audio/wav", "flac": "audio/flac", "ogg": "audio/ogg", "oga": "audio/ogg",
    "opus": "audio/opus", "wma": "audio/x-ms-wma", "aiff": "audio/aiff", "aif": "audio/aiff",
    "mov": "video/quicktime", "webm": "video/webm",
}

def content_type_for(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _CONTENT_TYPES.get(ext, "audio/mp4")   # Otter's own default fallback

def post_transcript(account_key: str) -> bool:
    return CONF_POST_TRANSCRIPT if account_key == "conf" else POST_TRANSCRIPT

def post_link(account_key: str) -> bool:
    return CONF_POST_LINK if account_key == "conf" else True
