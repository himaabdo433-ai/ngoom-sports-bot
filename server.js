const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");

const root = __dirname;
const port = Number(process.env.PORT || 3000);
const types = { ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".ico": "image/x-icon" };

http.createServer((req, res) => {
  const pathname = decodeURIComponent(new URL(req.url, "http://localhost").pathname);
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
