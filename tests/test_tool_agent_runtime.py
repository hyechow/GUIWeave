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
)
from gui_agent.core.tool_agent.data_store import RuntimeDataStore
from gui_agent.core.tool_agent.protocol import (
    MAX_ORDERED_ACTIONS,
    ProtocolError,
    capability_parameters,
)
from gui_agent.core.tool_agent.runtime import ToolAgentRuntime
from gui_agent.core.schemas import TargetVerify
from gui_agent.adapters.browser.control_grounding import ground_action_to_nearest_control


def _state(*, missing: bool) -> str:
    return json.dumps(
        {
            "status": "exploring",
            "summary": "A separate apply control is visible.",
            "established_facts": [],
            "open_gaps": ["Apply the configured filter"] if missing else [],
            "coverage": {},
            "action_space_status": "missing_action" if missing else "sufficient",
            "missing_action": "Tap the visible apply control" if missing else "",
            "next_instruction": "Continue the same subgoal on this frame.",
        }
    )


def test_runtime_rejects_max_turns_above_50(tmp_path) -> None:
    with pytest.raises(ValueError, match="cannot exceed 50"):
        ToolAgentRuntime(
            bundle=SimpleNamespace(platform="browser"),
            platform=SimpleNamespace(),
            log_dir=tmp_path,
            perception_mode="enhanced",
            max_turns=51,
        )


def test_android_runtime_rejects_browser_only_worker_action() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.bundle = SimpleNamespace(platform="android")
    runtime._platform_capabilities = frozenset({"tap", "scroll", "back"})
    runtime.data_store = RuntimeDataStore()
    spec = WorkerSpec(
        goal="Choose the required visible option",
        success_criteria=["The requested option is selected"],
        actions=[DynamicActionSpec(
            name="choose_option",
            capability="select_option",
            description="Choose the visible option required by the task",
            fixed_args={"text": "Enabled"},
        )],
    )

    with pytest.raises(ProtocolError, match="unavailable on the android adapter"):
        runtime._initial_worker_actions(spec)


def test_android_runtime_discovers_installed_apps_for_worker_prompt() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime._platform_capabilities = frozenset({"tap", "launch_app"})
    runtime.platform = SimpleNamespace(list_apps=lambda: ["Settings", "Calendar"])
    runtime._master_knowledge = ""
    runtime._worker_access_context = ""
    spec = WorkerSpec(
        goal="Open Calendar",
        success_criteria=["Calendar is visible"],
        actions=[DynamicActionSpec(
            name="open_calendar",
            capability="launch_app",
            description="Open the Calendar application",
            fixed_args={"app": "Calendar"},
        )],
    )

    prompt = runtime._worker_system_prompt(spec, runtime._initial_worker_actions(spec))

    assert '"Calendar"' in prompt
    assert '"Settings"' in prompt


@pytest.mark.parametrize(
    ("platform_name", "capabilities", "applications", "expected", "excluded"),
    [
        (
            "android",
            {"tap", "drag", "launch_app"},
            ["Settings", "Calendar"],
            {"tap", "drag", "launch_app"},
            {"open_url", "select_option"},
        ),
        (
            "browser",
            {"tap", "open_url", "select_option"},
            [],
            {"tap", "open_url", "select_option"},
            {"drag", "launch_app"},
        ),
    ],
)
def test_platform_prompt_context_contains_only_active_adapter_contracts(
    platform_name: str,
    capabilities: set[str],
    applications: list[str],
    expected: set[str],
    excluded: set[str],
) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.bundle = SimpleNamespace(platform=platform_name)
    runtime._platform_capabilities = frozenset(capabilities)
    runtime._installed_app_names = tuple(applications)

    context = runtime._platform_prompt_context()

    assert context["name"] == platform_name
    assert set(context["action_contracts"]) == expected
    assert excluded.isdisjoint(context["action_contracts"])
    assert context["applications"] == applications
    for capability, schema in context["action_contracts"].items():
        assert schema == capability_parameters(capability)


