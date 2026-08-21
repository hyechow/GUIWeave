import pytest

from gui_agent.adapters.browser.runtime_profile import resolve_browser_profile


def test_browser_runtime_profile_defaults_to_evaluation(monkeypatch) -> None:
    monkeypatch.delenv("BROWSER_RUNTIME_PROFILE", raising=False)
    assert resolve_browser_profile() == "evaluation"


def test_browser_runtime_profile_accepts_production_alias(monkeypatch) -> None:
    monkeypatch.setenv("BROWSER_RUNTIME_PROFILE", "production_browser")
    assert resolve_browser_profile() == "production"


def test_browser_runtime_profile_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="unsupported browser runtime profile"):
        resolve_browser_profile("stealth")
