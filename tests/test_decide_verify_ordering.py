"""Verify-first ordering in the single-step supervisor.

Regression for logs/.../android/20260611_085000: turn 2 hit the 闹钟 tab and the screen
DID advance to the alarm page, but TargetVerify false-flagged off-target (and settle
false-flagged no-effect on a 6.7<8.0 whole-frame diff). The OffTarget / NoEffect fast-paths
used to run BEFORE _single_check, so turn 3 skipped verification and replanned a milestone
the action had already satisfied.

Fix: run _single_check FIRST — if done, advance; only when NOT done do off-target /
no-effect route to replan. These lock that ordering by mocking the checker.
"""

from __future__ import annotations

from gui_agent.core.supervisor.milestone import policy as P
from gui_agent.core.supervisor.milestone import llm_runtime as L
from gui_agent.core.supervisor.milestone.evidence import (
    action_lifecycle_claims,
    checker_claim,
    execution_contract_for,
    target_value_claims,
)
from gui_agent.core.supervisor.milestone.execution_scope import execution_scope_for
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.supervisor.milestone.schemas import _SingleCheckResult
from gui_agent.core.run.turns import make_interactive_turn
from gui_agent.core.schemas import (
    BaseAction,
    BaseActionDecision,
    Milestone,
    Observation,
    PolicyTurn,
    SupervisorStep,
    TargetVerify,
)


def _executed_turn(
    *,
    index: int,
    source: str,
    step: SupervisorStep,
    action: BaseAction,
    actual_element: str,
    no_effect: bool = False,
) -> PolicyTurn:
    turn = make_interactive_turn(
        index=index,
        observation_source=source,
        supervisor_step=step,
        action_decision=BaseActionDecision(action=action),
        executed=True,
    )
    turn.target_verify = TargetVerify(on_target=True, actual_element=actual_element)
    turn.no_effect = no_effect
    return turn


def _policy():
    p = MilestoneSupervisorPolicy()
    m = Milestone.model_validate(
        {"id": "m1", "name": "进入闹钟页", "description": "d", "success_condition": "闹钟列表页", "kind": "navigation"}
    )
    p._milestones = {"m1": m}
    p._current_id = "m1"
    p._order = ["m1"]
    return p, m


def _tap_turn(*, on_target: bool, no_effect: bool = False) -> PolicyTurn:
    act = BaseAction(action_type="tap", x=125, y=967, description="点击底部『闹钟』tab")
    turn = _executed_turn(
        index=1,
        source="test",
        step=SupervisorStep(
            should_act=True, instruction="点击底部『闹钟』tab", stop=False,
            goal_completed=False, summary="", milestone_id="m1",
        ),
        action=act,
        actual_element="世界时钟 tab",
        no_effect=no_effect,
    )
    assert turn.target_verify is not None
    turn.target_verify.on_target = on_target
    return turn


def _submit_turn(*, no_effect: bool = True, reason: str = "点击 Submit Comment 按钮") -> PolicyTurn:
    act = BaseAction(action_type="tap", x=640, y=820, description=reason)
    return _executed_turn(
        index=1,
        source="browser",
        step=SupervisorStep(
            should_act=True,
            instruction=reason,
            stop=False,
            goal_completed=False,
            summary="",
            milestone_id="m1",
            atomic_role="commit",
        ),
        action=act,
        actual_element="Submit Comment button",
        no_effect=no_effect,
    )


def _write_turn(*, description: str = "在目标字段输入任务值") -> PolicyTurn:
    act = BaseAction(action_type="type", x=500, y=700, text="target", description=description)
    return _executed_turn(
        index=0,
        source="browser",
        step=SupervisorStep(
            should_act=True,
            instruction=description,
            stop=False,
            goal_completed=False,
            summary="",
            milestone_id="m1",
            atomic_role="write",
            action_family="input",
        ),
        action=act,
        actual_element="target field",
    )


