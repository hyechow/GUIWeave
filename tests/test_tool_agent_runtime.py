from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from gui_agent.adapters.browser.actions import BrowserAction
from gui_agent.adapters.android.actions import AndroidAction
from gui_agent.core.tool_agent.contracts import (
    DynamicActionSpec,
    MaterializedFrame,
    RuntimeInputBinding,
    WorkerOutcome,
    WorkerSpec,
    WorkerStateSnapshot,
    WorkerStrategy,
)
from gui_agent.core.tool_agent.action_guard import (
    action_boundary_error,
)
from gui_agent.core.tool_agent.data_store import RuntimeDataStore
from gui_agent.core.tool_agent.protocol import (
    MAX_ORDERED_ACTIONS,
    ProtocolError,
    decode_ordered_actions,
    worker_attempt_contract,
)
from gui_agent.core.tool_agent.runtime import (
    ToolAgentRuntime,
    _action_feedback,
    _constrain_boundary_scroll_actions,
    _is_transient_model_error,
    _scroll_boundary_feedback,
    _state_target_binding_error,
    _update_traversal_boundaries,
    _target_verification_result,
)
from gui_agent.core.tool_agent.strategy import ReflectionResult, Reflector
from gui_agent.core.schemas import TargetGrounding, TargetVerify
from gui_agent.core.tool_agent.worker_memory import WorkerJournal
from gui_agent.adapters.browser.control_grounding import ground_action_to_nearest_control


_TEST_IMAGE = BytesIO()
Image.new("RGB", (4, 4), "white").save(_TEST_IMAGE, format="PNG")
_TEST_PNG = _TEST_IMAGE.getvalue()


def _state() -> str:
    return json.dumps(
        {
            "status": "exploring",
            "summary": "A separate apply control is visible.",
            "memory_updates": [],
        }
    )


def _state_delta_args(mode: str, frame_id: str, events: list[dict]) -> dict:
    """Encode readable fixtures through the Markdown edit contract."""

    surface = None
    targets = []
    sections = []
    for event in events:
        if event["kind"] == "source_observed":
            sections.append(f"### observed_source\n- Source: {event['source_ref']}")
        elif event["kind"] == "surface_observed":
            surface = event["surface"]
        elif event["kind"] == "target_observed":
            targets.append({
                "target_ref": event["target_ref"],
                "identity": event["identity"],
                "visibility": event["visibility"],
                "owned_region_visibility": event["owned_region_visibility"],
            })
            sections.append(
                f"### {event['target_ref']}\n- Identity: {event['identity']}"
            )
        elif event["kind"] == "property_observed":
            sections.append(
                f"### {event['target_ref']}\n"
                f"- {event['property_ref']}: {event['value']!r}"
            )
    markdown = "\n\n".join(sections)
    return {
        "mode": "init" if mode == "init" else "edit",
        "frame_id": frame_id,
        "surface": surface,
        "visible_targets": targets,
        "edits": ([{
            "old_lines": [], "new_lines": markdown.splitlines(),
        }] if markdown else []),
        "status": "advance",
        "next_objective": "Advance the requested visible interaction.",
        "target_refs": [target["target_ref"] for target in targets],
        "evidence": [],
        "rows": [],
    }


def _state_complete_args(
    mode: str,
    frame_id: str,
    evidence: list[str],
    events: list[dict] | None = None,
) -> dict:
    args = _state_delta_args(mode, frame_id, events or [])
    args.update({
        "status": "complete",
        "next_objective": "",
        "target_refs": [],
        "evidence": evidence,
    })
    return args


def test_transient_model_error_detection_is_provider_neutral_and_narrow() -> None:
    transient = type("InternalServerError", (Exception,), {})("temporary")
    wrapped = RuntimeError("wrapper")
    wrapped.__cause__ = transient

    assert _is_transient_model_error(wrapped)
    assert not _is_transient_model_error(ValueError("bad decision"))


def test_scroll_boundary_feedback_is_mechanical_and_non_terminal() -> None:
    feedback = _scroll_boundary_feedback({"down"})

    assert feedback["status"] == "collection_traversal_boundary"
    assert feedback["boundary_directions"] == ["down"]
    assert feedback["surface_continuity"] == "preserved"
    assert feedback["decision_mode"] == "boundary_reconciliation"
    assert "current scroll container" in feedback["decision_rule"]
    assert "specific unresolved target" in feedback["instruction"]
    assert "does not prove" in feedback["instruction"]


def test_scroll_boundary_schema_excludes_only_recorded_directions() -> None:
    actions = [
        DynamicActionSpec(
            name="scroll",
            capability="scroll",
            description="Traverse the visible collection",
            exposed_args=["direction", "amount"],
        ),
        DynamicActionSpec(name="tap", capability="tap", description="Tap a control"),
    ]

    constrained = _constrain_boundary_scroll_actions(actions, {"down"})
    scroll = next(action for action in constrained if action.capability == "scroll")
    assert scroll.fixed_args["direction"] == "up"
    assert "direction" not in scroll.exposed_args
    assert any(action.capability == "tap" for action in constrained)

    exhausted = _constrain_boundary_scroll_actions(actions, {"up", "down", "left", "right"})
    assert all(action.capability != "scroll" for action in exhausted)


def test_traversal_episode_remembers_boundary_until_non_scroll_action() -> None:
    journal = WorkerJournal(worker_id="traversal")
    boundaries: set[str] = set()
    journal.record_action_result(
        step=1, frame_id="frame:1", tool="scroll", args={"direction": "down"},
        result={"status": "executed", "action_type": "scroll", "no_effect": True},
    )
    _update_traversal_boundaries(boundaries, journal.latest_action_receipt)
    assert boundaries == {"down"}

    journal.record_action_result(
        step=2, frame_id="frame:2", tool="scroll", args={"direction": "up"},
        result={"status": "executed", "action_type": "scroll", "no_effect": False},
    )
    _update_traversal_boundaries(boundaries, journal.latest_action_receipt)
    assert boundaries == {"down"}

    journal.record_action_result(
        step=3, frame_id="frame:3", tool="tap", args={},
        result={"status": "executed", "action_type": "tap", "no_effect": False},
    )
    _update_traversal_boundaries(boundaries, journal.latest_action_receipt)
    assert boundaries == set()


def test_state_target_binding_requires_current_unobscured_observed_ref() -> None:
    state = WorkerStateSnapshot.model_validate({
        "summary": "Three targets have been observed.",
        "targets": {
            "observed_target": {
                "identity": "A target with an observed fact",
                "visibility": "full",
                "owned_region_visibility": "unobscured",
            },
            "edge_target": {
                "identity": "A clipped target",
                "visibility": "partial",
                "owned_region_visibility": "edge_fragment",
            },
            "ready_target": {
                "identity": "An actionable target",
                "visibility": "partial",
                "owned_region_visibility": "unobscured",
            },
            "unauthorized_target": {
                "identity": "A visible but unrelated target",
                "visibility": "full",
                "owned_region_visibility": "unobscured",
            },
        },
        "task_transition": {
            "status": "advance",
            "next_objective": "Advance only the authorized visible targets.",
            "target_refs": ["observed_target", "edge_target", "ready_target"],
        },
    })

    assert "must copy" in _state_target_binding_error(state, "tap", "")
    assert not _state_target_binding_error(state, "tap", None)
    assert not _state_target_binding_error(state, "tap", "observed_target")
    assert "edge fragment" in _state_target_binding_error(
        state, "tap", "edge_target",
    )
    assert not _state_target_binding_error(state, "tap", "ready_target")
    assert "no observed target" in _state_target_binding_error(
        state, "tap", "unknown_target",
    )
    assert "outside the State-authorized targets" in _state_target_binding_error(
        state, "tap", "unauthorized_target",
    )
    assert not _state_target_binding_error(state, "scroll", None)


def _record_executed(
    journal: WorkerJournal, tool: str, *, frame_id: str = "frame:1", **result,
) -> None:
    journal.record_action_result(
        step=1, frame_id=frame_id, tool=tool, args={},
        result={"status": "executed", **result},
    )


def _worker_spec(
    *,
    actions: list[DynamicActionSpec],
    max_steps: int = 12,
    approach: str = "Execute the declared test actions.",
    required_filters: dict | None = None,
    **goal_contract,
) -> WorkerSpec:
    data_requirements = list(goal_contract.get("data_requirements") or [])
    if required_filters is not None and data_requirements:
        data_requirements[0] = {
            **data_requirements[0],
            "filters": required_filters,
        }
        goal_contract["data_requirements"] = data_requirements
    input_bindings = [
        {
            "name": (
                action.name
                if len(action.input_args) == 1
                else f"{action.name}_{argument}"
            ),
            "input": binding.input,
            "path": binding.path,
            "target": {
                ("type", "text"): "text_input",
                ("select_option", "text"): "choice",
                ("open_url", "url"): "url",
                ("launch_app", "app"): "application",
            }[(action.capability, argument)],
            "description": action.description,
        }
        for action in actions
        for argument, binding in action.input_args.items()
    ]
    spec = WorkerSpec(
        **goal_contract,
        input_bindings=input_bindings,
        strategy=WorkerStrategy(approach=approach),
    )
    object.__setattr__(spec, "_test_actions", actions)
    object.__setattr__(spec, "_test_max_steps", max_steps)
    return spec


def _validated_worker_spec(data: dict) -> WorkerSpec:
    values = dict(data)
    actions = values.pop("actions")
    return _worker_spec(
        actions=[
            action if isinstance(action, DynamicActionSpec)
            else DynamicActionSpec.model_validate(action)
            for action in actions
        ],
        max_steps=values.pop("max_steps", 12),
        required_filters=values.pop("required_filters", None),
        **values,
    )


def _install_test_worker_contract(
    runtime: ToolAgentRuntime,
    spec: WorkerSpec,
) -> None:
    runtime._initial_worker_actions = lambda _spec: list(spec._test_actions)
    observe = runtime._observe
    runtime.max_turns = spec._test_max_steps
    runtime._frame_no = 0

    def counted_observe(inner_spec):
        runtime._frame_no += 1
        return observe(inner_spec)

    runtime._observe = counted_observe


def test_runtime_rejects_max_turns_above_50(tmp_path) -> None:
    with pytest.raises(ValueError, match="cannot exceed 50"):
        ToolAgentRuntime(
            bundle=SimpleNamespace(platform="browser"),
            platform=SimpleNamespace(),
            log_dir=tmp_path,
            perception_mode="enhanced",
            max_turns=51,
        )


def test_runtime_blocks_disabled_structured_control() -> None:
    frame = MaterializedFrame(
        frame_id="frame:disabled", screenshot_path="frame.png",
        controls=[{
            "kind": "button", "label": "Confirm", "enabled": False,
            "rect": {"x": 500, "y": 500, "w": 200, "h": 80},
        }],
    )

    error = action_boundary_error(
        "tap", {"x": 500, "y": 500}, frame, set(),
    )

    assert "disabled control" in error


def test_runtime_guard_does_not_choose_query_entry_recovery() -> None:
    frame = MaterializedFrame(
        frame_id="frame:query", screenshot_path="frame.png",
        controls=[{
            "kind": "button", "label": "Search", "query_action": "open",
            "rect": {"x": 500, "y": 500, "w": 200, "h": 80},
        }],
    )

    error = action_boundary_error(
        "type", {"x": 500, "y": 500}, frame, set(),
    )

    assert "target an editable input" in error


def test_runtime_rejects_spatial_target_inside_clipped_collection_cell() -> None:
    frame = MaterializedFrame(
        frame_id="frame:30",
        screenshot_path="frame.png",
        visible_collection_regions=[{"cells": [{
            "ref": "row:settings",
            "bounds": [0, 897, 1000, 1000],
            "texts": ["partially visible record"],
            "clipped_bottom": True,
        }]}],
    )

    def inspect(target_frame: MaterializedFrame, capability: str, y: int) -> str:
        return action_boundary_error(
            capability, {"x": 500, "y": y}, target_frame, set(),
        )

    assert "clipped collection cell" in inspect(frame, "tap", 948)
    assert inspect(frame, "tap", 700) == ""
    assert inspect(frame, "scroll", 948) == ""

    selectable = frame.model_copy(update={"controls": [{
        "kind": "checkbox",
        "label": "Select record",
        "ref": "row:settings.checkbox",
        "selection_mode": "multiple",
        "rect": {"x": 500, "y": 948, "w": 40, "h": 40},
    }]})
    assert "clipped collection cell" in inspect(selectable, "tap", 948)

    unrelated = selectable.model_copy(update={"controls": [
        {**selectable.controls[0], "ref": "toolbar:button"},
    ]})
    assert inspect(unrelated, "tap", 948) == ""


def test_navigation_outside_clipped_collection_is_not_mechanically_blocked() -> None:
    # The ReAct collector owns traversal: navigating away from a clipped scroll
    # collection is a Worker judgment call.
    # The old `_incomplete_collection_exit_reason` gate is gone.
    frame = MaterializedFrame(
        frame_id="frame:9",
        screenshot_path="frame.png",
        visible_collection_regions=[{
            "bounds": [0, 116, 1000, 886],
            "viewport_tail_clipped": True,
            "cells": [],
        }],
    )
    error = action_boundary_error(
        "tap", {"x": 375, "y": 930, "description": "Back"}, frame, set(),
    )
    assert error == ""


def test_action_boundary_allows_typing_into_editable_aria_combobox() -> None:
    frame = MaterializedFrame(
        frame_id="frame:editable-combobox",
        screenshot_path="frame.png",
        controls=[{
            "kind": "aria_combobox",
            "label": "Assignee",
            "focused": True,
            "rect": {"x": 478, "y": 233, "w": 705, "h": 43},
        }],
    )

    error = action_boundary_error(
        "type", {"x": 478, "y": 233, "text": "Alex"}, frame, set(),
    )

    assert error == ""


def test_runtime_decodes_provider_encoded_coordinate_pair() -> None:
    assert decode_ordered_actions(
        '[{"name":"tap","args":{"x":499,499,"description":"Tap splash"}}]'
    ) == [{
        "name": "tap",
        "args": {"x": 499, "y": 499, "description": "Tap splash"},
    }]


def test_runtime_exposes_only_active_adapter_capabilities() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.bundle = SimpleNamespace(platform="android")
    runtime._platform_capabilities = frozenset({"tap", "scroll", "back"})
    runtime.data_store = RuntimeDataStore()
    spec = _worker_spec(
        goal="Choose the required visible option",
        success_criteria=["The requested option is selected"],
        actions=[DynamicActionSpec(
            name="choose_option",
            capability="select_option",
            description="Choose the visible option required by the task",
            fixed_args={"text": "Enabled"},
        )],
    )

    actions = runtime._initial_worker_actions(spec)

    assert {action.capability for action in actions} == {"tap", "scroll", "back"}


def _browser_runtime(**overrides) -> ToolAgentRuntime:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.__dict__.update({
        "bundle": SimpleNamespace(platform="browser"),
        "_platform_capabilities": frozenset({"scroll", "open_url", "back"}),
        "_task_goal": "Read https://initial.example.test/ as a public reference",
        "_master_knowledge": "",
        "_worker_knowledge": "",
        "_start_page_url": "",
        "_worker_access_context": "",
        "data_store": RuntimeDataStore(),
    }, **overrides)
    return runtime


def test_runtime_supplies_generic_navigation_without_master_actions() -> None:
    runtime = _browser_runtime()
    fixed_spec = _worker_spec(
        goal="Reach the replacement reference path",
        success_criteria=["The replacement reference is visible"],
        actions=[DynamicActionSpec(
            name="open_replacement_reference",
            capability="open_url",
            description="Open the replacement reference selected by Master",
            fixed_args={"url": "https://replacement.example.test/"},
        )],
    )

    actions = runtime._initial_worker_actions(fixed_spec)

    assert {action.name for action in actions} == {"back", "open_url", "scroll"}


