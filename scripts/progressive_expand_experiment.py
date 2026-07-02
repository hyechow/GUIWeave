# -*- coding: utf-8 -*-
"""Progressive-orchestration feasibility experiment, driven by REAL 778 log data.

The design under test (user direction 2026-07-02): the t=0 skeleton declares a deferred foreach
(expand_goal = semantic intent); at the CHECKPOINT — foreach entry, rows collected — ONE expansion
call sees the REAL rows and produces (a) the member selection as DATA (row ids, not a guessed
predicate) and (b) the concrete per-member body, which then executes deterministically.

This script replays that checkpoint offline using the rows a real 778 run collected off the live
site (logs/gui_agent/webarena/browser/20260702_124348, 7 rows: 3× size-28 variants ids 1841/1842/
1843 — exactly the evaluator's expected save targets — plus 3× size-29 distractors and the
configurable parent WP05). Ground truth is therefore known and the grading is deterministic:

  - selection: member_row_ids == {1841, 1842, 1843}  (the decision t=0 kept getting wrong:
    'size 28' literal vs the actual -28- encoding — here the model just LOOKS at the rows)
  - body: passes validate_program inside the ForEach + the value-binding shape (reads the current
    price, computes ×0.865 / (1-0.135) off the read, wires the computed {var} into the fill action)

K samples measure stability. Usage:
  AGENT_PLATFORM=browser uv run python scripts/progressive_expand_experiment.py --k 5
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
import os

os.chdir(PROJECT_ROOT)
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from gui_agent.core.config import resolve_llm_config  # noqa: E402
from gui_agent.core.orchestrator import Compute, ForEach, Program, Run, validate_program  # noqa: E402
from llm.structured import invoke_structured  # noqa: E402

# ── real rows collected by run 20260702_124348 off the live site ─────────────
REAL_ROWS = [
    {"id": "1842", "name": "Sahara Leggings-28-Gray", "sku": "WP05-28-Gray", "price": "$75.00"},
    {"id": "1846", "name": "Sahara Leggings-29-Red", "sku": "WP05-29-Red", "price": "$75.00"},
    {"id": "1843", "name": "Sahara Leggings-28-Red", "sku": "WP05-28-Red", "price": "$75.00"},
    {"id": "1847", "name": "Sahara Leggings", "sku": "WP05", "price": "$75.00"},
    {"id": "1844", "name": "Sahara Leggings-29-Blue", "sku": "WP05-29-Blue", "price": "$200.00"},
    {"id": "1841", "name": "Sahara Leggings-28-Blue", "sku": "WP05-28-Blue", "price": "$64.88"},
    {"id": "1845", "name": "Sahara Leggings-29-Gray", "sku": "WP05-29-Gray", "price": "$75.00"},
]
GT_MEMBERS = {"1841", "1842", "1843"}

GOAL = "Reduce the price of size 28 Sahara leggings by 13.5%"
EXPAND_GOAL = "对目标集合(size 28 的 Sahara leggings 变体)的每个成员:打开其编辑页,读取当前 Price,按降价 13.5% 计算新价,填入 Price 并保存"

_SYSTEM = """你在渐进式编排的【foreach 检查点】:骨架计划已声明「对目标集合的每个成员执行子目标」,\
现在集合的候选行已从页面真实采集到(见下方 JSON)。你的任务两件:

1. **圈选成员**(member_row_indices 给行号,或 member_row_ids 给 id):看着真实行数据,判断哪些行属于目标集合,给出它们的 id 列表。\
不要写谓词、不要归纳规律——直接根据每行的实际字段值逐行判断。
2. **产出成员 body**(body):对每个选中的成员要执行的具体步骤列表(所有成员共用,\
用 {row[字段]} 引用当前成员的行字段)。可用步骤:
   - {"op":"run","kind":"navigation|action","name":"...","success_condition":"...","var":"...","returns":[...],"read_spec":"..."}
   - {"op":"compute","var":"...","expr":"<受限表达式:round/float、算术、{row[字段]} 或已读变量>"}
   - {"op":"if","cond":{"var":"某读取步的var","field":"字段","cmp":"empty|exists|==|!=","value":"..."},"then":[...],"otherwise":[...]}(分支;空值回退用 cmp="empty")
   规则:运行时才知道的值必须先读(returns)再用;算出的值必须以 {变量名} 模板写进后续 action 的 name;\
