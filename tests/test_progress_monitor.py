"""Deterministic tests for ProgressMonitor — the consolidated task-execution-health memory
(absorbs the former state_trace.py). Covers the canonical-URL state, instruction-keyed repeat
detection (legacy Action-Loop Guard), the rendered trace fed to the checker, and the new
action-signature key.

The signature fixtures are the real executed actions from WebArena run 20260622_171843, where the
agent re-typed 'Olivia zip jacket' into the same Product filter box at T3/T9/T12/T13 (and re-clicked
Reset/Search) while the instruction wording kept changing — so the instruction-keyed guard missed
it and Feasibility didn't fire until T21. Keying on the action SIGNATURE catches it."""

from types import SimpleNamespace

from gui_agent.core.run.action_signals import action_signature
from gui_agent.core.run.progress_monitor import (
    ProgressMonitor,
    canonical_url,
    state_trace_block,
)


# ── canonical state ────────────────────────────────────────────────────────
def test_canonical_url_strips_host_and_volatile_filter_segments():
    base = "http://h:7780/admin/review/product/index"
    filtered = "http://h:7780/admin/review/product/index/filter/AbC123==/internal_reviews//form_key/xy/"
    # the oscillating filtered / unfiltered URLs collapse to ONE canonical page state
    assert canonical_url(base) == "/admin/review/product/index"
    assert canonical_url(filtered) == "/admin/review/product/index/internal_reviews"
    assert canonical_url(base) == canonical_url(base + "/sort/name/dir/asc")
    assert canonical_url("") == "" and canonical_url(None) == ""


# ── instruction-keyed repeat detection (legacy Action-Loop Guard) ───────────
def test_repeated_detects_same_state_action():
    tr = ProgressMonitor()
    s = "/admin/review/product/index"
    tr.note(3, s, "点击 Search 按钮执行搜索")
    tr.note(4, s, "点击 Reset Filter 按钮")
    assert tr.repeated(s, "在 Product 框输入关键词") is None
    hit = tr.repeated(s, "点击 Search 按钮执行搜索")
    assert hit is not None and hit.index == 3


def test_repeated_is_phrasing_tolerant_and_state_sensitive():
    tr = ProgressMonitor()
    tr.note(1, "/a", "点击 Search 按钮，执行搜索！")
    assert tr.repeated("/a", "点击Search按钮执行搜索") is not None
    assert tr.repeated("/b", "点击Search按钮执行搜索") is None


def test_repeated_is_interaction_state_sensitive_for_browser_forms():
    tr = ProgressMonitor()
    state = "/admin/catalog/product"
    tr.note(4, state, "按回车键提交", "Search by keyword=Olivia zip jacket")
    assert tr.repeated(state, "按回车键提交", "Search by keyword=Olivia") is None
    hit = tr.repeated(state, "按回车键提交", "Search by keyword=Olivia zip jacket")
    assert hit is not None and hit.index == 4


def test_distinct_states_counts_frontier():
    tr = ProgressMonitor()
    for i, s in enumerate(["/a", "/a", "/b", "/a"]):
        tr.note(i, s, f"act{i}")
    assert tr.distinct_states() == 2  # churned across only 2 pages


def test_render_marks_repeats_and_block():
    tr = ProgressMonitor()
    s = "/admin/review/product/index"
    tr.note(3, s, "搜索 Olivia")
    tr.note(4, s, "Reset Filter")
    tr.note(7, s, "搜索 Olivia")   # repeat of T3
    text = tr.render()
    assert "T3" in text and "T7" in text
    assert "⚠️重复(同 T3)" in text

    blk = state_trace_block(tr)
    assert blk is not None and blk.id == "runtime.state_trace"
    assert "任务进展轨迹" in blk.content and "⚠️重复" in blk.content
    assert state_trace_block(ProgressMonitor()) is None  # empty → no block


def test_regression_webarena_113_reset_search_loop_20260622_105707():
    """Regression: WebArena task-113 live run 20260622_105707 — a Reset→search→Reset loop the
    frame-level guards missed (every turn changed url/DOM → looked like progress, ran 11+ turns).
    The guard must collapse the oscillating URLs into ONE canonical state and flag the repeated
    Reset (T8≡T4) and Search (T10≡T7). Real (url, instruction) trace from that run."""
    seq = [
        (3, "http://h:7780/admin/review/product/index/filter/Y3Jl_name_Olivia/internal_reviews//form_key/vjgQ/",
         "点击 Search 按钮执行搜索"),
        (4, "http://h:7780/admin/review/product/index/filter//internal_reviews//form_key/vjgQ/",
         "点击 Reset Filter 按钮清除残留的过滤条件"),
        (5, "http://h:7780/admin/review/product/index/filter//internal_reviews//form_key/vjgQ/",
         "在 Review 搜索框的 detail 输入框中输入 Olivia zip jacket"),
        (7, "http://h:7780/admin/review/product/index/filter/Y3Jl_detail_Olivia/internal_reviews//form_key/vjgQ/",
         "点击 Search 按钮执行搜索筛选"),
        (8, "http://h:7780/admin/review/product/index/filter//internal_reviews//form_key/vjgQ/",
         "点击 Reset Filter 按钮清除残留的过滤条件"),
        (10, "http://h:7780/admin/review/product/index/filter/Y3Jl_detail_Olivia/internal_reviews//form_key/vjgQ/",
         "点击 Search 按钮执行搜索筛选"),
    ]
    assert len({canonical_url(u) for _, u, _ in seq}) == 1
    tr = ProgressMonitor()
    intercepted = []
    for idx, url, instr in seq:
        state = canonical_url(url)
        if tr.repeated(state, instr) is not None:
            intercepted.append(idx)
        else:
            tr.note(idx, state, instr)
    assert intercepted == [8, 10]
    assert tr.repeated(canonical_url(seq[0][1]), seq[0][2]) is not None


