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
        "RecentTotalExpenseTask",
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


def test_sum_file_lines_contract_accepts_extract_then_read_program() -> None:
    source = '''
def run(ctx):
    ctx.reach("Open Downloads", success={"entity": "DownloadFiles", "fields": ["name", "modified_at"]})
    downloads = ctx.query(entity="DownloadFiles", fields={"name": "text", "modified_at": "datetime"}, coverage="complete")
    archives = [row for row in downloads if row["name"].casefold().endswith(".zip") and row["modified_at"].month == 7]
    assert archives, "Required July archive is missing"
    archives.sort(key=lambda row: row["modified_at"])
    archive = archives[0]
    ctx.reach("Open archive", target=archive, success={"entity": "ArchiveEntries", "name": archive["name"], "fields": ["name"]})
    entries = ctx.query(entity="ArchiveEntries", fields={"name": "text"}, coverage="complete")
    ctx.commit("Extract every entry", target=archive, values={"selection": "all", "destination": "Downloads"})
    ctx.reach("Return to Downloads", success={"entity": "DownloadFiles", "fields": ["name"]})
    extracted = ctx.query(entity="DownloadFiles", fields={"name": "text"}, coverage="complete")
    contents = []
    for entry in entries:
        matches = [row for row in extracted if row["name"] == entry["name"]]
        assert matches, "Extracted entry is missing"
        target = matches[0]
        contents.append(ctx.read(target=target, fields={"content": "text"})["content"])
    return int(sum(len(content.splitlines()) for content in contents))
'''

    assert orchestrator_eval.evaluate_source(
        source,
        _case("SumFileLinesTask")["contract"],
    ) == []


def test_files_knowledge_uses_the_verified_archive_extraction_path() -> None:
    knowledge = (ROOT / "knowledge/android/Files/_app.md").read_text(encoding="utf-8")

    assert "long-press" not in knowledge
    assert "`More options`, use `Select all`" in knowledge
    assert "choose `Extract to…`" in knowledge
    assert "activate `EXTRACT`" in knowledge
