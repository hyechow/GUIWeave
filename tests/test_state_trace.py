"""Deterministic tests for the Action-Loop Guard's task-state memory (state_trace)."""

from gui_agent.core.supervisor.milestone.state_trace import (
    StateTrace,
    canonical_url,
    state_trace_block,
)


def test_canonical_url_strips_host_and_volatile_filter_segments():
    base = "http://h:7780/admin/review/product/index"
    filtered = "http://h:7780/admin/review/product/index/filter/AbC123==/internal_reviews//form_key/xy/"
    # the oscillating filtered / unfiltered URLs collapse to ONE canonical page state
    assert canonical_url(base) == "/admin/review/product/index"
    assert canonical_url(filtered) == "/admin/review/product/index/internal_reviews"
    assert canonical_url(base) == canonical_url(base + "/sort/name/dir/asc")
    assert canonical_url("") == "" and canonical_url(None) == ""


def test_repeated_detects_same_state_action():
    tr = StateTrace()
    s = "/admin/review/product/index"
    tr.note(3, s, "点击 Search 按钮执行搜索")
    tr.note(4, s, "点击 Reset Filter 按钮")
    # a NEW action in the same state is not a repeat
    assert tr.repeated(s, "在 Product 框输入关键词") is None
    # the SAME (state, action) as T3 is a repeat → points back at T3
    hit = tr.repeated(s, "点击 Search 按钮执行搜索")
    assert hit is not None and hit.index == 3


def test_repeated_is_phrasing_tolerant_and_state_sensitive():
    tr = StateTrace()
    tr.note(1, "/a", "点击 Search 按钮，执行搜索！")
    # same action, light phrasing diff, same state → repeat
    assert tr.repeated("/a", "点击Search按钮执行搜索") is not None
    # same action, DIFFERENT state → not a repeat (progressed to a new page)
    assert tr.repeated("/b", "点击Search按钮执行搜索") is None


def test_repeated_is_interaction_state_sensitive_for_browser_forms():
    tr = StateTrace()
    state = "/admin/catalog/product"
    tr.note(4, state, "按回车键提交", "Search by keyword=Olivia zip jacket")

    assert tr.repeated(state, "按回车键提交", "Search by keyword=Olivia") is None
    hit = tr.repeated(state, "按回车键提交", "Search by keyword=Olivia zip jacket")
    assert hit is not None and hit.index == 4


def test_distinct_states_counts_frontier():
    tr = StateTrace()
    for i, s in enumerate(["/a", "/a", "/b", "/a"]):
        tr.note(i, s, f"act{i}")
    assert tr.distinct_states() == 2  # churned across only 2 pages


def test_render_marks_repeats_and_block():
    tr = StateTrace()
    s = "/admin/review/product/index"
    tr.note(3, s, "搜索 Olivia")
    tr.note(4, s, "Reset Filter")
    tr.note(7, s, "搜索 Olivia")   # repeat of T3
    text = tr.render()
    assert "T3" in text and "T7" in text
    assert "⚠️重复(同 T3)" in text   # the loop is made visible

    blk = state_trace_block(tr)
    assert blk is not None and blk.id == "runtime.state_trace"
    assert "任务进展轨迹" in blk.content and "⚠️重复" in blk.content
    assert state_trace_block(StateTrace()) is None  # empty → no block


def test_regression_webarena_113_reset_search_loop_20260622_105707():
    """Regression: WebArena task-113 live run 20260622_105707 — a Reset→search→Reset loop the
    frame-level guards missed (every turn changed url/DOM → looked like progress, ran 11+ turns).

    The Action-Loop Guard must (1) collapse the oscillating filtered / cleared-filter URLs into ONE
    canonical page state, and (2) flag the repeated Reset (T8≡T4) and Search (T10≡T7) so the planner
    is forced off the loop. Sequence is the real (url, instruction) trace from that run."""
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
    # (1) the filtered and cleared-filter URLs collapse to a single canonical page state
    assert len({canonical_url(u) for _, u, _ in seq}) == 1

    # (2) replay: a (state, action) seen before is intercepted (not re-noted); the loop is bounded
    tr = StateTrace()
    intercepted = []
    for idx, url, instr in seq:
        state = canonical_url(url)
        if tr.repeated(state, instr) is not None:
            intercepted.append(idx)
        else:
            tr.note(idx, state, instr)
    assert intercepted == [8, 10]                     # the 2nd Reset and 2nd Search are caught
    assert tr.repeated(canonical_url(seq[0][1]), seq[0][2]) is not None  # 'Search' is now in memory


def test_run_checker_injects_state_trace_block_for_progress_judgment(monkeypatch):
    """The Action-Loop Guard also FEEDS the checker: run_checker must surface the state→decision
    trace (so the checker can judge advancing-vs-looping) when given state_trace_text, and omit the
    block when it's empty. Pins the wiring so a refactor can't silently drop it."""
    import gui_agent.core.supervisor.milestone.helpers as helpers
    from gui_agent.core.schemas import Milestone, Observation
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
        return _SingleCheckResult(status="in_progress", reason="检查中尚未达成", summary="进行中")

    monkeypatch.setattr(helpers, "invoke_structured", _fake_invoke)
    monkeypatch.setattr(helpers, "_make_llm", lambda: object())

    from io import BytesIO
    from PIL import Image
    _buf = BytesIO()
    Image.new("RGB", (8, 8), "white").save(_buf, "PNG")  # valid, survives the checker's retina halving
    _PNG = _buf.getvalue()
    ms = Milestone.model_validate({
        "id": "m1", "name": "筛选评论", "description": "d", "success_condition": "列表已刷新", "kind": "filter",
    })
    obs = Observation(png_bytes=_PNG, source="browser")
    trace = " T3 状态:/admin/review/product/index | 决策:点击search ⚠️重复(同 T1)"

    helpers.run_checker(ms, obs, [], state_trace_text=trace)
    assert "任务进展轨迹" in captured["text"]
    assert "⚠️重复" in captured["text"] and "点击search" in captured["text"]

    helpers.run_checker(ms, obs, [], state_trace_text="")   # empty → no trace block
    assert "任务进展轨迹" not in captured["text"]
