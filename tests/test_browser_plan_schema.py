import pytest
from pydantic import ValidationError

from gui_agent.adapters.browser.supervisor.milestone.prompts import BrowserPlanResult
from gui_agent.core.supervisor.milestone.schemas import _PlanResult


def test_browser_plan_rejects_action_family_used_as_atomic_role() -> None:
    with pytest.raises(ValidationError):
        BrowserPlanResult.model_validate({
            "instruction": "打开目标记录",
            "summary": "navigate",
            "atomic_role": "navigate",
            "action_family": "navigate",
        })


def test_core_plan_rejects_action_family_used_as_atomic_role() -> None:
    with pytest.raises(ValidationError):
        _PlanResult.model_validate({
            "instruction": "输入目标值",
            "summary": "write",
            "atomic_role": "input",
            "action_family": "input",
        })
