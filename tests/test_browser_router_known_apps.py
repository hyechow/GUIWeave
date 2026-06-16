from gui_agent.core.chat.session import _mentioned_known_apps


def test_known_apps_do_not_resolve_vague_references():
    assert _mentioned_known_apps(
        "How many reviews did our shop receive in Apr 2023?",
        session=[],
        prefs_context="",
        known_apps=["RoboTeam"],
    ) == []


def test_known_apps_match_explicit_input_history_or_prefs():
    known = ["RoboTeam", "GitHub"]
    assert _mentioned_known_apps(
        "登录 RoboTeam 做单车实验",
        session=[],
        prefs_context="",
        known_apps=known,
    ) == ["RoboTeam"]
    assert _mentioned_known_apps(
        "进入 settings",
        session=[{"user_msg": "打开 GitHub", "result_summary": "已打开 GitHub 首页"}],
        prefs_context="",
        known_apps=known,
    ) == ["GitHub"]
    assert _mentioned_known_apps(
        "How many reviews did our shop receive in Apr 2023?",
        session=[],
        prefs_context="用户偏好：our shop→RoboTeam",
        known_apps=known,
    ) == ["RoboTeam"]
