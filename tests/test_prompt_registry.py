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
        "task.tool_agent.state",
        "task.tool_agent.actor",
        "task.tool_agent.visual_transcription",
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
    assert "Preserve each user-owned value as exact or descriptive" in prompt
    assert "unresolved_inputs" in prompt
    assert "discover in the in-scope interface" in prompt
    assert "never put that entity in `unresolved_inputs`" in prompt
    assert "preference, secret, or external choice" in prompt
    assert "completion_facts" in prompt
    assert "externally checkable factual propositions" in prompt
    assert "merely useful supplemental metrics" in prompt
    assert "A collector only returns raw source records" in prompt
    assert "one hybrid collector" in prompt
    assert "the dependent mutation, and raw row output" in prompt
    assert "Do not enumerate atomic GUI actions" in prompt
    assert "Runtime supplies the active adapter's generic capabilities" in prompt
    assert "Never guess enum/status labels" in prompt
    assert "An absolute month/day with no year is not relative" in prompt
    assert "inclusive closed range" in prompt
    assert "uniquely anchored by `task_reference_time`" in prompt
    assert "never require perception to invent a missing year" in prompt
    assert "When success criteria guarantee exactly one target record" in prompt
    assert "Source layout order is not a data contract" in prompt
    assert "surface itself is the requested outcome" in prompt
    assert "use a scalar JSON Schema" in prompt
    assert "does not convert a differently displayed source value" in prompt
    assert "Current, first, or visually prominent values are not extrema" in prompt
    assert "never combine alternative sources" in prompt
    assert "Every row field maps to one actual source value" in prompt
    assert 'Set `cardinality="one"` only' in prompt
    assert "the first visible candidate is not" in prompt
    assert "execution experience" not in prompt


def test_visual_transcription_uses_runtime_time_without_inventing_dates() -> None:
    prompt = load_prompt_text("task.tool_agent.visual_transcription")

    assert "provenance-bearing platform clock" in prompt
    assert "explicitly relative visible labels" in prompt
    assert "Omit invisible optional properties" in prompt
    assert "Respect the requested record grain" in prompt
    assert "child identity or name field" in prompt


def test_presentation_requires_user_facing_prose() -> None:
    prompt = load_prompt_text("task.tool_agent.presentation")

    assert "user-facing prose" in prompt
    assert "not serialized JSON" in prompt
    assert "compact Markdown table" in prompt
    assert "status values" in prompt
    assert "Keep every row and column" in prompt


def test_state_and_actor_prompts_have_disjoint_responsibilities() -> None:
    state = load_prompt_text("task.tool_agent.state")
    actor = load_prompt_text("task.tool_agent.actor")

    assert "Observe what is true now" in state
    assert "conclude the current semantic task transition" in state
    assert "atomically updates the Markdown fact memory" in state
    assert "edit_state_memory" in state
    assert "one open document" in state
    assert "there is no predefined semantic schema" in state
    assert "smallest exact consecutive `old_lines`" in state
    assert "Never rewrite the full document" in state
    assert "Reuse exact refs from `previous_state.target_registry`" in state
    assert "A ref belongs to that one object" in state
    assert "never repurpose an app, surface, container, or parent ref" in state
    assert "its ref must be authorized" in state
    assert "an empty list is valid only when every immediate control" in state
    assert "same object keeps one ref across list/detail views" in state
    assert "names the goal, success criteria, and completion facts" in state
    assert "task conclusion only in the call's transition fields" in state
    assert "The envelope is not memory" in state
    assert "Current visibility belongs only in `visible_targets`" in state
    assert "`outcome.kind=no_effect` means no visual change" in state
    assert "visual identity is not a reason to skip" in state
    assert "empty edits are incorrect" in state
    assert "Write the object fact that is now true" in state
    assert "Never use status, progress, or completion as a field name" in state
    assert "including nested child lines" in actor
    assert "including under a nested child" in actor
    assert "another State-authorized visible target" in actor
    assert "Do not reopen a target to verify" in actor
    assert "Markdown is the confirmation" in actor
    assert "literal Markdown lines" in state
    assert "later navigation or absence never confirms" in state
    assert "Runtime records frame and receipt provenance outside" in state
    assert "Execute the State-provided current task objective" in actor
    assert "Never produce or revise facts" in actor
    assert "never emit `state`, memory, progress" in actor
    assert "never invent a lifecycle for a target" in actor
    assert "Do not recompute the full goal difference" in actor
    assert "does not retract Markdown facts" in actor
    assert "do not reopen that target" in actor
    assert "later back or navigation receipt" in actor
    assert "confirm its authorized target" in actor
    assert "An omitted field is unobserved" in actor
    assert "A value stated in the Goal Contract or Markdown is not missing" in actor
    assert "Text inside a tracked record is task data" in actor
    assert "Never copy record text into `report_blocked.reason`" in actor
    assert "You never declare the Goal Contract complete" in actor
    assert "You choose the next atomic action" in actor
    assert "never recompute the task plan" in actor
    assert "`owned_region_visibility=edge_fragment` is not safely actionable" in actor
    assert "target-oriented Markdown memory owns accumulated" in actor
    assert "state_property_ref" not in actor
    assert "copy its exact `target_ref`" in actor
    assert "safely inside the control's tappable interior" in actor
    assert "Scroll directions describe content traversal" in actor
    assert "Visibility comes only from current target-owned pixels" in state
    assert "spatial index, not a work queue" in actor
    assert "When `phase` is `start`" in actor
    assert "Write the object fact that is now true" in state


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