def _wire(monkeypatch, p, check_status: str) -> list[str]:
    """Mock the LLM checker + the two terminal branches; record which fired."""
    monkeypatch.setattr(P, "is_loading_frame", lambda obs: False)
    monkeypatch.setattr(
        p, "_single_check",
        lambda *a, **k: _SingleCheckResult(
            status=check_status,
            outcome_status="confirmed" if check_status == "done" else "unverified",
            reason="r",
            summary="s",
        ),
    )
    calls: list[str] = []
    monkeypatch.setattr(p, "_advance", lambda *a, **k: (calls.append("advance"), "ADV")[1])
    monkeypatch.setattr(p, "_handle_stuck", lambda *a, **k: (calls.append("stuck"), "STK")[1])
    return calls


def _wire_check(monkeypatch, p, check: _SingleCheckResult) -> list[str]:
    monkeypatch.setattr(P, "is_loading_frame", lambda obs: False)
    monkeypatch.setattr(p, "_single_check", lambda *a, **k: check)
    calls: list[str] = []
    monkeypatch.setattr(p, "_advance", lambda *a, **k: (calls.append("advance"), "ADV")[1])
    monkeypatch.setattr(p, "_handle_stuck", lambda *a, **k: (calls.append("stuck"), "STK")[1])
    monkeypatch.setattr(p, "_plan_single", lambda *a, **k: (calls.append("plan"), "PLAN")[1])
    monkeypatch.setattr(p._monitor, "check_instruction_repetition", lambda *a, **k: None)
    return calls


def test_done_advances_despite_false_off_target(monkeypatch):
    p, m = _policy()
    calls = _wire(monkeypatch, p, "done")
    p._run_single_turn(m, Observation(png_bytes=b"x", source="test"), [_tap_turn(on_target=False)])
    assert calls == ["advance"]  # verified first -> advanced, NOT replanned


def test_off_target_still_replans_when_not_done(monkeypatch):
    p, m = _policy()
    calls = _wire(monkeypatch, p, "in_progress")
    p._run_single_turn(m, Observation(png_bytes=b"x", source="test"), [_tap_turn(on_target=False)])
    assert calls == ["stuck"]  # not done -> off-target signal still routes to replan


def test_done_advances_despite_false_no_effect(monkeypatch):
    p, m = _policy()
    calls = _wire(monkeypatch, p, "done")
    p._run_single_turn(
        m, Observation(png_bytes=b"x", source="test"),
        [_tap_turn(on_target=True, no_effect=True)],
    )
    assert calls == ["advance"]  # false no_effect no longer blocks verification


# ── URL-change (browser ground truth) suppresses pixel false positives ──────────
def _wire_plan(monkeypatch, p) -> list[str]:
    """Like _wire but in_progress + also records _plan_single / suppresses repetition."""
    calls = _wire(monkeypatch, p, "in_progress")
    monkeypatch.setattr(p, "_plan_single", lambda *a, **k: (calls.append("plan"), "PLAN")[1])
    monkeypatch.setattr(p._monitor, "check_instruction_repetition", lambda *a, **k: None)
    return calls


def test_url_change_suppresses_false_no_effect(monkeypatch):
    p, m = _policy()
    calls = _wire_plan(monkeypatch, p)
    p._monitor._last_url = "http://x/orders"
    obs = Observation(png_bytes=b"x", source="browser", url="http://x/orders/transport")  # navigated
    p._run_single_turn(m, obs, [_tap_turn(on_target=True, no_effect=True)])
    assert calls == ["plan"]  # URL changed => the tap DID navigate => no_effect suppressed


def test_no_url_change_keeps_no_effect_replan(monkeypatch):
    p, m = _policy()
    calls = _wire_plan(monkeypatch, p)
    p._monitor._last_url = "http://x/orders"
    obs = Observation(png_bytes=b"x", source="browser", url="http://x/orders")  # unchanged
    p._run_single_turn(m, obs, [_tap_turn(on_target=True, no_effect=True)])
    assert calls == ["stuck"]  # URL unchanged => no_effect stands => replan


