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
    assert "Reopening the same message" in worker_context
    assert "does not confirm or repeat that copy" in worker_context
    assert "open the exact `Downloads` row through `Files`" in worker_context
    assert "destination file already exists" in worker_context
    assert "default download directory is `/sdcard/Download`" in worker_context
    assert "shown as `Downloads` in the file picker" in worker_context
    assert "the default `Recent` view" in worker_context
    assert "open the top-left menu" in worker_context
    assert "device storage → `Downloads`" in worker_context
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
        "directly exposes its own Favorite control", "action row are visibly",
        "alternate mutation", "exact tag title identifies", "show only fixed",
        "separate structured",
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


def test_mastodon_distinguishes_posting_language_from_account_locale() -> None:
    knowledge = load_knowledge_for_app("Mastodon", "android")
    assert knowledge is not None

    context = knowledge.worker_context()
    for required in (
        "top-right settings gear",
        "exact account row",
        "`Posting defaults` → `Posting language`",
        "`Posting language: Chinese`",
        "defaults for new posts",
        "does not establish",
        "account/interface locale",
    ):
        assert required in context

    assert "settings/preferences/appearance" not in context


def test_mastodon_featured_hashtags_are_not_profile_editor_fields() -> None:
    knowledge = load_knowledge_for_app("Mastodon", "android")
    assert knowledge is not None

    context = " ".join(knowledge.worker_context().split())
    for required in (
        "`Featured` tab is a read-only summary",
        "switches to `About`",
        "disables all four",
        "profile content tabs",
        "Label/Value profile metadata",
        "do not create featured hashtags",
        "no control for adding or removing featured hashtags",
    ):
        assert required in context

    assert "use its `Add`/featured-hashtag control" not in context


def test_mastodon_revises_existing_alt_text_through_edit_post() -> None:
    knowledge = load_knowledge_for_app("Mastodon", "android")
    assert knowledge is not None

    context = " ".join(knowledge.worker_context().split())
    for required in (
        "`ALT` badge opens a read-only `Alt text` sheet",
        "top-right three-dot overflow menu",
        "composer title is `Edit post`",
        "small edit control on the attachment card",
        "field contains the existing description",
        "Android Back returns the changed description",
        "no separate save button",
        "top-right submit arrow",
        "large floating pencil",
        "opens `New post`",
    ):
        assert required in context


def test_mastodon_reports_a_post_before_blocking_its_author() -> None:
    knowledge = load_knowledge_for_app("Mastodon", "android")
    assert knowledge is not None

    context = " ".join(knowledge.worker_context().split())
    for required in (
        "post's own three-dot overflow menu",
        "`Report <author>`",
        "scroll inside the open menu",
        "instead of concluding that reporting is unavailable",
        "profile `Timeline` is already that post's own menu",
        "do not dismiss it or open the post detail",
        "`It’s spam`",
        "radio control is selected",
        "already selects that post",
        "`Additional comments` is the report comment field",
        "success screen says the report was sent",
        "`Block @<author>`",
        "`Block user?` confirmation",
        "Do not tap `Done`",
    ):
        assert required in context


def test_mastodon_filters_content_languages_in_web_preferences() -> None:
    knowledge = load_knowledge_for_app("Mastodon", "android")
    assert knowledge is not None

    context = " ".join(knowledge.worker_context().split())
    for required in (
        "Account content-language filtering is a Mastodon Web preference",
        "native `Settings` → account → `Filters` only mutes words",
        "preferring a signed-in profile tab",
        "Chrome's numbered tab-counter are visible",
        "the next action is to tap that tab-counter",
        "never scroll the form or guess a toolbar target",
        "without typing a URL",
        "detail's only three-dot control",
        "`Open in browser`",
        "`Preferences` → `Other`",
        "`Filter languages` is a checkbox list",
        "`English`, `日本語`, and `简体中文`",
        "clear any other checked language",
        "`Diné bizaad`, `eesti`, `Ekakairũ Naoero`, `English`, `Español`",
        "`Afaan Oromoo`/`Afaraf` are at the top",
        "never an upward one",
        "`日本語` and `简体中文` are together near the end",
        "visible glyphs of the exact text",
        "Do not batch adjacent language changes",
        "`Save changes`",
        "`Posting language` on this page is a different default",
    ):
        assert required in context


