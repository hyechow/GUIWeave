"""Browser orchestrator-decompose eval: user goal -> DSL Program (run / if / finish).

Tests `gui_agent.core.orchestrator.decomposer.decompose` — the **DSL program**
decomposer used by `bin/runner ... --orchestrator`. This is a DIFFERENT module from
the DAG decomposer in `evals/browser/decomposer/` (that one drives
`MilestoneSupervisorPolicy._decompose` and emits milestones; this one emits a Program
of run/if/finish statements).

Production-faithful:
  * knowledge auto-discovery on the goal (same as runner.py), and
  * screenshot-less — the orchestrator decomposes the goal BEFORE turn 1, so production
    calls `decompose(goal, knowledge=knowledge.navigation)` with no png (runner.py:1279).

Because there's no screenshot input, this eval needs no image fixtures (clean re: the
no-images-in-git rule). It calls the real LLM, so it's an on-demand eval (non-deterministic;
run it a few times when judging a prompt change).

Run:  uv run python evals/browser/orchestrator/test_orchestrator_decompose.py
"""

from __future__ import annotations

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
from gui_agent.core.orchestrator.program import TEMPLATE_RE
from gui_agent.core.self_learning.app_summary import auto_discover_knowledge

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
            # 登录/认证类前置应写【登录后即固定存在、与数据无关的认证标志】（用户名/头像/导航/标题），
            # 已登录则第一帧判 done 跳过。两种坏验收都会让已登录会话永远卡死：
            #  ① 回归 20260615_153314：「登录表单可见」（账号/密码框）——已登录回不去登录页。
            #  ② 回归 20260615_162312：「主内容含监控卡片/列表/数据」——数据(地图)还没加载就是空的，
            #     而加载数据正是被这个登录步堵在后面的步骤，循环依赖、不可达。
            auth_ms = [
                r for r in runs
                if any(k in (r.name + r.success_condition) for k in ("登录", "登入", "登陆", "认证"))
            ]
            form_bad = [
                (r.name, r.success_condition) for r in auth_ms
                if any(k in r.success_condition for k in ("账号", "密码", "登录按钮", "登录表单", "登录框"))
            ]
            data_bad = [
                (r.name, r.success_condition) for r in auth_ms
                if any(k in r.success_condition for k in ("卡片", "监控", "订单", "统计", "业务数据"))
            ]
            if form_bad:
                details.append(f"登录验收写成「登录表单可见」（已登录会话不可达，会卡死）: {form_bad}")
            if data_bad:
                details.append(
                    f"登录验收依赖业务数据内容（卡片/列表/数据等，无数据时为空、不可达，且常要等后续步骤才产生）: {data_bad}"
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
        else:
            details.append(f"unknown assertion: {assertion}")
    return details


def test_orchestrator_decompose() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    for c in cases:
        try:
            k = auto_discover_knowledge(c["goal"], "browser")
            program = decompose(c["goal"], knowledge=k.navigation if k else "")
        except Exception as e:  # noqa: BLE001
            _report(c["label"], False, f"exception: {e}")
            continue

        details = _check_basic(program, c["expected"])
        details.extend(_check_assertions(program, c.get("assertions", [])))
        ok = len(details) == 0
        _report(c["label"], ok, "; ".join(details) if details else "")
        if not ok:
            for s in program.statements:
                if isinstance(s, Run):
                    print(f"       [{s.kind}] {s.name}: {s.success_condition}")
                elif isinstance(s, If):
                    print(f"       [if] {s.cond.var}[{s.cond.field}] {s.cond.cmp} {s.cond.value!r}")
                    for b in (*s.then, *s.otherwise):
                        nm = getattr(b, "name", None) or getattr(b, "message", "")
                        print(f"          └ [{getattr(b, 'kind', b.op)}] {nm}")
                elif isinstance(s, Finish):
                    print(f"       [finish] {s.message}")


def main() -> int:
    print("── Browser Orchestrator-Decompose Eval ──")
    print("  测的是 decompose() 原始产出 = prompt(L1) 质量。confirm-read 的 dispatch 门在生产里")
    print("  由 engine.normalize_confirm_read_gates(L2) 确定性兜底保证（见 tests/test_orchestrator.py）；")
    print("  故这里 FAIL = prompt 可以更好（软信号），不是生产 bug。")
    test_orchestrator_decompose()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