def _submit_milestone_policy():
    # TerminalDispatchGate only arms when the MILESTONE itself declares a dispatch terminal
    # (…提交/保存…) — an arrival milestone must not be force-done'd by a stray dispatch-verb click.
    p = MilestoneSupervisorPolicy()
    m = Milestone.model_validate(
        {
            "id": "m1", "name": "提交评论", "description": "d",
            "success_condition": "评论出现在历史中", "kind": "action",
            "requires_commit": True,
        }
    )
    p._milestones = {"m1": m}
    p._current_id = "m1"
    p._order = ["m1"]
    return p, m


def test_terminal_dispatch_advances_without_visible_feedback(monkeypatch):
    p, m = _submit_milestone_policy()
    calls = _wire_check(
        monkeypatch,
        p,
        _SingleCheckResult(
            status="in_progress",
            outcome_status="unverified",
            reason="未看到成功提示或新评论出现在历史中",
            summary="提交后页面没有明显反馈",
            missing_evidence=["缺少成功提示"],
        ),
    )
    p._monitor._last_url = "http://x/order/view/65"
    obs = Observation(png_bytes=b"x", source="browser", url="http://x/order/view/65")
    p._run_single_turn(m, obs, [_write_turn(), _submit_turn(no_effect=True)])
    assert calls == ["advance"]  # terminal dispatch is enough when only visible feedback is missing


def test_terminal_dispatch_gate_respects_negative_feedback(monkeypatch):
    p, m = _submit_milestone_policy()
    calls = _wire_check(
        monkeypatch,
        p,
        _SingleCheckResult(
            status="in_progress",
            reason="页面显示 validation failed: required field missing",
            summary="提交失败",
            missing_evidence=["需要修正表单错误"],
            outcome_status="contradicted",
        ),
    )
    p._monitor._last_url = "http://x/order/view/65"
    obs = Observation(png_bytes=b"x", source="browser", url="http://x/order/view/65")
    p._run_single_turn(m, obs, [_submit_turn(no_effect=True)])
    assert calls == ["stuck"]  # explicit failure must not be swallowed by dispatch completion


def test_checker_stuck_status_routes_to_handle_stuck(monkeypatch):
    # The checker's OWN PROGRESS verdict (status=stuck, judged from the task-progress trace) routes
    # to the stuck path — before the deterministic off-target / no-effect signals.
    p, m = _policy()
    calls = _wire(monkeypatch, p, "stuck")
    p._run_single_turn(m, Observation(png_bytes=b"x", source="test"), [_tap_turn(on_target=True)])
    assert calls == ["stuck"]


# ── require_fresh_action must not re-demand a write after a successful terminal submit ──────────
# Regression for WebArena 499 (logs/.../20260708_161248): "fill tracking + Submit Shipment" is one
# require_fresh_action milestone. Submit POSTed order_shipment/save (302) and REDIRECTED to the
# order view, so the submit's executed record fell out of the new page-scope → pre_existing turned
# True → FreshActionRequired flipped done→in_progress and the agent hunted the vanished tracking
# form until it reported ACTION_NOT_ALLOWED_ERROR — even though the mutation already succeeded.
def _fresh_action_policy():
    p = MilestoneSupervisorPolicy()
    m = Milestone.model_validate({
        "id": "m1", "name": "填入追踪号并提交发货", "description": "d",
        "success_condition": "发货已保存，订单出现追踪号", "kind": "action",
        "require_fresh_action": True,
        "requires_commit": True,
    })
    p._milestones = {"m1": m}
    p._current_id = "m1"
    p._order = ["m1"]
    return p, m


def _shipment_submit_turn() -> PolicyTurn:
    act = BaseAction(action_type="tap", x=896, y=798, description="点击页面右下角的 Submit Shipment 按钮")
    return _executed_turn(
        index=1,
        source="browser",
        step=SupervisorStep(
            should_act=True, instruction="点击页面右下角的 Submit Shipment 按钮",
            stop=False, goal_completed=False, summary="", milestone_id="m1",
            atomic_role="commit",
        ),
        action=act,
        actual_element="Submit Shipment button",
    )