def test_mastodon_reads_server_size_only_from_owner_admin_dashboard() -> None:
    knowledge = load_knowledge_for_app("Mastodon", "android")
    assert knowledge is not None

    context = " ".join(knowledge.worker_context().split())
    for required in (
        "Native and Web account sessions are independent",
        "switching the native app to `@owner` does not change Chrome's Mastodon login",
        "establish the required account identity independently on native and Web",
        "evidence from one never establishes the other",
        "`TEST (@test)` is an ordinary account",
        "Regular Chrome tabs share one Web login",
        "cannot hold different accounts",
        "do not search or revisit the tab overview for owner",
        "right-rail Settings gear",
        "complete menu with `Logout` but no `Administration` proves",
        "next observable account-switch state is the Mastodon sign-in form",
        "logging out requires no credential",
        "This is not blocked",
        "supplied owner login establishes the `@owner` Web session",
        "makes `Administration` available",
        "exact owner handle is `@owner`",
        "bottom `…` opens the instance `About` page",
        "it is not an account menu and never exposes `Logout`",
        "Even from `About`, use the separate right-rail Settings gear",
        "Once a tab title or URL establishes `@test`",
        "identity applies to every regular Mastodon tab",
        "open `Appearance` rather than revisiting the profile",
        "TEST profile is not an account switcher",
        "reopening it never reaches the sign-in form",
        "`Development` configures API applications",
        "`Administration` → `Dashboard`",
        "`Space usage` card's `PostgreSQL` row",
        "Return via `Back to Mastodon`",
        "only by long-pressing the whole bottom `Profile` tab",
        "chevrons are a visual affordance, not a separate tap target",
        "`OWNER` / `@owner`",
        "do not long-press an account row",
    ):
        assert required in context

    orchestrator = " ".join(knowledge.orchestrator_context(
        "switch to owner, query the database size in the settings backend, and post it"
    ).split())
    assert "establish owner identity separately in both session domains" in orchestrator
    assert "one does not satisfy the other" in orchestrator
    assert "`Administration` → `Dashboard`" in orchestrator
    assert "`Space usage`" in orchestrator
    assert "`PostgreSQL`" in orchestrator


def test_mastodon_configures_automated_deletion_in_one_web_form() -> None:
    knowledge = load_knowledge_for_app("Mastodon", "android")
    assert knowledge is not None

    goal = "enable automated deletion, keep only pinned posts, and set thresholds"
    orchestrator = " ".join(knowledge.orchestrator_context(goal).split())
    worker = " ".join(knowledge.worker_context().split())
    for required in (
        "only in authenticated Mastodon Web settings",
        "`keep_direct`, `keep_pinned`, `keep_self_fav`, `keep_self_bookmark`",
        "`keep_pinned=true`",
        "every other boolean exception listed above is `false`",
        "one `Save changes` commit",
    ):
        assert required in orchestrator
    for required in (
        "not a native Android `Behavior` option",
        "`1 week` is the 7-day age threshold",
        "favorite and boost minimums are separate numeric inputs",
        "returned saved confirmation commits the policy",
    ):
        assert required in worker


def test_mastodon_export_uses_authenticated_web_session_and_files_rename() -> None:
    knowledge = load_knowledge_for_app("Mastodon", "android")
    assert knowledge is not None

    goal = "export my follows in settings and save it as my_following.csv"
    orchestrator = " ".join(knowledge.orchestrator_context(goal).split())
    worker = " ".join(knowledge.worker_context().split())
    for required in (
        "Android client has no account data export control",
        "existing authenticated Mastodon web tab",
        "following_accounts.csv",
        "renaming the downloaded file in Files",
    ):
        assert required in orchestrator
    assert "horizontal three-line button" in worker
    assert "vertical three-dot toolbar button" in worker
    assert "inspect its tab switcher" in worker
    assert "never a lower section of the Appearance form" in worker
