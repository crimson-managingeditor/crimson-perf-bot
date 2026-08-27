// Cloudflare Worker — Slack /link commands -> edits watch/watchlist.json in GitHub.
//
// Slash commands (all point their Request URL at this Worker):
//   /link <url> <X>m   watch a page, checked every X min (X = 5, 30, 60, or 120)
//   /unlink <url>      stop watching a page
//   /links             list what's watched (only you see it)
//
// Re-running /link on a URL already watched just changes its interval.
//
// Cloudflare secrets to set (Settings -> Variables and Secrets):
//   SLACK_SIGNING_SECRET   from the Slack app's Basic Information
//   GITHUB_TOKEN           token with Contents:write on the repo
//   GITHUB_REPO            e.g. crimson-managingeditor/crimson-perf-bot
//   WATCHLIST_PATH         watch/watchlist.json   (optional; this is the default)
const GH = "https://api.github.com";
const ALLOWED = [5, 30, 60, 120];
const DEFAULT_INTERVAL = 60;

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") return new Response("Crimson Watch command endpoint");
    const body = await request.text();
    const ok = await verifySlack(env.SLACK_SIGNING_SECRET,
      request.headers.get("X-Slack-Request-Timestamp"),
      body, request.headers.get("X-Slack-Signature"));
    if (!ok) return reply({ text: "⛔ Slack signature check failed." }, "ephemeral");

    const p = new URLSearchParams(body);
    const responseUrl = p.get("response_url");
    // Ack within Slack's 3s window, then do the GitHub work in the background and
    // post the real result back via response_url (never times out).
    ctx.waitUntil(
      handle(env, p)
        .catch(e => ({ response_type: "ephemeral", text: `⚠️ ${e.message}` }))
        .then(msg => postToSlack(responseUrl, msg))
    );
    return reply({ text: "⏳ working…" }, "ephemeral");
  }
};

async function handle(env, p) {
  const command = p.get("command") || "";
  const parts = (p.get("text") || "").trim().split(/\s+/).filter(Boolean);
  const url = parts[0] || "";
  const interval = parseInterval(parts[1]);   // number, null (unspecified), or NaN (bad)
  const user = p.get("user_name") || p.get("user_id") || "someone";
  if (command === "/links")  return { response_type: "ephemeral", ...(await listCmd(env)) };
  if (command === "/unlink") return await mutate(env, "remove", url, user, null);
  if (command === "/link")   return await mutate(env, "add", url, user, interval);
  return { response_type: "ephemeral", text: `Unknown command ${command}` };
}

async function postToSlack(responseUrl, msg) {
  if (!responseUrl) return;
  const payload = msg.response_type ? msg : { response_type: "in_channel", ...msg };
  await fetch(responseUrl, { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
}

function reply(obj, response_type = "in_channel") {
  return new Response(JSON.stringify({ response_type, ...obj }),
    { headers: { "Content-Type": "application/json" } });
}
function parseInterval(tok) {
  if (!tok) return null;
  const n = parseInt(String(tok).replace(/m$/i, ""), 10);
  return ALLOWED.includes(n) ? n : NaN;
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
async function mutate(env, op, url, user, interval) {
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
        if (prevIv === interval) return { text: `Already checking ${url} every ${interval}m.` };
        const added_by = (typeof list[i] === "object" && list[i].added_by) || user;
        list[i] = { url, added_by, interval };
        msg = `watch: set ${url} to ${interval}m (via /link by ${user})`;
        done = { text: `🔁 Now checking <${url}> every ${interval}m.` };
      } else {
        list.push({ url, added_by: user, interval });
        msg = `watch: add ${url} @${interval}m (via /link by ${user})`;
        done = { text: `👀 Now checking <${url}> every ${interval}m — added by ${user}. First check within ~${interval}m.` };
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
