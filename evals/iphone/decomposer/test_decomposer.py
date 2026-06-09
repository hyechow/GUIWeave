"""Decomposer eval: validates milestone structure from goal + screenshot."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent.parent / ".env")

from gui_agent.core.schemas import Observation
from gui_agent.core.self_learning.app_summary import auto_discover_knowledge
from gui_agent.core.supervisor.milestone import MilestoneSupervisorPolicy

CASES_FILE = Path(__file__).parent / "cases.json"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:40s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _check_milestones(milestones: list, constraints: list[str], expected: dict) -> list[str]:
    details = []
    all_text = " ".join(f"{m.name} {m.description}" for m in milestones)
    constraints_text = " ".join(constraints)

    if "min_milestones" in expected and len(milestones) < expected["min_milestones"]:
        details.append(f"expected >={expected['min_milestones']} milestones, got {len(milestones)}")

    for kw in expected.get("any_milestone_contains", []):
        if kw not in all_text:
            details.append(f"no milestone mentions '{kw}'")

    for kw in expected.get("no_milestone_contains", []):
        if kw in all_text:
            details.append(f"unexpected '{kw}' found in milestones")

    for app in expected.get("all_apps_modeled", []):
        if app not in all_text:
            details.append(f"app '{app}' not modeled in any milestone")

    for required_kind in expected.get("any_milestone_kind", []):
        if not any(m.kind == required_kind for m in milestones):
            details.append(f"no milestone with kind='{required_kind}'")

    for forbidden_kind in expected.get("no_milestone_kind", []):
        violators = [m.name for m in milestones if m.kind == forbidden_kind]
        if violators:
            details.append(f"milestone(s) {violators} should not be kind='{forbidden_kind}'")

    for forbidden_strategy in expected.get("no_milestone_strategy", []):
        violators = [m.name for m in milestones if m.completion_strategy == forbidden_strategy]
        if violators:
            details.append(f"milestone(s) {violators} use forbidden strategy '{forbidden_strategy}'")

    for required_strategy in expected.get("any_milestone_strategy", []):
        if not any(m.completion_strategy == required_strategy for m in milestones):
            got = [(m.name, m.completion_strategy) for m in milestones]
            details.append(f"no milestone with completion_strategy='{required_strategy}' (got: {got})")

    for kw in expected.get("constraints_contain", []):
        if kw not in constraints_text:
            details.append(f"global_constraints missing '{kw}' (got: {constraints_text!r})")

    return details


def _check_assertions(milestones: list, assertions: list[str]) -> list[str]:
    """Check named assertions beyond simple keyword matching."""
    details = []
    for assertion in assertions:
        if assertion == "address_before_search":
            address_idx = None
            search_idx = None
            for i, m in enumerate(milestones):
                name_desc = f"{m.name} {m.description}".lower()
                address_terms = ("地址", "配送", "收货", "address", "delivery")
                address_action_terms = (
                    "设置",
                    "修改",
                    "选择",
                    "确认",
                    "切换",
                    "设为",
                    "set",
                    "select",
                    "choose",
                    "confirm",
                    "change",
                )
                search_terms = ("搜索", "查找", "search", "搜")
                if (
                    address_idx is None
                    and any(kw in name_desc for kw in address_terms)
                    and any(kw in name_desc for kw in address_action_terms)
                ):
                    address_idx = i
                if search_idx is None and any(kw in name_desc for kw in search_terms):
                    search_idx = i
            if address_idx is None:
                details.append("address_before_search: no milestone sets/selects address/delivery")
            elif search_idx is None:
                details.append("address_before_search: no milestone mentions search")
            elif address_idx > search_idx:
                details.append(
                    f"address_before_search: address step (#{address_idx} '{milestones[address_idx].name}') "
                    f"comes AFTER search step (#{search_idx} '{milestones[search_idx].name}')"
                )
        elif assertion == "filter_before_loop":
            milestones_by_id = {m.id: m for m in milestones}

            def _deps_closure(mid: str) -> set[str]:
                seen: set[str] = set()
                queue = [mid]
                while queue:
                    cur = queue.pop()
                    if cur in seen:
                        continue
                    seen.add(cur)
                    dep_m = milestones_by_id.get(cur)
                    if dep_m:
                        queue.extend(dep_m.depends_on)
                seen.discard(mid)
                return seen

            loop_milestones = [m for m in milestones if m.completion_strategy == "scroll_until_boundary"]
            if not loop_milestones:
                details.append("filter_before_loop: no scroll_until_boundary milestone found")
            else:
                for lm in loop_milestones:
                    dep_ids = _deps_closure(lm.id)
                    has_filter = any(
                        milestones_by_id.get(dep_id) is not None
                        and milestones_by_id[dep_id].kind == "filter"
                        for dep_id in dep_ids
                    )
                    if not has_filter:
                        details.append(
                            f"filter_before_loop: loop milestone '{lm.name}' "
                            f"has no kind=filter in its dependency chain"
                        )
        elif assertion == "filter_dates_no_april":
            # Filter milestone success_condition must not contain April dates (2026-04)
            # when the correct answer is in May. Catches month-arithmetic vs week-arithmetic bugs.
            filter_milestones = [m for m in milestones if m.kind == "filter"]
            if not filter_milestones:
                details.append("filter_dates_no_april: no kind=filter milestone found")
            else:
                for fm in filter_milestones:
                    text = f"{fm.name} {fm.description} {fm.success_condition}"
                    if "2026-04" in text or "-04-" in text:
                        details.append(
                            f"filter_dates_no_april: filter milestone '{fm.name}' "
                            f"contains April dates (wrong month arithmetic)"
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

        png_bytes = screenshot_path.read_bytes()
        observation = Observation(png_bytes=png_bytes, source="eval")

        try:
            policy = MilestoneSupervisorPolicy()
            knowledge = auto_discover_knowledge(c["goal"])
            if knowledge:
                policy.set_app_knowledge(
                    knowledge.navigation,
                    app_name=knowledge.app_name,
                    elements=knowledge.elements,
                )
            policy._decompose(c["goal"], observation)
            milestones = [policy._milestones[mid] for mid in policy._order]
            constraints = policy._global_constraints
        except Exception as e:
            _report(c["label"], False, f"exception: {e}")
            continue

        details = _check_milestones(milestones, constraints, c["expected"])
        details.extend(_check_assertions(milestones, c.get("assertions", [])))
        ok = len(details) == 0
        _report(c["label"], ok, "; ".join(details) if details else "")
        if not ok:
            print(f"       constraints: {constraints}")
            for m in milestones:
                print(f"       [{m.kind}] {m.name}: {m.success_condition}")


def main():
    print("── Decomposer Eval ──")
    test_decomposer()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
