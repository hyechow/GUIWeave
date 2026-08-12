"""Application knowledge loading for Tool Agent."""

from gui_agent.core.self_learning.app_summary import (
    AppKnowledge,
    auto_discover_knowledge,
    load_knowledge_for_app,
    match_app_by_url,
)

__all__ = [
    "AppKnowledge",
    "auto_discover_knowledge",
    "load_knowledge_for_app",
    "match_app_by_url",
]
