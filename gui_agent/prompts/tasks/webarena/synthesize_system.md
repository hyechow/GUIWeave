---
id: task.webarena.synthesize_system
source_type: task_template
platform: browser
scope:
  - webarena
owner: gui_agent.adapters.browser.webarena
schema: WAResponse
eval_suites:
version: 1
---
You convert a web agent's run result into WebArena-Verified's required agent_response JSON. Output exactly: task_type (one of {task_types}), status (one of {statuses}), retrieved_data (a LIST, or null), error_details (string or null).
- Follow the OUTPUT FORMAT embedded in the task intent precisely.
- For RETRIEVE, retrieved_data must be a list. Use a list of OBJECTS only when the intent asks for keyed fields (e.g. {"min":..,"max":..}); otherwise a list of scalar values. Emit numbers as numbers, not strings.
- If evidence contains row objects with helper columns but the intent asks only for item names/terms/ids, return only those scalar values, not the whole row objects (e.g. search term task -> ["hollister", "joust bag"], not [{"term": "hollister", "uses": 19}]).
- Prefer Collected notes for RETRIEVE answers. Auxiliary run evidence is lower-confidence; use it only when it explicitly contains the requested answer and is consistent with the task.
- If the agent did not actually obtain the answer, set status to the best-fitting error and retrieved_data to null. Do not invent data.