def test_navigation_validates_transport_safety_without_approach_semantics() -> None:
    runtime = _browser_runtime()
    runtime._validate_runtime_open_url("https://initial.example.test/")
    runtime._validate_runtime_open_url("https://active.example.test/next")
    runtime._validate_runtime_open_url("https://replacement.example.test/deep/path")
    for invalid in ("/relative", "ftp://example.test/file", "https://user@example.test/"):
        with pytest.raises(ValueError, match="absolute HTTP"):
            runtime._validate_runtime_open_url(invalid)
    with pytest.raises(ValueError, match="private, loopback"):
        runtime._validate_runtime_open_url("http://127.0.0.1/internal")


@pytest.mark.parametrize("readiness", ["loading", "blank"])
def test_runtime_passes_unready_frame_to_worker_without_waiting(
    tmp_path, readiness: str,
) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime._frame_no = 0
    runtime._active_worker_id = "worker"
    runtime._worker_journals = {}
    runtime._access_log_redactions = ()
    runtime.perception_mode = "enhanced"
    runtime.log_dir = tmp_path
    runtime.bundle = SimpleNamespace()
    runtime.platform = SimpleNamespace(
        wait_settled=lambda _action_type: (_ for _ in ()).throw(
            AssertionError("Runtime must not wait before Worker decision")
        ),
    )
    frame = MaterializedFrame(
        frame_id="frame:1",
        screenshot_path=f"{readiness}.png",
        readiness=readiness,
        readiness_reason="surface is not ready",
    )
    runtime.materializer = SimpleNamespace(
        observe=lambda **_kwargs: (frame, b"png"),
    )
    runtime._trace = lambda *_args, **_kwargs: None

    observed, _ = runtime._observe(_worker_spec(
        actions=[],
        goal="Inspect the current surface",
        success_criteria=["The current surface is actionable"],
    ))

    assert observed.readiness == readiness
    assert runtime._frame_no == 1


def test_android_runtime_discovers_installed_apps_for_worker_prompt() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime._platform_capabilities = frozenset({"tap", "launch_app"})
    runtime.platform = SimpleNamespace(list_apps=lambda: ["Settings", "Calendar"])
    runtime._master_knowledge = ""
    runtime._worker_access_context = ""
    spec = _worker_spec(
        goal="Open Calendar",
        success_criteria=["Calendar is visible"],
        actions=[DynamicActionSpec(
            name="open_calendar",
            capability="launch_app",
            description="Open the Calendar application",
            fixed_args={"app": "Calendar"},
        )],
    )

    prompt = runtime._actor_system_prompt()

    assert '"Calendar"' in prompt
    assert '"Settings"' in prompt


def test_actor_prompt_defines_safe_batching_once() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.allow_multi_action = True
    runtime._platform_capabilities = frozenset({"tap"})
    runtime._installed_app_names = ()
    runtime._master_knowledge = ""
    runtime._worker_access_context = ""

    prompt = runtime._actor_system_prompt()

    assert "targets are already visible" in prompt
    assert "newly revealed by earlier actions" in prompt
    assert "batch `type` then `press_enter`" in prompt
    assert "## Ordered multi-action mode" not in prompt


@pytest.mark.parametrize(
    ("enabled", "expected"),
    [(False, 1), (True, MAX_ORDERED_ACTIONS)],
)
def test_worker_action_limit_is_owned_by_runtime(
    enabled: bool,
    expected: int,
) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.allow_multi_action = enabled
    runtime.worker_cfg = SimpleNamespace(max_actions_per_call=1)

    assert runtime._worker_action_limit() == expected


@pytest.mark.parametrize(
    ("platform_name", "capabilities", "applications"),
    [
        (
            "android",
            {"tap", "drag", "launch_app"},
            ["Settings", "Calendar"],
        ),
        (
            "browser",
            {"tap", "open_url", "select_option"},
            [],
        ),
    ],
)
def test_master_platform_context_excludes_runtime_action_contracts(
    platform_name: str,
    capabilities: set[str],
    applications: list[str],
) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.bundle = SimpleNamespace(platform=platform_name)
    runtime._platform_capabilities = frozenset(capabilities)
    runtime._installed_app_names = tuple(applications)

    context = runtime._platform_prompt_context()

    assert context == {"name": platform_name, "applications": applications}


def test_private_access_context_reaches_worker_but_is_redacted_from_trace() -> None:
    from gui_agent.core.tool_agent.runtime import _access_log_redactions

    access_context = (
        "# Deployment\n"
        "Account `runtime-user-73` / password `runtime-secret-73`"
    )
    runtime = object.__new__(ToolAgentRuntime)
    runtime._worker_access_context = access_context
    runtime._master_knowledge = "Account settings are available from the profile menu."
    runtime._access_log_redactions = _access_log_redactions(access_context)
    runtime.trace = []
    spec = _worker_spec(
        goal="Reach the authenticated page",
        success_criteria=["The authenticated page is visible"],
        actions=[DynamicActionSpec(
            name="submit_login",
            capability="tap",
            description="Submit the visible login form",
        )],
    )

    prompt = runtime._actor_system_prompt()

    assert "Session access context" in prompt
    assert "runtime-user-73" in prompt
    assert "runtime-secret-73" in prompt
    assert "Application knowledge" in prompt

    runtime._trace(
        "worker_decision",
        state={"status": "exploring", "summary": "Using runtime-secret-73"},
        tool="runtime_type_visible",
        args={"text": "runtime-secret-73"},
        context_reports=[{"system_prompt": prompt}],
    )

    rendered_trace = json.dumps(runtime.trace, ensure_ascii=False)
    assert "runtime-user-73" not in rendered_trace
    assert "runtime-secret-73" not in rendered_trace
    assert "session access value redacted" in rendered_trace


def test_worker_attempt_contract_is_dynamic_and_approach_first() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime._master_knowledge = "The settings page is reached from the profile menu."
    runtime._worker_access_context = "Use the active authenticated session."
    first = _worker_spec(
        goal="Find the requested record",
        success_criteria=["The record is visible"],
        actions=[DynamicActionSpec(
            name="search_record",
            capability="type",
            description="Search the visible record grid using the requested literal.",
            fixed_args={"text": "record-17"},
        )],
        approach="Search the visible record grid for record-17.",
    )

    system_prompt = runtime._actor_system_prompt()
    first_contract = worker_attempt_contract(first)

    assert "Application knowledge" in system_prompt
    assert "Session access context" in system_prompt
    assert "Current Worker attempt" not in system_prompt
    assert "## Goal Contract" in first_contract
    assert first_contract.index("Approach:") < first_contract.index("Goal:")
    assert first_contract.index("Approach:") < first_contract.index("Phase:")
    assert "Phase: start" in first_contract
    assert "Approach: Search the visible record grid for record-17." in first_contract
    assert "Goal: Find the requested record" in first_contract
    assert "Search the visible record grid using the requested literal." not in first_contract
    assert '"exposed_args"' not in first_contract
    assert "row_schema" not in first_contract


def test_runtime_materializes_result_ref_into_fixed_action_argument() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.data_store = RuntimeDataStore()
    runtime._installed_app_names = ()
    runtime._master_knowledge = ""
    runtime._worker_access_context = ""
    runtime._task_goal = "Apply the description; authenticate by SMS if required"
    runtime.allow_multi_action = False
    descriptor = runtime.data_store.put_result(
        {"description": "3 customer(s) love it!"},
        {
            "type": "object",
            "properties": {"description": {"type": "string"}},
            "required": ["description"],
        },
    )
    spec = _validated_worker_spec({
        "profile": "operator",
        "goal": "Apply the computed description",
        "success_criteria": ["The computed description is saved"],
        "input_refs": {"computed": descriptor.ref},
        "actions": [{
            "name": "enter_computed_description",
            "capability": "type",
            "description": "Enter the Runtime-bound description",
            "input_args": {
                "text": {"input": "computed", "path": ["description"]},
            },
        }],
    })

    actions = runtime._initial_worker_actions(spec)
    bound = next(item for item in actions if item.name == "enter_computed_description")

    assert bound.fixed_args == {"text": "3 customer(s) love it!"}
    assert bound.input_args == {}
    assert "text" not in bound.exposed_args
    assert "enter_computed_description" in {item.name for item in actions}
    runtime._active_worker_id = "apply_description"
    runtime._worker_journals = {
        "apply_description": WorkerJournal(worker_id="apply_description")
    }
    tools = runtime._worker_tools_for_frame(
        spec,
        actions,
        MaterializedFrame(frame_id="frame:2", screenshot_path="frame.png"),
    )
    assert "enter_computed_description" in {
        tool["function"]["name"] for tool in tools
    }
    system_prompt = runtime._actor_system_prompt()
    attempt_contract = worker_attempt_contract(spec)
    assert "3 customer(s) love it!" not in system_prompt
    assert '"input":"computed"' in attempt_contract
    assert '"path":["description"]' in attempt_contract
    assert '"task_goal"' not in attempt_contract


def test_worker_reports_blocker_without_deciding_reflection_failure() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.perception_mode = "enhanced"
    runtime.allow_multi_action = False
    runtime._active_worker_id = "replacement_worker"
    journal = WorkerJournal(worker_id="replacement_worker")
    runtime._worker_journals = {"replacement_worker": journal}
    spec = _validated_worker_spec({
        "profile": "operator",
        "goal": "Reach the replacement surface",
        "success_criteria": ["The replacement surface is visible"],
        "actions": [{
            "name": "open_replacement",
            "capability": "open_url",
            "description": "Open the Strategy-bound replacement surface",
            "fixed_args": {"url": "https://replacement.example.test/"},
        }],
    })
    frame = MaterializedFrame(
        frame_id="frame:replacement",
        screenshot_path="replacement.png",
        url="https://blocked.example.test/",
    )

    pending = runtime._worker_tools_for_frame(
        spec, spec._test_actions, frame, allow_failure=False,
    )
    _record_executed(journal, "open_replacement", frame_id=frame.frame_id)
    attempted = runtime._worker_tools_for_frame(spec, spec._test_actions, frame)

    assert "report_blocked" not in {tool["function"]["name"] for tool in pending}
    assert "report_blocked" in {tool["function"]["name"] for tool in attempted}
    assert "fail" not in {tool["function"]["name"] for tool in pending}


def test_actor_never_owns_completion_when_runtime_mode_allows_it() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.allow_multi_action = False
    action = DynamicActionSpec(
        name="save_target", capability="tap", description="Save the visible target",
    )
    spec = _worker_spec(
        profile="operator", goal="Save the target",
        success_criteria=["The target is saved"], actions=[action],
        completion_facts=[{
            "property_ref": "target_saved",
            "description": "The target is saved.",
            "expected_value": True,
        }],
    )
    frame = MaterializedFrame(frame_id="frame:2", screenshot_path="frame.png")

    waiting = runtime._worker_tools_for_frame(
        spec, [action], frame, allow_failure=False,
    )
    ready = runtime._worker_tools_for_frame(
        spec, [action], frame, allow_failure=False,
    )

    # Completion is State-owned; the Actor tool set never exposes it.
    assert "complete" not in {tool["function"]["name"] for tool in waiting}
    assert "complete" not in {tool["function"]["name"] for tool in ready}


def test_bound_type_remains_a_worker_choice_on_closed_query_surface() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.perception_mode = "enhanced"
    runtime.allow_multi_action = False
    runtime._active_worker_id = "search_records"
    runtime._worker_journals = {
        "search_records": WorkerJournal(worker_id="search_records")
    }
    spec = _validated_worker_spec({
        "profile": "operator",
        "goal": "Find the computed record",
        "success_criteria": ["The computed record is visible"],
        "input_refs": {"query": "result:1"},
        "actions": [{
            "name": "enter_query",
            "capability": "type",
            "description": "Enter the computed query",
            "input_args": {"text": {"input": "query", "path": ["value"]}},
        }],
    })
    actions = [
        DynamicActionSpec(
            name="enter_query", capability="type",
            description="Enter the computed query", fixed_args={"text": "private"},
        ),
    ]
    opener = MaterializedFrame(
        frame_id="frame:1", screenshot_path="frame.png",
        controls=[{
            "kind": "button", "label": "Search", "query_action": "open",
            "enabled": True, "in_viewport": True,
            "rect": {"x": 500, "y": 80, "w": 120, "h": 50},
            "action_point": {"x": 510, "y": 85},
        }],
    )
    names = {
        tool["function"]["name"]
        for tool in runtime._worker_tools_for_frame(spec, list(actions), opener)
    }
    assert names == {"enter_query", "report_blocked"}


def test_global_turn_budget_is_shared_across_logical_workers() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_turns = 2
    runtime._frame_no = 0
    runtime.trace = []
    events = []
    runtime._trace = lambda event, **payload: events.append({"event": event, **payload})
    calls = []

    def run_worker(worker_id, _spec, *, require_attempt=False):
        calls.append(worker_id)
        runtime._frame_no += 1
        return WorkerOutcome(
            phase="completed",
            summary=f"Completed {worker_id}",
            steps=1,
        )

    runtime._run_worker = run_worker
    spec = _validated_worker_spec({
        "profile": "operator",
        "goal": "Complete one UI subgoal",
        "success_criteria": ["The subgoal is complete"],
        "actions": [{
            "name": "advance_subgoal",
            "capability": "tap",
            "description": "Advance the visible subgoal",
        }],
    })

    first = runtime._run_logical_worker("first_worker", spec)
    second = runtime._run_logical_worker("second_worker", spec)
    blocked = runtime._run_logical_worker("third_worker", spec)

    assert first.phase == second.phase == "completed"
    assert blocked.phase == "failed"
    assert blocked.steps == 0
    assert "global turn budget (2/2)" in blocked.summary
    assert calls == ["first_worker", "second_worker"]
    assert events == [{
        "event": "runtime_turn_budget_exhausted",
        "worker_id": "third_worker",
        "turns_used": 2,
        "max_turns": 2,
    }]


def test_reflection_does_not_receive_or_control_runtime_turn_budget() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_turns = 10
    runtime._frame_no = 8
    runtime._trace = lambda *_args, **_kwargs: None

    def fail_worker(_worker_id, _spec, *, require_attempt=False):
        runtime._frame_no += 1
        return WorkerOutcome(phase="failed", summary="Try a distinct path", steps=1)

    requests = []
    runtime._run_worker = fail_worker
    runtime._request_reflection = lambda **kwargs: ReflectionResult(
        decision="stop",
        reason="No feasible attempt remains",
        strategy=requests.append(kwargs),
    )
    spec = _validated_worker_spec({
        "profile": "operator",
        "goal": "Complete one UI subgoal",
        "success_criteria": ["The subgoal is complete"],
        "actions": [{
            "name": "advance_subgoal",
            "capability": "tap",
            "description": "Advance the visible subgoal",
        }],
    })

    runtime._run_logical_worker("logical_worker", spec)

    assert len(requests) == 1
    assert "remaining_steps" not in requests[0]


