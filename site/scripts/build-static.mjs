import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dist = resolve(root, "dist");
const html = await readFile(resolve(root, "static/index.html"), "utf8");
const css = await readFile(resolve(root, "app/globals.css"), "utf8");
const worker = `const htmlTemplate=${JSON.stringify(html)};\nconst css=${JSON.stringify(css)};\nexport default {async fetch(request, env) {const url=new URL(request.url);if(url.pathname==="/"||url.pathname==="/index.html")return new Response(htmlTemplate.replaceAll("__ORIGIN__",url.origin),{headers:{"content-type":"text/html; charset=utf-8","cache-control":"public, max-age=300"}});if(url.pathname==="/styles.css")return new Response(css,{headers:{"content-type":"text/css; charset=utf-8","cache-control":"public, max-age=3600"}});if(env?.ASSETS)return env.ASSETS.fetch(request);return new Response("Not found",{status:404});}};\n`;

await rm(dist, { recursive: true, force: true });
await mkdir(resolve(dist, "server"), { recursive: true });
await mkdir(resolve(dist, "client"), { recursive: true });
await mkdir(resolve(dist, ".openai"), { recursive: true });
await writeFile(resolve(dist, "server/index.js"), worker);
await cp(resolve(root, "public"), resolve(dist, "client"), { recursive: true });
await cp(resolve(root, ".openai/hosting.json"), resolve(dist, ".openai/hosting.json"));
await cp(resolve(root, "drizzle"), resolve(dist, ".openai/drizzle"), { recursive: true });
console.log("Built GUIWeave introduction site");
