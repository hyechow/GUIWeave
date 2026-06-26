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

from gui_agent.core.orchestrator import Finish, ForEach, If, Run, decompose
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
        elif isinstance(s, ForEach):
            out.extend(_flatten_runs(s.body))
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


def _has_foreach(stmts: list) -> bool:
    for s in stmts:
        if isinstance(s, ForEach):
            return True
        if isinstance(s, If) and (_has_foreach(s.then) or _has_foreach(s.otherwise)):
            return True
    return False


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
        if isinstance(s, Run) and s.kind == "action":
            nxt = stmts[i + 1] if i + 1 < len(stmts) else None
            if s.returns or (isinstance(nxt, Run) and nxt.kind == "read"):
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
                (r.name, r.sql) for r in seq
                if r.kind == "data_query" and (
                    "->" in (r.sql or "")
                    or _sql_has_quoted_display_identifier(r.sql or "")
                )
            ]
            if offenders:
                details.append(
                    "data_query SQL 只能使用实际 normalized column identifiers，"
                    "不能把 schema 展示里的 Header->column 映射文本或带空格/标点的 quoted UI 表头写进 SQL: "
                    f"{offenders}"
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
                sql = (r.sql or "").lower()
                returns = [x.lower() for x in (r.returns or [])]
                has_month_alias = bool(re.search(r"\bas\s+month\b", sql))
                has_count_alias = bool(re.search(r"\bas\s+count\b", sql))
                has_month_names = all(name in sql for name in ("january", "february", "march", "april", "may"))
                if returns != ["result"] or not has_month_alias or not has_count_alias or not has_month_names:
                    offenders.append((r.name, r.returns, r.sql))
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
                sql = (r.sql or "").lower()
                repeats_page_filter = (
                    re.search(r"\bwhere\b.*\bstatus\b", sql, flags=re.DOTALL)
                    or "created_at >=" in sql
                    or "created_at <=" in sql
                    or "between" in sql and "created_at" in sql
                )
                if repeats_page_filter:
                    offenders.append((r.name, r.sql))
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
                for s in program.statements
                if isinstance(s, ForEach)
            }
            bad_sql = []
            for r in seq:
                if r.kind != "data_query" or not re.search(r"\brating\b|\b评分\b", r.sql or "", flags=re.I):
                    continue
                refs = {
                    raw.lower()
                    for raw in re.findall(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)\b", r.sql or "", flags=re.I)
                }
                if not (refs & foreach_tables):
                    bad_sql.append((r.name, r.sql))
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
                    if isinstance(s, Run):
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
            foreaches = [s for s in program.statements if isinstance(s, ForEach)]
            all_runs = _flatten_runs(program.statements)
            collection_fields: list[str] = []
            for fe in foreaches:
                if fe.over:
                    collection_fields += next(
                        (r.returns for r in all_runs if r.kind == "read" and r.var == fe.over), []
                    )
                else:
                    collection_fields += fe.returns  # new-style foreach: collects its own rows
            reads_url_col = any(f.lower().endswith("_url") for f in collection_fields)
            body_open_url = False
            body_open_id: list[str] = []
            for fe in foreaches:
                for b in fe.body:
                    if isinstance(b, Run) and any(k in b.name for k in ("打开", "open", "进入", "详情")):
                        if re.search(r"\{row\[[^\]]*_url\]\}", b.name, re.I):
                            body_open_url = True
                        elif re.search(r"\{row\[", b.name):
                            body_open_id.append(b.name)
            if not foreaches:
                details.append(
                    "无 foreach，无法校验逐行 URL 直达（browser 逐行钻取应先采集候选行再 foreach→data_query）"
                )
            else:
                if not reads_url_col:
                    details.append(
                        "采集步未读出任何 `<列>_url` 详情链接列（browser 逐行钻取应默认读 Action_url "
                        "以走 URL 直达，规则11②；本 case 无表头→默认 Action_url）: "
                        f"collection_fields={collection_fields}"
                    )
                if not body_open_url:
                    details.append(
                        "foreach 打开步未用 `{row[..._url]}` URL 直达，而是按 id 在界面逐条点开"
                        "（违反 browser 非交互优先；这正是 20260625_145506 评分1.0却点击式钻取的回归）: "
                        f"{body_open_id}"
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
        knowledge=k.navigation if k else "",
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
            if isinstance(s, Run):
                fields = f" returns={s.returns!r}" if s.returns else ""
                spec = f" read_spec={s.read_spec!r}" if s.read_spec else ""
                print(f"{indent}[{s.kind}] {s.name}: {s.success_condition}{fields}{spec}")
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
                print(f"{indent}[foreach] {s.var} in {s.over} -> {s.into or s.var + 's'}")
                _dump_stmts(s.body, indent + "    ")
            elif isinstance(s, Finish):
                print(f"{indent}[finish] {s.message}")

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
