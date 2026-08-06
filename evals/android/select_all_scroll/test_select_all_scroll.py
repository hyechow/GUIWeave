"""Android select-all scroll gate eval: a 'select all rows' commit must not
dispatch while the scrollable member list may still have members below the fold.

Seeded from the passing MattermostCreateChannelTask run (logs/.../20260806_201541).
The supervisor selected the 8 visible members and decided to commit (atomic_role=
commit, turn 19) without scrolling; the fix makes the gate fire at the commit
DECISION (not the later statement completion) and compare the member-row set
across scrolls — force a scroll while new rows appear, allow the commit once the
current slice is within the rows already seen (boundary).

Run:  uv run python evals/android/select_all_scroll/test_select_all_scroll.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.run.statement_runtime import StatementRuntimeState
from gui_agent.core.schemas import Observation, StatementContract
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy

FIXTURES = Path(__file__).parent / "fixtures"
RUN_DIR = PROJECT_ROOT / "logs/gui_agent/mobileworld/android/20260806_201541"

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    print(f"  [{tag}] {label:72s} {detail}")


def _select_all_contract() -> StatementContract:
    return StatementContract(
        id="add",
        goal="Add all members and post welcome message",
        success="all members added and message posted",
        persistence="explicit_commit",
        required_values={"add_all_members": True, "message": "Welcome everyone to the reading channel!"},
    )


def _load_obs(turn: int) -> Observation:
    raw = json.loads((FIXTURES / f"turn_{turn}.json").read_text(encoding="utf-8"))
    return Observation(png_bytes=b"", source="android", semantic_tree=raw["semantic_tree"])


def test_select_all_scroll() -> None:
    statement = _select_all_contract()

    # --- Turn 19: the commit frame. The 8 visible members are selected and the
    # supervisor is about to click 'Add Members'. The scrollable list is not yet
    # exhausted -> the gate MUST force a scroll instead of the commit.
    policy = StatementSupervisorPolicy()
    policy._statement_rt = StatementRuntimeState(contract=statement, instance_id="i1:add")
    obs19 = _load_obs(19)
    step = policy._select_all_scroll_step(statement, obs19, execution_scope="s")
    ok = step is not None and step.action_intent is not None and step.action_intent.family == "iterate"
    _report("turn-19 commit frame forces a scroll (not the commit)", ok,
            f"family={step and step.action_intent and step.action_intent.family}")

    # --- Boundary: once the current slice is entirely within the rows already
    # seen, the gate allows the commit to proceed.
    # Simulate the seen-set being full (all members have appeared across scrolls).
    step = policy._select_all_scroll_step(statement, obs19, execution_scope="s")
    # obs19 is now fully in `seen` -> second evaluation on the same slice is the boundary.
    ok = step is None
    _report("repeating the same slice is the boundary (allows commit)", ok, "")

    # --- Turn 11: dialog open with the member list. The gate's mechanism sees a
    # scrollable list not yet exhausted -> on a commit decision it would scroll.
    policy2 = StatementSupervisorPolicy()
    policy2._statement_rt = StatementRuntimeState(contract=statement, instance_id="i1:add2")
    obs11 = _load_obs(11)
    step = policy2._select_all_scroll_step(statement, obs11, execution_scope="s")
    ok = step is not None and step.action_intent is not None and step.action_intent.family == "iterate"
    _report("dialog-open frame also needs traversal before any commit", ok,
            f"family={step and step.action_intent and step.action_intent.family}")

    # --- A non-select-all commit never triggers the gate.
    policy3 = StatementSupervisorPolicy()
    policy3._statement_rt = StatementRuntimeState(
        contract=StatementContract(
            id="move", goal="move file", success="moved",
            persistence="explicit_commit", required_values={"destination_folder": "x"},
        ),
        instance_id="i1:move",
    )
    step = policy3._select_all_scroll_step(
        StatementContract(id="move", goal="move file", success="moved",
                          persistence="explicit_commit", required_values={"destination_folder": "x"}),
        obs19, execution_scope="s",
    )
    ok = step is None
    _report("non-select-all commit never forces a scroll", ok, "")

    # --- Full decision-level replay of the real turn 19: the production flow now
    # turns the commit decision into an iterate (scroll) step. This is a bonus
    # end-to-end check; it skips when the (gitignored) run logs have been cleaned.
    try:
        if not RUN_DIR.exists():
            _report("real turn-19 replay decides iterate (not commit)", True, "skipped (logs cleaned)")
        else:
            import subprocess
            out = subprocess.run(
                [sys.executable, "-m", "replay.run", str(RUN_DIR), "--turn", "19"],
                capture_output=True, text=True, timeout=120,
            ).stdout
            ok = ('"atomic_role": "iterate"' in out) and ("向下滚动成员列表" in out)
            _report("real turn-19 replay decides iterate (not commit)", ok, "")
    except Exception as exc:  # noqa: BLE001
        _report("real turn-19 replay decides iterate (not commit)", False, f"replay error: {exc}")


def main() -> int:
    print("── Android Select-All Scroll Gate Eval ──")
    test_select_all_scroll()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