def test_redelegation_failure_reports_all_consumed_worker_steps() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_turns = 50
    runtime._frame_no = 0
    runtime._trace = lambda *_args, **_kwargs: None
    attempts = iter((3, 4))
    runtime._run_worker = lambda _worker_id, _spec, *, require_attempt=False: WorkerOutcome(
        phase="failed",
        summary="Try another local strategy",
        steps=next(attempts),
    )
    revisions = 0

    def revise(**_kwargs):
        nonlocal revisions
        revisions += 1
        if revisions == 1:
            return ReflectionResult(
                decision="revise_approach", reason="selected",
                strategy=spec.strategy.model_copy(
                    update={"approach": "Use the alternate visible path."}
                ),
            )
        raise ValueError("replacement is invalid")

    runtime._request_reflection = revise
    spec = _validated_worker_spec({
        "profile": "operator",
        "goal": "Complete one UI subgoal",
        "success_criteria": ["The subgoal is complete"],
        "actions": [{
            "name": "advance_subgoal",
            "capability": "tap",
            "description": "Advance the visible subgoal",
        }],
    })

    outcome = runtime._run_logical_worker("logical_worker", spec)

    assert outcome.phase == "failed"
    assert outcome.steps == 7
    assert "Reflection failed" in outcome.summary


def test_worker_blocked_requires_a_replacement_strategy() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_turns = 50
    runtime._frame_no = 0
    events = []
    runtime._trace = lambda event, **payload: events.append({"event": event, **payload})
    outcomes = iter((
        WorkerOutcome(
            phase="failed", summary="The current source is blocked",
            failure_kind="worker_blocked", steps=2,
        ),
        WorkerOutcome(phase="completed", summary="Completed with replacement", steps=1),
    ))
    worker_ids = []

    attempt_requirements = []

    def run_worker(worker_id, _spec, *, require_attempt=False):
        worker_ids.append(worker_id)
        attempt_requirements.append(require_attempt)
        return next(outcomes)

    runtime._run_worker = run_worker
    runtime._request_reflection = lambda **_kwargs: ReflectionResult(
        decision="revise_approach",
        strategy=spec.strategy.model_copy(update={
            "approach": "Use an evidenced alternative traversal.",
        }),
        reason="The original source is blocked.",
    )
    spec = _validated_worker_spec({
        "profile": "operator",
        "goal": "Complete one cohesive UI traversal",
        "success_criteria": ["The traversal is complete"],
        "actions": [{
            "name": "advance_traversal",
            "capability": "tap",
            "description": "Advance the visible traversal",
        }],
        "max_steps": 2,
    })

    outcome = runtime._run_logical_worker("logical_worker", spec)

    assert outcome.phase == "completed"
    assert outcome.steps == 3
    assert worker_ids == ["logical_worker", "logical_worker_reflection_1"]
    assert attempt_requirements == [False, True]
    assert any(event["event"] == "reflected_worker_dispatched" for event in events)


def test_reflection_replacements_use_only_the_global_turn_budget() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_turns = 4
    runtime._frame_no = 0
    runtime._trace = lambda *_args, **_kwargs: None
    outcomes = iter([
        *(WorkerOutcome(
            phase="failed", summary="source rejected navigation",
            failure_kind="navigation_blocked", steps=1,
        ) for _ in range(3)),
        WorkerOutcome(phase="completed", summary="source four completed", steps=1),
    ])
    calls = []

    def run_worker(worker_id, _spec, *, require_attempt=False):
        calls.append(("worker", worker_id))
        runtime._frame_no += 1
        return next(outcomes)

    def replace(**kwargs):
        attempt = kwargs["attempt_no"]
        calls.append(("strategy", attempt))
        return ReflectionResult(
            decision="revise_approach", reason="selected",
            strategy=WorkerStrategy(approach=f"alternative source {attempt}"),
        )

    runtime._run_worker = run_worker
    runtime._request_reflection = replace
    spec = _worker_spec(
        profile="operator",
        goal="Complete one cohesive UI traversal",
        success_criteria=["The traversal is complete"],
        actions=[],
    )

    outcome = runtime._run_logical_worker("logical_worker", spec)

    assert (outcome.phase, outcome.steps) == ("completed", 4)
    assert calls == [
        ("worker", "logical_worker"),
        ("strategy", 1), ("worker", "logical_worker_reflection_1"),
        ("strategy", 2), ("worker", "logical_worker_reflection_2"),
        ("strategy", 3), ("worker", "logical_worker_reflection_3"),
    ]


class _Executor:
    def __init__(self) -> None:
        self.actions = []
        self.execute_kwargs = []

    def execute(self, decision, **kwargs):
        self.execute_kwargs.append(kwargs)
        self.actions.append(decision.action)
        return True


class _GroundingExecutor(_Executor):
    def ground_coordinates(self, decision, controls):
        return ground_action_to_nearest_control(
            decision,
            controls,
            viewport_size=(1281, 963),
        )


class _ImmediateVerifyFuture:
    def __init__(self, value: TargetGrounding | TargetVerify) -> None:
        self.value = value

    def result(self, *, timeout=None):
        del timeout
        return self.value


class _ImmediateVerifyPool:
    def __init__(self, *values: TargetGrounding | TargetVerify) -> None:
        self.values = list(values)
        self.submitted = []

    def submit(self, function, *args):
        self.submitted.append((function, args))
        return _ImmediateVerifyFuture(self.values.pop(0))


class _Visualizer:
    def __init__(self) -> None:
        self.points = []
        self.clear_calls = 0

    def show_action(self, action) -> None:
        snap = action.snap if isinstance(action.snap, dict) else {}
        point = snap.get("snapped") or [action.x, action.y]
        self.points.append(tuple(point))

    def clear(self) -> None:
        self.clear_calls += 1


class _SplitWorkerFixture:
    def __init__(self) -> None:
        self.mode = ""
        self.state_calls = 0

    def bind_tools(self, tools, **kwargs):
        del kwargs
        names = {tool["function"]["name"] for tool in tools}
        self.mode = "state" if "edit_state_memory" in names else "actor"
        return self

    def bind(self, **kwargs):
        del kwargs
        self.mode = "state"
        return self

    def invoke(self, messages):
        if self.mode == "state":
            self.state_calls += 1
            state_input = json.loads(messages[-1].content[0]["text"])
            events = ([{
                "kind": "source_observed",
                "source_ref": "test_surface",
                "evidence": "The requested test surface is visible.",
            }] if state_input["mode"] == "init" else [])
            return SimpleNamespace(content="", tool_calls=[{
                "id": "state-delta",
                "name": "edit_state_memory",
                "args": _state_delta_args(
                    state_input["mode"], state_input["frame_id"], events,
                ),
            }])
        return self.actor_response(messages)

    def actor_response(self, messages):
        raise NotImplementedError


class _EmptyContentWorker(_SplitWorkerFixture):
    def actor_response(self, messages):
        del messages
        return SimpleNamespace(
            content="",
            tool_calls=[{
                "id": "tap-1",
                "name": "activate_visible_control",
                "args": {
                    "x": 400, "y": 300, "description": "Advance",
                    "state_target_ref": None,
                },
            }],
        )


class _MissingStateWorker(_EmptyContentWorker):
    def invoke(self, messages):
        if self.mode == "state":
            return SimpleNamespace(content=_state(), tool_calls=[])
        return super().invoke(messages)


class _ArrayCoordinateWorker(_SplitWorkerFixture):
    def actor_response(self, messages):
        del messages
        return SimpleNamespace(
            content="",
            tool_calls=[{
                "id": "type-1",
                "name": "enter_value",
                "args": {
                    "x": [200, 380],
                    "y": [200, 380],
                    "text": "01/01/2023",
                    "description": "Enter the start date",
                    "state_target_ref": None,
                },
            }],
        )


class _RepeatedThenGroundedWorker(_SplitWorkerFixture):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def actor_response(self, messages):
        del messages
        self.calls += 1
        args = {
            "x": 207,
            "y": 550,
            "text": "05/31/2023",
            "description": "Enter the end date into the Purchase Date to input",
            "state_target_ref": None,
        }
        if self.calls >= 3:
            args["x"] = 207
            args["y"] = 448
        return SimpleNamespace(
            content="",
            tool_calls=[{
                "id": f"type-{self.calls}",
                "name": "enter_end_date",
                "args": args,
            }],
        )


class _RepeatedEffectiveScrollWorker(_SplitWorkerFixture):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def actor_response(self, messages):
        del messages
        self.calls += 1
        return SimpleNamespace(
            content="",
            tool_calls=[{
                "id": f"scroll-{self.calls}",
                "name": "reveal_more",
                "args": {"amount": "medium"},
            }],
        )


_LOGIN_ACTIONS = [
    {"name": "enter_username", "args": {"x": 500, "y": 400, "text": "demo-user", "description": "Enter Username", "state_target_ref": None}},
    {"name": "enter_password", "args": {"x": 500, "y": 500, "text": "demo-pass", "description": "Enter Password", "state_target_ref": None}},
    {"name": "submit_login", "args": {"x": 500, "y": 600, "description": "Tap Sign in", "state_target_ref": None}},
]


def _code_action(code: str = "757570") -> dict:
    return {
        "name": "enter_code",
        "args": {
            "x": 500, "y": 500, "text": code,
            "description": "Enter verification code",
            "state_target_ref": None,
        },
    }


def _code_spec() -> DynamicActionSpec:
    return DynamicActionSpec(
        name="enter_code", capability="type",
        description="Enter verification code", exposed_args=["text"],
    )


class _MultiActionWorker(_SplitWorkerFixture):
    def __init__(
        self,
        action_batches: list[list[dict]] | None = None,
    ) -> None:
        super().__init__()
        self.calls = 0
        self.bound_names: set[str] = set()
        self.bound_schemas: list[str] = []
        self.action_batches = action_batches
        self.messages = []
        self.state_summary = "The complete login form is visible."
        self.state_target_identity = ""
        self.state_events: list[dict] = []
        self.actor_state = None

    def bind_tools(self, tools, **kwargs):
        assert kwargs.get("parallel_tool_calls") is False
        self.bound_names = {tool["function"]["name"] for tool in tools}
        self.mode = (
            "state" if "edit_state_memory" in self.bound_names else "actor"
        )
        self.bound_schemas.append(json.dumps(tools))
        return self

    def invoke(self, messages):
        if self.mode == "state":
            state_input = json.loads(messages[-1].content[0]["text"])
            events = []
            if state_input["mode"] == "init":
                events.append({
                    "kind": "source_observed",
                    "source_ref": "test_surface",
                    "evidence": "The requested test surface is visible.",
                })
                if self.state_target_identity:
                    events.append({
                        "kind": "target_observed",
                        "target_ref": "test_target",
                        "identity": self.state_target_identity,
                        "visibility": "full",
                        "owned_region_visibility": "unobscured",
                        "evidence": self.state_target_identity,
                    })
                events.extend(self.state_events)
            self.state_calls += 1
            return SimpleNamespace(content="", tool_calls=[{
                "id": "state-delta",
                "name": "edit_state_memory",
                "args": _state_delta_args(
                    state_input["mode"], state_input["frame_id"], events,
                ),
            }])
        self.messages = messages
        self.calls += 1
        actions = (
            self.action_batches[self.calls - 1]
            if self.action_batches is not None
            else _LOGIN_ACTIONS
        )
        actions = [
            {
                **action,
                "args": {
                    **action["args"],
                    **(
                        {"state_target_ref": None}
                        if (
                            ("x" in action["args"] or "y" in action["args"])
                            and "state_target_ref" not in action["args"]
                        )
                        else {}
                    ),
                },
            }
            for action in actions
        ]
        args = {"actions": actions}
        if self.actor_state is not None:
            args["state"] = self.actor_state
        return SimpleNamespace(content="", tool_calls=[{
            "id": f"decision-{self.calls}",
            "name": "continue_with_actions",
            "args": args,
        }])


class _SplitRoleWorker:
    def __init__(self) -> None:
        self.mode = ""
        self.state_calls = 0
        self.actor_calls = 0
        self.state_messages = []
        self.actor_messages = []
        self.actor_tools = []

    def bind(self, **kwargs):
        del kwargs
        self.mode = "state"
        return self

    def bind_tools(self, tools, **kwargs):
        assert kwargs.get("parallel_tool_calls") is False
        names = {tool["function"]["name"] for tool in tools}
        self.mode = "state" if "edit_state_memory" in names else "actor"
        if self.mode == "actor":
            self.actor_tools = tools
        return self

    def invoke(self, messages):
        if self.mode == "state":
            self.state_calls += 1
            self.state_messages = messages
            return SimpleNamespace(content="", tool_calls=[{
                "id": "state-delta",
                "name": "edit_state_memory",
                "args": _state_delta_args("init", "frame:1", [
                        {
                            "kind": "source_observed",
                            "source_ref": "login_form",
                            "evidence": "The requested login form is visible.",
                        },
                        {
                            "kind": "surface_observed",
                            "surface": "login_form",
                            "evidence": "The current surface is the login form.",
                        },
                        {
                            "kind": "target_observed",
                            "target_ref": "login_submit",
                            "identity": "The visible enabled submit control",
                            "visibility": "full",
                            "owned_region_visibility": "unobscured",
                            "evidence": "The submit control is fully visible.",
                        },
                    ]),
            }])
        self.actor_calls += 1
        self.actor_messages = messages
        return SimpleNamespace(content="", tool_calls=[{
            "id": "actor-decision",
            "name": "continue_with_actions",
            "args": {"actions": [{
                "name": "submit_login",
                "args": {
                    "state_target_ref": "login_submit",
                    "x": 500,
                    "y": 500,
                    "description": "Submit the visible login form",
                },
            }]},
        }])


class _SplitEachWorker:
    def __init__(self) -> None:
        self.mode = ""
        self.state_calls = 0
        self.actor_calls = 0
        self.state_modes = []

    def bind(self, **kwargs):
        del kwargs
        self.mode = "state"
        return self

    def bind_tools(self, tools, **kwargs):
        assert kwargs.get("parallel_tool_calls") is False
        names = {tool["function"]["name"] for tool in tools}
        self.mode = "state" if "edit_state_memory" in names else "actor"
        return self

    def invoke(self, messages):
        if self.mode == "state":
            self.state_calls += 1
            state_input = json.loads(messages[-1].content[0]["text"])
            init = state_input["mode"] == "init"
            self.state_modes.append(state_input["mode"]) if init else None
            # Each element: record it, then after the element's action the State
            # advances the plan cursor with `complete`.
            if self.state_calls in {2, 4}:
                return SimpleNamespace(content="", tool_calls=[{
                    "id": f"state-complete-{self.state_calls}",
                    "name": "edit_state_memory",
                    "args": _state_complete_args(
                        state_input["mode"], state_input["frame_id"],
                        ["The current element is processed."],
                    ),
                }])
            events = ([{
                "kind": "source_observed",
                "source_ref": "current_element",
                "evidence": "The current element is visible.",
            }] if init else [])
            return SimpleNamespace(content="", tool_calls=[{
                "id": "state-delta",
                "name": "edit_state_memory",
                "args": _state_delta_args(
                    state_input["mode"], state_input["frame_id"], events,
                ),
            }])
        self.actor_calls += 1
        return SimpleNamespace(content="", tool_calls=[{
            "id": f"action-{self.actor_calls}",
            "name": "continue_with_actions",
            "args": {"actions": [{
                "name": "apply_value",
                "args": {
                    "x": 500,
                    "y": 500,
                    "description": "Apply the current bound value",
                    "state_target_ref": None,
                },
            }]},
        }])


class _SplitRepairWorker(_SplitRoleWorker):
    def invoke(self, messages):
        if self.mode == "state":
            return super().invoke(messages)
        self.actor_calls += 1
        self.actor_messages = messages
        if self.actor_calls == 1:
            return SimpleNamespace(content="", tool_calls=[{
                "id": "invalid-launch",
                "name": "continue_with_actions",
                "args": {"actions": [{
                    "name": "open_settings",
                    "args": {"app": "Settings"},
                }]},
            }])
        return SimpleNamespace(content="", tool_calls=[{
            "id": "valid-launch",
            "name": "continue_with_actions",
            "args": {"actions": [{
                "name": "open_settings",
                "args": {"app": "com.android.settings/.HWSettings"},
            }]},
        }])