def _no_redemand_wire(monkeypatch, p):
    """Force post-redirect scope reset (pre_existing True) and record any re-demand plan call."""
    monkeypatch.setattr(P, "history_for_scope", lambda *a, **k: [])
    plan_calls: list[str] = []
    monkeypatch.setattr(p, "_plan_single", lambda *a, **k: (plan_calls.append("plan"), "PLAN")[1])
    return plan_calls


def _completion_decision(p, m, obs, history):
    assert p._last_check is not None
    scope = execution_scope_for(m, obs)
    claims = action_lifecycle_claims(
        m, history, scope=scope, monitor=p._monitor, ledger=p._action_ledger
    )
    claims.extend(target_value_claims(m, obs, scope=scope))
    claims.append(checker_claim(p._last_check, scope=scope, subject_scope=scope))
    return p._completion_evaluator.decide(
        execution_contract_for(m, p._execution_contract), claims, scope=scope
    )


def test_fresh_action_accepts_done_after_terminal_submit_redirect(monkeypatch):
    p, m = _fresh_action_policy()
    plan_calls = _no_redemand_wire(monkeypatch, p)
    p._last_check = _SingleCheckResult(status="done", outcome_status="confirmed", reason="发货已保存，已跳回订单详情页", summary="ok")
    obs = Observation(png_bytes=b"x", source="browser", url="http://x/admin/sales/order/view/order_id/304")
    history = [_write_turn(description="输入追踪号"), _shipment_submit_turn()]
    decision = _completion_decision(p, m, obs, history)
    assert decision.status == "satisfied"
    step = p._advance(m, obs, history, decision=decision)
    assert plan_calls == []            # FreshActionRequired suppressed — no re-demand
    assert m.status == "done"
    assert step.goal_completed is True


def test_fresh_action_still_redemands_when_nothing_dispatched(monkeypatch):
    # Control: genuinely pre-existing (no write for this milestone) → must still re-demand.
    p, m = _fresh_action_policy()
    plan_calls = _no_redemand_wire(monkeypatch, p)
    p._last_check = _SingleCheckResult(status="done", outcome_status="confirmed", reason="状态疑似已满足", summary="ok")
    obs = Observation(png_bytes=b"x", source="browser", url="http://x/admin/sales/order/view/order_id/304")
    decision = _completion_decision(p, m, obs, [])
    assert decision.status == "pending"
    assert "终端提交尚未派发" in decision.reason
    assert plan_calls == []
    assert m.status != "done"


def test_fresh_action_redemands_when_submit_shows_negative_feedback(monkeypatch):
    # Submit dispatched but the frame reports a validation failure → must NOT swallow as done.
    p, m = _fresh_action_policy()
    plan_calls = _no_redemand_wire(monkeypatch, p)
    p._last_check = _SingleCheckResult(
        status="done", reason="提交时页面提示 validation failed: required field missing", summary="ok",
        outcome_status="contradicted",
    )
    obs = Observation(png_bytes=b"x", source="browser", url="http://x/admin/sales/order/view/order_id/304")
    decision = _completion_decision(p, m, obs, [_shipment_submit_turn()])
    assert decision.status == "contradicted"
    assert plan_calls == []
    assert m.status != "done"


# ── WebArena 502 (20260708_185657) regression pair ──────────────────────────────
# Turn 20: FreshActionRequired fired on an ARRIVAL action milestone (click row → edit page); the
# planner invented a stray Save whose success banner persisted. Turn 22: the checker credited that
# leftover banner to the NEXT milestone ("set Stock Status … and save") whose own Save was never
# clicked → the mutation silently never happened, score 0.


def _arrival_click_turn(milestone_id: str = "m1") -> PolicyTurn:
    act = BaseAction(action_type="tap", x=400, y=500, description="点击列表中目标产品行")
    return _executed_turn(
        index=1,
        source="browser",
        step=SupervisorStep(
            should_act=True, instruction="点击列表中目标产品行",
            stop=False, goal_completed=False, summary="", milestone_id=milestone_id,
        ),
        action=act,
        actual_element="产品行",
    )


