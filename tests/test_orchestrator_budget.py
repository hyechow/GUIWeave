from gui_agent.core.orchestrator import (
    Command,
    Data,
    ForEach,
    If,
    Interact,
    Program,
    ValueRef,
    estimate_program_turns,
)
from gui_agent.core.orchestrator.program import Condition


def test_budget_counts_executor_costs_without_ui_phase_prediction():
    program = Program(
        statements=[
            Data(id="data", goal="derive data"),
            Command(id="back", capability="back"),
            Interact(id="edit", goal="edit", success="edited"),
        ]
    )
    assert estimate_program_turns(program) == 6


def test_budget_uses_larger_if_branch_and_representative_foreach_iterations():
    program = Program(
        statements=[
            If(
                cond=Condition(ref=ValueRef(var="x"), cmp="exists"),
                then=[Interact(id="one", goal="one", success="done")],
                otherwise=[
                    Interact(id="two", goal="two", success="done"),
                    Interact(id="three", goal="three", success="done"),
                ],
            ),
            ForEach(
                items=ValueRef(var="rows"),
                body=[Interact(id="each", goal="each", success="done")],
            ),
        ]
    )
    assert estimate_program_turns(program) == 16
    assert estimate_program_turns(program, floor=40) == 40
