from __future__ import annotations

from gui_agent.core import app_router
from gui_agent.core.self_learning import app_summary


def _app(
    root,
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    origin: str = "",
) -> None:
    app_dir = root / "browser" / name
    app_dir.mkdir(parents=True)
    (app_dir / "_app.md").write_text(
        f"# {name}\n\n{name} application navigation.",
        encoding="utf-8",
    )
    (app_dir / "_deploy.md").write_text(
        "---\naliases:\n"
        + "".join(f"  - {alias}\n" for alias in aliases)
        + "---\n"
        f"Entry URL: {origin}\n",
        encoding="utf-8",
    )


def _knowledge_root(tmp_path, monkeypatch):
    monkeypatch.setattr(app_summary, "KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setattr(app_summary, "get_user_knowledge_root", lambda: tmp_path)
    return tmp_path


def test_roboteam_goal_alias_routes_active_knowledge(tmp_path, monkeypatch) -> None:
    root = _knowledge_root(tmp_path, monkeypatch)
    _app(
        root,
        "RoboTeam",
        aliases=("Robo Team", "Robo Team 控制台"),
        origin="http://1.2.3.4:22000/",
    )

    result = app_router.resolve_app_routes(
        "查看RoboTeam的订单列表",
        "browser",
    )

    assert result.app_ids == ("RoboTeam",)
    assert result.targets[0].confidence == "strong"
    assert result.targets[0].evidence == ("goal_alias:RoboTeam",)
    assert result.needs_clarification is False


def test_latin_alias_does_not_match_inside_another_latin_word() -> None:
    result = app_router.resolve_app_routes(
        "Open the Steam dashboard",
        "browser",
        records=(app_router.AppRouteRecord(
            app_id="Team",
            platform="browser",
        ),),
    )

    assert result.app_ids == ()


def test_more_specific_overlapping_alias_wins() -> None:
    result = app_router.resolve_app_routes(
        "Open Google Calendar",
        "browser",
        records=(
            app_router.AppRouteRecord(app_id="Calendar", platform="browser"),
            app_router.AppRouteRecord(
                app_id="GoogleCalendar",
                platform="browser",
                aliases=("Google Calendar",),
            ),
        ),
    )

    assert result.app_ids == ("GoogleCalendar",)


def test_unique_app_name_disambiguates_shared_alias() -> None:
    result = app_router.resolve_app_routes(
        "Open Alpha Operations",
        "browser",
        records=(
            app_router.AppRouteRecord(
                app_id="Alpha",
                platform="browser",
                aliases=("Operations",),
            ),
            app_router.AppRouteRecord(
                app_id="Beta",
                platform="browser",
                aliases=("Operations",),
            ),
        ),
    )

    assert result.app_ids == ("Alpha",)
    assert result.needs_clarification is False


def test_non_overlapping_app_mentions_support_cross_app_goal() -> None:
    result = app_router.resolve_app_routes(
        "Copy the order from Alpha to Beta",
        "browser",
        records=(
            app_router.AppRouteRecord(app_id="Alpha", platform="browser"),
            app_router.AppRouteRecord(app_id="Beta", platform="browser"),
        ),
    )

    assert result.app_ids == ("Alpha", "Beta")


def test_roboteam_current_url_routes_generic_goal_by_unique_local_port(
    tmp_path,
    monkeypatch,
) -> None:
    root = _knowledge_root(tmp_path, monkeypatch)
    _app(root, "RoboTeam", origin="http://1.2.3.4:22000/")

    result = app_router.resolve_app_routes(
        "查看当前页面的订单列表",
        "browser",
        current_url="http://localhost:22000/orders/list",
    )

    assert result.app_ids == ("RoboTeam",)
    assert result.active_app == "RoboTeam"
    assert result.targets[0].active is True
    assert result.targets[0].confidence == "exact"
    assert result.targets[0].evidence == ("current_url:localhost:22000",)


def test_ambiguous_local_port_requires_clarification() -> None:
    result = app_router.resolve_app_routes(
        "查看当前页面",
        "browser",
        current_url="http://localhost:22000/",
        records=(
            app_router.AppRouteRecord(
                app_id="Alpha",
                platform="browser",
                origins=("http://1.2.3.4:22000/",),
            ),
            app_router.AppRouteRecord(
                app_id="Beta",
                platform="browser",
                origins=("http://5.6.7.8:22000/",),
            ),
        ),
    )

    assert result.app_ids == ()
    assert result.needs_clarification is True
    assert "current URL" in result.clarification


def test_named_target_remains_separate_from_current_site(tmp_path, monkeypatch) -> None:
    root = _knowledge_root(tmp_path, monkeypatch)
    _app(root, "RoboTeam", aliases=("Robo Team",), origin="http://one.test/")
    _app(root, "Admin", origin="http://admin.test/")

    result = app_router.resolve_app_routes(
        "打开 Robo Team",
        "browser",
        current_url="http://admin.test/orders",
    )

    assert result.app_ids == ("RoboTeam",)
    assert result.active_app == "Admin"
    assert result.targets[0].active is False
    assert result.targets[0].evidence == ("goal_alias:Robo Team",)


def test_ambiguous_alias_requires_clarification_instead_of_guessing(
    tmp_path,
    monkeypatch,
) -> None:
    root = _knowledge_root(tmp_path, monkeypatch)
    _app(root, "Alpha", aliases=("Operations",))
    _app(root, "Beta", aliases=("Operations",))

    result = app_router.resolve_app_routes("Open Operations", "browser")

    assert result.app_ids == ()
    assert result.needs_clarification is True
    assert "matches ['Alpha', 'Beta']" in result.clarification


def test_platform_identity_can_route_mobile_app_without_goal_mention() -> None:
    result = app_router.resolve_app_routes(
        "查看当前页面",
        "android",
        current_app_id="com.example.calendar",
        records=(app_router.AppRouteRecord(
            app_id="Calendar",
            platform="android",
            platform_ids=("com.example.calendar",),
        ),),
    )

    assert result.app_ids == ("Calendar",)
    assert result.active_app == "Calendar"
    assert result.targets[0].evidence == (
        "platform_id:com.example.calendar",
    )
