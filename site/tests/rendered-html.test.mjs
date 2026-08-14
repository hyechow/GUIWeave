import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);

test("ships the GUIWeave introduction page", async () => {
  const [page, layout, css, pkg, staticPage] = await Promise.all([
    readFile(new URL("app/page.tsx", root), "utf8"),
    readFile(new URL("app/layout.tsx", root), "utf8"),
    readFile(new URL("app/globals.css", root), "utf8"),
    readFile(new URL("package.json", root), "utf8"),
    readFile(new URL("static/index.html", root), "utf8"),
  ]);

  assert.match(page, /让 AI 真正/);
  assert.match(page, /macOS Developer Preview/);
  assert.match(page, /preview_knowledge_document/);
  assert.match(page, /run_iphone_task/);
  assert.match(page, /sck_server|SCK 截图/);
  assert.match(page, /Robo Team 使用说明书\.pdf/);
  assert.match(page, /模型网关/);
  assert.match(page, /API_KEY/);
  assert.match(page, /Multi-turn Run Console/);
  assert.match(page, /先理解对话/);
  assert.match(page, /需要补充/);
  assert.match(page, /新对话/);
  assert.doesNotMatch(page, /无需公网服务/);
  assert.match(layout, /GUIWeave — Local GUI Automation for Agents/);
  assert.match(layout, /多轮 Run Console/);
  assert.match(layout, /\/og\.png/);
  assert.match(css, /prefers-reduced-motion/);
  assert.match(css, /\.chat-showcase/);
  assert.match(staticPage, /Multi-turn Run Console/);
  assert.match(staticPage, /新对话/);
  assert.doesNotMatch(page, /SkeletonPreview|codex-preview/);
  assert.doesNotMatch(pkg, /react-loading-skeleton/);
});

test("removes all starter preview assets", async () => {
  await assert.rejects(access(new URL("app/_sites-preview", root)));
  await access(new URL("public/og.png", root));
});
