"""Router layer — the semantic understanding pass that runs BEFORE orchestration.

The router owns one semantic delta: it supplements implicit or ambiguous meaning without rewriting
the original task. The orchestrator then owns HOW to achieve the unchanged task.

Boundaries:
  Intent Resolver  original goal -> optional semantic supplement (intent.py)
  (future)         chat router (route_message) may consolidate here
"""

from .intent import IntentResolution, resolve_intent

__all__ = ["IntentResolution", "resolve_intent"]
