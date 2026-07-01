"""Off-viewport form controls must stay in the list (flagged), not be dropped.

Long forms (WebArena Cart Price Rule) put Rule Name at the top and Discount/Save at the bottom.
The old form-control snapshot dropped any control scrolled out of the viewport, so the feasibility
judge concluded "control missing" and aborted (702 Rule Name / 703 discount_amount). Now every
rendered control is reported with an in_viewport flag; only the off-viewport exceptions carry it."""
from __future__ import annotations

from gui_agent.adapters.browser.form_reader import normalize_form_controls
from gui_agent.context.runtime import format_form_controls_text


def test_off_viewport_control_kept_and_flagged():
    raw = {"controls": [
        {"kind": "text_input", "name": "name", "label": "Rule Name",
         "rect": {"x": 530, "y": -1461, "w": 100, "h": 30}, "in_viewport": False},
        {"kind": "text_input", "name": "discount_amount", "label": "Discount Amount",
         "rect": {"x": 500, "y": 500, "w": 100, "h": 30}, "in_viewport": True},
    ]}
    out = {c["name"]: c for c in normalize_form_controls(raw)}
    # the off-viewport control survives and is flagged
    assert "name" in out and out["name"].get("in_viewport") is False
    # the in-viewport control is present but not flagged (in-viewport is the default)
    assert "discount_amount" in out and "in_viewport" not in out["discount_amount"]


def test_context_block_annotates_off_viewport_control():
    controls = normalize_form_controls({"controls": [
        {"kind": "text_input", "name": "name", "label": "Rule Name",
         "rect": {"x": 530, "y": -1461, "w": 100, "h": 30}, "in_viewport": False},
    ]})
    text = format_form_controls_text(controls)
    assert "Rule Name" in text
    assert "需先滚动到视口" in text  # planner is told to scroll to it, not treat it as absent
