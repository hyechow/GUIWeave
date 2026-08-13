"""Deterministic application/site identity resolution for knowledge binding.

This module resolves *which application knowledge may apply*.  It does not
rewrite goals, choose a platform, plan navigation, or use an LLM.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Iterable, Literal
from urllib.parse import urlparse

from gui_agent.core.self_learning.app_summary import (
    list_known_apps,
    load_knowledge_for_app,
)


RouteConfidence = Literal["exact", "strong"]
_URL_RE = re.compile(r'''https?://[^\s\]\[(){}<>"'）】、,，。；;]+''')
_IDENTITY_KEYS = {
    "android": ("android_packages", "package_ids"),
    "iphone": ("iphone_bundle_ids", "bundle_ids"),
    "browser": (),
}


def _items(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    text = str(value or "").strip()
    return (text,) if text else ()


def _identity_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))


def _name_aliases(name: str) -> tuple[str, ...]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("_", " ")
    return tuple(dict.fromkeys((name, spaced)))


def _mention_spans(goal: str, alias: str) -> tuple[tuple[int, int], ...]:
    normalized_goal = unicodedata.normalize("NFKC", goal).casefold()
    normalized_alias = unicodedata.normalize("NFKC", alias).casefold()
    tokens = re.findall(r"[^\W_]+", normalized_alias, flags=re.UNICODE)
    if not tokens:
        return ()
    pattern = r"[\W_]*".join(map(re.escape, tokens))
    spans: list[tuple[int, int]] = []
    for match in re.finditer(pattern, normalized_goal, flags=re.UNICODE):
        before = normalized_goal[match.start() - 1] if match.start() else ""
        after = normalized_goal[match.end()] if match.end() < len(normalized_goal) else ""
        if _latin_or_digit(tokens[0][0]) and _latin_or_digit(before):
            continue
        if _latin_or_digit(tokens[-1][-1]) and _latin_or_digit(after):
            continue
        spans.append(match.span())
    return tuple(spans)


def _latin_or_digit(value: str) -> bool:
    if not value:
        return False
    return value.isdigit() or unicodedata.name(value, "").startswith("LATIN ")


def _origin(value: str) -> tuple[str, str, int | None] | None:
    try:
        parsed = urlparse(value)
        host = (parsed.netloc or "").casefold()
        name = (parsed.hostname or "").casefold()
        port = parsed.port
    except (TypeError, ValueError):
        return None
    return (host, name, port) if host else None


@dataclass(frozen=True)
class AppRouteRecord:
    """Machine-readable identity for one platform-scoped app/site."""

    app_id: str
    platform: str
    aliases: tuple[str, ...] = ()
    origins: tuple[str, ...] = ()
    platform_ids: tuple[str, ...] = ()

    @property
    def all_aliases(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*_name_aliases(self.app_id), *self.aliases)))


@dataclass(frozen=True)
class AppRoute:
    app_id: str
    platform: str
    confidence: RouteConfidence
    evidence: tuple[str, ...]
    active: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class AppRouteResult:
    platform: str
    targets: tuple[AppRoute, ...]
    active_app: str | None = None
    needs_clarification: bool = False
    clarification: str = ""

    @property
    def app_ids(self) -> tuple[str, ...]:
        return tuple(route.app_id for route in self.targets)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "deterministic_app_router",
            "platform": self.platform,
            "targets": [route.to_dict() for route in self.targets],
            "active_app": self.active_app,
            "needs_clarification": self.needs_clarification,
            "clarification": self.clarification,
        }


def knowledge_route_records(platform: str) -> tuple[AppRouteRecord, ...]:
    """Build the identity registry from active user/builtin knowledge metadata."""

    records: list[AppRouteRecord] = []
    for app_id in list_known_apps(platform):
        knowledge = load_knowledge_for_app(app_id, platform)
        if knowledge is None:
            continue
        metadata = {
            **knowledge.metadata.get("_app", {}),
            **knowledge.metadata.get("_deploy", {}),
        }
        aliases = _items(metadata.get("aliases"))
        origins = [*_items(metadata.get("browser_origins"))]
        origins.extend(match.group(0) for match in _URL_RE.finditer(knowledge.deployment))
        platform_ids = tuple(
            identifier
            for key in _IDENTITY_KEYS.get(platform, ())
            for identifier in _items(metadata.get(key))
        )
        records.append(AppRouteRecord(
            app_id=app_id,
            platform=platform,
            aliases=aliases,
            origins=tuple(dict.fromkeys(origins)),
            platform_ids=tuple(dict.fromkeys(platform_ids)),
        ))
    return tuple(records)


def _active_match(
    records: tuple[AppRouteRecord, ...],
    *,
    current_url: str,
    current_app_id: str,
) -> tuple[str | None, str, str]:
    if current_app_id:
        key = _identity_key(current_app_id)
        matches = {
            record.app_id
            for record in records
            if key in {
                _identity_key(value)
                for value in (record.app_id, *record.platform_ids)
            }
        }
        if len(matches) == 1:
            return next(iter(matches)), f"platform_id:{current_app_id}", ""
        if len(matches) > 1:
            return None, "", f"platform id {current_app_id!r} matches {sorted(matches)}"
    current = _origin(current_url)
    if current is None:
        return None, "", ""
    host, hostname, port = current
    exact: set[str] = set()
    local_port: set[str] = set()
    for record in records:
        for value in record.origins:
            candidate = _origin(value)
            if candidate is None:
                continue
            candidate_host, _candidate_name, candidate_port = candidate
            if candidate_host == host:
                exact.add(record.app_id)
            elif (
                port is not None
                and hostname in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
                and candidate_port == port
            ):
                local_port.add(record.app_id)
    matches = exact or local_port
    if len(matches) == 1:
        return next(iter(matches)), f"current_url:{host}", ""
    if len(matches) > 1:
        return None, "", f"current URL {host!r} matches {sorted(matches)}"
    return None, "", ""


def resolve_app_routes(
    goal: str,
    platform: str,
    *,
    current_url: str = "",
    current_app_id: str = "",
    records: Iterable[AppRouteRecord] | None = None,
) -> AppRouteResult:
    """Resolve target and active apps without model inference or goal rewriting."""

    registry = tuple(records) if records is not None else knowledge_route_records(platform)
    registry = tuple(record for record in registry if record.platform == platform)
    alias_index: dict[str, set[str]] = {}
    alias_variants: dict[str, list[str]] = {}
    for record in registry:
        for alias in record.all_aliases:
            key = _identity_key(alias)
            if not key:
                continue
            alias_index.setdefault(key, set()).add(record.app_id)
            variants = alias_variants.setdefault(key, [])
            if alias not in variants:
                variants.append(alias)

    evidence: dict[str, list[str]] = {}
    exact_apps: set[str] = set()
    strong_apps: set[str] = set()
    ambiguities: list[str] = []
    active_app, active_evidence, active_ambiguity = _active_match(
        registry,
        current_url=current_url,
        current_app_id=current_app_id,
    )
    goal_mentions: dict[tuple[str, int, int], str] = {}
    for key, aliases in alias_variants.items():
        for alias in aliases:
            for start, end in _mention_spans(goal, alias):
                goal_mentions.setdefault((key, start, end), alias)
    mentions = [
        (start, end, key, alias, alias_index[key])
        for (key, start, end), alias in goal_mentions.items()
    ]
    mentions = [
        mention
        for mention in mentions
        if not any(
            other[0] <= mention[0]
            and mention[1] <= other[1]
            and other[1] - other[0] > mention[1] - mention[0]
            for other in mentions
        )
    ]
    for _start, _end, _key, alias, matches in mentions:
        if len(matches) != 1:
            continue
        app_id = next(iter(matches))
        strong_apps.add(app_id)
        evidence.setdefault(app_id, []).append(f"goal_alias:{alias}")

    resolved_apps = strong_apps | ({active_app} if active_app else set())
    for _start, _end, _key, alias, matches in mentions:
        if len(matches) == 1 or matches.issubset(resolved_apps):
            continue
        if len(matches.intersection(resolved_apps)) != 1:
            ambiguities.append(f"goal alias {alias!r} matches {sorted(matches)}")

    target_ids = set(strong_apps)
    if active_ambiguity and not target_ids:
        ambiguities.append(active_ambiguity)
    if active_app and active_app in target_ids:
        evidence.setdefault(active_app, []).append(active_evidence)
        exact_apps.add(active_app)
    elif not target_ids and active_app:
        target_ids.add(active_app)
        evidence.setdefault(active_app, []).append(active_evidence)
        exact_apps.add(active_app)

    targets = tuple(
        AppRoute(
            app_id=app_id,
            platform=platform,
            confidence="exact" if app_id in exact_apps else "strong",
            evidence=tuple(dict.fromkeys(evidence.get(app_id, ()))),
            active=app_id == active_app,
        )
        for app_id in sorted(target_ids)
    )
    clarification = "; ".join(dict.fromkeys(ambiguities))
    return AppRouteResult(
        platform=platform,
        targets=targets,
        active_app=active_app,
        needs_clarification=bool(clarification),
        clarification=clarification,
    )


__all__ = [
    "AppRoute",
    "AppRouteRecord",
    "AppRouteResult",
    "knowledge_route_records",
    "resolve_app_routes",
]
