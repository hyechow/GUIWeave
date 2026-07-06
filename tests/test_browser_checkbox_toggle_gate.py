from __future__ import annotations

import gui_agent.core.supervisor.milestone.policy as policy_mod
from gui_agent.core.schemas import Milestone, Observation
from gui_agent.core.supervisor.milestone.helpers import checkbox_toggle_satisfies_target


class _CheckerReached(Exception):
    pass


def _column_milestone() -> Milestone:
    return Milestone(
        id="m_column",
        name="打开 Columns 面板并启用 Target Field 列",
        description="打开 Columns 面板并启用 Target Field 列",
        success_condition="Target Field 复选框已勾选并可用于后续读取",
        kind="action",
    )


def test_checkbox_toggle_gate_uses_semantic_checked_state() -> None:
    ms = _column_milestone()

    assert checkbox_toggle_satisfies_target(
        None,
        [{"role": "checkbox", "key": "Target Field", "value": "true", "ref": 1}],
        ms,
    )
    assert not checkbox_toggle_satisfies_target(
        None,
        [{"role": "checkbox", "key": "Target Field", "value": "false", "ref": 1}],
        ms,
    )


def test_checkbox_toggle_gate_falls_back_to_form_control_state() -> None:
    ms = _column_milestone()

    assert checkbox_toggle_satisfies_target(
        [{"kind": "checkbox_input", "label": "Target Field", "value": "on"}],
        None,
        ms,
    )
    assert not checkbox_toggle_satisfies_target(
        [{"kind": "checkbox_input", "label": "Target Field", "value": "off"}],
        None,
        ms,
    )


def test_checkbox_toggle_gate_does_not_fast_path_compound_save_goal() -> None:
    ms = Milestone(
        id="m_save",
        name="启用 Target Field 列并保存设置",
        description="启用 Target Field 列并保存设置",
        success_condition="Target Field 复选框已勾选并保存",
        kind="action",
    )

    assert not checkbox_toggle_satisfies_target(
        None,
        [{"role": "checkbox", "key": "Target Field", "value": "true", "ref": 1}],
        ms,
    )


def test_policy_checkbox_gate_bypasses_checker(monkeypatch) -> None:
    checker_calls: list[int] = []

    def _spy_run_checker(*_args, **_kwargs):
        checker_calls.append(1)
        raise _CheckerReached()

    monkeypatch.setattr(policy_mod, "run_checker", _spy_run_checker)
    monkeypatch.setattr(policy_mod, "is_loading_frame", lambda _obs: False)

    policy = policy_mod.MilestoneSupervisorPolicy()
    ms = _column_milestone()
    policy.reseed(ms)
    obs = Observation(
        png_bytes=b"\x89PNG\r\n\x1a\n",
        source="test",
        semantic_tree=[{"role": "checkbox", "key": "Target Field", "value": "true", "ref": 1}],
    )

    step = policy.step(obs, goal="read target field values", history=[])

    assert checker_calls == []
    assert ms.status == "done"
    assert step.goal_completed is True


def test_checkbox_gate_can_satisfy_fresh_action_milestone(monkeypatch) -> None:
    monkeypatch.setattr(policy_mod, "run_checker", lambda *_args, **_kwargs: (_ for _ in ()).throw(_CheckerReached()))
    monkeypatch.setattr(policy_mod, "is_loading_frame", lambda _obs: False)

    policy = policy_mod.MilestoneSupervisorPolicy()
    ms = _column_milestone()
    ms.require_fresh_action = True
    policy.reseed(ms)
    obs = Observation(
        png_bytes=b"\x89PNG\r\n\x1a\n",
        source="test",
        semantic_tree=[{"role": "checkbox", "key": "Target Field", "value": "true", "ref": 1}],
    )

    step = policy.step(obs, goal="read target field values", history=[])

    assert ms.status == "done"
    assert step.goal_completed is True


def test_policy_unchecked_toggle_falls_through_to_checker(monkeypatch) -> None:
    checker_calls: list[int] = []

    def _spy_run_checker(*_args, **_kwargs):
        checker_calls.append(1)
        raise _CheckerReached()

    monkeypatch.setattr(policy_mod, "run_checker", _spy_run_checker)
    monkeypatch.setattr(policy_mod, "is_loading_frame", lambda _obs: False)

    policy = policy_mod.MilestoneSupervisorPolicy()
    ms = _column_milestone()
    policy.reseed(ms)
    obs = Observation(
        png_bytes=b"\x89PNG\r\n\x1a\n",
        source="test",
        semantic_tree=[{"role": "checkbox", "key": "Target Field", "value": "false", "ref": 1}],
    )

    try:
        policy.step(obs, goal="read target field values", history=[])
    except _CheckerReached:
        pass

    assert checker_calls == [1]
