"""Feasibility Guard: runtime milestone-feasibility judgment (the goal-level stuck diagnosis).

When the agent is STUCK on a milestone, this asks a different question from the action-level
replanner: is the milestone's GOAL even achievable here — does the required UI control exist? —
or is it just that the action/query was wrong? If the required control is observed ABSENT, the
milestone is infeasible and the run should kick back to the orchestrator with a directive rather
than retry forever / give up.

Reliability recipe (validated offline on task-113, robust to wrong knowledge — see
scripts/milestone_feasibility_113.py and the stuck-diagnosis-two-mechanisms memory):
  - judge from the page's ACTUAL control inventory (DOM form_controls) — clean ground truth, not
    prose, not result counts;
  - DIRECT OBSERVATION (the control list) overrides the (fallible) knowledge section on conflict;
  - DEFAULT feasible — declare infeasible ONLY when the required control is observed absent, and
    never rationalize away a control that IS in the inventory. This conservative-toward-feasible
    bias is deliberate: it must not steal the action-level replanner's "feasible-but-stuck" cases.

This module is the pure judgment (prompt + LLM call); the supervisor hook and the kick-back/
re-decompose plumbing are wired in later stages."""

from __future__ import annotations

from typing import Any, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from gui_agent.context.runtime import format_form_controls_text
from gui_agent.core.config import resolve_llm_config
from gui_agent.prompts import load_prompt_text
from llm.structured import invoke_structured

_SYSTEM = load_prompt_text("task.milestone.feasibility")


class FeasibilityVerdict(BaseModel):
    """Feasibility Guard output: is the stuck milestone feasible, and (if not) the kick-back directive."""

    feasible: bool = Field(description="目标在当前环境可否达成:true=可行(继续试),false=不可行(踢回重规划)")
    reason: str = Field(default="", description="一句话依据:点名必需控件 + 它是否在页面控件清单中")
    directive: str = Field(
        default="",
        description="feasible=false 时填:给编排器的重规划指令(禁死路+规定唯一可行路线);feasible=true 时留空",
    )


def _ui_facts_text(ui_facts: Optional[list[dict[str, Any]]]) -> str:
    """Compact grid facts (active filters / record count / empty) — the goal-relevant state."""
    if not ui_facts:
        return ""
    lines: list[str] = []
    for fact in ui_facts[:12]:
        if not isinstance(fact, dict):
            continue
        bits = []
        for key in ("kind", "active_filters", "record_count", "empty", "sort", "message"):
            val = fact.get(key)
            if val not in (None, "", [], {}):
                bits.append(f"{key}={val}")
        if bits:
            lines.append("- " + "; ".join(bits))
    return "## 页面 UI 状态事实\n" + "\n".join(lines) if lines else ""


def control_presence_text(observation: Any) -> str:
    """The page's actual control inventory (DOM form_controls + grid facts) — the direct
    observation the Feasibility Guard judges against. Empty/visual-only platforms yield a clear sentinel."""
    parts = [
        format_form_controls_text(getattr(observation, "form_controls", None)),
        _ui_facts_text(getattr(observation, "ui_facts", None)),
    ]
    body = "\n\n".join(p for p in parts if p)
    return body or "(当前页面无适配器可感知的表单控件/UI 事实)"


def _llm() -> ChatOpenAI:
    # Feasibility reads the page screenshot (nav menus / visible structure), so its model MUST be
    # vision-capable — configured under supervisor.feasibility in config.yaml (default qwen3.5-35b-a3b,
    # the same vision model the checker uses). Do NOT point this at a text-only model: it would
    # silently drop the image and judge blind (the old unset key fell through to a text-only default).
    cfg = resolve_llm_config("supervisor.feasibility")
    if not cfg.model:
        cfg = resolve_llm_config("supervisor")
    return ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url)


def judge_feasibility(
    milestone_goal: str,
    control_text: str,
    knowledge: str = "",
    *,
    image: Optional[bytes] = None,
    llm: Optional[ChatOpenAI] = None,
    trace_sink: Optional[list[dict]] = None,
) -> FeasibilityVerdict:
    """Judge whether the stuck milestone is executable on the current page.

    ``control_text`` = control_presence_text(observation) — the AUTHORITATIVE source for control
    presence (filter/search/input). ``image`` = the page screenshot — supplements the DOM inventory
    for what it can't see (navigation menus, visible page structure); the prompt keeps the DOM
    authoritative for control presence so pixels can't hallucinate a filter that isn't there.
    ``knowledge`` = the relevant app section (a fallible prior, overridden by the inventory on
    conflict). Returns the verdict +, when infeasible, a kick-back directive."""
    human = (
        f"卡住的 milestone 目标(success_condition):\n{milestone_goal}\n\n"
        f"当前页面的实际控件清单(DOM 感知 = 控件存在性的权威依据):\n{control_text}\n"
    )
    if knowledge.strip():
        human += f"\n相关功能知识章节(可能过时/写错的文档,仅作先验,与控件清单冲突时以清单为准):\n{knowledge}\n"
    if image is not None:
        human += ("\n另附当前页面截图:仅用于看【导航入口与可见页面结构】(侧栏菜单展开了哪些子项、目标入口/链接在不在);"
                  "控件存在性仍以上面的 DOM 控件清单为准,不要从截图脑补清单里没有的筛选/搜索控件。\n")
    human += "\n请按规则判定;不可行时给出斩钉截铁、不 hedge 的重规划 directive。"
    if image is not None:
        import base64
        b64 = base64.b64encode(image).decode()
        human_msg = HumanMessage(content=[
            {"type": "text", "text": human},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ])
    else:
        human_msg = HumanMessage(content=human)
    msgs = [SystemMessage(content=_SYSTEM), human_msg]
    return invoke_structured(
        llm or _llm(), msgs, FeasibilityVerdict,
        trace_sink=trace_sink, trace_label="feasibility",
    )
