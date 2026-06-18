"""iPhone intent-router system prompt (moved verbatim out of core chat_session).

The router FRAMEWORK (route_message) is platform-neutral; this prompt is iphone-
flavored ("操控手机" / 「在[APP]中[操作]」) and lives in the iphone adapter, injected
by platform. core.chat_session.route_message selects it by platform (lazy import).
"""

from gui_agent.prompts import load_prompt_text

IPHONE_ROUTER_SYSTEM = load_prompt_text("task.router.iphone")
