from gui_agent.core.orchestrator import (
    Acquire,
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


def test_validator_keeps_collection_ownership_on_acquire():
    interact = Program(statements=[
        Interact(
            id="collect", bind="rows", goal="collect", success="collected",
            returns={"rows": OutputSpec(type="list[record]", coverage="complete")},
        )
    ])
    invalid_acquire = Program(statements=[
        Acquire(
            id="collect", bind="rows", goal="collect",
            returns={"rows": OutputSpec(type="list[record]")},
        )
    ])
    assert "INTERACT_COLLECTION_OUTPUT" in _codes(interact)
    assert "ACQUIRE_COVERAGE_REQUIRED" in _codes(invalid_acquire)


def test_validator_rejects_record_fields_on_raw_acquire_output():
    program = Program(statements=[
        Data(
            id="inspect", bind="schema", goal="inspect identity",
            mode="inspect", required_fields=["identity"],
            returns={
                "available": OutputSpec(type="boolean"),
                "bindings": OutputSpec(type="record"),
                "missing_fields": OutputSpec(type="json"),
            },
        ),
        Acquire(
            id="collect", bind="rows", goal="collect raw rows",
            source_check=ValueRef(var="schema", path=["available"]),
            returns={
                "rows": OutputSpec(
                    type="list[record]", coverage="complete", fields=["identity"],
                ),
            },
        ),
    ])

    assert "ACQUIRE_RAW_FIELDS_FORBIDDEN" in _codes(program)


def test_validator_accepts_inspect_if_acquire_contract():
    program = Program(statements=[
        Data(
            id="inspect", bind="schema", goal="inspect required fields",
            mode="inspect", required_fields=["identity", "amount"],
            returns={
                "available": OutputSpec(type="boolean"),
                "bindings": OutputSpec(type="record"),
                "missing_fields": OutputSpec(type="json"),
            },
        ),
        Acquire(
            id="collect", bind="rows", goal="collect scoped rows",
            source_check=ValueRef(var="schema", path=["available"]),
            returns={
                "rows": OutputSpec(type="list[record]", coverage="complete"),
            },
        ),
    ])
    assert validate_program(program) == []


def test_validator_routes_missing_inspect_fields_through_repair_issue():
    program = Program(statements=[
        Data(
            id="inspect", bind="schema", goal="inspect source schema",
            mode="inspect",
            returns={
                "available": OutputSpec(type="boolean"),
                "bindings": OutputSpec(type="record"),
                "missing_fields": OutputSpec(type="json"),
            },
        ),
    ])

    assert "DATA_INSPECT_FIELDS_REQUIRED" in _codes(program)


def test_validator_requires_acquire_to_use_data_inspect_result():
    program = Program(statements=[
        Acquire(
            id="collect", bind="rows", goal="collect scoped rows",
            returns={
                "rows": OutputSpec(type="list[record]", coverage="complete"),
            },
        ),
    ])

    assert "ACQUIRE_SOURCE_CHECK_REQUIRED" in _codes(program)


def test_validator_rejects_inspection_shaped_output_from_derive_data():
    program = Program(statements=[
        Data(
            id="fake", bind="schema", goal="derive a similarly shaped record",
            returns={
                "available": OutputSpec(type="boolean"),
                "bindings": OutputSpec(type="record"),
                "missing_fields": OutputSpec(type="json"),
            },
        ),
        Acquire(
            id="collect", bind="rows", goal="collect scoped rows",
            source_check=ValueRef(var="schema", path=["available"]),
            returns={
                "rows": OutputSpec(type="list[record]", coverage="complete"),
            },
        ),
    ])

    assert "ACQUIRE_SOURCE_CHECK_INVALID" in _codes(program)


def test_validator_requires_data_record_output_fields():
    missing = Program(statements=[
        Data(
            id="rank", bind="result", goal="return ranked identities",
            returns={"rows": OutputSpec(type="list[record]")},
        ),
    ])
    declared = Program(statements=[
        Data(
            id="rank", bind="result", goal="return ranked identities",
            returns={
                "rows": OutputSpec(type="list[record]", fields=["identity"]),
            },
        ),
    ])

    assert "DATA_RECORD_FIELDS_REQUIRED" in _codes(missing)
    assert validate_program(declared) == []


def _acquire_then_data(*, inspected_fields, required_fields):
    return Program(statements=[
        Data(
            id="inspect", bind="schema", goal="inspect semantic source fields",
            mode="inspect", required_fields=inspected_fields,
            returns={
                "available": OutputSpec(type="boolean"),
                "bindings": OutputSpec(type="record"),
                "missing_fields": OutputSpec(type="json"),
            },
        ),
        Acquire(
            id="collect", bind="collection", goal="collect scoped records",
            source_check=ValueRef(var="schema", path=["available"]),
            returns={
                "rows": OutputSpec(type="list[record]", coverage="complete"),
            },
        ),
        Data(
            id="rank", bind="result", goal="rank records by identity frequency",
            inputs={"rows": ValueRef(var="collection", path=["rows"])},
            required_fields=required_fields,
            returns={"count": OutputSpec(type="number")},
        ),
    ])


def test_validator_propagates_data_fields_through_acquire_check():
    missing_declaration = _acquire_then_data(
        inspected_fields=["customer email"], required_fields=[],
    )
    mismatched = _acquire_then_data(
        inspected_fields=["customer name"], required_fields=["customer email"],
    )
    aligned = _acquire_then_data(
        inspected_fields=["customer email", "status"],
        required_fields=["customer email"],
    )

    assert "DATA_REQUIRED_FIELDS_REQUIRED" in _codes(missing_declaration)
    assert "DATA_REQUIRED_FIELDS_NOT_ACQUIRED" in _codes(mismatched)
    assert validate_program(aligned) == []


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