def _run_fused_worker(
    monkeypatch,
    *,
    current_url: str,
    worker=None,
    actions: list[DynamicActionSpec] | None = None,
    controls: list[dict] | None = None,
    requirement_scopes: dict[str, dict] | None = None,
    visible_collection_regions: list[dict] | None = None,
    installed_apps: tuple[str, ...] = (),
    journal: WorkerJournal | None = None,
    bundle=None,
    executor=None,
    max_steps: int = 1,
    spec: WorkerSpec | None = None,
    data_store=None,
) -> ToolAgentRuntime:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime.statuses = []
    runtime._status_cb = runtime.statuses.append
    runtime._each_cursors = {}
    runtime._worker_state_snapshots = {}
    runtime._worker_state_frames = {}
    runtime._worker_last_frames = {}
    runtime.worker = worker or _MultiActionWorker()
    runtime._executor = executor or _Executor()
    if data_store is not None:
        runtime.data_store = data_store
    if bundle is not None:
        runtime.bundle = bundle
    runtime._installed_app_names = installed_apps
    runtime.screenshot_calls = []
    runtime.platform = SimpleNamespace(
        screenshot=lambda: runtime.screenshot_calls.append(True) or b"latest-png",
        client=SimpleNamespace(page_info=lambda: (current_url, "Login")),
    )
    runtime.allow_multi_action = True
    if journal is not None:
        runtime._worker_journals = {"fused-worker": journal}
    runtime.observe_calls = 0
    observation_image = BytesIO()
    Image.new("RGB", (4, 4), "white").save(observation_image, format="PNG")
    observation_png = observation_image.getvalue()

    def observe(_spec):
        runtime.observe_calls += 1
        return MaterializedFrame(
            frame_id=f"frame:{runtime.observe_calls}",
            screenshot_path="frame.png",
            url="https://example.test/login",
            controls=controls or [],
            visible_collection_regions=visible_collection_regions or [],
            requirement_scopes=requirement_scopes or {},
        ), observation_png

    runtime._observe = observe
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, **_kwargs: (0.0, False),
    )
    worker_spec = spec or _worker_spec(
        goal="Complete the visible local interaction",
        success_criteria=["The requested interface state is reached"],
        actions=actions or [
            DynamicActionSpec(
                name="enter_username",
                capability="type",
                description="Enter the visible username",
                exposed_args=["text"],
            ),
            DynamicActionSpec(
                name="enter_password",
                capability="type",
                description="Enter the visible password",
                exposed_args=["text"],
            ),
            DynamicActionSpec(
                name="submit_login",
                capability="tap",
                description="Submit the visible login form",
            ),
        ],
        max_steps=max_steps,
    )
    _install_test_worker_contract(runtime, worker_spec)
    runtime.outcome = runtime._run_worker("fused-worker", worker_spec)
    return runtime


def test_runtime_runs_state_before_action_only_actor(monkeypatch) -> None:
    worker = _SplitRoleWorker()
    runtime = _run_fused_worker(
        monkeypatch,
        current_url="https://example.test/login",
        worker=worker,
        actions=[DynamicActionSpec(
            name="submit_login",
            capability="tap",
            description="Submit the visible login form",
            exposed_args=["x", "y", "description"],
        )],
        installed_apps=("Calendar",),
    )

    assert (worker.state_calls, worker.actor_calls) == (1, 1)
    assert [event["event"] for event in runtime.trace if event["event"] in {
        "worker_state", "worker_decision",
    }] == ["worker_state", "worker_decision"]
    assert len(runtime._executor.actions) == 1
    for tool in worker.actor_tools:
        parameters = tool["function"]["parameters"]
        assert "state" not in parameters.get("properties", {})
        assert "state" not in parameters.get("required", [])
    actor_text = str(worker.actor_messages[-1].content)
    state_text = str(worker.state_messages[-1].content)
    assert "Continuous observed fact memory" in actor_text
    assert "Historical Progress" not in actor_text
    assert '"mode": "init"' in state_text
    assert '"output_contract"' not in state_text
    assert '"goal_contract"' in state_text
    assert '"observation_focus"' in state_text
    assert "Installed applications" not in str(worker.state_messages[0].content)
    assert "Installed applications" in str(worker.actor_messages[0].content)


def test_same_frame_action_repair_reuses_state_snapshot(monkeypatch) -> None:
    exact_app = "com.android.settings/.HWSettings"
    worker = _SplitRepairWorker()
    runtime = _run_fused_worker(
        monkeypatch,
        current_url="",
        worker=worker,
        actions=[DynamicActionSpec(
            name="open_settings",
            capability="launch_app",
            description="Open system settings",
            exposed_args=["app"],
        )],
        installed_apps=(exact_app,),
        bundle=SimpleNamespace(
            make_action=lambda payload: AndroidAction.model_validate(payload),
        ),
    )

    assert runtime.outcome.phase == "failed"
    assert (worker.state_calls, worker.actor_calls, runtime.observe_calls) == (1, 2, 1)
    assert any(
        event["event"] == "worker_state_reused" for event in runtime.trace
    )


def test_append_state_context_uses_compact_previous_and_current_frame_pair() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime._worker_explicit_cache = False
    runtime.worker_cfg = SimpleNamespace(image_scale=1.0)
    runtime._state_system_prompt = lambda: "state policy"
    runtime._current_each_element = lambda _spec, _worker_id: None
    previous = WorkerStateSnapshot.model_validate({
        "summary": "One tracked target has an observed value.",
        "frame_id": "frame:1",
        "surface": "collection",
        "markdown": "### stable_target\n- Requested state: false",
        "targets": {
            "stable_target": {
                "identity": "One stable visible target",
                "visibility": "full",
                "owned_region_visibility": "unobscured",
            },
        },
    })
    previous_image = BytesIO()
    current_image = BytesIO()
    Image.new("RGB", (400, 200), "red").save(previous_image, format="PNG")
    Image.new("RGB", (400, 200), "blue").save(current_image, format="PNG")
    runtime._worker_state_snapshots = {"worker": previous}
    runtime._worker_state_frames = {
        "worker": ("frame:1", previous_image.getvalue()),
    }
    journal = WorkerJournal(worker_id="worker")
    spec = _worker_spec(
        goal="Set the target to the requested state.",
        success_criteria=["The target has the requested state."],
        actions=[],
    )

    messages, reports = runtime._state_messages(
        spec=spec,
        journal=journal,
        frame=MaterializedFrame(frame_id="frame:2", screenshot_path="frame.png"),
        png=current_image.getvalue(),
        same_frame_feedback=None,
    )

    content = messages[1].content
    assert [part["type"] for part in content] == [
        "text", "text", "image_url", "text", "image_url",
    ]
    payload = json.loads(content[0]["text"])
    assert payload["mode"] == "edit"
    assert payload["visual_transition"] == {
        "previous_frame_id": "frame:1",
        "current_frame_id": "frame:2",
        "previous_frame_available": True,
    }
    assert payload["previous_state"] == {
        "surface": "collection",
        "target_registry": {"stable_target": "One stable visible target"},
        "memory_markdown": "### stable_target\n- Requested state: false",
        "previous_task_transition": None,
    }
    assert "goal_contract" in payload["observation_focus"]
    assert payload["observation_focus"] == {
        "visible_fields": [],
        "goal_contract": {
            "success_criteria": ["The target has the requested state."],
            "goal": "Set the target to the requested state.",
            "completion_facts": [],
        },
    }
    assert "execution_scope" not in payload
    assert "latest_runtime_receipt" not in payload
    assert "same_frame_runtime_feedback" not in payload
    assert reports[0]["strategy"] == "markdown_state_edit"
    assert reports[0]["previous_frame"] is True
    assert reports[0]["previous_frame_scale"] == 0.75


def test_split_state_reinitializes_after_each_element_advances(monkeypatch) -> None:
    worker = _SplitEachWorker()
    action = DynamicActionSpec(
        name="apply_value",
        capability="type",
        description="Apply the current bound value",
        input_args={
            "text": RuntimeInputBinding(input="plan", path=["value"]),
        },
        exposed_args=["x", "y", "description"],
    )
    spec = _worker_spec(
        goal="Process each planned value.",
        success_criteria=["The current planned value is visibly processed."],
        input_refs={"plan": "result:1"},
        actions=[action],
        max_steps=4,
    )

    runtime = _run_fused_worker(
        monkeypatch,
        current_url="https://example.test/form",
        worker=worker,
        spec=spec,
        data_store=SimpleNamespace(
            result_value=lambda _ref: [{"value": "first"}, {"value": "second"}],
        ),
    )

    assert runtime.outcome.phase == "completed"
    assert worker.state_modes == ["init", "init"]
    assert (worker.state_calls, worker.actor_calls) == (4, 2)
    assert len(runtime._executor.actions) == 2
    assert runtime._each_cursors[("fused-worker", "plan")] == 2


class _PrematureCompleteWorker(_SplitWorkerFixture):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def invoke(self, messages):
        if self.mode == "state":
            self.state_calls += 1
            state_input = json.loads(messages[-1].content[0]["text"])
            # The State role declares the operator established without doing work;
            # Runtime resolves it without semantically auditing the contract.
            return SimpleNamespace(content="", tool_calls=[{
                "id": "state-complete-1",
                "name": "edit_state_memory",
                "args": _state_complete_args(
                    state_input["mode"], state_input["frame_id"],
                    ["The visible Apply control was activated."],
                ),
            }])
        self.calls += 1
        return SimpleNamespace(content="", tool_calls=[{
            "id": "complete-1", "name": "complete",
            "args": {"evidence": ["The visible Apply control was activated."]},
        }])


class _AtomicFactCompleteWorker(_SplitWorkerFixture):
    def __init__(self) -> None:
        super().__init__()
        self.actor_calls = 0

    def invoke(self, messages):
        if self.mode == "state":
            self.state_calls += 1
            state_input = json.loads(messages[-1].content[0]["text"])
            return SimpleNamespace(content="", tool_calls=[{
                "id": "state-atomic-complete",
                "name": "edit_state_memory",
                "args": _state_complete_args(
                    state_input["mode"], state_input["frame_id"],
                    ["The requested value is visibly true."],
                    [{
                        "kind": "property_observed",
                        "target_ref": "record",
                        "property_ref": "requested_value",
                        "value": True,
                    }],
                ),
            }])
        self.actor_calls += 1
        raise AssertionError("Actor must not run after atomic State completion")


class _CompletionAuditExecutor(_Executor):
    def __init__(self) -> None:
        super().__init__()
        self.pending = True

    def refresh_controls(self):
        return [{
            "ref": "apply", "kind": "button", "label": "Apply",
            "form_action": "commit",
            "enabled": True, "in_viewport": True,
            "rect": {"x": 500, "y": 500, "w": 100, "h": 40},
        }] if self.pending else []

    def execute(self, decision, **kwargs):
        result = super().execute(decision, **kwargs)
        self.pending = False
        return result


def test_operator_completion_is_not_runtime_semantically_audited(monkeypatch) -> None:
    worker = _PrematureCompleteWorker()
    executor = _CompletionAuditExecutor()

    runtime = _run_fused_worker(
        monkeypatch,
        current_url="https://example.test/login",
        worker=worker,
        executor=executor,
        max_steps=1,
    )

    assert (runtime.outcome.phase, worker.calls, len(executor.actions)) == (
        "completed", 0, 0,
    )
    assert not any(
        event["event"] == "worker_completion_recheck" for event in runtime.trace
    )


def test_state_records_decisive_fact_and_completes_before_actor(monkeypatch) -> None:
    worker = _AtomicFactCompleteWorker()
    runtime = _run_fused_worker(
        monkeypatch,
        current_url="https://example.test/record",
        worker=worker,
        max_steps=1,
    )

    assert runtime.outcome.phase == "completed"
    assert (worker.state_calls, worker.actor_calls) == (1, 0)
    state = runtime._worker_state_snapshots["fused-worker"]
    assert "requested_value: True" in state.markdown
    assert state.task_transition is not None
    assert state.task_transition.status == "complete"


@pytest.mark.parametrize(
    ("current_url", "expected_actions", "expected_event"),
    [
        ("https://example.test/login", 3, "worker_multi_action_completed"),
        ("https://example.test/dashboard", 1, "worker_multi_action_aborted"),
    ],
)
def test_fused_worker_executes_ordered_actions_and_discards_invalid_suffix(
    monkeypatch,
    current_url: str,
    expected_actions: int,
    expected_event: str,
) -> None:
    runtime = _run_fused_worker(monkeypatch, current_url=current_url)

    assert runtime.worker.calls == 1
    assert "continue_with_actions" in runtime.worker.bound_names
    assert all(
        name in runtime.worker.bound_schemas[-1]
        for name in ("enter_username", "enter_password", "submit_login")
    )
    assert len(runtime._executor.actions) == expected_actions
    assert len(runtime.screenshot_calls) == (2 if expected_actions == 3 else 1)
    assert any(event["event"] == expected_event for event in runtime.trace)
    if expected_actions == 3:
        assert any(status.startswith("Action · 2/3 · type") for status in runtime.statuses)


def test_fused_worker_dispatches_public_deep_navigation_without_semantic_review(
    monkeypatch,
) -> None:
    worker = _MultiActionWorker([[{
            "name": "open_replacement",
            "args": {"url": "https://replacement.example.test/deep/path"},
        }]])

    runtime = _run_fused_worker(
        monkeypatch,
        current_url="https://active.example.test/current",
        worker=worker,
        actions=[DynamicActionSpec(
            name="open_replacement",
            capability="open_url",
            description="Open the replacement reference",
        )],
    )

    assert worker.calls == 1
    assert len(runtime._executor.actions) == 1
    assert runtime._executor.actions[0].action_type == "navigate"
    assert not any(
        event["event"] == "worker_same_frame_action_repair"
        for event in runtime.trace
    )


def test_fused_worker_returns_typed_failure_after_repeated_empty_action_envelope(
    monkeypatch,
) -> None:
    worker = _MultiActionWorker([[], []])

    runtime = _run_fused_worker(
        monkeypatch,
        current_url="https://example.test/items",
        worker=worker,
    )

    assert runtime.outcome.phase == "failed"
    assert runtime.outcome.steps == 0
    assert "action envelope must contain" in runtime.outcome.summary
    assert len(runtime._executor.actions) == 0
    assert worker.calls == 2
    assert any(
        "between 1 and 5 executable actions" in str(message.content)
        and "action envelope must contain" in str(message.content)
        for message in worker.messages
        if getattr(message, "type", "") == "human"
    )


def test_worker_repairs_rejected_launch_app_without_reobserving(monkeypatch) -> None:
    exact_app = "com.android.settings/.HWSettings"
    worker = _MultiActionWorker([
        [{"name": "open_settings", "args": {"app": "Settings"}}],
        [{"name": "open_settings", "args": {"app": exact_app}}],
    ])
    runtime = _run_fused_worker(
        monkeypatch, current_url="", worker=worker,
        installed_apps=(exact_app,),
        actions=[DynamicActionSpec(
            name="open_settings",
            capability="launch_app",
            description="Open system settings",
            exposed_args=["app"],
        )],
        bundle=SimpleNamespace(
            make_action=lambda payload: AndroidAction.model_validate(payload),
        ),
    )

    assert runtime.observe_calls == 1
    assert worker.calls == 2
    assert [action.app for action in runtime._executor.actions] == [exact_app]
    assert any(
        event["event"] == "worker_same_frame_action_repair"
        for event in runtime.trace
    )


