// Cloudflare Worker — Slack /link + /save commands.
//
//   /link <url> <X>m [css=<selector>] [ignore=<regex>]
//        watch a page (checked every X min = 5/30/60/120); alerts post in THIS channel.
//        css=  watch only the part matching a CSS selector (e.g. css=.article-body)
//        ignore=  drop lines matching a regex before diffing (kills timestamp noise)
//   /unlink <url>   stop watching it in this channel
//   /links          list what's watched in THIS channel (only)
//   /save <url>     preserve a page in the Wayback Machine and get a permalink back
//                   (for citing / before it changes or gets deleted).
//
// Cloudflare secrets: SLACK_SIGNING_SECRET, GITHUB_TOKEN,
//   GITHUB_REPO (e.g. crimson-managingeditor/crimson-perf-bot), WATCHLIST_PATH.
//   WAYBACK_KEY (optional) — "accesskey:secret" from https://archive.org/account/s3.php;
//   set it for guaranteed-fresh captures. Without it, /save still works (best-effort
//   capture + the latest snapshot), just without the reliability of an authenticated save.
const GH = "https://api.github.com";
const ALLOWED = [5, 30, 60, 120];
const DEFAULT_INTERVAL = 60;

export default {
  async fetch(request, env, exctx) {
    if (request.method !== "POST") return new Response("Crimson Watch command endpoint");
    const body = await request.text();
    const ok = await verifySlack(env.SLACK_SIGNING_SECRET,
      request.headers.get("X-Slack-Request-Timestamp"),
      body, request.headers.get("X-Slack-Signature"));
    if (!ok) return reply({ text: "⛔ Slack signature check failed." });
    const p = new URLSearchParams(body);
    try {
      return reply(await handle(env, exctx, p));
    } catch (e) {
      return reply({ text: `⚠️ ${e.message}` });
    }
  },
  // Cloudflare Cron Trigger (reliable, unlike GitHub cron) -> kick the GitHub
  // Actions page-watch workflow. Configure the schedule in the Cloudflare dashboard.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatchWatch(env));
  }
};

async function dispatchWatch(env) {
  // needs GITHUB_TOKEN with Actions: read+write on the repo
  try {
    const r = await fetch(`${GH}/repos/${env.GITHUB_REPO}/actions/workflows/watch.yml/dispatches`,
      { method: "POST", headers: ghHeaders(env), body: JSON.stringify({ ref: "main" }) });
    if (!r.ok) console.log("dispatch failed", r.status, await r.text());
  } catch (e) {
    console.log("dispatch error", e.message);
  }
}

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

const OPT_KEYS = ["css", "xpath", "json", "subtract", "extract", "ignore", "trigger"];

async function handle(env, exctx, p) {
  const command = p.get("command") || "";
  const text = (p.get("text") || "").trim();
  const tokens = text.split(/\s+/).filter(Boolean);
  const ivTok = tokens.slice(1).find(x => /^\d+m?$/i.test(x));
  const opts = {};
  for (const k of OPT_KEYS) {
    // stop the value at the next `key=`, at a bare flag (sort/dedupe/render/js), or end —
    // otherwise `trigger=resign sort` swallows the trailing flag into the value.
    const m = text.match(new RegExp("\\b" + k + "=(.+?)(?=\\s+[a-z]+=|\\s+(?:sort|dedupe|render|js)\\b|$)", "i"));
    if (m) opts[k] = m[1].trim();
  }
  const flags = new Set(tokens.map(t => t.toLowerCase()));
  const ctx = {
    url: cleanUrl(tokens[0]),
    interval: ivTok ? parseInterval(ivTok) : null,
    opts,
    sort: flags.has("sort"),
    dedupe: flags.has("dedupe"),
    render: flags.has("render") || flags.has("js"),   // headless-browser render (JS pages)
    channel: p.get("channel_id") || "",
    channel_name: p.get("channel_name") || "",
    user: p.get("user_name") || p.get("user_id") || "someone",
    userId: p.get("user_id") || "",
    responseUrl: p.get("response_url") || "",
    text,
  };
  if (command === "/roster") return await rosterCmd(env, ctx);
  if (command === "/save")   return saveCmd(env, exctx, ctx);
  if (command === "/links")  return await listCmd(env, ctx);
  if (command === "/unlink") return await mutate(env, "remove", ctx);
  if (command === "/link")   return await mutate(env, "add", ctx);
  return { text: `Unknown command ${command}` };
}

