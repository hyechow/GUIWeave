from pydantic import ValidationError
import pytest

from gui_agent.core.orchestrator import (
    Command,
    Condition,
    Data,
    Finish,
    ForEach,
    If,
    Interact,
    OutputSpec,
    Program,
    ProgramRunner,
    ValueRef,
)
from gui_agent.core.schemas import StatementOutcome


def test_program_runner_owns_typed_branch_and_foreach_control_flow():
    program = Program(
        goal="update each selected record",
        statements=[
            Data(
                id="select",
                bind="selection",
                goal="select the records that need an update",
                returns={"rows": OutputSpec(type="list[record]")},
            ),
            If(
                cond=Condition(
                    ref=ValueRef(var="selection", path=["rows"]),
                    cmp="empty",
                ),
                then=[Finish(message="nothing to do")],
                otherwise=[
                    ForEach(
                        items=ValueRef(var="selection", path=["rows"]),
                        item="row",
                        index="position",
                        body=[
                            Interact(
                                id="update",
                                bind="updated",
                                goal="update the current record",
                                success="the current record reflects the requested value",
                                inputs={
                                    "record": ValueRef(var="row"),
                                    "position": ValueRef(var="position"),
                                },
                                returns={"ok": OutputSpec(type="boolean")},
                            )
                        ],
                        collect=ValueRef(var="updated", path=["ok"]),
                        into="results",
                    ),
                    Finish(
                        message="updated {results}",
                        outputs={"results": ValueRef(var="results")},
                    ),
                ],
            ),
        ],
    )
    calls = []

    def execute(invocation):
        calls.append(invocation)
        if invocation.executor == "data":
            return StatementOutcome.completed(
                "selected",
                outputs={"rows": [{"id": "a"}, {"id": "b"}]},
            )
        assert invocation.inputs["record"] in ({"id": "a"}, {"id": "b"})
        return StatementOutcome.completed("updated", outputs={"ok": True})

    result = ProgramRunner(execute).run(program)

    assert result.failed is False
    assert result.reply == "updated [True, True]"
    assert result.env["results"] == [True, True]
    assert [call.loop_path for call in calls] == [[], [0], [1]]
    assert [call.inputs.get("position") for call in calls[1:]] == [0, 1]


def test_command_arguments_use_explicit_literal_and_reference_channels():
    program = Program(
        statements=[
            Data(
                id="route",
                bind="route",
                goal="resolve destination",
                returns={"url": OutputSpec(type="url")},
            ),
            Command(
                id="open",
                capability="open_url",
                arg_refs={"url": ValueRef(var="route", path=["url"])},
            ),
        ]
    )

    def execute(invocation):
        if invocation.executor == "data":
            return StatementOutcome.completed(
                "resolved", outputs={"url": "https://example.test/records"}
            )
        assert invocation.args == {"url": "https://example.test/records"}
        return StatementOutcome.completed("opened")

    assert ProgramRunner(execute).run(program).failed is False


def test_output_contract_rejects_missing_wrong_and_extra_values():
    statement = Data(
        id="data",
        bind="answer",
        goal="derive answer",
        returns={"count": OutputSpec(type="number")},
    )
    for outputs in ({}, {"count": "one"}, {"count": 1, "extra": True}):
        result = ProgramRunner(
            lambda _invocation, outputs=outputs: StatementOutcome.completed(
                "done", outputs=outputs
            )
        ).run(Program(statements=[statement]))
        assert result.failed is True
        assert "输出合同不满足" in result.reply


def test_failed_statement_stops_before_following_statement():
    calls = []
    program = Program(
        statements=[
            Interact(id="first", goal="first", success="first is done"),
            Interact(id="second", goal="second", success="second is done"),
        ]
    )

    def execute(invocation):
        calls.append(invocation.id)
        return StatementOutcome.failed("cannot complete")

    result = ProgramRunner(execute).run(program)
    assert result.failed is True
    assert calls == ["first"]


def test_direct_cutover_rejects_old_dsl_and_function_shapes():
    with pytest.raises(ValidationError):
        Program.model_validate(
            {
                "goal": "legacy",
                "statements": [
                    {"op": "run", "name": "old run", "description": "legacy"}
                ],
            }
        )
    with pytest.raises(ValidationError):
        Program.model_validate(
            {
                "goal": "legacy",
                "functions": [{"name": "helper", "body": []}],
                "statements": [],
            }
        )


def test_foreach_requires_a_fixed_body_and_explicit_collect_pair():
    with pytest.raises(ValidationError):
        ForEach.model_validate(
            {
                "items": {"var": "rows"},
                "body_goal": "sub-decompose each row",
                "body": [],
            }
        )
    with pytest.raises(ValidationError):
        ForEach(items=ValueRef(var="rows"), body=[], into="results")
