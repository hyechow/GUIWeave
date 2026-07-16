"""Android configuration for the unified Statement Transition."""

from gui_agent.core.supervisor.statement.schemas import StatementPrompts


ANDROID_STATEMENT_PROMPTS = StatementPrompts(
    image_resize="none",
    home_identity_markers=(
        "Android 主屏幕",
        "主屏幕",
        "主屏",
        "home screen",
        "launcher",
    ),
)
