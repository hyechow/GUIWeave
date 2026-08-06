"""Android action-feedback-chain eval: repeated off_target must converge to a ref.

Seeded from the MattermostCreateChannelTask run (logs/.../android/20260806_165352)
where the channel-list "+" icon's key is a private glyph, the supervisor blind-estimated
the same wrong point for ~24 turns (server icon / push-alert), every estimate was
rejected as off_target, and the loop never closed. Locks the three-layer fix:

1. affordance projection — a meaningless icon label falls back to the resource id
   (`channel_list_header.plus.button` → "plus") so the supervisor can name it.
2. feedback corrective — ≥2 consecutive off_target on the same control injects a
   "bind via target_ref, stop blind-estimating" constraint.
3. bind snap — once a ref is declared, bind snaps the LLM's estimated point to the
   ref's authoritative center instead of rejecting on a few-px error.

Run:  uv run python evals/android/action_feedback/test_action_feedback.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.run.statement_memory import build_memory_view
from gui_agent.core.schemas import (
    ActionIntent,
    ActionSignal,
    Observation,
    PolicyTurn,
    StatementContract,
    SupervisorStep,
    TargetBinding,
)
from gui_agent.adapters.android.actions import AndroidAction, AndroidActionDecision
from gui_agent.adapters.android.policies import AndroidActionPolicy
from gui_agent.core.supervisor.statement.observation_view import build_observation_view
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy

CASES_FILE = Path(__file__).parent / "cases.json"

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:72s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _contract() -> StatementContract:
    return StatementContract(id="c1", goal="create a channel", success="a channel was created")


def _off_target_turn(index: int, control: str) -> PolicyTurn:
    return PolicyTurn(
        index=index,
        observation_source="android",
        statement_instance_id="i1:c1",
        supervisor=SupervisorStep(
            statement_id="c1",
            summary=f"t{index}",
            action_intent=ActionIntent(
                instruction=f"tap {control}", role="write", family="activate",
                target_control=control,
            ),
        ),
        executed=False,
        action_signal=ActionSignal(
            role="write", execution="not_attempted", target="off_target",
            target_control=control,
            binding=TargetBinding(status="contradicted", source="structural", reason="off point"),
        ),
    )


def test_action_feedback() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    obs_path = PROJECT_ROOT / cases["observation"]
    raw = json.loads(obs_path.read_text(encoding="utf-8"))
    tree = raw.get("semantic_tree") or []
    obs = Observation(png_bytes=b"", source="android", semantic_tree=tree)
    view = build_observation_view(_contract(), obs, [])
    policy = StatementSupervisorPolicy.__new__(StatementSupervisorPolicy)

    # 1. The meaningless icon label must fall back to the resource id.
    plus_ref = cases["plus_ref"]
    plus = next((a for a in view.affordances if a.get("ref") == plus_ref), None)
    ok = plus is not None and plus.get("label") == cases["plus_label"]
    _report("affordance icon label falls back to resource id", ok,
            f"label={plus and plus.get('label')!r}")

    # 2. Repeated off_target on the same control injects a use-ref corrective.
    controls = cases["off_target_controls"]
    history = [_off_target_turn(i + 1, c) for i, c in enumerate(controls)]
    memory = build_memory_view(
        instance_id="i1:c1", contract=_contract(), history=history, observation=obs,
    )
    feedback = policy._grounding_ref_feedback(memory, view)
    ok = "target_ref" in feedback and plus_ref in feedback
    _report("repeated off_target injects use-target_ref corrective", ok,
            f"feedback={'yes' if ok else 'no'}")

    # 3. Once a ref is declared, bind snaps a few-px estimate error to the ref center.
    node = next(n for n in tree if n.get("ref") == plus_ref)
    ex, ey = node["point"]["x"], node["point"]["y"]
    step = SupervisorStep(
        summary="target frame",
        action_intent=ActionIntent(
            instruction="tap the + button", role="write", family="activate",
            target_control="plus", target_ref=plus_ref,
        ),
    )
    small = cases["small_offset"]
    decision = AndroidActionDecision(action=AndroidAction(
        action_type="tap", x=ex + small["x"], y=ey + small["y"], description="tap"))
    binding = AndroidActionPolicy().bind(step, obs, decision)
    ok = (
        binding is not None and binding.status == "bound"
        and decision.action.x == ex and decision.action.y == ey
    )
    _report("declared ref snaps small estimate error to ref center", ok,
            f"status={binding and binding.status} point=({decision.action.x},{decision.action.y})")

    # 3b. A grossly wrong point (e.g. the push-alert icon the run mistook for "+")
    #     stays contradicted — snap must not rescue a different control.
    wrong = cases["wrong_icon_point"]
    decision = AndroidActionDecision(action=AndroidAction(
        action_type="tap", x=wrong["x"], y=wrong["y"], description="tap"))
    binding = AndroidActionPolicy().bind(step, obs, decision)
    ok = binding is not None and binding.status == "contradicted"
    _report("grossly-wrong point stays contradicted (no false rescue)", ok,
            f"status={binding and binding.status}")

    # 4. End-to-end: a single blind estimate never triggers the corrective (the
    #    failure is repetition, not one miss).
    memory_one = build_memory_view(
        instance_id="i1:c1", contract=_contract(),
        history=[_off_target_turn(1, controls[0])], observation=obs,
    )
    ok = policy._grounding_ref_feedback(memory_one, view) == ""
    _report("single off_target does not force ref (one miss is acceptable)", ok, "")


def main() -> int:
    print("── Android Action Feedback Chain Eval ──")
    test_action_feedback()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
