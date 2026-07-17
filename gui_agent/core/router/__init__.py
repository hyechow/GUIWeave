"""Router layer — the semantic understanding pass that runs BEFORE orchestration.

The router owns WHAT the user means (intent): it classifies the goal's lookup entities
(precise vs approximate + search key) and bakes that decision into the goal the decomposer
reads. The orchestrator (gui_agent.core.orchestrator) then owns only HOW to achieve it
(the retrieval strategy, navigation, program shape). Keeping these separate means the
orchestrator never re-guesses semantics the router already settled.

Boundaries:
  Intent Resolver  user goal -> IntentResolution + annotated goal (intent.py)
  (future)         chat router (route_message) may consolidate here
"""

from .intent import EntityRef, IntentResolution, resolve_intent

__all__ = ["EntityRef", "IntentResolution", "resolve_intent"]
