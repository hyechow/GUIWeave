from __future__ import annotations

from gui_agent.core import config


def test_data_model_config_is_not_nested_under_supervisor():
    llm = config.load_config()["llm"]

    assert llm["data"]["model"]
    assert "data" not in llm["supervisor"]
    assert llm["supervisor"]["feasibility"]["model"]


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
