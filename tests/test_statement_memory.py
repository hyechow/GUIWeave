from gui_agent.core.run.statement_memory import (
    available_event_refs,
    build_memory_view,
    durable_kinds_present,
)
from gui_agent.core.schemas import (
    ActionIntent,
    ActionSignal,
    PolicyTurn,
    StatementContract,
    SupervisorStep,
)


def _contract() -> StatementContract:
    return StatementContract(
        id="edit",
        goal="add requested values to the current entity",
        success="all requested values are present and saved",
        inputs={"entity": {"id": "A"}},
        required_values={"values": ["30", "31"]},
        persistence="explicit_commit",
    )


def _turn(
    index: int,
    instance_id: str,
    *,
    dispatched: bool = True,
) -> PolicyTurn:
    intent = ActionIntent(
        instruction=f"perform step {index}",
        role="write",
        family="input",
        target_control="value field",
        target_value=str(index),
    )
    return PolicyTurn(
        index=index,
        observation_source="browser",
        statement_instance_id=instance_id,
        supervisor=SupervisorStep(
            statement_id="edit",
            summary=f"step {index}",
            action_intent=intent,
        ),
        executed=dispatched,
        action_signal=ActionSignal(
            role="write",
            execution="dispatched" if dispatched else "dispatch_failed",
            response="observed" if dispatched else "unknown",
        ),
    )


def test_memory_is_a_read_only_journal_projection_with_invocation_inputs():
    view = build_memory_view(
        instance_id="i1:edit",
        contract=_contract(),
        history=[_turn(1, "i1:edit"), _turn(2, "other")],
    )

    rendered = view.render_prompt_section()
    assert '"id": "A"' in rendered
    assert "30,31" in rendered
    assert [step.event_ref for step in view.recent_steps] == ["turn:1"]
    assert not hasattr(view, "phase")


def test_action_receipts_survive_narrative_compaction():
    history = [_turn(index, "i1:edit") for index in range(1, 10)]
    view = build_memory_view(
        instance_id="i1:edit",
        contract=_contract(),
        history=history,
        recent_k=2,
        compressed_k=2,
    )

    assert len(view.recent_steps) == 2
    assert len(view.compressed_history) == 2
    assert len(view.durable_facts) == 9
    assert durable_kinds_present(view) == {"action_receipt"}
    assert available_event_refs(view) == {f"turn:{index}" for index in range(1, 10)}


def test_failed_dispatch_is_retained_as_a_fact_not_a_runtime_phase():
    view = build_memory_view(
        instance_id="i1:edit",
        contract=_contract(),
        history=[_turn(1, "i1:edit", dispatched=False)],
    )
    assert durable_kinds_present(view) == {"dispatch_failure"}


def test_recent_history_excludes_prior_model_assessments():
    base = _turn(1, "i1:edit")
    turn = base.model_copy(update={
        "supervisor": base.supervisor.model_copy(
            update={"summary": "unsupported model belief"},
        ),
        "transition": {
            "proposal": {
                "assessment": {"status": "in_progress"},
                "kind": "act",
            },
            "validation_error": "",
        },
    })
    view = build_memory_view(
        instance_id="i1:edit",
        contract=_contract(),
        history=[turn],
    )

    text = view.recent_steps[0].text
    assert "unsupported model belief" not in text
    assert "模型状态" not in text
    assert "模型决定" not in text
    assert "指令[write]" in text
    assert "signal exec=dispatched" in text


def test_pending_result_is_derived_from_last_action_expected_result():
    intent = ActionIntent(
        instruction="press Home to open Messages and read the SMS code",
        role="write",
        family="input",
        target_control="Home",
        expected_result="exit the app, return to the home screen, then open Messages",
    )
    turn = PolicyTurn(
        index=1,
        observation_source="android",
        statement_instance_id="i1:edit",
        supervisor=SupervisorStep(
            statement_id="edit", summary="step 1", action_intent=intent,
        ),
        executed=True,
        action_signal=ActionSignal(
            role="write", execution="dispatched", response="observed",
            target_control="Home",
        ),
    )
    view = build_memory_view(
        instance_id="i1:edit", contract=_contract(), history=[turn],
    )
    assert view.pending_result == "exit the app, return to the home screen, then open Messages"
    # Rendered above the contract so the decision continues the in-flight sub-goal.
    text = view.render_prompt_section()
    assert text.index("当前进行中的子目标") < text.index("### 合同")
    assert "exit the app" in text


