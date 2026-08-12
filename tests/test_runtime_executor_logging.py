from __future__ import annotations

from types import SimpleNamespace

from gui_agent.adapters.android.actions import AndroidAction, AndroidActionDecision
from gui_agent.adapters.android.executor import AndroidExecutor


def test_android_sensitive_typed_value_is_not_printed(capsys) -> None:
    secret = "runtime-secret-73"
    client = SimpleNamespace(
        viewport_size=(1080, 2400),
        tap=lambda _x, _y: "OK tap",
        clear_text=lambda: "OK clear",
        type_text=lambda text: f"OK type {text!r}",
    )
    executor = AndroidExecutor(SimpleNamespace(client=client))
    executor.sensitive_text_values = (secret,)
    decision = AndroidActionDecision(action=AndroidAction(
        action_type="type",
        x=500,
        y=500,
        text=secret,
        description="Enter the private session value",
    ))

    assert executor.execute(decision) is True

    output = capsys.readouterr().out
    assert secret not in output
    assert "session access value redacted" in output
