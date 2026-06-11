"""Ingest a human-written operation manual (PDF / Markdown) into the two-layer knowledge base.

A manual describes ONE platform's UI & operations — the same app on iPhone vs browser vs
Android navigates differently — so a manual is ingested **for a target platform** and lands
under ``knowledge/<platform>/<app>/``. Only the INJECTION PLUMBING is platform-neutral (the
``set_app_knowledge`` path in ``core/``); the knowledge CONTENT is platform-specific, and
``auto_discover_knowledge`` only loads it for matching runs of that platform.

This is a NEW *producer* for the SAME knowledge contract that ``app_summary`` already
produces (``AppKnowledge`` = ``_app.md`` navigation + ``_elements.md`` elements). The
*consumer* side is untouched: ``auto_discover_knowledge(goal, platform)`` matches the app name
in the goal under the current platform's subtree and injects via ``supervisor.set_app_knowledge``.

Where ``app_summary`` distills per-page recon docs, this distills a freeform manual:
  manual (PDF/MD)  ──LLM──▶  _app.md (导航层)  +  _elements.md (元素层)
The output files land in ``knowledge/<platform>/<app>/`` exactly like ``app_summary``'s, so the
runtime auto-discovery path needs ZERO changes.

Offline by design (the agreed "mode A"): run it once, the LLM cost is paid once and cached to
disk; subsequent agent runs just read the files. Prompts are platform-neutral in wording (the
distilled platform comes from the manual's own content + the target dir), avoiding
``app_summary``'s iPhone-specific prompt flavor.

Usage:
    uv run python -m gui_agent.core.self_learning.manual_ingest \
        "Robo Team 使用说明书.pdf" browser RoboTeam
"""

from __future__ import annotations

import argparse
import unicodedata
from pathlib import Path

from dotenv import load_dotenv

from gui_agent.core.self_learning.app_summary import (
    KNOWLEDGE_DIR,
    AppKnowledge,
    _call_llm,
)

# ── Prompts for _app.md (navigation layer) — platform-neutral, manual-shaped ──

_MANUAL_NAV_SYSTEM = """\
你是一个软件操作知识分析专家。

给定一份应用/系统的**操作手册原文**(可能是 Web 后台、桌面或移动应用),\
提炼出一份「应用级导航概览」,只关注功能模块与页面之间的导航关系,不含逐个 UI 元素细节。要求:

1. **应用概述**:1-2 句说明这个应用的核心用途与形态(如 Web 后台 / 移动 App / 桌面端)。
2. **模块/页面列表**:按手册的章节层级列出主要功能模块与页面,每个用一句话概括其作用,\
   并标注页面类型(list/detail/form/dashboard/modal/settings 等)。
3. **导航关系**:进入每个页面的路径(如「订单 -> 订单列表」),以及触发方式\
   (顶部导航 / 侧边菜单 / 列表行末的更多图标 / 弹窗等)。
4. **关键操作路径**:列出 5-8 条最常见的端到端操作路径(如「创建订单」「导出订单」),\
   每条给出页面跳转序列。

只描述「在哪里、怎么到达」,不要罗列具体按钮坐标或输入框细节(那属于元素层)。
输出纯 Markdown,不要 YAML frontmatter,用中文。
"""

_MANUAL_NAV_PROMPT = """\
以下是「{app}」的操作手册原文(共 {chars} 字):

{manual}

请生成应用级导航概览(仅模块/页面结构与导航关系,不含逐个 UI 元素细节)。
"""

# ── Prompts for _elements.md (elements layer) ─────────────────────────────────

_MANUAL_ELEMENTS_SYSTEM = """\
你是一个软件操作知识分析专家。

给定一份应用/系统的**操作手册原文**,提炼出一份「页面元素与操作」清单,\
供执行器定位与操作具体控件。要求:

1. 按功能模块/页面分组。
2. 每个页面列出关键 UI 元素:名称、所在位置描述(如「右上角」「列表行末的更多图标」\
   「左下角用户信息区」)、作用、以及触发后的结果。
3. 对手册里每个「如何做某事」的操作,写清操作步骤序列(点哪个按钮、填什么、最后如何确认)。
4. 保留手册原文里的定位与名称用词,不要概括掉具体控件名。
5. 去掉纯导航/页面间跳转的概述(那属于导航层),专注单页面内的元素与操作。

输出纯 Markdown,不要 YAML frontmatter,用中文。
"""

