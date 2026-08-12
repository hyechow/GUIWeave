# Tool Agent eval gates

The release gate is the focused deterministic suite:

```bash
uv run pytest tests/test_tool_agent_*.py tests/test_runtime_executor_logging.py
```

It covers Master compilation and sandboxing, Worker protocol and memory, grounding, ordered actions, private data references, deterministic transforms, replay, presentation, and HTML report projection. A live run can also be re-executed offline with:

```bash
bin/replay_run logs/gui_agent/tool_agent/<platform>/<run-id> --json
```
