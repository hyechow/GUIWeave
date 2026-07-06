"""Deterministic reads from JSON URLs embedded in orchestrator runs."""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable
from typing import Any


_URL_RE = re.compile(r"https?://[^\s一-鿿]+")
_TRAILING_URL_PUNCT = ").,;，。）】」』\"'"


def _first_url(text: str) -> str | None:
    match = _URL_RE.search(text or "")
    if not match:
        return None
    return match.group(0).rstrip(_TRAILING_URL_PUNCT) or None


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _snake(value: str) -> str:
    s = re.sub(r"[^0-9A-Za-z]+", "_", value or "").strip("_").lower()
    return re.sub(r"_+", "_", s)


def _quoted_json_keys(text: str) -> list[str]:
    keys: list[str] = []
    for match in re.finditer(r'"([^"]+)"|`([^`]+)`|“([^”]+)”|「([^」]+)」', text or ""):
        key = next((part for part in match.groups() if part), "")
        if key and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            keys.append(key)
    return keys


def _field_read_context(read_spec: str, field: str) -> str:
    if not read_spec:
        return ""
    field_norm = _norm(field)
    lines = [line for line in read_spec.splitlines() if field_norm and field_norm in _norm(line)]
    return "\n".join(lines) if lines else read_spec


def _scalar_to_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | str):
        return str(value)
    return ""


def _wants_count(field: str, context: str) -> bool:
    haystack = f"{field}\n{context}".lower()
    return any(token in haystack for token in ("count", "number", "length", "数量", "个数", "统计", "数组长度"))


def _candidate_keys(field: str, context: str) -> list[str]:
    keys: list[str] = []
    keys.extend(_quoted_json_keys(context))
    field_norm = _norm(field)
    if "star" in field_norm or "星" in field:
        keys.append("stargazers_count")
    if "contributor" in field_norm or "贡献" in field:
        keys.extend(["contributors_count", "contributors"])
    keys.extend([field, _snake(field)])

    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def extract_json_api_reads(data: Any, returns: list[str], read_spec: str = "") -> dict[str, str] | None:
    """Extract requested scalar returns from already-parsed JSON.

    Returns ``None`` when this JSON shape/read spec cannot be handled deterministically, so callers
    can fall back to visual structured-read.
    """

    if not returns:
        return {}

    reads: dict[str, str] = {}
    handled = False
    for field in returns:
        context = _field_read_context(read_spec, field)
        value = ""
        if isinstance(data, dict):
            for key in _candidate_keys(field, context):
                if key in data:
                    value = _scalar_to_text(data.get(key))
                    if value:
                        handled = True
                        break
            if not value:
                for key in _candidate_keys(field, context):
                    nested = data.get(key)
                    if isinstance(nested, list) and _wants_count(field, context):
                        value = str(len(nested))
                        handled = True
                        break
        elif isinstance(data, list) and _wants_count(field, context):
            value = str(len(data))
            handled = True
        reads[field] = value

    return reads if handled else None


def _default_fetch_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "gui-agent-json-read",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - user-visible task URL
        raw = response.read(2_000_000)
    return json.loads(raw.decode("utf-8"))


def read_json_url_returns(
    source_text: str,
    returns: list[str],
    read_spec: str = "",
    *,
    fetch_json: Callable[[str], Any] | None = None,
    timeout: float = 8.0,
) -> dict[str, str] | None:
    """Fetch and parse a JSON URL mentioned in a run target, when the read spec calls for JSON.

    This is a narrow deterministic backstop for API pages that are already part of the visible UI
    workflow. If the URL is absent, not JSON-shaped, or the fields cannot be mapped, return ``None``.
    """

    if not returns:
        return {}
    url = _first_url(source_text)
    if not url:
        return None
    if "json" not in (read_spec or "").lower() and "api." not in url.lower():
        return None
    fetch = fetch_json or (lambda target: _default_fetch_json(target, timeout))
    try:
        data = fetch(url)
    except Exception:  # noqa: BLE001 - deterministic backstop only; visual read is the fallback
        return None
    return extract_json_api_reads(data, returns, read_spec)
