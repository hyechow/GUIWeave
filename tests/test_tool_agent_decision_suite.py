from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from replay.decision_suite import (
    load_decision_manifest,
    replay_decision_suite,
    score_decision_sample,
)
from replay.promote_decisions import promote_decisions


_FIXTURE = (
    Path(__file__).parents[1]
    / "evals"
    / "android"
    / "decision_replay"
    / "mobileworld_checkout"
)
_MAIL_FIXTURE = _FIXTURE.parent / "mobileworld_mail_reply"


def test_mobileworld_recorded_decision_suite() -> None:
    result = replay_decision_suite(_FIXTURE, recorded=True)

    assert result["status"] == "passed"
    assert {case["id"] for case in result["cases"]} == {
        "direct_open_messages",
        "return_with_observed_code",
        "enter_code_and_submit_login",
        "select_cart_item_and_checkout",
        "ground_off_box_address_action",
        "replan_picker_drag_after_guard",
    }
    assert all(case["passed_samples"] == 1 for case in result["cases"])


def test_mobileworld_decision_suite_can_run_one_group() -> None:
    result = replay_decision_suite(
        _FIXTURE, recorded=True, group="cross_app_auth",
    )

    assert result["status"] == "passed"
    assert result["group"] == "cross_app_auth"
    assert [case["id"] for case in result["cases"]] == [
        "direct_open_messages", "return_with_observed_code",
    ]


def test_mobileworld_mail_recorded_decision_suite() -> None:
    result = replay_decision_suite(_MAIL_FIXTURE, recorded=True)

    assert result["status"] == "passed"
    assert [case["id"] for case in result["cases"]] == [
        "complete_after_confirmed_commit",
    ]


def test_decision_matcher_allows_safe_split_but_rejects_wrong_target() -> None:
    result = replay_decision_suite(_FIXTURE, recorded=True)
    case_result = next(
        case for case in result["cases"]
        if case["id"] == "select_cart_item_and_checkout"
    )
    expected = next(
        case["expected"] for case in load_decision_manifest(_FIXTURE)["cases"]
        if case["id"] == case_result["id"]
    )
    single_tap = deepcopy(case_result["samples"][0])
    single_tap["action_semantics"] = single_tap["action_semantics"][:1]
    single_tap["action_capabilities"] = single_tap["action_capabilities"][:1]
    single_tap["args"]["actions"] = single_tap["args"]["actions"][:1]

    assert score_decision_sample(single_tap, expected) == []
    single_tap["args"]["actions"][0]["args"]["x"] = 500
    assert any(
        "outside" in error for error in score_decision_sample(single_tap, expected)
    )

    grounded_checkout = deepcopy(case_result["samples"][0])
    grounded_checkout["args"]["actions"][1]["args"]["y"] = 927
    assert score_decision_sample(grounded_checkout, expected) == []
    grounded_checkout["args"]["actions"][1]["args"]["description"] = "点击附近按钮"
    assert any(
        "missing '结算'" in error
        for error in score_decision_sample(grounded_checkout, expected)
    )


def test_promoter_keeps_only_selected_replay_material(tmp_path: Path) -> None:
    destination = tmp_path / "promoted"

    manifest = promote_decisions(_FIXTURE, destination, [14])

    trace = json.loads(
        (destination / "tool_agent_trace.json").read_text(encoding="utf-8")
    )["trace"]
    assert [case["frame"] for case in manifest["cases"]] == [14]
    assert len(trace) == 1
    assert len(trace[0]["context_reports"]) == 1
    assert trace[0]["context_reports"][0]["label"] == "tool_agent.worker"
    assert str(_FIXTURE) not in json.dumps(trace)
    assert (destination / "screenshot_tool_agent_14.png").read_bytes() == (
        _FIXTURE / "screenshot_tool_agent_14.png"
    ).read_bytes()


def test_decision_matcher_accepts_an_explicit_action_alternative() -> None:
    sample = {
        "tool": "continue_with_actions",
        "protocol_repairs": 0,
        "action_semantics": [{"capability": "tap"}],
        "args": {"actions": [{
            "name": "tap",
            "args": {"x": 165, "y": 892, "description": "点击浙江省"},
        }]},
    }
    expected = {
        "tool": "continue_with_actions",
        "required_prefix": [{"one_of": [
            {"capability": "drag"},
            {
                "capability": "tap",
                "target_box": [80, 860, 250, 930],
                "description_contains": ["浙江省"],
            },
        ]}],
    }

    assert score_decision_sample(sample, expected) == []
    sample["args"]["actions"][0]["args"]["y"] = 700
    assert any("outside" in error for error in score_decision_sample(sample, expected))