success_condition 写可见终态。
若子目标含「读某属性,为空则回退到父/关联实体」:body 必须写成 读取步(returns 该属性)→ if 该字段 empty → then 分支里 compute 派生关联键(如 name.rsplit('-', 2)[0] 去掉“-尺寸-颜色”后缀)→ 用 {派生变量} 搜索并打开关联实体 → 再读该属性;otherwise 分支留空(已读到)。

只输出 JSON。"""


class ExpandDraft(BaseModel):
    member_row_ids: list[str] = Field(default_factory=list)
    member_row_indices: list[int] = Field(default_factory=list)
    body: list[dict] = Field(default_factory=list)
    reason: str = ""


def _llm() -> ChatOpenAI:
    cfg = resolve_llm_config("supervisor.decompose")
    if not cfg.model:
        cfg = resolve_llm_config("supervisor")
    return ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url,
                      extra_body={"enable_thinking": False})


def grade(draft: ExpandDraft) -> list[str]:
    fails: list[str] = []
    sel = set(draft.member_row_ids)
    if sel != GT_MEMBERS:
        fails.append(f"selection {sorted(sel)} != GT {sorted(GT_MEMBERS)}")

    # body must parse as Stmts inside a ForEach and pass the standard validator
    for st in draft.body:  # tolerate null-valued optional strings from json_object mode
        for k in ("read_spec", "success_condition", "name", "var"):
            if k in st and st[k] is None:
                st[k] = "" if k != "var" else None
    try:
        program = Program(goal=GOAL, statements=[
            ForEach(var="row", target="已圈选的成员行", returns=["id", "name", "sku", "price"],
                    body=draft.body),  # pydantic parses the dicts through the Stmt union
        ])
    except Exception as e:  # noqa: BLE001
        fails.append(f"body 不是合法 DSL: {str(e)[:120]}")
        return fails
    issues = validate_program(program)
    for i in issues:
        fails.append(f"VALIDATOR:{i.code}")

    body = program.statements[0].body
    runs = [s for s in body if isinstance(s, Run)]
    computes = [s for s in body if isinstance(s, Compute)]
    reads_price = any(r.returns and any("price" in f.lower() or "价" in f for f in r.returns) for r in runs)
    # grid-collect source is equally valid: the row already carries the Price column
    row_price_src = any(re.search(r"row\[.?price|\{row\[price\]\}", c.expr) for c in computes)
    if not reads_price and not row_price_src:
        fails.append("body 没有价格来源(读取步或 {row[price]})")
    if not any(re.search(r"0\.865|1\s*-\s*0\.135|13\.5", c.expr) for c in computes):
        fails.append("body 没有按 13.5% 计算的 compute")
    cvars = [c.var for c in computes if c.var]
    if cvars and not any(("{" + v + "}") in (r.name or "") for v in cvars for r in runs):
        fails.append("算出的新价没有以 {var} 接进填值动作")
    return fails


# ── case 185: per-row JOIN body (read → if-empty → derive parent → search → read parent) ─────
# Real rows from run 20260626_163834 (task 185 family "material of products with 3 units left"):
# two qty=3 children, no material column collected — the body must do the full parent join.
ROWS_185 = [
    {"Action_url": "http://192.168.31.57:7780/admin/catalog/product/edit/id/1478/",
     "name": "Minerva LumaTech\u2122 V-Tee-XS-Blue", "size": "XS"},
    {"Action_url": "http://192.168.31.57:7780/admin/catalog/product/edit/id/1182/",
     "name": "Eos V-Neck Hoodie-S-Blue", "size": "S"},
]
GT_MEMBERS_185 = {0, 1}
GOAL_185 = "Give me the material of the products that have 3 units left"
EXPAND_GOAL_185 = ("对每个成员:打开 {row[Action_url]} 进入产品编辑页,读取 Material 属性;"
                   "若 Material 为空(子变体常不设),从产品名称去掉「-尺寸-颜色」后缀派生父产品名,"
                   "按父产品名搜索并打开父产品(Configurable Product),读取父的 Material;返回 material")


def _sel_indices(draft: "ExpandDraft") -> set[int]:
    if draft.member_row_indices:
        return {int(i) for i in draft.member_row_indices}
    return {int(x) for x in draft.member_row_ids if str(x).isdigit()}


def grade_185(draft: ExpandDraft) -> list[str]:
    fails: list[str] = []
    if _sel_indices(draft) != GT_MEMBERS_185:
        fails.append(f"selection {sorted(_sel_indices(draft))} != {sorted(GT_MEMBERS_185)}")
    from gui_agent.core.orchestrator.expansion import _parse_and_validate
    stmts, issues = _parse_and_validate(draft.body, "row", ["Action_url", "name", "size"], GOAL_185)
    if stmts is None:
        return fails + issues
    fails += issues
    body = stmts

    def _all_runs(stmts):
        out = []
        for s2 in stmts:
            if isinstance(s2, Run): out.append(s2)
            elif hasattr(s2, "then"): out += _all_runs(s2.then) + _all_runs(s2.otherwise)
        return out

    def _all_computes(stmts):
        out = []
        for s2 in stmts:
            if isinstance(s2, Compute): out.append(s2)
            elif hasattr(s2, "then"): out += _all_computes(s2.then) + _all_computes(s2.otherwise)
        return out

    runs, computes = _all_runs(body), _all_computes(body)
    if not any("{row[Action_url]}" in (r.name or "") for r in runs):
        fails.append("没有用 {row[Action_url]} 打开子产品(应 URL 直达)")
    if not any(r.returns and any("material" in f.lower() for f in r.returns) for r in runs):
        fails.append("没有读取 material 的步骤")
    has_if = any(not isinstance(s2, (Run, Compute)) and hasattr(s2, "then") for s2 in body)
    if not has_if:
        fails.append("没有 if 空值回退分支(子变体 Material 常为空,须回退父产品)")
    derives = any(re.search(r"rsplit|removesuffix|re_sub|split", c.expr) for c in computes)
    if not derives:
        fails.append("没有派生父产品名的 compute(去 -尺寸-颜色 后缀)")
    cvars = [c.var for c in computes if c.var]
    if cvars and not any(("{" + v + "}") in (r.name or "") for v in cvars for r in runs):
        fails.append("派生出的父名没有以 {var} 接进搜索/导航步骤")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--case", choices=["778", "185"], default="778")
    args = ap.parse_args()
    llm = _llm()
    if args.case == "185":
        goal, expand_goal, rows, grader = GOAL_185, EXPAND_GOAL_185, ROWS_185, grade_185
        gt_members = GT_MEMBERS_185
    else:
        goal, expand_goal, rows, grader = GOAL, EXPAND_GOAL, REAL_ROWS, grade
        gt_members = GT_MEMBERS
    human = (f"总目标:{goal}\n\n子目标(对每个成员):{expand_goal}\n\n循环变量名:row\n"
             f"已采集的候选行(真实页面数据,行号即列表下标):\n{json.dumps(rows, ensure_ascii=False, indent=1)}")

    ok = 0
    sel_ok = 0
    for i in range(args.k):
        draft = invoke_structured(llm, [SystemMessage(content=_SYSTEM), HumanMessage(content=human)], ExpandDraft)
        fails = grader(draft)
        if fails:  # one feedback retry, mirroring the real decompose pipeline
            retry_h = human + "\n\n上一版的问题(修正后重出完整 JSON):\n" + "\n".join(f"- {f}" for f in fails)
            draft = invoke_structured(llm, [SystemMessage(content=_SYSTEM), HumanMessage(content=retry_h)], ExpandDraft)
            fails = grader(draft)
        if args.case == "185":
            sel_hit = _sel_indices(draft) == gt_members
        else:
            sel_hit = set(draft.member_row_ids) == gt_members
        sel_ok += sel_hit
        ok += not fails
        print(f"[{i+1}/{args.k}] {'✅' if not fails else '❌'} 圈选={'✓' if sel_hit else sorted(set(draft.member_row_ids))}"
              f"  body={len(draft.body)}步" + (f"  fails: {fails}" if fails else ""))

    print(f"\n圈选正确率: {sel_ok}/{args.k}   整体(圈选+body 全过): {ok}/{args.k}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
