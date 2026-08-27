# Crimson Watch v2

A single always-on daemon (`app.py`) that **serves the Slack `/link` commands** *and*
**runs the change-detection scheduler**, backed by one **SQLite** database. Built to
scale to thousands of watches across many reporters — no giant-JSON parse, no repo
write-contention, and the watchlist is **private** (lives in the DB on your host, not a
public repo). Retires GitHub Actions + the Cloudflare Worker for watching.

```
Slack /link ─┐
             ├─► app.py (Flask :8787)  ──►  SQLite (watches + snapshots + changelog)
scheduler ───┘         ▲                         │
                       └── every ~15s: fetch due watches (rate-limited, render pool),
                           diff, alert to Slack, set next_check
```

## What you need
1. **A small always-on Linux host.** Any of: Oracle Cloud *Always Free* (4 ARM cores /
   24 GB RAM — ideal, free forever), a DigitalOcean droplet (free via the GitHub Student
   Pack $200 credit), or a box you already run. 1–2 GB RAM is enough for a few thousand
   text watches; add more if you render many JS pages.
2. **A public HTTPS URL** for Slack to reach the daemon — easiest is a **Cloudflare
   Tunnel** (free, no open ports).

## Deploy (Ubuntu)
```bash
sudo apt update && sudo apt install -y python3-venv git
git clone https://github.com/crimson-managingeditor/crimson-perf-bot.git
cd crimson-perf-bot/watcher-v2
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium      # only if you'll use render=

# secrets
cat > .env <<EOF
SLACK_SIGNING_SECRET=…            # Slack app → Basic Information
SLACK_BOT_TOKEN=xoxb-…            # Slack app → OAuth & Permissions
SLACK_WEBHOOK_URL=https://hooks…  # optional fallback
WORKERS=40
PER_DOMAIN=3
RENDER_WORKERS=2
EOF
set -a; . ./.env; set +a
python app.py     # test it runs; Ctrl-C, then use systemd below
```

## Run it as a service (systemd)
Copy `crimson-watch.service` to `/etc/systemd/system/`, edit the paths/user, then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now crimson-watch
journalctl -u crimson-watch -f          # logs
```

## Expose to Slack (Cloudflare Tunnel — free)
```bash
# install cloudflared, then either a quick tunnel (ephemeral URL, good for testing):
cloudflared tunnel --url http://localhost:8787
#   → prints https://<random>.trycloudflare.com
# …or a named tunnel bound to a domain for production (survives restarts).
```
Then in **api.slack.com/apps → Crimson Watch → Slash Commands**, set the Request URL for
`/link`, `/unlink`, `/links` to **`https://<your-tunnel>/slack`** (replacing the old
Cloudflare Worker URL). No app reinstall needed — same scopes.

*(Alternative to a tunnel: point a domain at the host and run `caddy reverse-proxy
--from watch.example.org --to localhost:8787` for automatic HTTPS.)*

## Scaling knobs (env)
| var | default | meaning |
|---|---|---|
| `WORKERS` | 40 | global concurrent fetches — raise for more throughput |
| `PER_DOMAIN` | 3 | max concurrent fetches per domain (politeness / anti-ban) |
| `RENDER_WORKERS` | 2 | concurrent headless renders (each ~300 MB RAM) |
| `TICK_SECONDS` | 15 | scheduler cycle |

Rough capacity on a 2-core / 4 GB box with mixed intervals: **several thousand text
watches** comfortably. Rendering is the heavy part — keep `render=` for pages that truly
need it. The scheduler pulls up to 1000 due watches per cycle and spreads work across
`WORKERS`, capped `PER_DOMAIN` per host.

## Ops
- **Backup:** the whole state is `watcher.db` — copy it (it's WAL-mode SQLite).
- **Inspect:** `sqlite3 watcher.db 'select url,interval_min,last_check from watches'`
- **Change log:** `sqlite3 watcher.db 'select ts,event,url,detail from changelog order by id desc limit 50'`
- **Commands:** `/link <url> <5|30|60|120>m [css= xpath= json= subtract= extract= ignore= trigger= sort dedupe render]`, `/unlink <url>`, `/links` (per-channel).

## Migrating from the MVP
The GitHub-Actions/Worker watcher and this daemon shouldn't both run. Once this is live:
disable the `Crimson page watch` workflow, remove the Cloudflare cron trigger, and point
the Slack slash commands at the tunnel. Reporters re-`/link` (or import the old
`watch/watchlist.json` into the DB with a short script).
