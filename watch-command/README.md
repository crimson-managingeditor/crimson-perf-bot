# `/watch` slash command — setup

`worker.js` is a Cloudflare Worker that receives Slack slash commands and edits
`watch/watchlist.json` in this repo. The page-watch workflow already reads that file.

```
Reporter types  /link <url> 30m  in Slack
      → Slack POSTs to the Worker
      → Worker verifies Slack + edits watch/watchlist.json via the GitHub API
      → the Crimson page watch workflow picks it up and checks it every 30 min
```

Commands:
- `/link <url> <X>m` — watch a page, checked every X minutes (**X = 5, 30, 60, or 120**; omit → 60). Re-running `/link` on the same URL changes its interval.
- `/unlink <url>` — stop watching
- `/links` — list what's watched (only you see it)
- `/save <url>` — preserve a page in the Wayback Machine and get a permalink back (cite it, or grab it before it changes/disappears). Acks instantly, then posts the permalink a few seconds later. No GitHub involved — it talks to archive.org directly.

## 1. GitHub token (Contents: write on this repo)
Fine-grained token at **github.com/settings/personal-access-tokens/new**:
- Resource owner: **crimson-managingeditor** (approve if the org prompts)
- Repository access: **Only select repositories → crimson-perf-bot**
- Permissions → Repository → **Contents: Read and write**
- Generate, copy. (If a fine-grained token can't target the org repo, a classic
  token with the `repo` scope works too — it's just broader.)

## 2. Slack app
**api.slack.com/apps → Create New App → From scratch** → name "Crimson Watch",
pick the workspace.
- **Basic Information → App Credentials →** copy the **Signing Secret**.
- (Set up the slash commands in step 4, after you have the Worker URL.)

## 3. Deploy the Worker
**dash.cloudflare.com → Workers & Pages → Create → Worker.** Name it
`crimson-watch`, deploy the default, then **Edit code**, paste all of `worker.js`,
**Deploy**. Copy the Worker URL (`https://crimson-watch.<you>.workers.dev`).

**Settings → Variables and Secrets →** add four **Secrets**:
| Name | Value |
|---|---|
| `SLACK_SIGNING_SECRET` | from step 2 |
| `GITHUB_TOKEN` | from step 1 |
| `GITHUB_REPO` | `crimson-managingeditor/crimson-perf-bot` |
| `WATCHLIST_PATH` | `watch/watchlist.json` |
| `WAYBACK_KEY` *(optional)* | `accesskey:secret` from https://archive.org/account/s3.php — set it for guaranteed-fresh `/save` captures; `/save` still works without it |

Re-deploy after adding secrets.

## 4. Point Slack at the Worker
In the Slack app → **Slash Commands → Create New Command**, once per command:
| Command | Request URL | Short description | Usage hint |
|---|---|---|---|
| `/link` | the Worker URL | Watch a page for changes | `<url> 30m` |
| `/unlink` | the Worker URL | Stop watching a page | `<url>` |
| `/links` | the Worker URL | Show watched pages | |
| `/save` | the Worker URL | Archive a page to the Wayback Machine | `<url>` |

Then **Install App** (Basic Information → Install to Workspace).

## 5. Test
In Slack: `/link https://www.harvard.edu/ 30m` → you should see
"👀 Now checking … every 30m". Confirm the commit landed in `watch/watchlist.json`.
