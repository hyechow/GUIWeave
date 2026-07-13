"""Goal decomposition and decomposition repair for the milestone supervisor."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from gui_agent.context.runtime import (
    browser_page_block,
    feedback_block,
    file_reference_block,
    knowledge_block,
    task_goal_block,
)
from gui_agent.core.llm.messages import assemble_messages
from llm.structured import invoke_structured
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.schemas import Milestone, Observation

from .model_io import resolve_file_refs
from .runtime import _is_loop
from .schemas import _DecomposeResponse, _StopConditionPatch

_AM_WORDS = ("上午", "早上", "早晨", "清晨")
_PM_WORDS = ("下午", "晚上", "傍晚", "夜晚")
_TIME_ENTITY_WORDS = ("闹钟", "提醒", "日程", "会议", "预约", "时间", "alarm", "reminder", "schedule", "meeting")

_ANALYSIS_KEYWORDS = ("多少", "什么", "有没有", "查看", "看看", "统计", "查一下", "帮我找", "列出", "汇总", "比较")
_ACTION_VERBS = (
    "新建", "创建", "建单", "建一个", "提交", "启用", "停用", "添加", "删除", "修改", "设置", "设为",
    "放到", "移动", "发送", "购买", "登录", "注册", "上传", "下单", "派发", "下达", "编辑", "切换",
    "保存", "配置", "加入调度", "上线", "绑定", "开启", "关闭",
)

_VALUE_CONVERGE_CONTROL_WORDS = (
    "picker",
    "滚轮",
    "选择器",
    "步进器",
    "滑块",
    "spinner",
)
_VALUE_SET_WORDS = ("设置为", "设为", "调到", "调整为", "改为", "选到", "显示为", "设定为")
_VALUE_DOMAIN_WORDS = (
    "时间",
    "日期",
    "闹钟",
    "小时",
    "分钟",
    "上午",
    "下午",
    "am",
    "pm",
    "数量",
    "数值",
    "音量",
    "亮度",
    "比例",
    "百分比",
    "档",
    "级",
    "年",
    "月",
    "日",
)

_WEEKDAY_ALIASES = {
    "周一": ("周一", "星期一", "礼拜一"),
    "周二": ("周二", "星期二", "礼拜二"),
    "周三": ("周三", "星期三", "礼拜三"),
    "周四": ("周四", "星期四", "礼拜四"),
    "周五": ("周五", "星期五", "礼拜五"),
    "周六": ("周六", "星期六", "礼拜六"),
    "周日": ("周日", "周天", "星期日", "星期天", "礼拜日", "礼拜天"),
}


def _looks_like_analysis(goal: str) -> bool:
    """True only for read/compute-purpose goals; query words among action verbs stay action."""
    has_query = any(kw in goal for kw in _ANALYSIS_KEYWORDS)
    has_action = any(kw in goal for kw in _ACTION_VERBS)
    return has_query and not has_action


@dataclass(frozen=True)
class _GoalValueConstraint:
    field: str
    target: str
    rejects: str = ""
    aliases: tuple[str, ...] = ()
    trigger_words: tuple[str, ...] = ()

    def global_text(self) -> str:
        reject = f"；{self.rejects} 不算完成" if self.rejects else ""
        return f"目标字段「{self.field}」：{self.target}{reject}"

    def present_in(self, text: str) -> bool:
        lowered = text.lower()
        if self.target and self.target in text:
            return True
        for alias in self.aliases:
            if alias in text or alias.lower() in lowered:
                return True
        return False


class MilestoneDecompositionMixin:
    _MAX_DECOMPOSE_RETRIES = 2

    def _decompose(self, goal: str, observation: Observation) -> None:
        self._goal = goal
        cfg = resolve_llm_config("supervisor.decompose")
        if not cfg.model:
            cfg = resolve_llm_config("supervisor")
        print(f"Supervisor: {cfg.provider} / {cfg.model}")
        llm = ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url)

        file_section = resolve_file_refs(goal)

        issues: list[str] = []
        for attempt in range(self._MAX_DECOMPOSE_RETRIES + 1):
            self._do_decompose(llm, goal, observation, issues, file_section)
            issues = self._validate_decomposition(goal)
            if not issues:
                break
            if attempt < self._MAX_DECOMPOSE_RETRIES:
                print(f"  [Guard] 分解校验发现 {len(issues)} 项问题，重试 ({attempt+1}/{self._MAX_DECOMPOSE_RETRIES})...")
                for i in issues:
                    print(f"  [Guard]   {i}")

        self._patch_decomposition(llm, goal)

        if file_section:
            _CAP = 3000
            snippet = (
                file_section if len(file_section) <= _CAP
                else file_section[:_CAP] + "\n…（配置过长已截断，其余以分解结果为准）"
            )
            self._static_constraints.append(snippet)

        if self._current_id not in self._milestones:
            self._current_id = self._next_milestone()

        print(f"任务分解为 {len(self._milestones)} 个子目标：")
        for mid in self._order:
            m = self._milestones[mid]
            deps = f" (依赖: {m.depends_on})" if m.depends_on else ""
            machine = "loop" if _is_loop(m) else "single"
            print(f"  [{m.id}][{machine}] {m.name}{deps}")
            print(f"       验收：{m.success_condition}")
            if m.scroll_stop_condition:
                print(f"       停止条件：{m.scroll_stop_condition}")

    def _do_decompose(
        self, llm: ChatOpenAI, goal: str, observation: Observation,
        feedback: list[str], file_section: str = "",
    ) -> None:
        context_blocks = [
            task_goal_block(goal),
            file_reference_block(file_section),
        ]
        if observation.url:
            site = ""
            try:
                from gui_agent.core.self_learning.app_summary import match_app_by_url
                site = match_app_by_url(observation.url, observation.source) or ""
            except Exception:
                site = ""
            context_blocks.append(browser_page_block(observation.url, observation.title, site=site))
        context_blocks.extend([
            knowledge_block("app_navigation", self._app_knowledge),
            feedback_block(feedback),
        ])
        msgs = assemble_messages(
            self._prompts.decompose,
            observation,
            human_blocks=context_blocks,
            image_resize=self._prompts.image_resize,
            label="milestone.decompose",
            context_reports=getattr(self, "_context_reports", None),
        )
        resp = invoke_structured(
            llm,
            msgs,
            _DecomposeResponse,
            trace_sink=getattr(self, "_context_reports", None),
            trace_label="milestone.decompose",
        )

        self._static_constraints = resp.global_constraints
        self.task_type = resp.task_type
        self._milestones = {m.id: m for m in resp.milestones}
        self._order = [m.id for m in resp.milestones]
        self._current_id = self._next_milestone()

    def _validate_decomposition(self, goal: str) -> list[str]:
        issues = []
        all_ids = set(self._milestones.keys())

        for m in self._milestones.values():
            for dep in m.depends_on:
                if dep not in all_ids:
                    issues.append(f"子目标「{m.name}」的 depends_on 包含不存在的 ID: {dep}")

        visited: set[str] = set()
        in_stack: set[str] = set()

        def _has_cycle(mid: str) -> bool:
            if mid in in_stack:
                return True
            if mid in visited:
                return False
            visited.add(mid)
            in_stack.add(mid)
            ms = self._milestones.get(mid)
            if ms:
                for dep in ms.depends_on:
                    if _has_cycle(dep):
                        return True
            in_stack.discard(mid)
            return False

        for mid in list(self._order):
            visited.clear()
            in_stack.clear()
            if _has_cycle(mid):
                issues.append(f"子目标之间存在循环依赖（从 {mid} 开始）")

        for m in self._milestones.values():
            if not m.success_condition.strip():
                issues.append(f"子目标「{m.name}」的验收条件为空")

        for m in self._milestones.values():
            if m.kind == "collection" and m.completion_strategy not in ("read_once", "scroll_until_boundary"):
                issues.append(f"子目标「{m.name}」kind=collection 但 completion_strategy={m.completion_strategy}，应为 read_once 或 scroll_until_boundary")

        for m in self._milestones.values():
            if m.completion_strategy == "scroll_until_boundary" and not m.scroll_stop_condition:
                issues.append(f"子目标「{m.name}」使用 scroll_until_boundary 但缺少 scroll_stop_condition")

        if self.task_type == "action" and _looks_like_analysis(goal):
            issues.append("task_type=action 但目标是纯查询/分析（含查询词、无动作动词），应为 analysis")

        for m in self._milestones.values():
            sc = m.success_condition
            if re.search(r"新增了|增加了|多出", sc):
                issues.append(
                    f"子目标「{m.name}」验收用了增量表述（{sc[:30]}…）：改写为完成后应处于的终态"
                    "（如「列表中至少有 N 个符合要求的条目」），不要写相对变化"
                )
            if m.kind == "action" and re.search(r"(弹出|展开|聚焦|打开).{0,6}(窗口|弹窗|对话框|下拉|面板)(?!.*(成功|完成|已|结果))", sc):
                issues.append(
                    f"子目标「{m.name}」验收停在中间态（{sc[:30]}…）：action 验收要写操作的最终可见结果"
                    "（提交后的成功提示/状态更新/结果），不是「弹出某弹窗」这类过程态"
                )

        if re.search(r"记录|报告|原因|结果说明", goal):
            has_read_step = any(
                m.kind == "collection" or any(kw in f"{m.name}{m.description}" for kw in ("记录", "读取", "获取结果", "原因"))
                for m in self._milestones.values()
            )
            if not has_read_step:
                issues.append("目标要求记录/报告结果或原因，但没有 collection 或读取结果的子目标——补一个读取/记录判定的步骤")

        return issues

    @staticmethod
    def _looks_like_value_converge_milestone(m: Milestone) -> bool:
        text = f"{m.name}\n{m.description}\n{m.success_condition}".lower()
        if any(w in text for w in _VALUE_CONVERGE_CONTROL_WORDS):
            return any(w in text for w in _VALUE_SET_WORDS)
        has_set_word = any(w in text for w in _VALUE_SET_WORDS)
        has_domain = any(w.lower() in text for w in _VALUE_DOMAIN_WORDS)
        has_value = bool(re.search(r"\d|am|pm|上午|下午|%", text))
        return has_set_word and has_domain and has_value

    @staticmethod
    def _goal_period_constraint(goal: str) -> Optional[_GoalValueConstraint]:
        text = goal.lower()
        has_am = any(w in goal for w in _AM_WORDS) or bool(re.search(r"\b(?:a\.?m\.?)\b", text))
        has_pm = any(w in goal for w in _PM_WORDS) or bool(re.search(r"\b(?:p\.?m\.?)\b", text))
        if has_am and not has_pm:
            return _GoalValueConstraint(
                field="时段",
                target="上午/早上/AM",
                rejects="下午/晚上/傍晚/PM",
                aliases=("上午", "早上", "早晨", "清晨", "AM", "am"),
                trigger_words=("上午", "早上", "AM", "时段", "上午/下午"),
            )
        if has_pm and not has_am:
            return _GoalValueConstraint(
                field="时段",
                target="下午/晚上/傍晚/PM",
                rejects="上午/早上/AM",
                aliases=("下午", "晚上", "傍晚", "夜晚", "PM", "pm"),
                trigger_words=("下午", "晚上", "PM", "时段", "上午/下午"),
            )
        return None

    @staticmethod
    def _goal_repeat_constraint(goal: str) -> Optional[_GoalValueConstraint]:
        if "工作日" in goal:
            return _GoalValueConstraint(
                field="重复规则",
                target="工作日/周一至周五",
                rejects="周末/每天/不重复",
                aliases=("工作日", "周一至周五", "周一到周五", "星期一至星期五"),
                trigger_words=("重复", "工作日", "周一", "周五", "星期"),
            )
        if "周末" in goal:
            return _GoalValueConstraint(
                field="重复规则",
                target="周末/周六周日",
                rejects="工作日/每天/不重复",
                aliases=("周末", "周六周日", "周六和周日", "星期六星期日"),
                trigger_words=("重复", "周末", "周六", "周日", "星期"),
            )
        if any(w in goal for w in ("每天", "每日", "天天")):
            return _GoalValueConstraint(
                field="重复规则",
                target="每天/每日",
                rejects="不重复/仅一次",
                aliases=("每天", "每日", "天天"),
                trigger_words=("重复", "每天", "每日"),
            )

        days: list[str] = []
        for canonical, aliases in _WEEKDAY_ALIASES.items():
            if any(alias in goal for alias in aliases):
                days.append(canonical)
        if days:
            target = "/".join(days)
            return _GoalValueConstraint(
                field="重复规则",
                target=target,
                rejects="其他星期/不重复",
                aliases=tuple(days),
                trigger_words=("重复", "星期", "周", *days),
            )
        return None

    @staticmethod
    def _goal_named_value_constraint(goal: str) -> Optional[_GoalValueConstraint]:
        match = re.search(
            r"(?:名称|名字|标题|备注|标签|闹钟名|提醒名)"
            r"(?:设置为|设为|命名为|改为|叫|为)"
            r"[「“\"']?([^」”\"'，。；;]+)",
            goal,
        )
        if not match:
            return None
        value = match.group(1).strip()
        if not value or len(value) > 30:
            return None
        return _GoalValueConstraint(
            field="名称/标签",
            target=value,
            aliases=(value,),
            trigger_words=("名称", "名字", "标题", "备注", "标签", "闹钟名", "提醒名"),
        )

    @staticmethod
    def _looks_like_time_target_milestone(m: Milestone) -> bool:
        text = f"{m.name}\n{m.description}\n{m.success_condition}".lower()
        has_clock_value = bool(
            re.search(
                r"\d{1,2}\s*[:：]\s*\d{1,2}|\d{1,2}\s*(?:点|时)(?:\s*\d{1,2}\s*分?)?",
                text,
            )
        )
        if has_clock_value:
            return True
        has_time_domain = any(w in text for w in (*_TIME_ENTITY_WORDS, "小时", "分钟", "time"))
        has_numeric_value = bool(re.search(r"\d", text))
        return has_time_domain and has_numeric_value

    @staticmethod
    def _extract_goal_value_constraints(goal: str) -> list[_GoalValueConstraint]:
        constraints: list[_GoalValueConstraint] = []
        for c in (
            MilestoneDecompositionMixin._goal_period_constraint(goal),
            MilestoneDecompositionMixin._goal_repeat_constraint(goal),
            MilestoneDecompositionMixin._goal_named_value_constraint(goal),
        ):
            if c is not None and c.global_text() not in {x.global_text() for x in constraints}:
                constraints.append(c)
        return constraints

    def _goal_constraint_applies_to_milestone(self, constraint: _GoalValueConstraint, m: Milestone) -> bool:
        text = f"{m.name}\n{m.description}\n{m.success_condition}"
        if any(w in text for w in constraint.trigger_words):
            return True
        return self._looks_like_time_target_milestone(m)

    def _patch_goal_value_constraints(self, goal: str, fixes: list[str]) -> None:
        constraints = self._extract_goal_value_constraints(goal)
        if not constraints:
            return

        for constraint in constraints:
            global_text = constraint.global_text()
            if global_text not in self._static_constraints:
                self._static_constraints.append(global_text)
                fixes.append(f"补充目标字段约束「{constraint.field}={constraint.target}」")

        patched: set[tuple[str, str]] = set()
        for m in self._milestones.values():
            if m.kind not in ("action", "filter"):
                continue
            for constraint in constraints:
                if not self._goal_constraint_applies_to_milestone(constraint, m):
                    continue
                text = f"{m.name}\n{m.description}\n{m.success_condition}"
                if constraint.present_in(text):
                    continue
                reject = f"，不能是{constraint.rejects}" if constraint.rejects else ""
                if any(w in text for w in ("列表", "条目", "返回", "新增", "出现")):
                    m.success_condition = (
                        f"{m.success_condition}（结果必须同时满足"
                        f"{constraint.field}={constraint.target}{reject}）"
                    )
                else:
                    m.description = (
                        f"{m.description} 同时必须设置{constraint.field}为{constraint.target}{reject}。"
                    )
                    m.success_condition = (
                        f"{m.success_condition}（必须同时满足"
                        f"{constraint.field}={constraint.target}{reject}）"
                    )
                key = (m.id, constraint.field)
                if key not in patched:
                    fixes.append(f"子目标「{m.name}」补充目标字段「{constraint.field}={constraint.target}」")
                    patched.add(key)

    def _patch_decomposition(self, llm: ChatOpenAI, goal: str) -> None:
        fixes = []

        verification_ids = [
            mid for mid, m in self._milestones.items()
            if m.kind == "verification"
        ]
        for vid in verification_ids:
            m = self._milestones[vid]
            if re.search(r"记录|读取|读出|获取|查看|报告|汇报", f"{m.name}{m.description}{m.success_condition}"):
                m.kind = "collection"
                m.completion_strategy = "read_once"
                fixes.append(f"子目标「{m.name}」（verification→collection）按读取/记录语义拯救")
                continue
            removed = self._milestones.pop(vid)
            self._order.remove(vid)
            for other in self._milestones.values():
                if vid in other.depends_on:
                    other.depends_on.remove(vid)
                    other.depends_on.extend(removed.depends_on)
            fixes.append(f"子目标「{removed.name}」（verification）已移除")

        all_ids = set(self._milestones.keys())
        for m in self._milestones.values():
            invalid = [d for d in m.depends_on if d not in all_ids]
            if invalid:
                m.depends_on = [d for d in m.depends_on if d in all_ids]
                fixes.append(f"子目标「{m.name}」移除无效依赖 {invalid}")

        visited: set[str] = set()
        in_stack: set[str] = set()

        def _has_cycle(mid: str) -> bool:
            if mid in in_stack:
                return True
            if mid in visited:
                return False
            visited.add(mid)
            in_stack.add(mid)
            ms = self._milestones.get(mid)
            if ms:
                for dep in ms.depends_on:
                    if _has_cycle(dep):
                        return True
            in_stack.discard(mid)
            return False

        for mid in self._order:
            visited.clear()
            in_stack.clear()
            if _has_cycle(mid):
                self._milestones[mid].depends_on = []
                fixes.append(f"清除子目标「{self._milestones[mid].name}」的依赖以打破循环")

        for m in self._milestones.values():
            if not m.success_condition.strip():
                m.success_condition = f"完成「{m.name}」"
                fixes.append(f"子目标「{m.name}」补全空的验收条件")

        for m in self._milestones.values():
            if m.kind == "collection" and m.completion_strategy not in ("read_once", "scroll_until_boundary"):
                m.completion_strategy = "scroll_until_boundary"
                fixes.append(f"子目标「{m.name}」策略修正为 scroll_until_boundary")

        for m in self._milestones.values():
            if (
                m.kind in ("action", "filter")
                and m.completion_strategy == "visible_once"
                and self._looks_like_value_converge_milestone(m)
            ):
                m.completion_strategy = "repeat_until_satisfied"
                fixes.append(f"子目标「{m.name}」策略修正为 repeat_until_satisfied")

        self._patch_goal_value_constraints(goal, fixes)

        scroll_milestones = [
            m for m in self._milestones.values()
            if m.completion_strategy == "scroll_until_boundary"
        ]
        for m in scroll_milestones:
            dep_context = ""
            if m.depends_on:
                dep_lines = []
                for dep_id in m.depends_on:
                    dep = self._milestones.get(dep_id)
                    if dep:
                        dep_lines.append(f"  - 前置子目标「{dep.name}」验收条件：{dep.success_condition}")
                if dep_lines:
                    dep_context = "\n".join(dep_lines)
            existing = f"\n当前停止条件：{m.scroll_stop_condition}" if m.scroll_stop_condition else "\n当前停止条件：（空）"
            patch = invoke_structured(
                llm,
                [
                    SystemMessage(content=self._prompts.stop_condition_patch),
                    HumanMessage(content=(
                        f"用户目标：{goal}\n"
                        f"子目标名称：{m.name}\n"
                        f"子目标描述：{m.description}\n"
                        f"本子目标验收条件：{m.success_condition}\n"
                        f"{dep_context}\n"
                        f"全局约束：{json.dumps(self._static_constraints, ensure_ascii=False)}"
                        f"{existing}"
                    )),
                ],
                _StopConditionPatch,
            )
            if patch.scroll_stop_condition != m.scroll_stop_condition:
                fixes.append(
                    f"子目标「{m.name}」停止条件修正：{m.scroll_stop_condition or '（空）'} → {patch.scroll_stop_condition}"
                )
                m.scroll_stop_condition = patch.scroll_stop_condition
                m.observable_boundary = patch.observable_boundary

        if self.task_type == "action" and _looks_like_analysis(goal):
            self.task_type = "analysis"
            fixes.append("task_type 从 action 修正为 analysis")

        if fixes:
            print(f"  [Guard] 补丁修复 {len(fixes)} 项：")
            for f in fixes:
                print(f"  [Guard]   {f}")

    def _next_milestone(self) -> Optional[str]:
        for mid in self._order:
            m = self._milestones[mid]
            if m.status != "pending":
                continue
            if all(self._milestones[dep].status == "done" for dep in m.depends_on):
                return mid
        return None
