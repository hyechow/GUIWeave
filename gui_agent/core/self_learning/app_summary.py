"""Generate app-level knowledge files from per-page knowledge.

Produces two files in knowledge/{platform}/{app}/ (platform defaults to iphone, the only
platform with per-page recon today):
- _app.md: Navigation structure for orchestrator planning
- _elements.md: UI element details for Planner (instruction generation)

Hand-maintained `_`-prefixed siblings survive re-ingest. `_deploy.md` and `_update.md` are folded
into the always-on Supervisor context. `_skill.md` is an optional orchestration accelerator and is
loaded only when a caller explicitly opts in; correctness must come from functional knowledge.

Usage:
    uv run python -m gui_agent.core.self_learning.app_summary 微信
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from gui_agent.core.config import resolve_llm_config
from gui_agent.prompts import load_prompt, load_prompt_text

KNOWLEDGE_DIR = Path(__file__).resolve().parents[3] / "knowledge"

# ── Prompts for _app.md (navigation structure) ─────────────────────────────

_NAV_SYSTEM = load_prompt_text("task.self_learning.app_summary.nav_system")
_NAV_PROMPT = load_prompt("task.self_learning.app_summary.nav_prompt")  # rendered: {app}/{n}/{pages_text}

# ── Prompts for _elements.md (UI elements) ────────────────────────────────

_ELEMENTS_SYSTEM = load_prompt_text("task.self_learning.app_summary.elements_system")
_ELEMENTS_PROMPT = load_prompt("task.self_learning.app_summary.elements_prompt")  # rendered: {app}/{n}/{pages_text}


@dataclass
class AppKnowledge:
    """Two-layer knowledge for an app."""
    navigation: str  # _app.md content → Supervisor
    elements: str    # _elements.md content → statement execution and fallback
    app_name: str
    sections: dict[str, str] = field(default_factory=dict)  # per-section bodies → progressive load
    check: str = ""  # _check.md content → Checker-only observable completion rules
    metadata: dict[str, dict[str, Any]] = field(default_factory=dict)  # channel/stem -> frontmatter
    # Hand-maintained overlay channels actually present this run → {"_check"|"_deploy"|"_skill"|
    # "_update": char_count}. _deploy/_update are always-on; _skill is explicit opt-in. Tracked here
    # purely so the report can show each channel's loaded state. Absent file → key absent.
    overlays: dict[str, int] = field(default_factory=dict)

    def orchestrator_sections(self, goal: str) -> list[str]:
        """Pick functional sections relevant to initial Program planning.

        Only sections explicitly scoped to ``orchestrator`` participate. This keeps page-level HOW
        out of the initial Program while making resource ownership, field semantics, and stable
        capability constraints available before execution.
        """
        from gui_agent.core.self_learning.progressive import ProgressiveKnowledge

        eligible: dict[str, str] = {}
        for stem, text in self.sections.items():
            meta, _ = _split_knowledge_frontmatter(text)
            scope = meta.get("scope") or []
            if isinstance(scope, str):
                scope = [scope]
            if "orchestrator" in {str(item).strip() for item in scope}:
                eligible[stem] = text
        if not eligible:
            return []
        return ProgressiveKnowledge(eligible).match_signals([goal])

    def orchestrator_context(self, goal: str) -> str:
        """Application overview plus a small goal-matched functional knowledge slice."""
        from gui_agent.core.self_learning.progressive import ProgressiveKnowledge

        stems = self.orchestrator_sections(goal)
        if not stems:
            return self.navigation
        selected = ProgressiveKnowledge({stem: self.sections[stem] for stem in stems}).bodies(stems)
        return f"{self.navigation}\n\n{selected}" if selected else self.navigation

    def summary(self) -> dict[str, object]:
        """Compact, log-friendly description of what got injected (→ context.json knowledge)."""
        return {
            "app_name": self.app_name,
            "profile": "with-skills" if "_skill" in self.overlays else "functional-only",
            "nav_chars": len(self.navigation),
            "elements_chars": len(self.elements),
            "check_chars": len(self.check),
            "section_count": len(self.sections),
            "overlays": dict(self.overlays),
            "metadata_keys": sorted(self.metadata),
            "overlay_metadata": {
                key: value for key, value in self.metadata.items()
                if key.startswith("_")
            },
        }


def _split_knowledge_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split metadata from knowledge markdown."""
    from gui_agent.core.self_learning.progressive import split_frontmatter

    return split_frontmatter(text)


