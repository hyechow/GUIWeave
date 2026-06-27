from __future__ import annotations

from gui_agent.core.schemas import BaseAction, BaseActionDecision
from gui_agent.core.vision.visualize import print_decision


def test_print_decision_tolerates_partial_coordinates(tmp_path, capsys):
    decision = BaseActionDecision(
        action=BaseAction(
            action_type="type",
            x=495,
            y=None,
            text="nature",
            description="输入 nature",
        )
    )

    print_decision(decision, b"", output_path=tmp_path / "unused.png")

    output = capsys.readouterr().out
    assert "[type] 输入 nature" in output
    assert "(495" not in output