def test_private_access_context_reaches_worker_but_is_redacted_from_trace() -> None:
    from gui_agent.core.tool_agent.runtime import _access_log_redactions

    access_context = (
        "# Deployment\n"
        "Username: `runtime-user-73`\n"
        "Password: `runtime-secret-73`"
    )
    runtime = object.__new__(ToolAgentRuntime)
    runtime._worker_access_context = access_context
    runtime._master_knowledge = "Account settings are available from the profile menu."
    runtime._access_log_redactions = _access_log_redactions(access_context)
    runtime.trace = []
    spec = WorkerSpec(
        goal="Reach the authenticated page",
        success_criteria=["The authenticated page is visible"],
        actions=[DynamicActionSpec(
            name="submit_login",
            capability="tap",
            description="Submit the visible login form",
        )],
    )

    prompt = runtime._worker_system_prompt(spec, spec.actions)

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


def test_worker_prompt_keeps_stable_context_before_compact_attempt_contract() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime._master_knowledge = "The settings page is reached from the profile menu."
    runtime._worker_access_context = "Use the active authenticated session."
    first = WorkerSpec(
        goal="Find the requested record",
        success_criteria=["The record is visible"],
        actions=[DynamicActionSpec(
            name="search_record",
            capability="type",
            description="Search the visible record grid using the requested literal.",
            fixed_args={"text": "record-17"},
        )],
    )
    revised = WorkerSpec(
        goal="Find the requested record using a different visible route",
        success_criteria=["The record is visible"],
        actions=[DynamicActionSpec(
            name="open_records",
            capability="tap",
            description="Open the visible records control.",
        )],
    )

    first_prompt = runtime._worker_system_prompt(first, first.actions)
    revised_prompt = runtime._worker_system_prompt(revised, revised.actions)
    delimiter = "## Worker attempt contract"

    assert first_prompt.split(delimiter, 1)[0] == revised_prompt.split(delimiter, 1)[0]
    assert first_prompt.index("Application knowledge") < first_prompt.index(delimiter)
    assert first_prompt.index("Session access context") < first_prompt.index(delimiter)
    assert '"fixed_args": {"text": "record-17"}' in first_prompt
    assert "Search the visible record grid using the requested literal." not in first_prompt
    attempt_contract = first_prompt.split(delimiter, 1)[1]
    assert '"capability"' not in attempt_contract
    assert '"exposed_args"' not in attempt_contract


