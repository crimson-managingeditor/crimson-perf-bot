// Cloudflare Worker — Slack /link commands -> edits watch/watchlist.json in GitHub.
//
//   /link <url> <X>m [css=<selector>] [ignore=<regex>]
//        watch a page (checked every X min = 5/30/60/120); alerts post in THIS channel.
//        css=  watch only the part matching a CSS selector (e.g. css=.article-body)
//        ignore=  drop lines matching a regex before diffing (kills timestamp noise)
//   /unlink <url>   stop watching it in this channel
//   /links          list what's watched in THIS channel (only)
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
      return reply(await handle(env, p));
    } catch (e) {
      return reply({ text: `⚠️ ${e.message}` });
    }
  }
};

function reply(msg) {
  return new Response(JSON.stringify({ response_type: "ephemeral", ...msg }),
    { headers: { "Content-Type": "application/json" } });
}
function parseInterval(tok) {
  if (!tok) return null;
  const n = parseInt(String(tok).replace(/m$/i, ""), 10);
  return ALLOWED.includes(n) ? n : NaN;
}
// Slack wraps typed URLs as <url> or <url|label>; add https:// for bare domains.
function cleanUrl(tok) {
  if (!tok) return "";
  const m = tok.match(/^<(.+?)(?:\|.*)?>$/);
  let u = (m ? m[1] : tok).trim();
  if (u && !/^https?:\/\//i.test(u) && /^[^\s/]+\.[^\s/]+/.test(u)) u = "https://" + u;
  return u;
}

async function handle(env, p) {
  const command = p.get("command") || "";
  const text = (p.get("text") || "").trim();
  const tokens = text.split(/\s+/).filter(Boolean);
  const ivTok = tokens.slice(1).find(x => /^\d+m?$/i.test(x));
  const ctx = {
    url: cleanUrl(tokens[0]),
    interval: ivTok ? parseInterval(ivTok) : null,
    css: (text.match(/\bcss=(.+?)(?=\s+\w+=|$)/i) || [, ""])[1].trim(),
    ignore: (text.match(/\bignore=(.+?)(?=\s+\w+=|$)/i) || [, ""])[1].trim(),
    channel: p.get("channel_id") || "",
    channel_name: p.get("channel_name") || "",
    user: p.get("user_name") || p.get("user_id") || "someone",
    userId: p.get("user_id") || "",
  };
  if (command === "/links")  return await listCmd(env, ctx);
  if (command === "/unlink") return await mutate(env, "remove", ctx);
  if (command === "/link")   return await mutate(env, "add", ctx);
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
  const content = btoa(unescape(encodeURIComponent(JSON.stringify(list, null, 2) + "\n")));
  return fetch(`${GH}/repos/${env.GITHUB_REPO}/contents/${encodeURI(path)}`, {
    method: "PUT", headers: ghHeaders(env),
    body: JSON.stringify(sha ? { message, content, sha } : { message, content }) });
}
const urlOf = e => (typeof e === "string" ? e : e.url);
const chanOf = e => (typeof e === "object" ? (e.channel || "") : "");
const sameWatch = (e, url, channel) => urlOf(e) === url && chanOf(e) === (channel || "");

// read-modify-write with one retry on sha conflict
async function mutate(env, op, c) {
  if (!/^https?:\/\//i.test(c.url))
    return { text: "Usage: `/link thecrimson.com 30m [css=<selector>] [ignore=<regex>]`  (interval = 5, 30, 60, 120)" };
  let interval = c.interval;
  if (op === "add") {
    if (Number.isNaN(interval)) return { text: "Interval must be `5m`, `30m`, `60m`, or `120m`." };
    if (interval === null) interval = DEFAULT_INTERVAL;
  }
  const where = c.channel_name && c.channel_name !== "directmessage" ? `#${c.channel_name}` : "here";
  for (let attempt = 0; attempt < 2; attempt++) {
    const { list, sha } = await ghGet(env);
    let msg, done;
    if (op === "add") {
      const filt = c.css ? ` · watching \`${c.css}\`` : "";
      const rec = { url: c.url, channel: c.channel, channel_name: c.channel_name,
                    added_by: c.user, added_by_id: c.userId, interval };
      if (c.css) rec.css = c.css;
      if (c.ignore) rec.ignore = c.ignore;
      const i = list.findIndex(e => sameWatch(e, c.url, c.channel));
      if (i >= 0) {
        rec.added_by = (typeof list[i] === "object" && list[i].added_by) || c.user;
        rec.added_by_id = (typeof list[i] === "object" && list[i].added_by_id) || c.userId;
        list[i] = rec;
        msg = `watch: update ${c.url} @${interval}m in ${c.channel} (by ${c.user})`;
        done = { text: `🔁 Updated <${c.url}> — every ${interval}m, alerts ${where}${filt}.` };
      } else {
        list.push(rec);
        msg = `watch: add ${c.url} @${interval}m in ${c.channel} (by ${c.user})`;
        done = { text: `✅ Watching <${c.url}> — every ${interval}m, alerts ${where}${filt}.` };
      }
    } else {
      const next = list.filter(e => !sameWatch(e, c.url, c.channel));
      if (next.length === list.length) return { text: `Not watched in this channel: ${c.url}` };
      list.length = 0; list.push(...next);
      msg = `watch: remove ${c.url} from ${c.channel} (by ${c.user})`;
      done = { text: `🗑️ Stopped watching ${c.url} in this channel.` };
    }
    const put = await ghPut(env, list, sha, msg);
    if (put.ok) return done;
    if (put.status !== 409) throw new Error(`GitHub write failed (${put.status})`);
  }
  throw new Error("watchlist was busy — try again in a moment");
}

async function listCmd(env, c) {
  const { list } = await ghGet(env);
  const here = list.filter(e => chanOf(e) === (c.channel || ""));
  if (!here.length)
    return { text: "Nothing is being watched in this channel yet. Add one with `/link <url> <interval>`." };
  const shown = here.slice(0, 50).map(e => {
    const iv = (typeof e === "object" && e.interval) ? ` — every ${e.interval}m` : "";
    const cs = (typeof e === "object" && e.css) ? `  \`${e.css}\`` : "";
    return `• ${urlOf(e)}${iv}${cs}`;
  }).join("\n");
  const more = here.length > 50 ? `\n…and ${here.length - 50} more` : "";
  const where = c.channel_name && c.channel_name !== "directmessage" ? `#${c.channel_name}` : "this channel";
  return { text: `*Watching ${here.length} page(s) in ${where}:*\n${shown}${more}` };
}
