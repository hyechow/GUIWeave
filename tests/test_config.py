from __future__ import annotations

from pathlib import Path

import pytest

from gui_agent.core import config


def test_data_model_config_is_not_nested_under_supervisor():
    llm = config.load_config()["llm"]

    assert llm["observation"]["model"]
    assert "observation" not in llm["supervisor"]
    assert llm["orchestrator"]["model"]


def test_agent_config_defaults_to_config_yaml(monkeypatch):
    monkeypatch.delenv("AGENT_CONFIG", raising=False)

    assert config.active_config_path() == config.CONFIG_PATH


def test_default_config_has_dashscope_baseline(monkeypatch):
    monkeypatch.delenv("AGENT_CONFIG", raising=False)
    config._load_raw.cache_clear()
    try:
        llm = config._load_raw()["llm"]

        assert llm["action_policy"] == {
            "provider": "dashscope",
            "model": "qwen3.5-35b-a3b",
        }
        assert llm["output"] == {
            "provider": "dashscope",
            "model": "qwen3.5-flash",
        }
        assert llm["target_verify"]["model"] == "qwen3.5-flash"
    finally:
        config._load_raw.cache_clear()


def test_agent_config_selects_sibling_yaml_by_filename(monkeypatch):
    monkeypatch.setenv("AGENT_CONFIG", "config.standard.yaml")
    config._load_raw.cache_clear()
    config.load_config.cache_clear()
    try:
        llm = config.load_config()["llm"]

        assert config.active_config_path() == config.CONFIG_PATH.with_name("config.standard.yaml")
        assert llm["action_policy"] == {
            "provider": "standard",
            "model": "qwen3.7-plus",
        }
        assert llm["output"] == {
            "provider": "standard",
            "model": "qwen3.6-flash",
        }
        worker = config.resolve_llm_config("tool_agent.worker")
        assert worker.model == "gpt-5.6-luna"
        assert worker.reasoning_effort == "low"
        assert worker.temperature is None
        assert worker.image_scale == 0.75
        assert worker.action_protocol == "json"
        assert worker.use_responses_api is True
        assert worker.timeout_s == 20
        assert worker.max_retries == 1
        perception = config.resolve_llm_config("tool_agent.perception")
        assert perception.model == "gpt-5.6-luna"
        assert perception.reasoning_effort == "low"
        assert perception.image_scale == 1.0
        assert perception.use_responses_api is True
    finally:
        config._load_raw.cache_clear()
        config.load_config.cache_clear()


def test_agent_config_missing_explicit_file_fails_clearly(monkeypatch, tmp_path: Path):
    missing = tmp_path / "missing.yaml"
    monkeypatch.setenv("AGENT_CONFIG", str(missing))
    config._load_raw.cache_clear()
    try:
        with pytest.raises(FileNotFoundError, match="AGENT_CONFIG file does not exist"):
            config._load_raw()
    finally:
        config._load_raw.cache_clear()


def test_recon_navigator_config_falls_back_to_legacy_back_nav(monkeypatch):
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {"llm": {"back_nav": {"provider": "local", "model": "legacy-nav"}}},
    )

    assert config.resolve_llm_config("recon.navigator").model == "legacy-nav"


def test_legacy_back_nav_config_falls_forward_to_recon_navigator(monkeypatch):
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {"llm": {"recon": {"navigator": {"provider": "local", "model": "neutral-nav"}}}},
    )

    assert config.resolve_llm_config("back_nav").model == "neutral-nav"


