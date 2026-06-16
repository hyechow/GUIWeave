"""Generate app-level knowledge files from per-page knowledge.

Produces two files in knowledge/{platform}/{app}/ (platform defaults to iphone, the only
platform with per-page recon today):
- _app.md: Navigation structure for Supervisor (task decomposition)
- _elements.md: UI element details for Planner (instruction generation)

Hand-maintained `_`-prefixed siblings (not generated here, survive re-ingest, folded into the
always-on Supervisor context): _deploy.md (environment/access facts), _update.md (current-version
updates over the older distilled base — trusted over conflicting sections), and _skill.md
(reusable multi-step orchestrations the decomposer follows when the goal matches a skill).

Usage:
    uv run python -m gui_agent.core.self_learning.app_summary 微信
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
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
    elements: str    # _elements.md content → Planner (full; replan/decompose + fallback)
    app_name: str
    sections: dict[str, str] = field(default_factory=dict)  # per-section bodies → progressive load
    check: str = ""  # _check.md content → Checker-only observable completion rules
    # Hand-maintained overlay channels actually present this run → {"_check"|"_deploy"|"_skill"|
    # "_update": char_count}. _deploy/_update/_skill are folded into `navigation`; tracked here
    # purely so the report can show each channel's loaded state. Absent file → key absent.
    overlays: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, object]:
        """Compact, log-friendly description of what got injected (→ context.json knowledge)."""
        return {
            "app_name": self.app_name,
            "nav_chars": len(self.navigation),
            "elements_chars": len(self.elements),
            "check_chars": len(self.check),
            "section_count": len(self.sections),
            "overlays": dict(self.overlays),
        }


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

    Returns list of (filename, content). Content keeps any YAML frontmatter (the ``when:``
    retrieval line) — ProgressiveKnowledge parses/strips it at load; the reduce path strips
    it via :func:`_page_body` so summaries never see it.
    """
    pages: list[tuple[str, str]] = []
    for md in sorted(app_dir.glob("*.md")):
        if md.name.startswith("_"):
            continue
        pages.append((md.stem, md.read_text(encoding="utf-8")))
    return pages


# ── _skill.md lint: keep skills pure orchestration so they never grow into a 2nd manual ──
# A skill may only carry 触发/数据/步骤; each step must be a short verb+data phrase with no
# UI/HOW detail (that lives in the per-feature sections and _update.md). validate_skill_doc is
# run by the loader (warns) and unit-tested; the constants are the enforced discipline.
_SKILL_ALLOWED_FIELDS = ("触发", "数据", "步骤")
_SKILL_UI_TOKENS = (
    "点击", "单击", "双击", "右键", "菜单", "按钮", "标签", "页签", "输入框", "下拉",
    "弹窗", "对话框", "图标", "勾选", "复选框", "单选", "滑块", "拖动", "搜索框", "地址栏",
)
_SKILL_MAX_STEP_LEN = 50


def validate_skill_doc(text: str) -> list[str]:
    """Lint a _skill.md body. Returns human-readable issues (empty list = clean).

    Flags the ways a skill drifts into a second manual: a step that grows long (writing
    detail), a step naming a UI/HOW action (which belongs in the sections), or a field other
    than 触发/数据/步骤 (structure creep)."""
    issues: list[str] = []
    current: str | None = None
    in_steps = False
    for raw in text.splitlines():
        s = raw.strip()
        m = re.match(r"^##\s*skill\s*[:：]\s*(.+)$", s)
        if m:
            current, in_steps = m.group(1).strip(), False
            continue
        if current is None:
            continue  # preamble before any skill
        fm = re.match(r"^-\s*([^:：]+)[:：]", s)
        if fm:
            field = fm.group(1).strip()
            in_steps = field.startswith("步骤")
            if not any(field.startswith(a) for a in _SKILL_ALLOWED_FIELDS):
                issues.append(f"skill「{current}」含非法字段「{field}」（只允许 触发/数据/步骤）")
            continue
        sm = re.match(r"^\d+[.)、]\s*(.+)$", s)
        if sm and in_steps:
            step = sm.group(1).strip()
            if len(step) > _SKILL_MAX_STEP_LEN:
                issues.append(
                    f"skill「{current}」步骤过长（{len(step)}>{_SKILL_MAX_STEP_LEN} 字，像在写细节）：{step[:24]}…"
                )
            hits = [t for t in _SKILL_UI_TOKENS if t in step]
            if hits:
                issues.append(
                    f"skill「{current}」步骤含界面操作词 {hits}（HOW 归章节，不写进 skill）：{step[:24]}…"
                )
    return issues


