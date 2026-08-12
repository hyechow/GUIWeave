import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { dirname, extname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const html = await readFile(resolve(root, "static/index.html"), "utf8");
const css = await readFile(resolve(root, "app/globals.css"), "utf8");
const types = { ".png": "image/png", ".svg": "image/svg+xml" };
const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", "http://localhost");
  if (url.pathname === "/" || url.pathname === "/index.html") {
    res.setHeader("content-type", "text/html; charset=utf-8");
    res.end(html.replaceAll("__ORIGIN__", `http://${req.headers.host}`));
    return;
  }
  if (url.pathname === "/styles.css") {
    res.setHeader("content-type", "text/css; charset=utf-8");
    res.end(css);
    return;
  }
  try {
    const file = resolve(root, "public", url.pathname.slice(1));
    res.setHeader("content-type", types[extname(file)] ?? "application/octet-stream");
    res.end(await readFile(file));
  } catch {
    res.statusCode = 404;
    res.end("Not found");
  }
});
server.listen(3000, "127.0.0.1", () => console.log("Local: http://127.0.0.1:3000"));