def test_tokenplan_config_uses_luna_visual_slots_and_qwen_elsewhere(monkeypatch):
    # 可选配置基线 = qwen3.7-plus + tokenplan provider；qwen3.7-max 是纯文本所以不用。
    # 外围 flash 用 qwen3.6-flash（token-plan 无 qwen3.5-flash）。
    monkeypatch.setenv("AGENT_CONFIG", "config.tokenplan.yaml")
    monkeypatch.setenv("TOKENPLAN_API_KEY", "sk-test-tokenplan")
    monkeypatch.setenv("STANDARD_API_KEY", "sk-test-luna")
    monkeypatch.setenv("STANDARD_BASE_URL", "http://standard.example/v1")
    monkeypatch.delenv("TOKENPLAN_BASE_URL", raising=False)
    config.load_config.cache_clear()
    config._load_raw.cache_clear()
    try:
        llm = config.load_config()["llm"]
        token_plan = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"

        assert llm["action_policy"]["model"] == "qwen3.7-plus"
        assert llm["action_policy"]["provider"] == "tokenplan"
        assert llm["recon"]["navigator"]["model"] == "qwen3.7-plus"
        assert llm["supervisor"]["model"] == "qwen3.7-plus"
        assert llm["orchestrator"]["model"] == "qwen3.7-plus"
        # 外围 flash 槽位
        assert llm["output"]["model"] == "qwen3.6-flash"
        assert llm["router"]["model"] == "qwen3.6-flash"
        assert llm["fingerprint"]["model"] == "qwen3.6-flash"
        assert llm["loading"]["model"] == "qwen3.6-flash"
        assert llm["target_verify"]["model"] == "qwen3.6-flash"
        assert llm["supervisor"]["intent"]["model"] == "qwen3.7-plus"
        assert llm["observation"]["model"] == "qwen3.7-plus"
        # resolve 走 TOKENPLAN_* env
        cfg = config.resolve_llm_config("action_policy")
        assert cfg.provider == "tokenplan"
        assert cfg.model == "qwen3.7-plus"
        assert cfg.base_url == token_plan
        assert cfg.api_key == "sk-test-tokenplan"
        worker = config.resolve_llm_config("tool_agent.worker")
        assert worker.provider == "standard"
        assert worker.model == "gpt-5.6-luna"
        assert worker.api_key == "sk-test-luna"
        assert worker.base_url == "http://standard.example/v1"
        assert worker.action_protocol == "json"
        perception = config.resolve_llm_config("tool_agent.perception")
        assert perception.provider == "standard"
        assert perception.model == "gpt-5.6-luna"
        assert perception.api_key == "sk-test-luna"
        assert perception.image_scale == 1.0
        assert perception.use_responses_api is True
        # 单价随模型登记（plus ≤256K 档官网价）
        assert config.model_price("qwen3.7-plus") == (2.0, 8.0)
        assert config.model_price("qwen3.7-max") == (12.0, 36.0)
        assert config.model_price("qwen3.6-flash") == (0.5, 4.0)
    finally:
        config.load_config.cache_clear()
        config._load_raw.cache_clear()


def test_resolve_llm_config_passes_section_base_url_and_api_key(monkeypatch):
    # yaml 槽位可直接写 base_url/api_key，不只靠 env。
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {
            "llm": {
                "action_policy": {
                    "provider": "tokenplan",
                    "model": "qwen3.7-plus",
                    "base_url": "https://token-plan.example/v1",
                    "api_key": "sk-test-from-yaml",
                }
            }
        },
    )

    cfg = config.resolve_llm_config("action_policy")
    assert cfg.provider == "tokenplan"
    assert cfg.model == "qwen3.7-plus"
    assert cfg.base_url == "https://token-plan.example/v1"
    assert cfg.api_key == "sk-test-from-yaml"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("image_scale", 0, "image_scale"),
        ("action_protocol", "xml", "action_protocol"),
    ],
)
def test_resolve_llm_config_rejects_invalid_model_options(
    monkeypatch, field, value, message,
) -> None:
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {"llm": {"tool_agent": {"worker": {field: value}}}},
    )

    with pytest.raises(ValueError, match=message):
        config.resolve_llm_config("tool_agent.worker")


def test_resolve_llm_config_rejects_runtime_action_limit(monkeypatch) -> None:
    monkeypatch.setattr(
        config,
        "load_config",
        lambda: {
            "llm": {
                "tool_agent": {
                    "worker": {"max_actions_per_call": 1},
                },
            },
        },
    )

    with pytest.raises(ValueError, match="Runtime execution policy"):
        config.resolve_llm_config("tool_agent.worker")
