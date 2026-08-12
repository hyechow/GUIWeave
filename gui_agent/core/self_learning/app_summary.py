"""Load platform-scoped application knowledge for Tool Agent."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from gui_agent.core.self_learning.progressive import (
    ProgressiveKnowledge,
    split_frontmatter,
)
from gui_agent.core.self_learning.paths import (
    BUILTIN_KNOWLEDGE_ROOT,
    get_user_knowledge_root,
)

KNOWLEDGE_DIR = BUILTIN_KNOWLEDGE_ROOT


def _knowledge_roots() -> tuple[Path, ...]:
    """Return user knowledge first, then repository knowledge."""

    user = get_user_knowledge_root()
    builtin = KNOWLEDGE_DIR.resolve()
    return (user,) if user == builtin else (user, builtin)


def _app_dirs(platform: str) -> list[Path]:
    """Return one highest-precedence directory per app name."""

    found: dict[str, Path] = {}
    for root in _knowledge_roots():
        platform_dir = root / platform
        if not platform_dir.is_dir():
            continue
        for path in sorted(platform_dir.iterdir()):
            if path.is_dir() and any(path.glob("*.md")):
                found.setdefault(path.name.casefold(), path)
    return list(found.values())


@dataclass
class AppKnowledge:
    """Functional facts and private deployment context for one app/site."""

    navigation: str
    elements: str
    app_name: str
    deployment: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    check: str = ""
    metadata: dict[str, dict[str, Any]] = field(default_factory=dict)
    overlays: dict[str, int] = field(default_factory=dict)

    def orchestrator_sections(self, goal: str) -> list[str]:
        eligible = {
            stem: text
            for stem, text in self.sections.items()
            if "orchestrator"
            in {
                str(item).strip()
                for item in _as_list(split_frontmatter(text)[0].get("scope"))
            }
        }
        if not eligible:
            return []
        return ProgressiveKnowledge(eligible).match_signals(
            [goal],
            min_overlap=2,
            match_titles=False,
        )

    def orchestrator_context(self, goal: str) -> str:
        from gui_agent.context import render_context_blocks

        stems = self.orchestrator_sections(goal)
        if stems:
            selected = ProgressiveKnowledge(
                {stem: self.sections[stem] for stem in stems}
            )
            return render_context_blocks(
                selected.body_blocks(stems), include_headers=False
            )
        nav_scope = _as_list(self.metadata.get("_app", {}).get("scope"))
        if self.metadata and "orchestrator" not in {
            str(item).strip() for item in nav_scope
        }:
            return ""
        navigation = self.navigation
        if self.deployment and navigation.startswith(self.deployment):
            navigation = navigation[len(self.deployment) :].lstrip()
        return navigation

    def summary(self) -> dict[str, object]:
        return {
            "app_name": self.app_name,
            "profile": "with-skills" if "_skill" in self.overlays else "functional-only",
            "nav_chars": len(self.navigation),
            "deployment_chars": len(self.deployment),
            "elements_chars": len(self.elements),
            "check_chars": len(self.check),
            "section_count": len(self.sections),
            "overlays": dict(self.overlays),
            "metadata_keys": sorted(self.metadata),
        }


def _as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    return [] if value in (None, "") else [value]


def _read_markdown(path: Path) -> tuple[dict[str, Any], str]:
    metadata, body = split_frontmatter(path.read_text(encoding="utf-8"))
    return metadata, body.strip()


def load_page_files(app_dir: Path) -> list[tuple[str, str]]:
    return [
        (path.stem, path.read_text(encoding="utf-8"))
        for path in sorted(app_dir.glob("*.md"))
        if not path.name.startswith("_")
    ]


def list_known_apps(platform: str = "browser") -> list[str]:
    return sorted(path.name for path in _app_dirs(platform))


def match_app_by_url(url: str, platform: str = "browser") -> str | None:
    """Match a URL to a knowledge directory through `_deploy.md` entry URLs."""

    if not url:
        return None
    try:
        current = urlparse(url)
        current_host = (current.netloc or "").lower()
        current_name = (current.hostname or "").lower()
        current_port = current.port
    except (TypeError, ValueError):
        return None
    if not current_host:
        return None
    port_matches: list[str] = []
    for app_dir in _app_dirs(platform):
        deploy = app_dir / "_deploy.md"
        if not deploy.is_file():
            continue
        try:
            text = deploy.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in re.finditer(r"https?://[^\s）)、,，。；;]+", text):
            parsed = urlparse(match.group(0))
            if (parsed.netloc or "").lower() == current_host:
                return app_dir.name
            try:
                entry_port = parsed.port
            except ValueError:
                entry_port = None
            if (
                current_port is not None
                and current_name in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
                and entry_port == current_port
            ):
                port_matches.append(app_dir.name)
    return port_matches[0] if len(set(port_matches)) == 1 else None


def _aliases(app_dir: Path) -> list[str]:
    deploy = app_dir / "_deploy.md"
    if not deploy.exists():
        return []
    aliases = _read_markdown(deploy)[0].get("aliases")
    return [str(item).strip() for item in _as_list(aliases) if str(item).strip()]


def load_app_dir(
    app_dir: Path,
    *,
    include_skills: bool = False,
) -> AppKnowledge | None:
    nav_path = app_dir / "_app.md"
    if not nav_path.exists():
        return None
    metadata: dict[str, dict[str, Any]] = {}
    nav_meta, navigation = _read_markdown(nav_path)
    if nav_meta:
        metadata["_app"] = nav_meta
    overlays: dict[str, int] = {}
    deployment = ""
    pinned: list[str] = []
    for name in ("_deploy.md", "_update.md"):
        path = app_dir / name
        if not path.exists():
            continue
        meta, body = _read_markdown(path)
        stem = path.stem
        metadata[stem] = meta
        overlays[stem] = len(body)
        if body:
            pinned.append(body)
        if name == "_deploy.md":
            deployment = body
    if pinned:
        navigation = "\n\n".join([*pinned, navigation])
    skill_path = app_dir / "_skill.md"
    if include_skills and skill_path.exists():
        meta, body = _read_markdown(skill_path)
        metadata["_skill"] = meta
        overlays["_skill"] = len(body)
        if body:
            navigation = f"{navigation}\n\n{body}"
    elements_path = app_dir / "_elements.md"
    elements = ""
    if elements_path.exists():
        metadata["_elements"], elements = _read_markdown(elements_path)
    check_path = app_dir / "_check.md"
    check = ""
    if check_path.exists():
        metadata["_check"], check = _read_markdown(check_path)
        overlays["_check"] = len(check)
    sections = dict(load_page_files(app_dir))
    for stem, text in sections.items():
        section_meta, _ = split_frontmatter(text)
        if section_meta:
            metadata[stem] = section_meta
    return AppKnowledge(
        navigation=navigation,
        elements=elements,
        app_name=app_dir.name,
        deployment=deployment,
        sections=sections,
        check=check,
        metadata=metadata,
        overlays=overlays,
    )


def load_knowledge_for_app(
    app: str,
    platform: str = "browser",
    *,
    include_skills: bool = False,
) -> AppKnowledge | None:
    requested = app.casefold()
    app_dir = next(
        (path for path in _app_dirs(platform) if path.name.casefold() == requested),
        None,
    )
    return load_app_dir(app_dir, include_skills=include_skills) if app_dir else None


def auto_discover_knowledge(
    goal: str,
    platform: str = "browser",
    *,
    include_skills: bool = False,
) -> AppKnowledge | None:
    goal_lower = goal.lower()
    candidates: dict[str, Path] = {}
    for app_dir in _app_dirs(platform):
        candidates[app_dir.name.lower()] = app_dir
        candidates.setdefault(app_dir.name.replace("_", " ").lower(), app_dir)
        for alias in _aliases(app_dir):
            candidates.setdefault(alias.lower(), app_dir)
    for name, app_dir in candidates.items():
        if name in goal_lower:
            return load_app_dir(app_dir, include_skills=include_skills)
    return None


__all__ = [
    "AppKnowledge",
    "auto_discover_knowledge",
    "list_known_apps",
    "load_app_dir",
    "load_knowledge_for_app",
    "match_app_by_url",
]
