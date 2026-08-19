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


def test_shopping_query_transform_is_worker_only() -> None:
    knowledge = load_knowledge_for_app("shopping", "browser")
    assert knowledge is not None

    worker = knowledge.worker_context()
    master = knowledge.orchestrator_context(
        "price range of a product in One Stop Market"
    )

    assert "noun-plus-gerund" in worker
    assert "noun-plus-gerund" not in master
    assert "full requested class unchanged" in master


def test_shopping_order_history_knowledge_separates_planning_and_navigation() -> None:
    knowledge = load_knowledge_for_app("shopping", "browser")
    assert knowledge is not None

    worker = knowledge.worker_context()
    master = knowledge.orchestrator_context(
        "amount spent on a product class within an order date interval"
    )
    master_text = " ".join(master.split())

    assert "ten per page" in worker
    assert "first row older than the lower boundary" in worker
    assert "one linked order-detail collection" in master_text
    assert "required stable parent identity" in master_text
    assert "Grand Total" in master_text
    assert "Order Total is the list rendering" in master_text
    assert "sum Order Total directly from the list" in master_text
    assert "not replacement filter values" in master_text


def test_shopping_purchased_option_uses_real_order_detail_fields() -> None:
    knowledge = load_knowledge_for_app("shopping", "browser")
    assert knowledge is not None

    worker = " ".join(knowledge.worker_context().split())
    master = knowledge.orchestrator_context(
        "Get the size of a product I bought during a calendar year"
    )
    master_text = " ".join(master.split())

    assert "selected option labels and values" in worker
    assert "raw Product Name cell" in master_text
    assert "required `order_number` from Order #" in master
    assert "primary_product_contains" in master_text
    assert "sole schema field" in master_text
    assert 'cardinality="one"' in master_text
    assert "width-by-height" in master_text
    assert "join an option label directly to its value" in master_text
    assert "<number> inches" in master_text
    assert "Forest Canvas" in master_text
    assert 'primary_product_contains: "Forest Canvas"' in master_text
    assert "explicit taxonomy equivalence" in master_text
    assert "Do not invent separate Purchase Year" in master_text


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
        "After both sets are complete", "controls on a tag row or TootDetail are not authoritative",
        "exact hashtag row", "0 people are talking", "never proof",
        "open its text body", "exact tag title identifies",
        "Never favorite from timeline action bars", "fixed `reply`",
        "separate structured `Favorite` control",
    ):
        assert fact in context
