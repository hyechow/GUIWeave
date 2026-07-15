"""The replanner MUST receive the DOM form-control inventory (each input's authoritative
`current=` value). Without it, it diagnoses a narrow scrolled input as truncated purely from
the screenshot and loops re-typing the same value.

Regression 20260622_163905: 'Olivia zip jacket' typed into a 71px Product filter box shows
only '…zip jacket' in the screenshot; the replanner got NO DOM block at all and kept ordering
"删除现有内容并重新输入" for 4 turns. The fix wires form_controls_block into _invoke_replanner's
human_blocks. This locks that wiring deterministically (the LLM is bypassed)."""

import gui_agent.core.supervisor.milestone.llm_runtime as pol
from gui_agent.core.schemas import StatementContract, Observation
from gui_agent.core.supervisor.milestone.policy import MilestoneSupervisorPolicy
from gui_agent.core.supervisor.milestone.schemas import _ReplanResult, _SingleCheckResult


def test_replanner_human_blocks_include_dom_form_controls(monkeypatch):
    captured: dict = {}

    def _fake_assemble(prompt, observation, *, system_blocks=None, human_blocks=None, **kw):
        captured["human_blocks"] = [b for b in (human_blocks or []) if b is not None]
        return [{"role": "user", "content": "x"}]

    monkeypatch.setattr(pol, "assemble_messages", _fake_assemble)
    monkeypatch.setattr(
        pol, "invoke_structured",
        lambda *a, **k: _ReplanResult(diagnosis="d", strategy="local_replan",
                                      instruction="点击 Search 按钮提交筛选"),
    )

    p = MilestoneSupervisorPolicy()
    monkeypatch.setattr(p, "_llm", lambda: object())  # invoke_structured is patched → never used

    milestone = StatementContract.model_validate({
        "id": "m3", "name": "在 Product 列筛选框输入 'Olivia zip jacket'", "description": "d",
        "success_condition": "Product 框内容为 'Olivia zip jacket'", "kind": "action",
    })
    p.begin_statement(milestone, instance_id="i1")
    check = _SingleCheckResult(status="in_progress", effect_status="unverified", reason="r", summary="s")
    obs = Observation(
        png_bytes=b"png", source="browser",
        form_controls=[{"label": "Product", "kind": "text_input",
                        "value": "Olivia zip jacket", "focused": True}],
    )

    p._invoke_replanner(milestone, check, obs, [])

    text = " ".join(getattr(b, "content", "") for b in captured["human_blocks"])
    assert "浏览器 DOM 表单控件" in text                 # the DOM control block reached the replanner
    assert 'current="Olivia zip jacket"' in text         # carrying the input's authoritative value


def test_replan_preserves_atomic_execution_contract(monkeypatch):
    policy = MilestoneSupervisorPolicy()
    milestone = StatementContract(
        id="open-products",
        name="enter products list",
        description="",
        success_condition="products list is visible",
        kind="navigation",
    )
    policy.begin_statement(milestone, instance_id="test:replan")
    monkeypatch.setattr(
        policy,
        "_invoke_replanner",
        lambda *_args, **_kwargs: _ReplanResult(
            diagnosis="the menu is open but the link was not activated",
            strategy="local_replan",
            instruction="点击展开菜单中的 Products 链接",
            atomic_role="prepare",
            action_family="navigate",
            target_control="Products",
        ),
    )

    step = policy._handle_stuck(
        milestone,
        _SingleCheckResult(
            status="stuck",
            effect_status="unverified",
            reason="wrong menu point",
            stuck_reason="wrong menu point",
            summary="",
        ),
        None,
        Observation(png_bytes=b"fixture", source="browser"),
        [],
    )

    assert step.instruction == "点击展开菜单中的 Products 链接"
    assert step.atomic_role == "prepare"
    assert step.action_family == "navigate"
    assert step.target_control == "Products"
