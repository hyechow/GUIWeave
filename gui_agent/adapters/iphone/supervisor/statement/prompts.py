"""iPhone configuration for the unified Statement Transition."""

from gui_agent.core.supervisor.statement.schemas import StatementPrompts


IPHONE_STATEMENT_PROMPTS = StatementPrompts(
    image_resize="retina",
    home_identity_markers=(
        "iOS 主屏幕",
        "主屏幕",
        "主屏",
        "home screen",
        "springboard",
    ),
)
