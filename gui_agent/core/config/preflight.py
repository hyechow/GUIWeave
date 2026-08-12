"""Validate model configuration before GUIWeave opens a local GUI session."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from gui_agent.core.config import active_config_path, load_config, resolve_llm_config


TASK_MODEL_SLOTS: tuple[tuple[str, bool], ...] = (
    ("tool_agent.master", False),
    ("tool_agent.worker", True),
    ("tool_agent.perception", True),
    ("tool_agent.presentation", False),
    ("loading", True),
    ("target_verify", True),
)
_PLACEHOLDER_KEYS = {
    "dummy",
    "changeme",
    "replace-me",
    "your-api-key",
    "your_api_key",
}


@dataclass(frozen=True)
class ModelSlotStatus:
    """Safe, non-secret projection of one resolved model slot."""

    slot: str
    provider: str
    model: str
    endpoint: str
    api_key_configured: bool
    vision_required: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ModelPreflightResult:
    """Model configuration readiness without exposing credential values."""

    ok: bool
    summary: str
    lines: tuple[str, ...]
    config_path: str
    slots: tuple[ModelSlotStatus, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "details": list(self.lines),
            "config_path": self.config_path,
            "slots": [slot.to_dict() for slot in self.slots],
        }


def _safe_endpoint(value: str) -> str:
    """Return a diagnostic endpoint without userinfo, query, or fragment."""

    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        parsed_port = parsed.port
    except ValueError:
        return "<invalid endpoint>"
    if not host:
        return "<invalid endpoint>"
    port = f":{parsed_port}" if parsed_port is not None else ""
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{host}{port}{path}"


def _valid_endpoint(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _has_real_key(provider: str, value: str | None) -> bool:
    if provider == "local":
        return True
    normalized = str(value or "").strip().casefold()
    return bool(normalized) and normalized not in _PLACEHOLDER_KEYS


def check_model_environment() -> ModelPreflightResult:
    """Resolve every task-time model slot and reject incomplete configuration."""

    config_path = active_config_path().resolve()
    try:
        load_config()
    except Exception as exc:  # noqa: BLE001 - configuration errors are user-facing
        return ModelPreflightResult(
            ok=False,
            summary="模型配置文件不可用",
            lines=(f"  ✗ {type(exc).__name__}: {exc}",),
            config_path=str(config_path),
        )

    statuses: list[ModelSlotStatus] = []
    errors: list[str] = []
    for slot, vision_required in TASK_MODEL_SLOTS:
        try:
            resolved = resolve_llm_config(slot)
        except Exception as exc:  # noqa: BLE001 - report every broken slot together
            errors.append(f"  ✗ {slot}: {type(exc).__name__}: {exc}")
            continue
        key_ready = _has_real_key(resolved.provider, resolved.api_key)
        endpoint_ready = _valid_endpoint(resolved.base_url)
        statuses.append(ModelSlotStatus(
            slot=slot,
            provider=resolved.provider,
            model=resolved.model,
            endpoint=_safe_endpoint(resolved.base_url),
            api_key_configured=key_ready,
            vision_required=vision_required,
        ))
        if not resolved.model.strip():
            errors.append(f"  ✗ {slot}: model 未配置")
        if not endpoint_ready:
            errors.append(
                f"  ✗ {slot}: {resolved.provider.upper()}_BASE_URL "
                "必须是有效的 http(s) 地址"
            )
        if not key_ready:
            errors.append(
                f"  ✗ {slot}: {resolved.provider.upper()}_API_KEY "
                "未配置或仍是占位值"
            )

    if errors:
        guidance = (
            "  → 在仓库根目录 .env 中配置 AGENT_CONFIG、对应 BASE_URL 和 API_KEY，"
            "然后重启 Run Console/Codex。"
        )
        return ModelPreflightResult(
            ok=False,
            summary="模型配置未就绪",
            lines=(*errors, guidance),
            config_path=str(config_path),
            slots=tuple(statuses),
        )

    providers = ", ".join(sorted({slot.provider for slot in statuses}))
    models = ", ".join(sorted({slot.model for slot in statuses}))
    return ModelPreflightResult(
        ok=True,
        summary="模型配置已就绪",
        lines=(
            f"  ✓ 配置文件: {config_path}",
            f"  ✓ Provider: {providers}",
            f"  ✓ Models: {models}",
            "  ! Worker、Perception 与视觉校验模型必须由网关支持图片输入。",
            "  ! 此检查验证本地配置；网关鉴权与模型可用性由首次请求确认。",
        ),
        config_path=str(config_path),
        slots=tuple(statuses),
    )


__all__ = [
    "ModelPreflightResult",
    "ModelSlotStatus",
    "TASK_MODEL_SLOTS",
    "check_model_environment",
]
