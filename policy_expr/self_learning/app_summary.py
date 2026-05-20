"""Generate app-level summary (_app.md) from per-page knowledge files.

Reads all page .md files from knowledge/{app}/, asks LLM to synthesize
a navigation-structure summary, writes _app.md back.

Usage:
    uv run python -m policy_expr.self_learning.app_summary 微信
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from policy_expr.config import resolve_llm_config

KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge"

_SUMMARY_SYSTEM = """\
你是一个 iPhone 应用导航结构分析专家。

给定一个应用的所有页面知识文档，生成一份 **应用级导航概览**，要求：

1. **应用概述**：1-2 句话说明这个 app 的核心功能
2. **页面列表**：按层级列出所有页面，标注页面类型（list/detail/chat/modal/form/home）
3. **导航关系**：从哪些页面可以跳转到哪些页面，用箭头表示
4. **关键操作路径**：列出 3-5 条最常见的用户操作路径

输出格式为纯 Markdown，不要包含 YAML frontmatter。
用中文输出。
"""

_SUMMARY_PROMPT = """\
以下是「{app}」应用的 {n} 个页面知识文档：

{pages_text}

请生成应用级导航概览。
"""


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract YAML frontmatter key-value pairs from markdown text."""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return {}
    result: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            result[k.strip()] = v.strip()
    return result


def load_page_files(app_dir: Path) -> list[tuple[str, str]]:
    """Load all page .md files (excluding _app.md) from app knowledge dir.

    Returns list of (filename, content).
    """
    pages: list[tuple[str, str]] = []
    for md in sorted(app_dir.glob("*.md")):
        if md.name == "_app.md":
            continue
        pages.append((md.stem, md.read_text(encoding="utf-8")))
    return pages


def build_app_summary(app: str, pages: list[tuple[str, str]]) -> str:
    """Call LLM to synthesize page-level knowledge into app-level summary."""
    pages_text = "\n\n---\n\n".join(
        f"### {name}\n{content}" for name, content in pages
    )

    cfg = resolve_llm_config("action_policy")
    llm = ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        temperature=0,
    )

    messages = [
        SystemMessage(content=_SUMMARY_SYSTEM),
        HumanMessage(content=_SUMMARY_PROMPT.format(
            app=app, n=len(pages), pages_text=pages_text,
        )),
    ]
    resp = llm.invoke(messages)
    return str(resp.content).strip()


def generate_summary(app: str) -> str:
    """Generate and save _app.md for the given app.

    Returns the generated summary text.
    """
    app_dir = KNOWLEDGE_DIR / app
    if not app_dir.is_dir():
        raise FileNotFoundError(f"Knowledge directory not found: {app_dir}")

    pages = load_page_files(app_dir)
    if not pages:
        raise ValueError(f"No page knowledge files found in {app_dir}")

    print(f"Loading {len(pages)} page files from {app_dir}")
    summary = build_app_summary(app, pages)

    out_path = app_dir / "_app.md"
    out_path.write_text(summary, encoding="utf-8")
    print(f"Written: {out_path} ({len(summary)} chars)")
    return summary


if __name__ == "__main__":
    load_dotenv()
    if len(sys.argv) < 2:
        print(f"Usage: python -m policy_expr.self_learning.app_summary <app_name>")
        print(f"Available: {[d.name for d in KNOWLEDGE_DIR.iterdir() if d.is_dir()]}")
        sys.exit(1)
    generate_summary(sys.argv[1])