def test_run_checker_injects_state_trace_block_for_progress_judgment(monkeypatch):
    """The guard also FEEDS the checker: run_checker must surface the state→decision trace when
    given state_trace_text, and omit the block when empty. Pins the wiring against silent drops."""
    import gui_agent.core.supervisor.milestone.model_io as model_io
    from gui_agent.core.schemas import StatementContract, Observation
    from gui_agent.core.supervisor.milestone.schemas import _SingleCheckResult

    captured: dict = {}

    def _flat(messages) -> str:
        out = []
        for m in messages:
            c = getattr(m, "content", m)
            if isinstance(c, str):
                out.append(c)
            elif isinstance(c, list):
                out.extend(str(p.get("text", p)) if isinstance(p, dict) else str(p) for p in c)
        return "\n".join(out)

    def _fake_invoke(llm, messages, schema, **_kw):
        captured["text"] = _flat(messages)
        return _SingleCheckResult(status="in_progress", effect_status="unverified", reason="检查中尚未达成", summary="进行中")

    monkeypatch.setattr(model_io, "invoke_structured", _fake_invoke)
    monkeypatch.setattr(model_io, "_make_llm", lambda: object())

    from io import BytesIO
    from PIL import Image
    _buf = BytesIO()
    Image.new("RGB", (8, 8), "white").save(_buf, "PNG")  # valid, survives the checker's retina halving
    _PNG = _buf.getvalue()
    ms = StatementContract.model_validate({
        "id": "m1", "name": "筛选评论", "description": "d", "success_condition": "列表已刷新", "kind": "filter",
    })
    obs = Observation(png_bytes=_PNG, source="browser")
    trace = " T3 状态:/admin/review/product/index | 决策:点击search ⚠️重复(同 T1)"

    model_io.run_checker(ms, obs, [], state_trace_text=trace)
    assert "任务进展轨迹" in captured["text"]
    assert "⚠️重复" in captured["text"] and "点击search" in captured["text"]

    model_io.run_checker(ms, obs, [], state_trace_text="")   # empty → no trace block
    assert "任务进展轨迹" not in captured["text"]


# ── action-signature key (reword/jitter-proof) — Kind-2 evidence ────────────
def _act(action_type, snapped, info, text=""):
    return SimpleNamespace(
        action_type=action_type, x=snapped[0], y=snapped[1], text=text,
        direction=None, snap={"snapped": list(snapped), "info": info},
    )


# Real (snapped center, snap.info, text) from 20260622_171843 — note the WxH and the x both jitter.
T3 = _act("type", (825.1, 444.4), "input 61x28", "Olivia zip jacket")   # Product box
T9 = _act("type", (819.7, 444.4), "input 62x28", "Olivia zip jacket")   # same box, jittered
T12 = _act("type", (825.1, 444.4), "input 61x28", "Olivia zip jacket")
T17 = _act("type", (613.6, 444.4), "input 92x28", "<=3")                # a DIFFERENT box (~200px left)
SEARCH = _act("tap", (121.8, 277.3), "button 75x33")
RESET = _act("tap", (195.2, 277.3), "button 106x33")


def test_action_signature_is_reword_and_jitter_proof():
    # Same control + value despite WxH jitter (61↔62) and 6px center jitter (819↔825).
    assert action_signature(T3) == action_signature(T9) == action_signature(T12)
    # A box ~200px away (the one the agent typed '<=3' into) is a different signature.
    assert action_signature(T3) != action_signature(T17)
    assert action_signature(SEARCH) != action_signature(RESET)


def test_monitor_flags_the_retype_loop_by_signature():
    state = "/admin/review/product"
    mon = ProgressMonitor()
    assert mon.repeated(state, action_signature(T3)) is None
    mon.note(3, state, action_signature(T3))
    mon.note(4, state, action_signature(SEARCH))

    # T9: re-typing the SAME value into the SAME box → flagged, pointing back at T3.
    hit = mon.repeated(state, action_signature(T9))
    assert hit is not None and hit.index == 3
    mon.note(9, state, action_signature(T9))

    mon.note(12, state, action_signature(T12))
    assert mon.repeated(state, action_signature(T12)).index == 3


def test_distinct_box_is_not_a_false_repeat_by_signature():
    state = "/admin/review/product"
    mon = ProgressMonitor()
    mon.note(3, state, action_signature(T3))
    assert mon.repeated(state, action_signature(T17)) is None


def test_check_loop_records_then_flags_repeat():
    # The supervisor's inline Action-Loop Guard, now one call: record-or-detect.
    state = "/admin/review/product"
    mon = ProgressMonitor()
    assert mon.check_loop(3, state, "点击 Search") is None      # first time → recorded
    assert mon.check_loop(4, state, "Reset Filter") is None
    hit = mon.check_loop(7, state, "点击 Search")               # redo of T3 → loop, points back
    assert hit is not None and hit.index == 3
    assert len(mon.turns) == 2                                 # T7 NOT re-noted
    # no-op when state/decision empty (visual platform with no url)
    assert mon.check_loop(8, "", "x") is None
    assert mon.check_loop(8, state, "") is None
