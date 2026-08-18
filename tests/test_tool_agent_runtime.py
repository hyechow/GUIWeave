from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gui_agent.adapters.browser.actions import BrowserAction
from gui_agent.adapters.android.actions import AndroidAction
from gui_agent.core.tool_agent.contracts import (
    DynamicActionSpec,
    MaterializedFrame,
    WorkerOutcome,
    WorkerSpec,
    WorkerState,
    WorkerStrategy,
)
from gui_agent.core.tool_agent.action_guard import (
    WorkerActionCircuitBreaker,
    action_signature,
    is_candidate_commit,
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
    _target_verification_result,
)
from gui_agent.core.tool_agent.strategy import Strategy
from gui_agent.core.schemas import TargetVerify
from gui_agent.core.tool_agent.worker_memory import (
    WorkerJournal,
    WorkerJournalEvent,
    build_worker_memory_view,
    project_worker_context,
)
from gui_agent.adapters.browser.control_grounding import ground_action_to_nearest_control


def _state() -> str:
    return json.dumps(
        {
            "status": "exploring",
            "summary": "A separate apply control is visible.",
            "established_facts": [],
        }
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


def test_action_guard_canonicalizes_equivalent_actions() -> None:
    def signature(capability, **args):
        return action_signature(tool="alias", capability=capability, args=args)

    assert signature("scroll", direction="down", amount=5, y=400) == signature(
        "scroll", direction="down", amount=9, y=560,
    )
    assert signature("tap", x=210, y=150) == signature("tap", x=219, y=151)


def test_action_guard_blocks_one_unchanged_repeat() -> None:
    frame = MaterializedFrame(frame_id="frame:stable", screenshot_path="frame.png")
    breaker = WorkerActionCircuitBreaker()
    first = breaker.inspect(
        tool="scroll", capability="scroll",
        args={"direction": "down", "amount": 5}, frame=frame,
    )
    breaker.record(first)
    second = breaker.inspect(
        tool="scroll", capability="scroll",
        args={"direction": "down", "amount": 9}, frame=frame,
    )
    assert second.blocked and second.prior_attempts == 1


def test_action_guard_allows_same_action_after_progress() -> None:
    breaker = WorkerActionCircuitBreaker()
    first = breaker.inspect(
        tool="open_url",
        capability="open_url",
        args={"url": "https://example.test/"},
        frame=MaterializedFrame(
            frame_id="frame:1",
            screenshot_path="frame:1.png",
            url="https://before.example/",
        ),
    )
    breaker.record(first)

    repeated = breaker.inspect(
        tool="open_url",
        capability="open_url",
        args={"url": "https://example.test/"},
        frame=MaterializedFrame(
            frame_id="frame:2",
            screenshot_path="frame:2.png",
            url="https://redirected.example/",
        ),
    )

    assert not repeated.blocked
    assert repeated.prior_attempts == 0


def test_explicit_no_effect_exhausts_an_unchanged_action_retry() -> None:
    frame = MaterializedFrame(frame_id="frame:stable", screenshot_path="frame.png")
    breaker = WorkerActionCircuitBreaker()
    first = breaker.inspect(
        tool="submit",
        capability="press_enter",
        args={},
        frame=frame,
    )

    ToolAgentRuntime._record_action_attempt(
        breaker,
        first,
        DynamicActionSpec(
            name="submit",
            capability="press_enter",
            description="Submit the focused form",
        ),
        {"status": "executed", "no_effect": True},
    )
    repeated = breaker.inspect(
        tool="submit",
        capability="press_enter",
        args={},
        frame=frame,
    )

    assert repeated.blocked
    assert repeated.prior_attempts == 1


def test_runtime_blocks_disabled_structured_control() -> None:
    frame = MaterializedFrame(
        frame_id="frame:disabled", screenshot_path="frame.png",
        controls=[{
            "kind": "button", "label": "Confirm", "enabled": False,
            "rect": {"x": 500, "y": 500, "w": 200, "h": 80},
        }],
    )

    decision = WorkerActionCircuitBreaker().inspect(
        tool="tap", capability="tap", args={"x": 500, "y": 500}, frame=frame,
    )

    assert decision.blocked is True
    assert "disabled control" in decision.reason


def test_runtime_guard_does_not_choose_query_entry_recovery() -> None:
    frame = MaterializedFrame(
        frame_id="frame:query", screenshot_path="frame.png",
        controls=[{
            "kind": "button", "label": "Search", "query_action": "open",
            "rect": {"x": 500, "y": 500, "w": 200, "h": 80},
        }],
    )

    decision = WorkerActionCircuitBreaker().inspect(
        tool="type", capability="type", args={"x": 500, "y": 500}, frame=frame,
    )

    assert decision.blocked is True
    assert "Runtime guard as authoritative" in decision.instruction


def _wizard_frame(step_label: str, *, checked: str = "") -> MaterializedFrame:
    controls = []
    if checked:
        controls.append({
            "kind": "checkbox_input",
            "label": checked,
            "value": "on",
            "rect": {"x": 326, "y": 873, "w": 20, "h": 20},
        })
    return MaterializedFrame(
        frame_id=f"frame:{step_label}",
        screenshot_path="frame.png",
        title=f"Create Product Configurations: {step_label}",
        controls=controls,
    )


def test_surface_cycle_blocks_traversal_loop_with_coordinate_jitter() -> None:
    """Run 550 oscillated Next/Back across three wizard steps six times; each
    tap used fresh coordinates, so the exact-pair cycle never matched."""
    breaker = WorkerActionCircuitBreaker()
    step2 = _wizard_frame("attribute values", checked="Blue")
    step3 = _wizard_frame("bulk images")
    step4 = _wizard_frame("summary")

    sequence = [
        ("back", {"x": 860, "y": 137}, step3),
        ("tap", {"x": 789, "y": 137}, step2),
        ("back", {"x": 861, "y": 140}, step3),
        ("tap", {"x": 860, "y": 137}, step2),
        ("tap", {"x": 855, "y": 139}, step3),
        ("back", {"x": 790, "y": 138}, step4),
    ]
    for capability, args, frame in sequence:
        decision = breaker.inspect(
            tool="nav", capability=capability, args=args, frame=frame,
        )
        assert decision.blocked is False
        breaker.record(decision)

    stuck = breaker.inspect(
        tool="nav", capability="tap", args={"x": 862, "y": 136}, frame=step2,
    )
    assert stuck.blocked is True
    assert "surface cycle" in stuck.reason

    # Progressing flows keep minting new surfaces and never trip the fuse.
    breaker = WorkerActionCircuitBreaker()
    frames = [
        _wizard_frame("attribute values", checked=color)
        for color in ("Blue", "Purple", "Green", "Red", "Black", "White")
    ]
    for index, frame in enumerate(frames):
        decision = breaker.inspect(
            tool="nav", capability="tap",
            args={"x": 300 + index * 10, "y": 500}, frame=frame,
        )
        assert decision.blocked is False
        breaker.record(decision)
    assert breaker.inspect(
        tool="nav", capability="tap", args={"x": 400, "y": 500},
        frame=_wizard_frame("summary", checked="Blue"),
    ).blocked is False

    # Fewer than six dispatches can never be a surface cycle. Coordinates sit
    # in distinct signature buckets so the exact-repeat fuse stays out of this.
    breaker = WorkerActionCircuitBreaker()
    for x in (100, 160, 220, 280):
        breaker.record(breaker.inspect(
            tool="nav", capability="tap", args={"x": x, "y": 100}, frame=step2,
        ))
    assert breaker.inspect(
        tool="nav", capability="tap", args={"x": 340, "y": 100}, frame=step2,
    ).blocked is False


def test_batched_actions_on_one_frame_do_not_fake_a_surface_cycle() -> None:
    """A login batch (type, type, tap) is decided on ONE frame; per-atomic
    progress hashes repeat the same surface and must not fill the window."""
    breaker = WorkerActionCircuitBreaker()
    login = _wizard_frame("login")
    dashboard = _wizard_frame("dashboard")
    grid = _wizard_frame("attribute grid")

    for args in ({"text": "user"}, {"text": "pass"}, {"x": 500, "y": 450}):
        capability = "type" if "text" in args else "tap"
        breaker.record(breaker.inspect(
            tool="auth", capability=capability, args=args, frame=login,
        ))
    breaker.record(breaker.inspect(
        tool="nav", capability="open_url", args={"url": "/admin"}, frame=dashboard,
    ))
    for args in ({"text": "size"}, {}):
        capability = "type" if "text" in args else "press_enter"
        breaker.record(breaker.inspect(
            tool="filter", capability=capability, args=args, frame=grid,
        ))

    assert breaker.inspect(
        tool="open_row", capability="tap", args={"x": 320, "y": 600}, frame=grid,
    ).blocked is False


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

    def inspect(target_frame: MaterializedFrame, capability: str, y: int):
        return WorkerActionCircuitBreaker().inspect(
            tool=capability,
            capability=capability,
            args={"x": 500, "y": y},
            frame=target_frame,
        )

    assert "clipped collection cell" in inspect(frame, "tap", 948).reason
    assert inspect(frame, "tap", 700).blocked is False
    assert inspect(frame, "scroll", 948).blocked is False

    selectable = frame.model_copy(update={"controls": [{
        "kind": "checkbox",
        "label": "Select record",
        "ref": "row:settings.checkbox",
        "selection_mode": "multiple",
        "rect": {"x": 500, "y": 948, "w": 40, "h": 40},
    }]})
    assert "clipped collection cell" in inspect(selectable, "tap", 948).reason

    unrelated = selectable.model_copy(update={"controls": [
        {**selectable.controls[0], "ref": "toolbar:button"},
    ]})
    assert inspect(unrelated, "tap", 948).blocked is False


def test_runtime_blocks_navigation_outside_unfinished_scroll_collection() -> None:
    frame = MaterializedFrame(
        frame_id="frame:9",
        screenshot_path="frame.png",
        visible_collection_regions=[{
            "bounds": [0, 116, 1000, 886],
            "viewport_tail_clipped": True,
            "cells": [],
        }],
    )
    state = WorkerState(
        status="collecting",
        summary="Collection is complete.",
    )
    journal = WorkerJournal("collection")
    journal.collection_context = "Bookmarks"

    reason = ToolAgentRuntime._incomplete_collection_exit_reason(
        capability="tap",
        args={"x": 375, "y": 930},
        state=state,
        journal=journal,
        frame=frame,
    )
    assert "latest traversal scroll returned no_effect" in reason

    journal.last_scroll_no_effect = True
    journal.last_scroll_direction = "down"
    journal.last_scroll_point = (375, 500)
    assert ToolAgentRuntime._incomplete_collection_exit_reason(
        capability="tap",
        args={"x": 375, "y": 930},
        state=state,
        journal=journal,
        frame=frame,
    ) == ""

    journal.collection_context = ""
    journal.last_scroll_no_effect = False
    assert ToolAgentRuntime._incomplete_collection_exit_reason(
        capability="tap",
        args={"x": 375, "y": 930},
        state=state,
        journal=journal,
        frame=frame,
    ) == ""


def test_action_guard_allows_typing_into_editable_aria_combobox() -> None:
    breaker = WorkerActionCircuitBreaker()
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

    decision = breaker.inspect(
        tool="runtime_type_visible",
        capability="type",
        args={"x": 478, "y": 233, "text": "Alex"},
        frame=frame,
    )

    assert not decision.blocked


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

    prompt = runtime._worker_system_prompt()

    assert '"Calendar"' in prompt
    assert '"Settings"' in prompt


def test_worker_prompt_defines_safe_batching_once() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.allow_multi_action = True
    runtime._platform_capabilities = frozenset({"tap"})
    runtime._installed_app_names = ()
    runtime._master_knowledge = ""
    runtime._worker_access_context = ""

    prompt = runtime._worker_system_prompt()

    assert "actions grounded in the current frame" in prompt
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

    prompt = runtime._worker_system_prompt()

    assert "Session access context" in prompt
    assert "runtime-user-73" in prompt
    assert "runtime-secret-73" in prompt
    assert "Application knowledge" in prompt
    assert "profile menu" in prompt

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

    system_prompt = runtime._worker_system_prompt()
    first_contract = worker_attempt_contract(first)

    assert "Application knowledge" in system_prompt
    assert "Session access context" in system_prompt
    assert "Current Worker attempt" not in system_prompt
    assert first_contract.index('"approach"') < first_contract.index('"goal"')
    assert first_contract.index('"approach"') < first_contract.index('"phase"')
    assert '"phase": "start"' in first_contract
    assert '"approach": "Search the visible record grid for record-17."' in first_contract
    assert '"goal": "Find the requested record"' in first_contract
    assert "Search the visible record grid using the requested literal." not in first_contract
    assert '"exposed_args"' not in first_contract


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
    system_prompt = runtime._worker_system_prompt()
    attempt_contract = worker_attempt_contract(spec)
    assert "3 customer(s) love it!" not in system_prompt
    assert '"input": "computed"' in attempt_contract
    assert '"path": ["description"]' in attempt_contract
    assert '"task_goal"' not in attempt_contract


def test_worker_reports_blocker_without_deciding_strategy_failure() -> None:
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
    journal.executed_tools.add("open_replacement")
    attempted = runtime._worker_tools_for_frame(spec, spec._test_actions, frame)

    assert "report_blocked" not in {tool["function"]["name"] for tool in pending}
    assert "report_blocked" in {tool["function"]["name"] for tool in attempted}
    assert "fail" not in {tool["function"]["name"] for tool in pending}


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
    assert names == {"enter_query", "complete", "report_blocked"}


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


def test_strategy_does_not_receive_or_control_runtime_turn_budget() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_turns = 10
    runtime._frame_no = 8
    runtime._trace = lambda *_args, **_kwargs: None

    def fail_worker(_worker_id, _spec, *, require_attempt=False):
        runtime._frame_no += 1
        return WorkerOutcome(phase="failed", summary="Try a distinct path", steps=1)

    requests = []
    runtime._run_worker = fail_worker
    runtime._request_strategy_decision = lambda **kwargs: (
        requests.append(kwargs) or None,
        "No feasible attempt remains",
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
            return spec.strategy.model_copy(
                update={"approach": "Use the alternate visible path."}
            ), "selected"
        raise ValueError("replacement is invalid")

    runtime._request_strategy_decision = revise
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
    assert "Strategy decision failed" in outcome.summary


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
    runtime._request_strategy_decision = lambda **_kwargs: (
        spec.strategy.model_copy(update={
            "approach": "Use an evidenced alternative traversal.",
        }),
        "The original source is blocked.",
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
    assert worker_ids == ["logical_worker", "logical_worker_strategy_1"]
    assert attempt_requirements == [False, True]
    assert any(event["event"] == "strategy_worker_dispatched" for event in events)


def test_strategy_replacements_use_only_the_global_turn_budget() -> None:
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
        return WorkerStrategy(approach=f"alternative source {attempt}"), "selected"

    runtime._run_worker = run_worker
    runtime._request_strategy_decision = replace
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
        ("strategy", 1), ("worker", "logical_worker_strategy_1"),
        ("strategy", 2), ("worker", "logical_worker_strategy_2"),
        ("strategy", 3), ("worker", "logical_worker_strategy_3"),
    ]


class _Executor:
    def __init__(self) -> None:
        self.actions = []

    def execute(self, decision, **kwargs):
        del kwargs
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
    def __init__(self, value: TargetVerify) -> None:
        self.value = value

    def result(self, *, timeout=None):
        del timeout
        return self.value


class _ImmediateVerifyPool:
    def __init__(self, *values: TargetVerify) -> None:
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


class _EmptyContentWorker:
    def __init__(self) -> None:
        self.mode = ""

    def bind_tools(self, tools, **kwargs):
        del tools, kwargs
        self.mode = "action"
        return self

    def bind(self, **kwargs):
        del kwargs
        self.mode = "state"
        return self

    def invoke(self, messages):
        del messages
        if self.mode == "action":
            return SimpleNamespace(
                content="",
                tool_calls=[{
                    "id": "tap-1",
                    "name": "activate_visible_control",
                    "args": {
                        "state": json.loads(_state()),
                        "x": 400, "y": 300, "description": "Advance",
                    },
                }],
            )
        return SimpleNamespace(content=_state(), tool_calls=[])


class _MissingStateWorker(_EmptyContentWorker):
    def invoke(self, messages):
        response = super().invoke(messages)
        for call in response.tool_calls:
            call["args"].pop("state", None)
        return response


class _ArrayCoordinateWorker:
    def bind_tools(self, tools, **kwargs):
        del tools, kwargs
        return self

    def invoke(self, messages):
        del messages
        return SimpleNamespace(
            content="",
            tool_calls=[{
                "id": "type-1",
                "name": "enter_value",
                "args": {
                    "state": {
                        "status": "exploring",
                        "summary": "The date input is visible.",
                    },
                    "x": [200, 380],
                    "y": [200, 380],
                    "text": "01/01/2023",
                    "description": "Enter the start date",
                },
            }],
        )


class _RepeatedThenGroundedWorker:
    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        del tools, kwargs
        return self

    def invoke(self, messages):
        del messages
        self.calls += 1
        args = {
            "state": {
                "status": "exploring",
                "summary": "The Purchase Date to field is empty.",
            },
            "x": 207,
            "y": 550,
            "text": "05/31/2023",
            "description": "Enter the end date into the Purchase Date to input",
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


class _RepeatedEffectiveScrollWorker:
    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        del tools, kwargs
        return self

    def invoke(self, messages):
        del messages
        self.calls += 1
        return SimpleNamespace(
            content="",
            tool_calls=[{
                "id": f"scroll-{self.calls}",
                "name": "reveal_more",
                "args": {
                    "state": {
                        "status": "collecting",
                        "summary": "More visual content remains below.",
                    },
                    "amount": "medium",
                },
            }],
        )


_LOGIN_ACTIONS = [
    {"name": "enter_username", "args": {"x": 500, "y": 400, "text": "demo-user", "description": "Enter Username"}},
    {"name": "enter_password", "args": {"x": 500, "y": 500, "text": "demo-pass", "description": "Enter Password"}},
    {"name": "submit_login", "args": {"x": 500, "y": 600, "description": "Tap Sign in"}},
]


class _MultiActionWorker:
    def __init__(
        self,
        action_batches: list[list[dict]] | None = None,
        state_status: str = "exploring",
    ) -> None:
        self.calls = 0
        self.bound_names: set[str] = set()
        self.bound_schemas: list[str] = []
        self.action_batches = action_batches
        self.state_status = state_status
        self.messages = []
        self.state_summary = "The complete login form is visible."

    def bind_tools(self, tools, **kwargs):
        assert kwargs.get("parallel_tool_calls") is False
        self.bound_names = {tool["function"]["name"] for tool in tools}
        self.bound_schemas.append(json.dumps(tools))
        return self

    def invoke(self, messages):
        self.messages = messages
        self.calls += 1
        actions = (
            self.action_batches[self.calls - 1]
            if self.action_batches is not None
            else _LOGIN_ACTIONS
        )
        return SimpleNamespace(content="", tool_calls=[{
            "id": f"decision-{self.calls}",
            "name": "continue_with_actions",
            "args": {
                "state": {
                    "status": self.state_status,
                    "summary": self.state_summary,
                    "established_facts": [],
                },
                "actions": actions,
            },
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
) -> ToolAgentRuntime:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime.statuses = []
    runtime._status_cb = runtime.statuses.append
    runtime.worker = worker or _MultiActionWorker()
    runtime._executor = _Executor()
    runtime.platform = SimpleNamespace(
        screenshot=lambda: b"latest-png",
        client=SimpleNamespace(page_info=lambda: (current_url, "Login")),
    )
    runtime.allow_multi_action = True
    runtime.observe_calls = 0

    def observe(_spec):
        runtime.observe_calls += 1
        return MaterializedFrame(
            frame_id="frame:1",
            screenshot_path="frame.png",
            url="https://example.test/login",
            controls=controls or [],
            visible_collection_regions=visible_collection_regions or [],
            requirement_scopes=requirement_scopes or {},
        ), b"initial-png"

    runtime._observe = observe
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, *, action_type: (0.0, False),
    )
    spec = _worker_spec(
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
        max_steps=1,
    )
    _install_test_worker_contract(runtime, spec)
    runtime.outcome = runtime._run_worker("fused-worker", spec)
    return runtime


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


@pytest.mark.parametrize("state_status", ["completed", "failed"])
def test_fused_worker_rejects_terminal_state_with_continuing_action(
    monkeypatch, state_status: str,
) -> None:
    actions = [[{
        "name": "submit_login",
        "args": {"x": 500, "y": 500, "description": "Submit the login form"},
    }]] * 2
    worker = _MultiActionWorker(actions, state_status=state_status)
    runtime = _run_fused_worker(
        monkeypatch,
        current_url="https://example.test/items",
        worker=worker,
    )

    assert runtime.outcome.phase == "failed"
    assert runtime.outcome.steps == 0
    assert "terminal state/tool mismatch" in runtime.outcome.summary
    assert len(runtime._executor.actions) == 0
    assert worker.calls == 2


def test_worker_repairs_rejected_launch_app_without_reobserving(monkeypatch) -> None:
    exact_app = "com.android.settings/.HWSettings"
    worker = _MultiActionWorker([
        [{"name": "open_settings", "args": {"app": "Settings"}}],
        [{"name": "open_settings", "args": {"app": exact_app}}],
    ])
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime.statuses = []
    runtime._status_cb = runtime.statuses.append
    runtime.worker = worker
    runtime._executor = _Executor()
    runtime._visualizer = None
    runtime._platform_capabilities = frozenset({"launch_app"})
    runtime._installed_app_names = (exact_app,)
    runtime._master_knowledge = ""
    runtime._worker_access_context = ""
    runtime.bundle = SimpleNamespace(
        platform="android",
        make_action=lambda payload: AndroidAction.model_validate(payload),
    )
    runtime.platform = SimpleNamespace(
        screenshot=lambda: b"latest-png",
        client=SimpleNamespace(),
    )
    runtime.perception_mode = "enhanced"
    runtime.allow_multi_action = True
    runtime.observe_calls = 0

    def observe(_spec):
        runtime.observe_calls += 1
        return MaterializedFrame(
            frame_id="frame:settings",
            screenshot_path="frame.png",
        ), b"initial-png"

    runtime._observe = observe
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, *, action_type: (0.0, False),
    )
    spec = _worker_spec(
        goal="Open system settings",
        success_criteria=["System settings is visible"],
        actions=[DynamicActionSpec(
            name="open_settings",
            capability="launch_app",
            description="Open system settings",
            exposed_args=["app"],
        )],
        max_steps=1,
    )

    _install_test_worker_contract(runtime, spec)
    runtime._run_worker("open-settings", spec)

    assert runtime.observe_calls == 1
    assert worker.calls == 2
    assert [action.app for action in runtime._executor.actions] == [exact_app]
    assert any(
        event["event"] == "worker_same_frame_action_repair"
        for event in runtime.trace
    )


def test_multi_action_continuation_uses_adapter_and_control_facts() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.perception_mode = "enhanced"
    runtime._executor = SimpleNamespace(
        type_suffix_safe=False,
        refresh_controls=lambda: [],
    )
    tap = DynamicActionSpec(name="tap", capability="tap", description="Tap")
    type_action = DynamicActionSpec(name="type", capability="type", description="Type")
    scroll = DynamicActionSpec(name="scroll", capability="scroll", description="Scroll")
    actions = {item.name: item for item in (tap, type_action, scroll)}
    remaining = [{"name": "type", "args": {}, "_control_ref": "input"}]
    selection = MaterializedFrame(
        frame_id="selection", screenshot_path="frame.png", controls=[{
            "kind": "checkbox", "selection_mode": "multiple",
            "rect": {"x": 500, "y": 500, "w": 100, "h": 100},
        }],
    )

    assert runtime._can_continue_batch(
        tap, {"x": 500, "y": 500}, remaining, actions, selection,
    )
    assert not runtime._can_continue_batch(
        scroll, {}, remaining, actions, selection,
    )
    runtime.perception_mode = "vision-only"
    assert not runtime._can_continue_batch(
        type_action, {"x": 500, "y": 500}, remaining, actions, selection,
    )


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


def _candidate_frame(
    frame_id: str, *, selected: bool = False, value: str = "Search",
) -> MaterializedFrame:
    controls = [{
        "kind": "text_input", "label": "Search", "is_filter": True, "value": value,
    }]
    if selected:
        controls += [
            {"kind": "checkbox", "selection_mode": "multiple", "selected": True,
             "rect": {"x": 500, "y": 250, "w": 900, "h": 60}},
            {"kind": "button", "form_action": "commit",
             "rect": {"x": 500, "y": 900, "w": 800, "h": 60}},
        ]
    return MaterializedFrame(
        frame_id=frame_id,
        screenshot_path=f"{frame_id}.png",
        controls=controls,
    )


def test_confirmed_candidate_commit_marks_matching_unfiltered_reopen_exhausted() -> None:
    selected = _candidate_frame("selected", selected=True)
    committed = is_candidate_commit({"x": 500, "y": 900}, selected)
    journal = WorkerJournal(worker_id="select-all", events=[WorkerJournalEvent(
        event_ref="commit", kind="candidate_commit", durable_text="confirmed",
    )])

    def context(frame: MaterializedFrame) -> str:
        return project_worker_context(
            memory=build_worker_memory_view(journal), frame=frame,
        ).text

    assert committed is True
    assert '"status": "exhausted"' in context(_candidate_frame("empty"))
    assert '"status": "exhausted"' not in context(
        _candidate_frame("filtered", value="query"))
    initial = build_worker_memory_view(WorkerJournal(worker_id="select-all"))
    assert '"status": "exhausted"' not in project_worker_context(
        memory=initial, frame=_candidate_frame("initial-empty")).text


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


def test_worker_rejects_missing_tool_state_without_executing(monkeypatch) -> None:
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
        lambda platform, png, *, action_type: (0.0, False),
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
        event for event in runtime.trace if event["event"] == "worker_protocol_error"
    ]) == 2


def test_replacement_strategy_starts_with_fresh_journal(monkeypatch) -> None:
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
        lambda platform, png, *, action_type: (0.0, False),
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
    second = runtime._run_worker("advance_subgoal_strategy_1", spec)

    assert first.phase == second.phase == "failed"
    starts = [event for event in runtime.trace if event["event"] == "worker_started"]
    assert [event["retained_memory_events"] for event in starts] == [0, 0]
    decisions = [event for event in runtime.trace if event["event"] == "worker_decision"]
    assert [event["memory_event_count"] for event in decisions] == [0, 0]
    assert set(runtime._worker_journals) == {
        "advance_subgoal",
        "advance_subgoal_strategy_1",
    }
    assert set(runtime._worker_action_breakers) == set(runtime._worker_journals)
    assert runtime._active_worker_journal() is runtime._worker_journals[
        "advance_subgoal_strategy_1"
    ]


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
        lambda platform, png, *, action_type: (0.0, False),
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


def test_worker_blocks_unchanged_repeat_and_accepts_same_frame_ref_repair(
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
            b"png",
        )

    runtime._observe = observe
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, *, action_type: (0.0, False),
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
    assert runtime.worker.calls == 3
    assert len(runtime._executor.actions) == 2
    assert (runtime._executor.actions[-1].x, runtime._executor.actions[-1].y) == (
        212,
        428,
    )
    assert runtime._executor.actions[-1].snap["method"] == "control_geometry"
    assert runtime._visualizer.points[-2:] == [
        (212.0, 428.0),
        (212.0, 428.0),
    ]
    blocked = [event for event in runtime.trace if event["event"] == "worker_action_blocked"]
    assert len(blocked) == 1
    assert blocked[0]["prior_attempts"] == 1


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
            b"png",
        )

    runtime._observe = observe
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, *, action_type: (0.0, False),
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
        event["event"] == "worker_action_blocked" for event in runtime.trace
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
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, *, action_type: (0.0, False),
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
        lambda platform, png, *, action_type: (0.0, False),
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



