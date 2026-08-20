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


def test_mattermost_knowledge_distinguishes_replies_from_channel_posts() -> None:
    knowledge = load_knowledge_for_app("Mattermost", "android")
    assert knowledge is not None

    context = knowledge.worker_context()
    for fact in (
        "long-press the visible message body",
        "composer is a new message, not a reply",
        "task user represented by first-person",
        "credentials, not the task user's identity",
    ):
        assert fact in context
    assert "Sam is the task user" not in context


def test_calendar_knowledge_scopes_title_deduplication_and_search() -> None:
    knowledge = load_knowledge_for_app("Calendar", "android")
    assert knowledge is not None

    context = knowledge.worker_context()
    assert "equal visible titles count once" in context
    assert "collect `title` and visible `date` from the MONTH grid" in context
    assert "do not enter DAY view or scroll beyond the range" in context
    assert "date-number glyph" in context
    assert "Use SEARCH only when the task supplies a literal title predicate" in context


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
        "Mail email send compose subject body attachment"
    )
    for fact in (
        "indexed category", "matches descendants recursively",
        "before scrolling", "preserve the selection",
    ):
        assert fact in files_context
    for fact in (
        "regardless of type", "`Documents` category can omit types",
        "return after one selection", "verify the attachment in compose",
        "message **body**", "must visibly contain that content",
    ):
        assert fact in mail_context
    assert "message body" in mail.worker_context()
    assert "fill the body with that content before Send" in mail.worker_context()
    worker_context = " ".join(mail.worker_context().split())
    assert "copies the file into Android `Downloads`" in worker_context
    assert "matching Runtime invocation receipt" in worker_context
    assert "do not invoke the same control again" in worker_context
    assert "open the exact `Downloads` row through `Files`" in worker_context
    assert "destination file already exists" in worker_context
    assert "only selection that attaches that file" in worker_context
    assert "Android Back cancels" in worker_context
    assert "never press Back before selecting" in worker_context
    assert "does not interpret advanced date operators" in worker_context
    assert "distinctive content terms for recall" in worker_context
    assert "`All mail` source" in worker_context
    assert "no combined subject-or-attachment identity field" in worker_context


def test_files_archive_content_exposes_only_application_facts() -> None:
    knowledge = load_knowledge_for_app("Files", "android")
    assert knowledge is not None

    context = knowledge.orchestrator_context(
        "earliest zip from July extract contents and count lines"
    )

    for fact in (
        "Downloads top-bar search", "not a tap target",
        "Entry names identify the same-named", "leaves the archive",
        "return to Downloads", "not scoped to the latest extraction",
        "may also contain unrelated prior files", "identity link", "detail field",
    ):
        assert fact in context


def test_mastodon_saved_views_require_global_profile_navigation() -> None:
    knowledge = load_knowledge_for_app("Mastodon", "android")
    assert knowledge is not None

    context = knowledge.orchestrator_context(
        "favorite dogs except existing favorites and bookmarks"
    )

    for fact in (
        "Profile → Saved → Favorites", "selected `Saved` tab", "profile strip",
        "use visible Back one", "until the global bar returns",
        "independent state dimensions", "task-stated predicate",
        "filled blue star with a count", "outline star means it is not",
        "exact hashtag row", "0 people are talking", "never proof",
        "open its text body", "exact tag title identifies",
        "Never favorite from timeline action bars", "fixed `reply`",
        "separate structured `Favorite` control",
    ):
        assert fact in context


def test_mastodon_knowledge_does_not_add_conditional_exclusions() -> None:
    knowledge = load_knowledge_for_app("Mastodon", "android")
    assert knowledge is not None

    contexts = (
        knowledge.orchestrator_context("favorite all toots tagged #dogs"),
        knowledge.worker_context(),
    )
    for context in contexts:
        assert "independent state dimensions" in context
        assert "absent from both saved sets" not in context
        assert "determine exclusion only" not in context