def test_actor_protocol_rejects_state_annotations(monkeypatch) -> None:
    worker = _MultiActionWorker()
    worker.actor_state = {
        "status": "executing", "summary": "Actor-authored state is invalid.",
    }

    runtime = _run_fused_worker(
        monkeypatch,
        current_url="https://example.test/login",
        worker=worker,
    )

    assert runtime.outcome.failure_kind == "protocol_invalid"
    assert runtime.observe_calls == 1
    assert worker.calls == 2
    assert len(runtime._executor.actions) == 0


def test_worker_repairs_home_then_launch_app_before_dispatch(monkeypatch) -> None:
    worker = _MultiActionWorker([
        [
            {"name": "go_home", "args": {}},
            {"name": "open_messages", "args": {}},
        ],
        [{"name": "open_messages", "args": {}}],
    ])
    runtime = _run_fused_worker(
        monkeypatch,
        current_url="",
        worker=worker,
        installed_apps=("Messages",),
        actions=[
            DynamicActionSpec(name="go_home", capability="home", description="Go home"),
            DynamicActionSpec(
                name="open_messages",
                capability="launch_app",
                description="Open Messages",
                fixed_args={"app": "Messages"},
            ),
        ],
    )

    assert runtime.observe_calls == 1
    assert worker.calls == 2
    assert [action.action_type for action in runtime._executor.actions] == ["launch_app"]
    assert any(event["event"] == "worker_protocol_error" for event in runtime.trace)


@pytest.mark.parametrize("code", ["757570", "580954"])
def test_explicit_auth_fact_allows_code_entry_across_frames(
    monkeypatch, code: str,
) -> None:
    action = _code_action(code)
    worker = _MultiActionWorker([[action]])
    journal = None
    if code == "757570":
        worker.state_target_identity = f"The visible verification code is {code}."
        action["args"]["state_target_ref"] = "test_target"
    else:
        journal = WorkerJournal(worker_id="fused-worker")
        journal.record_runtime_input(
            key="verification_code",
            event_ref="runtime-input:verification-code",
            statement=f"验证码为{code}",
        )

    runtime = _run_fused_worker(
        monkeypatch, current_url="", worker=worker, journal=journal,
        actions=[_code_spec()],
    )

    assert [action.text for action in runtime._executor.actions] == [code]


def test_auth_code_in_summary_alone_remains_blocked(monkeypatch) -> None:
    action = _code_action()
    worker = _MultiActionWorker([[action], [action]])
    worker.state_summary = "The visible verification code is 757570."

    runtime = _run_fused_worker(
        monkeypatch,
        current_url="",
        worker=worker,
        actions=[_code_spec()],
    )

    assert worker.calls == 2
    assert runtime._executor.actions == []
    assert runtime.outcome.failure_kind == "action_contract_invalid"


@pytest.mark.parametrize(
    "capability",
    ["back", "home", "app_switch", "launch_app", "open_url", "scroll", "drag"],
)
def test_geometry_or_surface_changing_action_must_be_last_in_batch(capability: str) -> None:
    args = {
        "launch_app": {"app": "Messages"},
        "open_url": {"url": "https://example.test/"},
        "drag": {
            "x": 500,
            "y": 700,
            "to_x": 500,
            "to_y": 300,
            "description": "Drag the visible item upward",
        },
    }.get(capability, {})
    actions = [
        DynamicActionSpec(name="change_surface", capability=capability, description="Change surface"),
        DynamicActionSpec(name="tap", capability="tap", description="Tap visible control"),
    ]
    calls = [
        {"name": "change_surface", "args": args},
        {"name": "tap", "args": {"x": 500, "y": 500}},
    ]

    with pytest.raises(ProtocolError, match="must be the final batch action"):
        ToolAgentRuntime._validate_multi_action_calls(calls, actions)

    ToolAgentRuntime._validate_multi_action_calls(list(reversed(calls)), actions)


def test_action_suffix_rebinds_after_layout_change() -> None:
    controls = [
        {"kind": kind, "ref": ref, "rect": {"x": 500, "y": y, "w": 500, "h": 60}}
        for kind, ref, y in (
            ("text_input", "username", 400), ("button", "submit", 600),
        )
    ]
    shifted = [
        {**item, "rect": {**item["rect"], "y": item["rect"]["y"] - 50}}
        for item in controls
    ]
    runtime = object.__new__(ToolAgentRuntime)
    runtime._executor = SimpleNamespace(refresh_controls=lambda: shifted)
    tap = DynamicActionSpec(name="tap", capability="tap", description="Tap")
    type_action = DynamicActionSpec(
        name="type", capability="type", description="Type", exposed_args=["text"],
    )
    remaining = [
        {"name": "type", "args": {"x": 500, "y": 400, "text": "user"}},
        {"name": "tap", "args": {"x": 500, "y": 600}},
    ]
    frame = MaterializedFrame(
        frame_id="frame:login", screenshot_path="login.png", controls=controls,
    )
    for call, ref in zip(remaining, ("username", "submit"), strict=True):
        call["_control_ref"] = ref

    assert runtime._refresh_next_action(remaining[0], type_action, frame)
    assert runtime._refresh_next_action(remaining[1], tap, frame)
    assert [call["args"]["y"] for call in remaining] == [350, 550]


def test_multi_action_runtime_accepts_five_and_rejects_six_calls() -> None:
    actions = [DynamicActionSpec(
        name="tap",
        capability="tap",
        description="Tap a target",
    )]
    calls = [{
        "name": "tap",
        "args": {"x": 100, "y": 100, "description": "Visible test button"},
    }] * MAX_ORDERED_ACTIONS

    ToolAgentRuntime._validate_multi_action_calls(calls, actions)
    with pytest.raises(ProtocolError, match=f"1–{MAX_ORDERED_ACTIONS} actions"):
        ToolAgentRuntime._validate_multi_action_calls([*calls, calls[0]], actions)


def test_worker_rejects_invalid_state_trace_without_executing(monkeypatch) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime.worker = _MissingStateWorker()
    runtime._executor = _Executor()
    runtime.platform = object()
    runtime._observe = lambda spec: (
        MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png"),
        b"png",
    )
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, **_kwargs: (0.0, False),
    )
    spec = _worker_spec(
        goal="Advance one cohesive subgoal",
        success_criteria=["The visible control is activated"],
        actions=[DynamicActionSpec(
            name="activate_visible_control",
            capability="tap",
            description="Activate the visible control",
        )],
        max_steps=1,
    )

    _install_test_worker_contract(runtime, spec)
    outcome = runtime._run_worker("advance_subgoal", spec)

    assert outcome.phase == "failed"
    assert outcome.failure_kind == "protocol_invalid"
    assert runtime._executor.actions == []
    assert len([
        event for event in runtime.trace
        if event["event"] == "worker_state_protocol_error"
    ]) == 2


def test_replacement_reflection_starts_with_fresh_journal(monkeypatch) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime.worker = _EmptyContentWorker()
    runtime._executor = _Executor()
    runtime.platform = object()
    runtime._observe = lambda spec: (
        MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png"),
        b"png",
    )
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, **_kwargs: (0.0, False),
    )
    spec = _worker_spec(
        goal="Advance one cohesive subgoal",
        success_criteria=["The visible control is activated"],
        actions=[DynamicActionSpec(
            name="activate_visible_control",
            capability="tap",
            description="Activate the visible control",
        )],
        max_steps=1,
    )

    _install_test_worker_contract(runtime, spec)
    first = runtime._run_worker("advance_subgoal", spec)
    runtime._frame_no = 0
    second = runtime._run_worker("advance_subgoal_reflection_1", spec)

    assert first.phase == second.phase == "failed"
    starts = [event for event in runtime.trace if event["event"] == "worker_started"]
    assert [event["retained_memory_events"] for event in starts] == [0, 0]
    decisions = [event for event in runtime.trace if event["event"] == "worker_decision"]
    assert [event["memory_event_count"] for event in decisions] == [0, 0]
    assert set(runtime._worker_journals) == {
        "advance_subgoal",
        "advance_subgoal_reflection_1",
    }
    assert runtime._active_worker_journal() is runtime._worker_journals[
        "advance_subgoal_reflection_1"
    ]


def test_replacement_reflection_inherits_only_runtime_task_memory() -> None:
    base = WorkerJournal(worker_id="worker")
    base.record_runtime_input(
        key="authorized_destination",
        statement="The authoritative destination is the requested collection",
    )
    base.record_action_result(
        step=1,
        frame_id="frame:1",
        tool="scroll",
        args={"direction": "down"},
        result={"status": "executed", "action_type": "scroll", "no_effect": True},
    )
    retry = WorkerJournal(worker_id="worker_reflection_1")

    ToolAgentRuntime._inherit_task_memory(
        {"worker": base, "worker_reflection_1": retry},
        retry,
        retry.worker_id,
    )

    assert len(retry.events) == 1
    assert retry.events[0].origin == "runtime"
    assert retry.events[0].lifetime == "task"
    assert retry.events[0].key == "authorized_destination"


def test_reflected_attempt_preserves_same_progress_journal() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    journal = WorkerJournal(worker_id="logical_worker")
    journal.record_runtime_input(
        key="verified_target", statement="The target identity is verified",
    )
    runtime._worker_journals = {"logical_worker": journal}
    frame = MaterializedFrame(frame_id="frame:7", screenshot_path="frame.png")
    runtime._worker_last_frames = {"logical_worker": frame}

    runtime._preserve_progress_for_reflected_attempt(
        current_worker_id="logical_worker",
        next_worker_id="logical_worker_reflection_1",
    )

    assert runtime._worker_journals["logical_worker_reflection_1"] is journal
    assert runtime._worker_last_frames["logical_worker_reflection_1"] is frame


def test_worker_normalizes_provider_point_schema_and_executes_type(monkeypatch) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime.worker = _ArrayCoordinateWorker()
    runtime._executor = _Executor()
    runtime.platform = object()
    runtime._observe = lambda spec: (
        MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png"),
        b"png",
    )
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, **_kwargs: (0.0, False),
    )
    spec = _worker_spec(
        goal="Enter a required value",
        success_criteria=["The value is entered"],
        actions=[DynamicActionSpec(
            name="enter_value",
            capability="type",
            description="Enter the required value",
            exposed_args=["text"],
        )],
        max_steps=1,
    )

    _install_test_worker_contract(runtime, spec)
    runtime._run_worker("type_value", spec)

    assert len(runtime._executor.actions) == 1
    action = runtime._executor.actions[0]
    assert action.action_type == "type"
    assert action.x == 200
    assert action.y == 380
    assert action.text == "01/01/2023"
    decision = next(
        event for event in runtime.trace if event["event"] == "worker_decision"
    )
    assert decision["args"]["x"] == 200
    assert decision["args"]["y"] == 380


def test_worker_allows_repeated_action_without_history_based_blocking(
    monkeypatch,
) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime.worker = _RepeatedThenGroundedWorker()
    runtime._executor = _GroundingExecutor()
    runtime._visualizer = _Visualizer()
    runtime.platform = object()
    observed = []

    def observe(spec):
        del spec
        observed.append(True)
        return (
            MaterializedFrame(
                frame_id=f"frame:{len(observed)}",
                screenshot_path="frame.png",
                url="http://example.test/orders",
                title="Orders",
                controls=[{
                    "kind": "text_input",
                    "label": "to",
                    "id": "E1WHE5T",
                    "rect": {"x": 212, "y": 428, "w": 184, "h": 32},
                }],
                requirement_scopes={
                    "orders": {"status": "unmet", "applied_filters": {}},
                },
            ),
            _TEST_PNG,
        )

    runtime._observe = observe
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, **_kwargs: (0.0, False),
    )
    spec = _worker_spec(
        goal="Set the order end date",
        success_criteria=["The end date is set"],
        actions=[DynamicActionSpec(
            name="enter_end_date",
            capability="type",
            description="Enter the order end date",
            exposed_args=["text"],
        )],
        max_steps=2,
    )

    _install_test_worker_contract(runtime, spec)
    outcome = runtime._run_worker("ground_date", spec)

    assert outcome.phase == "failed"
    assert len(observed) == 2
    assert runtime.worker.calls == 2
    assert len(runtime._executor.actions) == 2
    assert (runtime._executor.actions[-1].x, runtime._executor.actions[-1].y) == (
        212,
        428,
    )
    assert runtime._executor.actions[-1].snap["method"] == "control_semantic_geometry"
    assert runtime._visualizer.points[-2:] == [
        (212.0, 428.0),
        (212.0, 428.0),
    ]
    assert not any(
        event["event"] == "worker_action_rejected" for event in runtime.trace
    )


def test_worker_allows_effective_scrolls_until_bounded_step_limit(
    monkeypatch,
) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime.worker = _RepeatedEffectiveScrollWorker()
    runtime._executor = _Executor()
    runtime.platform = object()
    observe_calls = []

    def observe(spec):
        del spec
        observe_calls.append(True)
        return (
            MaterializedFrame(
                frame_id=f"frame:{len(observe_calls)}",
                screenshot_path="frame.png",
            ),
            _TEST_PNG,
        )

    runtime._observe = observe
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, **_kwargs: (0.0, False),
    )
    spec = _worker_spec(
        goal="Collect a long visual surface",
        success_criteria=["All relevant visible records are collected"],
        actions=[DynamicActionSpec(
            name="reveal_more",
            capability="scroll",
            description="Reveal more content",
            fixed_args={"direction": "down"},
            exposed_args=["amount"],
        )],
        max_steps=3,
    )

    _install_test_worker_contract(runtime, spec)
    outcome = runtime._run_worker("visual_collection", spec)

    assert outcome.phase == "failed"
    assert len(observe_calls) == runtime.worker.calls == 3
    assert len(runtime._executor.actions) == 3
    assert not any(
        event["event"] == "worker_action_rejected" for event in runtime.trace
    )


def test_vision_only_execution_does_not_use_enhanced_control_geometry(
    monkeypatch,
) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.perception_mode = "vision-only"
    runtime._executor = _GroundingExecutor()
    runtime._visualizer = None
    runtime.platform = object()
    runtime._trace = lambda *_args, **_kwargs: None
    settle_calls = []
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, **kwargs: settle_calls.append(kwargs) or (0.0, False),
    )
    action = DynamicActionSpec(
        name="enter_visible_value",
        capability="type",
        description="Enter the value into the visible input",
        exposed_args=["text", "description"],
    )
    spec = _worker_spec(
        goal="Enter a visible value",
        success_criteria=["The value is entered"],
        actions=[action],
    )
    frame = MaterializedFrame(
        frame_id="frame:1",
        screenshot_path="frame.png",
        controls=[{
            "kind": "text_input",
            "label": "to",
            "rect": {"x": 212, "y": 428, "w": 184, "h": 32},
        }],
    )

    runtime._execute_worker_tool(
        spec,
        [action],
        {
            "name": "enter_visible_value",
            "args": {
                "x": 207,
                "y": 448,
                "text": "05/31/2023",
                "description": "Enter the end date into the visible to input",
            },
        },
        b"png",
        frame,
    )

    executed = runtime._executor.actions[-1]
    assert (executed.x, executed.y) == (207, 448)
    assert executed.snap is None
    assert settle_calls == [{"action_type": "type", "focus_y": 448.0, "center": None}]


