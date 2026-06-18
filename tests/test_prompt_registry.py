"""Prompt registry invariants for Markdown-backed prompt assets."""

from __future__ import annotations

import ast
from pathlib import Path

from gui_agent.prompts import iter_prompt_templates, load_prompt_text

ROOT = Path(__file__).resolve().parents[1]


def test_prompt_registry_loads_all_assets():
    prompts = iter_prompt_templates()
    assert prompts
    ids = [p.id for p in prompts]
    assert len(ids) == len(set(ids))
    for prompt in prompts:
        assert prompt.path.exists()
        assert prompt.source_type
        assert prompt.platform
        assert prompt.scope
        assert prompt.owner
        assert prompt.body.strip()


def test_prompt_eval_suites_point_to_existing_paths():
    for prompt in iter_prompt_templates():
        for suite in prompt.eval_suites:
            assert (ROOT / suite).exists(), f"{prompt.id} references missing suite {suite}"


def test_migrated_prompt_constants_load_from_registry():
    from gui_agent.adapters.browser.router_prompt import BROWSER_ROUTER_SYSTEM
    from gui_agent.adapters.browser.supervisor.milestone.prompts import PLAN_PROMPT
    from gui_agent.adapters.iphone.policies.structured_output import SYSTEM_PROMPT
    from gui_agent.core.llm.reader import SYSTEM_PROMPT as READER_PROMPT

    assert PLAN_PROMPT == load_prompt_text("task.milestone.browser.planner")
    assert SYSTEM_PROMPT == load_prompt_text("task.action_policy.iphone")
    assert BROWSER_ROUTER_SYSTEM == load_prompt_text("task.router.browser")
    assert READER_PROMPT == load_prompt_text("task.reader.screenshot_text")


def test_migrated_modules_do_not_inline_large_prompt_strings():
    migrated = [
        "gui_agent/adapters/iphone/supervisor/milestone/prompts.py",
        "gui_agent/adapters/browser/supervisor/milestone/prompts.py",
        "gui_agent/adapters/android/supervisor/milestone/prompts.py",
        "gui_agent/adapters/iphone/policies/structured_output.py",
        "gui_agent/adapters/browser/policies.py",
        "gui_agent/adapters/android/policies.py",
        "gui_agent/adapters/iphone/router_prompt.py",
        "gui_agent/adapters/browser/router_prompt.py",
        "gui_agent/core/llm/output.py",
        "gui_agent/core/llm/reader.py",
        "gui_agent/core/orchestrator/decomposer.py",
        "gui_agent/core/orchestrator/structured_read.py",
        "gui_agent/core/supervisor/milestone/helpers.py",
        "gui_agent/core/vision/target_verify.py",
    ]
    for rel in migrated:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
                continue
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(name.endswith(("PROMPT", "SYSTEM", "RULE")) for name in names):
                assert len(node.value.value) < 200, f"{rel}:{names} still inlines a large prompt"
