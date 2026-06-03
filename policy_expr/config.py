"""Configuration helpers for policy_expr."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from llm.provider_config import ChatProviderConfig, resolve_chat_provider_config

# Default config.yaml (cheap qwen3.5). Override with POLICY_EXPR_CONFIG to point
# at an alternate file, e.g. config.qwen36.yaml for the 3.6 comparison runs:
#   POLICY_EXPR_CONFIG=policy_expr/config.qwen36.yaml ./bin/runner ...
_env_config = os.environ.get("POLICY_EXPR_CONFIG")
CONFIG_PATH = Path(_env_config) if _env_config else Path(__file__).with_name("config.yaml")

_NAMED_CONFIGS: dict[str, Path] = {
    "qwen35": Path(__file__).with_name("config.yaml"),
    "qwen36": Path(__file__).with_name("config.qwen36.yaml"),
}

_active_config_path: Path = CONFIG_PATH


def active_config_name() -> str:
    """Return the short name of the currently active config, or its filename."""
    for name, path in _NAMED_CONFIGS.items():
        if path == _active_config_path:
            return name
    return _active_config_path.name


def switch_config(name_or_path: str) -> Path:
    """Switch active config by name ('qwen35'/'qwen36') or file path. Clears lru_cache."""
    global _active_config_path
    resolved = _NAMED_CONFIGS.get(name_or_path) or Path(name_or_path)
    _active_config_path = resolved
    load_config.cache_clear()
    return resolved


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    if not _active_config_path.exists():
        return {}
    data = yaml.safe_load(_active_config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{_active_config_path} must contain a YAML mapping")
    return data


def resolve_llm_config(name: str) -> ChatProviderConfig:
    """Resolve LLM config by dotted name (e.g. 'supervisor.checker')."""

    llm_config = load_config().get("llm", {})
    if not isinstance(llm_config, dict):
        raise ValueError("policy_expr config 'llm' must be a mapping")

    section = llm_config
    for key in name.split("."):
        if not isinstance(section, dict):
            raise ValueError(f"policy_expr config llm.{name} must be a mapping")
        section = section.get(key, {})

    if not isinstance(section, dict):
        raise ValueError(f"policy_expr config llm.{name} must be a mapping")
    return resolve_chat_provider_config(
        provider=_optional_str(section.get("provider")),
        model=_optional_str(section.get("model")),
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None
