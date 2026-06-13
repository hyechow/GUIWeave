"""Browser decomposer eval: validates milestone structure from goal + screenshot.

Mirrors evals/android/decomposer; browser-specific extras: production-parity knowledge
injection (auto_discover on the goal) and @<file> config references resolved by decompose.

Run:  uv run python evals/browser/decomposer/test_decomposer.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)  # @<file> refs in goals resolve relative to the repo root

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from gui_agent.adapters.browser.supervisor.milestone.prompts import BROWSER_MILESTONE_PROMPTS
from gui_agent.core.schemas import Observation
from gui_agent.core.self_learning.app_summary import auto_discover_knowledge
from gui_agent.core.supervisor.milestone import MilestoneSupervisorPolicy

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


def _milestone_text(m) -> str:
    return f"{m.name} {m.description} {m.success_condition}"


def _check_basic(milestones: list, task_type: str, expected: dict) -> list[str]:
    details: list[str] = []
    if "task_type" in expected and task_type != expected["task_type"]:
        details.append(f"task_type: expected {expected['task_type']!r}, got {task_type!r}")
    if "min_milestones" in expected and len(milestones) < expected["min_milestones"]:
        details.append(f"expected >={expected['min_milestones']} milestones, got {len(milestones)}")
    return details


def _check_assertions(milestones: list, constraints: list[str], assertions: list[str]) -> list[str]:
    details: list[str] = []
    all_text = " ".join(_milestone_text(m) for m in milestones)
    for assertion in assertions:
        if assertion == "conditional_terminal_acceptance":
            # 「确认X，不足才创建」的条件分支安全形态，二选一：
            #  A. 保留确认/检查步（Guard 没删 或 分解为 navigation+检查语义）
            #  B. 创建类 milestone 的验收写【终态】（"至少两台…在线"——已满足时 checker 直接
            #     判 done 跳过创建），而非【增量】（"新增两台"——已在线场景下永不成立且会多建）
            has_confirm = any(
                any(kw in _milestone_text(m) for kw in ("确认", "检查", "核对", "查看"))
                and "在线" in _milestone_text(m)
                for m in milestones
            )
            create_ms = [m for m in milestones if any(kw in _milestone_text(m) for kw in ("创建", "新建", "添加"))]
            terminal_ok = bool(create_ms) and all(
                # 终态量词:两台/2台/至少…,或直接点名两个目标机器人
                (re.search(r"(至少|不少于)?\s*(两台|2\s*台)", m.success_condition)
                 or ("robot-10002" in m.success_condition and "robot-10003" in m.success_condition))
                # 状态词:在线 或 该页实际标签「启用」(见 enable_acceptance_uses_ui_status_word)
                and re.search(r"在线|启用", m.success_condition)
                and not re.search(r"新增|增加了", m.success_condition)
                for m in create_ms
            )
            if not (has_confirm or terminal_ok):
                got = [(m.name, m.success_condition) for m in milestones]
                details.append(f"条件分支坍塌：无确认步且创建验收非终态 (got: {got})")
        elif assertion == "no_hallucinated_robot_names":
            # 配置只给前缀 robot-；goal 只点名 10002/10003。验收里编造其它具体编号
            # （如 robot-10004）会让执行期 checker 永不通过。
            invented = sorted({
                name for name in re.findall(r"robot-\d+", all_text)
                if name not in ("robot-10002", "robot-10003")
            })
            if invented:
                details.append(f"验收幻觉了配置中不存在的机器人编号: {invented}")
        elif assertion == "virtual_robot_module_not_confused":
            # 回归 20260612_093022：「机器人」(真实设备,机器人列表/手动添加=按IP注册) 与
            # 「虚拟机器人」(模拟器,工具菜单下) 是不同模块。goal 要虚拟机器人,分解却指向
            # 裸「机器人列表」→ planner 进错模块还手动添加真实设备。判据:提到 机器人列表/
            # 手动添加 的 milestone 必须同时带「虚拟」限定。
            confused = [
                m.name for m in milestones
                if any(kw in _milestone_text(m) for kw in ("机器人列表", "手动添加"))
                and "虚拟" not in _milestone_text(m)
            ]
            if confused:
                details.append(f"指向真实机器人模块而非虚拟机器人(工具菜单): {confused}")
        elif assertion == "robot_move_uses_control_surface":
            # 回归 20260612_111555(2026-06-12 用户拍板:操作/移动机器人统一走场控页,知识勘误层
            # 已只保留该入口)。正典路径=场控地图点机器人→详情面板→[模拟器操作]→更改位置=站点。
            # 判据:① 必须落在控制面(更改位置/模拟器操作);② 必须带场控/地图语义(不得锚定
            # 虚拟机器人列表页);③ 不得把「编辑」(参数配置弹窗,无移动功能)当移动入口。
            uses_control_surface = any(
                any(kw in _milestone_text(m) for kw in ("更改位置", "模拟器操作"))
                for m in milestones
            )
            if not uses_control_surface:
                got = [(m.name, m.description[:40]) for m in milestones]
                details.append(f"没有 milestone 使用移动位置的控制面(更改位置/[模拟器操作]): {got}")
            on_field_control = any(
                ("场控" in _milestone_text(m) or "地图" in _milestone_text(m))
                for m in milestones
            )
            if not on_field_control:
                details.append("缺少场控/地图语义——移动机器人必须在场控页进行")
            edit_as_mover = [
                m.name for m in milestones
                if "编辑" in _milestone_text(m)
                and any(kw in _milestone_text(m) for kw in ("位置", "移动", "站点"))
            ]
            if edit_as_mover:
                details.append(f"把「编辑」弹窗当成了移动位置入口: {edit_as_mover}")
        elif assertion == "connectivity_verdict_states":
            # 回归 20260612_121718:验收态定义依赖知识库——连通性检测的真实终态是
            # 「连通=地图路径高亮(无文字判定)/不可达=出提示」,且「机器人群组」实测必填
            # (不选则静默无结果),「记录原因」要求把判定读出来。勘误层写明后,分解应:
            # ① 检测类 milestone 的验收用这两个标志;② 群组选择成为前置步;③ 有读取/
            # 记录结果的步骤(goal 要求记录原因)。
            detect_ms = [m for m in milestones if "检测" in _milestone_text(m) and m.kind == "action"]
            if not detect_ms:
                details.append("缺少执行检测的 action milestone")
            elif not any(
                ("高亮" in m.success_condition or "不可达" in m.success_condition)
                for m in detect_ms
            ):
                details.append(
                    f"检测验收未使用知识定义的终态标志(高亮/不可达提示): "
                    f"{[(m.name, m.success_condition) for m in detect_ms]}"
                )
            # 「机器人群组」为可选（2026-06-12 用户确认,早前「必填」是误判——回归 20260612_221523:
            # 分解把群组写进验收必要条件,goal 又没给群组名,planner 编造「目标群组名称」输入过滤,
            # 候选清空死锁 10 轮）。goal 未指定群组时,验收不得要求群组已选择/填入。
            group_required = [
                m.name for m in milestones
                if re.search(r"(填入|选择|设置|选定).{0,8}群组|群组.{0,10}(已|被)(正确)?(填入|选择|设置|选定)", m.success_condition)
            ]
            if group_required:
                details.append(f"goal 未指定群组,验收却把「选择群组」设为必要条件: {group_required}")
            if not any(
                any(kw in _milestone_text(m) for kw in ("记录", "读取", "原因"))
                for m in milestones
            ):
                details.append("缺少读取/记录检测结果的步骤(goal 要求不可达记录原因)")
        elif assertion == "enable_acceptance_uses_ui_status_word":
            # 回归 20260612_101639:虚拟机器人页状态列只有「启用/停用」,「启用」即已加入调度
            # (知识 s15 与 _deploy 辨析都写明)。decompose 把 goal 口语词「在线」硬化成验收
            # 标签 →checker 字面派永不通过,8 轮全在追一个该页不存在的标签(车其实早建好且
            # 已启用)。判据:启用/调度类 milestone 的验收要求「在线/调度中」时必须同时含
            # 「启用」(实际 UI 词)或「以页面实际」(语义兜底),否则视为标签硬化。
            bad = [
                m.name for m in milestones
                if any(kw in (m.name + m.description) for kw in ("启用", "调度"))
                and any(kw in m.success_condition for kw in ("在线", "调度中"))
                and "启用" not in m.success_condition
                and "以页面实际" not in m.success_condition
            ]
            if bad:
                details.append(f"启用/调度验收硬化了页面不存在的状态标签(在线/调度中): {bad}")
        elif assertion == "config_fields_reach_planner_channel":
            # @配置的字段值必须到达执行期可见通道（global_constraints）。靠 LLM 蒸馏不稳定
            # （实测同一配置有时 8 字段全进、有时 0 进）；代码层兜底后此断言应稳定通过。
            ctext = " ".join(constraints)
            markers = ["robot-", "STD", "80", "1000"]
            missing = [m for m in markers if m not in ctext]
            if missing:
                details.append(f"配置字段未到达 constraints（缺 {missing}）: {constraints}")
        else:
            details.append(f"unknown assertion: {assertion}")
    return details


def test_decomposer() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    for c in cases:
        screenshot_path = PROJECT_ROOT / c["screenshot"]
        if not screenshot_path.exists():
            _report(c["label"], False, f"screenshot not found: {c['screenshot']}（本地截图不入 git，需自备）")
            continue

        observation = Observation(png_bytes=screenshot_path.read_bytes(), source="eval")
        try:
            policy = MilestoneSupervisorPolicy(prompts=BROWSER_MILESTONE_PROMPTS)
            # Production parity: knowledge auto-discovery on the goal (no-op if the local
            # knowledge dir doesn't have the app).
            k = auto_discover_knowledge(c["goal"], "browser")
            if k and hasattr(policy, "set_app_knowledge"):
                policy.set_app_knowledge(k.navigation, app_name=k.app_name, elements=k.elements, sections=k.sections)
            policy._decompose(c["goal"], observation)
            milestones = [policy._milestones[mid] for mid in policy._order]
        except Exception as e:  # noqa: BLE001
            _report(c["label"], False, f"exception: {e}")
            continue

        details = _check_basic(milestones, policy.task_type, c["expected"])
        details.extend(_check_assertions(milestones, policy._global_constraints, c.get("assertions", [])))
        ok = len(details) == 0
        _report(c["label"], ok, "; ".join(details) if details else "")
        if not ok:
            for m in milestones:
                print(f"       [{m.kind}/{m.completion_strategy}] {m.name}: {m.success_condition}")
            print(f"       constraints: {policy._global_constraints}")


def main() -> int:
    print("── Browser Decomposer Eval ──")
    test_decomposer()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