def _page_body(text: str) -> str:
    """Section content without retrieval frontmatter — what the reduce prompts should read."""
    from gui_agent.core.self_learning.progressive import split_frontmatter

    return split_frontmatter(text)[1]


def _call_llm(system: str, prompt: str) -> str:
    cfg = resolve_llm_config("action_policy")
    llm = ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        temperature=0,
        # DashScope qwen thinking mode burns 30s+ per call on these small text reductions
        # (measured on reader: 119s → 0.9s with thinking off). Pure summarization — no need.
        extra_body={"enable_thinking": False},
    )
    resp = llm.invoke([
        SystemMessage(content=system),
        HumanMessage(content=prompt),
    ])
    return str(resp.content).strip()


def build_navigation_summary(app: str, pages: list[tuple[str, str]]) -> str:
    """Generate navigation structure summary (_app.md) for Supervisor."""
    pages_text = "\n\n---\n\n".join(
        f"### {name}\n{_page_body(content)}" for name, content in pages
    )
    return _call_llm(
        _NAV_SYSTEM,
        _NAV_PROMPT.format(app=app, n=len(pages), pages_text=pages_text),
    )


def build_elements_summary(app: str, pages: list[tuple[str, str]]) -> str:
    """Generate UI elements summary (_elements.md) for Planner."""
    pages_text = "\n\n---\n\n".join(
        f"### {name}\n{_page_body(content)}" for name, content in pages
    )
    return _call_llm(
        _ELEMENTS_SYSTEM,
        _ELEMENTS_PROMPT.format(app=app, n=len(pages), pages_text=pages_text),
    )


def generate_summary(app: str, platform: str = "iphone", make_elements: bool = True) -> AppKnowledge:
    """Generate _app.md (+ optionally _elements.md) for the given app (under knowledge/<platform>/).

    ``_elements.md`` is the Planner's element-detail blob, used ONLY as a fallback when no
    per-section page files exist (the runtime prefers progressive per-section selection — see
    ``policy.set_app_knowledge``). Manual ingestion always produces per-section files, so it passes
    ``make_elements=False`` to skip the now-vestigial reduce (one fewer LLM call; mirrors RoboTeam,
    which carries sections + _app.md and no _elements.md)."""
    app_dir = KNOWLEDGE_DIR / platform / app
    if not app_dir.is_dir():
        raise FileNotFoundError(f"Knowledge directory not found: {app_dir}")

    pages = load_page_files(app_dir)
    if not pages:
        raise ValueError(f"No page knowledge files found in {app_dir}")

    steps = 2 if make_elements else 1
    print(f"  归约 {len(pages)} 个页知识 → {'两层概览' if make_elements else '导航概览'}", flush=True)

    print(f"  [1/{steps}] 归约导航层 _app.md …", flush=True)
    nav = build_navigation_summary(app, pages)
    nav_path = app_dir / "_app.md"
    nav_path.write_text(nav, encoding="utf-8")
    print(f"        ✓ {nav_path.name} ({len(nav)} 字)", flush=True)

    elements = ""
    if make_elements:
        print(f"  [2/{steps}] 归约元素层 _elements.md …", flush=True)
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


def list_known_apps(platform: str = "iphone") -> list[str]:
    """App names with a knowledge dir under knowledge/<platform>/.

    Used by the router (chat_session.route_message) so it treats these apps as
    fully specified instead of asking the user for facts — entry URL, access,
    usage — that knowledge injection provides downstream at decompose time.
    """
    platform_dir = KNOWLEDGE_DIR / platform
    if not platform_dir.is_dir():
        return []
    return sorted(d.name for d in platform_dir.iterdir() if d.is_dir() and any(d.glob("*.md")))


