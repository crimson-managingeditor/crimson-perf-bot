// Cloudflare Worker — Slack /watch commands -> edits watch/watchlist.json in GitHub.
//
// Slash commands (all point their Request URL at this Worker):
//   /watch <url>     add a page to the watchlist
//   /unwatch <url>   remove it
//   /watchlist       list what's watched (only you see it)
//
// Cloudflare secrets to set (Settings -> Variables and Secrets):
//   SLACK_SIGNING_SECRET   from the Slack app's Basic Information
//   GITHUB_TOKEN           token with Contents:write on the repo
//   GITHUB_REPO            e.g. crimson-managingeditor/crimson-perf-bot
//   WATCHLIST_PATH         watch/watchlist.json   (optional; this is the default)
const GH = "https://api.github.com";

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("Crimson Watch command endpoint");
    const body = await request.text();
    const ok = await verifySlack(env.SLACK_SIGNING_SECRET,
      request.headers.get("X-Slack-Request-Timestamp"),
      body, request.headers.get("X-Slack-Signature"));
    if (!ok) return reply({ text: "⛔ Slack signature check failed." }, "ephemeral");

    const p = new URLSearchParams(body);
    const command = p.get("command") || "";
    const arg = (p.get("text") || "").trim().split(/\s+/)[0] || "";
    const user = p.get("user_name") || p.get("user_id") || "someone";
    try {
      if (command === "/watchlist") return reply(await listCmd(env), "ephemeral");
      if (command === "/unwatch")   return reply(await mutate(env, "remove", arg, user));
      if (command === "/watch")     return reply(await mutate(env, "add", arg, user));
      return reply({ text: `Unknown command ${command}` }, "ephemeral");
    } catch (e) {
      return reply({ text: `⚠️ ${e.message}` }, "ephemeral");
    }
  }
};

function reply(obj, response_type = "in_channel") {
  return new Response(JSON.stringify({ response_type, ...obj }),
    { headers: { "Content-Type": "application/json" } });
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
async function mutate(env, op, url, user) {
  if (!/^https?:\/\//i.test(url))
    return { text: "Usage: `/watch https://example.com/page`" };
  for (let attempt = 0; attempt < 2; attempt++) {
    const { list, sha } = await ghGet(env);
    if (op === "add") {
      if (list.some(e => urlOf(e) === url)) return { text: `Already watching ${url}` };
      list.push({ url, added_by: user });
    } else {
      const next = list.filter(e => urlOf(e) !== url);
      if (next.length === list.length) return { text: `Not on the list: ${url}` };
      list.length = 0; list.push(...next);
    }
    const msg = op === "add" ? `watch: add ${url} (via /watch by ${user})`
                             : `watch: remove ${url} (via /unwatch by ${user})`;
    const put = await ghPut(env, list, sha, msg);
    if (put.ok) return { text: op === "add"
        ? `👀 Now watching <${url}> — added by ${user}. First check within ~2h.`
        : `🗑️ Stopped watching ${url}.` };
    if (put.status !== 409) throw new Error(`GitHub write failed (${put.status})`);
  }
  throw new Error("watchlist was busy — try again in a moment");
}

async function listCmd(env) {
  const { list } = await ghGet(env);
  if (!list.length) return { text: "Watchlist is empty." };
  const shown = list.slice(0, 50).map(e => `• ${urlOf(e)}`).join("\n");
  const more = list.length > 50 ? `\n…and ${list.length - 50} more` : "";
  return { text: `*Watching ${list.length} page(s):*\n${shown}${more}` };
}
