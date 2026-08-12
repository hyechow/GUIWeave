"""Application knowledge loading for Tool Agent."""

from gui_agent.core.self_learning.app_summary import (
    AppKnowledge,
    auto_discover_knowledge,
    load_knowledge_for_app,
    match_app_by_url,
)
from gui_agent.core.self_learning.document_ingest import KnowledgeImportService
from gui_agent.core.self_learning.paths import get_user_knowledge_root

__all__ = [
    "AppKnowledge",
    "auto_discover_knowledge",
    "load_knowledge_for_app",
    "match_app_by_url",
    "KnowledgeImportService",
    "get_user_knowledge_root",
]