def test_runtime_materializes_result_ref_into_fixed_action_argument() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.data_store = RuntimeDataStore()
    descriptor = runtime.data_store.put_result(
        {"description": "3 customer(s) love it!"},
        {
            "type": "object",
            "properties": {"description": {"type": "string"}},
            "required": ["description"],
        },
    )
    spec = WorkerSpec.model_validate({
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


def test_global_turn_budget_is_shared_across_logical_workers() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_turns = 2
    runtime.max_subgoal_replans = 2
    runtime._frame_no = 0
    runtime.trace = []
    events = []
    runtime._trace = lambda event, **payload: events.append({"event": event, **payload})
    calls = []

    def run_worker(worker_id, _spec):
        calls.append(worker_id)
        runtime._frame_no += 1
        return WorkerOutcome(
            phase="completed",
            summary=f"Completed {worker_id}",
            steps=1,
        )

    runtime._run_worker = run_worker
    spec = WorkerSpec.model_validate({
        "profile": "operator",
        "goal": "Complete one UI subgoal",
        "success_criteria": ["The subgoal is complete"],
        "actions": [{
            "name": "advance_subgoal",
            "capability": "tap",
            "description": "Advance the visible subgoal",
        }],
    })

    first = runtime._run_worker_with_local_replanning("first_worker", spec)
    second = runtime._run_worker_with_local_replanning("second_worker", spec)
    blocked = runtime._run_worker_with_local_replanning("third_worker", spec)

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


def test_redelegation_failure_reports_all_consumed_worker_steps() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_turns = 50
    runtime.max_subgoal_replans = 2
    runtime._frame_no = 0
    runtime._trace = lambda *_args, **_kwargs: None
    attempts = iter((3, 4))
    runtime._run_worker = lambda _worker_id, _spec: WorkerOutcome(
        phase="failed",
        summary="Try another local strategy",
        steps=next(attempts),
    )
    revisions = 0

    def revise(**_kwargs):
        nonlocal revisions
        revisions += 1
        if revisions == 1:
            return spec
        raise ValueError("replacement is invalid")

    runtime._revise_worker_spec = revise
    spec = WorkerSpec.model_validate({
        "profile": "operator",
        "goal": "Complete one UI subgoal",
        "success_criteria": ["The subgoal is complete"],
        "actions": [{
            "name": "advance_subgoal",
            "capability": "tap",
            "description": "Advance the visible subgoal",
        }],
    })

    outcome = runtime._run_worker_with_local_replanning("logical_worker", spec)

    assert outcome.phase == "failed"
    assert outcome.steps == 7
    assert "redelegation failed" in outcome.summary


class _Worker:
    def __init__(self) -> None:
        self.responses = [
            SimpleNamespace(
                content=_state(missing=True),
                tool_calls=[
                    {
                        "id": "patch-1",
                        "name": "request_action_patch",
                        "args": {
                            "name": "apply_visible_filter",
                            "capability": "tap",
                            "description": "Apply the currently configured visible filter",
                            "reason": "The current frame shows a separate apply control.",
                        },
                    }
                ],
            ),
            SimpleNamespace(
                content=_state(missing=False),
                tool_calls=[
                    {
                        "id": "tap-1",
                        "name": "apply_visible_filter",
                        "args": {"x": 900, "y": 650},
                    }
                ],
            ),
        ]
        self.bound_tool_names: list[set[str]] = []

    def bind_tools(self, tools, **kwargs):
        del kwargs
        self.bound_tool_names.append({tool["function"]["name"] for tool in tools})
        return self

    def invoke(self, messages):
        del messages
        return self.responses.pop(0)


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


class _ImmediateVerifyPool:
    def __init__(self, *values: TargetVerify) -> None:
        self.values = list(values)
        self.submitted = []

    def submit(self, function, *args):
        self.submitted.append((function, args))
        value = self.values.pop(0)
        return SimpleNamespace(result=lambda timeout=None: value)


class _BrowserSnapExecutor(_Executor):
    def execute(self, decision, **kwargs):
        decision.action.snap = {
            "method": "dom",
            "snapped": [525.0, 540.0],
            "info": "Create Channel",
        }
        return super().execute(decision, **kwargs)


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
                    "name": "runtime_tap_visible",
                    "args": {"x": 400, "y": 300, "description": "Advance"},
                }],
            )
        return SimpleNamespace(content=_state(missing=False), tool_calls=[])


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
                "name": "runtime_type_visible",
                "args": {
                    "state": {
                        "status": "exploring",
                        "summary": "The date input is visible.",
                        "next_instruction": "Enter the required date.",
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
                "next_instruction": "Enter the end date.",
            },
            "x": 207,
            "y": 550,
            "text": "05/31/2023",
            "description": "Enter the end date into the Purchase Date to input",
        }
        if self.calls == 4:
            args["x"] = 207
            args["y"] = 448
        return SimpleNamespace(
            content="",
            tool_calls=[{
                "id": f"type-{self.calls}",
                "name": "runtime_type_visible",
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
                        "next_instruction": "Continue scrolling to collect it.",
                    },
                    "amount": "medium",
                },
            }],
        )


_LOGIN_ACTIONS = [
    {"name": "runtime_type_visible", "args": {"x": 500, "y": 400, "text": "demo-user", "description": "Enter Username"}},
    {"name": "runtime_type_visible", "args": {"x": 500, "y": 500, "text": "demo-pass", "description": "Enter Password"}},
    {"name": "runtime_tap_visible", "args": {"x": 500, "y": 600, "description": "Tap Sign in"}},
]


class _MultiActionWorker:
    def __init__(self, action_batches: list[list[dict]] | None = None) -> None:
        self.calls = 0
        self.bound_names: set[str] = set()
        self.bound_schemas: list[str] = []
        self.action_batches = action_batches

    def bind_tools(self, tools, **kwargs):
        assert kwargs.get("parallel_tool_calls") is False
        self.bound_names = {tool["function"]["name"] for tool in tools}
        self.bound_schemas.append(json.dumps(tools))
        return self

    def invoke(self, messages):
        del messages
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
                    "status": "exploring",
                    "summary": "The complete login form is visible.",
                    "next_instruction": "Fill and submit the login form.",
                },
                "actions": actions,
            },
        }])


_GUARD_REPAIR_ACTIONS = [
    [{
        "name": "runtime_scroll_visible",
        "args": {
            "direction": "down",
            "amount": "medium",
            "description": "Scroll to reveal Material",
        },
    }],
    [{"name": "task_action", "args": {"x": 500, "y": 100}}],
]


