# Tool Agent eval gates

The release gate is the focused deterministic suite:

```bash
uv run pytest tests/test_tool_agent_*.py tests/test_runtime_executor_logging.py
```

It covers Master compilation and sandboxing, Worker protocol and memory, grounding, ordered actions, private data references, deterministic transforms, replay, presentation, and HTML report projection. A live run can also be re-executed offline with:

```bash
bin/replay_run logs/gui_agent/tool_agent/<platform>/<run-id> --json
```

`replay_run` is deterministic and does not call a model. `replay_decision` uses
the current configured model, static prompt, and tool schemas with the recorded
task or frozen Worker frame/memory; it does not connect to a device:

```bash
bin/replay_decision <run-dir> --master --samples 3
bin/replay_decision <run-dir> --worker-frame 12 --samples 3
```

Curated multi-frame Worker suites use semantic matchers instead of exact action
sequences, so a safe suffix may move to the next turn without failing the gate:

```bash
bin/replay_decision_suite evals/android/decision_replay/mobileworld_checkout --recorded
uv run --env-file .env bin/replay_decision_suite \
  evals/android/decision_replay/mobileworld_checkout --samples 3
```
