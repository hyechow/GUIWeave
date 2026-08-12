---
name: guiweave-local-automation
description: Run user-authorized tasks in a local Chrome browser or connected Android device with GUIWeave Tool Agent Master. Use when the user asks Codex to navigate, inspect, test, or operate one of those local GUI surfaces, diagnose GUIWeave setup, or inspect a prior GUIWeave run.
---

# GUIWeave Local Automation

Use the GUIWeave MCP tools to execute a bounded natural-language goal against the user's local browser or Android device. Treat the device and its signed-in sessions as user-controlled resources.

## Run a task

1. Confirm the requested platform and turn the request into one exact goal. Do not broaden the goal or infer unrelated follow-up work.
2. Call `check_environment` before the first run on a platform or when setup may have changed. Report actionable setup failures instead of repeatedly retrying.
3. For Chrome, call `run_browser_task`. For Android, call `run_android_task`. Start with the default perception mode and a conservative turn limit; increase the limit only when the task clearly needs it.
4. Report the terminal phase, verification state, final answer, `run_id`, and artifact paths. If the outcome is incomplete or unclear, call `get_run_result` before proposing another run.

## Safety boundaries

- Require an explicit user request before operating a local signed-in surface.
- Ask for confirmation immediately before consequential actions such as sending, publishing, purchasing, deleting data, changing account or security settings, or submitting an irreversible form unless the user explicitly authorized that exact action in the current request.
- Never copy secrets, authentication material, or unrelated private content from observations or logs into the response.
- Stop when the requested outcome is reached, the environment becomes unavailable, or continuing would require credentials, permissions, or a materially broader goal.
- Prefer visible UI interaction. Do not use benchmark-only shortcuts or privileged app APIs to manufacture task completion.

## Diagnose and recover

- Use the returned report, trace, screenshots, and replay artifacts to explain a failure.
- Retry only when there is a concrete recoverable cause, such as a transient page load or a corrected device connection.
- Keep WebArena and MobileWorld evaluation goals inside their respective harnesses; use the general browser and Android tools for ordinary local tasks.
