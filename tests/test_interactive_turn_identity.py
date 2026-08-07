from gui_agent.core.run.contracts import Interact, StatementInvocation
from gui_agent.core.run.interactive import contract_for_interact
from gui_agent.core.run.turns import emit_statement_fields
from gui_agent.core.supervisor.statement.policy import StatementSupervisorPolicy


def _contract(goal="open the current record"):
    invocation = StatementInvocation(
        statement=Interact(
            id="s1",
            goal=goal,
            success="the current record detail is visible",
        )
    )
    return contract_for_interact(invocation, 0)


def test_statement_info_is_emitted_once_per_invocation():
    policy = StatementSupervisorPolicy()
    policy.begin_statement(_contract(), instance_id="i9:s1")

    first_info, first_id = emit_statement_fields(policy)
    next_info, next_id = emit_statement_fields(policy)

    assert first_info is not None and first_info.id == "s1"
    assert next_info is None
    assert first_id == next_id == "i9:s1"


def test_new_invocation_emits_its_own_contract_once():
    policy = StatementSupervisorPolicy()
    policy.begin_statement(_contract(), instance_id="i9:s1")
    emit_statement_fields(policy)
    policy.end_statement()
    policy.begin_statement(_contract("open another record"), instance_id="i10:s1")
    info, instance_id = emit_statement_fields(policy)
    assert info is not None and info.goal == "open another record"
    assert instance_id == "i10:s1"


def test_extract_read_code_zh_en_alnum_and_negative():
    """The turn-recording extraction reads verification codes from the semantic
    tree — Chinese/English, digits and letters — and rejects lookalikes."""
    from gui_agent.core.run.turns import extract_read_code
    from gui_agent.core.schemas import Observation

    def obs(*keys):
        return Observation(
            png_bytes=b"",
            source="android",
            semantic_tree=[
                {"role": "text", "key": key, "ref": f"n{i}"}
                for i, key in enumerate(keys)
            ],
        )

    # Chinese, English, alphanumeric
    for key, want in [
        ("您的验证码是299603，有效期10分钟，请勿泄露给他人。", "299603"),
        ("验证码：888888", "888888"),
        ("验证码为A1B2C3", "A1B2C3"),
        ("Your verification code is 299603, valid for 10 minutes.", "299603"),
        ("Verification code: ABC123", "ABC123"),
        ("Your verification code is 4F7K", "4F7K"),
    ]:
        assert extract_read_code(obs(key)) == want, key

    # Lookalikes must NOT be extracted: phone-send line, empty-value labels, and
    # unrelated English text whose bare "code" + 4-8 char word is not a
    # verification code (e.g. "Code review meeting at 3 PM").
    none_keys = [
        "验证码已通过短信发送到您的手机13802138888",
        "请输入验证码",
        "获取验证码",
        "验证码",
        "Code review meeting at 3 PM. Please have your changes pushed by then.",
        "code 123456",  # bare "code", not "verification code"
    ]
    assert extract_read_code(obs(*none_keys)) == ""

    assert extract_read_code(None) == ""