def _run_fused_worker(
    monkeypatch,
    *,
    current_url: str,
    worker=None,
    actions: list[DynamicActionSpec] | None = None,
    controls: list[dict] | None = None,
    requirement_scopes: dict[str, dict] | None = None,
    target_verify: TargetVerify | None = None,
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
    runtime._target_verify_pool = (
        _ImmediateVerifyPool(target_verify) if target_verify else None
    )
    runtime.observe_calls = 0

    def observe(_spec):
        runtime.observe_calls += 1
        return MaterializedFrame(
            frame_id="frame:1",
            screenshot_path="frame.png",
            url="https://example.test/login",
            controls=controls or [],
            requirement_scopes=requirement_scopes or {},
        ), b"initial-png"

    runtime._observe = observe
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, *, action_type: (0.0, False),
    )
    spec = WorkerSpec(
        goal="Complete the visible local interaction",
        success_criteria=["The requested interface state is reached"],
        actions=actions or [DynamicActionSpec(
            name="task_action",
            capability="tap",
            description="Complete the visible local interaction",
        )],
        max_steps=1,
    )
    runtime._run_worker("fused-worker", spec)
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
    assert "runtime_type_visible" not in runtime.worker.bound_names
    assert len(runtime._executor.actions) == expected_actions
    assert any(event["event"] == expected_event for event in runtime.trace)
    if expected_actions == 3:
        assert any(status.startswith("Action · 2/3 · type") for status in runtime.statuses)


def test_fused_worker_repairs_guarded_first_action_on_same_frame(monkeypatch) -> None:
    worker = _MultiActionWorker(_GUARD_REPAIR_ACTIONS)
    runtime = _run_fused_worker(
        monkeypatch,
        current_url="https://example.test/item",
        worker=worker,
        controls=[{
            "kind": "button",
            "label": "Filters",
            "rect": {"x": 500, "y": 100, "w": 100, "h": 40},
        }],
        requirement_scopes={"records": {
            "status": "unknown",
            "detail_resolution": {
                "detail_fields": ["material"],
                "current_observed_detail_fields": ["material"],
            },
        }},
        actions=[DynamicActionSpec(
                name="task_action",
                capability="tap",
                description="Open the visible Filters button",
        )],
    )
    assert runtime.observe_calls == 1
    assert runtime.worker.calls == 2
    assert len(runtime._executor.actions) == 1
    assert any(event["event"] == "worker_action_blocked" for event in runtime.trace)
    assert "runtime_scroll_visible" in worker.bound_schemas[0]
    assert "runtime_scroll_visible" in worker.bound_schemas[1]


def test_multi_action_suffix_requires_stable_visible_targets() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    specs = {
        name: DynamicActionSpec(name=name, capability=capability, description=name)
        for name, capability in {
            "tap": "tap", "clear": "clear_text", "type": "type",
            "select": "select_option", "scroll": "scroll",
        }.items()
    }
    type_call = {"name": "type", "args": {"x": 500, "y": 400}}

    assert runtime._suffix_requires_reobservation(
        call={"name": "tap", "args": {"x": 500, "y": 400}},
        action=specs["tap"],
        remaining=[
            {"name": "clear", "args": {}},
            type_call,
        ],
        action_by_name=specs,
    ) == ""
    assert "invalidate coordinates" in runtime._suffix_requires_reobservation(
        call={"name": "tap", "args": {"x": 500, "y": 400}},
        action=specs["tap"],
        remaining=[type_call, {"name": "tap", "args": {"x": 500, "y": 600}}],
        action_by_name=specs,
    )
    assert "invalidate coordinates" in runtime._suffix_requires_reobservation(
        call={"name": "scroll", "args": {}},
        action=specs["scroll"],
        remaining=[type_call],
        action_by_name=specs,
    )
    assert "invalidate coordinates" in runtime._suffix_requires_reobservation(
        call={"name": "select", "args": {"text": "Complete"}},
        action=specs["select"],
        remaining=[type_call],
        action_by_name=specs,
    )


