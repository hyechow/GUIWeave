---
id: task.webarena.synthesize_human
source_type: task_template
platform: browser
scope:
  - webarena
owner: gui_agent.adapters.browser.webarena
eval_suites:
rendered: true
version: 1
---
Task intent (includes the required output format):
{intent}

Agent task_type guess: {task_type_guess}
Goal completed: {goal_completed}
Stop reason: {stop_reason}
Run summary: {result_summary}

Collected notes:
{notes_text}

Auxiliary run evidence:
{evidence_text}

Produce the agent_response JSON.