def test_fresh_action_accepts_arrival_click_from_full_history(monkeypatch):
    # Arrival action (no terminal-dispatch verb in the name): the row click navigated, so its turn
    # dropped out of the destination page scope. Full-history execution must count — no stray
    # "write" may be demanded.
    p = MilestoneSupervisorPolicy()
    m = Milestone.model_validate({
        "id": "m1", "name": "点击列表中 Type=Configurable 的那一行打开编辑页", "description": "d",
        "success_condition": "已进入该产品编辑页", "kind": "navigation",
    })
    p._milestones = {"m1": m}
    p._current_id = "m1"
    p._order = ["m1"]
    plan_calls = _no_redemand_wire(monkeypatch, p)
    p._last_check = _SingleCheckResult(status="done", outcome_status="confirmed", reason="已进入编辑页", summary="ok")
    obs = Observation(png_bytes=b"x", source="browser", url="http://x/admin/catalog/product/edit/id/446")
    history = [_arrival_click_turn()]
    decision = _completion_decision(p, m, obs, history)
    assert decision.status == "satisfied"
    step = p._advance(m, obs, history, decision=decision)
    assert plan_calls == []            # no FreshActionRequired override, no stray write demanded
    assert m.status == "done"
    assert step.goal_completed is True


def _select_option_turn(milestone_id: str = "m1") -> PolicyTurn:
    act = BaseAction(action_type="tap", x=369, y=874, description="选择下拉选项 Out of Stock")
    return _executed_turn(
        index=2,
        source="browser",
        step=SupervisorStep(
            should_act=True, instruction="在 Stock Status 下拉框选择 Out of Stock",
            stop=False, goal_completed=False, summary="", milestone_id=milestone_id,
            atomic_role="write", action_family="select",
        ),
        action=act,
        actual_element="Stock Status 下拉",
    )


def _save_milestone_policy():
    p = MilestoneSupervisorPolicy()
    m = Milestone.model_validate({
        "id": "m1", "name": "将 Stock Status 下拉设为 Out of Stock 并保存", "description": "d",
        "success_condition": "页面显示保存成功提示", "kind": "action",
        "require_fresh_action": True,
        "requires_commit": True,
    })
    p._milestones = {"m1": m}
    p._current_id = "m1"
    p._order = ["m1"]
    return p, m


def test_dispatch_ledger_blocks_done_on_residual_banner(monkeypatch):
    # The select_option executed (so pre_existing is False and FreshActionRequired stays quiet),
    # the checker sees a leftover success banner and says done — but this milestone's own Save
    # was never dispatched. The ledger must veto done.
    p, m = _save_milestone_policy()
    plan_calls: list[str] = []
    monkeypatch.setattr(p, "_plan_single", lambda *a, **k: (plan_calls.append("plan"), "PLAN")[1])
    p._last_check = _SingleCheckResult(
        status="done", outcome_status="confirmed", reason="页面顶部显示 'You saved the product.'，Stock Status 为 Out of Stock", summary="ok",
    )
    obs = Observation(png_bytes=b"x", source="browser", url="http://x/admin/catalog/product/edit/id/446")
    decision = _completion_decision(p, m, obs, [_select_option_turn()])
    assert decision.status == "pending"
    assert "终端提交尚未派发" in decision.reason
    assert plan_calls == []
    assert m.status != "done"


def test_dispatch_ledger_accepts_done_after_own_save_click(monkeypatch):
    # Same milestone, but the Save WAS clicked within this milestone → done stands.
    p, m = _save_milestone_policy()
    plan_calls: list[str] = []
    monkeypatch.setattr(p, "_plan_single", lambda *a, **k: (plan_calls.append("plan"), "PLAN")[1])
    p._last_check = _SingleCheckResult(status="done", outcome_status="confirmed", reason="保存成功提示可见", summary="ok")
    obs = Observation(png_bytes=b"x", source="browser", url="http://x/admin/catalog/product/edit/id/446")
    # Verb-class agreement: the milestone says 保存 (save class) → the dispatch must be a Save
    # click, not just any dispatch-verb action (an Apply Filters click must not count).
    act = BaseAction(action_type="tap", x=896, y=180, description="点击右上角 Save 按钮保存")
    save = _executed_turn(
        index=3,
        source="browser",
        step=SupervisorStep(
            should_act=True, instruction="点击右上角 Save 按钮保存",
            stop=False, goal_completed=False, summary="", milestone_id="m1",
            atomic_role="commit",
        ),
        action=act,
        actual_element="Save button",
    )
    history = [_select_option_turn(), save]
    decision = _completion_decision(p, m, obs, history)
    assert decision.status == "satisfied"
    step = p._advance(m, obs, history, decision=decision)
    assert plan_calls == []
    assert m.status == "done"
    assert step.goal_completed is True


