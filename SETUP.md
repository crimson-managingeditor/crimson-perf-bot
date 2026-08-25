# Crimson performance bot — setup (~30 min, one time)

Daily + weekly Slack reports on which News articles are performing. Runs free on
GitHub Actions. Three one-time setups: Slack webhook, GA4 service account, GitHub secrets.

---

## 1. Slack webhook (5 min)
1. https://api.slack.com/apps → **Create New App** → *From scratch* → name it "Crimson Stats", pick your workspace.
2. Left nav → **Incoming Webhooks** → toggle **On**.
3. **Add New Webhook to Workspace** → choose the channel (e.g. `#analytics`) → **Allow**.
4. Copy the webhook URL (`https://hooks.slack.com/services/T…/B…/…`). Keep it secret.

## 2. GA4 service account (15 min)
1. https://console.cloud.google.com → create/pick a project.
2. **APIs & Services → Library** → search **Google Analytics Data API** → **Enable**.
3. **APIs & Services → Credentials → Create credentials → Service account** → name it, **Done**.
4. Click the service account → **Keys → Add key → Create new key → JSON** → downloads a `.json`. Keep it secret.
5. Copy the service account's email (`…@….iam.gserviceaccount.com`).
6. In **GA4** (analytics.google.com): Admin → **Property Access Management** → **+** → paste that email → role **Viewer** → save.
7. Get your **Property ID**: GA4 Admin → **Property details** → the number under the name (e.g. `123456789`). Just the number — not "G-XXXX".

## 3. Put it on GitHub (10 min)
1. Create a **private** GitHub repo. Copy the whole `crimson_metrics/slackbot/` folder into it
   (keep the `.github/workflows/crimson-bot.yml` path).
2. Repo → **Settings → Secrets and variables → Actions → New repository secret**, add three:
   - `SLACK_WEBHOOK_URL`  → the webhook from step 1
   - `GA4_PROPERTY_ID`    → the number from step 2.7
   - `GA4_CREDENTIALS_JSON` → **paste the entire contents** of the `.json` key file
3. Repo → **Actions** tab → enable workflows if prompted.
4. **Test it:** Actions → "Crimson performance bot" → **Run workflow** → mode `daily` → Run.
   A post should land in your Slack channel within a minute.

Done. It now posts **daily ~9am ET (Mon–Sat)** and a **weekly rollup Monday ~9:30am ET**.

---

## Test locally first (optional, recommended)
```bash
cd crimson_metrics/slackbot
pip install -r requirements.txt
export GA4_PROPERTY_ID=123456789
export GA4_CREDENTIALS_JSON=/path/to/key.json      # path OR paste the JSON
export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
python report.py daily --dry-run     # prints to console, does NOT post
python report.py daily               # actually posts
python report.py weekly --dry-run
```

## Tweaks
- **Times:** edit the `cron:` lines in `.github/workflows/crimson-bot.yml` (UTC; Boston = UTC-4/-5).
- **What counts as "fresh":** `report.py` → `daily()` uses articles published in the last 2 days with ≥25 views. Change the `<= 2` / `>= 25`.
- **Section:** it focuses on News; remove the `section == "News"` filter to include everything.
- **Metrics shown:** views + read-time now. Recirculation would need a Chartbeat feed (your key is real-time only today — ask your Chartbeat admin for the Historical/Query API to add it).

## Notes / caveats
- GA4 "yesterday" data is usually final by ~9am ET but can wiggle for a few hours; the weekly (T-1 to T-7) is stable.
- Read-time here is GA4 engagement/user, a rougher proxy than Chartbeat engaged-minutes.
- The bot enriches each article via the Crimson GraphQL API (title/section/byline/scoop) — no key needed.
- Keep the repo **private**: the GA4 key in secrets is encrypted, but don't commit the raw `.json`.
