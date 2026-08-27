# Page watch

`watchlist.json` is the list of URLs the watcher checks for **main-text changes**.
The `Crimson page watch` workflow sweeps it every ~2 hours; when a page's content
changes it posts a diff to Slack. Boilerplate (scripts, nav, header/footer) is
stripped, so ad/timestamp churn doesn't fire — best on specific content pages, not
busy homepages.

## Format
A JSON array. Each item is either a bare URL string or an object:

```json
[
  "https://provost.harvard.edu/some-policy-page",
  { "url": "https://example.harvard.edu/statements",
    "label": "Harvard statements page",
    "added_by": "jsmith" }
]
```

- `url` (required) — the page to watch
- `label` (optional) — friendly name shown in the alert
- `added_by` (optional) — who requested it (the `/watch` command fills this in)

Reporters normally add entries with the **`/watch <url>`** Slack command, which
edits this file. You can also edit it by hand.

First time a URL is seen, the watcher records a baseline (no alert); it alerts on
the next change after that.
