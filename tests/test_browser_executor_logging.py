from __future__ import annotations

from types import SimpleNamespace

from gui_agent.adapters.browser.executor import BrowserExecutor


def test_sensitive_typed_value_is_not_printed(capsys) -> None:
    secret = "runtime-secret-73"
    client = SimpleNamespace(select_all=lambda: "OK select all")
    executor = BrowserExecutor(SimpleNamespace(client=client))
    executor.sensitive_text_values = (secret,)

    assert executor._clear_before_type(client, secret)

    output = capsys.readouterr().out
    assert secret not in output
    assert "session access value redacted" in output
