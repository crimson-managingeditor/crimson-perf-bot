# `/watch` slash command — setup

`worker.js` is a Cloudflare Worker that receives Slack slash commands and edits
`watch/watchlist.json` in this repo. The page-watch workflow already reads that file.

```
Reporter types  /watch <url>  in Slack
      → Slack POSTs to the Worker
      → Worker verifies Slack + edits watch/watchlist.json via the GitHub API
      → the Crimson page watch workflow (every 2h) picks it up
```

Commands: `/watch <url>`, `/unwatch <url>`, `/watchlist`.

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

Re-deploy after adding secrets.

## 4. Point Slack at the Worker
In the Slack app → **Slash Commands → Create New Command**, three times:
| Command | Request URL | Short description |
|---|---|---|
| `/watch` | the Worker URL | Watch a page for changes |
| `/unwatch` | the Worker URL | Stop watching a page |
| `/watchlist` | the Worker URL | Show watched pages |

Then **Install App** (Basic Information → Install to Workspace).

## 5. Test
In Slack: `/watch https://www.harvard.edu/` → you should see
"👀 Now watching …". Confirm the commit landed in `watch/watchlist.json`.
