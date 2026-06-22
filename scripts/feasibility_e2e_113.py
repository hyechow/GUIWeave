#!/usr/bin/env python3
"""Mechanism-2 STAGE 4 — production end-to-end integration check (offline) on task-113.

Unlike scripts/feasibility_kickback_113.py (bespoke judge), this drives the SHIPPED production
components, the exact chain the live loop runs:

  observation.form_controls  →  feasibility.control_presence_text  →  feasibility.judge_feasibility
  (the production module + prompt asset)  →  {infeasible, directive}  →  decompose(goal, knowledge +
  directive)  [the webarena _redecompose seam]  →  direction check on the re-decomposed program.

Proves the production judge + directive + re-decompose produce a feasible (drill-direction) plan.
The TRUE live run (the loop actually hot-swapping mid-task) is the human morning check.
Run:  PYTHONPATH=. uv run python scripts/feasibility_e2e_113.py --n 3
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

from gui_agent.core.orchestrator.decomposer import decompose
from gui_agent.core.orchestrator.program import Finish, If, Program, Run, Stmt
from gui_agent.core.self_learning.app_summary import load_knowledge_for_app
from gui_agent.core.supervisor.milestone.feasibility import control_presence_text, judge_feasibility

GOAL = "Return the customer nickname(s) who gave a rating of 3 stars or below for Olivia zip jacket"
SITE = "shopping_admin"
STUCK_MILESTONE = "设置筛选 Product='Olivia zip jacket' 且 Rating<=3 —— 可见 Rating<=3 已应用，列表已刷新"


@dataclass
class _Obs:
    # The All Reviews grid as the adapter would perceive it: real filter controls, NO Rating control.
    form_controls: Optional[list[dict[str, Any]]] = None
    ui_facts: Optional[list[dict[str, Any]]] = None


_REVIEWS_GRID = _Obs(
    form_controls=[
        {"label": "Status", "kind": "native_select"},
        {"label": "Title", "kind": "input"},
        {"label": "Nickname", "kind": "input"},
        {"label": "Review", "kind": "input"},
        {"label": "Visibility", "kind": "native_select"},
        {"label": "Type", "kind": "native_select"},
        {"label": "Product", "kind": "input", "value": "Olivia zip jacket"},
        {"label": "SKU", "kind": "input"},
    ],
    ui_facts=[{"kind": "grid_state", "record_count": 0, "empty": True}],
)


def _runs(stmts: list[Stmt]) -> list[Run]:
    out: list[Run] = []
    for s in stmts:
        if isinstance(s, Run):
            out.append(s)
        elif isinstance(s, If):
            out += _runs(s.then) + _runs(s.otherwise)
    return out


_RATING = ("rating", "评分", "星级", "stars")
_DRILL = ("逐条", "每条", "逐个", "点开", "打开每", "进入每", "详情", "detail")
_REVIEW = ("评论", "review")


def direction_ok(p: Program) -> tuple[bool, list[str]]:
    runs = _runs(p.statements)
    has_drill = any(
        r.kind == "collect"
        or (any(w in (r.name + r.success_condition).lower() for w in _DRILL)
            and any(w in (r.name + r.success_condition).lower() for w in _REVIEW))
        for r in runs
    )
    wrong = []
    for r in runs:
        t = (r.name + r.success_condition).lower()
        if r.kind == "filter" and any(w in t for w in _RATING):
            wrong.append(f"filter按评分「{r.name}」")
        if r.kind == "data_query" and any(w in (r.sql or "").lower() for w in _RATING) and not has_drill:
            wrong.append(f"data_query查rating「{r.name}」")
    sig = wrong + (["逐条钻取"] if has_drill else [])
    return (not wrong and has_drill), sig


def _fmt(p: Program) -> str:
    out = []
    for i, s in enumerate(p.statements):
        if isinstance(s, Run):
            out.append(f"  [{i}] run/{s.kind} «{s.name}»" + (f" sql={s.sql!r}" if s.sql else ""))
        elif isinstance(s, If):
            out.append(f"  [{i}] if {s.cond.var}[{s.cond.field}]")
        elif isinstance(s, Finish):
            out.append(f"  [{i}] finish «{s.message}»")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3)
    args = ap.parse_args()

    know = load_knowledge_for_app(SITE, "browser")
    base = know.navigation if know else ""
    control_text = control_presence_text(_REVIEWS_GRID)
    print(f"知识={len(base)}c\n[STAGE0] 卡住 milestone: {STUCK_MILESTONE}")
    print(f"[STAGE1] 控件清单(production control_presence_text):\n{control_text}\n")

    print("=" * 70 + "\n[STAGE1] production judge_feasibility\n" + "=" * 70)
    verdict = judge_feasibility(STUCK_MILESTONE, control_text, base)
    print(f"  feasible={verdict.feasible} | {verdict.reason}")
    if verdict.feasible or not verdict.directive:
        print("  ⚠️ 未判不可行/无 directive — 流程在第一关断了"); return
    print(f"  directive: {verdict.directive[:180]}")

    print("\n" + "=" * 70 + "\n[STAGE2+3] 用 directive 重 decompose（_redecompose seam），检查方向\n" + "=" * 70)
    extra = "\n\n## ⚠️ 上层反馈·重规划必读（milestone 判定不可行）\n" + verdict.directive + "\n"
    ok_count = 0
    for k in range(args.n):
        try:
            program = decompose(GOAL, knowledge=base + extra, current_site=SITE)
        except Exception as exc:  # noqa: BLE001
            print(f"\n--- 重规划 #{k+1}: decompose 失败: {exc}"); continue
        ok, sig = direction_ok(program)
        ok_count += ok
        print(f"\n--- 重规划 #{k+1}: {'✅走对(钻取)' if ok else '❌仍走错'}  [{'; '.join(sig)}]")
        print(_fmt(program))
    print("\n" + "=" * 70)
    print(f"production 全链路: STAGE1 判不可行+directive ✓ → STAGE2+3 重规划走对率 = {ok_count}/{args.n}")


if __name__ == "__main__":
    main()
