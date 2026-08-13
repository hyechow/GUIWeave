from pathlib import Path

from gui_agent.core.self_learning.app_summary import load_knowledge_for_app


ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "knowledge"
ANDROID_KNOWLEDGE = ROOT / "knowledge" / "android"


def test_android_app_knowledge_is_complete_and_declarative() -> None:
    files = {path.parent.name: path for path in ANDROID_KNOWLEDGE.glob("*/_app.md")}

    assert set(files) == {
        "Calendar", "Chrome", "Clock", "Files", "Mail", "Mastodon", "Mattermost",
        "Messages", "Settings", "Taodian",
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


def test_calendar_knowledge_locks_month_grid_not_readable_fact() -> None:
    """The coffee availability fix: the month grid renders a compact display range
    that is not extractable, so availability reads must come from the DAY/interval
    view. If this knowledge line is ever reverted, the regression gate catches it."""
    knowledge = load_knowledge_for_app("Calendar", "android")
    assert knowledge is not None
    context = knowledge.orchestrator_context(
        "check whether I am available for a coffee meeting this week"
    )

    assert "DAY view" in context
    assert "month grid" in context
    assert "availability" in context
    assert "not from the month grid" in context


def test_taodian_multi_item_delete_uses_group_editor() -> None:
    knowledge = load_knowledge_for_app("Taodian", "android")
    assert knowledge is not None
    context = knowledge.orchestrator_context(
        "delete all matching T-shirts from the shopping cart"
    )

    assert "管理" in context
    assert "multi-select" in context
    assert "删除选中" in context
    assert "contains **any of** `短袖`, `T恤`, or `衬衫`" in context
    assert "per-row mutation" not in context


def test_files_mail_use_physical_folder_and_single_file_picker() -> None:
    files = load_knowledge_for_app("Files", "android")
    mail = load_knowledge_for_app("Mail", "android")
    assert files is not None and mail is not None

    files_context = files.orchestrator_context(
        "move review pdf from Documents to paper and email files"
    )
    mail_context = mail.orchestrator_context(
        "Mail email send compose subject attachment"
    )
    for fact in (
        "indexed category", "matches descendants recursively",
        "before scrolling", "preserve the selection",
    ):
        assert fact in files_context
    for fact in (
        "regardless of type", "`Documents` category can omit types",
        "return after one selection", "verify the attachment in compose",
    ):
        assert fact in mail_context