def test_android_focus_requires_reobservation_before_type() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime._executor = type("AndroidExecutorStub", (), {
        "tap_type_suffix_safe": False,
    })()
    tap = DynamicActionSpec(name="tap", capability="tap", description="focus")
    type_action = DynamicActionSpec(name="type", capability="type", description="type")

    reason = runtime._suffix_requires_reobservation(
        call={"name": "tap", "args": {"x": 500, "y": 900}},
        action=tap,
        remaining=[{"name": "type", "args": {"x": 500, "y": 900}}],
        action_by_name={"tap": tap, "type": type_action},
    )

    assert "invalidate coordinates" in reason

    runtime._executor.type_suffix_safe = False
    reason = runtime._suffix_requires_reobservation(
        call={"name": "type", "args": {"x": 500, "y": 900}},
        action=type_action,
        remaining=[{"name": "tap", "args": {"x": 500, "y": 700}}],
        action_by_name={"tap": tap, "type": type_action},
    )
    assert "reflow" in reason


def test_multi_action_suffix_allows_distinct_visible_noncommit_taps() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    tap = DynamicActionSpec(name="tap", capability="tap", description="tap")
    actions = {"tap": tap}
    frame = MaterializedFrame(
        frame_id="frame:members",
        screenshot_path="frame.png",
        controls=[{
            "kind": "button" if label == "Add Members" else "checkbox",
            "label": label, "ref": f"row:{label}",
            "selection_mode": "multiple",
            "rect": {"x": 500, "y": y, "w": 1000, "h": 60},
            **({"form_action": "commit"} if label == "Add Members" else {}),
        } for label, y in (("alex", 240), ("arjun", 320), ("Add Members", 900))],
    )

    stable = runtime._suffix_requires_reobservation(
        call={"name": "tap", "args": {"x": 900, "y": 240}},
        action=tap,
        remaining=[{"name": "tap", "args": {"x": 900, "y": 320}}],
        action_by_name=actions,
        frame=frame,
    )
    commit = runtime._suffix_requires_reobservation(
        call={"name": "tap", "args": {"x": 900, "y": 320}},
        action=tap,
        remaining=[{"name": "tap", "args": {"x": 500, "y": 900}}],
        action_by_name=actions,
        frame=frame,
    )

    assert stable == ""
    assert "invalidate coordinates" in commit


def test_multi_action_runtime_accepts_five_and_rejects_six_calls() -> None:
    actions = [DynamicActionSpec(
        name="tap",
        capability="tap",
        description="Tap a target",
    )]
    calls = [{"name": "tap", "args": {}}] * MAX_ORDERED_ACTIONS

    ToolAgentRuntime._validate_multi_action_calls(calls, actions)
    with pytest.raises(ProtocolError, match=f"1–{MAX_ORDERED_ACTIONS} actions"):
        ToolAgentRuntime._validate_multi_action_calls([*calls, calls[0]], actions)


def test_worker_patches_action_space_and_acts_on_same_frame(monkeypatch) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.trace = []
    runtime.worker = _Worker()
    runtime._executor = _Executor()
    runtime.platform = object()
    observe_calls = []

    def _observe(spec):
        observe_calls.append(spec)
        return (
            MaterializedFrame(
                frame_id="frame:1",
                screenshot_path="frame.png",
            ),
            b"png",
        )

    runtime._observe = _observe
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, *, action_type: (0.0, False),
    )
    spec = WorkerSpec(
        goal="Complete one cohesive filtered-data subgoal",
        success_criteria=["The configured filter is applied"],
        actions=[
            DynamicActionSpec(
                name="reveal_more",
                capability="scroll",
                description="Reveal more content",
                fixed_args={"direction": "down"},
            )
        ],
        max_steps=1,
    )

    outcome = runtime._run_worker("filtered_subgoal", spec)

    assert outcome.steps == 1
    assert len(observe_calls) == 1
    assert "apply_visible_filter" not in runtime.worker.bound_tool_names[0]
    assert "apply_visible_filter" in runtime.worker.bound_tool_names[1]
    assert len(runtime._executor.actions) == 1
    assert runtime._executor.actions[0].action_type == "tap"
    assert runtime._executor.actions[0].x == 900
    assert runtime._executor.actions[0].y == 650
    patches = [event for event in runtime.trace if event["event"] == "worker_action_patch"]
    assert len(patches) == 1
    assert patches[0]["frame_id"] == "frame:1"


