from gui_agent.core.orchestrator import (
    Command,
    Data,
    Finish,
    ForEach,
    Interact,
    OutputSpec,
    Program,
    ValueRef,
    validate_program,
)
from gui_agent.core.orchestrator._validator.issue import ALL_CODES


def _codes(program: Program) -> set[str]:
    return {issue.code for issue in validate_program(program)}


def test_validator_is_structural_and_accepts_semantic_ui_goals():
    program = Program(
        statements=[
            Interact(
                id="edit",
                goal="make the requested values present for the target entity",
                success="all requested values are present and persisted",
                required_values={"values": ["30", "31"]},
                persistence="explicit_commit",
            )
        ]
    )
    assert validate_program(program) == []


def test_validator_checks_typed_reference_and_bind_flow():
    program = Program(
        statements=[
            Data(
                id="select",
                bind="selection",
                goal="select records",
                returns={"rows": OutputSpec(type="list[record]")},
            ),
            Interact(
                id="edit",
                goal="edit record",
                success="record is updated",
                inputs={"record": ValueRef(var="selection", path=["missing"])},
            ),
        ]
    )
    assert "REF_FIELD_NOT_DECLARED" in _codes(program)


def test_validator_checks_foreach_collection_type_and_collect_scope():
    program = Program(
        statements=[
            Data(
                id="count",
                bind="answer",
                goal="derive count",
                returns={"count": OutputSpec(type="number")},
            ),
            ForEach(
                items=ValueRef(var="answer", path=["count"]),
                body=[Interact(id="one", goal="do one", success="done")],
                collect=ValueRef(var="missing"),
                into="results",
            ),
        ]
    )
    codes = _codes(program)
    assert "FOREACH_ITEMS_NOT_LIST" in codes
    assert "FOREACH_COLLECT_NOT_IN_SCOPE" in codes


def test_validator_checks_command_capability_contract():
    missing = Program(statements=[Command(id="open", capability="open_url")])
    unsupported = Program(
        statements=[
            Command(
                id="open",
                capability="open_url",
                args={"url": "https://example.test"},
                bind="page",
                returns={"rows": OutputSpec(type="list[record]")},
            )
        ]
    )
    assert "COMMAND_MISSING_ARGUMENT" in _codes(missing)
    assert "COMMAND_OUTPUT_UNSUPPORTED" in _codes(unsupported)


def test_all_emitted_codes_are_registered():
    programs = [
        Program(),
        Program(statements=[Interact(id="", goal="", success="")]),
        Program(statements=[Command(id="open", capability="open_url")]),
        Program(
            statements=[
                Interact(
                    id="read",
                    bind="title",
                    goal="read title",
                    success="title read",
                    returns={"title": OutputSpec(type="text", coverage="complete")},
                ),
            ]
        ),
        # Trigger sample for FINISH_NUMERIC_FROM_DATA (Interact number → Finish).
        Program(
            statements=[
                Interact(
                    id="filter",
                    bind="counts",
                    goal="filter and count",
                    success="filtered",
                    returns={"total": OutputSpec(type="number")},
                ),
                Finish(outputs={"result": ValueRef(var="counts", path=["total"])}),
            ]
        ),
    ]
    emitted = {issue.code for program in programs for issue in validate_program(program)}
    assert emitted <= ALL_CODES
    assert "FINISH_NUMERIC_FROM_DATA" in emitted
