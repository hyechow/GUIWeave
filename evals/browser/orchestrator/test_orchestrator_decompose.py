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
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)  # @<file> refs / knowledge dir resolve relative to repo root

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.core.orchestrator import Compute, Finish, ForEach, If, Query, Read, Run, RunLike, decompose
from gui_agent.core.orchestrator.passes import normalize_confirm_read_gates, normalize_precondition_gates
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
        if isinstance(s, RunLike):
            out.append(s)
        elif isinstance(s, If):
            out.extend(_flatten_runs(s.then))
            out.extend(_flatten_runs(s.otherwise))
        elif isinstance(s, ForEach):
            out.extend(_flatten_runs(s.body))
    return out


def _flatten_computes(stmts: list) -> list[Compute]:
    out: list[Compute] = []
    for s in stmts:
        if isinstance(s, Compute):
            out.append(s)
        elif isinstance(s, If):
            out.extend(_flatten_computes(s.then))
            out.extend(_flatten_computes(s.otherwise))
        elif isinstance(s, ForEach):
            out.extend(_flatten_computes(s.body))
    return out


def _foreach_body_goals(stmts: list) -> list[str]:
    """All ForEach.body_goal strings anywhere — the per-row sub-goal re-decomposed at runtime (so its
    read→compute→fill is expressed as text here, not explicit Run/Compute nodes)."""
    out: list[str] = []
    for s in stmts:
        if isinstance(s, ForEach):
            if getattr(s, "body_goal", ""):
                out.append(s.body_goal)
            out.extend(_foreach_body_goals(s.body))
        elif isinstance(s, If):
            out.extend(_foreach_body_goals(s.then))
            out.extend(_foreach_body_goals(s.otherwise))
    return out


def _has_finish(stmts: list) -> bool:
    for s in stmts:
        if isinstance(s, Finish):
            return True
        if isinstance(s, If) and (_has_finish(s.then) or _has_finish(s.otherwise)):
            return True
    return False


def _flatten_finishes(stmts: list) -> list[Finish]:
    out: list[Finish] = []
    for s in stmts:
        if isinstance(s, Finish):
            out.append(s)
        elif isinstance(s, If):
            out.extend(_flatten_finishes(s.then))
            out.extend(_flatten_finishes(s.otherwise))
    return out


def _flatten_ifs(stmts: list) -> list[If]:
    out: list[If] = []
    for s in stmts:
        if isinstance(s, If):
            out.append(s)
            out.extend(_flatten_ifs(s.then))
            out.extend(_flatten_ifs(s.otherwise))
    return out


def _has_foreach(stmts: list) -> bool:
    for s in stmts:
        if isinstance(s, ForEach):
            return True
        if isinstance(s, If) and (_has_foreach(s.then) or _has_foreach(s.otherwise)):
            return True
    return False


def _flatten_foreaches(stmts: list) -> list[ForEach]:
    out: list[ForEach] = []
    for s in stmts:
        if isinstance(s, ForEach):
            out.append(s)
            out.extend(_flatten_foreaches(s.body))
        elif isinstance(s, If):
            out.extend(_flatten_foreaches(s.then))
            out.extend(_flatten_foreaches(s.otherwise))
    return out


def _sql_identifier(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_")
    if not text:
        return ""
    if text[0].isdigit():
        text = "c_" + text
    return text


def _sql_has_quoted_display_identifier(sql: str) -> bool:
    for pattern in (r'"([^"]+)"', r"`([^`]+)`", r"\[([^\]]+)\]"):
        for raw in re.findall(pattern, sql or ""):
            text = str(raw or "").strip()
            if text and _sql_identifier(text) != text:
                return True
    return False


def _confirm_read_actions(stmts: list) -> list[Run]:
    """Action Runs whose result is structurally read.

    New plans put returns/read_spec on the action itself. Legacy plans may still have
    action -> scalar read; normalize_confirm_read_gates folds those into the action.
    """
    out: list[Run] = []
    for i, s in enumerate(stmts):
        if isinstance(s, RunLike) and s.kind == "action":
            nxt = stmts[i + 1] if i + 1 < len(stmts) else None
            if s.returns or (isinstance(nxt, RunLike) and nxt.kind == "read"):
                out.append(s)
        elif isinstance(s, If):
            out.extend(_confirm_read_actions(s.then))
            out.extend(_confirm_read_actions(s.otherwise))
    return out


def _adjacent_run_pairs(stmts: list) -> list[tuple[Run, Run]]:
    """Adjacent Run pairs within each statement list, recursing into if branches."""
    out: list[tuple[Run, Run]] = []
    for i, s in enumerate(stmts):
        if isinstance(s, RunLike):
            nxt = stmts[i + 1] if i + 1 < len(stmts) else None
            if isinstance(nxt, RunLike):
                out.append((s, nxt))
        elif isinstance(s, If):
            out.extend(_adjacent_run_pairs(s.then))
            out.extend(_adjacent_run_pairs(s.otherwise))
    return out


def _check_basic(program, expected: dict) -> list[str]:
    details: list[str] = []
    runs = _flatten_runs(program.statements)
    reads = [r for r in runs if r.kind == "read"]
    result_runs = [r for r in runs if r.returns or r.kind == "data_query"]
    if "min_statements" in expected and len(program.statements) < expected["min_statements"]:
        details.append(f"expected >={expected['min_statements']} top-level steps, got {len(program.statements)}")
    if expected.get("has_read") and not result_runs:
        details.append("缺少返回值读取（无带 returns 的步骤或只读结果提取步）")
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
    # 结果让位给结构化返回值读取
    "由下一步", "由后续", "下一步读取", "下一步判", "read 判", "不判定结果",
    "不判具体", "具体结果由", "成败由", "结果由", "返回值", "完成帧",
)


