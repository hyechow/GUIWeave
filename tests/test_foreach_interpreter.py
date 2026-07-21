import pytest

from gui_agent.core.orchestrator import (
    Interpreter,
    Read,
    Finish,
    ForEach,
    Interact,
    OutputSpec,
    Program,
    ProgramRunner,
    ValueRef,
)
from gui_agent.core.schemas import StatementOutcome


def _program() -> Program:
    return Program(
        statements=[
            Read(
                id="members",
                bind="members",
                returns={"rows": OutputSpec(type="list[record]")},
            ),
            ForEach(
                items=ValueRef(var="members", path=["rows"]),
                item="member",
                body=[
                    Interact(
                        id="apply",
                        goal="apply the change to this member",
                        success="this member reflects the requested value",
                        inputs={"member": ValueRef(var="member")},
                    ),
                    Read(
                        id="read_result",
                        bind="result",
                        inputs={"member": ValueRef(var="member")},
                        returns={"id": OutputSpec(type="text")},
                    )
                ],
                collect=ValueRef(var="result", path=["id"]),
                into="changed_ids",
            ),
            Finish(
                message="done",
                outputs={"changed_ids": ValueRef(var="changed_ids")},
            ),
        ]
    )


def test_foreach_iterates_a_materialized_list_without_runtime_expansion():
    seen = []

    def execute(invocation):
        if invocation.id == "members":
            return StatementOutcome.completed(
                "members", outputs={"rows": [{"id": "a"}, {"id": "b"}]}
            )
        if invocation.executor == "interact":
            seen.append((invocation.inputs["member"], invocation.loop_path))
            return StatementOutcome.completed("changed")
        return StatementOutcome.completed("read", outputs={
            "id": invocation.inputs["member"]["id"]
        })

    result = ProgramRunner(execute).run(_program())

    assert seen == [({"id": "a"}, [0]), ({"id": "b"}, [1])]
    assert result.env["changed_ids"] == ["a", "b"]
    assert [record.node_id for record in result.run_log] == [
        "members", "apply", "read_result", "apply", "read_result"
    ]
    assert result.run_log[1].instance_id == ""


def test_foreach_empty_collection_skips_body_and_binds_empty_result():
    result = ProgramRunner(
        lambda _invocation: StatementOutcome.completed(
            "members", outputs={"rows": []}
        )
    ).run(_program())

    assert result.failed is False
    assert result.env["changed_ids"] == []
    assert [record.node_id for record in result.run_log] == ["members"]


def test_foreach_fails_when_runtime_value_is_not_a_list():
    program = Program(
        statements=[
            Read(
                id="members",
                bind="members",
                returns={"value": OutputSpec(type="json")},
            ),
            ForEach(
                items=ValueRef(var="members", path=["value"]),
                body=[Interact(id="one", goal="do one", success="done")],
            ),
        ]
    )
    result = ProgramRunner(
        lambda _invocation: StatementOutcome.completed(
            "value", outputs={"value": {"id": "not-a-list"}}
        )
    ).run(program)

    assert result.failed is True
    assert "foreach 只接受 list" in result.reply


@pytest.mark.parametrize("verification", ["confirmed", "accepted_unverified"])
def test_foreach_collect_preserves_value_verification(verification):
    interpreter = Interpreter(_program())

    def execute(invocation):
        if invocation.id == "members":
            return StatementOutcome.completed("members", outputs={"rows": [{"id": "a"}]})
        if invocation.id == "apply":
            return StatementOutcome.completed("changed")
        return StatementOutcome.completed(
            "read", verification=verification, outputs={"id": "a"}
        )

    assert interpreter.drive(execute).failed is False
    assert interpreter.terminal_verification == verification
