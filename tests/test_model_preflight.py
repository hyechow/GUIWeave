from __future__ import annotations

from pathlib import Path

from gui_agent.core.config import preflight
from llm.provider_config import ChatProviderConfig


def _provider(
    *,
    api_key: str | None = "test-secret",
    base_url: str = "https://user:pass@gateway.test/v1?token=private",
) -> ChatProviderConfig:
    return ChatProviderConfig(
        provider="standard",
        model="vision-model",
        api_key=api_key,
        base_url=base_url,
    )


def _stub_config(monkeypatch, tmp_path: Path, provider: ChatProviderConfig) -> None:
    config_path = tmp_path / "config.standard.yaml"
    config_path.write_text("llm: {}\n", encoding="utf-8")
    monkeypatch.setattr(preflight, "active_config_path", lambda: config_path)
    monkeypatch.setattr(preflight, "load_config", lambda: {"llm": {}})
    monkeypatch.setattr(preflight, "resolve_llm_config", lambda _slot: provider)


def test_model_preflight_reports_ready_without_exposing_secrets(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _stub_config(monkeypatch, tmp_path, _provider())

    result = preflight.check_model_environment()
    payload = result.to_dict()

    assert result.ok
    assert len(payload["slots"]) == len(preflight.TASK_MODEL_SLOTS)
    assert all(slot["api_key_configured"] for slot in payload["slots"])
    serialized = repr(payload)
    assert "test-secret" not in serialized
    assert "user:pass" not in serialized
    assert "token=private" not in serialized
    assert "https://gateway.test/v1" in serialized


def test_model_preflight_blocks_missing_or_placeholder_key(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _stub_config(monkeypatch, tmp_path, _provider(api_key="replace-me"))

    result = preflight.check_model_environment()

    assert not result.ok
    assert result.summary == "模型配置未就绪"
    assert any("API_KEY" in line for line in result.lines)
    assert not any(slot.api_key_configured for slot in result.slots)


def test_model_preflight_rejects_invalid_endpoint_without_echoing_it(
    monkeypatch,
    tmp_path: Path,
) -> None:
    private_value = "https://user:secret@gateway.test:bad/v1?api_key=secret"
    _stub_config(monkeypatch, tmp_path, _provider(base_url=private_value))

    result = preflight.check_model_environment()

    assert not result.ok
    assert any("BASE_URL" in line for line in result.lines)
    assert all(slot.endpoint == "<invalid endpoint>" for slot in result.slots)
    assert private_value not in repr(result.to_dict())


def test_local_provider_does_not_require_api_key(monkeypatch, tmp_path: Path) -> None:
    provider = ChatProviderConfig(
        provider="local",
        model="local-vision-model",
        api_key=None,
        base_url="http://127.0.0.1:30000/v1",
    )
    _stub_config(monkeypatch, tmp_path, provider)

    assert preflight.check_model_environment().ok
