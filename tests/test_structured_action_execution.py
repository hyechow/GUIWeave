from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision
from gui_agent.adapters.browser.control_grounding import (
    ground_rendered_action,
    rendered_target_evidence,
    resolve_native_control_action,
)
from gui_agent.core.run import action_exec as action_exec_module
from gui_agent.core.run.action_exec import ActionExecutor
from gui_agent.core.schemas import Observation, SupervisorStep


class _Policy:
    def __init__(self) -> None:
        self.vision_called = False
        self.evidence_context = ""

    def resolve_native_action(self, observation, **kwargs):
        return resolve_native_control_action(observation.form_controls, **kwargs)

    def ground_rendered_action(self, decision, observation, **kwargs):
        return ground_rendered_action(decision, observation.form_controls, **kwargs)

    def action_evidence_context(self, observation, **kwargs):
        return rendered_target_evidence(observation.form_controls, **kwargs)

    def decide(self, *_args, **kwargs):
        self.vision_called = True
        self.evidence_context = str(kwargs.get("evidence_context") or "")
        return BrowserActionDecision(action=BrowserAction(
            action_type="type",
            x=820,
            y=175,
            text="XXXL",
            description="在 Admin Description 输入 XXXL",
        ))


class _Supervisor:
    def __init__(self) -> None:
        self._context_reports: list[dict] = []
        self._timings: dict[str, float] = {}
        self._timings_order: list[str] = []
        self._token_usage: dict[str, dict[str, int]] = {}


def test_action_execution_grounds_rendered_input_after_vision(monkeypatch, tmp_path) -> None:
    policy = _Policy()
    supervisor = _Supervisor()
    observation = Observation(
        png_bytes=b"fixture",
        source="browser",
        form_controls=[{
            "label": "Description",
            "kind": "text_input",
            "value": "",
            "group_id": "collection:20",
            "group_field": "Admin",
            "rect": {"x": 578, "y": 668},
        }],
    )
    step = SupervisorStep(
        should_act=True,
        instruction="在 Admin Description 输入 XXXL",
        summary="typed write",
        statement_id="size-option",
        atomic_role="write",
        action_family="input",
        target_control="Admin Description",
        target_value="XXXL",
    )
    monkeypatch.setattr(action_exec_module, "print_decision", lambda *_args, **_kwargs: None)

    decision = ActionExecutor()._decide_action(
        sv_step=step,
        observation=observation,
        action_policy=policy,
        supervisor=supervisor,
        log_dir=tmp_path,
        turn_no=1,
        status=lambda *_args: None,
        say=lambda *_args: None,
    )

    assert policy.vision_called is True
    assert decision.action.action_type == "type"
    assert decision.action.text == "XXXL"
    assert "declared_target='Admin Description'" in policy.evidence_context
    assert "current_value=''" in policy.evidence_context
    assert supervisor._context_reports[-1]["kind"] == "action_grounding"


def test_native_select_skips_vision_policy(monkeypatch, tmp_path) -> None:
    policy = _Policy()
    supervisor = _Supervisor()
    observation = Observation(
        png_bytes=b"fixture",
        source="browser",
        form_controls=[{
            "label": "Status",
            "kind": "native_select",
            "rect": {"x": 700, "y": 400},
        }],
    )
    step = SupervisorStep(
        should_act=True,
        instruction="将 Status 设为 Complete",
        summary="native select",
        statement_id="status",
        atomic_role="write",
        action_family="select",
        target_control="Status",
        target_value="Complete",
    )
    monkeypatch.setattr(action_exec_module, "print_decision", lambda *_args, **_kwargs: None)

    decision = ActionExecutor()._decide_action(
        sv_step=step,
        observation=observation,
        action_policy=policy,
        supervisor=supervisor,
        log_dir=tmp_path,
        turn_no=1,
        status=lambda *_args: None,
        say=lambda *_args: None,
    )

    assert policy.vision_called is False
    assert decision.action.action_type == "select_option"
    assert supervisor._context_reports[-1]["kind"] == "native_action"


def test_action_policy_grounding_failure_is_not_reinterpreted(monkeypatch, tmp_path) -> None:
    class RetryPolicy(_Policy):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def decide(self, *_args, **kwargs):
            self.calls += 1
            self.evidence_context = str(kwargs.get("evidence_context") or "")
            return BrowserActionDecision(
                action=None,
                not_found_reason="当前帧无法定位 Admin Swatch",
            )

    policy = RetryPolicy()
    supervisor = _Supervisor()
    observation = Observation(
        png_bytes=b"fixture",
        source="browser",
        form_controls=[
            {
                "label": "Description",
                "group_field": "Admin",
                "group_id": "collection:19",
                "kind": "text_input",
                "value": "XXXL",
                "rect": {"x": 578, "y": 665},
            },
            {
                "label": "Swatch",
                "group_field": "Admin",
                "group_id": "collection:19",
                "kind": "text_input",
                "value": "",
                "rect": {"x": 457, "y": 665},
            },
        ],
    )
    step = SupervisorStep(
        should_act=True,
        instruction="在 Admin Swatch 输入 XXXL",
        summary="typed write",
        statement_id="size-option",
        atomic_role="write",
        action_family="input",
        target_control="Admin Swatch",
        target_value="XXXL",
    )
    messages: list[str] = []
    monkeypatch.setattr(action_exec_module, "print_decision", lambda *_args, **_kwargs: None)

    decision = ActionExecutor()._decide_action(
        sv_step=step,
        observation=observation,
        action_policy=policy,
        supervisor=supervisor,
        log_dir=tmp_path,
        turn_no=10,
        status=lambda *_args: None,
        say=messages.append,
    )

    assert policy.calls == 1
    assert decision.action is None
    assert decision.not_found_reason == "当前帧无法定位 Admin Swatch"
    assert not any("纠正" in message or "拒绝" in message for message in messages)