def test_worker_compatibly_accepts_missing_content_without_another_llm_call(monkeypatch) -> None:
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
    spec = WorkerSpec(
        goal="Advance one cohesive subgoal",
        success_criteria=["The visible control is activated"],
        actions=[DynamicActionSpec(
            name="reveal_more",
            capability="scroll",
            description="Reveal more content",
            fixed_args={"direction": "down"},
        )],
        max_steps=1,
    )

    outcome = runtime._run_worker("advance_subgoal", spec)

    assert outcome.phase == "failed"
    assert len(runtime._executor.actions) == 1
    assert runtime._executor.actions[0].x == 400
    recovered = [event for event in runtime.trace if event["event"] == "worker_state_recovered"]
    assert recovered == []
    decisions = [event for event in runtime.trace if event["event"] == "worker_decision"]
    assert decisions[0]["state_source"] == "runtime_compat"
    assert "assistant content state unavailable" in " ".join(
        decisions[0]["state_compatibility"]
    )


def test_retried_gui_worker_retains_bounded_journal_experience(monkeypatch) -> None:
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
    spec = WorkerSpec(
        goal="Advance one cohesive subgoal",
        success_criteria=["The visible control is activated"],
        actions=[DynamicActionSpec(
            name="reveal_more",
            capability="scroll",
            description="Reveal more content",
            fixed_args={"direction": "down"},
        )],
        max_steps=1,
    )

    first = runtime._run_worker("advance_subgoal", spec)
    second = runtime._run_worker("advance_subgoal_replan_1", spec)

    assert first.phase == second.phase == "failed"
    starts = [event for event in runtime.trace if event["event"] == "worker_started"]
    assert [(event["attempt"], event["retained_memory_events"]) for event in starts] == [
        (1, 0),
        (2, 1),
    ]
    decisions = [event for event in runtime.trace if event["event"] == "worker_decision"]
    assert [event["memory_event_count"] for event in decisions] == [0, 2]


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
    spec = WorkerSpec(
        goal="Enter a required value",
        success_criteria=["The value is entered"],
        actions=[DynamicActionSpec(
            name="reveal_more",
            capability="scroll",
            description="Reveal more content",
            fixed_args={"direction": "down"},
        )],
        max_steps=1,
    )

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
    assert decision["state_source"] == "tool_args"
    assert decision["args"]["x"] == 200
    assert decision["args"]["y"] == 380


