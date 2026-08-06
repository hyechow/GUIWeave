"""Lock the deterministic read-focus short-circuit.

A ``ctx.read(target, ...)`` navigation statement must complete in one shot when the
target is already a visible source record, so the read binds from the current frame
instead of letting the supervisor LLM wander (e.g. into a Calendar form).  The target
string appearing only as a form-control value must NOT short-circuit: a pre-filled
Title box echoing a phone number is not the target's source record.
"""

from __future__ import annotations

from gui_agent.core.schemas import Observation, StatementContract
from gui_agent.core.supervisor.statement.policy import _read_focus_target_in_view


def _focus(target: dict) -> StatementContract:
    return StatementContract(
        id="c1",
        goal=f"Open target {target!r}'s detail view",
        success=f"Target {target!r}'s detail is in view",
        inputs={"target": target},
        persistence="immediate",
    )


def _obs_with_cells(cell_texts: list[list[str]]) -> Observation:
    return Observation.model_validate({
        "png_bytes": b"",
        "source": "android",
        "collection_regions": [
            {
                "ref": "region",
                "surface_fingerprint": "fp",
                "cells": [
                    {"ref": f"c{i}", "structural_key": f"s{i}",
                     "content_key": f"k{i}", "texts": texts}
                    for i, texts in enumerate(cell_texts)
                ],
            }
        ],
    })


def _obs_with_form_value(target_value: str) -> Observation:
    """Target echoed as a form field value, with no data cells."""
    return Observation.model_validate({
        "png_bytes": b"",
        "source": "android",
        "form_controls": [
            {"kind": "input", "label": "Title", "value": target_value},
        ],
        "semantic_tree": [
            {"role": "textbox", "key": target_value, "ref": "t1"},
        ],
    })


TARGET = {"id": "(505) 123-4567"}


def test_short_circuits_when_target_is_a_visible_source_record() -> None:
    obs = _obs_with_cells([
        ["(505) 123-4567 said Hi! Would you like to join me for lunch"],
        ["12:00 PM", "Texting with (505) 123-4567 (SMS/MMS)"],
    ])
    assert _read_focus_target_in_view(_focus(TARGET), obs)


def test_does_not_short_circuit_when_target_is_only_a_form_value() -> None:
    # Calendar New Event form: Title pre-filled with the phone number.  No source
    # record cell -> the read must NOT be declared satisfied here.
    obs = _obs_with_form_value("(505) 123-4567")
    assert not _read_focus_target_in_view(_focus(TARGET), obs)


def test_no_cells_no_short_circuit() -> None:
    obs = _obs_with_cells([])
    assert not _read_focus_target_in_view(_focus(TARGET), obs)


def test_unrelated_cells_no_short_circuit() -> None:
    obs = _obs_with_cells([["Some other conversation"], ["SPOTIFY", "GROCERY"]])
    assert not _read_focus_target_in_view(_focus(TARGET), obs)


def test_partial_target_match_does_not_short_circuit() -> None:
    # Target id must appear as a whole; a fragment is not the record.
    obs = _obs_with_cells([["call 123-4567", "not the full target"]])
    assert not _read_focus_target_in_view(_focus({"id": "(505) 123-4567"}), obs)


def test_non_focus_statements_never_short_circuit() -> None:
    base = _focus(TARGET)
    # explicit_commit (a commit) must not be short-circuited by the focus rule.
    commit = StatementContract(
        id="c1",
        goal="commit",
        success="done",
        inputs={"target": TARGET},
        persistence="explicit_commit",
    )
    obs = _obs_with_cells([["(505) 123-4567", "body text"]])
    assert not _read_focus_target_in_view(commit, obs)
    # a reach carries a collection intent -> not a focus.
    reach = StatementContract(
        id="c1",
        goal="reach",
        success="done",
        inputs={"target": TARGET},
        expected_state={"entity": "Message"},
    )
    assert not _read_focus_target_in_view(reach, obs)
    assert base.id == "c1"
