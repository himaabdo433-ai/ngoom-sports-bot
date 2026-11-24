const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");

const root = __dirname;
const port = Number(process.env.PORT || 3000);
const types = { ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".ico": "image/x-icon" };
const leagues = {
  "eng.1": "الدوري الإنجليزي الممتاز",
  "esp.1": "الدوري الإسباني",
  "ita.1": "الدوري الإيطالي",
  "ger.1": "الدوري الألماني",
  "fra.1": "الدوري الفرنسي",
  "uefa.champions": "دوري أبطال أوروبا",
  "uefa.europa": "الدوري الأوروبي",
  "usa.1": "الدوري الأمريكي"
};
const cache = new Map();

function json(res, status, data) {
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", "Access-Control-Allow-Origin": "*" });
  res.end(JSON.stringify(data));
}
async function getMatches(date) {
  const key = date;
  const cached = cache.get(key);
  if (cached && Date.now() - cached.at < 45000) return cached.data;
  const results = await Promise.all(Object.entries(leagues).map(async ([slug, leagueName]) => {
    try {
      const url = new URL("https://site.api.espn.com/apis/site/v2/sports/soccer/" + slug + "/scoreboard");
      url.searchParams.set("dates", date);
      const response = await fetch(url, { headers: { "User-Agent": "NogoomSports/1.0", "Accept": "application/json" }, signal: AbortSignal.timeout(9000) });
      if (!response.ok) throw new Error("ESPN HTTP " + response.status);
      const body = await response.json();
      return (body.events || []).map(event => {
        const competition = (event.competitions || [])[0] || {};
        const competitors = competition.competitors || [];
        const home = competitors.find(team => team.homeAway === "home") || competitors[0] || {};
        const away = competitors.find(team => team.homeAway === "away") || competitors[1] || {};
        const status = competition.status || event.status || {};
        const statusType = status.type || {};
        return {
          id: event.id,
          league: leagueName,
          leagueCode: slug,
          date: event.date || competition.date,
          home: home.team?.displayName || home.team?.name || "الفريق صاحب الأرض",
          away: away.team?.displayName || away.team?.name || "الفريق الضيف",
          homeLogo: home.team?.logo || home.team?.logos?.[0]?.href || "",
          awayLogo: away.team?.logo || away.team?.logos?.[0]?.href || "",
          homeScore: home.score ?? null,
          awayScore: away.score ?? null,
          state: statusType.state || "pre",
          completed: Boolean(statusType.completed),
          clock: status.displayClock || "",
          detail: statusType.shortDetail || statusType.detail || statusType.description || "",
          venue: competition.venue?.fullName || ""
        };
      });
    } catch (error) {
      return [];
    }
  }));
  const matches = results.flat().sort((a,b) => new Date(a.date) - new Date(b.date));
  const data = { date, matches, leagues: Object.keys(leagues).map(code => ({ code, name: leagues[code] })), source: "ESPN" };
  cache.set(key, { at: Date.now(), data });
  return data;
}


const newsCache = { at: 0, items: [] };
function decodeXml(value) {
  return String(value || "").replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1")
    .replace(/<[^>]*>/g, " ").replace(/&amp;/g, "&").replace(/&quot;/g, '"')
    .replace(/&#39;|&apos;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(Number(n)))
    .replace(/&#x([\da-f]+);/gi, (_, n) => String.fromCharCode(parseInt(n, 16)))
    .replace(/\s+/g, " ").trim();
}
function xmlTag(block, tag) {
  const match = block.match(new RegExp("<" + tag + "(?:\\s[^>]*)?>([\\s\\S]*?)</" + tag + ">", "i"));
  return match ? decodeXml(match[1]) : "";
}
async function getNews() {
  if (newsCache.items.length && Date.now() - newsCache.at < 180000) return newsCache.items;
  const queries = [
    "site:yallakora.com كرة القدم",
    "site:filgoal.com كرة القدم",
    "أخبار كرة القدم مصر الأهلي الزمالك الدوري المصري"
  ];
  const feeds = await Promise.all(queries.map(async q => {
    try {
      const feedUrl = "https://news.google.com/rss/search?q=" + encodeURIComponent(q) + "&hl=ar&gl=EG&ceid=EG:ar";
      const response = await fetch(feedUrl, { headers: { "User-Agent": "Mozilla/5.0 NogoomSports/1.0", "Accept": "application/rss+xml, application/xml, text/xml" }, signal: AbortSignal.timeout(10000) });
      if (!response.ok) return [];
      const xml = await response.text();
      return [...xml.matchAll(/<item(?:\s[^>]*)?>([\s\S]*?)<\/item>/gi)].map(match => {
        const block = match[1];
        const title = xmlTag(block, "title");
        const link = xmlTag(block, "link");
        const description = xmlTag(block, "description").replace(/\s+/g, " ").slice(0, 220);
        const pubDate = xmlTag(block, "pubDate");
        const source = xmlTag(block, "source") || (link.includes("yallakora") ? "يلا كورة" : link.includes("filgoal") ? "في الجول" : "مصدر رياضي");
        return { title, link, description, pubDate, source };
      }).filter(item => item.title && item.link);
    } catch (error) { return []; }
  }));
  const unique = new Map();
  for (const item of feeds.flat()) {
    const key = item.title.toLowerCase();
    if (!unique.has(key)) unique.set(key, item);
  }
  const items = [...unique.values()].sort((a,b) => new Date(b.pubDate || 0) - new Date(a.pubDate || 0)).slice(0, 24);
  newsCache.at = Date.now();
  newsCache.items = items;
  return items;
}

http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  if (url.pathname === "/api/news") {
    try { return json(res, 200, { items: await getNews(), source: "Google News RSS" }); }
    catch (error) { return json(res, 502, { error: "تعذر جلب الأخبار حاليًا" }); }
  }
  if (url.pathname === "/api/matches") {
    const date = url.searchParams.get("date") || new Date().toISOString().slice(0,10).replace(/-/g,"");
    if (!/^\d{8}$/.test(date)) return json(res, 400, { error: "صيغة التاريخ غير صحيحة" });
    try {
      const data = await getMatches(date);
      return json(res, 200, data);
    } catch (error) {
      return json(res, 502, { error: "تعذر جلب المباريات حاليًا" });
    }
  }
  let pathname;
  try { pathname = decodeURIComponent(url.pathname); } catch { pathname = "/"; }
  let requested = path.join(root, pathname === "/" ? "index.html" : pathname);
  if (!requested.startsWith(root)) { res.writeHead(403); return res.end("Forbidden"); }
  fs.stat(requested, (err, stat) => {
    if (!err && stat.isDirectory()) requested = path.join(requested, "index.html");
    fs.readFile(requested, (readErr, data) => {
      if (readErr) { res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" }); return res.end("Not found"); }
      res.writeHead(200, { "Content-Type": types[path.extname(requested)] || "application/octet-stream", "Cache-Control": "public, max-age=60" });
      res.end(data);
    });
  });
}).listen(port, "0.0.0.0", () => console.log("Nogoom Sports website listening on " + port));
