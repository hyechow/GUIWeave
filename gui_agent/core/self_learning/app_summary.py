"""Generate app-level knowledge files from per-page knowledge.

Produces two files in knowledge/{platform}/{app}/ (platform defaults to iphone, the only
platform with per-page recon today):
- _app.md: Navigation structure for Supervisor (task decomposition)
- _elements.md: UI element details for Planner (instruction generation)

Usage:
    uv run python -m gui_agent.core.self_learning.app_summary 微信
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from gui_agent.core.config import resolve_llm_config

KNOWLEDGE_DIR = Path(__file__).resolve().parents[3] / "knowledge"

# ── Prompts for _app.md (navigation structure) ─────────────────────────────

_NAV_SYSTEM = """\
你是一个应用导航结构分析专家。

给定一个应用的所有页面知识文档，生成一份 **应用级导航概览**，只关注页面间的导航关系，\
不包含具体 UI 元素细节。要求：

1. **应用概述**：1-2 句话说明这个 app 的核心功能
2. **页面列表**：按层级列出所有页面，标注页面类型（list/detail/chat/modal/form/home），\
   每个页面用一句话概括其主要功能
3. **导航关系**：从哪些页面可以跳转到哪些页面，标注触发方式（如「底部导航」「点击搜索」）
4. **关键操作路径**：列出 3-5 条最常见的用户操作路径

不要列出具体的 UI 元素（如按钮位置、输入框等），这些信息属于元素层面，不在本文档范围内。

输出格式为纯 Markdown，不要包含 YAML frontmatter。
用中文输出。
"""

_NAV_PROMPT = """\
以下是「{app}」应用的 {n} 个页面知识文档：

{pages_text}

请生成应用级导航概览（仅页面结构和导航关系，不含 UI 元素细节）。
"""

# ── Prompts for _elements.md (UI elements) ────────────────────────────────

_ELEMENTS_SYSTEM = """\
你是一个应用 UI 元素分析专家。

给定一个应用的所有页面知识文档，提取并汇总所有页面中的 UI 元素信息。要求：

1. 按页面分组，每个页面列出所有关键 UI 元素
2. 每个元素保留：名称、位置描述（如「左上角」「底部」）、功能/操作方式
3. 保留原文的定位描述，不要省略或概括
4. 去掉页面间的导航关系信息，只保留单页面内的元素信息

输出格式为纯 Markdown，不要包含 YAML frontmatter。
用中文输出。
"""

_ELEMENTS_PROMPT = """\
以下是「{app}」应用的 {n} 个页面知识文档：

{pages_text}

