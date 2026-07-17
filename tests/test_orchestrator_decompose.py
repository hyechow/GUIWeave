from pydantic import ValidationError
import pytest

from gui_agent.core.orchestrator import Command, Data, ForEach, If, Interact, OutputSpec
from gui_agent.core.orchestrator._decomposer.draft import (
    _PlanDraft,
    _StepDraft,
    to_program,
)
from gui_agent.core.orchestrator.decomposer import decompose


def test_draft_converts_only_the_six_semantic_nodes():
    draft = _PlanDraft(
        goal="update selected records",
        steps=[
            _StepDraft(
                op="data",
                bind="selection",
                goal="select records that need a change",
                returns={"rows": OutputSpec(type="list[record]")},
            ),
            _StepDraft(
                op="foreach",
                items={"var": "selection", "path": ["rows"]},
                item="row",
                body=[
                    _StepDraft(
                        op="interact",
                        goal="apply the requested change to this record",
                        success="the record reflects the requested value",
                        inputs={"row": {"var": "row"}},
                    )
                ],
            ),
            _StepDraft(op="command", capability="back"),
            _StepDraft(
                op="if",
                cond_ref={"var": "selection", "path": ["rows"]},
                cond_cmp="empty",
                then=[_StepDraft(op="finish", message="nothing to do")],
                otherwise=[_StepDraft(op="finish", message="done")],
            ),
        ],
    )

    program = to_program(draft)

    assert isinstance(program.statements[0], Data)
    assert isinstance(program.statements[1], ForEach)
    assert isinstance(program.statements[1].body[0], Interact)
    assert isinstance(program.statements[2], Command)
    assert isinstance(program.statements[3], If)
    assert [
        program.statements[0].id,
        program.statements[1].body[0].id,
        program.statements[2].id,
    ] == ["s1", "s2", "s3"]


@pytest.mark.parametrize(
    "payload",
    [
        {"op": "run", "goal": "legacy"},
        {"op": "interact", "goal": "missing success"},
        {"op": "command"},
        {"op": "if"},
        {"op": "foreach", "items": {"var": "rows"}, "body": []},
    ],
)
def test_draft_rejects_legacy_or_incomplete_shapes(payload):
    with pytest.raises(ValidationError):
        _StepDraft.model_validate(payload)


def test_decompose_repairs_at_most_once(monkeypatch):
    from gui_agent.core.orchestrator import decomposer as module

    drafts = iter(
        [
            _PlanDraft(
                steps=[
                    _StepDraft(
                        op="command",
                        capability="open_url",
                    )
                ]
            ),
            _PlanDraft(
                steps=[
                    _StepDraft(
                        op="command",
                        capability="open_url",
                        args={"url": "https://example.test"},
                    )
                ]
            ),
        ]
    )
    calls = []
    monkeypatch.setattr(module, "ChatOpenAI", lambda **_kwargs: object())

    def invoke(*_args, **_kwargs):
        calls.append(1)
        return next(drafts)

    monkeypatch.setattr(module, "invoke_structured", invoke)
    program = decompose("open the known destination")

    assert len(calls) == 2
    assert isinstance(program.statements[0], Command)
    assert program.statements[0].args["url"] == "https://example.test"