def test_worker_action_returns_flash_off_target_signal(monkeypatch) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.bundle = SimpleNamespace(
        make_action=lambda payload: AndroidAction.model_validate(payload)
    )
    runtime.perception_mode = "enhanced"
    runtime._executor = _Executor()
    runtime._visualizer = None
    runtime.platform = object()
    runtime.trace = []
    runtime._target_verify_pool = _ImmediateVerifyPool(TargetVerify(
        on_target=False,
        actual_element="Browse Channels",
        reason="The marker is on the adjacent row.",
    ))
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, **_kwargs: (0.0, False),
    )
    action = DynamicActionSpec(
        name="open_named_menu_item",
        capability="tap",
        description="Tap a visible control",
    )
    spec = _worker_spec(
        goal="Open the named menu item",
        success_criteria=["The named menu item opens"],
        actions=[action],
    )

    payload, terminal = runtime._execute_worker_tool(
        spec,
        [action],
        {
            "name": action.name,
            "args": {
                "x": 500,
                "y": 870,
                "description": "Tap Create New Channel",
            },
        },
        b"png",
        MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png"),
    )

    assert terminal is None
    assert payload["target_signal"] == {
        "status": "off_target",
        "actual_element": "Browse Channels",
        "reason": "The marker is on the adjacent row.",
    }
    submitted = runtime._target_verify_pool.submitted
    assert len(submitted) == 1
    assert submitted[0][1] == (
        b"png", 500.0, 870.0, "Tap Create New Channel",
    )


def test_visual_grounding_snaps_type_to_high_confidence_field_center(
    monkeypatch,
) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.bundle = SimpleNamespace(
        make_action=lambda payload: AndroidAction.model_validate(payload)
    )
    runtime.perception_mode = "enhanced"
    runtime._executor = _Executor()
    runtime._visualizer = None
    runtime.platform = object()
    runtime.trace = []
    runtime._target_ground_pool = _ImmediateVerifyPool(TargetGrounding(
        target_found=True,
        target_box=(210, 105, 790, 150),
        control_type="text_input",
        label="收货人姓名",
        confidence="high",
        reason="The editable value area is clearly visible.",
    ))
    runtime._target_verify_pool = None
    settle_calls = []
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, **kwargs: settle_calls.append(kwargs) or (0.0, False),
    )
    action = DynamicActionSpec(
        name="enter_recipient",
        capability="type",
        description="Enter recipient name",
        exposed_args=["text"],
    )
    spec = _worker_spec(
        goal="Fill the recipient form",
        success_criteria=["Recipient is filled"],
        actions=[action],
    )

    payload, terminal = runtime._execute_worker_tool(
        spec,
        [action],
        {
            "name": action.name,
            "args": {
                "x": 500,
                "y": 160,
                "text": "张先生",
                "description": "在收货人姓名输入框输入张先生",
            },
        },
        b"png",
        MaterializedFrame(frame_id="frame:address", screenshot_path="address.png"),
    )

    assert terminal is None
    executed = runtime._executor.actions[-1]
    assert (executed.x, executed.y) == (500, 127.5)
    assert executed.snap["method"] == "visual_target_grounding"
    assert executed.snap["original"] == [500.0, 160.0]
    assert payload["target_signal"]["status"] == "on_target"
    assert payload["target_signal"]["actual_element"] == "收货人姓名"
    assert settle_calls == [{"action_type": "type", "focus_y": 127.5, "center": None}]
    entry = next(
        item for item in runtime.trace
        if item["event"] == "worker_target_grounding"
    )
    assert runtime._human_line(entry).startswith(
        "Grounding  : high · 收货人姓名 · point outside"
    )


def test_type_is_rejected_before_dispatch_when_target_is_off_target(monkeypatch) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.bundle = SimpleNamespace(
        make_action=lambda payload: AndroidAction.model_validate(payload)
    )
    runtime.perception_mode = "enhanced"
    runtime._executor = _Executor()
    runtime._visualizer = None
    runtime.platform = SimpleNamespace(
        screenshot=lambda: b"latest",
        client=SimpleNamespace(page_info=lambda: ("", "")),
    )
    runtime.trace = []
    runtime._target_ground_pool = _ImmediateVerifyPool(TargetGrounding(
        target_found=True,
        target_box=(400, 400, 600, 500),
        control_type="text_input",
        label="Username",
        confidence="medium",
        reason="The candidate point missed the Password field.",
    ))
    runtime._target_verify_pool = None
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, **_kwargs: (0.0, False),
    )
    type_action = DynamicActionSpec(
        name="enter_password",
        capability="type",
        description="Type into a visible input",
        exposed_args=["text"],
    )
    tap_action = DynamicActionSpec(
        name="submit_login",
        capability="tap",
        description="Tap a visible button",
    )
    spec = _worker_spec(
        goal="Complete the login form",
        success_criteria=["The login form is submitted"],
        actions=[type_action, tap_action],
    )
    journal = WorkerJournal(worker_id="login")
    calls = [
        {
            "name": type_action.name,
            "args": {
                "x": 500,
                "y": 600,
                "text": "secret",
                "description": "Password text input",
            },
        },
        {
            "name": tap_action.name,
            "args": {
                "x": 500,
                "y": 750,
                "description": "Log In button",
            },
        },
    ]

    payload, terminal = runtime._execute_multi_action_calls(
        worker_id="login",
        spec=spec,
        actions=[type_action, tap_action],
        calls=calls,
        state=WorkerStateSnapshot(
            summary="The login form is visible.",
        ),
        step=1,
        frame=MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png"),
        png=b"png",
        journal=journal,
    )

    assert terminal is None
    assert payload["status"] == "aborted"
    assert payload["executed_actions"] == 0
    assert payload["reuse_current_frame"] is True
    assert payload["_memory_commit_safe"] is False
    assert "predispatch visual grounding" in payload["reason"]
    assert len(runtime._executor.actions) == 0


def test_multi_action_validates_every_observed_target_before_dispatch(monkeypatch) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.bundle = SimpleNamespace(
        make_action=lambda payload: BrowserAction.model_validate(payload)
    )
    runtime.perception_mode = "vision-only"
    runtime._executor = _Executor()
    runtime._visualizer = None
    runtime.platform = SimpleNamespace(
        screenshot=lambda: b"latest",
        client=SimpleNamespace(page_info=lambda: ("", "")),
    )
    runtime.trace = []
    runtime._target_ground_pool = None
    runtime._target_verify_pool = None
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, **_kwargs: (0.0, False),
    )
    first = DynamicActionSpec(
        name="activate_open_target",
        capability="tap",
        description="Activate the visible target",
    )
    second = DynamicActionSpec(
        name="activate_unknown_target",
        capability="tap",
        description="Activate an unobserved target",
    )
    spec = _worker_spec(
        goal="Resolve all visible targets",
        success_criteria=["Every target has the requested state"],
        actions=[first, second],
    )
    state = WorkerStateSnapshot.model_validate({
        "summary": "Two target facts are visible.",
        "targets": {
            "open_target": {
                "identity": "Open target",
                "visibility": "full",
                "owned_region_visibility": "unobscured",
            },
            "done_target": {
                "identity": "Done target",
                "visibility": "full",
                "owned_region_visibility": "unobscured",
            },
        },
        "markdown": "### done_target\n- Requested state: true",
    })
    calls = [
        {
            "name": first.name,
            "args": {"x": 300, "y": 500, "description": first.description},
            "_state_target_ref": "open_target",
        },
        {
            "name": second.name,
            "args": {"x": 700, "y": 500, "description": second.description},
            "_state_target_ref": "unknown_target",
        },
    ]

    payload, terminal = runtime._execute_multi_action_calls(
        worker_id="targets",
        spec=spec,
        actions=[first, second],
        calls=calls,
        state=state,
        step=1,
        frame=MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png"),
        png=b"png",
        journal=WorkerJournal(worker_id="targets"),
    )

    assert terminal is None
    assert payload["status"] == "aborted"
    assert payload["executed_actions"] == 1
    assert "no observed target" in payload["reason"]
    assert len(runtime._executor.actions) == 1
    assert runtime._executor.execute_kwargs[0]["target_control"] == "Open target"


def test_runtime_recovers_missing_exposed_action_description(monkeypatch) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.bundle = SimpleNamespace(
        make_action=lambda payload: BrowserAction.model_validate(payload)
    )
    runtime.perception_mode = "enhanced"
    runtime._executor = _Executor()
    runtime._visualizer = None
    runtime.platform = object()
    runtime._trace = lambda *_args, **_kwargs: None
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, **_kwargs: (0.0, False),
    )
    action = DynamicActionSpec(
        name="reveal_required_detail",
        capability="scroll",
        description="Scroll the main content to reveal the required detail",
        exposed_args=["direction", "amount", "target_area", "description"],
    )
    spec = _worker_spec(
        goal="Reveal the required detail",
        success_criteria=["The detail is visible"],
        actions=[action],
    )

    payload, terminal = runtime._execute_worker_tool(
        spec,
        [action],
        {"name": action.name, "args": {"direction": "down"}},
        b"png",
        MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png"),
    )

    assert payload["status"] == "executed"
    assert terminal is None
    assert runtime._executor.actions[-1].description == action.description


@pytest.mark.parametrize(
    ("capability", "args", "action_type"),
    [
        ("open_url", {"url": "https://example.test/reviews"}, "navigate"),
        ("back", {}, "back"),
        ("clear_text", {}, "clear_text"),
        ("press_enter", {}, "press_enter"),
    ],
)
def test_runtime_executes_nonspatial_browser_capabilities_through_adapter_action(
    monkeypatch,
    capability: str,
    args: dict[str, str],
    action_type: str,
) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.bundle = SimpleNamespace(
        make_action=lambda payload: BrowserAction.model_validate(payload)
    )
    runtime.perception_mode = "enhanced"
    runtime._executor = _Executor()
    runtime._visualizer = None
    runtime.platform = object()
    runtime._trace = lambda *_args, **_kwargs: None
    runtime._task_goal = str(args.get("url") or "Advance the browser subgoal")
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, **_kwargs: (0.0, False),
    )
    action = DynamicActionSpec(
        name=f"do_{capability}",
        capability=capability,
        description=f"Execute {capability} for the current subgoal",
        exposed_args=list(args),
    )
    spec = _worker_spec(
        goal="Advance the browser subgoal",
        success_criteria=["The browser state advances"],
        actions=[action],
    )

    payload, terminal = runtime._execute_worker_tool(
        spec,
        [action],
        {"name": action.name, "args": args},
        b"png",
        MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png"),
    )

    executed = runtime._executor.actions[-1]
    assert isinstance(executed, BrowserAction)
    assert executed.action_type == action_type
    assert getattr(executed, "url", None) == args.get("url")
    assert payload["status"] == "executed"
    assert terminal is None


@pytest.mark.parametrize(
    ("capability", "args"),
    [
        ("home", {}),
        ("app_switch", {}),
        ("launch_app", {"app": "Calendar"}),
        (
            "drag",
            {
                "x": 200,
                "y": 500,
                "to_x": 800,
                "to_y": 500,
                "description": "Drag the visible slider to the right edge",
            },
        ),
        (
            "long_press",
            {
                "x": 400,
                "y": 600,
                "duration_ms": 700,
                "description": "Long-press the visible file row",
            },
        ),
    ],
)
def test_runtime_executes_android_device_capabilities(
    monkeypatch,
    capability: str,
    args: dict[str, object],
) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.bundle = SimpleNamespace(
        platform="android",
        make_action=lambda payload: AndroidAction.model_validate(payload),
    )
    runtime._installed_app_names = ("Calendar", "Settings")
    runtime.perception_mode = "enhanced"
    runtime._executor = _Executor()
    runtime._visualizer = None
    runtime.platform = SimpleNamespace(client=SimpleNamespace())
    runtime._trace = lambda *_args, **_kwargs: None
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, **_kwargs: (0.0, False),
    )
    action = DynamicActionSpec(
        name=f"do_{capability}",
        capability=capability,
        description=f"Execute {capability} for the current Android subgoal",
        exposed_args=list(args),
    )
    spec = _worker_spec(
        goal="Advance the Android subgoal",
        success_criteria=["The Android state advances"],
        actions=[action],
    )

    payload, terminal = runtime._execute_worker_tool(
        spec,
        [action],
        {"name": action.name, "args": args},
        b"png",
        MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png"),
    )

    executed = runtime._executor.actions[-1]
    assert isinstance(executed, AndroidAction)
    assert executed.action_type == capability
    assert getattr(executed, "app", None) == args.get("app")
    assert payload["status"] == "executed"
    assert terminal is None


def test_runtime_rejects_launching_an_unlisted_android_app() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.bundle = SimpleNamespace(
        platform="android",
        make_action=lambda payload: AndroidAction.model_validate(payload),
    )
    runtime._installed_app_names = ("Calendar",)
    runtime._executor = _Executor()
    runtime._visualizer = None
    runtime.platform = SimpleNamespace(client=SimpleNamespace())
    action = DynamicActionSpec(
        name="open_settings",
        capability="launch_app",
        description="Open the Settings application",
        fixed_args={"app": "Settings"},
    )
    spec = _worker_spec(
        goal="Open Settings",
        success_criteria=["Settings is visible"],
        actions=[action],
    )

    with pytest.raises(ValueError, match="Runtime-provided application name"):
        runtime._execute_worker_tool(
            spec,
            [action],
            {"name": action.name, "args": {}},
            b"png",
            MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png"),
        )


def test_runtime_ask_user_records_authoritative_task_evidence() -> None:
    questions = []
    runtime = object.__new__(ToolAgentRuntime)
    runtime.bundle = SimpleNamespace(
        platform="android",
        request_user_input=lambda question: (
            questions.append(question) or "Documents/expense/invoice"
        ),
    )
    runtime._trace = lambda *_args, **_kwargs: None
    action = DynamicActionSpec(
        name="ask_user",
        capability="ask_user",
        description="Ask for missing user-owned information",
        exposed_args=["question"],
    )
    spec = _worker_spec(
        goal="Use the user's destination",
        success_criteria=["The target is handled using the supplied destination"],
        actions=[action],
    )

    payload, terminal = runtime._execute_worker_tool(
        spec,
        [action],
        {
            "name": "ask_user",
            "args": {"question": "Which destination folder should I use?"},
        },
        b"png",
        MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png"),
    )
    journal = WorkerJournal(worker_id="ask_user_worker")
    journal.record_action_result(
        step=1,
        frame_id="frame:1",
        tool="ask_user",
        args={"question": questions[0]},
        result=payload,
    )
    journal.record_runtime_result(step=1, result=payload)

    facts = journal.active_fact_statements(frame_id="frame:2")
    task_events = [event for event in journal.events if event.lifetime == "task"]
    assert terminal is None
    assert payload["action_type"] == "ask_user"
    assert questions == ["Which destination folder should I use?"]
    assert len(task_events) == 1
    assert "Documents/expense/invoice" in facts[0]


def _browser_execution_runtime(
    monkeypatch,
    feedback_reader,
    *,
    worker_id: str,
) -> ToolAgentRuntime:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.bundle = SimpleNamespace(
        make_action=lambda payload: BrowserAction.model_validate(payload)
    )
    runtime.perception_mode = "enhanced"
    runtime._executor = _Executor()
    runtime._visualizer = runtime._target_verify_pool = None
    runtime._active_worker_id = worker_id
    runtime.platform = SimpleNamespace(client=SimpleNamespace(
        consume_action_feedback=feedback_reader,
    ))
    runtime._trace = lambda *_args, **_kwargs: None
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, **_kwargs: (0.0, False),
    )
    return runtime


def test_runtime_ignores_provider_echo_of_fixed_action_argument(monkeypatch) -> None:
    runtime = _browser_execution_runtime(
        monkeypatch, lambda: [], worker_id="search_worker",
    )
    action = DynamicActionSpec(
        name="enter_query",
        capability="type",
        description="Enter the fixed query",
        fixed_args={"text": "authoritative query"},
    )
    spec = _worker_spec(
        goal="Search for the requested information",
        success_criteria=["Relevant results are visible"],
        actions=[action],
    )

    payload, terminal = runtime._execute_worker_tool(
        spec,
        [action],
        {"name": action.name, "args": {
            "x": 500,
            "y": 400,
            "description": "Visible search field",
            "text": "provider echo",
        }},
        b"png",
        MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png"),
    )

    assert terminal is None
    assert payload["status"] == "executed"
    assert runtime._executor.actions[-1].text == "authoritative query"


