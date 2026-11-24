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

http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
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
