"""Bad case — WebArena shopping_admin task 185, live run 20260630_143407.

Goal: "Give me the material of the products that have 3 units left" → expected [cotton, fleece].
The orchestration (foreach → resolve_parent_material drill → read Material) ran end to end, but the
final answer was [burlap, fleece] (score 0): WS08's primary material read as **Burlap** not **Cotton**.

Root cause (NOT state pollution — the agent never clicked the Material control, only scrolled):
the Material field is a native `<select multiple>` rendered as a multi-row listbox (selected options
highlighted). Both products' reads fell to VISION, which is inconsistent:
  - WH11 selected = "Fleece, Polyester, Spandex" → vision happened to pick the first SELECTED → Fleece ✓
  - WS08 selected = "Cotton"                      → vision picked the first LISTED option   → Burlap ✗
                                                     (Burlap is alphabetically first in the option list)

Fix direction: the native-select DOM value is authoritative and must win — the primary material is
the FIRST SELECTED option (from selectedOptions / selected_text), never the first listed option, and
never a vision guess. These tests pin that invariant on the DOM read path (`_read_from_form_controls`).
A present-but-unselected multiselect must read "" so the program can branch/fail honestly, NOT guess
the first option.
"""

from gui_agent.adapters.browser.page_read import _read_from_form_controls
from gui_agent.adapters.browser import page_read as page_read_mod


def _material_control(selected_text: str) -> dict:
    # Faithful to form_reader's native_select shape: `selected_text` joins ALL selectedOptions,
    # `options` lists every option (Burlap is alphabetically first → the value vision wrongly grabbed).
    return {
        "kind": "native_select",
        "label": "Material",
        "value": "33",
        "selected_text": selected_text,
        "options": ["Burlap", "Canvas", "Cotton", "Fleece", "Linen", "Polyester", "Spandex", "Wool"],
        "rect": {"x": 500, "y": 1800, "w": 200, "h": 80},
    }


def test_ws08_reads_selected_cotton_not_first_option_burlap():
    # WS08: only Cotton selected. The read must be Cotton, NEVER Burlap (the first listed option).
    out = _read_from_form_controls([_material_control("Cotton")], ["material"])
    assert out == {"material": "Cotton"}
    assert out["material"] != "Burlap"


def test_wh11_primary_material_is_first_selected_option():
    # WH11: three materials selected. The PRIMARY material webarena expects is the first selected
    # (Fleece), not the whole joined string "Fleece, Polyester, Spandex".
    out = _read_from_form_controls([_material_control("Fleece, Polyester, Spandex")], ["material"])
    assert out == {"material": "Fleece"}


def test_unselected_multiselect_reads_empty_not_first_option():
    # Nothing selected → "". Must NOT fall back to the first listed option (Burlap) — that is
    # exactly the WS08 vision misread.
    out = _read_from_form_controls([_material_control("")], ["material"])
    assert out == {"material": ""}
    assert out["material"] != "Burlap"


def test_form_read_uses_read_spec_label_for_semantic_field():
    controls = [
        {"kind": "text", "label": "Nickname", "value": "Roxie"},
        {"kind": "textarea", "label": "Summary of Review", "value": "Doesn't fit me. Luma fail."},
    ]
    out = _read_from_form_controls(
        controls,
        ["title"],
        read_spec="title：读取 Summary of Review 字段文字，不要读取 Nickname。",
    )
    assert out == {"title": "Doesn't fit me. Luma fail."}


def test_page_read_keeps_partial_dom_reads_for_visual_fallback(monkeypatch):
    class Obs:
        form_controls = [
            {"kind": "textarea", "label": "Summary of Review", "value": "Doesn't fit me. Luma fail."},
        ]
        semantic_tree = None
        png_bytes = b"x"

    calls: list[list[str]] = []

    def fake_structured_read(png_bytes, returns, **kwargs):
        calls.append(list(returns))
        return {"rating": "2"}

    import gui_agent.core.orchestrator.primitives.structured_read as structured_read_mod

    monkeypatch.setattr(structured_read_mod, "structured_read", fake_structured_read)
    out = page_read_mod.read_page_complete(
        Obs(),
        ["title", "rating"],
        read_spec=(
            "title：读取 Summary of Review 字段文字；"
            "rating：读取 Detailed Rating 区域的星级数值。"
        ),
    )

    assert out == {"rating": "2", "title": "Doesn't fit me. Luma fail."}
    assert calls == [["rating"]]


# ── core hand-off read: DOM authoritative over vision ──────────────────────────────
from gui_agent.core.orchestrator.primitives.structured_read import read_form_control_returns


def test_handoff_read_takes_dom_selected_over_vision():
    # The orchestrator hand-off return-read was vision-only (bypassing the DOM), so it returned the
    # first listed option (Burlap). With DOM-first, the native <select>'s selected option wins:
    # WS08 → Cotton, never Burlap. This is the actual fix for the live 185 failure.
    fc = [_material_control("Cotton")]
    assert read_form_control_returns(fc, ["material"]) == {"material": "Cotton"}


def test_handoff_read_returns_primary_of_multiselect():
    fc = [_material_control("Fleece, Polyester, Spandex")]
    assert read_form_control_returns(fc, ["material"]) == {"material": "Fleece"}


def test_handoff_read_returns_empty_native_select_without_vision_fallback():
    # A matched-but-unselected native select is an authoritative empty value. It must not be
    # omitted, because omission tells the caller to vision-read the option list; that is how the
    # live bad case picked Burlap, the first listed option.
    assert read_form_control_returns([_material_control("")], ["material"]) == {"material": ""}
    assert read_form_control_returns(None, ["material"]) == {}
    assert read_form_control_returns([{"kind": "native_select", "label": "Status",
                                       "selected_text": "Complete"}], ["material"]) == {}


def test_handoff_read_uses_read_spec_label_for_semantic_field():
    controls = [
        {"kind": "text", "label": "Nickname", "value": "Roxie"},
        {"kind": "textarea", "label": "Summary of Review", "value": "Doesn't fit me. Luma fail."},
    ]
    assert read_form_control_returns(
        controls,
        ["title"],
        read_spec="title：读取 Summary of Review 字段文字，不要读取 Nickname。",
    ) == {"title": "Doesn't fit me. Luma fail."}
