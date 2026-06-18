"""Generate `when:` frontmatter for knowledge sections — the selector's synonym bridge.

The KnowledgeSelector picks sections from a title manifest. Bare titles lose on literal
matching when the UI/task wording differs from the manual's heading (实测: a 新建虚拟机器人
task picks 「如何添加机器人」(real-device registration, harmful) over 「如何使用机器人模拟器」
(the right section) 3/3 — the 虚拟机器人↔模拟器 synonym never survives title-only ranking).

This script writes a one-line "when to consult this section" trigger into each section's
YAML frontmatter; ``ProgressiveKnowledge`` surfaces it in the selector manifest as
``[s15] 如何使用机器人模拟器 — 创建/启用/批量修改虚拟机器人（模拟器）…``. Idempotent:
sections that already carry ``when:`` are skipped unless --force.

Usage:
    uv run python -m gui_agent.core.self_learning.gen_when RoboTeam --platform browser
    uv run python -m gui_agent.core.self_learning.gen_when 微信 --platform iphone --force
"""

from __future__ import annotations

import argparse
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from gui_agent.core.self_learning.app_summary import KNOWLEDGE_DIR, _call_llm  # noqa: E402
from gui_agent.core.self_learning.progressive import split_frontmatter
from gui_agent.prompts import load_prompt  # noqa: E402

_SYSTEM = "你是知识库检索描述生成器。"

_PROMPT = load_prompt("task.self_learning.gen_when.prompt")  # rendered: {title}/{body}

_BODY_CAP = 2500  # the when-line only needs the gist; cap keeps the call small


def _compose(meta: dict[str, str], when: str, body: str) -> str:
    """Re-serialize frontmatter (merged with the new when) + body."""
    meta = {**meta, "when": when}
    lines = [f"{k}: {v}" for k, v in meta.items()]
    return "---\n" + "\n".join(lines) + "\n---\n" + body


def generate_for_app(app: str, platform: str = "browser", force: bool = False) -> int:
    """Generate `when:` for every section of knowledge/<platform>/<app>/. Returns count written."""
    app_dir = KNOWLEDGE_DIR / platform / app
    files = [p for p in sorted(app_dir.glob("*.md")) if not p.name.startswith("_")]
    if not files:
        print(f"未找到章节文件: {app_dir}")
        return 0

    todo = []
    for p in files:
        meta, _ = split_frontmatter(p.read_text(encoding="utf-8"))
        if force or not meta.get("when"):
            todo.append(p)
    print(f"{app}: 共 {len(files)} 节，待生成 {len(todo)} 节（已有 when 的跳过{'，--force 重生成' if force else ''}）")

    written = 0
    t0 = time.time()
    for i, p in enumerate(todo, 1):
        meta, body = split_frontmatter(p.read_text(encoding="utf-8"))
        title = p.stem.replace("_", " ")
        try:
            when = _call_llm(_SYSTEM, _PROMPT.render(title=title, body=body[:_BODY_CAP]))
        except Exception as exc:  # noqa: BLE001 — one bad call shouldn't kill the batch
            print(f"  [{i}/{len(todo)}] ✗ {p.stem}: {exc}")
            continue
        when = when.splitlines()[0].strip().strip("。.")
        if not when:
            print(f"  [{i}/{len(todo)}] ✗ {p.stem}: 生成为空，跳过")
            continue
        p.write_text(_compose(meta, when, body), encoding="utf-8")
        written += 1
        print(f"  [{i}/{len(todo)}] ✓ {p.stem} — {when}", flush=True)
    print(f"完成：写入 {written}/{len(todo)} 节，耗时 {time.time() - t0:.0f}s")
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="为知识库章节生成 when:（何时查阅）frontmatter")
    parser.add_argument("app", help="应用名（knowledge/<platform>/<app>/）")
    parser.add_argument("--platform", default="browser", help="平台子目录，默认 browser")
    parser.add_argument("--force", action="store_true", help="重生成已有 when 的章节")
    args = parser.parse_args()
    sys.exit(0 if generate_for_app(args.app, args.platform, args.force) >= 0 else 1)


if __name__ == "__main__":
    main()