def test_runtime_keeps_page_request_rejection_observational(monkeypatch) -> None:
    rejection_feedback = [{
        "kind": "xhr",
        "url": "https://example.test/action",
        "status": 200,
        "body": '{"error":true,"message":"The action is not allowed."}',
    }]
    feedback = iter((rejection_feedback, []))
    runtime = _browser_execution_runtime(
        monkeypatch, lambda: next(feedback), worker_id="submit_worker",
    )
    action = DynamicActionSpec(
        name="submit_change",
        capability="tap",
        description="Submit the requested change",
    )
    spec = _worker_spec(
        goal="Submit the requested change",
        success_criteria=["The change is accepted"],
        actions=[action],
    )

    payload, terminal = runtime._execute_worker_tool(
        spec,
        [action],
        {"name": action.name, "args": {"x": 500, "y": 500}},
        b"png",
        MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png"),
    )

    assert payload["status"] == "executed"
    assert payload["platform_feedback"] == [{
        "status": 200,
        "url": "https://example.test/action",
        "rejected": False,
        "message": "The action is not allowed.",
    }]
    assert terminal is None


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, '{"error":true,"message":"Not allowed"}'),
        (404, '{"errors":[{"error":{"message":"Not found"}}]}'),
    ],
)
def test_page_request_errors_are_observational(status: int, body: str) -> None:
    feedback = [{"kind": "fetch", "status": status, "body": body}]

    assert _action_feedback(feedback, "scroll") == []
    assert _action_feedback(feedback, "tap")[0]["rejected"] is False


def test_failed_transport_action_skips_settle_and_returns_control(monkeypatch) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.bundle = SimpleNamespace(
        make_action=lambda payload: BrowserAction.model_validate(payload)
    )
    runtime._executor = SimpleNamespace(
        execute=lambda _decision, *, png_bytes: False,
    )
    runtime._visualizer = None
    runtime._target_verify_pool = None
    runtime._active_worker_id = "navigation_worker"
    runtime._validate_runtime_open_url = lambda *_args, **_kwargs: None
    runtime.platform = SimpleNamespace(
        client=SimpleNamespace(consume_action_feedback=lambda: [{
            "kind": "navigation",
            "url": "https://example.test/",
            "status": 0,
            "body": '{"error":true,"message":"navigation timed out"}',
        }])
    )
    runtime._trace = lambda *_args, **_kwargs: None
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("failed actions must not settle")
        ),
    )
    action = DynamicActionSpec(
        name="open_page",
        capability="open_url",
        description="Open the requested page",
        fixed_args={"url": "https://example.test/"},
    )
    spec = _worker_spec(
        profile="operator",
        goal="Activate the requested state",
        success_criteria=["The requested state is visible"],
        actions=[action],
    )
    frame = MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png")

    payload, terminal = runtime._execute_worker_tool(
        spec,
        [action],
        {"name": action.name, "args": {}},
        b"unchanged",
        frame,
    )
    assert terminal == "navigation_blocked"
    assert payload["status"] == "failed"
    assert payload["reason"] == "navigation timed out"
    assert payload["platform_feedback"][0]["message"] == "navigation timed out"
    assert payload["settle_seconds"] == 0.0
    assert payload["no_effect"] is True

    outcome = WorkerOutcome(
        phase="failed",
        summary=payload["platform_feedback"][0]["message"],
        failure_kind="navigation_blocked",
        steps=1,
    )
    assert Reflector.route(outcome) == "replace"


def test_timed_out_target_verification_is_cancelled() -> None:
    cancelled = []
    future = SimpleNamespace(
        result=lambda **_kwargs: (_ for _ in ()).throw(TimeoutError()),
        cancel=lambda: cancelled.append(True),
    )
    signal, error = _target_verification_result(future)

    assert signal is None
    assert isinstance(error, TimeoutError)
    assert cancelled == [True]

