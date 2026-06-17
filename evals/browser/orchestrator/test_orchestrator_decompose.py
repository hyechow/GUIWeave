"""Browser orchestrator-decompose eval: user goal -> DSL Program (run / if / finish).

Tests `gui_agent.core.orchestrator.decomposer.decompose` — the **DSL program**
decomposer used by `bin/runner ... --orchestrator`. This is a DIFFERENT module from
the DAG decomposer in `evals/browser/decomposer/` (that one drives
`MilestoneSupervisorPolicy._decompose` and emits milestones; this one emits a Program
of run/if/finish statements).

Production-faithful:
  * knowledge auto-discovery on the goal (same as runner.py), unless a case pins a site
    name for WebArena-style tasks whose intent text does not mention the site, and
  * screenshot-less by default — browser runner.py decomposes the goal before turn 1,
    while WebArena can pass front-tab metadata / screenshot explicitly.

Because there's no screenshot input, this eval needs no image fixtures (clean re: the
no-images-in-git rule). It calls the real LLM, so it's an on-demand eval (non-deterministic;
run it a few times when judging a prompt change).

Run:
  uv run python evals/browser/orchestrator/test_orchestrator_decompose.py
  uv run python evals/browser/orchestrator/test_orchestrator_decompose.py --label WebArena --show-program
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)  # @<file> refs / knowledge dir resolve relative to repo root

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.orchestrator import Finish, If, Run, decompose
from gui_agent.core.orchestrator.engine import normalize_confirm_read_gates, normalize_precondition_gates
from gui_agent.core.orchestrator.program import TEMPLATE_RE
from gui_agent.core.self_learning.app_summary import auto_discover_knowledge, load_knowledge_for_app

CASES_FILE = Path(__file__).parent / "cases.json"

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:64s}"
    if detail:
        line += f"  {detail}"
    print(line)


# ── Program walkers (control flow lives in if-branches; flatten before asserting) ──


def _flatten_runs(stmts: list) -> list[Run]:
    out: list[Run] = []
    for s in stmts:
        if isinstance(s, Run):
            out.append(s)
        elif isinstance(s, If):
            out.extend(_flatten_runs(s.then))
            out.extend(_flatten_runs(s.otherwise))
    return out


def _has_finish(stmts: list) -> bool:
    for s in stmts:
        if isinstance(s, Finish):
            return True
        if isinstance(s, If) and (_has_finish(s.then) or _has_finish(s.otherwise)):
            return True
    return False


def _flatten_ifs(stmts: list) -> list[If]:
    out: list[If] = []
    for s in stmts:
        if isinstance(s, If):
            out.append(s)
            out.extend(_flatten_ifs(s.then))
            out.extend(_flatten_ifs(s.otherwise))
    return out


def _confirm_read_actions(stmts: list) -> list[Run]:
    """Action Runs immediately followed by a read Run — the confirm-read shape (rule 6).
    Adjacency is checked WITHIN each statement list, recursing into if-branches (same
    structural rule the engine's normalize_confirm_read_gates uses)."""
    out: list[Run] = []
    for i, s in enumerate(stmts):
        if isinstance(s, Run) and s.kind == "action":
            nxt = stmts[i + 1] if i + 1 < len(stmts) else None
            if isinstance(nxt, Run) and nxt.kind == "read":
                out.append(s)
        elif isinstance(s, If):
            out.extend(_confirm_read_actions(s.then))
            out.extend(_confirm_read_actions(s.otherwise))
    return out


def _adjacent_run_pairs(stmts: list) -> list[tuple[Run, Run]]:
    """Adjacent Run pairs within each statement list, recursing into if branches."""
    out: list[tuple[Run, Run]] = []
    for i, s in enumerate(stmts):
        if isinstance(s, Run):
            nxt = stmts[i + 1] if i + 1 < len(stmts) else None
            if isinstance(nxt, Run):
                out.append((s, nxt))
        elif isinstance(s, If):
            out.extend(_adjacent_run_pairs(s.then))
            out.extend(_adjacent_run_pairs(s.otherwise))
    return out


def _check_basic(program, expected: dict) -> list[str]:
    details: list[str] = []
    runs = _flatten_runs(program.statements)
    reads = [r for r in runs if r.kind == "read"]
    if "min_statements" in expected and len(program.statements) < expected["min_statements"]:
        details.append(f"expected >={expected['min_statements']} top-level steps, got {len(program.statements)}")
    if expected.get("has_read") and not reads:
        details.append("缺少 read 步（无只读结果提取步）")
    if expected.get("has_finish") and not _has_finish(program.statements):
        details.append("缺少 finish 步（无最终答复模板）")
    return details


# A confirm-read-backed action's gate must read as DISPATCH (the action fired) and/or
# DEFER (result is the next read's job) — NOT as a bare result/verdict the checker must
# adjudicate. Vocab-agnostic across create/submit/delete/send/detect: we only look for a
# dispatch- or defer-signal, never for domain result words (那会退回连通专用)。
_DISPATCH_DEFER_MARKERS = (
    # 动作已发出
    "点击", "已点", "按下", "提交", "发送", "触发", "发起", "进入计算", "计算中",
    "加载", "出现响应", "已发出", "已请求", "已执行", "已操作",
    # 结果让位给后续 read
    "由下一步", "由后续", "下一步读取", "下一步判", "read 判", "不判定结果",
    "不判具体", "具体结果由", "成败由", "结果由",
)


def _check_assertions(program, assertions: list[str]) -> list[str]:
    details: list[str] = []
    runs = _flatten_runs(program.statements)
    cr_actions = _confirm_read_actions(program.statements)

    for assertion in assertions:
        if assertion == "key_action_has_confirm_read":
            # 规则6：会改状态/出结果的关键动作后要补一个 read 确认结果。判据=存在「action 紧跟
            # read」的 confirm-read 对（否则结果只能靠会幻觉的 checker 判，正是要避免的）。
            if not cr_actions:
                details.append(
                    f"无 confirm-read 对（没有 action 紧跟 read）: "
                    f"{[(r.kind, r.name) for r in runs]}"
                )
        elif assertion == "confirm_read_action_uses_dispatch_gate":
            # 回归 20260615_100753 的泛化版（词表无关）。凡是 confirm-read 撑腰的 action，其
            # success_condition 必须是 dispatch/defer 门（动作已发出 / 结果由下一步读取判定），
            # 不得把结果当终态——否则执行期 checker 与 read 双判同一结果，反复纠结刚出现/会消失
            # 的标志（看到却不信→重触发、把同一标志判读漂移）。判据=每个 confirm-read action 的
            # 验收里至少含一个 dispatch/defer 信号；纯结果门（无任一信号）即违规。
            offenders = [
                (a.name, a.success_condition) for a in cr_actions
                if not any(m in a.success_condition for m in _DISPATCH_DEFER_MARKERS)
            ]
            if offenders:
                details.append(
                    f"confirm-read 的 action 验收非 dispatch/defer 门、把结果当终态: {offenders}"
                )
        elif assertion == "auth_milestone_terminal_state":
            # 登录/认证类前置回归两次卡死（153314 验收=登录表单、162312 验收=业务数据卡片，已登录会话都
            # 不可达）。现在生产由【结构标记 run.precondition】兜底：decomposer 标 precondition=true →
            # engine.normalize_precondition_gates 确定性把验收换成「已处于目标状态」的通用门（详见
            # tests/test_orchestrator.py），与 confirm-read 的 L2 同构、但检测信号是 flag 不是关键词。
            # 所以这里测的是【这个结构信号的可靠性】：登录/认证前置 milestone 必须标 precondition=true
            # （标了兜底才接得住；没标→门写歪就会卡死）。【软信号·有生产兜底】FAIL = decomposer 漏标了
            # flag（可更可靠），但具体登录态判读由 checker 的 _check.md 兜，且只要标了 flag 门就被通用门
            # 覆盖。用关键词在【测试侧】定位登录步（生产侧用 flag，不碰字符串）。
            # 测试侧用关键词定位登录【前置】步：非 read、名字含登录/认证，且排除「查/看登录日志/记录/历史」
            # 这种操作 login 数据的步（它们不是前置，不该要求标 flag）。生产侧不碰字符串、只看 flag。
            auth_ms = [
                r for r in runs
                if r.kind != "read"
                and any(k in r.name for k in ("登录", "登入", "登陆", "认证"))
                and not any(k in r.name for k in ("日志", "记录", "历史"))
            ]
            unflagged = [r.name for r in auth_ms if not getattr(r, "precondition", False)]
            if auth_ms and unflagged:
                details.append(
                    f"登录/认证前置 milestone 没标 precondition=true（L2 兜底靠这个结构标记，没标就接不住、门写歪会卡死）: {unflagged}"
                )
        elif assertion == "read_has_spec":
            # 只读单帧没判读说明就只能瞎猜（见 structured_read / prompt 规则）。每个 read 都要有
            # returns + 非空 read_spec。
            bad = [
                (r.name, r.returns) for r in runs
                if r.kind == "read" and (not r.returns or not r.read_spec.strip())
            ]
            if bad:
                details.append(f"read 步缺 returns 或 read_spec（判读说明）: {bad}")
        elif assertion == "shopping_admin_review_count_uses_action_read":
            # WebArena shopping_admin #15/#11 style: query a Magento admin grid, then report the
            # authoritative count. Applying a Review/search filter is only a trigger; the count
            # must be read from the grid summary such as "N records found". In production this
            # case runs after normalize_confirm_read_gates(), so a filter immediately followed by
            # read should have become action→read with a dispatch/defer gate.
            def _looks_like_count_read(r: Run) -> bool:
                text = " ".join([r.name, r.read_spec, *r.returns]).lower()
                return any(
                    marker in text
                    for marker in (
                        "record", "records found", "count", "total", "review",
                        "评论", "评价", "记录数", "数量", "总数",
                    )
                )

            count_pairs = [
                (a, b) for a, b in _adjacent_run_pairs(program.statements)
                if b.kind == "read" and _looks_like_count_read(b)
            ]
            action_pairs = [(a, b) for a, b in count_pairs if a.kind == "action"]
            filter_pairs = [(a, b) for a, b in count_pairs if a.kind == "filter"]
            if not count_pairs:
                details.append(
                    "没有找到紧邻触发步骤的计数 read（应先筛选/搜索，再 read 读取 N records found/评论总数）"
                )
            elif not action_pairs:
                details.append(
                    f"计数 read 前一跳不是 action 触发器（生产 normalizer 后应为 action→read）: "
                    f"{[(a.kind, a.name, b.name) for a, b in count_pairs]}"
                )
            if filter_pairs:
                details.append(
                    f"计数 read 前仍是 filter→read，说明筛选结果还会被 filter checker 重判: "
                    f"{[(a.name, b.name) for a, b in filter_pairs]}"
                )
            bad_gates = [
                (a.name, a.success_condition) for a, _ in action_pairs
                if not any(m in a.success_condition for m in _DISPATCH_DEFER_MARKERS)
            ]
            if bad_gates:
                details.append(
                    f"计数 read 前的 action 不是 dispatch/defer 门（筛选成败应由 read 判定）: {bad_gates}"
                )
            action_text = " ".join(a.name.lower() for a, _ in action_pairs)
            if action_pairs and not any(k in action_text for k in ("best", "review", "评论", "评价", "筛选", "搜索", "filter", "search")):
                details.append(
                    f"计数 read 前的 action 看不出是在按 review/best 搜索筛选: {[a.name for a, _ in action_pairs]}"
                )
        elif assertion == "action_targets_read_entity":
            # read-then-reference（规则8，回归 20260615_163258）。不验「出现了某种 {var[字段]} 形态」
            # （那只是 prompt 样式），而验整条**顺序不变量**：
            #   action(创建/识别实体) → read(读出该实体的标识，绑定 var) → 之后的 action 用同一 {var[字段]} 引用。
            # 163258 侥幸做对仅因列表只有一台、planner 从屏幕拿到了名字；有同类兄弟就指错。这里要求三段
            # 按程序顺序齐备：read 之前有产生该实体的 action、read 之后有 action 用同一 var 引用其 returns 字段。
            seq = _flatten_runs(program.statements)  # DFS = 程序顺序（本 case 线性，无分支）
            ok = False
            for i, rd in enumerate(seq):
                if rd.kind != "read" or not rd.var:
                    continue
                fields = set(rd.returns)
                prior_action = any(a.kind == "action" for a in seq[:i])
                later_ref = any(
                    a.kind == "action" and any(
                        m.group(1) == rd.var and m.group(2).strip().strip("'\"") in fields
                        for m in TEMPLATE_RE.finditer(a.name)
                    )
                    for a in seq[i + 1:]
                )
                if prior_action and later_ref:
                    ok = True
                    break
            if not ok:
                details.append(
                    "缺少 read-then-reference 顺序结构：应为 action(创建/识别实体) → read(读出标识,var) → "
                    "之后的 action 用同一 {var[字段]} 引用（系统生成名称要 read 出再接力，别裸名词/赌列表只有一个）: "
                    f"{[(r.kind, r.name) for r in seq]}"
                )
        elif assertion == "connectivity_gates_order_no_robot_creation":
            # 回归 20260615_194320：分解器 ~1/4 概率把「给现成机器人下移动订单」幻觉成「先新建一台
            # 机器人 + 编辑设预设站点 + 再下单」，把目标没要求的造实体前置块塞在连通检测之前，整个 run
            # 耗死在幻觉机器人上、真任务一步没碰（页面本来就有现成 lucas-10003）。两条不变量：
            #  ① 建订单受连通检测门控：有 read 检测连通 + if 按它分支，建订单 action 在该 if 内（连通分支），
            #     且顶层不得有无条件的建订单 action。
            #  ② 连通检测之前不得出现「新建/创建机器人」或「编辑预设站点」这类目标没要求的造实体步骤。
            seq = _flatten_runs(program.statements)

            def _conn(r):  # 连通检测相关（名字或读取字段含 连通/可达）
                return ("连通" in r.name or "可达" in r.name
                        or any("连通" in x or "可达" in x for x in r.returns))

            def _order(r):  # 建移动订单（≠ 造机器人）
                return r.kind == "action" and any(k in r.name for k in ("订单", "建单", "下单"))

            def _robot_create(r):  # 造机器人幻觉的 action（排除「为机器人创建订单」、也排除只是名字含「新建」的 read）
                return (r.kind == "action" and "机器人" in r.name and "订单" not in r.name
                        and any(k in r.name for k in ("新建", "创建", "添加")))

            def _preset(r):  # 编辑预设站点幻觉
                return "预设站点" in r.name

            conn_vars = {r.var for r in seq if r.kind == "read" and r.var and _conn(r)}
            gated: list = []

            def _walk_gate(stmts, under):
                for s in stmts:
                    if isinstance(s, Run):
                        if _order(s) and under:
                            gated.append(s)
                    elif isinstance(s, If):
                        deeper = under or s.cond.var in conn_vars
                        _walk_gate(s.then, deeper)
                        _walk_gate(s.otherwise, deeper)
            _walk_gate(program.statements, False)
            top_orders = [s for s in program.statements if isinstance(s, Run) and _order(s)]

            if not conn_vars:
                details.append("没有检测连通的 read 步（应先 read 检测连通，再据此 if 分支）")
            if not gated:
                details.append("建订单的 action 未被连通检测的 if 门控（应在 if 连通 的分支里）")
            if top_orders:
                details.append(
                    f"顶层出现无条件建订单（未受连通检测门控、连通与否都会下单）: {[r.name for r in top_orders]}"
                )
            conn_idx = next((i for i, r in enumerate(seq) if _conn(r)), None)
            if conn_idx is not None:
                bad = [r.name for r in seq[:conn_idx] if _robot_create(r) or _preset(r)]
                if bad:
                    details.append(
                        f"连通检测前出现目标没要求的造实体步骤（新建机器人/编辑预设站点幻觉，回归 194320）: {bad}"
                    )
        elif assertion == "cross_page_action_navigates_no_list_pick":
            # 回归 20260615_211634：连通后分支直接 read 机器人列表 + 建单，没导航到机器人/订单页 →
            # read 落在连通面板上读空、建单 action 也在连通面板上被遗留的连通✓蹭成「空判完成」（静默假
            # 成功）。两条不变量：
            #  A1 检测/读取之后、建单 action 之前要有 navigation（先到操作页：屏幕换走，read 读对页、
            #     action 不被上一步 stale ✓ 误判）。
            #  A2 不读「*列表」字段去挑实体（集合索引表达不了，规则8 只接力单个实体；要操作的表单能选就在
            #     action 里选）。
            seq = _flatten_runs(program.statements)

            def _conn_read(r):  # 连通检测相关步（导航/检测/读取，名字或读取字段含 连通/可达）
                return ("连通" in r.name or "可达" in r.name
                        or any("连通" in x or "可达" in x for x in r.returns))

            conn_idx = next((i for i, r in enumerate(seq) if _conn_read(r)), None)
            order_idx = next(
                (i for i, r in enumerate(seq)
                 if r.kind == "action" and any(k in r.name for k in ("订单", "建单", "下单"))),
                None,
            )
            if order_idx is None:
                details.append(f"没有建单 action（连通则建单）: {[(r.kind, r.name) for r in seq]}")
            elif conn_idx is None:
                details.append("没有连通检测 read（无法判断建单前是否换页）")
            elif not any(r.kind == "navigation" for r in seq[conn_idx + 1:order_idx]):
                details.append(
                    "连通检测后、建单 action 前缺 navigation（建单会落在连通面板上读空/被 stale✓ 误判完成，"
                    f"回归 211634）: {[(r.kind, r.name) for r in seq]}"
                )
            list_reads = [
                (r.name, r.returns) for r in seq
                if r.kind == "read" and any("列表" in f for f in r.returns)
            ]
            if list_reads:
                details.append(
                    f"read 了「*列表」字段去挑实体（集合索引表达不了，规则8 只接力单个实体）: {list_reads}"
                )
        elif assertion == "order_action_has_confirm_read":
            # 当前 Hard+ 任务（20260616_092555）已证明：连通后建单是一个长表单 action，
            # 但提交后的成败仍必须由紧跟的 read 读取结构化结果，不能只信 action 完成。
            # 判据保持结构化：订单/建单/下单 action 后面紧跟 read。
            ok = False

            def _walk(stmts):
                nonlocal ok
                for i, s in enumerate(stmts):
                    if isinstance(s, Run):
                        nxt = stmts[i + 1] if i + 1 < len(stmts) else None
                        if (
                            s.kind == "action"
                            and any(k in s.name for k in ("订单", "建单", "下单"))
                            and isinstance(nxt, Run)
                            and nxt.kind == "read"
                        ):
                            ok = True
                    elif isinstance(s, If):
                        _walk(s.then)
                        _walk(s.otherwise)
            _walk(program.statements)
            if not ok:
                details.append(
                    "建单/订单 action 后缺少紧跟的 read 确认结果（应 action 提交，read 读取订单状态/创建结果）"
                )
        elif assertion == "condition_uses_empty":
            if not any(s.cond.cmp == "empty" for s in _flatten_ifs(program.statements)):
                details.append(
                    f"未使用 empty 条件判断空字段: "
                    f"{[(s.cond.var, s.cond.field, s.cond.cmp, s.cond.value, s.cond.values) for s in _flatten_ifs(program.statements)]}"
                )
        elif assertion == "condition_uses_contains":
            if not any(s.cond.cmp == "contains" and s.cond.value.strip() for s in _flatten_ifs(program.statements)):
                details.append(
                    f"未使用 contains 条件判断子串: "
                    f"{[(s.cond.var, s.cond.field, s.cond.cmp, s.cond.value, s.cond.values) for s in _flatten_ifs(program.statements)]}"
                )
        elif assertion == "condition_uses_in_values":
            if not any(s.cond.cmp == "in" and s.cond.values for s in _flatten_ifs(program.statements)):
                details.append(
                    f"未使用 in + cond_values 多候选条件: "
                    f"{[(s.cond.var, s.cond.field, s.cond.cmp, s.cond.value, s.cond.values) for s in _flatten_ifs(program.statements)]}"
                )
        else:
            details.append(f"unknown assertion: {assertion}")
    return details


def _load_case_knowledge(case: dict):
    platform = case.get("platform", "browser")
    app = case.get("knowledge_app") or case.get("site")
    if app:
        return load_knowledge_for_app(app, platform)
    return auto_discover_knowledge(case["goal"], platform)


def _case_program(case: dict):
    k = _load_case_knowledge(case)
    screenshot_path = case.get("screenshot")
    png_bytes = None
    if screenshot_path:
        png_bytes = (PROJECT_ROOT / screenshot_path).read_bytes()
    program = decompose(
        case["goal"],
        png_bytes=png_bytes,
        knowledge=k.navigation if k else "",
        current_url=case.get("current_url", ""),
        current_title=case.get("current_title", ""),
        current_site=case.get("current_site") or (k.app_name if k and case.get("use_knowledge_app_as_current_site") else ""),
    )
    if case.get("normalize"):
        program = normalize_precondition_gates(normalize_confirm_read_gates(program))
    return program


def _dump_program(program) -> None:
    for s in program.statements:
        if isinstance(s, Run):
            fields = f" returns={s.returns!r}" if s.returns else ""
            spec = f" read_spec={s.read_spec!r}" if s.read_spec else ""
            print(f"       [{s.kind}] {s.name}: {s.success_condition}{fields}{spec}")
        elif isinstance(s, If):
            print(
                f"       [if] {s.cond.var}[{s.cond.field}] {s.cond.cmp} "
                f"{s.cond.value!r} values={s.cond.values!r}"
            )
            for b in (*s.then, *s.otherwise):
                nm = getattr(b, "name", None) or getattr(b, "message", "")
                print(f"          └ [{getattr(b, 'kind', b.op)}] {nm}")
        elif isinstance(s, Finish):
            print(f"       [finish] {s.message}")


def run_orchestrator_decompose_eval(label_filter: str = "", show_program: bool = False) -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    for c in cases:
        if label_filter and label_filter.lower() not in c["label"].lower():
            continue
        try:
            program = _case_program(c)
        except Exception as e:  # noqa: BLE001
            _report(c["label"], False, f"exception: {e}")
            continue

        details = _check_basic(program, c["expected"])
        details.extend(_check_assertions(program, c.get("assertions", [])))
        ok = len(details) == 0
        _report(c["label"], ok, "; ".join(details) if details else "")
        if show_program or not ok:
            _dump_program(program)


def test_orchestrator_decompose() -> None:
    run_orchestrator_decompose_eval()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="", help="Only run cases whose label contains this text.")
    parser.add_argument("--show-program", action="store_true", help="Print each generated Program.")
    args = parser.parse_args()

    print("── Browser Orchestrator-Decompose Eval ──")
    print("  测的是 decompose() 原始产出 = prompt(L1) 质量。部分断言有生产兜底，FAIL=prompt 可更好（软信号）非生产 bug：")
    print("    · confirm-read 的 dispatch 门 → engine.normalize_confirm_read_gates(L2) 确定性兜底；")
    print("    · 登录前置（auth_milestone_terminal_state）→ per-app _check.md 的登录判据、checker 权威兜底。")
    print("  若 case 设置 normalize=true，则额外验证生产 normalizer 后的可执行形态。")
    print("  连通门控/读了就引用 等无兜底的断言，FAIL 才是真问题。")
    run_orchestrator_decompose_eval(label_filter=args.label, show_program=args.show_program)
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
