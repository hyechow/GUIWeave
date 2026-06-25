"""Android decomposer eval: validates milestone structure from goal + screenshot.

Run:  uv run python evals/android/decomposer/test_decomposer.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.adapters.android.supervisor.milestone.prompts import ANDROID_MILESTONE_PROMPTS
from gui_agent.core.schemas import Observation
from gui_agent.core.supervisor.milestone import MilestoneSupervisorPolicy

CASES_FILE = Path(__file__).parent / "cases.json"

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:64s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _milestone_text(m) -> str:
    return f"{m.name} {m.description} {m.success_condition}"


def _check_basic(milestones: list, task_type: str, expected: dict) -> list[str]:
    details: list[str] = []
    if "task_type" in expected and task_type != expected["task_type"]:
        details.append(f"task_type: expected {expected['task_type']!r}, got {task_type!r}")
    if "min_milestones" in expected and len(milestones) < expected["min_milestones"]:
        details.append(f"expected >={expected['min_milestones']} milestones, got {len(milestones)}")
    return details


def _check_assertions(milestones: list, assertions: list[str]) -> list[str]:
    details: list[str] = []
    for assertion in assertions:
        if assertion == "alarm_time_picker_uses_converge":
            matches = [
                m for m in milestones
                if m.completion_strategy == "repeat_until_satisfied"
                and any(kw in _milestone_text(m) for kw in ("闹钟", "时间", "06:30", "6点30"))
            ]
            if not matches:
                got = [(m.name, m.kind, m.completion_strategy) for m in milestones]
                details.append(f"no alarm time milestone uses repeat_until_satisfied (got: {got})")
        elif assertion == "alarm_create_final_requires_saved_entry":
            final = milestones[-1]
            text = _milestone_text(final)
            has_target_time = "06:30" in text or "6:30" in text or "6点30" in text
            has_saved_list_evidence = any(kw in text for kw in ("列表", "条目", "出现", "返回闹钟", "保存后"))
            has_create_or_active_evidence = any(kw in text for kw in ("保存", "创建", "添加", "开启", "已设定"))
            if not (has_target_time and has_saved_list_evidence and has_create_or_active_evidence):
                details.append(
                    "final milestone must verify saved alarm list entry, got "
                    f"{final.name!r}: {final.success_condition!r}"
                )
            weak_picker_only = (
                any(kw in text for kw in ("滚轮", "picker", "时间显示", "AM标识", "中间高亮"))
                and not has_saved_list_evidence
            )
            if weak_picker_only:
                details.append(f"final milestone is picker-display-only: {final.success_condition!r}")
        elif assertion == "alarm_goal_period_preserved":
            time_targets = [
                m for m in milestones
                if m.kind == "action"
                and any(kw in _milestone_text(m) for kw in ("06:30", "6:30", "6点30"))
            ]
            if not time_targets:
                details.append("no action milestone mentions target time 06:30")
                continue
            missing_period = [
                m.name for m in time_targets
                if not any(kw in _milestone_text(m) for kw in ("上午", "早上", "AM"))
            ]
            if missing_period:
                details.append(f"time target milestones dropped AM/morning constraint: {missing_period}")
            final_text = _milestone_text(milestones[-1])
            if not any(kw in final_text for kw in ("上午", "早上", "AM")):
                details.append("final saved-entry milestone dropped AM/morning constraint")
        elif assertion == "no_api_json_direct_link":
            import re

            _API_RE = re.compile(
                r"https?://api\.|api\.github\.com|/repos/|/contributors\?",
                re.IGNORECASE,
            )
            offenders = [m.name for m in milestones if _API_RE.search(_milestone_text(m))]
            if offenders:
                details.append(
                    f"milestone 走了 API/JSON 直链取数（应走网页/应用界面视觉）: {offenders}"
                )
        else:
            details.append(f"unknown assertion: {assertion}")
    return details


def test_decomposer() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    for c in cases:
        screenshot_path = PROJECT_ROOT / c["screenshot"]
        if not screenshot_path.exists():
            _report(c["label"], False, f"screenshot not found: {c['screenshot']}")
            continue

        observation = Observation(png_bytes=screenshot_path.read_bytes(), source="eval")
        try:
            policy = MilestoneSupervisorPolicy(prompts=ANDROID_MILESTONE_PROMPTS)
            policy._decompose(c["goal"], observation)
            milestones = [policy._milestones[mid] for mid in policy._order]
        except Exception as e:  # noqa: BLE001
            _report(c["label"], False, f"exception: {e}")
            continue

        details = _check_basic(milestones, policy.task_type, c["expected"])
        details.extend(_check_assertions(milestones, c.get("assertions", [])))
        ok = len(details) == 0
        _report(c["label"], ok, "; ".join(details) if details else "")
        if not ok:
            for m in milestones:
                print(
                    "       "
                    f"[{m.kind}/{m.completion_strategy}] {m.name}: {m.success_condition}"
                )


def main() -> int:
    print("── Android Decomposer Eval ──")
    test_decomposer()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
