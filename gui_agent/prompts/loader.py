"""Markdown prompt loader.

Prompts are authored as Markdown files with a small YAML-like frontmatter block.
The loader intentionally supports only the subset we use (scalars and simple
dash lists), so prompt loading stays dependency-free and predictable.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from string import Formatter
from typing import Any

PROMPT_ROOT = Path(__file__).resolve().parent


class PromptRegistryError(RuntimeError):
    """Raised when prompt assets are malformed or ambiguous."""


@dataclass(frozen=True)
class PromptTemplate:
    """One prompt or context block loaded from disk."""

    id: str
    source_type: str
    body: str
    path: Path
    platform: str = ""
    scope: tuple[str, ...] = ()
    owner: str = ""
    schema: str = ""
    eval_suites: tuple[str, ...] = ()
    version: int = 1
    metadata: dict[str, Any] | None = None

    @property
    def placeholders(self) -> tuple[str, ...]:
        names: list[str] = []
        try:
            parsed = Formatter().parse(self.body)
            for _, field_name, _, _ in parsed:
                if field_name and field_name not in names:
                    names.append(field_name)
        except ValueError:
            # Some system prompts contain literal JSON examples and are never
            # rendered via str.format. Keep metadata inspection total-order safe.
            return ()
        return tuple(names)

    def render(self, **values: object) -> str:
        """Render with Python's existing ``str.format`` semantics."""
        return self.body.format(**values)


def load_prompt_text(prompt_id: str) -> str:
    """Return only the prompt body for the given prompt id."""
    return load_prompt(prompt_id).body


def load_prompt(prompt_id: str) -> PromptTemplate:
    """Load one prompt by id."""
    registry = _registry()
    try:
        return registry[prompt_id]
    except KeyError as exc:
        raise PromptRegistryError(f"Unknown prompt id: {prompt_id}") from exc


def iter_prompt_templates() -> tuple[PromptTemplate, ...]:
    """Return all registered prompt templates, sorted by id."""
    return tuple(_registry()[key] for key in sorted(_registry()))


@lru_cache(maxsize=1)
def _registry() -> dict[str, PromptTemplate]:
    prompts: dict[str, PromptTemplate] = {}
    for path in sorted(PROMPT_ROOT.rglob("*.md")):
        meta, body = _split_frontmatter(path)
        prompt_id = str(meta.get("id") or "").strip()
        if not prompt_id:
            raise PromptRegistryError(f"{path} is missing frontmatter id")
        if prompt_id in prompts:
            other = prompts[prompt_id].path
            raise PromptRegistryError(f"Duplicate prompt id {prompt_id}: {other} and {path}")
        prompts[prompt_id] = PromptTemplate(
            id=prompt_id,
            source_type=str(meta.get("source_type") or "").strip(),
            platform=str(meta.get("platform") or "").strip(),
            scope=_as_tuple(meta.get("scope")),
            owner=str(meta.get("owner") or "").strip(),
            schema=str(meta.get("schema") or "").strip(),
            eval_suites=_as_tuple(meta.get("eval_suites")),
            version=_as_int(meta.get("version"), default=1),
            body=body,
            path=path,
            metadata=meta,
        )
    return prompts


def _split_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise PromptRegistryError(f"{path} is missing frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise PromptRegistryError(f"{path} has unterminated frontmatter")
    raw_meta = text[4:end]
    body = text[end + len("\n---\n") :]
    return _parse_frontmatter(raw_meta), body


def _parse_frontmatter(raw: str) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    current_list_key: str | None = None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if current_list_key and stripped.startswith("- "):
            value = stripped[2:].strip()
            meta.setdefault(current_list_key, []).append(_parse_scalar(value))
            continue
        current_list_key = None
        if ":" not in line:
            raise PromptRegistryError(f"Invalid frontmatter line: {line}")
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not value:
            meta[key] = []
            current_list_key = key
        else:
            meta[key] = _parse_scalar(value)
    return meta


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"true", "false"}:
        return value == "true"
    if value.isdigit():
        return int(value)
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in {"'", '"'}
    ):
        return value[1:-1]
    return value


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return (str(value),)


def _as_int(value: Any, *, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
