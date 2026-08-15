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
        "task.tool_agent.strategy_propose",
        "task.tool_agent.strategy_select",
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
    assert "execution experience" not in prompt


@pytest.mark.parametrize(
    ("prompt_id", "phrases"),
    [
        ("task.tool_agent.master", (
            "Keep each collector schema minimal", "never require perception to invent a missing year",
            "surface itself is the requested outcome", "immutable logical `goal`",
                "current-provider details belong only", "`relative_date_offsets`",
            'coverage="first_match"', "ResultRefs cannot serve as hidden Worker memory",
            "complete—not merely visible—candidate traversal", "Action `description` is static metadata",
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
            "Runtime data-reference values are private", "Never guess an authentication secret",
                "human-presence challenge", "WorkerSpec-declared action that leaves", "otherwise call `fail`",
            "Coordinates are normalized 0..999", "request_action_patch", "strategy_status = blocked",
                "Treat autocomplete as pending state", "strongest leading result",
            "candidate_set_state.status = exhausted", "complete application-declared identity",
                "visible_collection_regions", "Loading, empty chrome, or a result region not yet visible",
        )),
        ("task.tool_agent.strategy_propose", (
            "one to three genuinely different", "invalidated_assumption",
            "observable expected progress", "Never invent credentials",
            "public web origin", "authoritative empty result",
        )),
        ("task.tool_agent.strategy_select", (
            "independent Strategy Selector", "chosen_index", "Otherwise stop",
            "JSON object", "equivalent entry/actions", "One transport interruption",
            "invented deep URL", "remaining budget", "Prefer fewer estimated steps",
            "rather than demanding prior proof",
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