def test_terminal_save_redirect_wins_before_affordance_acquire(monkeypatch):
    # WebArena 545 (20260709_173419): after clicking Save, Magento redirected back to the edit URL
    # and the backend POST was already correct, but the next frame still exposed Content/Short
    # Description below the fold. The acquire gate ran first and kept scrolling until max_turns.
    # A terminal Save + URL change is stronger than "field is offscreen again": skip reacquire,
    # but still let checker consume any result feedback before falling back to accepted_unverified.
    p, m = _save_milestone_policy()
    monkeypatch.setattr(P, "is_loading_frame", lambda _obs: False)
    checker_calls: list[int] = []

    def _unverified_checker(*_args, **_kwargs):
        checker_calls.append(1)
        return _SingleCheckResult(
            status="in_progress",
            reason="保存请求已响应，但当前帧没有直接展示目标字段",
            summary="提交已响应，结果未完全确认",
            outcome_status="unverified",
        )

    monkeypatch.setattr(L, "run_checker", _unverified_checker)
    p._monitor._last_url = "http://x/admin/catalog/product/edit/id/1556/"
    p._last_check = _SingleCheckResult(
        status="in_progress",
        outcome_status="unverified",
        reason="Short Description 已更新，但尚未点击保存按钮",
        summary="等待保存",
    )
    save = _executed_turn(
        index=3,
        source="browser",
        step=SupervisorStep(
            should_act=True,
            instruction="点击页面右上角的「Save」按钮",
            stop=False,
            goal_completed=False,
            summary="",
            milestone_id="m1",
            atomic_role="commit",
        ),
        action=BaseAction(
            action_type="tap",
            x=1165,
            y=39,
            description="点击页面右上角的 Save 按钮",
        ),
        actual_element="Save button",
    )
    obs = Observation(
        png_bytes=b"x",
        source="browser",
        url="http://x/admin/catalog/product/edit/id/1556/set/9/type/configurable/store/0/back/edit/",
        form_controls=[
            {
                "kind": "section_toggle",
                "label": "Content",
                "value": "false",
                "rect": {"x": 528, "y": 1400, "w": 1118, "h": 62},
                "in_viewport": False,
                "viewport_pos": "below",
            },
            {
                "kind": "rich_textarea",
                "label": "Short Description",
                "rect": {"x": 528, "y": 1800, "w": 542, "h": 402},
                "in_viewport": False,
                "viewport_pos": "below",
            },
        ],
    )

    step = p._run_single_turn(m, obs, [_select_option_turn(), save])

    assert m.status == "done"
    assert step.goal_completed is True
    assert step.should_act is False
    assert step.completion_status == "accepted_unverified"
    assert checker_calls == [1]


