---
id: task.orchestrator.android
source_type: task_template
platform: android
scope:
  - orchestrator
owner: gui_agent.adapters.android.mobileworld
schema: restricted_python
eval_suites:
rendered: true
version: 2
---
## Android platform capabilities

Available application names:
{app_list}

- `ctx.command("launch_app", app=<application name>)` is the documented deterministic
  capability for bringing a different Android application to the foreground.
- Use it when the task's dataflow moves from one explicitly identified application
  to another. The `app` value must exactly match one available application name;
  never invent, translate, normalize, or alias it, and never use an Android package
  or launcher component in the program.
- `launch_app` only changes the foreground application. It does not create a durable
  business record or establish a queryable UI state.
