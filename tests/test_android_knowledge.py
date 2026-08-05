from pathlib import Path

from gui_agent.core.self_learning.app_summary import load_knowledge_for_app


ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "knowledge"
ANDROID_KNOWLEDGE = ROOT / "knowledge" / "android"


def test_android_app_knowledge_is_complete_and_declarative() -> None:
    files = {path.parent.name: path for path in ANDROID_KNOWLEDGE.glob("*/_app.md")}

    assert set(files) == {
        "Calendar", "Chrome", "Clock", "Files", "Mastodon", "Messages", "Settings", "Taodian",
    }
    forbidden = (
        "ctx.", "success=", "values=", "fields=", "filters=", "coverage=",
        "atomic_role=", "rows[", "in Python", "Planning boundary",
    )
    for path in files.values():
        text = path.read_text(encoding="utf-8")
        assert not [token for token in forbidden if token in text], path


def test_knowledge_has_no_planner_owned_markdown_sections() -> None:
    forbidden = ("Planning boundary", "planning-boundary")

    for path in KNOWLEDGE.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        assert not [token for token in forbidden if token in text], path


def test_android_orchestrator_selects_compact_interface_documents() -> None:
    cases = {
        "Calendar": ("check the calendar time slot", "Event"),
        "Files": ("earliest zip from July in Downloads", "DownloadFiles"),
        "Mastodon": ("favorite all toots tagged #dogs", "TaggedToots"),
        "Taodian": ("three most expensive shopping cart items", "ShoppingCart"),
    }

    for app, (goal, expected) in cases.items():
        knowledge = load_knowledge_for_app(app, "android")
        assert knowledge is not None
        context = knowledge.orchestrator_context(goal)
        assert expected in context
        assert "[context:" not in context


def test_calendar_knowledge_declares_typed_interval_fields() -> None:
    knowledge = load_knowledge_for_app("Calendar", "android")
    assert knowledge is not None
    context = knowledge.orchestrator_context("check the calendar time slot")

    assert "start_ts" in context
    assert "end_ts" in context
    assert "complete event set" in context
    assert "overlap" in context
