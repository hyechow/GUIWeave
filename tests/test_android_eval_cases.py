from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR_EVAL = ROOT / "evals/android/orchestrator/test_orchestrator.py"
SPEC = importlib.util.spec_from_file_location("android_orchestrator_eval", ORCHESTRATOR_EVAL)
assert SPEC is not None and SPEC.loader is not None
orchestrator_eval = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(orchestrator_eval)


def _case(task_name: str) -> dict[str, Any]:
    return next(
        case for case in orchestrator_eval.load_cases()
        if case["task_name"] == task_name
    )


def test_android_orchestrator_eval_covers_current_gui_baseline() -> None:
    assert {case["task_name"] for case in orchestrator_eval.load_cases()} == {
        "CloseFlightModeTask",
        "AdjustBrightnessMaximumTask",
        "SetAlarmTask",
        "ChromeSearchBeijingWeatherTask",
        "CheckCartPriceTask",
        "SumFileLinesTask",
        "MastodonConditionalFavoTask",
    }


def test_conditional_collection_contract_accepts_pair_difference_program() -> None:
    source = '''
def run(ctx):
    ctx.reach("Open #dogs", success={"entity": "TaggedToots", "tag": "#dogs"})
    tagged = ctx.query(entity="TaggedToots", fields=["author_handle", "content"], filters={"tag": "#dogs"})
    ctx.reach("Open favorites", success={"entity": "SavedFavorites", "active_view": "Favorites"})
    favorites = {(row["author_handle"], row["content"]) for row in ctx.query(entity="SavedFavorites", fields=["author_handle", "content"])}
    ctx.reach("Open bookmarks", success={"entity": "SavedBookmarks", "active_view": "Bookmarks"})
    bookmarks = {(row["author_handle"], row["content"]) for row in ctx.query(entity="SavedBookmarks", fields=["author_handle", "content"])}
    ctx.reach("Return to #dogs", success={"entity": "TaggedToots", "tag": "#dogs"})
    for row in tagged:
        pair = (row["author_handle"], row["content"])
        if pair not in favorites and pair not in bookmarks:
            ctx.reach(
                "Open the exact toot",
                target=row,
                success={
                    "entity": "TootDetail",
                    "tag": "#dogs",
                    "author_handle": row["author_handle"],
                    "content": row["content"],
                },
            )
            ctx.commit("Favorite toot", target=row, values={"favorited": True})
'''

    assert orchestrator_eval.evaluate_source(
        source,
        _case("MastodonConditionalFavoTask")["contract"],
    ) == []


def test_conditional_collection_contract_rejects_unsupported_status_filter() -> None:
    source = '''
def run(ctx):
    ctx.reach(
        "Open #dogs", success={"entity": "TaggedToots", "tag": "#dogs"}
    )
    return ctx.query(
        entity="TaggedToots",
        fields=["author_handle", "content"],
        filters={"tag": "#dogs", "favorited": False},
    )
'''

    failures = orchestrator_eval.evaluate_source(
        source,
        _case("MastodonConditionalFavoTask")["contract"],
    )

    assert any("ORDERED_CALL:1" in failure for failure in failures)