// --- /roster : show a Harvard varsity roster (read the tracker's committed JSON) ---
async function rosterCmd(env, c) {
  const SORTS = ["jersey", "class", "hometown", "highschool", "name", "pos"];
  const toks = (c.text || "").split(/\s+/).filter(Boolean);
  let sortby = "jersey";
  const rest = toks.filter(t => { if (SORTS.includes(t.toLowerCase())) { sortby = t.toLowerCase(); return false; } return true; });
  const sport = rest.join(" ").toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9-]/g, "");
  if (!sport) return { text: "Usage: `/roster <sport> [class|hometown|highschool|name|pos]` — e.g. `/roster mens-basketball hometown`" };
  const repo = env.GITHUB_REPO || "crimson-managingeditor/crimson-perf-bot";
  const r = await fetch(`https://raw.githubusercontent.com/${repo}/main/research/rosters_data/${sport}.json`,
    { headers: { "User-Agent": "crimson-watch" } });
  if (r.status === 404) return { text: `No roster for \`${sport}\`. Use the slug form, e.g. \`mens-basketball\`, \`womens-ice-hockey\`, \`football\`.` };
  if (!r.ok) return { text: `Couldn't read the roster (${r.status}).` };
  let players = await r.json();
  const jn = j => parseInt(String(j).replace(/\D/g, "")) || 999;
  const kf = { jersey: p => jn(p.jersey), class: p => p.class || "", hometown: p => p.hometown || "",
    highschool: p => p.highschool || "", name: p => (p.name || "").split(" ").pop(), pos: p => p.pos || "" };
  const f = kf[sortby] || kf.jersey;
  players = players.slice().sort((a, b) => { const av = f(a), bv = f(b); return av < bv ? -1 : av > bv ? 1 : 0; });
  const cap = 60;
  const lines = players.slice(0, cap).map(p =>
    `#${p.jersey || "–"} ${p.name} · ${p.class || "?"} ${p.pos || ""} · ${p.hometown || ""}${p.highschool ? " · " + p.highschool : ""}`);
  const more = players.length > cap ? `\n…and ${players.length - cap} more` : "";
  return { text: `*${sport}* — ${players.length} players (by ${sortby}):\n` + lines.join("\n") + more };
}

// --- /save : preserve a page in the Wayback Machine, reply async via response_url ---
const sleep = ms => new Promise(r => setTimeout(r, ms));

