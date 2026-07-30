from gui_agent.core.chat.session import format_session_history


def test_format_session_history_accepts_legacy_result_summary():
    history = [{
        "user_msg": "打开 GitHub",
        "result_summary": "已打开 GitHub 首页",
        "phase": "completed",
        "verification": "confirmed",
    }]

    assert format_session_history(history) == (
        "1. 用户说「打开 GitHub」→ ✓ 已打开 GitHub 首页"
    )