请提取所有页面的 UI 元素信息，按页面分组汇总。
"""


@dataclass
class AppKnowledge:
    """Two-layer knowledge for an app."""
    navigation: str  # _app.md content → Supervisor
    elements: str    # _elements.md content → Planner
    app_name: str


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
    """Load all page .md files (excluding _app.md and _elements.md) from app knowledge dir.

    Returns list of (filename, content).
    """
    pages: list[tuple[str, str]] = []
    for md in sorted(app_dir.glob("*.md")):
        if md.name.startswith("_"):
            continue
        pages.append((md.stem, md.read_text(encoding="utf-8")))
    return pages


def _call_llm(system: str, prompt: str) -> str:
    cfg = resolve_llm_config("action_policy")
    llm = ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        temperature=0,
    )
    resp = llm.invoke([
        SystemMessage(content=system),
        HumanMessage(content=prompt),
    ])
    return str(resp.content).strip()


def build_navigation_summary(app: str, pages: list[tuple[str, str]]) -> str:
    """Generate navigation structure summary (_app.md) for Supervisor."""
    pages_text = "\n\n---\n\n".join(
        f"### {name}\n{content}" for name, content in pages
    )
    return _call_llm(
        _NAV_SYSTEM,
        _NAV_PROMPT.format(app=app, n=len(pages), pages_text=pages_text),
    )


def build_elements_summary(app: str, pages: list[tuple[str, str]]) -> str:
    """Generate UI elements summary (_elements.md) for Planner."""
    pages_text = "\n\n---\n\n".join(
        f"### {name}\n{content}" for name, content in pages
    )
    return _call_llm(
        _ELEMENTS_SYSTEM,
        _ELEMENTS_PROMPT.format(app=app, n=len(pages), pages_text=pages_text),
    )


def generate_summary(app: str, platform: str = "iphone") -> AppKnowledge:
    """Generate _app.md and _elements.md for the given app (under knowledge/<platform>/)."""
    app_dir = KNOWLEDGE_DIR / platform / app
    if not app_dir.is_dir():
        raise FileNotFoundError(f"Knowledge directory not found: {app_dir}")

    pages = load_page_files(app_dir)
    if not pages:
        raise ValueError(f"No page knowledge files found in {app_dir}")

    print(f"  归约 {len(pages)} 个页知识 → 两层概览", flush=True)

    print("  [1/2] 归约导航层 _app.md …", flush=True)
    nav = build_navigation_summary(app, pages)
    nav_path = app_dir / "_app.md"
    nav_path.write_text(nav, encoding="utf-8")
    print(f"        ✓ {nav_path.name} ({len(nav)} 字)", flush=True)

    print("  [2/2] 归约元素层 _elements.md …", flush=True)
    elements = build_elements_summary(app, pages)
    elements_path = app_dir / "_elements.md"
    elements_path.write_text(elements, encoding="utf-8")
    print(f"        ✓ {elements_path.name} ({len(elements)} 字)", flush=True)

    return AppKnowledge(navigation=nav, elements=elements, app_name=app)


# Aliases: English/common names → canonical Chinese app names
_APP_ALIASES: dict[str, str] = {
    "wechat": "微信",
    "alipay": "支付宝",
}

# Known app names for name detection even when no knowledge directory exists
_KNOWN_APP_NAMES: list[str] = [
    "微信", "支付宝", "美团", "拼多多", "京东", "淘宝", "天猫",
    "抖音", "小红书", "闲鱼", "虎嗅", "高德地图", "百度地图",
    "饿了么", "滴滴", "携程", "大众点评",
]


def auto_discover_knowledge(goal: str, platform: str = "iphone") -> AppKnowledge | None:
    """Match goal against knowledge/<platform>/<app>/ dir names and load both layers.

    Knowledge is **platform-scoped**: a manual / recon captures ONE platform's UI &
    navigation, and the same app operates differently on iPhone vs browser vs Android. So we
    only look under the CURRENT platform's subtree — a browser app's knowledge is never
    injected into an iPhone run. The mobile-app name/alias fallbacks (recognize the app even
    when it has no knowledge dir yet) are iPhone-only.

    App name detection and knowledge loading are decoupled:
    - If a knowledge directory with _app.md exists → return full AppKnowledge
    - If directory exists but no knowledge files → return AppKnowledge with app_name only
    - If no directory but app name recognized (iPhone _KNOWN_APP_NAMES) → return app_name only
    - Returns None only when no app name can be identified
    """
    goal_lower = goal.lower()
    platform_dir = KNOWLEDGE_DIR / platform

    candidates: dict[str, Path | None] = {}
    if platform_dir.is_dir():
        for d in platform_dir.iterdir():
            if d.is_dir():
                candidates[d.name.lower()] = d
    if platform == "iphone":
        for alias, target in _APP_ALIASES.items():
            target_dir = platform_dir / target
            if target_dir.is_dir():
                candidates[alias] = target_dir
        # Add known app names that may not have a directory yet
        for app in _KNOWN_APP_NAMES:
            key = app.lower()
            if key not in candidates:
                candidates[key] = None

    for name, d in candidates.items():
        if name not in goal_lower:
            continue
        if d is None:
            # App name recognized but no knowledge directory
            canonical = _APP_ALIASES.get(name, name)
            print(f"  [Knowledge] 识别到应用「{canonical}」，但暂无知识库")
            return AppKnowledge(navigation="", elements="", app_name=canonical)
        nav_path = d / "_app.md"
        elements_path = d / "_elements.md"
        if nav_path.exists():
            nav = nav_path.read_text(encoding="utf-8").strip()
            elements = (
                elements_path.read_text(encoding="utf-8").strip()
                if elements_path.exists() else ""
            )
            return AppKnowledge(navigation=nav, elements=elements, app_name=d.name)
        # Directory exists but no knowledge file yet
        print(f"  [Knowledge] 识别到应用「{d.name}」，目录存在但暂无知识文件")
        return AppKnowledge(navigation="", elements="", app_name=d.name)

    return None


if __name__ == "__main__":
    load_dotenv()
    iphone_dir = KNOWLEDGE_DIR / "iphone"
    if len(sys.argv) < 2:
        print(f"Usage: python -m gui_agent.core.self_learning.app_summary <app_name>")
        available = [d.name for d in iphone_dir.iterdir() if d.is_dir()] if iphone_dir.is_dir() else []
        print(f"Available (iphone): {available}")
        sys.exit(1)
    generate_summary(sys.argv[1])
