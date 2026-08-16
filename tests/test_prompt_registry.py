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
    assert "A collector only acquires raw source records" in prompt
    assert "A collector may perform prerequisite navigation or mutation" in prompt
    assert "one invocation of one capability against one control" in prompt
    assert "Never guess enum/status labels" in prompt
    assert "never require perception to invent a missing year" in prompt
    assert "When success criteria guarantee exactly one target record" in prompt
    assert "Source layout order is not a data contract" in prompt
    assert "navigation, not data retrieval" in prompt
    assert "use a scalar JSON Schema" in prompt
    assert "does not convert a differently displayed source value" in prompt
    assert "Current, first, or visually prominent values are not extrema" in prompt
    assert 'Set `cardinality="one"` only' in prompt
    assert "the first visible candidate is not" in prompt


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

    assert "A successful mutation does not prove that the UI navigated" in prompt
    assert "Runtime data-reference values are private" in prompt
    assert "Values visibly read during this Worker's own cohesive GUI branch" in prompt
    assert "Never guess an authentication secret" in prompt
    assert "transient verification code on its delivery surface" in prompt
    assert "visible required acknowledgement" in prompt
    assert "select all matching records before committing" in prompt
    assert "goal and success criteria fully bound this attempt" in prompt
    assert "call `complete` immediately" in prompt
    assert "Coordinates are normalized 0..999" in prompt
    assert "request_action_patch" in prompt
    assert "Placeholder labels on collection rows never override" in prompt
    assert "`state.status = completed | failed` is terminal" in prompt


def test_master_keeps_visual_conditional_dependencies_in_one_worker() -> None:
    prompt = load_prompt_text("task.tool_agent.master")

    for rule in (
        "drive a later conditional GUI mutation", "including across application switches",
        "ResultRefs cannot serve as hidden Worker memory", "including by nesting it in an object",
        "authorized authentication method", "exact acceptance set", "never prescribe per-record commits",
        "traverses every prerequisite collection", "compares each candidate", "mutates only nonmatches",
        "candidate-local evidence", "complete—not merely visible—candidate traversal",
        "excluded/already-processed identities", "never emit an empty list", "intermediate identities",
        "UI transitions/mutations", "clean environment", "Action `description` is static metadata",
        "such as `type.text`", "finishing with `effect=\"data\"`",
        "neither typed data nor a transferable scope",
    ):
        assert rule in prompt


def test_worker_handles_exhausted_candidate_sets_and_row_targets() -> None:
    prompt = load_prompt_text("task.tool_agent.worker")

    for rule in (
        "an exhausted candidate set is direct", "the same unfiltered selector",
        "latest selected batch's commit produced a confirmed transition",
        "candidate_set_state.status = exhausted", "An initially empty selector, a filtered zero-result view",
        "describe the row/button itself", "adjacent child icon or decoration", "named action does not prove",
        "never relabel another visible control", "retain processed identities",
        "explicitly remaining candidate", "durable completion fact means processed", "without reopening",
        "comparison evidence", "implement `state.next_instruction`",
        "never an internal step such as compare/evaluate/determine", "excluded match permits traversal",
        "complete application-declared identity", "repeated, prefixed, ellipsized, or partial",
        "complete identity and confirmed effect", "this item/record", "visible_collection_regions",
        "not record boundaries", "skip exact excluded matches", "viewport_tail_clipped = true",
        "repeated identity alone is insufficient", "Stable page chrome", "selector scrolls offscreen",
        "unobscured central viewport", "opening tap returns `no_effect`",
        "exclusive `required_interactions`", "perception-owned physical prerequisite",
    ):
        assert rule in prompt


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