# ── WebArena 505 (20260708_194754/195215): TerminalDispatchGate verb-class misfire ──────────────
def test_terminal_dispatch_gate_ignores_arrival_milestone(monkeypatch):
    # Arrival milestone (click a row to open its edit page) declares NO dispatch verb: a
    # mid-milestone "Apply Filters" click (apply class) must not force-done it.
    p = MilestoneSupervisorPolicy()
    m = Milestone.model_validate({
        "id": "m1", "name": "点选 Type=Configurable Product 的产品行，打开其编辑页", "description": "d",
        "success_condition": "已进入该产品编辑页", "kind": "action",
    })
    p._milestones = {"m1": m}
    p._current_id = "m1"
    p._order = ["m1"]
    calls = _wire_check(
        monkeypatch, p,
        _SingleCheckResult(
            status="in_progress",
            outcome_status="unverified",
            reason="筛选已应用并显示目标行，但尚未点击进入编辑页",
            summary="仍在列表页",
        ),
    )
    p._monitor._last_url = "http://x/admin/catalog/product/"
    obs = Observation(png_bytes=b"x", source="browser", url="http://x/admin/catalog/product/")
    apply_turn = _submit_turn(no_effect=False, reason="点击 'Apply Filters' 按钮以应用筛选条件")
    p._run_single_turn(m, obs, [apply_turn])
    assert calls == ["plan"]  # gate must NOT force done — keep planning toward the row click


def test_dispatch_ledger_rejects_wrong_verb_class(monkeypatch):
    # Save milestone (…并保存): an Apply Filters dispatch (apply class) in history must not
    # satisfy the ledger — only a save-class dispatch counts.
    p, m = _save_milestone_policy()
    plan_calls: list[str] = []
    monkeypatch.setattr(p, "_plan_single", lambda *a, **k: (plan_calls.append("plan"), "PLAN")[1])
    p._last_check = _SingleCheckResult(status="done", outcome_status="confirmed", reason="成功提示可见", summary="ok")
    obs = Observation(png_bytes=b"x", source="browser", url="http://x/admin/catalog/product/edit/id/446")
    apply_turn = _submit_turn(no_effect=False, reason="点击 'Apply Filters' 按钮以应用筛选条件")
    decision = _completion_decision(p, m, obs, [apply_turn])
    assert decision.status == "pending"
    assert plan_calls == []
    assert m.status != "done"


# ── Feasibility kickback must not re-decompose a milestone whose terminal submit already succeeded ──
# Regression for WebArena 499 (20260708_165316): after the shipment Submit succeeded and redirected,
# the return-location recovery re-opened the milestone; Feasibility then judged the vanished form
# infeasible and kicked back → 25-turn re-decompose loop. A completed mutation must degrade to a
# clean bounded fail, not a re-decompose.
def _obs() -> Observation:
    return Observation(png_bytes=b"x", source="browser", url="http://x/admin/sales/order/view/order_id/304")


def test_maybe_kickback_suppressed_after_successful_terminal_submit(monkeypatch):
    import gui_agent.core.supervisor.milestone.feasibility as feas

    def _boom(*a, **k):
        raise AssertionError("judge_feasibility must not run once the terminal submit has succeeded")

    monkeypatch.setattr(feas, "judge_feasibility", _boom)
    monkeypatch.setattr(feas, "control_presence_text", lambda obs: "Add Tracking Number 缺失")

    p, m = _fresh_action_policy()
    p._last_check = _SingleCheckResult(status="stuck", outcome_status="unverified", reason="表单不见了", summary="stuck")
    step = p._maybe_kickback(m, _obs(), None, [_shipment_submit_turn()])
    assert step is None            # suppressed → falls through to a clean bounded fail, no re-decompose


def test_maybe_kickback_still_fires_when_no_dispatch_and_control_absent(monkeypatch):
    import gui_agent.core.supervisor.milestone.feasibility as feas
    from gui_agent.core.supervisor.milestone.feasibility import FeasibilityVerdict

    monkeypatch.setattr(feas, "control_presence_text", lambda obs: "required control 缺失")
    monkeypatch.setattr(
        feas, "judge_feasibility",
        lambda *a, **k: FeasibilityVerdict(feasible=False, reason="控件确实缺失", dead_end="x", directive="重规划"),
    )
    monkeypatch.setattr(feas, "compose_directive", lambda v: "重规划指令")

    p, m = _fresh_action_policy()
    p._last_check = _SingleCheckResult(status="stuck", outcome_status="unverified", reason="控件缺失", summary="stuck")
    step = p._maybe_kickback(m, _obs(), None, [])   # empty history: nothing dispatched
    assert step is not None
    assert step.replan_directive == "重规划指令"
    assert m.status == "failed"
