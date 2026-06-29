"""Browser RE-decompose eval: mid-run kick-back → revise the REMAINING plan (NOT a fresh decompose).

Tests `gui_agent.core.orchestrator.decomposer.redecompose` — the dedicated re-decompose used by the
Feasibility-Guard kick-back (loop._perform_replan). Unlike `decompose` (goal → full plan from the
start screen, exercised by evals/browser/orchestrator), this is invoked mid-execution: some
milestones already ran (prior_experience), one hit a correction (corrective_directive), and the rest
(remaining_plan) must be re-planned from the CURRENT page. The whole point is that re-decompose is
STATEFUL — it continues from where the run actually is, absorbing experience, NOT restarting from the
homepage (the user's "重编排不是首页开始的 / 前面的决策过程就是一种经验，也要作为 context").

Seeded from logs/gui_agent/webarena/browser/20260622_201729 (task 113): the live closure froze the
T0 homepage frame + passed no experience, so re-decompose produced "进入 Products List → 搜索 Olivia
zip jacket" — the same doomed product-search dead end — and the run ended UNKNOWN_ERROR. With the
current page + experience + directive fed (as the fixed closure now does), the plan must instead drill
each review's detail for its rating, per the directive.

Like the orchestrator-decompose eval: real LLM (on-demand, non-deterministic — run a few times when
judging a prompt change), screenshot-less by default (no image fixtures needed).

Run:
  uv run python evals/browser/redecompose/test_redecompose.py
  uv run python evals/browser/redecompose/test_redecompose.py --show-program
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.orchestrator import Finish, ForEach, If, Run, redecompose
from gui_agent.core.orchestrator.engine import normalize_confirm_read_gates, normalize_precondition_gates
from gui_agent.core.self_learning.app_summary import auto_discover_knowledge, load_knowledge_for_app

CASES_FILE = Path(__file__).parent / "cases.json"

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:72s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _flatten_runs(stmts: list) -> list[Run]:
    out: list[Run] = []
    for s in stmts:
        if isinstance(s, Run):
            out.append(s)
        elif isinstance(s, If):
            out.extend(_flatten_runs(s.then))
            out.extend(_flatten_runs(s.otherwise))
        elif isinstance(s, ForEach):
            out.extend(_flatten_runs(s.body))   # body Runs count too (the drill steps live here)
    return out


# ── Named assertions (per-case, keyed by string — same pattern as the decompose eval) ──

# Re-navigating AWAY from the current page back to a start/entry point — the 201729 failure shape
# (re-decompose planned "进入 Products List" + "搜索产品" from the frozen homepage). A stateful
# re-decompose continues from the current page; it must not plan these restart milestones.
_RESTART_MARKERS = (
    "products list", "产品列表", "商品列表",
    "重新登录", "重新登陆", "回到首页", "返回首页", "打开网站", "进入网站", "回到仪表盘", "回到 dashboard",
)
# The 201729 dead end specifically: re-searching the product by name on a page that has no such
# control. The directive says to USE the already-found candidates, not re-search the product.
_PRODUCT_RESEARCH_MARKERS = ("搜索产品", "搜索 产品", "按产品名", "重新搜索 olivia", "搜索 olivia zip jacket")

# Browser agent has NO filesystem access. Planning "Export CSV/Excel → read the file" is an
# invalid path: after download the agent cannot read the local file. Re-decompose must stay
# within browser capabilities (grid columns / foreach drill / data_query). Regression
# logs/gui_agent/webarena/browser/20260629_092711 (task 63): data_query failed (Orders grid has
# no Customer Email column, partial), kick-back re-decompose planned "click Export → export CSV
# → read file" — a dead path the browser agent cannot complete.
_FILE_EXPORT_MARKERS = (
    "export", "导出", "下载", "download", "csv", "excel", "读文件", "读取文件", "解析文件",
)


def _check_assertions(program, assertions: list[str]) -> list[str]:
    details: list[str] = []
    runs = _flatten_runs(program.statements)
    names = " | ".join(r.name.lower() for r in runs)
    for assertion in assertions:
        if assertion == "no_homepage_restart":
            hits = [r.name for r in runs if any(m in r.name.lower() for m in _RESTART_MARKERS)]
            if hits:
                details.append(
                    f"重排不得从首页/起点重启（应从当前页面续）: 出现重启型 milestone {hits}"
                )
        elif assertion == "no_product_research_dead_end":
            hits = [r.name for r in runs if any(m in r.name.lower() for m in _PRODUCT_RESEARCH_MARKERS)]
            if hits:
                details.append(
                    f"重排不得退回『按产品名重新搜索』的死路（纠正指令要求用已找到的候选逐条钻取）: {hits}"
                )
        elif assertion == "no_offline_file_export":
            hits = [r.name for r in runs if any(m in r.name.lower() for m in _FILE_EXPORT_MARKERS)]
            if hits:
                details.append(
                    f"重排不得规划浏览器外路径（导出/下载文件后读取——agent 是浏览器助手，无文件系统能力，"
                    f"download 后无法读本地文件）: {hits}"
                )
        elif assertion == "drills_detail_reads_rating":
            # The directive's route: open each review's DETAIL and read its rating (+ nickname).
            opens_detail = any(
                any(k in r.name.lower() for k in ("详情", "detail", "打开", "逐条", "点击"))
                for r in runs
            )
            reads = [r for r in runs if r.returns or r.kind == "data_query"]
            read_text = " ".join(
                (r.name + " " + " ".join(r.returns) + " " + (r.read_spec or "")).lower() for r in reads
            )
            reads_rating = any(k in read_text for k in ("rating", "评分", "星", "star"))
            if not opens_detail:
                details.append(f"未见『打开/逐条进入评论详情』步骤: names=[{names}]")
            if not reads or not reads_rating:
                details.append(
                    f"未见读取 rating/评分 的返回值/data_query 步（纠正指令要求逐条读取 Detailed Rating）"
                )
        elif assertion == "scopes_collection_to_entity":
            # Regression logs/gui_agent/webarena/browser/20260622_201729 (live re-run): the drill
            # mechanism worked (27 reviews read via direct-URL jumps) but the ANSWER was wrong — the
            # collection was never scoped to the target product (the Olivia=Product filter was fumbled
            # / dropped on re-decompose), so 27 reviews of the WRONG product got drilled and the
            # rating<=3 filter returned other-product nicknames. The fix (decomposer rule 11⑤ +
            # redecomposer override 6): the entity-scoped drill must (a) read the entity-identifying
            # column (Product) in the row collection, and (b) carry the entity-scope predicate (search_key) into
            # the final data_query — so a fumbled upstream filter yields empty → re-take, instead of
            # silently returning the wrong product's reviewers. search_key for task 113 == "Olivia".
            key = "olivia"
            foreaches = [s for s in program.statements if isinstance(s, ForEach)]
            collection_fields: list[str] = []
            for fe in foreaches:
                if fe.over:
                    collection_fields += next(
                        (r.returns for r in runs if r.kind == "read" and r.var == fe.over), []
                    )
                else:
                    collection_fields += fe.returns  # new-style foreach: collects its own rows
            reads_entity_col = any("product" in f.lower() for f in collection_fields)
            dqs = [r for r in runs if r.kind == "data_query"]
            dq_sql = " ".join((r.sql or "").lower() for r in dqs)
            scopes_dq = "product" in dq_sql and key in dq_sql
            if not foreaches:
                details.append(f"未见 foreach 采集步，无从校验实体范围采集: names=[{names}]")
            elif not reads_entity_col:
                details.append(
                    "采集步（read 步或 foreach.returns）未读出实体标识列 Product（规则11⑤要求顺手读出，作 data_query 范围谓词的防线）"
                )
            if not dqs:
                details.append("缺少 data_query 步（应在累积表上按 Product 范围 + rating 过滤）")
            elif not scopes_dq:
                details.append(
                    f"data_query 未带实体范围谓词（应 `WHERE Product LIKE '%Olivia%' AND ...`，防上游 Product 筛选失效采到别产品）: sql=[{dq_sql[:160]}]"
                )
        elif assertion == "uses_foreach_iteration":
            # The general-iteration shape (NOT N unrolled steps / not just the first row): a row
            # collection (legacy read step, or the foreach's own name/returns) feeds a foreach whose
            # body opens each detail + reads rating, then a data_query filters the accumulated set.
            # Regression 20260622_211542: the unrolled re-decompose truncated and processed only the
            # first review → NOT_FOUND. foreach covers ALL rows.
            foreaches = [s for s in program.statements if isinstance(s, ForEach)]
            if not foreaches:
                details.append(f"未用 foreach 迭代（应先采集候选行再 foreach→data_query，不展开/不只读第一条）: names=[{names}]")
            else:
                fe = foreaches[0]
                if fe.over:
                    over_is_read_var = any(
                        isinstance(s, Run) and s.kind == "read" and s.var == fe.over
                        for s in program.statements
                    )
                    if not over_is_read_var:
                        details.append(f"foreach 的 over「{fe.over}」未指向任何 read 步")
                body_reads = [b for b in fe.body if isinstance(b, Run) and b.returns]
                body_text = " ".join(
                    (b.name + " " + " ".join(b.returns) + " " + (b.read_spec or "")).lower() for b in body_reads
                )
                if not any(k in body_text for k in ("rating", "评分", "星", "star")):
                    details.append("foreach body 未读取 rating/评分（应逐条进详情读评分）")
                if not any(isinstance(s, Run) and s.kind == "data_query" for s in program.statements):
                    details.append("foreach 之后缺少 data_query（应对累积表筛 rating<=3）")
        elif assertion == "has_finish":

            def _has_finish(stmts: list) -> bool:
                for s in stmts:
                    if isinstance(s, Finish):
                        return True
                    if isinstance(s, If) and (_has_finish(s.then) or _has_finish(s.otherwise)):
                        return True
                return False
            if not _has_finish(program.statements):
                details.append("缺少 finish 步（无最终答复模板）")
        else:
            details.append(f"未知断言: {assertion}")
    return details


def _load_case_knowledge(case: dict):
    platform = case.get("platform", "browser")
    app = case.get("knowledge_app") or case.get("site")
    if app:
        return load_knowledge_for_app(app, platform)
    return auto_discover_knowledge(case["goal"], platform)


def _case_program(case: dict):
    k = _load_case_knowledge(case)
    resolution = None
    if case.get("resolution"):
        from gui_agent.core.router import EntityRef, IntentResolution
        resolution = IntentResolution(entities=[EntityRef(**e) for e in case["resolution"]])
    program = redecompose(
        case["goal"],
        remaining_plan=case.get("remaining_plan", ""),
        prior_experience=case.get("prior_experience", ""),
        corrective_directive=case.get("corrective_directive", ""),
        knowledge=k.navigation if k else "",
        current_url=case.get("current_url", ""),
        current_title=case.get("current_title", ""),
        current_site=case.get("current_site")
        or (k.app_name if k and case.get("use_knowledge_app_as_current_site") else ""),
        table_summaries=case.get("table_summaries"),
        resolution=resolution,
    )
    if case.get("normalize"):
        program = normalize_precondition_gates(normalize_confirm_read_gates(program))
    return program


def _dump_program(program) -> None:
    def _walk(stmts, indent="       "):
        for s in stmts:
            if isinstance(s, Run):
                extra = f" returns={s.returns!r}" if s.returns else ""
                spec = f" read_spec={s.read_spec!r}" if s.read_spec else ""
                print(f"{indent}[{s.kind}] {s.name}: {s.success_condition}{extra}{spec}")
            elif isinstance(s, If):
                print(f"{indent}[if] {s.cond.var}[{s.cond.field}] {s.cond.cmp} {s.cond.value!r}")
                if s.then:
                    print(f"{indent}  then:")
                    _walk(s.then, indent + "    ")
                if s.otherwise:
                    print(f"{indent}  else:")
                    _walk(s.otherwise, indent + "    ")
            elif isinstance(s, ForEach):
                print(f"{indent}[foreach] {s.var} in {s.over} -> {s.into or s.var + 's'}")
                _walk(s.body, indent + "    ")
            elif isinstance(s, Finish):
                print(f"{indent}[finish] {getattr(s, 'message', '')}")
    _walk(program.statements)


def run_redecompose_eval(show_program: bool = False) -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    print("── Browser Re-decompose Eval ──")
    print("  测的是 redecompose() 原始产出：中途重排剩余计划，从当前页面续、带经验、服从纠正指令。")
    for c in cases:
        try:
            program = _case_program(c)
        except Exception as e:  # noqa: BLE001
            _report(c["label"], False, f"exception: {e}")
            continue
        details = _check_assertions(program, c.get("assertions", []))
        ok = len(details) == 0
        _report(c["label"], ok, "; ".join(details) if details else "")
        if show_program or not ok:
            _dump_program(program)


def test_redecompose() -> None:
    run_redecompose_eval()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--show-program", action="store_true")
    args = parser.parse_args()
    run_redecompose_eval(show_program=args.show_program)
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