_MANUAL_ELEMENTS_PROMPT = """\
以下是「{app}」的操作手册原文(共 {chars} 字):

{manual}

请提取所有页面的 UI 元素与操作步骤,按页面分组汇总。
"""


def load_manual_text(path: Path) -> str:
    """Extract plain text from a manual file (.pdf / .md / .txt).

    PDF text is extracted per-page (page markers preserved for context). All text is
    NFKC-normalized, which folds the CJK compatibility / Kangxi-radical glyphs that PDF
    extraction often emits (e.g. ``⽤``→``用``, ``⻚``→``页``) back to normal characters.
    Raises if the format is unsupported; returns ``""`` for an image-only/scanned PDF
    (caller turns that into a clear error).
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import pypdf

        reader = pypdf.PdfReader(str(path))
        parts: list[str] = []
        for i, page in enumerate(reader.pages):
            t = (page.extract_text() or "").strip()
            if t:
                parts.append(f"[第 {i + 1} 页]\n{t}")
        text = "\n\n".join(parts)
    elif suffix in (".md", ".markdown", ".txt"):
        text = path.read_text(encoding="utf-8")
    else:
        raise ValueError(f"不支持的手册格式: {suffix}(支持 .pdf / .md / .txt)")
    return unicodedata.normalize("NFKC", text).strip()


def ingest_manual(manual_path: Path, platform: str, app: str) -> AppKnowledge:
    """Distill a manual into knowledge/<platform>/<app>/_app.md + _elements.md (two LLM calls).

    Same output shape as ``app_summary.generate_summary``, written to the platform-scoped
    dir, so the runtime ``auto_discover_knowledge(goal, platform)`` path consumes it unchanged.
    """
    if not manual_path.exists():
        raise FileNotFoundError(f"手册文件不存在: {manual_path}")
    text = load_manual_text(manual_path)
    if not text:
        raise ValueError(
            f"未能从 {manual_path.name} 抽取到文字(可能是扫描版/纯图片 PDF,需 OCR 或视觉解析)"
        )
    print(f"Loaded manual: {manual_path.name} ({len(text)} chars) → platform={platform}, app={app}")

    app_dir = KNOWLEDGE_DIR / platform / app
    app_dir.mkdir(parents=True, exist_ok=True)

    nav = _call_llm(
        _MANUAL_NAV_SYSTEM,
        _MANUAL_NAV_PROMPT.format(app=app, chars=len(text), manual=text),
    )
    nav_path = app_dir / "_app.md"
    nav_path.write_text(nav, encoding="utf-8")
    print(f"Written: {nav_path} ({len(nav)} chars)")

    elements = _call_llm(
        _MANUAL_ELEMENTS_SYSTEM,
        _MANUAL_ELEMENTS_PROMPT.format(app=app, chars=len(text), manual=text),
    )
    elements_path = app_dir / "_elements.md"
    elements_path.write_text(elements, encoding="utf-8")
    print(f"Written: {elements_path} ({len(elements)} chars)")

    return AppKnowledge(navigation=nav, elements=elements, app_name=app)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="把操作手册(PDF/Markdown)解析成两层知识库写入 knowledge/<平台>/<app>/,"
        "供 auto_discover_knowledge 在该平台运行时按 app 名匹配注入。"
    )
    parser.add_argument("manual", type=Path, help="手册文件路径(.pdf / .md / .txt)")
    parser.add_argument(
        "platform",
        choices=["iphone", "browser", "android"],
        help="手册所属平台(决定 knowledge/<平台>/ 子目录,只在该平台运行时被注入)",
    )
    parser.add_argument(
        "app",
        help="应用名,决定 knowledge/<平台>/<app>/ 目录;须能在任务目标文本里被匹配到(如 RoboTeam)",
    )
    args = parser.parse_args()
    ingest_manual(args.manual, args.platform, args.app)


if __name__ == "__main__":
    main()
