from __future__ import annotations

from types import SimpleNamespace

from gui_agent.core.run.turns import SupervisorTimingCarry, make_verdict_turn
from gui_agent.core.schemas import SupervisorStep


def _step() -> SupervisorStep:
    return SupervisorStep(
        should_act=False,
        stop=False,
        goal_completed=True,
        summary="完成",
    )


def test_make_verdict_turn_captures_supervisor_state():
    check = SimpleNamespace(model_dump=lambda exclude_none=True: {"status": "done"})
    supervisor = SimpleNamespace(
        _last_check=check,
        _last_plan=None,
        _last_replan=None,
        _timings={"checker": 1.2},
        _token_usage={"checker": {"input": 10, "output": 5}},
        _last_sections_loaded=["orders"],
    )

    turn = make_verdict_turn(
        index=2,
        observation_source="screen.png",
        supervisor_step=_step(),
        supervisor=supervisor,
        llm_calls=1,
        input_tokens=10,
        output_tokens=5,
    )

    assert turn.index == 2
    assert turn.observation_source == "screen.png"
    assert turn.action_decision is None
    assert turn.executed is False
    assert turn.checker == {"status": "done"}
    assert turn.timings == {"checker": 1.2}
    assert turn.token_usage == {"checker": {"input": 10, "output": 5}}
    assert turn.sections_loaded == ["orders"]


def test_supervisor_timing_carry_merges_ordered_timings_and_tokens():
    carry = SupervisorTimingCarry()
    first = SimpleNamespace(
        _timings={"checker": 1.0},
        _token_usage={"checker": {"input": 2, "output": 3}},
    )
    final = SimpleNamespace(
        _timings={"checker": 0.5, "action_policy": 2.0},
        _token_usage={
            "checker": {"input": 1, "output": 1},
            "action_policy": {"input": 4, "output": 6},
        },
    )

    carry.collect(first)
    carry.merge_into(final)

    assert final._timings == {"checker": 1.5, "action_policy": 2.0}
    assert final._timings_order == ["checker", "action_policy"]
    assert final._token_usage == {
        "checker": {"input": 3, "output": 4},
        "action_policy": {"input": 4, "output": 6},
    }