function saveCmd(env, exctx, c) {
  if (!/^https?:\/\//i.test(c.url))
    return { text: "Usage: `/save <url>` — archives the page to the Wayback Machine and returns a permalink you can cite." };
  // The capture takes longer than Slack's 3s window, so ack now and post the result to
  // response_url when it's done (valid for 30 min).
  exctx.waitUntil(archiveAndReport(env, c.url, c.responseUrl));
  return { text: `📼 Saving <${c.url}> to the Wayback Machine… I'll drop the permalink here in a moment.` };
}

async function archiveAndReport(env, url, responseUrl) {
  let res;
  try { res = await waybackArchive(env, url); }
  catch (e) { res = { ok: false, error: e.message }; }
  const today = "https://archive.ph/newest/" + url;   // click to view/save a second copy
  const text = res.ok
    ? `✅ Saved <${url}>\n• Wayback permalink: ${res.permalink}\n• Second copy on archive.today: ${today}`
    : `⚠️ Couldn't confirm a Wayback capture of <${url}> yet (${res.error || "archiver busy"}). `
      + `It may still be processing — check <https://web.archive.org/web/2/${url}|the latest snapshot> in a minute, `
      + `or save directly: https://web.archive.org/save/${url}\n• archive.today: ${today}`;
  await postResponse(responseUrl, text);
}

async function waybackArchive(env, url) {
  const key = env.WAYBACK_KEY;   // optional "accesskey:secret"
  if (key) {
    // authenticated Save Page Now 2 — reliable, returns the fresh capture timestamp
    try {
      const r = await fetch("https://web.archive.org/save/", {
        method: "POST",
        headers: { "Authorization": "LOW " + key, "Accept": "application/json",
                   "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "crimson-watch" },
        body: "url=" + encodeURIComponent(url) + "&skip_first_archive=1",
      });
      const j = await r.json().catch(() => null);
      if (j && j.job_id) {
        const ts = await pollSpn(key, j.job_id);
        if (ts) return { ok: true, permalink: `https://web.archive.org/web/${ts}/${url}` };
      }
    } catch (_) { /* fall through to availability */ }
  } else {
    // no key: trigger a best-effort capture but DON'T block ~40s for it to finish (that
    // would blow the Worker's execution budget). The request enqueues the capture
    // server-side; we abort our connection after a few seconds and resolve below.
    try {
      const ac = new AbortController();
      const t = setTimeout(() => ac.abort(), 4000);
      await fetch("https://web.archive.org/save/" + url,
        { headers: { "User-Agent": "crimson-watch" }, redirect: "manual", signal: ac.signal }).catch(() => {});
      clearTimeout(t);
    } catch (_) {}
  }
  // resolve a real permalink via the availability API (poll to catch a fresh capture)
  for (let i = 0; i < 4; i++) {
    await sleep(4000);
    const snap = await availability(url);
    if (snap) return { ok: true, permalink: snap };
  }
  return { ok: false, error: "no snapshot confirmed yet" };
}

async function pollSpn(key, jobId) {
  for (let i = 0; i < 6; i++) {
    await sleep(3000);
    try {
      const r = await fetch("https://web.archive.org/save/status/" + jobId,
        { headers: { "Authorization": "LOW " + key, "Accept": "application/json" } });
      const j = await r.json().catch(() => null);
      if (j && j.status === "success" && j.timestamp) return j.timestamp;
      if (j && j.status === "error") return null;
    } catch (_) {}
  }
  return null;
}

async function availability(url) {
  try {
    const r = await fetch("https://archive.org/wayback/available?url=" + encodeURIComponent(url),
      { headers: { "User-Agent": "crimson-watch" } });
    const j = await r.json().catch(() => null);
    const c = j && j.archived_snapshots && j.archived_snapshots.closest;
    return c && c.available ? c.url.replace(/^http:/, "https:") : null;
  } catch (_) { return null; }
}

async function postResponse(responseUrl, text) {
  if (!responseUrl) return;
  try {
    await fetch(responseUrl, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ response_type: "ephemeral", text }) });
  } catch (_) {}
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
    return { text: "Usage: `/link <url> <5|30|60|120>m [css=… | xpath=… | json=…] [subtract=… extract=… ignore=… trigger=… sort dedupe]`" };
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
      const rec = { url: c.url, channel: c.channel, channel_name: c.channel_name,
                    added_by: c.user, added_by_id: c.userId, interval };
      for (const [k, v] of Object.entries(c.opts)) if (v) rec[k] = v;
      if (c.sort) rec.sort = true;
      if (c.dedupe) rec.dedupe = true;
      if (c.render) rec.render = true;
      const fbits = [];
      for (const k of OPT_KEYS) if (rec[k]) fbits.push(`${k}=\`${rec[k]}\``);
      for (const fl of ["render", "sort", "dedupe"]) if (rec[fl]) fbits.push(fl);
      const filt = fbits.length ? "  ·  " + fbits.join(" ") : "";
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
    let f = "";
    if (typeof e === "object") {
      for (const k of OPT_KEYS) if (e[k]) f += `  ${k}=\`${e[k]}\``;
      for (const fl of ["render", "sort", "dedupe"]) if (e[fl]) f += `  ${fl}`;
    }
    return `• ${urlOf(e)}${iv}${f}`;
  }).join("\n");
  const more = here.length > 50 ? `\n…and ${here.length - 50} more` : "";
  const where = c.channel_name && c.channel_name !== "directmessage" ? `#${c.channel_name}` : "this channel";
  return { text: `*Watching ${here.length} page(s) in ${where}:*\n${shown}${more}` };
}
