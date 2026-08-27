// Cloudflare Worker — Slack /link commands -> edits watch/watchlist.json in GitHub.
//
// Slash commands (all point their Request URL at this Worker):
//   /link <url> <X>m   watch a page, checked every X min (X = 5, 30, 60, or 120)
//   /unlink <url>      stop watching a page
//   /links             list what's watched
// Re-running /link on a URL already watched just changes its interval.
//
// Cloudflare secrets: SLACK_SIGNING_SECRET, GITHUB_TOKEN,
//   GITHUB_REPO (e.g. crimson-managingeditor/crimson-perf-bot), WATCHLIST_PATH.
const GH = "https://api.github.com";
const ALLOWED = [5, 30, 60, 120];
const DEFAULT_INTERVAL = 60;

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("Crimson Watch command endpoint");
    const body = await request.text();
    const ok = await verifySlack(env.SLACK_SIGNING_SECRET,
      request.headers.get("X-Slack-Request-Timestamp"),
      body, request.headers.get("X-Slack-Signature"));
    if (!ok) return reply({ text: "⛔ Slack signature check failed." });
    const p = new URLSearchParams(body);
    try {
      return reply(await handle(env, p));   // do the work and answer directly (private)
    } catch (e) {
      return reply({ text: `⚠️ ${e.message}` });
    }
  }
};

function reply(msg) {
  // ephemeral = only the person who ran the command ever sees it
  return new Response(JSON.stringify({ response_type: "ephemeral", ...msg }),
    { headers: { "Content-Type": "application/json" } });
}
function parseInterval(tok) {
  if (!tok) return null;
  const n = parseInt(String(tok).replace(/m$/i, ""), 10);
  return ALLOWED.includes(n) ? n : NaN;
}

async function handle(env, p) {
  const command = p.get("command") || "";
  const parts = (p.get("text") || "").trim().split(/\s+/).filter(Boolean);
  const url = parts[0] || "";
  const interval = parseInterval(parts[1]);
  const user = p.get("user_name") || p.get("user_id") || "someone";
  const userId = p.get("user_id") || "";       // stored so the watcher can DM the flagger
  if (command === "/links")  return await listCmd(env);
  if (command === "/unlink") return await mutate(env, "remove", url, user, userId, null);
  if (command === "/link")   return await mutate(env, "add", url, user, userId, interval);
  return { text: `Unknown command ${command}` };
}

// --- Slack request signature (v0 HMAC-SHA256, 5-min replay window) ---
async function verifySlack(secret, ts, body, sig) {
  if (!secret || !ts || !sig) return false;
  if (Math.abs(Date.now() / 1000 - Number(ts)) > 300) return false;
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, enc.encode(`v0:${ts}:${body}`));
  const hex = [...new Uint8Array(mac)].map(b => b.toString(16).padStart(2, "0")).join("");
  const mine = `v0=${hex}`;
  if (mine.length !== sig.length) return false;
  let d = 0; for (let i = 0; i < mine.length; i++) d |= mine.charCodeAt(i) ^ sig.charCodeAt(i);
  return d === 0;
}

// --- GitHub Contents API ---
function ghHeaders(env) {
  return { "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
           "Accept": "application/vnd.github+json",
           "User-Agent": "crimson-watch-worker",
           "Content-Type": "application/json" };
}
async function ghGet(env) {
  const path = env.WATCHLIST_PATH || "watch/watchlist.json";
  const r = await fetch(`${GH}/repos/${env.GITHUB_REPO}/contents/${encodeURI(path)}`,
    { headers: ghHeaders(env) });
  if (r.status === 404) return { list: [], sha: null };
  if (!r.ok) throw new Error(`GitHub read failed (${r.status})`);
  const j = await r.json();
  const list = JSON.parse(atob(j.content.replace(/\n/g, "")));
  return { list: Array.isArray(list) ? list : [], sha: j.sha };
}
async function ghPut(env, list, sha, message) {
  const path = env.WATCHLIST_PATH || "watch/watchlist.json";
  const content = btoa(JSON.stringify(list, null, 2) + "\n");
  return fetch(`${GH}/repos/${env.GITHUB_REPO}/contents/${encodeURI(path)}`, {
    method: "PUT", headers: ghHeaders(env),
    body: JSON.stringify(sha ? { message, content, sha } : { message, content }) });
}
const urlOf = e => (typeof e === "string" ? e : e.url);

// read-modify-write with one retry if another edit lands first (sha conflict)
async function mutate(env, op, url, user, userId, interval) {
  if (!/^https?:\/\//i.test(url))
    return { text: "Usage: `/link https://example.com/page 30m`  (interval = 5, 30, 60, or 120)" };
  if (op === "add") {
    if (Number.isNaN(interval)) return { text: "Interval must be `5m`, `30m`, `60m`, or `120m`." };
    if (interval === null) interval = DEFAULT_INTERVAL;
  }
  for (let attempt = 0; attempt < 2; attempt++) {
    const { list, sha } = await ghGet(env);
    let msg, done;
    if (op === "add") {
      const i = list.findIndex(e => urlOf(e) === url);
      if (i >= 0) {
        const prevIv = (typeof list[i] === "object" && list[i].interval) || null;
        if (prevIv === interval) return { text: `Already watching <${url}> every ${interval}m.` };
        const added_by = (typeof list[i] === "object" && list[i].added_by) || user;
        const added_by_id = (typeof list[i] === "object" && list[i].added_by_id) || userId;
        list[i] = { url, added_by, added_by_id, interval };
        msg = `watch: set ${url} to ${interval}m (via /link by ${user})`;
        done = { text: `🔁 Updated: watching <${url}> every ${interval}m now.` };
      } else {
        list.push({ url, added_by: user, added_by_id: userId, interval });
        msg = `watch: add ${url} @${interval}m (via /link by ${user})`;
        done = { text: `✅ Watching <${url}> — checking every ${interval}m. I'll DM you if it changes.` };
      }
    } else {
      const next = list.filter(e => urlOf(e) !== url);
      if (next.length === list.length) return { text: `Not on the list: ${url}` };
      list.length = 0; list.push(...next);
      msg = `watch: remove ${url} (via /unlink by ${user})`;
      done = { text: `🗑️ Stopped watching ${url}.` };
    }
    const put = await ghPut(env, list, sha, msg);
    if (put.ok) return done;
    if (put.status !== 409) throw new Error(`GitHub write failed (${put.status})`);
  }
  throw new Error("watchlist was busy — try again in a moment");
}

async function listCmd(env) {
  const { list } = await ghGet(env);
  if (!list.length) return { text: "Watchlist is empty." };
  const shown = list.slice(0, 50).map(e => {
    const iv = (typeof e === "object" && e.interval) ? ` — every ${e.interval}m` : "";
    return `• ${urlOf(e)}${iv}`;
  }).join("\n");
  const more = list.length > 50 ? `\n…and ${list.length - 50} more` : "";
  return { text: `*Watching ${list.length} page(s):*\n${shown}${more}` };
}
