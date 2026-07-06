# -*- coding: utf-8 -*-
"""Progressive-orchestration checkpoint expansion: ONE refinement call at foreach entry.

Design (user direction 2026-07-02, feasibility validated in scripts/progressive_expand_experiment.py
on real 778 rows — member selection 10/10 vs 0-for-all for t=0 literal guessing):

The t=0 skeleton declares a deferred foreach (body_goal = semantic intent, e.g. 「判断 {row[sku]}
是否为 size 28 的变体；若是，读现价→算→更新→保存」). At the CHECKPOINT — the foreach entry, where
collect_fn has just materialized the REAL rows — this module makes one LLM call that sees all rows
and returns:

  • member_row_indices — the membership judgment AS DATA (which rows belong to the target set),
    made by looking at actual field values, not by guessing a predicate at t=0;
  • body — the concrete per-member statement list (shared by all members, {var[field]} templated),
    which then executes deterministically (zero further planning).

The expanded body goes through the SAME validate_program gate with one feedback retry (mirroring
decompose's loop). On any persistent failure the caller falls back to the existing per-row
subdecompose path, so expansion is strictly an upgrade, never a new failure mode.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .program import ForEach, Program, Stmt
from .validator import validate_program

# Above this many rows the selection prompt risks truncation-and-silent-member-drop; fall back to
# the per-row path rather than select over a partial view.
EXPAND_MAX_ROWS = 60

def _system_prompt() -> str:
    from gui_agent.prompts import load_prompt_text

    return load_prompt_text("task.orchestrator.foreach_expand")


class _ExpandDraft(BaseModel):
    member_row_indices: list[int] = Field(default_factory=list)
    body: list[dict] = Field(default_factory=list)
    reason: str = ""


class ForeachExpansion(BaseModel):
    """The checkpoint-expansion product the interpreter consumes."""

    member_indices: list[int]
    body: list[Stmt]
    note: str = ""


def _parse_and_validate(body_dicts: list[dict], loop_var: str, row_fields: list[str],
                        goal: str) -> tuple[Optional[list[Stmt]], list[str]]:
    """Parse the drafted body dicts through the Stmt union and run the standard validator against
    a ForEach wrapper (so {var[field]} row templates resolve). Returns (stmts, issues) — stmts is
    None when the draft isn't even structurally parseable."""
    def _normalize(steps: list) -> None:
        # Recursive: if-branch bodies carry the same draft quirks as top level (found via the 185
        # join case — nested steps kept read_spec=None/False/dict and failed pydantic).
        for st in steps:
            if not isinstance(st, dict):
                continue
            for k in ("read_spec", "success_condition", "sql", "expr", "name"):
                if k in st and not isinstance(st[k], str):
                    st[k] = "" if not isinstance(st[k], (int, float)) else str(st[k])
            # kind-as-op alias: the model naturally writes {"op":"navigation"/"read"/...} even
            # though the union tag is op="run" + kind — normalize instead of burning a retry.
            if st.get("op") in ("navigation", "filter", "action", "read", "data_query"):
                st["kind"] = st["op"]
                st["op"] = "run"
            for branch in ("then", "otherwise", "body"):
                if isinstance(st.get(branch), list):
                    _normalize(st[branch])

    _normalize(body_dicts)
    try:
        program = Program(goal=goal, statements=[
            ForEach(var=loop_var, target="检查点已圈选的成员行",
                    returns=list(row_fields), row_fields=list(row_fields),
                    body=body_dicts),  # pydantic parses dicts through the Stmt union
        ])
    except Exception as e:  # noqa: BLE001
        return None, [f"body 不是合法的步骤列表: {str(e)[:200]}"]
    fe = program.statements[0]
    assert isinstance(fe, ForEach)
    if not fe.body:
        return None, ["body 为空——必须给出对每个成员执行的具体步骤"]
    issues = [str(i) for i in validate_program(program)]
    return list(fe.body), issues


def _build_llm():
    from langchain_openai import ChatOpenAI

    from gui_agent.core.config import resolve_llm_config

    cfg = resolve_llm_config("supervisor.decompose")
    if not cfg.model:
        cfg = resolve_llm_config("supervisor")
    return ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url,
                      extra_body={"enable_thinking": False})