def _read_dir_aliases(d: Path) -> list[str]:
    """Alternate names / common misspellings for an app, one per line in ``_aliases.md``.

    Lets ``auto_discover_knowledge`` match a goal that refers to the app by a nickname or a typo
    (e.g. "RebotTeam" for RoboTeam) instead of only its exact dir name. Lines starting with ``#``
    are comments; blanks ignored. Platform-agnostic (unlike the iPhone-only ``_APP_ALIASES``)."""
    p = d / "_aliases.md"
    if not p.exists():
        return []
    out: list[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def load_app_dir(d: Path) -> AppKnowledge | None:
    """Load an app's knowledge (nav + overlays + sections) from its dir, or None if no _app.md.

    Shared by goal-substring discovery (``auto_discover_knowledge``) and exact-name binding
    (``load_knowledge_for_app``, used by the WebArena entry where the intent never names the
    site). Pins the hand-maintained _-prefixed overlays ABOVE the distilled nav (they survive
    re-ingest and aren't loaded as retrievable sections):
      _deploy.md — environment/access facts (entry URL, host, creds): where/how to reach this
                   instance. Per-instance config; overrides nothing.
      _update.md — current-version updates over the (older) distilled base. On conflict the agent
                   trusts these and the live UI. Folded back into the base on re-distill.
    """
    nav_path = d / "_app.md"
    if not nav_path.exists():
        return None
    nav = nav_path.read_text(encoding="utf-8").strip()
    channels: dict[str, int] = {}  # overlay file stem → char count (for the report)
    overlays = []
    for overlay_name in ("_deploy.md", "_update.md"):
        overlay_path = d / overlay_name
        if overlay_path.exists():
            text = overlay_path.read_text(encoding="utf-8").strip()
            channels[overlay_name[:-3]] = len(text)
            if text:
                overlays.append(text)
    if overlays:
        nav = "\n\n".join(overlays + [nav])
    # Reusable multi-step orchestrations (skills): appended after the nav so the decomposer sees
    # both the layout and the workflows. Hand-maintained, _-prefixed (not a retrievable section).
    skill_path = d / "_skill.md"
    if skill_path.exists():
        skill = skill_path.read_text(encoding="utf-8").strip()
        channels["_skill"] = len(skill)
        if skill:
            for issue in validate_skill_doc(skill):
                print(f"  [Skill] ⚠️ {issue}")
            nav = f"{nav}\n\n{skill}"
    elements_path = d / "_elements.md"
    elements = elements_path.read_text(encoding="utf-8").strip() if elements_path.exists() else ""
    check_path = d / "_check.md"
    check = check_path.read_text(encoding="utf-8").strip() if check_path.exists() else ""
    if check_path.exists():
        channels["_check"] = len(check)
    # Per-section page files (excludes _app.md/_elements.md) → progressive-load bodies.
    sections = {stem: body for stem, body in load_page_files(d)}
    return AppKnowledge(navigation=nav, elements=elements, app_name=d.name,
                        sections=sections, check=check, overlays=channels)


def load_knowledge_for_app(app: str, platform: str = "browser") -> AppKnowledge | None:
    """Load knowledge by EXACT app/dir name (no goal-substring match).

    The WebArena entry binds knowledge by the task's ``sites`` tag — the intent never names the
    site — so it needs a direct loader keyed on ``knowledge/<platform>/<app>/`` rather than
    ``auto_discover_knowledge``'s substring match against the goal text."""
    d = KNOWLEDGE_DIR / platform / app
    return load_app_dir(d) if d.is_dir() else None


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
                # Per-app aliases / misspellings (knowledge/<plat>/<app>/_aliases.md) — platform-
                # agnostic, so a typo'd or nicknamed goal still discovers the knowledge. The exact
                # dir name (set above) wins; aliases only fill gaps.
                for alias in _read_dir_aliases(d):
                    candidates.setdefault(alias.lower(), d)
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
        knowledge = load_app_dir(d)
        if knowledge is not None:
            return knowledge
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