def _read_knowledge_markdown(path: Path) -> tuple[dict[str, Any], str]:
    meta, body = _split_knowledge_frontmatter(path.read_text(encoding="utf-8"))
    return meta, body.strip()


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
    from llm.provider_config import dashscope_extra_body

    # Prefer thinking off for latency on small reductions; models that force thinking stay on.
    llm = ChatOpenAI(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        temperature=0,
        extra_body=dashscope_extra_body(cfg.model),
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
        _NAV_PROMPT.render(app=app, n=len(pages), pages_text=pages_text),
    )


def build_elements_summary(app: str, pages: list[tuple[str, str]]) -> str:
    """Generate UI elements summary (_elements.md) for Planner."""
    pages_text = "\n\n---\n\n".join(
        f"### {name}\n{_page_body(content)}" for name, content in pages
    )
    return _call_llm(
        _ELEMENTS_SYSTEM,
        _ELEMENTS_PROMPT.render(app=app, n=len(pages), pages_text=pages_text),
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


def match_app_by_url(url: str, platform: str = "iphone") -> str | None:
    """Match a front-tab URL to a known app by the entry-URL host in its _deploy.md.

    The IP:port in a browser URL (e.g. http://192.168.31.57:22000/map/list) is opaque to an
    LLM; this maps it to the app's directory name (e.g. 'RoboTeam') by exact host+port first,
    then by a unique port fallback for Docker redirects such as localhost:7780. Returns None
    when the URL doesn't match any known app (e.g. a Google new-tab page), the port is
    ambiguous, or the platform has no url-keyed knowledge (iphone/android). Gives
    route/plan with a semantic site name instead of a bare IP.
    """
    from urllib.parse import urlparse

    if not url:
        return None
    try:
        cur = urlparse(url)
        cur_host = (cur.netloc or "").lower()
        cur_name = (cur.hostname or "").lower()
        cur_port = cur.port
    except Exception:
        return None
    if not cur_host:
        return None
    platform_dir = KNOWLEDGE_DIR / platform
    if not platform_dir.is_dir():
        return None
    port_matches: list[str] = []
    for d in sorted(platform_dir.iterdir()):
        if not d.is_dir():
            continue
        deploy = d / "_deploy.md"
        if not deploy.is_file():
            continue
        try:
            text = deploy.read_text(encoding="utf-8")
        except Exception:
            continue
        for m in re.finditer(r"https?://[^\s）)、,，。；;]+", text):
            parsed = urlparse(m.group(0))
            entry_host = (parsed.netloc or "").lower()
            if entry_host and entry_host == cur_host:
                return d.name
            try:
                entry_port = parsed.port
            except ValueError:
                entry_port = None
            if (
                cur_port is not None
                and cur_name in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
                and entry_port == cur_port
            ):
                port_matches.append(d.name)
    return port_matches[0] if len(set(port_matches)) == 1 else None


def list_known_apps(platform: str = "iphone") -> list[str]:
    """App names with a knowledge dir under knowledge/<platform>/.

    Used by the router (chat_session.route_message) so it treats these apps as
    fully specified instead of asking the user for facts — entry URL, access,
    usage — that knowledge injection provides downstream at planning time.
    """
    platform_dir = KNOWLEDGE_DIR / platform
    if not platform_dir.is_dir():
        return []
    return sorted(d.name for d in platform_dir.iterdir() if d.is_dir() and any(d.glob("*.md")))


def _read_dir_aliases(d: Path) -> list[str]:
    """Alternate names / common misspellings for an app.

    Lets ``auto_discover_knowledge`` match a goal that refers to the app by a nickname or a typo
    (e.g. "RebotTeam" for RoboTeam) instead of only its exact dir name. Aliases live
    in ``_deploy.md`` frontmatter because they bind spoken names to this deployed app
    directory. Platform-agnostic (unlike the iPhone-only ``_APP_ALIASES``)."""
    deploy = d / "_deploy.md"
    if deploy.exists():
        meta, _ = _read_knowledge_markdown(deploy)
        aliases = meta.get("aliases")
        if isinstance(aliases, list):
            return [str(alias).strip() for alias in aliases if str(alias).strip()]
        if isinstance(aliases, str) and aliases.strip():
            return [aliases.strip()]
    return []


def load_app_dir(d: Path, *, include_skills: bool = False) -> AppKnowledge | None:
    """Load an app's knowledge (nav + overlays + sections) from its dir, or None if no _app.md.

    Shared by goal-substring discovery (``auto_discover_knowledge``) and exact-name binding
    (``load_knowledge_for_app``, used by callers whose task metadata names the app but whose
    goal text does not). Pins the hand-maintained _-prefixed overlays ABOVE the distilled nav (they survive
    re-ingest and aren't loaded as retrievable sections):
      _deploy.md — environment/access facts (entry URL, host, creds): where/how to reach this
                   instance. Per-instance config; overrides nothing.
      _update.md — current-version updates over the (older) distilled base. On conflict the agent
                   trusts these and the live UI. Folded back into the base on re-distill.
    """
    nav_path = d / "_app.md"
    if not nav_path.exists():
        return None
    metadata: dict[str, dict[str, Any]] = {}
    nav_meta, nav = _read_knowledge_markdown(nav_path)
    if nav_meta:
        metadata["_app"] = nav_meta
    channels: dict[str, int] = {}  # overlay file stem → char count (for the report)
    overlays = []
    for overlay_name in ("_deploy.md", "_update.md"):
        overlay_path = d / overlay_name
        if overlay_path.exists():
            meta, text = _read_knowledge_markdown(overlay_path)
            if meta:
                metadata[overlay_name[:-3]] = meta
            channels[overlay_name[:-3]] = len(text)
            if text:
                overlays.append(text)
    if overlays:
        nav = "\n\n".join(overlays + [nav])
    # Optional reusable orchestrations. They are deliberately excluded by default so a cold-start
    # app with functional documentation remains the correctness baseline.
    skill_path = d / "_skill.md"
    if include_skills and skill_path.exists():
        meta, skill = _read_knowledge_markdown(skill_path)
        if meta:
            metadata["_skill"] = meta
        channels["_skill"] = len(skill)
        if skill:
            for issue in validate_skill_doc(skill):
                print(f"  [Skill] ⚠️ {issue}")
            nav = f"{nav}\n\n{skill}"
    elements_path = d / "_elements.md"
    elements = ""
    if elements_path.exists():
        meta, elements = _read_knowledge_markdown(elements_path)
        if meta:
            metadata["_elements"] = meta
    check_path = d / "_check.md"
    check = ""
    if check_path.exists():
        meta, check = _read_knowledge_markdown(check_path)
        if meta:
            metadata["_check"] = meta
    if check_path.exists():
        channels["_check"] = len(check)
    # Per-section page files (excludes _app.md/_elements.md) → progressive-load bodies.
    sections = {stem: body for stem, body in load_page_files(d)}
    for stem, body in sections.items():
        meta, _ = _split_knowledge_frontmatter(body)
        if meta:
            metadata[stem] = meta
    return AppKnowledge(navigation=nav, elements=elements, app_name=d.name,
                        sections=sections, check=check, overlays=channels, metadata=metadata)


def load_knowledge_for_app(
    app: str,
    platform: str = "browser",
    *,
    include_skills: bool = False,
) -> AppKnowledge | None:
    """Load knowledge by EXACT app/dir name (no goal-substring match).

    Some benchmark/task-file entries bind knowledge by metadata such as a ``sites`` tag while the
    intent never names the site, so they need a direct loader keyed on ``knowledge/<platform>/<app>/`` rather than
    ``auto_discover_knowledge``'s substring match against the goal text."""
    d = KNOWLEDGE_DIR / platform / app
    return load_app_dir(d, include_skills=include_skills) if d.is_dir() else None


def auto_discover_knowledge(
    goal: str,
    platform: str = "iphone",
    *,
    include_skills: bool = False,
) -> AppKnowledge | None:
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
                # Per-app aliases / misspellings from _deploy.md frontmatter —
                # platform-agnostic, so a typo'd or nicknamed goal still discovers
                # the knowledge. The exact dir name wins; aliases only fill gaps.
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
        knowledge = load_app_dir(d, include_skills=include_skills)
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
