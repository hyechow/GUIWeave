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
        "task.tool_agent.presentation",
        "task.tool_agent.strategy_decide",
        "task.tool_agent.visual_transcription",
        "task.tool_agent.worker",
        "task.knowledge.document_ingest",
        "task.vision.loading",
        "task.vision.target_grounding",
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
    assert "Application knowledge explains interface mechanics" in prompt
    assert "unless the user's task requires it" in prompt
    assert "Keep independent state dimensions independent" in prompt
    assert "merely useful supplemental metrics" in prompt
    assert "A collector only acquires raw source records" in prompt
    assert "A collector may perform prerequisite navigation or mutation" in prompt
    assert "Do not enumerate atomic GUI actions" in prompt
    assert "Runtime supplies the active adapter's generic capabilities" in prompt
    assert "Never guess enum/status labels" in prompt
    assert "Preserve every user-supplied string predicate verbatim" in prompt
    assert "never require perception to invent a missing year" in prompt
    assert "When success criteria guarantee exactly one target record" in prompt
    assert "Source layout order is not a data contract" in prompt
    assert "surface itself is the requested outcome" in prompt
    assert "use a scalar JSON Schema" in prompt
    assert "does not convert a differently displayed source value" in prompt
    assert "Current, first, or visually prominent values are not extrema" in prompt
    assert 'Set `cardinality="one"` only' in prompt
    assert "the first visible candidate is not" in prompt
    assert "execution experience" not in prompt


def test_visual_transcription_uses_runtime_time_without_inventing_dates() -> None:
    prompt = load_prompt_text("task.tool_agent.visual_transcription")

    assert "provenance-bearing platform clock" in prompt
    assert "explicitly relative visible labels" in prompt
    assert "Omit invisible optional properties" in prompt


def test_presentation_requires_user_facing_prose() -> None:
    prompt = load_prompt_text("task.tool_agent.presentation")

    assert "user-facing prose" in prompt
    assert "not serialized JSON" in prompt
    assert "compact Markdown table" in prompt
    assert "status values" in prompt
    assert "Keep every row and column" in prompt


def test_worker_keeps_data_private_and_coordinates_normalized() -> None:
    prompt = load_prompt_text("task.tool_agent.worker")

    assert "A successful mutation does not prove navigation" in prompt
    assert "Runtime-owned ResultRef and collection values are private" in prompt
    assert "visible states across frames" in prompt
    assert "Never guess credentials" in prompt
    assert "transient code from its delivery surface" in prompt
    assert "immutable goal, success criteria" in prompt
    assert "Complete a single-element operator only" in prompt
    assert "consumes a plan array element-wise" in prompt
    assert "after EACH element's target UI state" in prompt
    assert "Coordinates are normalized 0..999" in prompt
    assert "Use only Runtime-supplied actions" in prompt
    assert "request_action_patch" not in prompt
    assert "`state.status = completed | failed` is terminal" in prompt


def test_worker_prompt_stays_a_compact_role_contract() -> None:
    prompt = load_prompt_text("task.tool_agent.worker")

    assert len(prompt) < 8_000
    assert sum(line.startswith("-") for line in prompt.splitlines()) <= 30


def test_master_keeps_visual_conditional_dependencies_in_one_worker() -> None:
    prompt = load_prompt_text("task.tool_agent.master")

    for rule in (
        "drive a later conditional GUI mutation", "including across application switches",
        "ResultRefs cannot serve as hidden Worker memory", "consume=\"each\"",
        "authorized authentication method", "exact acceptance set", "never prescribe per-record commits",
        "traverses every prerequisite collection", "compares each candidate", "mutates only nonmatches",
        "candidate-local evidence", "complete—not merely visible—candidate traversal",
        "excluded/already-processed identities", "intermediate identities",
        "UI transitions/mutations", "clean environment", "Do not enumerate atomic GUI actions",
        "never an action string like `type.text`", "finishing with `effect=\"data\"`",
        "neither typed data nor a transferable scope",
    ):
        assert rule in prompt


def test_worker_keeps_action_grounding_and_memory_boundaries() -> None:
    prompt = load_prompt_text("task.tool_agent.worker")

    for rule in (
        "exactly one visible control", "do not relabel a nearby or generic control",
        "record identities and visible states across frames",
        "Stable page identity",
        "A `no_effect` result requires inspecting the next frame",
        "A no-effect traversal establishes the boundary", "call `complete`",
        "After a commit exits its editor or form", "instead of reopening the mutation",
        "no encountered record is known unsatisfied", "without rechecking handled records",
        "never establish a collection boundary",
    ):
        assert rule in prompt


@pytest.mark.parametrize(
    ("prompt_id", "phrases"),
    [
        ("task.tool_agent.master", (
            "Keep each collector schema minimal", "never require perception to invent a missing year",
            "surface itself is the requested outcome", "immutable Worker goal/output contract",
                "initial `approach`", "`relative_date_offsets`",
            'coverage="first_match"', "ResultRefs cannot serve as hidden Worker memory",
            "complete—not merely visible—candidate traversal", "Do not enumerate atomic GUI actions",
            "Preserve every user-supplied string predicate verbatim",
        )),
        ("task.tool_agent.visual_transcription", (
            "provenance-bearing platform clock", "Omit invisible optional properties",
            "inside one visible record boundary", "scope context, not row evidence",
            "no enabled pagination or further scrolling remains", "visible page-level identity",
            "declared source semantics",
        )),
        ("task.tool_agent.presentation", (
            "user-facing prose", "not serialized JSON", "compact Markdown table",
            "Keep every row and column", "keep result values unchanged",
        )),
        ("task.tool_agent.worker", (
            "ResultRef and collection values are private", "Never guess credentials",
                "human-presence challenge", "Runtime-supplied actions", "`report_blocked`",
            "Coordinates are normalized 0..999", "binding approach",
                "Worker observations and recent steps", "Do not interact with residue",
                "never establish a collection boundary",
        )),
        ("task.tool_agent.strategy_decide", (
            "materially different, falsifiable implementation approach",
            "Do not emit actions, action arguments, budgets, data filters",
            "Never invent credentials", "Worker chooses atomic actions",
            "Goal, success criteria, profile, inputs, data requirements",
        )),
    ],
)
def test_tool_agent_prompts_keep_contract_rules(
    prompt_id: str,
    phrases: tuple[str, ...],
) -> None:
    prompt = load_prompt_text(prompt_id)
    assert not [phrase for phrase in phrases if phrase not in prompt]


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
