from __future__ import annotations

from gui_agent.core import config


def test_data_model_config_is_not_nested_under_supervisor():
    llm = config.load_config()["llm"]

    assert llm["observation"]["model"]
    assert "observation" not in llm["supervisor"]
    assert llm["orchestrator"]["model"]


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


def test_default_config_is_qwen37_token_plan(monkeypatch):
    # 默认基线 = qwen3.7-plus + tokenplan provider；qwen3.7-max 是纯文本所以不用。
    # 外围 flash 用 qwen3.6-flash（token-plan 无 qwen3.5-flash）。
    monkeypatch.setenv("TOKENPLAN_API_KEY", "sk-test-tokenplan")
    monkeypatch.delenv("TOKENPLAN_BASE_URL", raising=False)
    config.load_config.cache_clear()
    config._load_raw.cache_clear()
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
    assert llm["supervisor"]["intent"]["model"] == "qwen3.7-plus"
    assert llm["observation"]["model"] == "qwen3.7-plus"
    # resolve 走 TOKENPLAN_* env
    cfg = config.resolve_llm_config("action_policy")
    assert cfg.provider == "tokenplan"
    assert cfg.model == "qwen3.7-plus"
    assert cfg.base_url == token_plan
    assert cfg.api_key == "sk-test-tokenplan"
    # 单价随模型登记（plus ≤256K 档官网价）
    assert config.model_price("qwen3.7-plus") == (2.0, 8.0)
    assert config.model_price("qwen3.7-max") == (12.0, 36.0)
    assert config.model_price("qwen3.6-flash") == (0.5, 4.0)


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
