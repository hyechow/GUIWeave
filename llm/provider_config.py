"""Shared provider and model-capability resolution for chat clients."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

SUPPORTED_CHAT_PROVIDERS = (
    "modelscope",
    "dashscope",
    "tokenplan",
    "standard",
    "nvidia",
    "openai",
    "local",
)

_PROVIDER_ENV_MAP = {
    "modelscope": {
        "model": "MODELSCOPE_MODEL",
        "api_key": "MODELSCOPE_API_KEY",
        "base_url": "MODELSCOPE_BASE_URL",
    },
    "dashscope": {
        "model": "DASHSCOPE_MODEL",
        "api_key": "DASHSCOPE_API_KEY",
        "base_url": "DASHSCOPE_BASE_URL",
    },
    # 阿里云 MaaS token-plan（OpenAI 兼容）；与 dashscope 公网分钥，读 TOKENPLAN_*。
    "tokenplan": {
        "model": "TOKENPLAN_MODEL",
        "api_key": "TOKENPLAN_API_KEY",
        "base_url": "TOKENPLAN_BASE_URL",
    },
    # Private OpenAI-compatible endpoint configured through STANDARD_*.
    "standard": {
        "model": "STANDARD_MODEL",
        "api_key": "STANDARD_API_KEY",
        "base_url": "STANDARD_BASE_URL",
    },
    "nvidia": {
        "model": "NVIDIA_MODEL",
        "api_key": "NVIDIA_API_KEY",
        "base_url": "NVIDIA_BASE_URL",
    },
    "openai": {
        "model": "OPENAI_MODEL",
        "api_key": "OPENAI_API_KEY",
        "base_url": "OPENAI_BASE_URL",
    },
    "local": {
        "model": "LOCAL_MODEL",
        "api_key": "LOCAL_API_KEY",
        "base_url": "LOCAL_BASE_URL",
    },
}

DEFAULT_MODEL_BY_PROVIDER = {
    "modelscope": "Qwen/Qwen2.5-72B-Instruct",
    "dashscope": "qwen-plus",
    "tokenplan": "qwen3.7-plus",
    "standard": "qwen3.7-plus",
    "nvidia": "minimaxai/minimax-m2.1",
    "openai": "gpt-4o-mini",
    "local": "Qwen/Qwen3-8B",
}

DEFAULT_BASE_URL_BY_PROVIDER = {
    "modelscope": "https://api-inference.modelscope.cn/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "tokenplan": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    "standard": "http://localhost:30000/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "openai": "https://api.openai.com/v1",
    "local": "http://localhost:30000/v1",
}

DEFAULT_API_KEY_BY_PROVIDER = {
    "modelscope": "dummy",
    "local": "dummy",
}


@dataclass(frozen=True)
class ChatProviderConfig:
    provider: str
    model: str
    api_key: Optional[str]
    base_url: str
    temperature: float | None = 0.0
    reasoning_effort: str | None = None
    enable_thinking: bool | None = None
    image_scale: float = 1.0
    max_actions_per_call: int = 5
    action_protocol: str = "tool_call"
    use_responses_api: bool = False
    # Bound stalled requests; slower endpoints can override these defaults.
    timeout_s: float = 60.0
    max_retries: int = 2


def enable_thinking_for_model(model: Optional[str]) -> bool:
    """Whether DashScope-compatible chat must send enable_thinking=True for this model.

    Agent calls default to thinking off for latency and cost. Add a model only when its
    endpoint demonstrably requires another value.
    """
    del model  # No known model currently forces thinking on; keep the hook signature.
    return False


def dashscope_extra_body(model: Optional[str] = None) -> dict:
    """extra_body for DashScope OpenAI-compatible chat, including enable_thinking."""
    return {"enable_thinking": enable_thinking_for_model(model)}


def chat_model_kwargs(config: ChatProviderConfig) -> dict[str, Any]:
    """Return constructor parameters supported by the configured model family."""

    kwargs: dict[str, Any] = {}
    if config.temperature is not None:
        kwargs["temperature"] = config.temperature
    if config.reasoning_effort:
        kwargs["reasoning_effort"] = config.reasoning_effort
    if config.enable_thinking is not None:
        kwargs["extra_body"] = {"enable_thinking": config.enable_thinking}
    if config.use_responses_api:
        kwargs["use_responses_api"] = True
    return kwargs


def build_chat_model(config: ChatProviderConfig, **overrides: Any) -> Any:
    """Build one configured ChatOpenAI client without duplicating provider policy."""

    from langchain_openai import ChatOpenAI

    kwargs = {
        "timeout": config.timeout_s, "max_retries": config.max_retries,
        **chat_model_kwargs(config), **overrides,
    }
    return ChatOpenAI(
        model=config.model, api_key=config.api_key, base_url=config.base_url, **kwargs,
    )


def chat_request_kwargs(model: Optional[str]) -> dict[str, Any]:
    """Return per-request compatibility fields for one model family.

    ``enable_thinking`` belongs to Qwen-compatible endpoints and must not leak
    into OpenAI GPT requests. An unknown model keeps the historical Qwen behavior
    so model-less test doubles and legacy callers remain backward compatible.
    """

    normalized = str(model or "").strip().casefold()
    if not normalized or normalized.startswith("qwen") or "/qwen" in normalized:
        return {"extra_body": dashscope_extra_body(model)}
    return {}


def resolve_chat_provider_config(
    provider: Optional[str] = None,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    default_models: Optional[Mapping[str, str]] = None,
    default_provider: str = "modelscope",
) -> ChatProviderConfig:
    """Resolve provider/model/api_key/base_url from overrides and environment."""
    resolved_provider = str(provider or os.getenv("API_PROVIDER", default_provider)).lower()
    if resolved_provider not in _PROVIDER_ENV_MAP:
        supported = ", ".join(SUPPORTED_CHAT_PROVIDERS)
        raise ValueError(f"Unsupported API_PROVIDER: {resolved_provider}. Expected one of: {supported}")

    env_keys = _PROVIDER_ENV_MAP[resolved_provider]
    models: Dict[str, str] = dict(DEFAULT_MODEL_BY_PROVIDER)
    if default_models:
        models.update(default_models)

    resolved_model = model or os.getenv(env_keys["model"], models[resolved_provider])
    resolved_api_key = api_key or os.getenv(env_keys["api_key"], DEFAULT_API_KEY_BY_PROVIDER.get(resolved_provider))
    resolved_base_url = base_url or os.getenv(env_keys["base_url"], DEFAULT_BASE_URL_BY_PROVIDER[resolved_provider])

    return ChatProviderConfig(
        provider=resolved_provider,
        model=resolved_model,
        api_key=resolved_api_key,
        base_url=resolved_base_url,
        timeout_s=float(os.getenv("CHAT_TIMEOUT_S", "60")),
        max_retries=int(os.getenv("CHAT_MAX_RETRIES", "2")),
    )
