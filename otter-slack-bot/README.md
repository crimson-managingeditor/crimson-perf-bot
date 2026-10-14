# Otter Uploader — Slack bot

A reporter drops an audio file into Slack, taps **Jr reporters / Sr reporters / Confidential**,
and the bot uploads it to the matching Otter.ai account for them — then posts the finished
transcript back into the thread. It automates exactly what an editor does by hand today.

```
reporter drags interview.mp3 into Slack
        │
        ▼
🎙️ interview.mp3 — which Otter account?   [ Jr reporters ] [ Sr reporters ] [ Confidential ]
        │ (taps Sr reporters)
        ▼
✅ Uploaded to Sr reporters Otter — transcribing now…
        ▼
📝 Transcript ready — Sr reporters Otter      interview.docx
```

## How it works under the hood
Otter has **no public upload API** except on Enterprise plans, so this uses the community
[`otterai-api`](https://github.com/gmchad/otterai-api) library, which logs into an account
with its **email + password** and uploads the file the same way the website does. That has
two consequences worth knowing:
- The three account passwords are stored as **secrets on the host** (never in this repo).
- Because it rides Otter's private endpoints, Otter *could* change them and break uploads. The
  bot fails **loudly** — it tells the reporter "please upload manually this time" — it never
  silently drops a file.

The accounts must use **email + password login with 2FA off**. If an account uses "Sign in with
Google," set a real Otter password on it first (Otter account settings) or this can't log in.

---

## 1. Create the Slack app

Go to **https://api.slack.com/apps → Create New App → From an app manifest**, pick the
workspace, and paste this (YAML):

```yaml
display_information:
  name: Otter Uploader
  description: Upload audio to Otter.ai from Slack
features:
  bot_user:
    display_name: otter-uploader
    always_online: true
oauth_config:
  scopes:
    bot:
      - chat:write        # post the buttons + status messages
      - files:read        # download the audio the reporter uploaded
      - files:write       # post the finished transcript back
      - channels:history   # receive uploads in public channels it's invited to
      - groups:history     # ... in private channels
      - im:history         # ... in DMs to the bot
      - mpim:history       # ... in group DMs
settings:
  event_subscriptions:
    bot_events:
      - message.channels
      - message.groups
      - message.im
      - message.mpim
  interactivity:
    is_enabled: true       # the Jr/Sr/Confidential buttons
  socket_mode_enabled: true
  org_deploy_enabled: false
```

Then:
1. **Install to Workspace** (left sidebar → *Install App*). Copy the **Bot User OAuth Token**
   (`xoxb-…`) → that's `SLACK_BOT_TOKEN`.
2. **Basic Information → App-Level Tokens → Generate Token**. Name it `socket`, add the
   **`connections:write`** scope, generate. Copy the `xapp-…` token → that's `SLACK_APP_TOKEN`.
3. **Invite the bot** to the channel(s) you'll use: `/invite @otter-uploader`. (Reporters can
   also just DM the bot the file.)

---

## 2. Deploy (Render — recommended)

A Socket Mode bot needs no public URL, so the simplest home is a Render **Background Worker**.

1. Push this folder to a **private** GitHub repo.
2. Render → **New → Blueprint** → pick the repo (it reads `render.yaml`).
3. In the service's **Environment**, set the secrets (see `.env.example` for the full list):
   `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `OTTER_JR_EMAIL/PASSWORD`, `OTTER_SR_EMAIL/PASSWORD`,
   `OTTER_CONF_EMAIL/PASSWORD`, `CONF_ALLOWLIST`.
4. Deploy. Logs should show `otter-slack-bot starting (accounts: jr, sr, conf)`.

<details>
<summary>Alternative: Fly.io</summary>

```bash
fly launch --no-deploy --copy-config --name otter-slack-bot
fly secrets set SLACK_BOT_TOKEN=... SLACK_APP_TOKEN=... \
  OTTER_JR_EMAIL=... OTTER_JR_PASSWORD=... OTTER_SR_EMAIL=... OTTER_SR_PASSWORD=... \
  OTTER_CONF_EMAIL=... OTTER_CONF_PASSWORD=... CONF_ALLOWLIST=U012,U345
fly deploy && fly scale count 1
```
</details>

<details>
<summary>Run locally (to test before deploying)</summary>

```bash
pip install -r requirements.txt
cp .env.example .env     # fill it in
set -a; source .env; set +a
python app.py
```
</details>

---

## 3. The Confidential account — read this

- Only Slack users whose IDs are in **`CONF_ALLOWLIST`** can use the Confidential button.
  Everyone else gets a private "not enabled for you" reply. **An empty allowlist means nobody
  can use it** (safe default). Get an ID: click the person → **⋯ → Copy member ID** (`U0123…`).
- The transcript comes back into the **same thread** the reporter posted the audio to — same
  visibility as the audio they already shared. The Otter *account* deep-link is **not** echoed
  for Confidential (`CONF_POST_LINK=false`).
- The audio file is **deleted from the host** the moment it reaches Otter, success or failure.
- Consider a dedicated private channel for confidential source audio and inviting only the bot
  and the allowlisted people, so routing mistakes aren't possible.

---

## 4. First live run (one-time)
Because this talks to Otter's private API, do one real upload per account and watch the Render
logs the first time. If a transcript doesn't post, the logs print `otter speech keys: [...]`
and the raw upload response — that tells us if Otter renamed a field, which is a one-line fix in
`otter_client.py`. The upload itself (the core job) is independent of that and will still work.

## Config reference
Everything is env-driven; see [`.env.example`](.env.example). Tests: `python tests/test_flow.py`.
