"""Configuration helpers for gui_agent."""

import copy
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from llm.provider_config import ChatProviderConfig, resolve_chat_provider_config

# Single config file. AGENT_MODEL selects a *profile* (default 'qwen37' = base, no
# override); a profile under `profiles:` deep-merges into `llm` to swap the core model,
# so adding a comparison model is one small block, not a whole duplicated file.
CONFIG_PATH = Path(__file__).with_name("config.yaml")
_DEFAULT_PROFILE = "qwen37"
_active_profile: str = os.environ.get("AGENT_MODEL", _DEFAULT_PROFILE)
_LLM_CONFIG_ALIASES = {
    "recon.navigator": ("back_nav",),
    "back_nav": ("recon.navigator",),
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@lru_cache(maxsize=1)
def _load_raw() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{CONFIG_PATH} must contain a YAML mapping")
    return data


def available_profiles() -> list[str]:
    """Selectable profile names: the base default plus any defined under `profiles`."""
    raw = _load_raw()
    profs = list(raw.get("profiles", {}).keys()) if isinstance(raw.get("profiles"), dict) else []
    return [_DEFAULT_PROFILE] + [p for p in profs if p != _DEFAULT_PROFILE]


def active_config_name() -> str:
    """Return the active profile name."""
    return _active_profile


def switch_config(profile: str) -> str:
    """Switch the active profile (e.g. 'qwen37'/'qwen36'); clears the merged-config cache."""
    global _active_profile
    _active_profile = profile
    load_config.cache_clear()
    return profile


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    """Base config with the active profile's overrides deep-merged into `llm`."""
    raw = _load_raw()
    override = (raw.get("profiles") or {}).get(_active_profile) if isinstance(raw.get("profiles"), dict) else None
    if isinstance(override, dict) and override:
        merged = copy.deepcopy(raw)
        base_llm = raw.get("llm") if isinstance(raw.get("llm"), dict) else {}
        merged["llm"] = _deep_merge(base_llm, override)
        return merged
    return raw


def resolve_llm_config(name: str) -> ChatProviderConfig:
    """Resolve LLM config by dotted name (e.g. 'supervisor.checker')."""

    llm_config = load_config().get("llm", {})
    if not isinstance(llm_config, dict):
        raise ValueError("gui_agent config 'llm' must be a mapping")

    section: dict[str, Any] = {}
    for candidate in (name, *_LLM_CONFIG_ALIASES.get(name, ())):
        found, section = _lookup_llm_section(llm_config, candidate)
        if found:
            break

    return resolve_chat_provider_config(
        provider=_optional_str(section.get("provider")),
        model=_optional_str(section.get("model")),
        api_key=_optional_str(section.get("api_key")),
        base_url=_optional_str(section.get("base_url")),
    )


def _lookup_llm_section(llm_config: dict[str, Any], name: str) -> tuple[bool, dict[str, Any]]:
    section: Any = llm_config
    for key in name.split("."):
        if not isinstance(section, dict):
            raise ValueError(f"gui_agent config llm.{name} must be a mapping")
        if key not in section:
            return False, {}
        section = section[key]
    if not isinstance(section, dict):
        raise ValueError(f"gui_agent config llm.{name} must be a mapping")
    return True, section


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


# ── Pricing (per-model token cost, for report cost estimation) ─────────────

def get_pricing() -> dict[str, Any]:
    p = load_config().get("pricing", {})
    return p if isinstance(p, dict) else {}


def pricing_currency() -> str:
    return str(get_pricing().get("currency") or "¥")


def model_price(model: str) -> tuple[float, float]:
    """(input, output) price per 1M tokens for a model; falls back to default → (1.0, 4.0)."""
    pricing = get_pricing()
    models = pricing.get("models")
    models = models if isinstance(models, dict) else {}
    entry = models.get(model) or pricing.get("default") or {"input": 1.0, "output": 4.0}
    if not isinstance(entry, dict):
        entry = {"input": 1.0, "output": 4.0}
    return float(entry.get("input", 0) or 0), float(entry.get("output", 0) or 0)