def _map_selection(raw: list[int], rows: list[dict]) -> list[int]:
    """Positions in range, deduped; id-VALUE answers ([1478, 1182]) mapped back to positions."""
    idx = [i for i in raw if 0 <= i < len(rows)]
    idx = list(dict.fromkeys(idx))
    if not idx and raw:
        wanted = {str(i) for i in raw}
        idx = [pos for pos, r in enumerate(rows)
               if any(str(r.get(k, "")) in wanted for k in ("id", "ID", "Id"))]
    return idx


class _SelectDraft(BaseModel):
    member_row_indices: list[int] = Field(default_factory=list)
    reason: str = ""


def select_members(
    member_desc: str,
    rows: list[dict],
    *,
    goal: str = "",
    llm=None,
    trace_sink: Optional[list] = None,
) -> Optional[list[int]]:
    """Selection-only checkpoint call: which of the REAL rows belong to the target set. The body is
    NOT authored here — it was written at t=0 under the mature decomposer prompt and full gates;
    this defers ONLY the membership decision (the part that needs runtime data; cross-family 16/16
    offline). None ⇒ caller keeps all rows / falls back."""
    if not rows or len(rows) > EXPAND_MAX_ROWS:
        return None
    import json

    from langchain_core.messages import HumanMessage, SystemMessage

    from gui_agent.prompts import load_prompt_text
    from llm.structured import invoke_structured

    if llm is None:
        llm = _build_llm()
    human = (
        f"总目标:{goal}\n\n目标集合(成员描述):{member_desc}\n\n"
        f"已采集的候选行(真实页面数据,行号即列表下标):\n{json.dumps(rows, ensure_ascii=False, indent=1)}"
    )
    try:
        draft = invoke_structured(
            llm,
            [SystemMessage(content=load_prompt_text("task.orchestrator.foreach_select")),
             HumanMessage(content=human)],
            _SelectDraft, trace_sink=trace_sink, trace_label="foreach.select")
    except Exception:  # noqa: BLE001 — selection is an upgrade; failure → keep all rows upstream
        return None
    return _map_selection(draft.member_row_indices, rows)


def expand_foreach(
    body_goal: str,
    loop_var: str,
    rows: list[dict],
    returns: list[str],
    *,
    goal: str = "",
    llm=None,
    max_retries: int = 1,
    trace_sink: Optional[list] = None,
) -> Optional[ForeachExpansion]:
    """One checkpoint-expansion call. None ⇒ caller falls back to per-row subdecompose."""
    if not rows or len(rows) > EXPAND_MAX_ROWS:
        return None
    import json

    from langchain_core.messages import HumanMessage, SystemMessage

    from llm.structured import invoke_structured

    if llm is None:
        llm = _build_llm()

    row_fields = sorted({k for r in rows for k in r})
    human = (
        f"总目标:{goal}\n\n子目标(对每个成员):{body_goal}\n\n循环变量名:{loop_var}\n"
        f"每行产出契约(returns):{returns}\n\n"
        f"已采集的候选行(真实页面数据,行号即列表下标):\n{json.dumps(rows, ensure_ascii=False, indent=1)}"
    )
    feedback = ""
    for attempt in range(max_retries + 1):
        msg = human + (f"\n\n上一版的问题(全部修正后重出完整 JSON):\n{feedback}" if feedback else "")
        try:
            draft = invoke_structured(llm, [SystemMessage(content=_system_prompt()), HumanMessage(content=msg)],
                                      _ExpandDraft, trace_sink=trace_sink, trace_label="foreach.expand")
        except Exception:  # noqa: BLE001 — expansion is an upgrade; any LLM failure → fallback
            return None
        idx = _map_selection(draft.member_row_indices, rows)
        stmts, issues = _parse_and_validate(draft.body, loop_var, row_fields, goal or body_goal)
        if stmts is not None and not issues:
            note = f"检查点展开:圈选 {len(idx)}/{len(rows)} 行;body {len(stmts)} 步"
            return ForeachExpansion(member_indices=idx, body=stmts, note=note)
        feedback = "\n".join(f"- {i}" for i in (issues or ["(不可解析)"]))
        if attempt < max_retries:
            print(f"  [Expand] 检查点展开校验未过,重试 ({attempt + 1}/{max_retries}): {feedback[:160]}")
    return None
