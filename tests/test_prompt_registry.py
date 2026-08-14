"""Prompt registry invariants for the Tool Agent release surface."""

from __future__ import annotations

import ast
import string
from pathlib import Path

import pytest

from gui_agent.prompts import iter_prompt_templates, load_prompt, load_prompt_text
from gui_agent.prompts.loader import PromptRegistryError

ROOT = Path(__file__).resolve().parents[1]


def test_prompt_registry_loads_all_assets() -> None:
    prompts = iter_prompt_templates()
    assert prompts
    assert len({prompt.id for prompt in prompts}) == len(prompts)
    assert {prompt.id for prompt in prompts} == {
        "task.chat.router",
        "task.tool_agent.master",
        "task.tool_agent.master_redelegate",
        "task.tool_agent.presentation",
        "task.tool_agent.visual_transcription",
        "task.tool_agent.worker",
        "task.knowledge.document_ingest",
        "task.vision.loading",
        "task.vision.target_verify",
        "task.webarena.synthesize_human",
        "task.webarena.synthesize_system",
    }
    for prompt in prompts:
        assert prompt.path.exists()
        assert prompt.source_type
        assert prompt.platform
        assert prompt.scope
        assert prompt.owner
        assert prompt.body.strip()


def test_rendered_flag_governs_brace_scanning() -> None:
    for prompt in iter_prompt_templates():
        if not prompt.rendered:
            assert prompt.placeholders == ()
            continue
        fields = [
            name
            for _, name, _, _ in string.Formatter().parse(prompt.body)
            if name is not None
        ]
        assert "" not in fields
        assert prompt.placeholders


def test_render_rejects_raw_prompts() -> None:
    with pytest.raises(PromptRegistryError):
        load_prompt("task.tool_agent.master").render(anything="x")


def test_master_exposes_only_tool_agent_runtime_api() -> None:
    prompt = load_prompt_text("task.tool_agent.master")
    for method in (
        "ctx.gui_worker",
        "ctx.transform",
        "ctx.worker_result",
        "ctx.finish",
        "ctx.fail",
    ):
        assert method in prompt
    for retired in ("ctx.reach", "ctx.query", "ctx.read", "ctx.commit"):
        assert retired not in prompt
    assert "Keep each collector schema minimal" in prompt
    assert "merely useful supplemental metrics" in prompt
    assert "never require perception to invent a missing year" in prompt
    assert "When success criteria guarantee exactly one target record" in prompt
    assert "Source layout order is not a data contract" in prompt
    assert "navigation, not data retrieval" in prompt


def test_visual_transcription_uses_runtime_time_without_inventing_dates() -> None:
    prompt = load_prompt_text("task.tool_agent.visual_transcription")

    assert "provenance-bearing platform clock" in prompt
    assert "explicitly relative visible labels" in prompt
    assert "omit it instead of returning null" in prompt


def test_presentation_requires_user_facing_prose() -> None:
    prompt = load_prompt_text("task.tool_agent.presentation")

    assert "user-facing prose" in prompt
    assert "not serialized JSON" in prompt
    assert "compact Markdown table" in prompt
    assert "status values" in prompt
    assert "Keep every row and column" in prompt


def test_worker_keeps_data_private_and_coordinates_normalized() -> None:
    prompt = load_prompt_text("task.tool_agent.worker")
    assert "Raw data values are private runtime data" in prompt
    assert "Coordinates are normalized 0..999" in prompt
    assert "request_action_patch" in prompt


def test_worker_handles_exhausted_candidate_sets_and_row_targets() -> None:
    prompt = load_prompt_text("task.tool_agent.worker")

    assert "an exhausted candidate set is direct" in prompt
    assert "the same unfiltered selector" in prompt
    assert "latest selected batch's commit produced a confirmed transition" in prompt
    assert "An initially empty selector, a filtered zero-result view" in prompt
    assert "describe the row/button itself" in prompt
    assert "adjacent child icon or decoration" in prompt


def test_large_inline_prompt_constants_are_not_added() -> None:
    suffixes = ("PROMPT", "SYSTEM", "RULE")
    violations = []
    for path in (ROOT / "gui_agent").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Constant) or not isinstance(
                node.value.value, str
            ):
                continue
            names = [
                target.id
                for target in node.targets
                if isinstance(target, ast.Name) and target.id.endswith(suffixes)
            ]
            if names and len(node.value.value) >= 200:
                violations.extend((str(path.relative_to(ROOT)), name) for name in names)
    assert violations == []