def test_collector_completion_binds_accumulated_rows_regardless_of_coverage() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.data_store = RuntimeDataStore()
    _, collection, _ = runtime.data_store.put_chunk(
        requirement_id="records",
        frame_id="frame:1",
        provider="structured",
        rows=[{"value": "partial"}],
        row_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        coverage={
            "source_scope": "structured_surface",
            "window_key": "page:1",
            "page_index": 1,
            "page_count": 2,
            "has_next_page": True,
            "at_end": False,
            "partial": True,
        },
    )
    spec = _worker_spec(
        profile="collector",
        goal="Collect all records",
        success_criteria=["Collection coverage is complete"],
        data_requirements=[{
            "id": "records",
            "description": "Collect all records",
            "row_schema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        }],
        actions=[DynamicActionSpec(
            name="reveal_more",
            capability="scroll",
            description="Reveal more collected records",
            fixed_args={"direction": "down"},
        )],
    )

    frame = MaterializedFrame(
        frame_id="frame:1",
        screenshot_path="frame.png",
        collections=[collection],
        requirement_scopes={"records": {"status": "met"}},
    )
    # State owns completion; the completer binds whatever rows the perception loop
    # accumulated, even while the mechanical coverage verdict is still "incomplete".
    assert collection.coverage.get("status") == "incomplete"
    payload, terminal = runtime._resolve_worker_complete(
        spec,
        {
            "name": "complete",
            "args": {"evidence": []},
        },
        "records_worker",
        frame,
    )
    assert terminal == "complete"
    assert payload["ref"] == collection.ref
    assert payload["row_count"] == 1


def test_collector_complete_without_any_accumulated_rows_is_rejected() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.data_store = RuntimeDataStore()
    spec = _worker_spec(
        profile="collector",
        goal="Collect all records",
        success_criteria=["Collection coverage is complete"],
        data_requirements=[{
            "id": "records",
            "description": "Collect all records",
            "row_schema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        }],
        actions=[],
    )
    frame = MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png")

    with pytest.raises(ValueError, match="no accumulated rows"):
        runtime._resolve_worker_complete(
            spec,
            {
                "name": "complete",
                "args": {"evidence": []},
            },
            "records_worker",
            frame,
        )


def test_ready_collector_completion_uses_runtime_bound_collection_ref() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.data_store = RuntimeDataStore()
    _, collection, _ = runtime.data_store.put_chunk(
        requirement_id="records",
        frame_id="frame:1",
        provider="structured",
        rows=[{"value": "ready"}],
        row_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        coverage={
            "scope_status": "met",
            "source_scope": "structured_surface",
            "total_records": 1,
            "page_index": 1,
            "page_count": 1,
            "has_next_page": False,
            "at_end": True,
        },
    )
    spec = _worker_spec(
        profile="collector",
        goal="Collect all records",
        success_criteria=["Collection coverage is complete"],
        data_requirements=[{
            "id": "records",
            "description": "Collect all records",
            "row_schema": collection.row_schema,
        }],
        actions=[DynamicActionSpec(
            name="reveal_more",
            capability="scroll",
            description="Reveal more collected records",
            fixed_args={"direction": "down"},
        )],
    )
    frame = MaterializedFrame(
        frame_id="frame:1",
        screenshot_path="frame.png",
        collections=[collection],
        requirement_scopes={"records": {"status": "met"}},
    )

    payload, terminal = runtime._resolve_worker_complete(
        spec,
        {"name": "complete", "args": {"evidence": ["coverage complete"]}},
        "records_worker",
        frame,
    )

    assert terminal == "complete"
    assert payload["ref"] == collection.ref


def test_worker_observed_rows_accumulate_into_the_collection() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.data_store = RuntimeDataStore()
    runtime.trace = []
    spec = WorkerSpec(
        profile="collector",
        goal="Collect conference events",
        success_criteria=["All events collected"],
        data_requirements=[{
            "id": "events",
            "description": "Conference events",
            "row_schema": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                },
                "required": ["title", "start", "end"],
            },
        }],
        strategy=WorkerStrategy(approach="Read event details"),
    )
    runtime._accumulate_observed_rows(
        spec, "w", {"rows": [
            {"title": "Conference in Tokyo", "start": "Oct 4", "end": "Oct 10"},
        ]}, step=1,
    )
    collection = runtime.data_store.collection_for_requirement("events")
    assert collection is not None
    assert collection.row_count == 1
    assert "worker_observed_rows" in [e["event"] for e in runtime.trace]


def test_current_each_element_hints_the_plan_record_to_the_worker() -> None:
    # R2.3 regression: the Worker must know which plan element it is operating
    # on, otherwise it locates the first visible row (e.g. an already-renamed
    # file) instead of the actual target.
    runtime = object.__new__(ToolAgentRuntime)
    runtime.data_store = RuntimeDataStore()
    plan = [
        {"old_name": "bid_restaurant_proposal.txt", "new_name": "bid_1.txt"},
        {"old_name": "bid_menu_design_contract.pdf", "new_name": "bid_8.pdf"},
    ]
    ref = runtime.data_store.put_result(
        plan,
        {"type": "array", "items": {
            "type": "object",
            "properties": {
                "old_name": {"type": "string"}, "new_name": {"type": "string"},
            },
            "required": ["old_name", "new_name"],
        }},
        summary="rename plan",
    ).ref
    runtime._each_cursors = {("worker", "plan"): 0}
    spec = WorkerSpec(
        profile="operator",
        goal="Rename each plan element",
        success_criteria=["Every plan element renamed"],
        input_refs={"plan": ref},
        input_bindings=[
            {
                "name": "target_file", "input": "plan", "path": ["old_name"],
                "target": "text_input", "consume": "each",
                "description": "Current file name to locate and select",
            },
            {
                "name": "new_name", "input": "plan", "path": ["new_name"],
                "target": "text_input", "consume": "each",
                "description": "New file name to enter",
            },
        ],
        strategy=WorkerStrategy(approach="per-row rename"),
    )

    hint = runtime._current_each_element(spec, "worker")
    assert hint is not None
    assert "target_file=bid_restaurant_proposal.txt" in hint
    assert "new_name=bid_1.txt" in hint

    runtime._each_cursors[("worker", "plan")] = 1
    hint2 = runtime._current_each_element(spec, "worker")
    assert "bid_menu_design_contract.pdf" in hint2
    assert "bid_restaurant_proposal.txt" not in hint2


def test_attempt_contract_carries_current_element_hint() -> None:
    spec = WorkerSpec(
        profile="operator",
        goal="Rename each plan element",
        success_criteria=["Every plan element renamed"],
        input_refs={"plan": "result:1"},
        input_bindings=[
            {
                "name": "target_file", "input": "plan", "path": ["old_name"],
                "target": "text_input", "consume": "each",
                "description": "Current file name to locate and select",
            },
        ],
        strategy=WorkerStrategy(approach="per-row rename"),
    )
    plain = worker_attempt_contract(spec)
    assert "Current element" not in plain
    with_hint = worker_attempt_contract(
        spec, current_element="target_file=bid_menu_design_contract.pdf"
    )
    assert "Current element" in with_hint
    assert "bid_menu_design_contract.pdf" in with_hint


def test_runtime_streams_live_logs_and_observation_artifacts(tmp_path) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime.log_dir = tmp_path
    statuses = []
    runtime._status_cb = statuses.append
    runtime.data_store = RuntimeDataStore()
    runtime.perception_mode = "enhanced"
    runtime._frame_no = 0
    runtime.bundle = object()
    runtime.platform = object()
    runtime._access_log_redactions = ("runtime-secret-73",)
    runtime.materializer = SimpleNamespace(
        model="observer-model",
        observe=lambda **_kwargs: (
            MaterializedFrame(
                frame_id="frame:1",
                screenshot_path=str(tmp_path / "screenshot_tool_agent_1.png"),
                title="Signed in as runtime-secret-73",
            ),
            b"png",
        ),
    )
    spec = _worker_spec(
        profile="operator",
        goal="Reach the requested state",
        success_criteria=["The state is reached"],
        actions=[DynamicActionSpec(
            name="advance",
            capability="tap",
            description="Advance the goal",
        )],
    )

    runtime._observe(spec)

    events = [
        json.loads(line)
        for line in (tmp_path / "tool_agent_events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    live_trace = json.loads(
        (tmp_path / "tool_agent_trace.json").read_text(encoding="utf-8")
    )
    assert events[0]["layer"] == "observer"
    assert events[0]["event"] == "observe"
    assert "control_count" not in events[0]
    assert "Observe frame:1" in events[0]["message"]
    assert live_trace["phase"] == "running"
    assert live_trace["trace"] == events
    human_log = (tmp_path / "tool_agent.log").read_text(encoding="utf-8")
    assert "--- Turn 1 ---" in human_log
    assert "0 controls" not in human_log
    assert events[0]["timestamp"]
    assert statuses == ["Observer · Observe frame:1 for ?: no collection refs"]
    assert (tmp_path / "observation_tool_agent_1.json").is_file()
    observation = (tmp_path / "observation_tool_agent_1.json").read_text(
        encoding="utf-8"
    )
    assert "runtime-secret-73" not in observation
    assert "session access value redacted" in observation
    assert (tmp_path / "tool_agent_data_store.json").is_file()


def test_worker_timing_displays_parallel_state_perception_critical_path() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    decision = {
        "event": "worker_decision",
        "llm_elapsed_s": 3.4,
        "state": {"summary": "Current facts"},
        "tool": "continue_with_actions",
    }
    runtime.trace = [
        {"event": "observe", "mode": "vision-only"},
        {"event": "worker_state", "llm_elapsed_s": 7.3},
        {"event": "perception_extract", "llm_elapsed_s": 7.0},
        decision,
    ]

    line = runtime._human_line(decision)

    assert "parallel(state=7.3s, perception=7.0s)" in line
    assert "policy=3.4s" in line
    assert "llm_critical=10.7s" in line


def test_runtime_defers_detail_candidates_until_bound_action_runs(tmp_path) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime._frame_no = 0
    runtime._active_worker_id = "collect_target"
    journal = WorkerJournal(worker_id="collect_target")
    runtime._worker_journals = {"collect_target": journal}
    runtime.bundle = object()
    runtime.platform = object()
    runtime.perception_mode = "enhanced"
    runtime.log_dir = tmp_path
    runtime._trace = lambda *_args, **_kwargs: None
    observed: list[bool] = []

    def observe(**kwargs):
        observed.append(kwargs["allow_linked_details"])
        return MaterializedFrame(
            frame_id=f"frame:{len(observed)}", screenshot_path="frame.png",
        ), b"png"

    runtime.materializer = SimpleNamespace(observe=observe)
    spec = _worker_spec(
        profile="collector",
        goal="Collect the selected target details",
        success_criteria=["Every selected target has its details"],
        input_refs={"target": "result:1"},
        data_requirements=[{
            "id": "targets",
            "description": "Selected target details",
            "row_schema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            "field_sources": {"name": "Name"},
            "field_types": {"name": "text"},
            "filters": {},
        }],
        actions=[DynamicActionSpec(
            name="search_target", capability="type",
            description="Enter the selected target in the visible query input",
            input_args={"text": {"input": "target", "path": []}},
        )],
    )

    runtime._observe(spec)
    _record_executed(journal, "search_target")
    runtime._observe(spec)

    assert observed == [False, True]


class _CodingMaster:
    def __init__(self, *sources: str) -> None:
        self.sources = list(sources)

    def bind(self, **kwargs):
        del kwargs
        return self

    def invoke(self, messages):
        del messages
        return SimpleNamespace(content=self.sources.pop(0))


class _InterruptingMaster:
    def bind(self, **kwargs):
        del kwargs
        return self

    def invoke(self, messages):
        del messages
        raise KeyboardInterrupt


def _coding_program() -> str:
    return '''def run(ctx):
    result = ctx.gui_worker(
        worker_id="collect_records",
        profile="collector",
        goal="Collect the requested records",
        success_criteria=["The requested records are collected"],
        data_requirements=[{
            "id": "records",
            "description": "Requested records",
            "row_schema": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        }],
        approach="Traverse the requested collection from the current UI.",
    )
    if result["phase"] != "completed":
        ctx.fail(result["summary"])
    computed = ctx.transform(
        transform_id="count_records",
        inputs=[result["collection_ref"]["ref"]],
        source="def transform(inputs):\\n    return len(inputs[0])",
        result_schema={"type": "integer"},
    )
    ctx.finish(computed["ref"], effect="data")
'''


def test_runtime_replaces_reflection_inside_worker_call_without_replaying_program(tmp_path) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_compile_attempts = 1
    runtime.data_store = RuntimeDataStore()
    runtime.trace = []
    replacement = WorkerStrategy(
        approach="Use a different visible collection traversal path.",
    )
    runtime.master = _CodingMaster(_coding_program())
    runtime.master_cfg = SimpleNamespace(model="coding-master")
    runtime.worker_cfg = SimpleNamespace(model="visual-worker")
    runtime.materializer = SimpleNamespace(model="perception")
    runtime.perception_mode = "enhanced"
    runtime.log_dir = tmp_path
    runtime._request_reflection = lambda **_kwargs: ReflectionResult(
        decision="revise_approach", reason="selected", strategy=replacement,
    )
    worker_calls = []

    def run_worker(worker_id, spec, *, require_attempt=False):
        worker_calls.append((worker_id, spec))
        if len(worker_calls) == 1:
            return WorkerOutcome(
                phase="failed",
                summary="Need another GUI attempt",
                steps=4,
            )
        _, descriptor, _ = runtime.data_store.put_chunk(
            requirement_id="records",
            frame_id="frame:2",
            provider="structured",
            rows=[{"value": 1}],
            row_schema=spec.data_requirements[0].row_schema,
            coverage={
                "source_scope": "structured_surface",
                "scope_status": "met",
                "traversal_type": "static",
                "partial": False,
                "total_records": 1,
            },
        )
        return WorkerOutcome(
            phase="completed",
            summary="Collected after Strategy replacement",
            collection_ref=descriptor,
            steps=2,
        )

    runtime._run_worker = run_worker

    run = runtime.run("Collect the requested records")

    assert run.phase == "completed"
    assert run.output == 1
    assert len(worker_calls) == 2
    assert [worker_id for worker_id, _ in worker_calls] == [
        "collect_records",
        "collect_records_reflection_1",
    ]
    assert [spec.strategy.approach for _, spec in worker_calls] == [
        "Traverse the requested collection from the current UI.",
        "Use a different visible collection traversal path.",
    ]
    assert runtime.master.sources == []
    assert any(event["event"] == "reflected_worker_dispatched" for event in run.trace)
    assert (tmp_path / "tool_agent_trace.json").is_file()
    replay = json.loads(
        (tmp_path / "tool_agent_replay.json").read_text(encoding="utf-8")
    )
    assert replay["status"] == "passed"
    assert replay["program_count"] == 1
    assert replay["gui_worker_count"] == 1
    assert replay["uses_browser"] is False
    assert replay["uses_llm"] is False


def test_runtime_does_not_replay_frozen_program_after_local_budget_failure(
    tmp_path,
) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_compile_attempts = 1
    runtime.data_store = RuntimeDataStore()
    runtime.trace = []
    runtime.master = _CodingMaster(_coding_program())
    runtime.master_cfg = SimpleNamespace(model="coding-master")
    runtime.worker_cfg = SimpleNamespace(model="visual-worker")
    runtime.materializer = SimpleNamespace(model="perception")
    runtime.perception_mode = "enhanced"
    runtime.log_dir = tmp_path
    worker_calls = []

    def fail_worker(worker_id, spec, *, require_attempt=False):
        worker_calls.append((worker_id, spec))
        return WorkerOutcome(
            phase="failed",
            summary="No remaining local strategy",
            steps=4,
        )

    runtime._run_worker = fail_worker

    run = runtime.run("Collect the requested records")

    assert run.phase == "failed"
    assert len(worker_calls) == 1
    assert sum(
        event["event"] == "master_program_execution_started"
        for event in run.trace
    ) == 1
    replay = json.loads(
        (tmp_path / "tool_agent_replay.json").read_text(encoding="utf-8")
    )
    assert replay["status"] == "passed"
    assert replay["program_count"] == 1


def test_runtime_interruption_is_sealed_as_a_reportable_failed_run(tmp_path) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_compile_attempts = 1
    runtime.data_store = RuntimeDataStore()
    runtime.trace = []
    runtime.master = _InterruptingMaster()
    runtime.master_cfg = SimpleNamespace(model="coding-master")
    runtime.worker_cfg = SimpleNamespace(model="visual-worker")
    runtime.materializer = SimpleNamespace(model="perception")
    runtime.perception_mode = "enhanced"
    runtime.log_dir = tmp_path
    runtime._status_cb = None
    runtime._visualizer = _Visualizer()

    run = runtime.run("Collect the requested records")

    assert run.phase == "failed"
    assert "interrupted" in run.summary
    assert [event["event"] for event in run.trace] == [
        "runtime_started",
        "runtime_interrupted",
        "runtime_finished",
    ]
    persisted = json.loads(
        (tmp_path / "tool_agent_trace.json").read_text(encoding="utf-8")
    )
    assert persisted["phase"] == "failed"
    replay = json.loads(
        (tmp_path / "tool_agent_replay.json").read_text(encoding="utf-8")
    )
    assert replay["status"] == "unavailable"
    assert runtime._visualizer.clear_calls == 1


def test_each_binding_advances_cursor_on_complete_and_exhausts() -> None:
    """consume="each" bindings materialize one array element per cursor, advance
    on complete, and drop the action once the array is exhausted."""
    from gui_agent.core.tool_agent.runtime import _EachExhausted
    from gui_agent.core.tool_agent.contracts import RuntimeInputBinding

    runtime = object.__new__(ToolAgentRuntime)
    runtime._active_worker_id = "rename_worker"
    runtime._each_cursors = {}
    runtime.data_store = SimpleNamespace(
        result_value=lambda ref: [{"name": "a.txt", "new": "bid_1.txt"},
                                  {"name": "b.txt", "new": "bid_2.txt"}]
    )
    spec = WorkerSpec(
        profile="operator",
        goal="Rename files",
        success_criteria=["All renamed"],
        input_refs={"plan": "result:1"},
        input_bindings=[
            {
                "name": "rename_new",
                "input": "plan",
                "path": ["new"],
                "target": "text_input",
                "description": "New name",
                "consume": "each",
            },
        ],
        strategy=WorkerStrategy(approach="Rename each"),
    )
    action = DynamicActionSpec(
        name="rename_new",
        capability="type",
        description="Type the new name",
        input_args={"text": RuntimeInputBinding(input="plan", path=["new"])},
        exposed_args=["x", "y"],
    )

    def materialized_text():
        return runtime._materialize_action_inputs(spec, action).fixed_args["text"]

    # Cursor 0 → first element.
    assert materialized_text() == "bid_1.txt"
    # Advance cursor (as complete does) → second element.
    runtime._each_cursors[("rename_worker", "plan")] = 1
    assert materialized_text() == "bid_2.txt"
    # Exhausted → action is unavailable.
    runtime._each_cursors[("rename_worker", "plan")] = 2
    with pytest.raises(_EachExhausted):
        runtime._materialize_action_inputs(spec, action)


def test_review_accepts_array_ref_consumed_by_each_binding() -> None:
    """WORKER_ARRAY_INPUT_UNSUPPORTED is lifted when the array is consumed by a
    consume='each' binding."""
    from gui_agent.core.tool_agent.orchestrator import _static_flow_diagnostics
    import ast

    source = (
        'def run(ctx):\n'
        '    plan = ctx.transform(transform_id="plan", inputs=[coll["collection_ref"]["ref"]],\n'
        '        source="def transform(rows):\\n    return [{\'n\': r[\'name\']} for r in rows]",\n'
        '        result_schema={"type": "array", "items": {"type": "object"}})\n'
        '    ctx.gui_worker(worker_id="w", profile="operator", goal="g", success_criteria=["c"],\n'
        '        approach="a", input_refs={"plan": plan["ref"]},\n'
        '        input_bindings=[{"name": "apply", "input": "plan", "path": ["n"],\n'
        '            "target": "text_input", "description": "apply", "consume": "each"}])\n'
        '    ctx.finish(plan["ref"], effect="data")\n'
    )
    tree = ast.parse(source)
    diags = _static_flow_diagnostics(tree)
    array_diags = [d for d in diags if d.code == "WORKER_ARRAY_INPUT_UNSUPPORTED"]
    assert array_diags == [], f"each-consumed array must be allowed, got {array_diags}"


def test_each_binding_implicit_array_ref_loops_on_complete() -> None:
    """A binding over an array ref without explicit consume still iterates: the
    complete branch uses the same predicate as materialization."""
    from gui_agent.core.tool_agent.contracts import RuntimeInputBinding
    from gui_agent.core.tool_agent.runtime import _EachExhausted, ToolAgentRuntime

    runtime = object.__new__(ToolAgentRuntime)
    runtime._active_worker_id = "rename_worker"
    runtime._each_cursors = {}
    runtime.data_store = SimpleNamespace(
        result_value=lambda ref: [{"n": "a"}, {"n": "b"}]
    )
    spec = WorkerSpec(
        profile="operator",
        goal="g", success_criteria=["c"],
        input_refs={"plan": "result:1"},
        input_bindings=[{
            "name": "apply", "input": "plan", "path": ["n"],
            "target": "text_input", "description": "apply",
        }],  # no consume field → implicit each over array
        strategy=WorkerStrategy(approach="a"),
    )
    action = DynamicActionSpec(
        name="apply", capability="type", description="d",
        input_args={"text": RuntimeInputBinding(input="plan", path=["n"])},
        exposed_args=["x", "y"],
    )
    # Materialization follows the cursor for an implicit-each array ref.
    m = ToolAgentRuntime._materialize_action_inputs.__get__(runtime)
    first = m(spec, action)
    assert first.fixed_args["text"] == "a"
    runtime._each_cursors[("rename_worker", "plan")] = 1
    second = m(spec, action)
    assert second.fixed_args["text"] == "b"


def test_each_binding_complete_advances_cursor_and_exhausts() -> None:
    """The full complete→each_next→terminal chain advances the cursor across
    elements and ends once the array is consumed."""
    from gui_agent.core.tool_agent.runtime import ToolAgentRuntime

    runtime = object.__new__(ToolAgentRuntime)
    runtime._active_worker_id = "w"
    runtime._each_cursors = {}
    runtime.data_store = SimpleNamespace(
        result_value=lambda ref: [{"n": "a"}, {"n": "b"}]
    )
    spec = WorkerSpec(
        profile="operator",
        goal="g", success_criteria=["c"],
        input_refs={"plan": "result:1"},
        input_bindings=[{
            "name": "apply", "input": "plan", "path": ["n"],
            "target": "text_input", "description": "apply", "consume": "each",
        }],
        strategy=WorkerStrategy(approach="a"),
    )
    frame = MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png")
    # complete #1 → advances to cursor 1, returns each_next (not terminal).
    result, terminal = runtime._resolve_worker_complete(
        spec, {"name": "complete", "args": {}}, "w", frame,
    )
    assert terminal == "each_next"
    assert result.get("each_advanced") is True
    assert runtime._each_cursors[("w", "plan")] == 1
    # complete #2 → cursor 2 == len, exhausted → real terminal.
    result, terminal = runtime._resolve_worker_complete(
        spec, {"name": "complete", "args": {}}, "w", frame,
    )
    assert terminal == "complete"
    assert result.get("each_advanced") is None


class _QueueWorker(_SplitWorkerFixture):
    """One rename per plan element: State advances each element via `complete`."""

    def __init__(self) -> None:
        super().__init__()
        self.state_calls = 0
        self.actor_calls = 0

    def invoke(self, messages):
        if self.mode == "state":
            self.state_calls += 1
            # Even State frames complete the current element and advance the plan.
            if self.state_calls % 2 == 0:
                state_input = json.loads(messages[-1].content[0]["text"])
                return SimpleNamespace(content="", tool_calls=[{
                    "id": f"state-complete-{self.state_calls}",
                    "name": "edit_state_memory",
                    "args": _state_complete_args(
                        state_input["mode"], state_input["frame_id"], ["renamed"],
                    ),
                }])
            state_input = json.loads(messages[-1].content[0]["text"])
            events = ([{
                "kind": "source_observed",
                "source_ref": "test_surface",
                "evidence": "The requested test surface is visible.",
            }] if state_input["mode"] == "init" else [])
            return SimpleNamespace(content="", tool_calls=[{
                "id": f"state-delta-{self.state_calls}",
                "name": "edit_state_memory",
                "args": _state_delta_args(
                    state_input["mode"], state_input["frame_id"], events,
                ),
            }])
        self.actor_calls += 1
        return SimpleNamespace(content="", tool_calls=[{
            "id": f"tap-{self.actor_calls}",
            "name": "continue_with_actions",
            "args": {"actions": [{
                "name": "tap",
                "args": {
                    "x": 750, "y": 450,
                    "description": "Tap Rename",
                    "state_target_ref": None,
                },
            }]},
        }])


def test_each_element_allows_same_structural_action() -> None:
    """The same menu action may be valid for consecutive plan elements."""
    rename_frame = MaterializedFrame(
        frame_id="frame:menu",
        screenshot_path="frame.png",
        controls=[{
            "kind": "button", "label": "Rename", "enabled": True,
            "rect": {"x": 752, "y": 449, "w": 477, "h": 53},
        }],
    )
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime._status_cb = None
    runtime._worker_journals = {}
    runtime._worker_last_frames = {}
    runtime._each_cursors = {}
    observed_frames = 0

    def observe_rename(_spec):
        nonlocal observed_frames
        observed_frames += 1
        return rename_frame.model_copy(update={
            "frame_id": f"frame:{observed_frames}",
        }), _TEST_PNG

    runtime._observe = observe_rename
    runtime.data_store = SimpleNamespace(
        result_value=lambda ref: [
            {"old_name": "a.txt", "new_name": "bid_1.txt"},
            {"old_name": "b.doc", "new_name": "bid_2.doc"},
        ],
        collection_for_requirement=lambda req: None,
    )
    runtime._platform_capabilities = frozenset({"tap"})
    runtime._initial_worker_actions = lambda spec: [
        DynamicActionSpec(name="tap", capability="tap", description="Tap target")
    ]
    runtime.worker = _QueueWorker()
    spec = WorkerSpec(
        profile="operator",
        goal="Rename each plan element",
        success_criteria=["Every element renamed"],
        input_refs={"plan": "result:1"},
        input_bindings=[
            {
                "name": "target_file", "input": "plan", "path": ["old_name"],
                "target": "text_input", "consume": "each",
                "description": "locate row",
            },
            {
                "name": "new_name", "input": "plan", "path": ["new_name"],
                "target": "text_input", "consume": "each",
                "description": "new name",
            },
        ],
        strategy=WorkerStrategy(approach="per-row rename"),
    )

    outcome = runtime._run_worker("rename_worker", spec)

    # Both taps dispatched (the second, same-coordinate tap on a structurally
    # identical menu was NOT blocked as a repeat) and both elements completed.
    assert (runtime.worker.state_calls, runtime.worker.actor_calls) == (4, 2)
    assert outcome.phase == "completed"
    assert outcome.steps >= 1
    each = [event for event in runtime.trace if event["event"] == "worker_each_advanced"]
    assert len(each) == 1
