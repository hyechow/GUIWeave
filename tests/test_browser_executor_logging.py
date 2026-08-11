from __future__ import annotations

from types import SimpleNamespace

from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
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


def test_same_origin_relative_navigation_uses_current_browser_origin() -> None:
    navigated: list[str] = []
    client = SimpleNamespace(
        page_info=lambda: (
            "http://192.168.1.103:7780/admin/admin/dashboard/",
            "Dashboard",
        ),
        navigate=lambda url: navigated.append(url) or f"OK navigate {url}",
    )
    executor = BrowserExecutor(SimpleNamespace(client=client))
    decision = BrowserActionDecision(action=BrowserAction(
        action_type="navigate",
        url="/admin/review/product/index/",
        description="Open the known All Reviews route",
    ))

    assert executor.execute(decision) is True
    assert navigated == [
        "http://192.168.1.103:7780/admin/review/product/index/"
    ]


def test_left_panel_scroll_resolves_anchor_inside_left_region() -> None:
    calls: list[tuple[float, float, str]] = []

    def resolve(x: float, y: float, area: str):
        calls.append((x, y, area))
        return x, y, "left-panel"

    client = SimpleNamespace(
        viewport_size=(1280, 960),
        safe_scroll_anchor=resolve,
    )
    executor = BrowserExecutor(SimpleNamespace(client=client))
    action = BrowserAction(
        action_type="scroll",
        direction="down",
        target_area="left_panel",
        description="Scroll the expanded navigation panel",
    )

    executor._prepare_scroll_anchor(action)

    assert calls == [(320.0, 480.0, "left_panel")]
    assert (action.x, action.y) == (250.0, 500.0)