def _sms_turn(
    index: int,
    *,
    summary: str,
    gap: str | None = None,
    executed: bool = False,
    code: str | None = None,
) -> PolicyTurn:
    transition = None
    if gap is not None:
        transition = {
            "proposal": {
                "assessment": {"status": "in_progress", "open_gaps": [gap]},
                "kind": "act",
            },
            "validation_error": "",
        }
    return PolicyTurn(
        index=index,
        observation_source="android",
        statement_instance_id="i1:edit",
        supervisor=SupervisorStep(
            statement_id="edit",
            summary=summary,
            action_intent=(
                ActionIntent(
                    instruction=f"step {index}", role="write", family="input",
                )
                if executed
                else None
            ),
        ),
        executed=executed,
        transition=transition,
        read_code=code or "",
    )


def test_external_read_fact_extracted_from_read_code_field():
    """An SMS code seen in an external app becomes a durable fact, not narrative.

    The value is extracted from the observation semantic tree at turn-record time
    (turn.read_code), a perception-layer fact — not from the LLM summary.
    """
    view = build_memory_view(
        instance_id="i1:edit",
        contract=_contract(),
        history=[_sms_turn(
            7,
            summary="已看到包含验证码的最新短信，需读取验证码后返回淘店完成登录",
            code="749500",
        )],
    )
    reads = [fact for fact in view.durable_facts if fact.kind == "external_read"]
    assert len(reads) == 1
    assert reads[0].metadata["code"] == "749500"
    assert "749500" in reads[0].text


def test_external_read_falls_back_to_supervisor_summary():
    """When the semantic tree carried no code, the supervisor summary (zero extra
    cost) still anchors the fill step — the two sources are complementary."""
    view = build_memory_view(
        instance_id="i1:edit",
        contract=_contract(),
        history=[_sms_turn(
            7,
            summary="验证码已发送至手机，短信内容：您的验证码是749500，请勿泄露",
        )],
    )
    reads = [fact for fact in view.durable_facts if fact.kind == "external_read"]
    assert len(reads) == 1
    assert reads[0].metadata["code"] == "749500"


def test_pending_gap_advances_when_external_read_exists():
    """The read step converges to a fill step once the code is a durable fact.

    Regression for the CartManagement SMS login loop: the agent read 749500 in
    Messages but, back at the login form with an empty code field, kept re-reading
    because the assessment re-framed the gap as "尚未读取验证码". With external_read
    holding the code, the pending gap must advance to filling it instead.
    """
    history = [
        _sms_turn(5, summary="验证码短信已到达且内容可见，被弹窗阻塞需先关闭"),
        _sms_turn(6, summary="按下 Home 离开淘店应用"),
        _sms_turn(7, summary="已看到包含验证码的最新短信", executed=True, code="749500"),
        _sms_turn(8, summary="主屏幕，重新打开淘店应用"),
        _sms_turn(9, summary="淘店登录页，验证码输入框可见但为空", gap="尚未读取验证码"),
    ]
    view = build_memory_view(
        instance_id="i1:edit", contract=_contract(), history=history,
    )
    assert view.pending_gap == "将已读取的验证码 749500 填入登录表单并提交"


def test_external_read_rendered_in_prompt():
    view = build_memory_view(
        instance_id="i1:edit",
        contract=_contract(),
        history=[_sms_turn(
            7,
            summary="已看到包含验证码的最新短信",
            code="749500",
        )],
    )
    text = view.render_prompt_section()
    assert "external_read" in text
    assert "749500" in text