def _check_assertions(program, assertions: list[str]) -> list[str]:
    details: list[str] = []
    runs = _flatten_runs(program.statements)
    cr_actions = _confirm_read_actions(program.statements)

    for assertion in assertions:
        if assertion == "key_action_has_confirm_read":
            # 规则8：会改状态/出结果的关键动作必须有结构化返回值确认，优先是 action 自带 returns。
            if not cr_actions:
                details.append(
                    f"无 action 返回值读取（也没有兼容的 action→read）: "
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
        elif assertion == "current_context_no_login_action":
            # Production-like path: router/decompose already knows the current browser tab is
            # inside the target app. In that context, "登录" should be an ensure-state
            # precondition or skipped if already on the target page, not a fresh credential
            # submission action. This catches variants that avoid the word 登录 but still say
            # 输入账号/密码/admin.
            offenders = []
            for r in runs:
                if r.kind != "action":
                    continue
                text = f"{r.name} {r.success_condition}".lower()
                if any(k in text for k in ("登录", "登入", "登陆", "认证", "账号", "密码", "admin", "password")):
                    offenders.append((r.name, r.success_condition))
            if offenders:
                details.append(
                    "已知当前站点/页面上下文时不应重新执行登录 action，应使用 precondition 或直接进入目标页: "
                    f"{offenders}"
                )
        elif assertion == "read_has_spec":
            # 返回值读取没判读说明就只能瞎猜（见 structured_read / prompt 规则）。每个结果读取都要有
            # returns + 非空 read_spec。
            bad = [
                (r.name, r.returns) for r in runs
                if (
                    (r.kind == "read" and not r.returns)
                    or (r.kind != "data_query" and r.returns and not r.read_spec.strip())
                )
            ]
            if bad:
                details.append(f"返回值读取缺 returns 或 read_spec（判读说明）: {bad}")
        elif assertion == "shopping_admin_review_count_uses_action_read":
            # WebArena shopping_admin #15/#11 style: query a Magento admin grid, then report the
            # authoritative count. Applying a Review/search filter is only a trigger; the count
            # must be read from the grid summary such as "N records found". New plans attach that
            # read as returns on the trigger action; legacy filter/action→read pairs are normalized.
            def _looks_like_count_read(r: Run) -> bool:
                text = " ".join([r.name, r.read_spec, *r.returns]).lower()
                return any(
                    marker in text
                    for marker in (
                        "record", "records found", "count", "total", "review",
                        "评论", "评价", "记录数", "数量", "总数",
                    )
                )

            action_reads = [
                r for r in runs
                if r.kind == "action" and r.returns and _looks_like_count_read(r)
            ]
            legacy_pairs = [
                (a, b) for a, b in _adjacent_run_pairs(program.statements)
                if b.kind == "read" and _looks_like_count_read(b)
            ]
            filter_pairs = [(a, b) for a, b in legacy_pairs if a.kind == "filter"]
            if not action_reads and not legacy_pairs:
                details.append(
                    "没有找到计数返回值读取（应先筛选/搜索，并在该 action returns 中读取 N records found/评论总数）"
                )
            elif not action_reads and not any(a.kind == "action" for a, _ in legacy_pairs):
                details.append(
                    f"计数读取不是 action 触发器返回值: "
                    f"{[(a.kind, a.name, b.name) for a, b in legacy_pairs]}"
                )
            if filter_pairs:
                details.append(
                    f"计数读取仍是 filter→read，说明筛选结果还会被 filter checker 重判: "
                    f"{[(a.name, b.name) for a, b in filter_pairs]}"
                )
            bad_gates = [
                (a.name, a.success_condition) for a in action_reads
                if not any(m in a.success_condition for m in _DISPATCH_DEFER_MARKERS)
            ]
            if bad_gates:
                details.append(
                    f"计数返回值 action 不是 dispatch/defer 门（筛选成败应由返回值判定）: {bad_gates}"
                )
            action_text = " ".join(a.name.lower() for a in action_reads)
            if action_reads and not any(k in action_text for k in ("best", "review", "评论", "评价", "筛选", "搜索", "filter", "search")):
                details.append(
                    f"计数返回值 action 看不出是在按 review/best 搜索筛选: {[a.name for a in action_reads]}"
                )
        elif assertion == "action_targets_read_entity":
            # result-then-reference（规则10，回归 20260615_163258）。不验「出现了某种 {var[字段]} 形态」
            # （那只是 prompt 样式），而验整条**顺序不变量**：
            #   action(创建/识别实体并返回标识，绑定 var) → 之后的 action 用同一 {var[字段]} 引用。
            # 163258 侥幸做对仅因列表只有一台、planner 从屏幕拿到了名字；有同类兄弟就指错。这里要求三段
            # 按程序顺序齐备：产生该实体的 action 有返回值、之后有 action 用同一 var 引用其 returns 字段。
            seq = _flatten_runs(program.statements)  # DFS = 程序顺序（本 case 线性，无分支）
            ok = False
            for i, producer in enumerate(seq):
                if not producer.var or not producer.returns:
                    continue
                fields = set(producer.returns)
                has_action_producer = producer.kind == "action" or any(a.kind == "action" for a in seq[:i])
                later_ref = any(
                    a.kind == "action" and any(
                        m.group(1) == producer.var and m.group(2).strip().strip("'\"") in fields
                        for m in TEMPLATE_RE.finditer(a.name)
                    )
                    for a in seq[i + 1:]
                )
                if has_action_producer and later_ref:
                    ok = True
                    break
            if not ok:
                details.append(
                    "缺少 result-then-reference 顺序结构：应为 action(创建/识别实体并返回标识,var) → "
                    "之后的 action 用同一 {var[字段]} 引用（系统生成名称要作为返回值接力，别裸名词/赌列表只有一个）: "
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

            conn_vars = {r.var for r in seq if r.var and r.returns and _conn(r)}
            gated: list = []

            def _walk_gate(stmts, under):
                for s in stmts:
                    if isinstance(s, RunLike):
                        if _order(s) and under:
                            gated.append(s)
                    elif isinstance(s, If):
                        deeper = under or s.cond.var in conn_vars
                        _walk_gate(s.then, deeper)
                        _walk_gate(s.otherwise, deeper)
            _walk_gate(program.statements, False)
            top_orders = [s for s in program.statements if isinstance(s, RunLike) and _order(s)]

            if not conn_vars:
                details.append("没有检测连通的返回值步骤（应先产生连通检测 returns，再据此 if 分支）")
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
            #  A2 不读「*列表」字段去挑实体（集合索引表达不了，规则10 只接力单个实体；要操作的表单能选就在
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
                    f"read 了「*列表」字段去挑实体（集合索引表达不了，规则10 只接力单个实体）: {list_reads}"
                )
        elif assertion == "order_action_has_confirm_read":
            # 当前 Hard+ 任务（20260616_092555）已证明：连通后建单是一个长表单 action，
            # 但提交后的成败仍必须由紧跟的 read 读取结构化结果，不能只信 action 完成。
            # 判据保持结构化：订单/建单/下单 action 后面紧跟 read。
            ok = False

            def _walk(stmts):
                nonlocal ok
                for i, s in enumerate(stmts):
                    if isinstance(s, RunLike):
                        nxt = stmts[i + 1] if i + 1 < len(stmts) else None
                        if (
                            s.kind == "action"
                            and any(k in s.name for k in ("订单", "建单", "下单"))
                            and isinstance(nxt, RunLike)
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
        elif assertion == "shopping_admin_top_terms_locates_before_read":
            # 回归 WebArena task 42：带 dashboard 截图时，decomposer 曾被页面上可见的
            # 'Last Search Terms' 摘要小部件误导成裸 read（无前置 navigation）→ 读到按时间的 widget
            # （≠ 任务要的按热度 Top）。read 是单帧读取，不负责滚动/找目标；若目标在当前页但
            # 当前视口未见，应先 navigation=滚动/定位到 Top Search Terms 区块；若目标在完整报表页，
            # 应先 navigation 到 Search Terms Report。两种都接受，但裸 read 不接受。
            seq = _flatten_runs(program.statements)
            first_result = next((i for i, r in enumerate(seq) if r.returns), None)
            if first_result is None:
                details.append("没有返回值读取（应读取前 2 个搜索词）")
            elif seq[first_result].kind != "navigation" and not any(r.kind == "navigation" for r in seq[:first_result]):
                details.append(
                    "返回值读取前缺 navigation：目标若在当前页下方，必须先滚动/定位到 Top Search Terms；"
                    "若使用完整数据源，必须先进入 Search Terms Report。run_kinds="
                    f"{[(r.kind, r.name) for r in seq]}"
                )
        elif assertion == "shopping_admin_top_terms_does_not_read_last_terms":
            seq = _flatten_runs(program.statements)
            bad_reads = []
            for r in seq:
                if not r.returns:
                    continue
                text = " ".join([r.name, r.read_spec, *r.returns]).lower()
                if "last search terms" in text:
                    bad_reads.append((r.name, r.returns, r.read_spec))
            if bad_reads:
                details.append(
                    "读取目标指向了 Last Search Terms（按时间/最近），但任务要 Top Search Terms（按热度/前 N）: "
                    f"{bad_reads}"
                )
        elif assertion == "shopping_admin_top_terms_not_unsorted_report":
            seq = _flatten_runs(program.statements)
            first_read = next((i for i, r in enumerate(seq) if r.returns), None)
            if first_read is not None:
                read = seq[first_read]
                read_text = " ".join([read.name, read.read_spec, *read.returns]).lower()
                prior_text = " ".join(
                    f"{r.kind} {r.name} {r.success_condition}".lower()
                    for r in seq[:first_read]
                )
                uses_metric = any(m in prior_text + " " + read_text for m in (
                    "uses", "count", "使用", "用量", "次数", "热度",
                ))
                explicit_sort = any(m in prior_text for m in (
                    "sort", "sorted", "descending", "desc", "排序", "降序", "从高到低",
                ))
                exact_dashboard_top = "top search terms" in prior_text + " " + read_text
                reads_report_rows = (
                    "search terms report" in read_text
                    or ("report" in read_text and "search term" in read_text)
                    or ("报表" in read_text and "搜索词" in read_text)
                )
                if reads_report_rows and not exact_dashboard_top and not (uses_metric and explicit_sort):
                    details.append(
                        "使用 Search Terms Report 回答 Top/前 N 时，缺少按 Uses/Count 降序排序的前置步骤；"
                        "不能读取报表默认前两行当作 Top。"
                        f" seq={[(r.kind, r.name, r.success_condition) for r in seq]}"
                    )
        elif assertion == "shopping_admin_order_count_uses_orders_grid_source":
            # WebArena shopping_admin order-count email tasks (#62/#63/#64): the source must
            # be raw order rows, because the final output is Customer Email and the filter may
            # be order Status. Customer Reports lack email, and Customers grid cannot be assumed
            # to expose a reliable Total Orders column.
            seq = _flatten_runs(program.statements)
            text = " ".join(
                f"{r.kind} {r.name} {r.success_condition} {r.read_spec} {getattr(r, 'sql', '')}"
                for r in seq
            ).lower()
            orders_source = any(marker in text for marker in (
                "sales > orders",
                "sales -> orders",
                "sales/orders",
                "orders grid",
                "orders list",
                "orders 页面",
                "订单列表",
                "销售订单",
            ))
            if not orders_source:
                details.append(
                    "订单数聚合取 customer email(s) 应以 Sales > Orders / Orders grid 原始订单行为主源，"
                    f"当前未看到 Orders grid 源: {[(r.kind, r.name) for r in seq]}"
                )
            bad_source = any(marker in text for marker in (
                "order count report",
                "order total report",
                "customer reports",
                "reports > customers",
                "customer_reports",
                "客户报表",
                "total orders column",
                "all customers",
                "customers grid",
                "客户列表",
            ))
            if bad_source:
                details.append(
                    "订单数聚合不应把 Customer Reports 或 Customers grid/Total Orders 当主源；"
                    "这些源缺 Customer Email 或总订单数不可靠。"
                    f" seq={[(r.kind, r.name) for r in seq]}"
                )
        elif assertion == "shopping_admin_order_count_uses_data_query":
            seq = _flatten_runs(program.statements)
            if not any(r.kind == "data_query" for r in seq):
                details.append(
                    "订单数聚合应在采集/导出完整 Orders rows 后用 data_query 做 group/count/rank/tie，"
                    f"当前没有 data_query: {[(r.kind, r.name) for r in seq]}"
                )
        elif assertion == "data_query_sql_no_schema_mapping_text":
            seq = _flatten_runs(program.statements)
            offenders = [
                (r.name, getattr(r, 'sql', '')) for r in seq
                if r.kind == "data_query" and (
                    "->" in (getattr(r, 'sql', '') or "")
                    or _sql_has_quoted_display_identifier(getattr(r, 'sql', '') or "")
                )
            ]
            if offenders:
                details.append(
                    "data_query SQL 只能使用实际 normalized column identifiers，"
                    "不能把 schema 展示里的 Header->column 映射文本或带空格/标点的 quoted UI 表头写进 SQL: "
                    f"{offenders}"
                )
        elif assertion == "data_query_sql_no_template_refs":
            seq = _flatten_runs(program.statements)
            offenders = [
                (r.name, getattr(r, 'sql', '')) for r in seq
                if r.kind == "data_query" and re.search(r"\{[^{}]+\}", getattr(r, 'sql', '') or "")
            ]
            if offenders:
                details.append(
                    "data_query SQL 不是模板面，不能包含 {变量[字段]} 或任何 {...}；"
                    "若要做差值/比例/合计，应先把相关行集 materialize 成表，再在 SQL/CTE 内基于表列计算。"
                    f" offenders={offenders}"
                )
        elif assertion == "shopping_admin_completed_order_count_filters_complete":
            seq = _flatten_runs(program.statements)
            text = " ".join(
                f"{r.kind} {r.name} {r.success_condition} {r.read_spec} {getattr(r, 'sql', '')}"
                for r in seq
            ).lower()
            if "status" not in text or "complete" not in text:
                details.append(
                    "completed order-count 任务必须筛选/查询 Status = Complete 后再统计，"
                    f"当前未看到 complete status 过滤: {[(r.kind, r.name) for r in seq]}"
                )
        elif assertion == "shopping_admin_completed_order_count_clears_unrequested_filters":
            seq = _flatten_runs(program.statements)
            first_data_query = next((i for i, r in enumerate(seq) if r.kind == "data_query"), len(seq))
            prior = seq[:first_data_query]
            clear_step = False
            status_step = False
            for r in prior:
                text = f"{r.kind} {r.name} {r.success_condition} {r.read_spec}".lower()
                if "status" in text and "complete" in text:
                    status_step = True
                mentions_filters = any(marker in text for marker in (
                    "active filter",
                    "active filters",
                    "filter",
                    "filters",
                    "筛选",
                    "过滤",
                ))
                clears = any(marker in text for marker in (
                    "clear all",
                    "clear filters",
                    "clear existing",
                    "clear inherited",
                    "no unrequested",
                    "only status",
                    "只保留",
                    "清除",
                    "清空",
                    "无关筛选",
                    "旧筛选",
                ))
                if mentions_filters and clears:
                    clear_step = True
            if not (clear_step and status_step):
                details.append(
                    "completed entire-history order-count 任务必须先清除继承的无关 Active filters，"
                    "再只应用 Status=Complete；否则可能沿用上一任务的 Purchase Date 范围。"
                    f" seq={[(r.kind, r.name, r.success_condition) for r in prior]}"
                )
        elif assertion == "shopping_admin_any_state_order_count_no_complete_filter":
            seq = _flatten_runs(program.statements)
            text = " ".join(
                f"{r.kind} {r.name} {r.success_condition} {r.read_spec} {getattr(r, 'sql', '')}"
                for r in seq
            ).lower()
            if "status = complete" in text or "status='complete'" in text or "status=\"complete\"" in text:
                details.append(
                    "any-state order-count 任务不应筛选 Status = Complete，应统计所有状态订单，"
                    f"当前出现 complete status 过滤: {[(r.kind, r.name) for r in seq]}"
                )
        elif assertion == "shopping_admin_any_state_order_count_clears_active_filters":
            seq = _flatten_runs(program.statements)
            clear_step = False
            for r in seq:
                text = f"{r.kind} {r.name} {r.success_condition} {r.read_spec}".lower()
                if not any(marker in text for marker in (
                    "active filter",
                    "active filters",
                    "clear all",
                    "clear filters",
                    "清除",
                    "清空",
                    "筛选",
                    "过滤",
                )):
                    continue
                clears = any(marker in text for marker in (
                    "clear all",
                    "clear filters",
                    "no active filters",
                    "没有 active filters",
                    "无 active filters",
                    "没有筛选",
                    "无筛选",
                    "清除",
                    "清空",
                ))
                if clears:
                    clear_step = True
                    break
            if not clear_step:
                details.append(
                    "any-state order-count 任务必须先确保 Orders grid 没有继承的 Active filters"
                    "（例如上一任务留下的 Status: Complete）；应有 Clear all/清除筛选步骤。"
                    f" seq={[(r.kind, r.name, r.success_condition) for r in seq]}"
                )
        elif assertion == "shopping_admin_monthly_orders_filters_page_first":
            seq = _flatten_runs(program.statements)
            first_data_query = next((i for i, r in enumerate(seq) if r.kind == "data_query"), None)
            if first_data_query is None:
                details.append(
                    "monthly completed-order count 应先筛选 Orders grid，再用 data_query 按月聚合；当前无 data_query。"
                )
                continue
            prior = seq[:first_data_query]
            filter_text = " ".join(
                f"{r.kind} {r.name} {r.success_condition} {r.read_spec}".lower()
                for r in prior
                if r.kind in {"filter", "action"}
            )
            has_complete = "status" in filter_text and "complete" in filter_text
            has_purchase_date = (
                ("purchase date" in filter_text or "created_at" in filter_text or "date" in filter_text or "日期" in filter_text)
                and any(marker in filter_text for marker in ("from", "to", "range", "范围", "起", "止", "到", "至"))
            )
            has_us_dates = any(marker in filter_text for marker in (
                "01/01/2023", "1/1/2023", "05/31/2023", "5/31/2023",
            ))
            if not (has_complete and has_purchase_date and has_us_dates):
                details.append(
                    "monthly completed-order count 必须在 data_query 前通过页面 Filters 应用 "
                    "Status=Complete 和 Purchase Date 01/01/2023-05/31/2023；"
                    f"当前前置筛选不足: {[(r.kind, r.name, r.success_condition) for r in prior]}"
                )
        elif assertion == "shopping_admin_monthly_orders_returns_result_objects":
            seq = _flatten_runs(program.statements)
            data_queries = [r for r in seq if r.kind == "data_query"]
            if not data_queries:
                details.append("monthly completed-order count 缺少 data_query 聚合步骤。")
                continue
            offenders = []
            for r in data_queries:
                sql = (getattr(r, 'sql', '') or "").lower()
                returns = [x.lower() for x in (r.returns or [])]
                has_month_alias = bool(re.search(r"\bas\s+month\b", sql))
                has_count_alias = bool(re.search(r"\bas\s+count\b", sql))
                has_month_names = all(name in sql for name in ("january", "february", "march", "april", "may"))
                if returns != ["result"] or not has_month_alias or not has_count_alias or not has_month_names:
                    offenders.append((r.name, r.returns, getattr(r, 'sql', '')))
            if offenders:
                details.append(
                    "最终要求 JSON 对象数组时，data_query 应 SELECT month/count 列并用 returns=['result']；"
                    "month 列必须输出 January/February/... 月名，不能输出 month_num/月份数字。"
                    "finish 直接引用 {q[result]}；不要拆成 {q[month]}/{q[count]}。"
                    f" offenders={offenders}"
                )
        elif assertion == "shopping_admin_monthly_orders_query_uses_filtered_rows":
            seq = _flatten_runs(program.statements)
            offenders = []
            for r in seq:
                if r.kind != "data_query":
                    continue
                sql = (getattr(r, 'sql', '') or "").lower()
                repeats_page_filter = (
                    re.search(r"\bwhere\b.*\bstatus\b", sql, flags=re.DOTALL)
                    or "created_at >=" in sql
                    or "created_at <=" in sql
                    or "between" in sql and "created_at" in sql
                )
                if repeats_page_filter:
                    offenders.append((r.name, getattr(r, 'sql', '')))
            if offenders:
                details.append(
                    "页面 Filters 已经应用 Status=Complete 和 Purchase Date 范围后，data_query 应只对已筛选行做"
                    "按月 group/count，不要再重复 status/date WHERE；否则容易把 UI 大小写/日期格式误用于 provider 字段。"
                    f" offenders={offenders}"
                )
        elif assertion == "product_lookup_uses_product_field":
            # Given a product-like constraint, any UI filtering/searching step that uses the
            # product term must keep that term bound to the PRODUCT field/column. Do not force a
            # particular exact/fuzzy strategy here; that is a runtime decision based on feedback.
            filters = [r for r in _flatten_runs(program.statements) if r.kind in ("filter", "action")]
            both = lambda r: f"{r.name} {r.success_condition}"
            prod = [r for r in filters if re.search(r"产品|Product", both(r)) and "Olivia" in both(r)]
            if not prod:
                details.append(
                    "没有『按 Product/产品 列检索 Olivia』的 milestone（疑似筛错列，如填进 Review 文本列）："
                    f"{[(r.kind, r.name) for r in filters]}"
                )
            product_field_re = re.compile(r"Product|产品(?:名|列|字段|筛选框|搜索框|输入框|filter)", re.I)
            product_terms = ("Olivia", "Olivia zip jacket")
            product_term_without_product_field = [
                (r.kind, r.name, r.success_condition)
                for r in filters
                if any(term in both(r) for term in product_terms)
                if not product_field_re.search(both(r))
            ]
            if product_term_without_product_field:
                details.append(
                    "使用产品词筛选/搜索时必须继续点名 Product/产品字段或列；"
                    "不能退化成泛搜索，否则 planner 容易填到 Review/Title/Nickname 等错误字段。"
                    f" offenders={product_term_without_product_field}"
                )
        elif assertion == "uses_foreach_iteration":
            if not _has_foreach(program.statements):
                details.append(
                    "需要对运行时发现的候选集合逐条处理，但程序没有 foreach；"
                    f"seq={[(r.kind, r.name, r.returns) for r in runs]}"
                )
        elif assertion == "shopping_admin_review_rating_initial_uses_reviews_source":
            # Regression for task 113 live run 20260624_171049: initial decompose chose
            # Catalog > Products, searched the product, and planned to read a product detail page.
            # The requested answer is a field of review records filtered by product/rating, so the
            # initial source must be a Reviews/All Reviews collection, with the product as a filter.
            seq = _flatten_runs(program.statements)
            texts = [f"{r.kind} {r.name} {r.success_condition} {r.read_spec}" for r in seq]
            all_text = " ".join(texts).lower()
            review_source = any(
                re.search(
                    r"all reviews|review[s]? list|reviews 数据源|reviews? grid|"
                    r"marketing\s*>.*reviews|user content.*reviews|评论列表|评价列表|评论数据源",
                    text,
                    flags=re.I,
                )
                for text in texts
            )
            if not review_source:
                details.append(
                    "产品评论评分任务的初始编排应以 Reviews/All Reviews 评论集合为主数据源，"
                    "Product 只是筛选条件；当前没有看到评论集合数据源。"
                    f" seq={[(r.kind, r.name) for r in seq]}"
                )
            product_dead_end = (
                re.search(r"catalog\s*>?\s*products|products?\s+list|产品列表|商品列表", all_text, flags=re.I)
                and re.search(r"产品详情|商品详情|product detail|product workspace|点击搜索结果", all_text, flags=re.I)
                and not review_source
            )
            if product_dead_end:
                details.append(
                    "不应先进入 Products List 搜商品详情再读取评论；这会把 product 实体误当主目标，"
                    "而不是在 Reviews 行集合上按 Product 筛选。"
                )
        elif assertion == "shopping_admin_review_rating_reorch_drills_detail":
            # 回归 WebArena task 113 / 20260622_142614：第一次 data_query 已因当前 Reviews
            # grid 为 partial 且缺 rating 字段失败；随后 Feasibility 又确认列表页不存在 Rating
            # 筛选控件。重编排的唯一可行路线是逐条打开 Review Detail，读取实际 Detailed
            # Rating + Nickname 后本地筛出 <=3。不能再编出「列表页设置 Rating<=3」或在
            # 当前列表 data_query 里写 WHERE rating <= 3。
            seq = _flatten_runs(program.statements)
            all_text = " ".join(
                f"{r.kind} {r.name} {r.success_condition} {r.read_spec} {getattr(r, 'sql', '')} "
                f"{' '.join(r.returns or [])}"
                for r in seq
            )
            has_detail_route = any(
                marker in all_text
                for marker in ("Review Detail", "评论详情", "详情", "Edit", "编辑", "逐条", "每条")
            )
            reads_rating = any(
                r.returns
                and any(marker in f"{r.name} {r.read_spec} {' '.join(r.returns or [])}"
                        for marker in ("Rating", "rating", "评分", "Detailed Rating"))
                for r in seq
            )
            reads_nickname = any(
                r.returns
                and any(marker in f"{r.name} {r.read_spec} {' '.join(r.returns or [])}"
                        for marker in ("Nickname", "nickname", "昵称", "customer"))
                for r in seq
            )
            if not has_detail_route:
                details.append(
                    "重编排未体现逐条打开 Review Detail/评论详情 的取数路线；"
                    f"seq={[(r.kind, r.name) for r in seq]}"
                )
            if not reads_rating:
                details.append(
                    "重编排缺少从评论详情返回实际 Rating/评分 的步骤；"
                    f"reads={[(r.name, r.returns, r.read_spec) for r in seq if r.returns]}"
                )
            if not reads_nickname:
                details.append(
                    "重编排缺少返回 Nickname/昵称 的步骤；"
                    f"reads={[(r.name, r.returns, r.read_spec) for r in seq if r.returns]}"
                )
            foreach_tables = {
                (s.into or f"{s.var}s").lower()
                for s in _flatten_foreaches(program.statements)
            }
            bad_sql = []
            for r in seq:
                if r.kind != "data_query" or not re.search(r"\brating\b|\b评分\b", getattr(r, 'sql', '') or "", flags=re.I):
                    continue
                refs = {
                    raw.lower()
                    for raw in re.findall(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)\b", getattr(r, 'sql', '') or "", flags=re.I)
                }
                if not (refs & foreach_tables):
                    bad_sql.append((r.name, getattr(r, 'sql', '')))
            if bad_sql:
                details.append(
                    "当前列表 schema 无 Rating 列；只有先 foreach 产出详情 into 表后，data_query 才能使用 rating 字段；"
                    f"bad_sql={bad_sql}"
                )
            bad_filters = []
            for r in seq:
                if r.kind not in {"filter", "action"}:
                    continue
                text = f"{r.name} {r.success_condition}"
                mentions_rating_filter = (
                    any(marker in text for marker in ("Rating", "rating", "评分"))
                    and any(marker in text for marker in ("筛选", "过滤", "filter", "<=3", "≤3", "3星", "3 星"))
                )
                mentions_detail = any(marker in text for marker in ("Review Detail", "评论详情", "详情", "Edit", "编辑", "逐条", "每条"))
                if mentions_rating_filter and not mentions_detail:
                    bad_filters.append((r.kind, r.name, r.success_condition))
            if bad_filters:
                details.append(
                    "重编排仍在列表层尝试不可行的 Rating<=3 筛选；应钻取详情读取评分。"
                    f" bad_filters={bad_filters}"
                )
            bad_index_refs = []
            for r in seq:
                for attr in ("name", "success_condition", "read_spec"):
                    text = getattr(r, attr, "") or ""
                    if re.search(r"\{\w+\[[^\]]+\]\s*\[", text):
                        bad_index_refs.append((r.kind, r.name, attr, text))
            if bad_index_refs:
                details.append(
                    "DSL 模板只支持 {var[field]}，不支持 {var[field][0]} 这类列表索引；"
                    "逐条打开详情应直接操作可见行/Edit 链接，或读取单个标识后用 {var[field]} 接力。"
                    f" bad_index_refs={bad_index_refs}"
                )
            bad_templates = []

            def _walk_template_texts(stmts):
                for s in stmts:
                    if isinstance(s, RunLike):
                        for attr in ("name", "success_condition", "read_spec"):
                            yield f"{s.kind}:{s.name}:{attr}", getattr(s, attr, "") or ""
                    elif isinstance(s, If):
                        yield from _walk_template_texts(s.then)
                        yield from _walk_template_texts(s.otherwise)
                    elif isinstance(s, Finish):
                        yield "finish:message", s.message or ""

            for where, text in _walk_template_texts(program.statements):
                for raw in re.findall(r"\{[^{}]+\}", text):
                    if not re.fullmatch(r"\{\w+\[[^\[\]]+\]\}", raw):
                        bad_templates.append((where, raw, text))
            if bad_templates:
                details.append(
                    "重编排产物不得留下无法由 runner 替换的伪模板/裸变量；"
                    "最终答案必须引用真实 read/data_query 字段（如 {r1[nickname]}），或通过可执行分支生成。"
                    f" bad_templates={bad_templates}"
                )
        elif assertion == "shopping_admin_review_rating_drills_detail":
            seq = _flatten_runs(program.statements)
            all_text = " ".join(
                f"{r.kind} {r.name} {r.success_condition} {r.read_spec} {getattr(r, 'sql', '')} "
                f"{' '.join(r.returns or [])}"
                for r in seq
            )
            has_detail_route = any(
                marker in all_text
                for marker in ("Review Detail", "评论详情", "详情", "Edit", "编辑", "逐条", "每条")
            )
            reads_rating = any(
                r.returns
                and any(
                    marker in f"{r.name} {r.read_spec} {' '.join(r.returns or [])}"
                    for marker in ("Rating", "rating", "评分", "Detailed Rating")
                )
                for r in seq
            )
            if not has_detail_route:
                details.append(
                    "评论评分任务未体现逐条打开 Review Detail/评论详情 的取数路线；"
                    f"seq={[(r.kind, r.name) for r in seq]}"
                )
            if not reads_rating:
                details.append(
                    "评论评分任务缺少从评论详情返回实际 Rating/评分 的步骤；"
                    f"reads={[(r.name, r.returns, r.read_spec) for r in seq if r.returns]}"
                )
            foreach_tables = {
                (s.into or f"{s.var}s").lower()
                for s in _flatten_foreaches(program.statements)
            }
            bad_sql = []
            for r in seq:
                if r.kind != "data_query" or not re.search(r"\brating\b|\b评分\b", getattr(r, 'sql', '') or "", flags=re.I):
                    continue
                refs = {
                    raw.lower()
                    for raw in re.findall(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)\b", getattr(r, 'sql', '') or "", flags=re.I)
                }
                if not (refs & foreach_tables):
                    bad_sql.append((r.name, getattr(r, 'sql', '')))
            if bad_sql:
                details.append(
                    "Reviews 当前列表 schema 不保证有可查询 Rating 列；只有先 foreach 产出详情 into 表后，"
                    "data_query 才能使用 rating 字段；"
                    f"bad_sql={bad_sql}"
                )
        elif assertion == "shopping_admin_review_title_uses_summary":
            # Regression WebArena task 214 / live run 20260707_122624: the plan asked for
            # title+rating, but the detail read spec said "Review Title" and runtime selected
            # Nickname/page heading ("Roxie", "Edit Review") instead of the actual review title.
            # In Magento Review Detail, requested title is Summary of Review. A healthy plan can
            # either carry the grid Title/Summary column forward, or explicitly read Summary of
            # Review while drilling details for Detailed Rating.
            seq = _flatten_runs(program.statements)
            foreaches = _flatten_foreaches(program.statements)
            all_text = " ".join(
                f"{r.kind} {r.name} {r.success_condition} {r.read_spec} "
                f"{' '.join(r.returns or [])} {getattr(r, 'sql', '')}"
                for r in seq
            )
            all_text += " " + " ".join(
                f"{fe.body_goal} {' '.join(fe.row_fields or [])} "
                f"{' '.join(fe.output_fields or [])} {' '.join(fe.returns or [])}"
                for fe in foreaches
            )
            title_row_fields = [
                field
                for fe in foreaches
                for field in [*(fe.row_fields or []), *(fe.returns or [])]
                if re.search(r"\btitle\b|summary|评论标题|评论摘要", field, flags=re.I)
            ]
            summary_detail = re.search(r"summary\s+of\s+review|评论标题|评论摘要", all_text, flags=re.I)
            if not title_row_fields and not summary_detail:
                details.append(
                    "评论 title+rating 任务没有把 title 绑定到 grid Title/摘要列，也没有在详情页 "
                    "read_spec 中明确 Summary of Review；容易误读 Nickname 或页面标题。"
                    f" foreach_fields={[(fe.row_fields, fe.output_fields, fe.returns) for fe in foreaches]}"
                    f" reads={[(r.name, r.returns, r.read_spec) for r in seq if r.returns]}"
                )
            title_readers = [
                r for r in seq
                if r.returns and any(str(ret).strip().lower() == "title" for ret in r.returns)
            ]
            bad_title_sources = [
                (r.name, r.read_spec)
                for r in title_readers
                if re.search(r"\bnickname\b|昵称|edit review|页面标题|page title", f"{r.name} {r.read_spec}", flags=re.I)
                and not re.search(r"summary\s+of\s+review|评论标题|评论摘要", f"{r.name} {r.read_spec}", flags=re.I)
            ]
            if bad_title_sources:
                details.append(
                    "title 读取步骤疑似绑定到 Nickname/页面标题，而不是 Summary of Review: "
                    f"{bad_title_sources}"
                )
            bad_summary_sql = [
                (r.name, getattr(r, "sql", "") or "")
                for r in seq
                if r.kind == "data_query"
                and re.search(r"\bsummary\b", getattr(r, "sql", "") or "", flags=re.I)
                and not re.search(r"\bsummary_of_review\b", getattr(r, "sql", "") or "", flags=re.I)
            ]
            if bad_summary_sql:
                details.append(
                    "评论 title 字段来自 `Summary of Review`，data_query 应使用 normalized "
                    "`summary_of_review AS title`，不要写不存在的简称 `summary`: "
                    f"{bad_summary_sql}"
                )
            bad_summary_row_fields = [
                fe.row_fields
                for fe in foreaches
                if any(str(field).strip().lower() == "summary_of_review" for field in (fe.row_fields or []))
            ]
            if bad_summary_row_fields:
                details.append(
                    "foreach row_fields 要写 UI/source label `Summary of Review`，不要写 SQL normalized "
                    "`summary_of_review`；normalized 名只用于 data_query SQL: "
                    f"{bad_summary_row_fields}"
                )
        elif assertion == "filter_leads_with_exact_value":
            # Regression task-113 live run 20260625_145506: the approximate entity "Olivia zip
            # jacket" (search_key "Olivia") was filtered straight by the bare keyword
            # ("用 Product 列按关键词 'Olivia' 筛选") — fuzzy-first, skipping the exact attempt
            # rule 4b mandates. The filter step must LEAD with the full exact value; the keyword
            # is only the 0-results fallback. (Whether exact→fuzzy is one filter step that relaxes
            # or an if-branch is up to the planner; either way the exact value must be named.)
            seq = _flatten_runs(program.statements)
            prod_filters = [
                r for r in seq
                if r.kind in ("filter", "action")
                and re.search(r"olivia", f"{r.name} {r.success_condition}", re.I)
            ]
            exact_present = any(
                "olivia zip jacket" in f"{r.name} {r.success_condition}".lower()
                for r in prod_filters
            )
            if not prod_filters:
                details.append(
                    "没有按 Olivia 检索的 filter/action 步（无从校验先精确后模糊）: "
                    f"{[(r.kind, r.name) for r in seq if r.kind in ('filter', 'action')]}"
                )
            elif not exact_present:
                details.append(
                    "filter 步直接用关键词 'Olivia' 检索、跳过了精确原值 'Olivia zip jacket'"
                    "（模糊优先，违反规则4b 先精确后模糊；filter name 必须先点名完整精确原值）: "
                    f"{[(r.kind, r.name) for r in prod_filters]}"
                )
        elif assertion == "browser_drill_uses_url_direct":
            # Regression task-113 live run 20260625_145506: on browser, the per-row drill opened
            # each review by clicking ("打开评论 {row[review_id]} 的详情") instead of jumping by the
            # row's folded detail link ("打开 {row[Action_url]}"). The score was 1.0 but the
            # execution clicked through every Edit-Review page — defeating part-2's non-interactive
            # value. Browser list-grid drills must default to URL-direct (rule 11②): the row
            # collection (legacy read step, or the foreach's own returns) reads a `<col>_url` link
            # column and the foreach body opens via it, so non_interactive._direct_nav_url fires
            # (deterministic navigate, no UI clicking). This case carries NO table_summaries on
            # purpose — the upfront decompose runs on the Dashboard with no headers, exactly where
            # the live bug lived; url-direct must be the browser default, not contingent on seeing
            # a `_url` header.
            foreaches = _flatten_foreaches(program.statements)
            all_runs = _flatten_runs(program.statements)
            collection_fields: list[str] = []
            for fe in foreaches:
                if fe.over:
                    collection_fields += next(
                        (r.returns for r in all_runs if r.kind == "read" and r.var == fe.over), []
                    )
                else:
                    collection_fields += [*(fe.row_fields or []), *(fe.returns or [])]
            reads_url_col = any(f.lower().endswith("_url") for f in collection_fields)
            body_open_url = False
            body_open_id: list[str] = []
            for fe in foreaches:
                for b in fe.body:
                    if isinstance(b, RunLike) and any(k in b.name for k in ("打开", "open", "进入", "详情")):
                        if re.search(r"\{row\[[^\]]*_url\]\}", b.name, re.I):
                            body_open_url = True
                        elif re.search(r"\{row\[", b.name):
                            body_open_id.append(b.name)
            if not foreaches:
                details.append(
                    "无 foreach，无法校验逐行 URL 直达（browser 逐行钻取应先采集候选行再 foreach→data_query）"
                    )
            else:
                identity_fields = [
                    f for f in collection_fields
                    if re.search(r"\b(id|sku|code|name|title|email|number)\b", _sql_identifier(f), flags=re.I)
                ]
                if not reads_url_col and not identity_fields:
                    details.append(
                        "采集步既未读出 `<列>_url` 详情链接列，也未读出可定位当前行的稳定身份字段 "
                        "（如 ID/SKU/Name/Title）；逐行钻取缺少可执行的行定位依据: "
                        f"collection_fields={collection_fields}"
                    )
                if not body_open_url and not body_open_id:
                    details.append(
                        "foreach 打开步既未用 `{row[..._url]}` URL 直达，也未使用 `{row[...]}` "
                        "行身份字段定位详情；逐行钻取目标不确定: "
                        f"{body_open_id}"
                    )
        elif assertion == "customer_phone_lookup_uses_keyword_search":
            # WebArena task 212 live run 20260626_233039: "find customer with phone 555-229-3326"
            # was planned (and executed) as a Filters→Phone-column EXACT filter on the full
            # punctuated number → 0 records → NOT_FOUND. Ground truth: Magento stores the phone as
            # '(555) 229-3326' (parenthesized area code + space); the customer grid keyword search
            # is a literal substring LIKE, so the full '555-229-3326' is never a contiguous
            # substring (the ') ' breaks it) and matches nothing by ANY control. The stable hit is
            # the local-number fragment '229-3326' via the top "Search by keyword" box (see
            # Customers functional knowledge). So the phone-lookup filter/action step must
            # (a) use keyword search, not the Phone column filter, and (b) search the local
            # fragment '229-3326', not the full '555-229-3326'.
            seq = _flatten_runs(program.statements)
            phone_steps = [
                r for r in seq
                if r.kind in ("filter", "action")
                and re.search(r"229-?3326|电话|phone", f"{r.name} {r.success_condition}", re.I)
            ]
            if not phone_steps:
                details.append(
                    "没有按电话检索的 filter/action 步（无从校验电话查找走法）: "
                    f"{[(r.kind, r.name) for r in seq if r.kind in ('filter', 'action')]}"
                )
            else:
                blob = " ".join(f"{r.name} {r.success_condition}" for r in phone_steps).lower()
                uses_phone_column = bool(re.search(r"phone\s*列|phone\s*column|phone\s*字段|phone\s*筛选|按\s*phone", blob))
                uses_keyword = bool(re.search(r"keyword|search by keyword|关键词|搜索框|顶部.*搜索", blob))
                if uses_phone_column or not uses_keyword:
                    details.append(
                        "电话查找用了 Phone 列精确筛选（或没走顶部 keyword search）——电话存为 "
                        "'(555) 229-3326'，Phone 列精确/整串匹配命中不了；应走顶部 Search by keyword: "
                        f"{[(r.kind, r.name) for r in phone_steps]}"
                    )
                if "555-229-3326" in blob and "229-3326" not in blob.replace("555-229-3326", ""):
                    details.append(
                        "电话查找搜了完整号 '555-229-3326'——它不是存储值 '(555) 229-3326' 的连续子串、0 命中；"
                        "应搜去区号的本地号段 '229-3326': "
                        f"{[(r.kind, r.name) for r in phone_steps]}"
                    )
        elif assertion == "products_qty_zero_uses_ui_filter_before_data_query":
            # WebArena task 184: Products grid has 2040 rows (340 pages). data_scope:complete
            # reads max_pages=20 (~120 rows), so collected << total_records → partial=true →
            # data_query refuses. A Quantity From=0,To=0 UI filter reduces results to ~150
            # rows (~8 pages, fits within max_pages=20) so a complete read is possible.
            # The decomposer must plan a UI step (filter or action) before data_query.
            seq = _flatten_runs(program.statements)
            qty_keywords = ("quantity", "qty", "库存", "0 units", "unit", "数量")
            ui_steps = [
                r for r in seq
                if r.kind in ("filter", "action")
                and any(kw in f"{r.name} {r.success_condition}".lower() for kw in qty_keywords)
            ]
            if not ui_steps:
                details.append(
                    "Products grid 有 2040 行（340 页）→ data_scope 最多读 120 行 → partial=true → "
                    "data_query 拒绝运行；必须先建 filter/action run 在 UI 侧设 Quantity From=0,To=0，"
                    "把结果缩到约 150 行（≤max_pages=20 可完整采集），再 data_query。"
                    "不能只靠 data_query SQL WHERE quantity=0（大表截断后无法查）。"
                    "当前计划缺少针对 quantity/qty/库存 的 UI 步骤。"
                    f" seq={[(r.kind, r.name) for r in seq]}"
                )
        elif assertion == "products_qty_zero_enables_color_column":
            # WebArena task 184: Color is NOT a default column in the Products grid.
            # The decomposer must include a step to enable the Color column (via the Columns
            # button) before collecting the grid. Without this, the grid has no Color data
            # and the task cannot be answered without drilling into each product detail page.
            seq = _flatten_runs(program.statements)
            color_keywords = ("color", "colour", "颜色", "columns", "column")
            color_steps = [
                r for r in seq
                if r.kind in ("filter", "action")
                and any(kw in f"{r.name} {r.success_condition}".lower() for kw in color_keywords)
            ]
            if not color_steps:
                details.append(
                    "Color 列不在 Products 网格的默认列里，必须先点 Columns 按钮启用 Color 列，"
                    "才能在网格采集时获取颜色数据。否则只能钻每个产品详情页（~150 次），极低效。"
                    "当前计划缺少启用 Color 列的步骤（filter/action run，关键词 color/columns/颜色）。"
                    f" seq={[(r.kind, r.name) for r in seq]}"
                )
        elif assertion == "products_qty_zero_uses_foreach_collect":
            # WebArena task 184: After filtering to ~150 rows and enabling Color column,
            # the plan must use a foreach (body can be empty, returns=[Name, Color]) to
            # collect all grid rows via collect_fn/read_grid_complete (automatic pagination).
            # list_read was removed in 19b63e5; foreach is the correct op.
            # Uses module-level _has_foreach (line 98) — do NOT redefine locally.
            if not _has_foreach(program.statements):
                details.append(
                    "过滤后约 150 条记录（8 页），必须用 foreach（body 留空，returns 含 Name/Color，"
                    "into 产出完整表）采集全量网格行——collect_fn 自动通过 AX 树翻全部分页，"
                    "不能直接 data_query 当前页（partial=true，只有 20 行，data_query 会拒绝）。"
                    "注意：list_read 已在 19b63e5 移除，不再是有效 DSL op。"
                    f" top-level stmts={[type(s).__name__ for s in program.statements]}"
                )
        elif assertion == "shopping_admin_material_drills_parent_configurable":
            # WebArena task 185 ("Give me the material of the products that have 3 units left",
            # expected ["cotton","fleece"]). REVISED root cause (live REST + run 20260629_164903):
            # this is a parent/child ENTITY problem, not a vision-read bug. The Qty=3 filter matches
            # CHILD simple variants (qty lives on children; parent qty=0). Material is NOT a
            # distinguishing attribute — it lives ONLY on the PARENT configurable (a multiselect),
            # and is empty on the child. So a plan that just drills the qty-filtered (child) row
            # reads an empty Material and loops. The correct plan must (a) filter Qty=3 (covered by
            # products_qty_zero_uses_ui_filter_before_data_query), (b) foreach over the filtered
            # variants, (c) for each, RESOLVE TO THE PARENT CONFIGURABLE — strip the -SIZE-COLOR
            # suffix, keyword-search the base name in the Products grid, pick the Type=Configurable
            # row — and read Material there (primary/first selected value only). See memory
            # webarena-185-material-multiselect-read and Products functional knowledge.
            # Accepts BOTH the agentic body_goal shape (preferred — per-row sub-goal decomposed at
            # runtime) AND the legacy pre-baked body shape. The 185 lineage of live failures is
            # encoded as guards: material must actually LAND per row (214011 DATA_VALIDATION_ERROR),
            # a SINGLE foreach (no collect+drill over='' split, 214011), and the row must be
            # TEMPLATED so each iteration is distinct (215344 searched "Minerva" twice).
            seq = _flatten_runs(program.statements)
            foreaches = _flatten_foreaches(program.statements)
            functions = list(getattr(program, "functions", []) or [])
            funcs_by_name = {fn.name: fn for fn in functions}

            def _fn_text(fn) -> str:
                parts = [fn.name, " ".join(fn.returns or [])]
                for r in _flatten_runs(fn.body):
                    parts.append(f"{r.name} {r.read_spec} {' '.join(r.returns or [])}")
                parts += [st.expr for st in fn.body if type(st).__name__ == "Compute"]
                return " ".join(parts)

            # text spans statement runs + foreach body_goals + ALL function bodies (the function
            # shape puts the parent-resolution + material read INSIDE a FunctionDef, not in main)
            all_text = (
                " ".join(f"{r.name} {r.success_condition} {r.read_spec} {' '.join(r.returns or [])}"
                         for r in seq)
                + " " + " ".join((fe.body_goal or "") + " " + " ".join(fe.returns or []) for fe in foreaches)
                + " " + " ".join(_fn_text(fn) for fn in functions)
            ).lower()
            fn_text_all = " ".join(_fn_text(fn) for fn in functions).lower()
            if not any(m in all_text for m in ("material", "材质", "材料")):
                details.append(
                    "task 185 要读产品的 Material，但计划（含 foreach body_goal/returns）里没出现 material/材质。"
                    f" foreaches={[(fe.into, fe.returns, (fe.body_goal or '')[:40]) for fe in foreaches]}"
                )
            if not _has_foreach(program.statements):
                details.append(
                    "Material 不在 Products 网格默认列里，必须用 foreach 逐个产品处理；当前计划没有 foreach。"
                    f" top-level stmts={[type(s).__name__ for s in program.statements]}"
                )
            if not any(m in all_text for m in (
                "configurable", "父产品", "父配置", "parent", "基名", "去后缀",
                "-size-color", "去掉后缀", "去除后缀", "去掉 -size",
            )):
                details.append(
                    "Material 在本数据集常由父配置型产品承载（qty 筛出的子变体自身 Material 为空）；计划必须"
                    "保留『自身为空则回退到父 configurable』的 fallback 路径（去 -SIZE-COLOR 后缀搜基名 → 选 "
                    "Type=Configurable 行再读），否则空值回退无门。"
                )
            # The redesign reads the product's OWN attribute FIRST and only falls back to the parent
            # when it's empty — parent must be a FALLBACK, not the unconditional route. The plan should
            # therefore read self (a self/variant read or an if on the self value), not jump straight
            # to the parent. Lenient: any sign of a self-first / conditional-fallback shape.
            if not any(m in all_text for m in (
                "自身", "自己", "self", "为空", "若空", "空则", "exists", "empty", "selectedindex", "回退", "fallback",
            )):
                details.append(
                    "属性来源应按证据解析：先在产品自身读 Material、仅自身为空才回退父 configurable（parent 是 "
                    "fallback 不是默认路线）；计划里看不到『先读自身 / 空则回退』的迹象，疑似把『直奔父产品』写死。"
                )
            url_cols = [
                r for fe in foreaches for r in (fe.returns or [])
                if "url" in (r or "").lower() or "链接" in (r or "").lower()
            ]
            if not url_cols or not any(m in all_text for m in (
                "{product_url}", "{row[action_url]}", "action_url", "_url", "href", "详情链接",
            )):
                details.append(
                    "自身属性读取应使用 Products 行自带详情链接（如 Action_url/product_url）直达，而不是依赖"
                    "『当前 qty 结果列表仍在场』后再点行；父产品 fallback 会改变列表搜索状态，下一轮会漂。"
                    f" foreach returns={[(fe.into, fe.returns) for fe in foreaches]}"
                )
            if any(m in all_text for m in (
                "当前products结果列表", "当前 products 结果列表", "当前结果列表", "qty=3 结果列表",
                "库存结果列表", "点开sku=", "点开 sku=", "sku={sku}那一行",
            )):
                details.append(
                    "函数里的自身读取不能写成『当前结果列表点 SKU 那一行』；这是位置相关入口。应 foreach 采 "
                    "Action_url 并打开该 URL 直达自身详情，fallback 分支再单独回 Products 搜父。"
                )
            # Accept any derived-parent-sku naming（base_sku / parent_sku / 父sku…）：the LLM's
            # variable name is free; what matters is a DERIVED sku identity anchoring the parent
            # search. A parent_sku-named textbook plan (compute rsplit → 搜索 {parent_sku} →
            # SKU={parent_sku}+Type=Configurable) was flunked by the old base_sku-only tuple
            # (2026-07-05 S8 smoke), inflating the "~1/3 base_sku flakiness" read on 07-04.
            if not any(m in all_text for m in (
                "base_sku", "parent_sku", "父 sku", "父sku",
                "sku={base", "sku = {base", "sku={parent", "sku = {parent",
            )):
                details.append(
                    "父产品 fallback 应由 SKU 去 -SIZE-COLOR 后缀得到父 SKU，并以 SKU={base_sku} + "
                    "Type=Configurable Product 验证父行；不要把父 identity 建在产品名/品牌词上。"
                    f" all_text={all_text[:240]}"
                )
            # Scan all_text (statement runs + foreach bodies + functions), not fn_text_all:
            # the inline-body shape puts back-navs inside the foreach body, and a functions-only
            # scan flunked structurally-correct inline plans (branch 185 A/B, 2026-07-04).
            if not any(m in all_text for m in ("返回", "上一页", "go_back", "back")):
                details.append(
                    "逐行详情读取应在读完详情后显式返回上一页/Products 搜索结果列表，复用浏览器历史；"
                    "否则下一行会从前一条详情页开始，进入列表和搜索父产品会多烧多轮。"
                )
            if len(foreaches) > 1:
                details.append(
                    "Material 下钻必须收敛成**单个 foreach**；拆成两个 over='' foreach 时，浏览器路径下第二个"
                    "会重新采集网格、material 落不进表（live 214011）。"
                    f"当前有 {len(foreaches)} 个 foreach：{[fe.into for fe in foreaches]}"
                )
            # NO-DEAD-CONDITIONAL: the self-first resolution is expressed as a function with an `if` on
            # the self read (resolve_product_material). The orchestration logic must not be "written
            # dead" — i.e. an `if.cond.var` that no preceding read/call in scope binds → the condition
            # is always empty → every row falls to else → degenerates to hardcoded-always-parent (the
            # "编排逻辑写死" the user flagged). Flag any If whose cond.var isn't produced upstream.
            def _bound_vars(stmts):
                out = set()
                for s in stmts:
                    nm = type(s).__name__
                    if nm in ("Run", "Call", "Compute") and getattr(s, "var", ""):
                        out.add(s.var)
                    if nm == "If":
                        out |= _bound_vars(s.then) | _bound_vars(s.otherwise)
                    if nm == "ForEach":
                        out |= _bound_vars(s.body)
                return out
            for fn in functions:
                bound = _bound_vars(fn.body) | set(fn.params)
                for st in fn.body:
                    if type(st).__name__ == "If" and st.cond.var not in bound:
                        details.append(
                            f"函数 {fn.name} 的 if 条件引用了未绑定的变量 {st.cond.var!r}（没有在先的 read/call/"
                            "compute 产出它）→ 条件恒空、永远走 else，self-first 退化成 hardcoded-always-parent。"
                            f" if.cond.var={st.cond.var!r}, 已绑定={sorted(bound)}")
            # material must LAND per row + the row must be TEMPLATED — checked per the foreach shape:
            for fe in foreaches:
                tmpl = "{%s[" % fe.var
                calls = [s for s in fe.body if type(s).__name__ == "Call"]
                if calls:  # function-call shape (preferred): material lands via a called function
                    called = [funcs_by_name[c.func] for c in calls if c.func in funcs_by_name]
                    if not any(
                        any("material" in (r or "").lower() for r in (fn.returns or []))
                        for fn in called
                    ):
                        details.append(
                            "foreach body 的 op=call 必须调用一个 returns 含 material 的函数（material 随行"
                            "汇进 into 表供 data_query 查询）；当前被调函数都没返回 material。"
                            f" calls={[c.func for c in calls]}, funcs={[(fn.name, fn.returns) for fn in functions]}")
                    # The function must ACT to reach the parent (search+open) — a pure `read`
                    # milestone doesn't navigate/search, it only reads the current frame, so material
                    # comes back empty (live 20260630_094410: run_kind=read → no search/open → "").
                    if called and not any(
                        any(r.kind in ("navigation", "action", "filter") for r in _flatten_runs(fn.body))
                        for fn in called
                    ):
                        details.append(
                            "被调函数必须有一个【会动作的】milestone（run_kind=navigation/action/filter）去搜索并"
                            "打开父产品——纯 read milestone 不导航/不搜索、只读当前帧，material 必空"
                            f"（live 094410）。函数 body kinds={[[r.kind for r in _flatten_runs(fn.body)] for fn in called]}")
                    if not any(tmpl in v for c in calls for v in (c.args or {}).values()):
                        details.append(
                            f"op=call 的 call_args 必须用 `{tmpl}…]`（如 `{{{fe.var}[Name]}}`）把当前行代入函数参数，"
                            "运行时才逐行不同；否则每次都用同一个值、只命中第一个（live 215344）。"
                            f" call_args={[c.args for c in calls]}")
                    if not any(
                        f"{{{fe.var}[sku]}}" in str(v).lower()
                        or str(k).lower() == "sku"
                        for c in calls
                        for k, v in (c.args or {}).items()
                    ):
                        details.append(
                            f"op=call 必须把当前行 SKU 传入函数（如 sku=`{{{fe.var}[SKU]}}`），"
                            "fallback 父产品由 SKU 去 -SIZE-COLOR 后缀得到；不要只传产品名。"
                            f" call_args={[c.args for c in calls]}"
                        )
                elif fe.body_goal:  # agentic sub-goal shape
                    if not any("material" in (r or "").lower() for r in (fe.returns or [])):
                        details.append(
                            f"body_goal foreach(var={fe.var!r}) 的 returns 必须含 material —— 这是每行子目标"
                            "产出的契约，运行时汇进 into 表供 data_query 查询；否则 material 落不进表。"
                            f" returns={fe.returns}")
                    if tmpl not in fe.body_goal:
                        details.append(
                            f"body_goal 必须**字面**含循环变量模板 `{tmpl}…]`（如 `{{{fe.var}[Name]}}`），"
                            "运行时才按行代入每个变体名；否则每行子目标都一样、只命中第一个（live 215344）。"
                            f" body_goal={(fe.body_goal or '')[:60]!r}")
                else:  # legacy pre-baked body shape
                    body_runs = _flatten_runs(fe.body)
                    if not body_runs:
                        continue
                    if not any("material" in " ".join(r.returns or []).lower()
                               or any(m in (r.read_spec or "").lower() for m in ("material", "材质"))
                               for r in body_runs):
                        details.append(
                            "Material 必须由 foreach body 内的 run 逐行读出（随行汇进 into 表）；当前 body 没有"
                            " run 返回 material（live 214011 类 DATA_VALIDATION_ERROR）。"
                            f" foreach into={fe.into}")
                    if not any(tmpl in (r.name or "") for r in body_runs):
                        details.append(
                            f"foreach(var={fe.var!r}) 的 body 必须在某 run name 里字面引用 `{tmpl}…]`，运行时"
                            "才逐行代入变体名；否则每次都跑同一条泛指令、只命中第一个（live 215344）。"
                            f" body run names={[(r.name or '')[:40] for r in body_runs]}")
        elif assertion == "filter_step_clears_residual_filters":
            # WebArena task 186 (live run 1 scored 0.0): Magento admin grid filters persist
            # server-side per admin account and leak across tasks. Task 186 ("products with
            # 2-3 units left") inherited a `Keyword: WS08` filter from task 185, silently
            # narrowing the grid to 1 of 2 products. The data source must reflect EXACTLY the
            # task's required filter set — so the filter step must clear unrelated residual
            # filters / search / keyword / range, not just add the qty 2-3 range. This applies
            # even though the task HAS a specific filter (qty 2-3); it is not an any/all task.
            seq = _flatten_runs(program.statements)
            clear_keywords = (
                "清除", "残留", "无其它", "无其他", "无关", "仅保留", "只保留", "恰好等于",
                "重置", "clear", "reset", "no other", "remove existing",
            )
            clear_steps = [
                r for r in seq
                if r.kind in ("filter", "action", "navigate")
                and any(kw in f"{r.name} {r.success_condition}".lower() for kw in
                        (k.lower() for k in clear_keywords))
            ]
            if not clear_steps:
                details.append(
                    "Magento 后台 grid 筛选按账号持久化、跨任务残留（task 186 继承了 185 的 "
                    "`Keyword: WS08`，结果只剩 1/2 产品，静默失败）。数据源必须恰好反映本任务要求的"
                    "筛选集——filter 步骤的 name/success_condition 必须包含清除任务未要求的残留"
                    "筛选/搜索/关键词/范围（关键词：清除/残留/无其它/无关/仅保留/恰好等于/clear/reset），"
                    "不能只加 qty 2-3 而不管别的筛选。即使本任务有具体筛选值也要保证无无关筛选叠加。"
                    f" seq={[(r.kind, r.name) for r in seq]}"
                )
        elif assertion == "orders_total_payment_no_detail_drill":
            # WebArena task 193: Grand Total is a default column in the Orders grid.
            # The plan must NOT drill into individual order detail pages (foreach body
            # with order/view navigation). Reading top-N rows directly from the grid is
            # sufficient — no foreach drill needed.
            for s in program.statements:
                if not isinstance(s, ForEach):
                    continue
                body_runs = _flatten_runs(s.body)
                drill_runs = [
                    r for r in body_runs
                    if r.kind == "navigation" and any(
                        marker in f"{r.name} {r.success_condition}".lower()
                        for marker in ("order/view", "订单详情", "detail", "view/order_id")
                    )
                ]
                if drill_runs:
                    details.append(
                        "Orders 网格默认含 Grand Total 列，最近 N 笔订单金额应直接从网格读取，"
                        "不需要 foreach drill 进订单详情页。"
                        "检测到 foreach body 含详情页导航（anti-pattern：每笔订单单独 URL-direct 开详情）。"
                        f" drill_runs={[(r.kind, r.name) for r in drill_runs]}"
                    )
        elif assertion == "orders_total_payment_filters_complete_clears_residual":
            # WebArena task 193: must filter Status=Complete AND clear residual filters.
            seq = _flatten_runs(program.statements)
            text = " ".join(
                f"{r.kind} {r.name} {r.success_condition}"
                for r in seq
            ).lower()
            if "status" not in text or "complete" not in text:
                details.append(
                    "task 193 需要筛选 Status=Complete 后读取订单金额，当前未看到 Status/Complete 筛选。"
                    f" seq={[(r.kind, r.name) for r in seq]}"
                )
            clear_keywords = (
                "清除", "残留", "无其它", "无其他", "无关", "仅保留", "只保留", "恰好等于",
                "重置", "clear", "reset",
            )
            clear_steps = [
                r for r in seq
                if r.kind in ("filter", "action", "navigate")
                and any(kw in f"{r.name} {r.success_condition}".lower() for kw in clear_keywords)
            ]
            if not clear_steps:
                details.append(
                    "Magento 后台 grid 筛选持久化跨任务残留，filter 步骤必须清除无关残留筛选，"
                    "success_condition 含「清除/残留/恰好等于」等关键词。"
                    f" seq={[(r.kind, r.name) for r in seq]}"
                )
        elif assertion == "orders_total_payment_uses_foreach_collect_and_data_query":
            # WebArena task 193, 20260626_195323: The correct route is a grid
            # collection, not detail drill and not direct data_query over the current
            # partial DOM table. foreach body=[] materializes a complete
            # `into` table; data_query must aggregate the first 2 rows from that table.
            foreaches = _flatten_foreaches(program.statements)
            grid_collects = [
                fe for fe in foreaches
                if not fe.body and any(
                    "grand" in str(ret).lower() and "total" in str(ret).lower()
                    for ret in (fe.row_fields or fe.returns)
                )
            ]
            if not grid_collects:
                details.append(
                    "task 193 应用 foreach body=[] 从 Orders 网格直接采集 Grand Total (Purchased)，"
                    "让 collect_fn 自动翻页并产出 complete into 表；不能直接 data_query 当前 DOM "
                    "partial 表，也不能钻详情页。"
                    f" foreaches={[(fe.target, fe.row_fields or fe.returns, len(fe.body), fe.into) for fe in foreaches]}"
                )
            dqs = [r for r in _flatten_runs(program.statements) if r.kind == "data_query"]
            into_names = {
                (fe.into or f"{fe.var}s").strip().lower()
                for fe in grid_collects
                if (fe.into or f"{fe.var}s").strip()
            }
            ok_query = False
            for dq in dqs:
                sql = (dq.sql or "").lower()
                amount_is_typed = "grand_total_purchased_num" in sql or re.search(
                    r"\b[a-z0-9_]*grand[a-z0-9_]*total[a-z0-9_]*_num\b", sql
                )
                date_sort_is_typed = (
                    "order by" not in sql
                    or "purchase_date" not in sql
                    or "purchase_date_ts" in sql
                )
                if (
                    "sum" in sql
                    and "limit" in sql
                    and "2" in sql
                    and any(name and name in sql for name in into_names)
                    and amount_is_typed
                    and date_sort_is_typed
                ):
                    ok_query = True
                    break
            if not ok_query:
                details.append(
                    "task 193 的 data_query 应对 foreach 产出的完整表求前 2 行 Grand Total 之和"
                    "（SQL 需引用 foreach into 表，并包含 SUM 与 LIMIT 2；金额显示文本必须用 "
                    "grand_total_purchased_num 一类 _num 影子列；若 SQL 按 Purchase Date 排序，"
                    "必须用 purchase_date_ts 一类 _ts 影子列，不能按原始日期文本排序）。"
                    f" dqs={[(r.name, getattr(r, 'sql', '')) for r in dqs]} into_names={sorted(into_names)}"
                )
        elif assertion == "orders_total_payment_sorts_purchase_date_desc":
            # The SQL LIMIT 2 is only meaningful if the UI data source has first been
            # sorted by Purchase Date descending. The live run 20260626_195323 was
            # wrongly accepted while the visible sort arrow was on Status.
            seq = _flatten_runs(program.statements)
            prep_steps = [r for r in seq if r.kind in ("filter", "action", "navigation")]
            sort_steps = [
                r for r in prep_steps
                if "purchase date" in f"{r.name} {r.success_condition}".lower()
                and any(
                    kw in f"{r.name} {r.success_condition}".lower()
                    for kw in ("降序", "倒序", "desc", "descending", "latest", "最近", "newest")
                )
            ]
            if not sort_steps:
                details.append(
                    "task 193 的 LIMIT 2 代表最近 2 笔，前置 UI 步骤必须明确按 Purchase Date "
                    "降序/最新在前排序；不能只筛 Status=Complete，或把 Status 列排序误当日期排序。"
                    f" seq={[(r.kind, r.name, r.success_condition) for r in seq]}"
                )
        elif assertion == "orders_payment_difference_uses_both_status_filters":
            seq = _flatten_runs(program.statements)
            text = " ".join(f"{r.kind} {r.name} {r.success_condition} {getattr(r, 'sql', '')}" for r in seq).lower()
            if not re.search(r"\bcancell?ed\b|取消", text) or "complete" not in text:
                details.append(
                    "task 196 必须分别取得 cancelled/canceled 与 completed/complete 两个订单状态口径；"
                    "当前计划没有同时体现这两个状态。"
                    f" seq={[(r.kind, r.name, r.success_condition, getattr(r, 'sql', '')) for r in seq]}"
                )
            clear_keywords = (
                "清除", "残留", "无其它", "无其他", "无关", "仅保留", "只保留", "恰好等于",
                "clear", "reset",
            )
            clear_steps = [
                r for r in seq
                if r.kind in ("filter", "action", "navigation")
                and any(kw in f"{r.name} {r.success_condition}".lower() for kw in clear_keywords)
            ]
            if not clear_steps:
                details.append(
                    "task 196 会连续切换两个 Orders grid 状态筛选，必须清除无关/上一状态残留筛选，"
                    "并让 active filters 恰好等于当前状态条件。"
                    f" seq={[(r.kind, r.name, r.success_condition) for r in seq]}"
                )
        elif assertion == "orders_payment_difference_uses_abs_last4_subqueries":
            foreaches = _flatten_foreaches(program.statements)
            grid_collects = [
                fe for fe in foreaches
                if not fe.body
                and any("grand" in str(ret).lower() and "total" in str(ret).lower() for ret in fe.returns)
                and any("purchase" in str(ret).lower() and "date" in str(ret).lower() for ret in fe.returns)
            ]
            if not grid_collects:
                details.append(
                    "task 196 应从 Orders 网格采集可见列 Purchase Date 与 Grand Total (Purchased)，"
                    "让 data_query 对完整表按日期排序后聚合；不能只查当前 DOM、不能钻详情，"
                    "也不能把内部字段名 created_at 写进 foreach returns（collect_fn 读不到）。"
                    f" foreaches={[(fe.target, fe.returns, len(fe.body), fe.into) for fe in foreaches]}"
                )
            dqs = [r for r in _flatten_runs(program.statements) if r.kind == "data_query"]
            combined_sql = "\n".join((getattr(r, 'sql', '') or "").lower() for r in dqs)
            bad_limit = [
                getattr(r, 'sql', '') for r in dqs
                if re.search(
                    r"\bselect\s+sum\s*\([^)]*\)\s+(?:as\s+\w+\s+)?from\s+[a-z_][a-z0-9_]*\s+limit\s+4\b",
                    (getattr(r, 'sql', '') or "").lower(),
                    flags=re.DOTALL,
                )
            ]
            if bad_limit:
                details.append(
                    "task 196 的旧失败就是 `SELECT SUM(...) FROM table LIMIT 4`：LIMIT 在聚合后生效，"
                    "实际求了全表总和。必须写成 `SUM FROM (SELECT ... ORDER BY purchase_date_ts DESC LIMIT 4)`。"
                    f" bad_sql={bad_limit}"
                )
            if "abs" not in combined_sql:
                finish_text = " ".join(f.message.lower() for f in _flatten_finishes(program.statements))
                if "绝对" not in finish_text and "absolute" not in finish_text:
                    details.append(
                        "task 196 问的是 payment difference between A and B，未要求 A minus B；"
                        "应返回绝对差 ABS(a-b)，不能在 finish 中拼接可能为负的 a-b。"
                        f" dqs={[(r.name, getattr(r, 'sql', '')) for r in dqs]} finishes={[f.message for f in _flatten_finishes(program.statements)]}"
                    )
            if combined_sql.count("limit 4") < 2:
                details.append(
                    "task 196 需要分别取最近 4 笔 cancelled 和最近 4 笔 completed；"
                    "SQL 应对两个状态口径各自 LIMIT 4。"
                    f" dqs={[(r.name, getattr(r, 'sql', '')) for r in dqs]}"
                )
            has_amount_num = "grand_total_purchased_num" in combined_sql
            has_date_ts = "purchase_date_ts" in combined_sql or "created_at_ts" in combined_sql
            if not has_amount_num or not has_date_ts:
                details.append(
                    "task 196 金额聚合必须用 grand_total_purchased_num 一类 _num 影子列，"
                    "最近订单排序必须用 purchase_date_ts/created_at_ts 一类 _ts 影子列。"
                    f" dqs={[(r.name, getattr(r, 'sql', '')) for r in dqs]}"
                )
        elif assertion == "orders_payment_difference_no_visual_row_aggregation":
            seq = _flatten_runs(program.statements)
            offenders = []
            for r in seq:
                if r.kind == "data_query" or not r.returns:
                    continue
                text = f"{r.name} {r.success_condition} {r.read_spec} {' '.join(r.returns)}".lower()
                mentions_top_n_rows = re.search(
                    r"(first|top|last|latest|recent|oldest|前|最近|最后|最新|最旧|最早)\s*4.{0,80}"
                    r"(row|rows|record|records|order|orders|行|条|笔)",
                    text,
                    flags=re.I,
                )
                mentions_manual_agg = re.search(
                    r"sum|total|add up|average|avg|difference|aggregate|总和|合计|求和|相加|平均|差值|聚合",
                    text,
                    flags=re.I,
                )
                if mentions_top_n_rows and mentions_manual_agg:
                    offenders.append((r.kind, r.name, r.returns, r.read_spec))
            if offenders:
                details.append(
                    "task 196 不能让 action/filter/read 目测读取前 4 行并手工求总额/差值；"
                    "必须用 foreach body=[] 采集两个状态口径的完整 Orders 网格，再由 data_query 聚合计算。"
                    f" offenders={offenders}"
                )
        elif assertion == "orders_non_cancelled_total_uses_status_exclusion":
            seq = _flatten_runs(program.statements)
            foreaches = _flatten_foreaches(program.statements)
            grid_collects = [
                fe for fe in foreaches
                if not fe.body
                and any("status" in str(ret).lower() for ret in fe.returns)
                and any("grand" in str(ret).lower() and "total" in str(ret).lower() for ret in fe.returns)
                and any("purchase" in str(ret).lower() and "date" in str(ret).lower() for ret in fe.returns)
            ]
            if not grid_collects:
                details.append(
                    "task 197 的 non-cancelled 口径应从 Orders 网格采集完整行，foreach returns 至少包含 "
                    "Status、Purchase Date、Grand Total (Purchased)；不能只采金额/日期，也不能钻详情。"
                    f" foreaches={[(fe.target, fe.returns, len(fe.body), fe.into) for fe in foreaches]}"
                )
            bad_status_filters = []
            negative_status_filters = []
            for r in seq:
                if r.kind not in {"filter", "action", "navigation"}:
                    continue
                text = f"{r.name} {r.success_condition}".lower()
                sets_single_status = "status" in text and re.search(r"\bcomplete\b|\bprocessing\b|\bpending\b", text)
                mentions_non_cancel = re.search(r"non[- ]?cancell?ed|not cancell?ed|exclude cancell?ed|排除.*取消|非取消", text)
                if sets_single_status and not mentions_non_cancel:
                    bad_status_filters.append((r.kind, r.name, r.success_condition))
                negative_status_ui = (
                    ("status" in text or "状态" in text)
                    and re.search(
                        r"不为|不是|不含|排除|非取消|无\s*cancell?ed|not\s+cancell?ed|exclude\s+cancell?ed|non[- ]?cancell?ed",
                        text,
                        flags=re.I,
                    )
                )
                clear_only = re.search(r"清除|清空|无关|残留|clear|reset|no active", text, flags=re.I)
                negative_action = re.search(r"设置|筛选|应用|选择|确保|set|apply|select|filter|ensure", text, flags=re.I)
                if negative_status_ui and (negative_action or not clear_only):
                    negative_status_filters.append((r.kind, r.name, r.success_condition))
            if bad_status_filters:
                details.append(
                    "task 197 是 non-cancelled，不等于 Status=Complete/Processing/Pending 单一状态；"
                    "页面应清除状态筛选后采集完整 Orders，再在 SQL 里排除 Canceled/Cancelled。"
                    f" bad_status_filters={bad_status_filters}"
                )
            if negative_status_filters:
                details.append(
                    "task 197 不能规划 UI 负筛选「Status 不为/排除 Canceled」；Magento Status 是单值下拉，"
                    "执行层会退化成选择 Complete。正确做法是清除状态筛选，采集含 Status 的完整 Orders，"
                    "再在 data_query SQL 中排除 Canceled/Cancelled。"
                    f" negative_status_filters={negative_status_filters}"
                )
            clear_keywords = (
                "清除", "残留", "无其它", "无其他", "无关", "clear", "reset",
                "no active", "active filters", "all orders", "全量",
            )
            if not any(
                r.kind in {"filter", "action", "navigation"}
                and any(kw in f"{r.name} {r.success_condition}".lower() for kw in clear_keywords)
                for r in seq
            ):
                details.append(
                    "task 197 需要全量 non-cancelled 口径；data_query 前必须清除无关/残留 Status 筛选，"
                    "不能继承上一题 Canceled/Complete active filter。"
                    f" seq={[(r.kind, r.name, r.success_condition) for r in seq]}"
                )
            dqs = [r for r in seq if r.kind == "data_query"]
            combined_sql = "\n".join((getattr(r, 'sql', '') or "").lower() for r in dqs)
            has_status_exclusion = bool(
                re.search(r"\bstatus\b.{0,80}(?:not\s+like|not\s+in|!=|<>).{0,80}cancell?ed|"
                          r"\bstatus\b.{0,80}(?:not\s+like|not\s+in|!=|<>).{0,80}cancel",
                          combined_sql, flags=re.I | re.S)
                or re.search(r"\bwhere\b(?:(?!\border\s+by\b).)*\bstatus\b(?:(?!\border\s+by\b).)*\bnot\b(?:(?!\border\s+by\b).)*cancel",
                             combined_sql, flags=re.I | re.S)
            )
            if not has_status_exclusion:
                details.append(
                    "task 197 必须在 SQL 中按 Status 排除 Canceled/Cancelled（如 lower(status) NOT LIKE '%cancel%'），"
                    "不能只取 complete 或不处理 non-cancelled 口径。"
                    f" dqs={[(r.name, getattr(r, 'sql', '')) for r in dqs]}"
                )
            if "limit 5" not in combined_sql:
                details.append(
                    "task 197 需要取最近 5 笔 non-cancelled orders；SQL 应包含 LIMIT 5。"
                    f" dqs={[(r.name, getattr(r, 'sql', '')) for r in dqs]}"
                )
            if "grand_total_purchased_num" not in combined_sql or "purchase_date_ts" not in combined_sql:
                details.append(
                    "task 197 金额求和必须用 grand_total_purchased_num，最近排序必须用 purchase_date_ts。"
                    f" dqs={[(r.name, getattr(r, 'sql', '')) for r in dqs]}"
                )
            bad_limit = [
                getattr(r, 'sql', '') for r in dqs
                if re.search(
                    r"\bselect\s+sum\s*\([^)]*\)\s+(?:as\s+\w+\s+)?from\s+[a-z_][a-z0-9_]*\s+limit\s+5\b",
                    (getattr(r, 'sql', '') or "").lower(),
                    flags=re.DOTALL,
                )
            ]
            if bad_limit:
                details.append(
                    "task 197 不能写 `SELECT SUM(...) FROM table LIMIT 5`；"
                    "必须先在子查询里按日期排序并 LIMIT 5，再外层 SUM。"
                    f" bad_sql={bad_limit}"
                )
        elif assertion == "most_recent_order_drills_and_reads_all_items":
            # WebArena task 204: product name + price (low to high) of the most-recent completed order.
            # Robust shape (Orders/Order Detail functional knowledge + memory webarena-204-most-recent-order):
            #   filter Status + Purchase Date sort → foreach collect [Action_url, Purchase Date]
            #   → data_query ORDER BY purchase_date_ts LIMIT 1 (pick the latest order's detail url)
            #   → URL-direct drill → SECOND foreach collect [Product, Price] (read ALL line items)
            #   → data_query clean+sort → finish list.
            # Guards the live-run failures: hardcoded/decoy order_id, "open列表第一行", reading only the first product,
            # and deciding "most recent" from the detail-page Order Date instead of the grid Purchase Date.
            seq = _flatten_runs(program.statements)
            foreaches = _flatten_foreaches(program.statements)
            dqs = [r for r in seq if r.kind == "data_query"]
            combined_sql = "\n".join((getattr(r, 'sql', '') or "").lower() for r in dqs)
            grid_collect = [
                fe for fe in foreaches
                if any("_url" in str(ret).lower() or "action" in str(ret).lower() for ret in fe.returns)
                and any("purchase" in str(ret).lower() and "date" in str(ret).lower() for ret in fe.returns)
            ]
            if not grid_collect:
                details.append(
                    "task 204 判定『最近订单』必须 foreach 采集 Action_url + Purchase Date，由 data_query 按日期选最新，"
                    "不能靠目测列表第一行。"
                    f" foreaches={[(fe.returns, len(fe.body), fe.into) for fe in foreaches]}"
                )
            if "purchase_date_ts" not in combined_sql and "created_at_ts" not in combined_sql:
                details.append(
                    "task 204 选最新单必须用 purchase_date_ts/created_at_ts 影子列排序（grid Purchase Date），"
                    "绝不用详情页 Order Date。"
                    f" dqs={[(r.name, getattr(r, 'sql', '')) for r in dqs]}"
                )
            items_collect = [
                fe for fe in foreaches
                if any("product" in str(ret).lower() for ret in fe.returns)
                and any("price" in str(ret).lower() for ret in fe.returns)
            ]
            if not items_collect:
                details.append(
                    "task 204 一张订单可能含多个商品，必须用第二个 foreach 采 Items Ordered 表的 Product+Price 读全部行，"
                    "不能用一个 read 只读『第一行/first product』。"
                    f" foreaches={[(fe.returns, len(fe.body), fe.into) for fe in foreaches]}"
                )
            hardcoded = [
                r.name for r in seq
                if re.search(r"order_id/\d+", r.name or "", flags=re.I)
            ]
            if hardcoded:
                details.append(
                    "task 204 钻取必须 URL 直达 data_query 选出的链接（{var[字段]} 模板），绝不硬编码 order_id。"
                    f" hardcoded={hardcoded}"
                )
        elif assertion == "customer_recent_pending_order_preserves_scope_and_selects_url":
            # WebArena 491/493 class: locate a customer's most recent pending order before attempting
            # a mutation. The status/date narrowing must preserve the customer scope, and "most
            # recent" must be selected from collected row data, not by eyeballing the current first row.
            seq = _flatten_runs(program.statements)
            foreaches = _flatten_foreaches(program.statements)
            dqs = [r for r in seq if r.kind == "data_query"]
            filter_texts = [
                f"{r.name} {r.success_condition}".lower()
                for r in seq
                if r.kind == "filter"
            ]
            scope_keys = ("grace", "miller", "sarah")
            preserve_words = ("保留", "同时", "追加", "keep", "retain", "preserve", "with")
            pending_filters = [text for text in filter_texts if "pending" in text]
            if not any(
                any(key in text for key in scope_keys)
                and any(word in text for word in preserve_words)
                for text in pending_filters
            ):
                details.append(
                    "追加 Pending/状态筛选的同一个 filter milestone 必须显式点名要保留的客户实体值"
                    "（如 Grace/Sarah Miller），不能只写『保留客户筛选结果范围』，否则运行时无法按值"
                    "保留上游 scope，会把客户筛选当残留清掉。"
                    f" filters={[(r.name, r.success_condition) for r in seq if r.kind == 'filter']}"
                )
            customer_scope_filters = [
                text for text in filter_texts
                if any(key in text for key in scope_keys)
            ]
            concrete_order_customer_sources = (
                "search by keyword",
                "keyword box",
                "top search",
                "顶部搜索",
                "搜索框",
                "bill-to name",
                "ship-to name",
                "customer email",
                "bill_to_name",
                "ship_to_name",
                "customer_email",
            )
            if customer_scope_filters and not any(
                any(source in text for source in concrete_order_customer_sources)
                for text in customer_scope_filters
            ):
                details.append(
                    "Orders grid 里按客户定位订单必须点名实际可用入口：顶部 Search by keyword，"
                    "或 Bill-to Name / Ship-to Name / Customer Email；不能只写泛称『客户字段』。"
                    f" filters={[(r.name, r.success_condition) for r in seq if r.kind == 'filter']}"
                )
            if any("customer name" in text or "客户字段" in text for text in customer_scope_filters):
                details.append(
                    "Orders grid 的 Customer Name 不是可靠筛选控件，『客户字段』会被执行器误解成"
                    "不存在的 Customer Name input。请改用顶部 Search by keyword 或 Bill-to/Ship-to Name。"
                    f" filters={[(r.name, r.success_condition) for r in seq if r.kind == 'filter']}"
                )

            def _fe_fields(fe: ForEach) -> list[str]:
                return [str(x) for x in (fe.row_fields or fe.returns or [])]

            grid_collect = [
                fe for fe in foreaches
                if any(("url" in f.lower() or "action" in f.lower() or "link" in f.lower()) for f in _fe_fields(fe))
                and any(("date" in f.lower() or "time" in f.lower() or "purchase" in f.lower()) for f in _fe_fields(fe))
            ]
            if not grid_collect:
                details.append(
                    "最近一笔订单必须 foreach body=[] 采集详情入口 URL/link/action_url 和 Purchase Date/时间列，"
                    "不能只采 order_id 后让 planner 点第一行。"
                    f" foreaches={[(fe.row_fields, fe.returns, len(fe.body), fe.into) for fe in foreaches]}"
                )
            combined_sql = "\n".join((getattr(r, "sql", "") or "").lower() for r in dqs)
            if not (
                "order by" in combined_sql
                and "_ts" in combined_sql
                and "limit 1" in combined_sql
                and "pending" in combined_sql
                and any(key in combined_sql for key in ("grace", "miller", "sarah"))
            ):
                details.append(
                    "最近 pending 订单必须由 data_query 在 collected table 上按客户+Pending 过滤，"
                    "并 ORDER BY <date>_ts DESC LIMIT 1。"
                    f" dqs={[(r.name, getattr(r, 'sql', '')) for r in dqs]}"
                )
            nav_text = "\n".join(r.name for r in seq if r.kind == "navigation")
            if not re.search(r"\{q\[[^\]]*(?:url|link|action)[^\]]*\]\}", nav_text, re.IGNORECASE):
                details.append(
                    "打开目标订单详情页必须使用 data_query 选出的 URL/link 字段（如 {q[detail_url]}），"
                    "不要写成『打开最近一笔』让 planner 猜当前第一行。"
                    f" navs={[r.name for r in seq if r.kind == 'navigation']}"
                )
        elif assertion == "order_notify_customer_comment_action":
            # WebArena 493: "Notify <customer> ... with message ..." is not an internal note.
            # The final mutation must use the order comment form, enable customer notification,
            # and submit/update it. Otherwise the official evaluator will miss
            # sales/order/addComment with history[is_customer_notified]=1.
            seq = _flatten_runs(program.statements)
            action_text = "\n".join(
                f"{r.name} {r.success_condition}".lower()
                for r in seq
                if r.kind == "action"
            )
            has_comment_message = any(
                token in action_text
                for token in ("comment", "message", "备注", "消息")
            )
            has_customer_notify = any(
                token in action_text
                for token in ("notify customer", "customer by email", "email", "通知客户", "客户通知", "邮件")
            )
            has_submit = any(
                token in action_text
                for token in ("update", "submit comment", "submit", "保存", "提交")
            )
            has_detail_notes_route = any(
                token in action_text
                for token in (
                    "comments history",
                    "notes for this order",
                    "order detail",
                    "detail page",
                    "详情页",
                    "评论历史",
                    "备注区域",
                    "notes 区域",
                )
            )
            internal_only = any(
                token in action_text
                for token in ("internal note", "internal-only", "内部备注", "内部 note")
            )
            edit_route = any(
                token in action_text
                for token in ("edit order", "order_edit", "点击 edit", "进入 edit", "编辑订单")
            )
            if (
                not (has_comment_message and has_customer_notify and has_submit and has_detail_notes_route)
                or internal_only
                or edit_route
            ):
                details.append(
                    "Notify customer 订单任务最后一步必须使用订单 Notes/Comment 表单：填写 message/comment，"
                    "勾选 Notify Customer by Email，并点击 Update/Submit Comment；路线必须定位到订单详情页的 "
                    "Comments History / Notes for this Order 区域，不能进入 Edit Order，也不能写成内部备注。"
                    f" actions={[(r.name, r.success_condition) for r in seq if r.kind == 'action']}"
                )
        elif assertion == "orders_keyword_search_submits_top_box":
            # WebArena 493 live 20260708_123304: after typing Grace Nguyen into the Orders
            # top Search by keyword box, the planner repeatedly clicked Filters / Apply Filters.
            # That only submits expanded column filters; the top keyword box needs Enter or the
            # inline magnifying-glass search button. If the plan uses top keyword search, make the
            # submission mechanism explicit so runtime does not infer the wrong control.
            seq = _flatten_runs(program.statements)
            filter_steps = [r for r in seq if r.kind == "filter"]
            top_keyword_steps = [
                r for r in filter_steps
                if any(
                    token in f"{r.name} {r.success_condition}".lower()
                    for token in ("search by keyword", "顶部搜索", "搜索框", "keyword")
                )
                and any(
                    key in f"{r.name} {r.success_condition}".lower()
                    for key in ("grace", "miller", "sarah")
                )
            ]
            submit_tokens = (
                "enter", "press enter", "回车", "提交", "submit", "search icon",
                "magnifying", "放大镜", "搜索图标", "搜索按钮", "点搜索",
            )
            bad_steps = [
                r for r in top_keyword_steps
                if not any(token in f"{r.name} {r.success_condition}".lower() for token in submit_tokens)
            ]
            if bad_steps:
                details.append(
                    "Orders 顶部 Search by keyword 路径必须显式写提交方式：按 Enter 或点击输入框内"
                    "放大镜/搜索图标；不能只写填入关键词，否则执行层容易误点 Filters/Apply Filters。"
                    f" keyword_filters={[(r.name, r.success_condition) for r in bad_steps]}"
                )
        elif assertion == "shopping_admin_theme_settings_via_content_design":
            # task 375「Go to the Magento Luma theme settings page」: 主题设置在
            # Content › Design › Themes(点 Magento Luma 行 → system_design_theme/edit/id/3),
            # 不在 System/Stores 菜单。旧失败计划去 SYSTEM 菜单找 Design → NOT_FOUND。
            # 判据=计划经 Content+Design/Themes(或直达 system_design_theme URL),否则违规。
            seq = _flatten_runs(program.statements)
            text = " ".join(
                f"{r.kind} {r.name} {r.success_condition} {r.read_spec}" for r in seq
            ).lower()
            via_content = ("content" in text) and ("design" in text or "theme" in text or "主题" in text)
            via_url = "system_design_theme" in text
            if not (via_content or via_url):
                details.append(
                    "「Magento Luma theme settings」导航必须经 Content › Design › Themes(点 Magento Luma 行)，"
                    "不要去 System/Stores 菜单找 Design;当前计划未见 Content/Design/Themes 路径: "
                    f"{[(r.kind, r.name) for r in seq]}"
                )
        elif assertion == "shopping_admin_show_report_is_navigate_no_returns":
            # task 707「Show the sales order report for last year (today Mar 15 2023)」:
            # 纯导航/展示意图 — 进 Reports › Sales › Orders、设日期范围、点 Show Report,
            # 报表渲染即终态。NetworkEvent 已通过;失败根因 = decompose 误当取数任务,
            # 给计划绑 returns ['total_orders','total_revenue'] → 空读→kickback 死循环→
            # 自报 RETRIEVE/DATA_VALIDATION_ERROR(期望 navigate/success)。
            # 判据①: 计划不得绑任何 returns、不得有 data_query(任务不要求返回字段)。
            # 判据②: 计划必须包含 Show Report(生成/渲染报表)动作。
            seq = _flatten_runs(program.statements)
            text = " ".join(
                f"{r.kind} {r.name} {r.success_condition} {r.read_spec}" for r in seq
            ).lower()
            has_returns = any(getattr(r, "returns", None) for r in seq)
            has_data_query = any(r.kind == "data_query" for r in seq)
            has_show_report = any(
                k in text for k in ("show report", "提交", "submit", "生成", "应用", "apply", "查看报")
            )
            if has_returns or has_data_query:
                offenders = [
                    (r.kind, r.name, getattr(r, "returns", None)) for r in seq
                    if getattr(r, "returns", None) or r.kind == "data_query"
                ]
                details.append(
                    "「Show the sales order report」是纯导航/展示意图,不要绑 returns 或 data_query"
                    "(任务不要求返回 total_orders/total_revenue 等字段,只需渲染报表);"
                    f"当前计划出现取数: {offenders}"
                )
            if not has_show_report:
                details.append(
                    "「Show the sales order report」计划必须包含点击 Show Report(渲染报表)的动作作为终态;"
                    f"当前计划未见: {[(r.kind, r.name) for r in seq]}"
                )
            # case 设 normalize=true 时,终态提交 action 必须被 normalize 成 navigate-submit dispatch
            # gate(含标记 "动作已发出且界面给出响应"),让确定性 url_changed 判 done、绕过会把 Magento
            # 渲染 URL(仍含 "filter")误读成"未提交"的 LLM checker —— 这是 707 反复点 Show Report 的根因修复。
            from gui_agent.core.supervisor.milestone.helpers import is_dispatch_gate_sc
            terminal_action = next(
                (r for r in reversed(seq) if r.kind in ("action", "filter")), None
            )
            if terminal_action is not None and not is_dispatch_gate_sc(terminal_action.success_condition):
                details.append(
                    "终态提交 action 应被 normalize 成 navigate-submit dispatch gate(success_condition 含"
                    "「动作已发出且界面给出响应」),使 url_changed 确定性判 done、不交给 LLM checker 误判渲染 URL;"
                    f"当前终态 action success_condition='{terminal_action.success_condition}'"
                )
        elif assertion == "variant_price_reads_current_before_set":
            # 778/780/782: percentage price change on a configurable-product variant. The new price
            # depends on the variant's CURRENT price (only known at runtime), so the plan must READ
            # the current Price before computing/setting it — not leave the fill target empty or
            # hardcode a number. Regression 778: milestone was "更新 Price 字段为 <空>" and the agent
            # submitted product[price]=100.00 (expected 64.88 = 75.00×0.865).
            seq = _flatten_runs(program.statements)
            # A foreach body_goal defers the per-row read→compute→fill to a runtime re-decompose (which
            # goes through THIS same decomposer + the value-binding rules), so its text — not explicit
            # Run/Compute nodes — carries the requirement. Accept either shape.
            body_goals = " ".join(_foreach_body_goals(program.statements))
            bg_reads_price = (("price" in body_goals.lower() or "价" in body_goals)
                              and any(m in body_goals for m in ("current", "现价", "当前", "读", "read"))
                              and any(m in body_goals for m in ("计算", "算", "降", "×", "*", "percent", "%")))

            def _reads_current_price(r: Run) -> bool:
                text = " ".join([r.name, r.read_spec, *(r.returns or [])]).lower()
                has_price = "price" in text or "价" in text
                has_read = any(m in text for m in ("current", "现价", "当前", "read", "读"))
                return has_price and has_read and bool((r.returns or []) or r.read_spec.strip())

            def _grid_price_collected(stmts: list) -> bool:
                # Third valid shape (offline 778 v3): the foreach COLLECTS the Price column off the
                # grid (returns=['sku','price',...]) and a body compute derives from the loop var
                # ({p[price]} * 0.865) — the grid Price of a simple-product variant row IS its
                # current price, so this reads-then-computes without a separate read step.
                for s in stmts:
                    if isinstance(s, ForEach):
                        has_price_col = any(("price" in f.lower() or "价" in f) for f in (s.returns or []))
                        body_computes = [b for b in s.body if isinstance(b, Compute)]
                        if has_price_col and any(s.var in c.expr for c in body_computes):
                            return True
                        if _grid_price_collected(s.body):
                            return True
                    elif isinstance(s, If) and (_grid_price_collected(s.then) or _grid_price_collected(s.otherwise)):
                        return True
                return False

            if (not any(_reads_current_price(r) for r in seq) and not bg_reads_price
                    and not _grid_price_collected(program.statements)):
                details.append(
                    "百分比调价未先读取变体当前 Price → 会凭空填/留空目标价（回归 778：milestone「更新 Price 为 空」、"
                    "实际提交 product[price]=100.00，期望 64.88=75.00×0.865）；计划应含「读当前 Price → 按系数算 → 填」"
                    "（显式 read+compute，或 foreach body_goal 里写明读现价+算）。"
                    f"当前步骤: {[(r.kind, r.name) for r in seq]}"
                )
            # And the computed value must be WIRED into the fill action as a bare {var} template —
            # a generic name ("更新为新值") gives the planner no concrete value and it hallucinates
            # one (778 live: computed 86.50, planner typed 150.00). Mirrors COMPUTE_VAR_UNUSED.
            # (Only for EXPLICIT computes; a body_goal defers this to the per-row re-decompose.)
            compute_vars = [c.var for c in _flatten_computes(program.statements) if c.var]
            if compute_vars and not any(
                ("{" + v + "}") in (r.name or "") for v in compute_vars for r in seq
            ):
                details.append(
                    "算出的新价没有以 {var} 模板接进填值动作名（如「将价格更新为 {new_price} 并保存」）——"
                    "泛指「新值」会让 planner 现场瞎猜（回归 778：算出 86.50、实际填 150.00）。"
                    f"compute vars: {compute_vars}; 动作: {[r.name for r in seq if r.kind == 'action']}"
                )
        elif assertion == "membership_judgment_semantic":
            # Skeleton-light principle (2026-07-02): member judgment must be delegated semantically
            # (body_goal states INTENT, the runtime agent judges against the REAL row), never baked
            # as a GUESSED literal predicate. Live 114429 wrote body_goal "若 SKU 不含 'size 28'
            # 则跳过" — actual SKUs are WP02-28-Blue, every row skipped, 0 saves. Flag quoted
            # multi-word literals used with containment mechanics against sku/name in body_goal
            # texts and compute exprs ("是否为 size 28 的变体" semantic phrasing stays legal).
            def _texts(stmts) -> list:
                out = []
                for s in stmts:
                    if isinstance(s, ForEach):
                        if getattr(s, "body_goal", ""):
                            out.append(("body_goal", s.body_goal))
                        out.extend(_texts(s.body))
                    elif isinstance(s, Compute):
                        out.append(("compute", s.expr))
                    elif isinstance(s, If):
                        out.extend(_texts(s.then))
                        out.extend(_texts(s.otherwise))
                return out

            _mechanic = re.compile(
                r"(不含|包含|含有|startswith|\bin\b|\bcontains?\b)\s*[（(]?\s*['\"]([^'\"]* [^'\"]*)['\"]"
                r"|['\"]([^'\"]* [^'\"]*)['\"]\s*(不在|在|\bin\b|\bnot in\b)"
            )
            for where, text in _texts(program.statements):
                m = _mechanic.search(text or "")
                if m:
                    lit = m.group(2) or m.group(3)
                    details.append(
                        f"{where} 把猜测的多词字面量谓词烤进了计划：用 {lit!r} 做包含性判定"
                        "（回归 114429：SKU 实为 WP02-28-Blue，'size 28' 永不命中→全部跳过 0 保存）。"
                        "成员判定必须写语义意图（「判断…是否为 size 28 的变体」），交给运行时看真实行的 agent 判。"
                    )
        elif assertion == "multi_variant_price_iterates":
            # 778 is multi-target: "size 28 Sahara leggings" = the 3 colour variants of size 28 (eval
            # expects 3 saves). With the router marking cardinality=set, the plan MUST foreach over the
            # matching variants — a single-variant linear plan updates at most 1/3 → score 0. And the
            # per-variant price calc must live INSIDE the foreach (each variant read→compute→fill), not
            # once at top level. NOTE: uses the MODULE-LEVEL _has_foreach — a nested redefinition
            # here made `_has_foreach` local to this whole function, so the earlier
            # uses_foreach_iteration branch hit UnboundLocalError (185 case crashed the eval).
            def _compute_in_foreach(stmts: list, in_fe: bool = False) -> bool:
                for s in stmts:
                    if isinstance(s, Compute) and in_fe:
                        return True
                    if isinstance(s, ForEach) and _compute_in_foreach(s.body, True):
                        return True
                    if isinstance(s, If) and (
                        _compute_in_foreach(s.then, in_fe) or _compute_in_foreach(s.otherwise, in_fe)
                    ):
                        return True
                return False

            if not _has_foreach(program.statements):
                details.append(
                    "cardinality=set 的变体改价必须 foreach 遍历所有匹配变体（778: size 28 = 3 个颜色变体，"
                    f"期望 3 次 save）；当前是单变体线性计划、最多改 1/3→score 0。步骤: {[type(s).__name__ for s in program.statements]}"
                )
            elif (_flatten_computes(program.statements)
                  and not _compute_in_foreach(program.statements)
                  and not _foreach_body_goals(program.statements)):
                # A body_goal foreach defers per-row read→compute→fill to runtime; only flag a
                # top-level explicit compute (runs once) when there is no body_goal to carry it.
                details.append(
                    "价格 compute 在 foreach 外（只算一次）——应在 foreach body 内对每个变体各读现价、各算、各填、各存。"
                )
        elif assertion == "out_of_stock_configurable_single_aggregate_mutation":
            # 505/502 家族："Mark all <配置型商品> as out of stock" = 对父产品的一次保存。
            # router 标 set；知识用 covers_set 把 set 坍缩成单次聚合 mutation。语义不变量：
            # ① 无 foreach（模糊回退结果集喂 foreach 会跨产品线批量误改——live 175322 事故）；
            # ② 恰有交互 action 步声明 covers_set（聚合覆盖的结构化声明，不是文字描述）；
            # ③ 纯 mutate：covers_set 那步不带 returns（静态 finish，durable 验收）。
            # 检索阶梯（精确原值→0 条回退 mention token）由 intent contracts 在编译期强制，
            # 本 case 能编译出厂本身就断言了它。
            if _has_foreach(program.statements):
                details.append(
                    "配置型商品标缺货不得 foreach 遍历匹配行（父产品一次保存覆盖全部变体；"
                    "模糊回退集喂 foreach 会跨产品线批量误改并超时——live 175322）。"
                )
            covers = [
                s for s in _flatten_runs(program.statements)
                if isinstance(s, Run) and getattr(s, "covers_set", "")
            ]
            if not covers:
                details.append(
                    "没有任何 mutation 步声明 covers_set——router 标 set 时，单次线性计划必须在"
                    "执行聚合动作（改 Stock Status 并保存）那一步上声明 covers_set=<实体提及>，"
                    "否则 set 契约会强制 foreach。"
                )
            elif any(s.returns for s in covers):
                details.append(
                    f"covers_set 聚合 mutation 步不应带 returns（纯 mutate 静态收尾）：{[s.name for s in covers if s.returns]}"
                )
        elif assertion == "shopping_admin_configurable_variant_two_phase_mutation":
            seq = _flatten_runs(program.statements)
            texts = [
                f"{run.name} {run.success_condition} {run.read_spec}".lower()
                for run in seq
            ]

            bad_name_filters = [
                run.name
                for run, text in zip(seq, texts)
                if run.kind == "filter"
                and "green" in text
                and "minerva" in text
                and any(token in text for token in ("name", "名称", "产品名"))
            ]
            if bad_name_filters:
                details.append(
                    "颜色是变体维度，不能并入配置型父商品 Name 筛选："
                    f"{bad_name_filters}"
                )

            attribute_mutation_indexes = [
                index
                for index, (run, text) in enumerate(zip(seq, texts))
                if run.kind == "action"
                and "xxxl" in text
                and any(token in text for token in ("size", "attribute", "属性", "option", "选项"))
                and any(token in text for token in ("保存", "持久化", "包含", "persist", "save"))
            ]
            config_mutation_indexes = [
                index
                for index, (run, text) in enumerate(zip(seq, texts))
                if run.kind == "action"
                and "xxxl" in text
                and any(token in text for token in ("green", "绿色", "绿 色"))
                and any(token in text for token in ("configuration", "matrix", "配置", "矩阵", "组合"))
                and any(token in text for token in ("保存", "持久化", "包含", "persist", "save"))
            ]
            if not attribute_mutation_indexes:
                details.append(
                    "缺少独立的 Size option mutation：应先让 Size 属性选项集合持久化包含 XXXL。"
                )
            else:
                first_attribute = min(attribute_mutation_indexes)
                owner_navigation = [
                    run
                    for index, (run, text) in enumerate(zip(seq, texts))
                    if index < first_attribute
                    and run.kind in {"navigation", "filter"}
                    and "size" in text
                    and any(token in text for token in ("attribute", "属性"))
                    and any(
                        token in text
                        for token in (
                            "既有", "现有", "打开", "定位", "编辑", "attribute code",
                        )
                    )
                ]
                if not owner_navigation:
                    details.append(
                        "Size option mutation 前没有定位并打开既有 Size attribute owner；"
                        "在属性列表页直接写『创建 Size 选项』会误点 Add New Attribute、创建重复属性。"
                    )
                attribute_run = seq[first_attribute]
                terminal = (attribute_run.success_condition or "").lower()
                if not (
                    "xxxl" in terminal
                    and any(token in terminal for token in ("option", "选项", "values", "swatch"))
                    and ("size" in terminal or owner_navigation)
                ):
                    details.append(
                        "Size option mutation 的 success_condition 没有验收 member=XXXL，且前序也没有"
                        "锁定 owner=Size；泛化成『动作有响应/status』会让保存了错误属性也通过验收。"
                    )
            if not config_mutation_indexes:
                details.append(
                    "缺少父商品 Configurations mutation：应让 Green/XXXL 组合生成并持久化。"
                )
            if (
                attribute_mutation_indexes
                and config_mutation_indexes
                and min(attribute_mutation_indexes) >= min(config_mutation_indexes)
            ):
                details.append("Size option mutation 必须先于父商品 Configurations mutation。")

            parent_editor_indexes = [
                index
                for index, (run, text) in enumerate(zip(seq, texts))
                if run.kind == "navigation"
                and any(token in text for token in ("product edit", "product workspace", "商品编辑", "产品编辑", "父商品"))
            ]
            if (
                attribute_mutation_indexes
                and parent_editor_indexes
                and min(parent_editor_indexes) < min(attribute_mutation_indexes)
            ):
                details.append(
                    "资源阶段没有按依赖拓扑排序：必须先持久化 Size option，再进入父商品编辑阶段。"
                )

            first_attribute = min(attribute_mutation_indexes, default=-1)
            config_stage_start = min(parent_editor_indexes, default=first_attribute)
            config_related_actions = [
                run.name
                for index, (run, text) in enumerate(zip(seq, texts))
                if index > config_stage_start
                and run.kind == "action"
                and any(token in text for token in ("configuration", "matrix", "green", "xxxl", "配置", "矩阵"))
            ]
            if len(config_related_actions) > 1:
                details.append(
                    "同一 Configurations 向导应是一个语义 mutation，展开/选择/保存由 Milestone 渐进执行；"
                    f"当前拆成了 {config_related_actions}"
                )

            scope_text = " ".join(
                f"{run.name} {run.success_condition} {getattr(run, 'sql', '')}".lower()
                for run in seq
            )
            if "configurable product" not in scope_text and "配置型" not in scope_text:
                details.append("父商品选择没有锁定 Type=Configurable Product。")
            ambiguous_parent_queries = [
                run.name
                for run, text in zip(seq, texts)
                if run.kind == "data_query"
                and any(token in text for token in ("configurable product", "配置型", "父商品"))
                and re.search(r"\blimit\s+1\b", getattr(run, "sql", "") or "", flags=re.I)
                and not re.search(r"\border\s+by\b", getattr(run, "sql", "") or "", flags=re.I)
            ]
            if ambiguous_parent_queries:
                details.append(
                    "父商品入口查询用裸 LIMIT 1 隐藏多候选歧义；应返回 match_count，"
                    f"仅在唯一时使用 URL：{ambiguous_parent_queries}"
                )

        elif assertion == "shopping_admin_review_count_writes_configurable_parent_short_description":
            # WebArena task-544 family (live 20260708_205937): the plan drifted into the product
            # edit page's own "Product Reviews" section and got stuck scrolling for it — reviews
            # are a child-record collection and must come from Reviews/All Reviews (see the
            # shopping_admin_review_rating_* assertions), not from inside the parent product's
            # detail page. Separately, webarena-verified's oracle for this task targets product
            # id=1108 type=configurable, field `product[short_description]`: Products retrieval by
            # name returns BOTH the configurable parent and its simple variants, and "product
            # description" in this app is the Short Description field, not the long Description
            # field. A plan that writes to whichever row a bare name search lands on, or to the
            # wrong field, mutates the wrong entity/field and the network-event evaluator finds no
            # matching POST.
            seq = _flatten_runs(program.statements)
            all_text = " ".join(
                f"{r.kind} {r.name} {r.success_condition} {r.read_spec} {getattr(r, 'sql', '')} "
                f"{' '.join(r.returns or [])}"
                for r in seq
            )
            stuck_in_product_detail = re.search(
                r"产品详情|商品详情|product\s+detail|product\s+workspace|滚动.*product\s+reviews|"
                r"滚动.*产品评论|scroll.*product\s+reviews",
                all_text,
                flags=re.I,
            )
            if stuck_in_product_detail:
                details.append(
                    "计划里出现『滚动/进入产品详情页找 Product Reviews』的路线；评论是子记录集合，"
                    "必须以 Reviews/All Reviews 为主数据源，产品详情页不是评论入口。"
                    f" seq={[(r.kind, r.name) for r in seq]}"
                )
            # NOTE (known DSL debt): "writes Short Description, not the long Description" can only be
            # asserted on step TEXT here — a DSL action step has no structured target_field slot, so
            # there is no dataflow identifier to key on (unlike the configurable filter below, which
            # keys on the data_query SQL). Tightening this needs a DSL change (a target_field on
            # mutation steps); until then it stays a text match — the weakest assertion in this case.
            mutates_description = [
                r for r in seq
                if r.kind == "action"
                and re.search(r"description|描述", f"{r.name} {r.success_condition}", flags=re.I)
            ]
            if not mutates_description:
                details.append(
                    "计划里没有看到写商品描述的 action 步；"
                    f"seq={[(r.kind, r.name) for r in seq]}"
                )
            elif not any(
                re.search(
                    r"short\s*description|short_description|简短描述|短描述",
                    f"{r.name} {r.success_condition}",
                    flags=re.I,
                )
                for r in mutates_description
            ):
                details.append(
                    "写商品描述必须点名 Short Description 字段（保存后是 product[short_description]），"
                    "不是通用的长 Description 字段；当前写描述步没有点名 Short Description。"
                    f" mutates_description={[(r.name, r.success_condition) for r in mutates_description]}"
                )
            # Dataflow judgment (NOT a name-text match): parent/child disambiguation is REALIZED
            # by a data_query that filters the normalized `type` column to Configurable
            # (=/LIKE/IN against a 'Configurable…' value). A step whose free-text name merely says
            # "configurable" is not evidence the plan acts on it — the LLM rephrases and the literal
            # word-list misses it (the exact字面词表 failure CLAUDE.md warns about). The SQL column
            # name + value are the deterministic dataflow identifiers, so assert on those.
            selects_configurable_parent = any(
                r.kind == "data_query"
                and re.search(
                    r"type\w*\s*(?:=|like|in)\s*\(?\s*['\"%]*configurable",
                    getattr(r, "sql", "") or "",
                    flags=re.I,
                )
                for r in seq
            )
            if not selects_configurable_parent:
                details.append(
                    "Products 按产品名检索命中的行常同时含配置型父商品与其简单变体；写描述前必须有一个 "
                    "data_query 对 type 列施加 Configurable 过滤（如 `WHERE type LIKE '%Configurable%'`）"
                    "来消歧锁定父行，而不能靠 step 描述里写“配置型”字样、或对检索结果 LIMIT 1 随意选一行——"
                    "选中变体会写错实体，evaluator 判定的目标行是配置型父商品。"
                    f" data_query_sql={[(getattr(r, 'sql', '') or '')[:80] for r in seq if r.kind == 'data_query']}"
                )
        else:
            details.append(f"unknown assertion: {assertion}")
    return details


def _load_case_knowledge(case: dict):
    platform = case.get("platform", "browser")
    app = case.get("knowledge_app") or case.get("site")
    include_skills = case.get("knowledge_profile") == "with_skills"
    if app:
        return load_knowledge_for_app(app, platform, include_skills=include_skills)
    return auto_discover_knowledge(
        case["goal"],
        platform,
        include_skills=include_skills,
    )


def _case_program(case: dict):
    k = _load_case_knowledge(case)
    screenshot_path = case.get("screenshot")
    png_bytes = None
    if screenshot_path:
        png_bytes = (PROJECT_ROOT / screenshot_path).read_bytes()
    # A case may pin a fixed Intent Resolution (entities precise/approximate + search key) so the
    # plan's column choice + exact→fuzzy ladder is tested deterministically, independent of the
    # resolver LLM (which has its own suite, evals/browser/intent_resolver).
    resolution = None
    if case.get("resolution"):
        from gui_agent.core.router import EntityRef, IntentResolution
        resolution = IntentResolution(entities=[EntityRef(**e) for e in case["resolution"]])
    program = decompose(
        case["goal"],
        png_bytes=png_bytes,
        knowledge=k.decompose_context(case["goal"]) if k else "",
        current_url=case.get("current_url", ""),
        current_title=case.get("current_title", ""),
        current_site=case.get("current_site") or (k.app_name if k and case.get("use_knowledge_app_as_current_site") else ""),
        table_summaries=case.get("table_summaries"),
        corrective_directive=case.get("corrective_directive", ""),
        resolution=resolution,
    )
    if case.get("normalize"):
        program = normalize_precondition_gates(normalize_confirm_read_gates(program))
    return program


def _dump_program(program) -> None:
    def _dump_stmts(stmts, indent: str = "       ") -> None:
        for s in stmts:
            if isinstance(s, RunLike):
                fields = f" returns={s.returns!r}" if s.returns else ""
                spec = f" read_spec={s.read_spec!r}" if s.read_spec else ""
                domains = (
                    f" return_domains={s.return_domains!r}"
                    if getattr(s, "return_domains", None) else ""
                )
                print(f"{indent}[{s.kind}] {s.name}: {s.success_condition}{fields}{spec}{domains}")
            elif isinstance(s, If):
                print(
                    f"{indent}[if] {s.cond.var}[{s.cond.field}] {s.cond.cmp} "
                    f"{s.cond.value!r} values={s.cond.values!r}"
                )
                if s.then:
                    print(f"{indent}  then:")
                    _dump_stmts(s.then, indent + "    ")
                if s.otherwise:
                    print(f"{indent}  else:")
                    _dump_stmts(s.otherwise, indent + "    ")
            elif isinstance(s, ForEach):
                row_fields = f" row_fields={s.row_fields}" if s.row_fields else ""
                output_fields = f" output_fields={s.output_fields}" if s.output_fields else ""
                returns = f" returns={s.returns}" if s.returns else ""
                print(
                    f"{indent}[foreach] {s.var} in {s.over} -> {s.into or s.var + 's'}"
                    f"{row_fields}{output_fields}{returns}"
                )
                if s.body_goal:
                    print(f"{indent}    [body_goal] {s.body_goal}")
                _dump_stmts(s.body, indent + "    ")
            elif isinstance(s, Finish):
                print(f"{indent}[finish] {s.message}")
            elif type(s).__name__ == "Compute":
                print(f"{indent}[compute] {s.var} = {s.expr}")
            elif type(s).__name__ == "Call":
                print(f"{indent}[call] {s.func}({s.args}) -> {s.var}")

    for fn in getattr(program, "functions", []) or []:
        print(f"  [def] {fn.name}({', '.join(fn.params)}) -> {fn.returns}")
        _dump_stmts(fn.body, "      ")
    _dump_stmts(program.statements)


def run_orchestrator_decompose_eval(label_filter: str = "", show_program: bool = False) -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    for c in cases:
        if label_filter and label_filter.lower() not in c["label"].lower():
            continue
        # A case may pin a screenshot fixture (e.g. WebArena #42's dashboard, whose visible
        # "Last Search Terms" widget is the bug trigger). Screenshots are gitignored
        # (evals/**/*.png), so on a fresh checkout the fixture is absent — SKIP such a case
        # (don't crash) instead of FAILing on a FileNotFoundError. Locally place the png to run it.
        shot = c.get("screenshot")
        if shot and not (PROJECT_ROOT / shot).exists():
            print(f"  [SKIP] {c['label']}")
            print(f"         截图缺失: {shot}（该 case 需本地放置截图；evals/**/*.png 被 gitignore 不入库）")
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