def test_multi_action_aborts_suffix_after_flash_off_target(monkeypatch) -> None:
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
    runtime._target_verify_pool = _ImmediateVerifyPool(TargetVerify(
        on_target=False,
        actual_element="Username",
        reason="The marker missed the Password field.",
    ))
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, *, action_type: (0.0, False),
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
        state=WorkerState(
            status="exploring",
            summary="The login form is visible.",
        ),
        step=1,
        frame=MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png"),
        png=b"png",
        journal=journal,
        circuit_breaker=WorkerActionCircuitBreaker(),
    )

    assert terminal is None
    assert payload["status"] == "aborted"
    assert payload["executed_actions"] == 1
    assert "flash verifier reported off_target" in payload["reason"]
    assert len(runtime._executor.actions) == 1


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
        lambda platform, png, *, action_type: (0.0, False),
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
        lambda platform, png, *, action_type: (0.0, False),
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
        lambda platform, png, *, action_type: (0.0, False),
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
        lambda platform, png, *, action_type: (0.0, False),
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
    assert Strategy.route(outcome) == "replace"


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

def test_collector_can_complete_on_partial_under_pure_react() -> None:
    """Pure ReAct collector: complete is always available; LLM decides sufficiency.

    Even with partial coverage (more pages exist), the worker may call complete
    and runtime snapshots the rows seen so far (accum or current frame).
    """
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
    tools = runtime._worker_tools_for_frame(spec, spec._test_actions, frame)
    assert "complete" in {tool["function"]["name"] for tool in tools}

    # Execute complete on partial: succeeds, returns snapshot with the 1 row.
    payload, terminal = runtime._execute_worker_tool(
        spec,
        spec._test_actions,
        {"name": "complete", "args": {}},
        b"png",
        frame,
    )
    assert terminal == "complete"
    assert payload["row_count"] == 1
    assert payload.get("coverage", {}).get("react_complete") is True


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

    tools = runtime._worker_tools_for_frame(spec, spec._test_actions, frame)
    complete = next(tool for tool in tools if tool["function"]["name"] == "complete")
    assert "collection_ref" not in complete["function"]["parameters"]["properties"]
    payload, terminal = runtime._execute_worker_tool(
        spec,
        spec._test_actions,
        {"name": "complete", "args": {"evidence": ["coverage complete"]}},
        b"png",
        frame,
    )

    assert terminal == "complete"
    assert payload["ref"] == collection.ref


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
    assert "Observe frame:1" in events[0]["message"]
    assert live_trace["phase"] == "running"
    assert live_trace["trace"] == events
    assert "--- Turn 1 ---" in (
        tmp_path / "tool_agent.log"
    ).read_text(encoding="utf-8")
    assert events[0]["timestamp"]
    assert statuses == ["Observer · Observe frame:1 for ?: no collection refs"]
    assert (tmp_path / "observation_tool_agent_1.json").is_file()
    observation = (tmp_path / "observation_tool_agent_1.json").read_text(
        encoding="utf-8"
    )
    assert "runtime-secret-73" not in observation
    assert "session access value redacted" in observation
    assert (tmp_path / "tool_agent_data_store.json").is_file()


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
    journal.executed_tools.add("search_target")
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


def test_runtime_replaces_strategy_inside_worker_call_without_replaying_program(tmp_path) -> None:
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
    runtime._request_strategy_decision = lambda **_kwargs: (replacement, "selected")
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
        "collect_records_strategy_1",
    ]
    assert [spec.strategy.approach for _, spec in worker_calls] == [
        "Traverse the requested collection from the current UI.",
        "Use a different visible collection traversal path.",
    ]
    assert runtime.master.sources == []
    assert any(event["event"] == "strategy_worker_dispatched" for event in run.trace)
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