def test_worker_fuses_third_repeated_action_and_accepts_same_frame_ref_repair(
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
    spec = WorkerSpec(
        goal="Set the order end date",
        success_criteria=["The end date is set"],
        actions=[DynamicActionSpec(
            name="reveal_more",
            capability="scroll",
            description="Reveal more content",
            fixed_args={"direction": "down"},
        )],
        max_steps=3,
    )

    outcome = runtime._run_worker("ground_date", spec)

    assert outcome.phase == "failed"
    assert len(observed) == 3
    assert runtime.worker.calls == 4
    assert len(runtime._executor.actions) == 3
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
    assert blocked[0]["prior_attempts"] == 2


def test_worker_does_not_fuse_repeated_scrolls_that_change_visual_frame(
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
    spec = WorkerSpec(
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

    outcome = runtime._run_worker("visual_collection", spec)

    assert outcome.phase == "failed"
    assert len(observe_calls) == 3
    assert runtime.worker.calls == 3
    assert len(runtime._executor.actions) == 3
    assert not any(
        event["event"] == "worker_action_blocked"
        for event in runtime.trace
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
    spec = WorkerSpec(
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


@pytest.mark.parametrize(
    ("action_type", "executor", "expected_point"),
    [
        (AndroidAction, _Executor(), (480.0, 500.0)),
        (BrowserAction, _BrowserSnapExecutor(), (525.0, 540.0)),
    ],
)
def test_worker_target_verify_uses_final_platform_point(
    monkeypatch, action_type, executor, expected_point,
) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.bundle = SimpleNamespace(
        make_action=lambda payload: action_type.model_validate(payload)
    )
    runtime.perception_mode = "enhanced"
    runtime._executor = executor
    runtime._visualizer = None
    runtime.platform = object()
    runtime.trace = []
    runtime._target_verify_pool = _ImmediateVerifyPool(TargetVerify(
        on_target=False,
        actual_element="Create Channel",
        reason="The marker is on the adjacent control.",
    ))
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, *, action_type: (0.0, False),
    )
    action = DynamicActionSpec(
        name="activate_visible_control",
        capability="tap",
        description="Activate a visible control",
    )
    spec = WorkerSpec(
        goal="Open the channel form",
        success_criteria=["The channel form opens"],
        actions=[action],
    )

    payload, terminal = runtime._execute_worker_tool(
        spec,
        [action],
        {
            "name": action.name,
            "args": {
                "x": 480,
                "y": 500,
                "description": "Activate Create Channel",
            },
        },
        b"png",
        MaterializedFrame(frame_id="frame:1", screenshot_path="frame.png"),
    )

    assert terminal is None
    assert payload["target_signal"]["status"] == "off_target"
    assert runtime._target_verify_pool.submitted[0][1][1:3] == expected_point


def test_multi_action_aborts_suffix_after_flash_off_target(monkeypatch) -> None:
    runtime = _run_fused_worker(
        monkeypatch,
        current_url="https://example.test/login",
        target_verify=TargetVerify(
        on_target=False,
        actual_element="Username",
        reason="The marker missed the Password field.",
        ),
    )

    assert len(runtime._executor.actions) == 1
    aborted = next(
        event for event in runtime.trace
        if event["event"] == "worker_multi_action_aborted"
    )
    assert "flash verifier reported off_target" in aborted["reason"]


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
        name="runtime_scroll_visible",
        capability="scroll",
        description="Scroll the main content to reveal the required detail",
        exposed_args=["direction", "amount", "target_area", "description"],
    )
    spec = WorkerSpec(
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
    spec = WorkerSpec(
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
    spec = WorkerSpec(
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
    spec = WorkerSpec(
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


def test_runtime_surfaces_same_origin_platform_rejection(monkeypatch) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.bundle = SimpleNamespace(
        make_action=lambda payload: BrowserAction.model_validate(payload)
    )
    runtime.perception_mode = "enhanced"
    runtime._executor = _Executor()
    runtime._visualizer = None
    runtime._active_worker_id = "submit_worker"
    runtime._worker_platform_rejections = {}
    rejection_feedback = [{
        "kind": "xhr",
        "url": "https://example.test/action",
        "status": 200,
        "body": '{"error":true,"message":"The action is not allowed."}',
    }]
    feedback = iter((rejection_feedback, rejection_feedback, []))
    runtime.platform = SimpleNamespace(client=SimpleNamespace(
        consume_action_feedback=lambda: next(feedback)
    ))
    runtime._trace = lambda *_args, **_kwargs: None
    monkeypatch.setattr(
        "gui_agent.core.tool_agent.runtime.settle_after_action",
        lambda platform, png, *, action_type: (0.0, False),
    )
    action = DynamicActionSpec(
        name="submit_change",
        capability="tap",
        description="Submit the requested change",
    )
    spec = WorkerSpec(
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

    assert payload["status"] == "failed"
    assert payload["platform_feedback"] == [{
        "status": 200,
        "url": "https://example.test/action",
        "rejected": True,
        "message": "The action is not allowed.",
    }]
    assert terminal is None

    completed_payload, completed_terminal = runtime._execute_worker_tool(
        spec,
        [action],
        {"name": "complete", "args": {}},
        b"png",
        MaterializedFrame(frame_id="frame:2", screenshot_path="frame.png"),
    )

    assert completed_terminal == "platform_rejected"
    assert completed_payload["reason"] == "The action is not allowed."

    repeated_payload, repeated_terminal = runtime._execute_worker_tool(
        spec,
        [action],
        {"name": action.name, "args": {"x": 500, "y": 500}},
        b"png",
        MaterializedFrame(frame_id="frame:3", screenshot_path="frame.png"),
    )

    assert repeated_terminal == "platform_rejected"
    assert repeated_payload["reason"] == "The action is not allowed."

    recovered_payload, recovered_terminal = runtime._execute_worker_tool(
        spec,
        [action],
        {"name": action.name, "args": {"x": 500, "y": 500}},
        b"png",
        MaterializedFrame(frame_id="frame:4", screenshot_path="frame.png"),
    )
    completed_payload, completed_terminal = runtime._execute_worker_tool(
        spec,
        [action],
        {"name": "complete", "args": {}},
        b"png",
        MaterializedFrame(frame_id="frame:5", screenshot_path="frame.png"),
    )

    assert recovered_payload["status"] == "executed"
    assert recovered_terminal is None
    assert completed_payload == {"status": "completed"}
    assert completed_terminal == "complete"
    assert runtime._worker_platform_rejections == {}


def test_runtime_open_url_rejects_task549_inferred_route_before_navigation() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime._task_goal = "Add a new size option to a product"
    runtime._task_page_url = "http://example.test/admin"
    runtime._master_knowledge = "Product Attributes are under Stores > Attributes > Product."
    action = DynamicActionSpec(
        name="runtime_open_url",
        capability="open_url",
        description="Open a sourced URL",
    )
    spec = WorkerSpec(
        goal="Open Product Attributes",
        success_criteria=["The Product Attributes grid is visible"],
        actions=[action],
    )

    with pytest.raises(ValueError, match="rejected an inferred URL"):
        runtime._validate_runtime_open_url(
            "http://example.test/admin/catalog/product/attribute/",
            spec=spec,
            frame=MaterializedFrame(
                frame_id="frame:6",
                screenshot_path="frame.png",
                url="http://example.test/admin/catalog/product/",
            ),
        )


def test_runtime_open_url_accepts_exact_knowledge_route_with_replaced_host() -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime._task_goal = "Open the review page"
    runtime._task_page_url = "http://new-host.test/admin"
    runtime._master_knowledge = "Exact route: http://old-host.test/admin/reviews/pending/"
    action = DynamicActionSpec(
        name="runtime_open_url",
        capability="open_url",
        description="Open a sourced URL",
    )
    spec = WorkerSpec(
        goal="Open pending reviews",
        success_criteria=["Pending reviews are visible"],
        actions=[action],
    )

    runtime._validate_runtime_open_url(
        "http://new-host.test/admin/reviews/pending/",
        spec=spec,
        frame=None,
    )


def test_collector_completion_is_unavailable_until_collection_is_ready() -> None:
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
    spec = WorkerSpec(
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
    tools = runtime._worker_tools_for_frame(spec, spec.actions, frame)
    assert "complete" not in {tool["function"]["name"] for tool in tools}

    with pytest.raises(ValueError, match="complete is unavailable"):
        runtime._execute_worker_tool(
            spec,
            spec.actions,
            {
                "name": "complete",
                "args": {},
            },
            b"png",
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
    spec = WorkerSpec(
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

    tools = runtime._worker_tools_for_frame(spec, spec.actions, frame)
    complete = next(tool for tool in tools if tool["function"]["name"] == "complete")
    assert "collection_ref" not in complete["function"]["parameters"]["properties"]
    payload, terminal = runtime._execute_worker_tool(
        spec,
        spec.actions,
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
    spec = WorkerSpec(
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
        actions=[{
            "name": "reveal_more",
            "capability": "scroll",
            "description": "Reveal more records",
            "fixed_args": {"direction": "down"},
            "exposed_args": ["amount"],
        }],
        max_steps=4,
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


def test_runtime_replans_inside_worker_call_without_replaying_program(tmp_path) -> None:
    runtime = object.__new__(ToolAgentRuntime)
    runtime.max_subgoal_replans = 1
    runtime.max_compile_attempts = 1
    runtime.data_store = RuntimeDataStore()
    runtime.trace = []
    replacement = {
        "profile": "collector",
        "goal": "Collect the requested records",
        "success_criteria": ["The requested records are collected"],
        "data_requirements": [{
            "id": "records",
            "description": "Requested records",
            "row_schema": {
                "type": "object",
                "properties": {"value": {"type": "integer"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        }],
        "actions": [{
            "name": "reveal_more_aggressively",
            "capability": "scroll",
            "description": "Reveal a larger window of records",
            "fixed_args": {
                "direction": "down",
                "amount": "large",
                "target_area": "main_content",
            },
        }],
        "max_steps": 4,
    }
    runtime.master = _CodingMaster(
        _coding_program(),
        json.dumps({"worker_spec": replacement}),
    )
    runtime.master_cfg = SimpleNamespace(model="coding-master")
    runtime.worker_cfg = SimpleNamespace(model="visual-worker")
    runtime.materializer = SimpleNamespace(model="perception")
    runtime.perception_mode = "enhanced"
    runtime.log_dir = tmp_path
    worker_calls = []

    def run_worker(worker_id, spec):
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
            summary="Collected after local replan",
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
        "collect_records_replan_1",
    ]
    assert runtime.master.sources == []
    assert any(event["event"] == "master_worker_redelegated" for event in run.trace)
    assert not any(event["event"] == "subgoal_replan" for event in run.trace)
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
    runtime.max_subgoal_replans = 2
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

    def fail_worker(worker_id, spec):
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
    runtime.max_subgoal_replans = 0
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
