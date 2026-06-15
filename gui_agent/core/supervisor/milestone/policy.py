"""MilestoneSupervisorPolicy: two-machine milestone supervisor."""

from dataclasses import dataclass
import json
import re
import time
from datetime import date
from typing import Literal, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from llm.structured import get_llm_token_usage, invoke_structured
from gui_agent.core.config import resolve_llm_config
from gui_agent.core.frame_analysis import CHANGE_SSIM_DIST_THR, is_loading_frame, region_change
from gui_agent.core.schemas import Milestone, Observation, PolicyTurn, SupervisorStep
from gui_agent.core.self_learning.progressive import ProgressiveKnowledge, _norm as _norm_page

from .helpers import _build_msgs, _format_history, _inject_knowledge, _make_llm, run_loop_check, run_planner
from .helpers import resolve_file_refs, run_checker, run_selector, _default_milestone_prompts
from .schemas import (
    MilestonePrompts,
    _DecomposeResponse,
    _LoopFrameResult,
    _PlanResult,
    _ReplanResult,
    _SingleCheckResult,
    _StopConditionPatch,
)

MAX_RETRIES = 3
STUCK_SCREEN_WINDOW = 3
STUCK_SCREEN_SIMILARITY = 0.95   # 全局 tier: whole-frame similarity above this = no global change
STUCK_SCREEN_FROZEN = 0.99
# 局部 tier: the action that produced a frame carries coordinates (tap/scroll/drag all have
# x/y), so look at whether the REGION the agent actually touched changed — the 1-SSIM structural
# distance inside a box around the action's (x,y) (see frame_analysis.region_change). A picker /
# spinner moves only that small region: global similarity stays ~99.9% but the touched region's
# 1-SSIM jumps to ~0.24, while a no-op stays ~0.00. A frame pair is "no change" (stuck candidate)
# only when BOTH tiers are under threshold — global similar AND the action region didn't move.
# 局部阈值复用 frame_changed 的 CHANGE_SSIM_DIST_THR(同一套 SSIM 生效判据，不再用灰度)。
MAX_SCROLL_PER_MILESTONE = 3
STUCK_REPEAT_WINDOW = 3
STUCK_REPEAT_WORD_OVERLAP = 0.85
# 连续调值类（is_iterative）的卡住判据：屏幕冻结/指令重复都是正常的（picker 反复拖同一下），
# 不适用。改判「被监控值连续 N 轮未变化」=真停滞。N>1 容忍摘要框提交滞后(1 轮) + 小步未到一格。
STUCK_VALUE_STALL_WINDOW = 4

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

_AM_WORDS = ("上午", "早上", "早晨", "清晨")
_PM_WORDS = ("下午", "晚上", "傍晚", "夜晚")
_TIME_ENTITY_WORDS = ("闹钟", "提醒", "日程", "会议", "预约", "时间", "alarm", "reminder", "schedule", "meeting")

# Task-type heuristic. A task is `analysis` only when its PURPOSE is to read/compute info.
# A query verb sitting among action-heavy steps (e.g. 「新建…建单…最后查看状态」) does NOT make
# the whole task analysis — that final read is just a substep. So flip action→analysis only when
# the goal has query keywords AND no state-changing action verb.
_ANALYSIS_KEYWORDS = ("多少", "什么", "有没有", "查看", "看看", "统计", "查一下", "帮我找", "列出", "汇总", "比较")
_ACTION_VERBS = (
    "新建", "创建", "建单", "建一个", "提交", "启用", "停用", "添加", "删除", "修改", "设置", "设为",
    "放到", "移动", "发送", "购买", "登录", "注册", "上传", "下单", "派发", "下达", "编辑", "切换",
    "保存", "配置", "加入调度", "上线", "绑定", "开启", "关闭",
)


def _looks_like_analysis(goal: str) -> bool:
    """True only for read/compute-purpose goals — a query keyword among action verbs stays action."""
    has_query = any(kw in goal for kw in _ANALYSIS_KEYWORDS)
    has_action = any(kw in goal for kw in _ACTION_VERBS)
    return has_query and not has_action


_WEEKDAY_ALIASES = {
    "周一": ("周一", "星期一", "礼拜一"),
    "周二": ("周二", "星期二", "礼拜二"),
    "周三": ("周三", "星期三", "礼拜三"),
    "周四": ("周四", "星期四", "礼拜四"),
    "周五": ("周五", "星期五", "礼拜五"),
    "周六": ("周六", "星期六", "礼拜六"),
    "周日": ("周日", "周天", "星期日", "星期天", "礼拜日", "礼拜天"),
}


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



# ── Helpers ───────────────────────────────────────────────────────────


class _Timer:
    """Context manager: time a named section, accumulating into a collector dict.

    When `tokens` is given, also records the LLM input/output token delta during
    the section into tokens[name] = {"input": .., "output": ..} (same global-counter
    caveats as get_llm_call_count: concurrent calls in this window leak in).
    """

    def __init__(self, collector: dict[str, float], order: list[str], name: str,
                 tokens: "dict[str, dict[str, int]] | None" = None):
        self._c = collector
        self._o = order
        self._n = name
        self._s = 0.0
        self._tokens = tokens
        self._tok0 = (0, 0)

    def __enter__(self):
        self._s = time.perf_counter()
        if self._tokens is not None:
            self._tok0 = get_llm_token_usage()
        return self

    def __exit__(self, *a):
        d = time.perf_counter() - self._s
        if self._n not in self._c:
            self._o.append(self._n)
        self._c[self._n] = self._c.get(self._n, 0) + d
        if self._tokens is not None:
            inp, out = get_llm_token_usage()
            slot = self._tokens.setdefault(self._n, {"input": 0, "output": 0})
            slot["input"] += inp - self._tok0[0]
            slot["output"] += out - self._tok0[1]


def _is_loop(milestone: Milestone) -> bool:
    return (
        milestone.kind == "collection"
        and milestone.completion_strategy == "scroll_until_boundary"
    )


def _last_scroll_was_for(history: list[PolicyTurn], milestone_id: str) -> bool:
    return bool(
        history
        and history[-1].supervisor.milestone_id == milestone_id
        and history[-1].action_decision
        and history[-1].action_decision.action.action_type == "scroll"
        and history[-1].executed
    )


def _has_successful_scroll_for(history: list[PolicyTurn], milestone_id: str) -> bool:
    return any(
        t.supervisor.milestone_id == milestone_id
        and t.action_decision
        and t.action_decision.action.action_type in {"scroll", "drag"}
        and t.executed
        for t in history
    )


def _has_collected(history: list[PolicyTurn], milestone_id: str) -> bool:
    return any(
        t.supervisor.milestone_id == milestone_id and t.read_added_content
        for t in history
    )


def _default_read_instruction(milestone: Milestone) -> str:
    return (
        f"提取当前屏幕中与「{milestone.name}」相关的所有可见内容，"
        "保留名称/标题、时间/位置、目标相关数值、状态、类别等字段；如果是列表，逐条提取。"
    )


def _ctx(milestone: Milestone, read_instruction: Optional[str], collection_scope=None) -> dict:
    allow_read = milestone.kind in {"collection", "verification"}
    return {
        "read_instruction": read_instruction,
        "allow_read": bool(read_instruction and allow_read),
        "milestone_id": milestone.id,
        "milestone_kind": milestone.kind,
        "completion_strategy": milestone.completion_strategy,
        "collection_scope": collection_scope,
    }


def _is_home_identity(page_identity: str) -> bool:
    """Derive 'on the iOS home screen' from the checker's free-text page_identity.

    Home-screen icon labels are not tappable (only the icon glyph launches the
    app), so the executor must skip OCR text-snap there. The checker reliably
    writes '主屏' for springboard; also accept the English variants.
    """
    pid = (page_identity or "").lower()
    return "主屏" in (page_identity or "") or "home screen" in pid or "springboard" in pid


# ── Main class ────────────────────────────────────────────────────────


class MilestoneSupervisorPolicy:
    """Two-machine milestone supervisor: single-step and loop run independently."""

    name = "milestone"

    def __init__(self, prompts: Optional[MilestonePrompts] = None) -> None:
        # Platform prompt set. Defaults to iphone's (lazy import) so every existing
        # no-arg constructor (iphone factory, evals, tests) is unchanged; browser can
        # inject its own once written — today it borrows iphone's.
        self._prompts = prompts or _default_milestone_prompts()
        self._global_constraints: list[str] = []
        self._milestones: dict[str, Milestone] = {}
        self._order: list[str] = []
        self._current_id: Optional[str] = None
        # One-shot: skip the next step()'s initial done-check and plan directly (set by reseed
        # for a freshly-entered navigation milestone — DAG _advance parity, see reseed).
        self._skip_initial_check: bool = False
        # Each entry pairs a frame with the action CENTER (normalized 0-1000 x/y, or None)
        # that produced it — the 局部 stuck tier inspects the region the agent touched.
        self._recent_screenshots: list[tuple[bytes, Optional[tuple[float, float]]]] = []
        self._scroll_counts: dict[str, int] = {}
        self.task_type: Literal["action", "analysis"] = "action"
        self._app_knowledge: Optional[str] = None
        self._check_knowledge: str = ""
        self._elements_knowledge: Optional[str] = None
        self._pk: Optional[ProgressiveKnowledge] = None  # progressive (skill-like) section loader
        self._app_name: str = ""
        self._last_url: Optional[str] = None  # 上一轮页面 URL(结构化跳页信号；浏览器才有)
        self._last_dom_state: Optional[str] = None  # 上一轮交互状态指纹(表单值+焦点；浏览器才有)
        self._dom_changed = False  # 本轮指纹是否变化 = 确定性进展信号(填表时像素/指令都相似)
        self._last_page_identity: dict[str, str] = {}
        self._last_check_summary: dict[str, str] = {}
        # 连续调值类的进展追踪：每 milestone 一个滑动窗口，存最近若干轮 checker 读到的「当前值」。
        self._progress_values: dict[str, list[str]] = {}
        self._last_check: Optional[_SingleCheckResult] = None
        self._milestone_done_checks: dict[str, "_SingleCheckResult"] = {}  # milestone_id → done check
        self._last_plan: Optional[_PlanResult] = None
        self._last_replan: Optional[_ReplanResult] = None
        self._last_sections_loaded: list[str] = []  # progressive section stems injected this turn (logged)
        # KnowledgeSelector cache: (milestone_id, normalized page_identity) → section stems.
        # Selection only changes when the page or the milestone changes, so within a key the
        # selector LLM is never re-invoked (empty selections are cached too).
        self._selector_cache: dict[tuple[str, str], list[str]] = {}
        self._goal: str = ""  # set by _decompose; selector prompt context
        self._timings: dict[str, float] = {}
        self._timings_order: list[str] = []
        self._token_usage: dict[str, dict[str, int]] = {}   # per-module {input, output}

    def set_app_knowledge(
        self,
        text: str,
        app_name: str = "",
        elements: str = "",
        sections: Optional[dict[str, str]] = None,
        check: str = "",
    ) -> None:
        self._app_knowledge = text
        self._elements_knowledge = elements or None
        # _check.md → Checker-only observable completion rules（动态验收知识）：描述该 app 界面
        # 的实际显示形态（列渲染短形式/成功提示样式/错误 toast 语义等）。静态 checker prompt 只留
        # 跨 app 通用原则；app 特定的验收事实从这里按 app 注入，避免静态规则膨胀和过拟合。
        self._check_knowledge = check
        # When per-section bodies exist, the planner loads them progressively: a dedicated
        # KnowledgeSelector micro-decision picks section ids per (milestone, page) — cached,
        # so it only fires on page/milestone changes. Falls back to the full `elements`
        # blob when absent.
        self._pk = ProgressiveKnowledge(sections) if sections else None
        self._selector_cache.clear()
        if app_name:
            self._app_name = app_name

    def step(self, observation: Observation, goal: str, history: list[PolicyTurn]) -> SupervisorStep:
        self._timings.clear()
        self._timings_order.clear()
        self._token_usage.clear()
        self._last_sections_loaded = []  # reset; _invoke_planner fills it when progressive knowledge is active
        # Report-only carry-overs: clear so a turn that DOESN'T run the planner/replan reports
        # null instead of the previous turn's stale plan (e.g. the terminal "done" turn). Both
        # are read solely by runner's _extract_plan/_extract_replan — no internal logic depends
        # on them persisting across turns (unlike _last_check, which is cross-turn memory).
        self._last_plan = None
        self._last_replan = None

        if not self._order:
            with _Timer(self._timings, self._timings_order, "decompose", self._token_usage):
                self._decompose(goal, observation)

        if self._current_id is None:
            return self._terminal_step()

        milestone = self._milestones[self._current_id]
        if _is_loop(milestone):
            result = self._run_loop_turn(milestone, observation, history)
        else:
            result = self._run_single_turn(milestone, observation, history)

        return result

    def reseed(
        self,
        milestone: Milestone,
        task_type: Literal["action", "analysis"] = "action",
        fresh_advance: bool = False,
    ) -> None:
        """单 milestone 模式（DSL orchestrator 用）：把 supervisor 重置成只驱动这一个 milestone，
        绕过内部 decompose（DSL program 已含全部 milestone，由解释器排序）。step() 把这个 milestone
        跑到 done 后，_current_id 自然走到 None → 下一次 step() 发 terminal/goal_completed，agent_loop
        据此向解释器要下一个并 reseed。

        与 DAG 模式的 `_advance` 对齐：跨 milestone **只清 `_recent_screenshots`**（stuck 检测器
        的帧历史，不该跨 milestone 串）；其余 per-milestone 态（page_identity / last_plan /
        last_replan / dom_state / done_checks / sections_loaded …）一律**保留**，与 `_advance`
        一致——否则编排器会比 DAG 更狠地清空，造成跨 milestone 上下文断裂。应用知识/全局约束本就
        保留（跨 milestone 复用）。task_type 控制读取门（读取类 milestone 传 'analysis' 才会真读）。"""
        self._milestones = {milestone.id: milestone}
        self._order = [milestone.id]
        self._current_id = milestone.id
        self.task_type = task_type  # 读取门：read milestone 须为 'analysis' 才读
        self._recent_screenshots.clear()  # 唯一要清的，和 _advance 一致
        # DAG `_advance` 的 nav 跳 check 镜像：fresh_advance=刚从上一个 milestone 推进过来（同帧），
        # 此时若新 milestone 是 navigation，它「in_progress by construction」，跳过首次验收直接规划
        # 第一步导航动作——省掉交接时第 2 次 checker。幂等（重点 nav 目标无害；残留 already-done 由
        # 下一轮正常 check 兜底）。action/filter/collection 一律保留 check：重跑 action 可能双执行
        # （re-send/re-submit），collection 需要 checker 的 read_instruction。一次性，step() 消费后清。
        self._skip_initial_check = fresh_advance and milestone.kind == "navigation"

    # ── Single-step machine ───────────────────────────────────────────

    def _run_single_turn(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
    ) -> SupervisorStep:
        if is_loading_frame(observation):
            print("  [BlankScreen] 检测到白屏，页面加载中，等待下一帧...")
            return SupervisorStep(
                should_act=False,
                instruction=None,
                stop=False,
                goal_completed=False,
                is_loading=True,
                summary="页面加载中（白屏），等待...",
                **_ctx(milestone, None),
            )

        # Freshly entered navigation milestone (reseed fresh_advance, mirror DAG _advance):
        # skip the initial done-check and plan the first nav action directly — drops the 2nd
        # checker call on the milestone hand-off. One-shot.
        if self._skip_initial_check:
            self._skip_initial_check = False
            print("  [SkipCheck] 新进入导航子目标，跳过首次验收，直接规划")
            synthetic = _SingleCheckResult(
                status="in_progress",
                reason=f"刚进入子目标「{milestone.name}」，默认未完成",
                summary="",
            )
            self._last_check = synthetic
            self._last_page_identity[milestone.id] = ""
            return self._plan_single(milestone, synthetic, observation, history)

        if history and history[-1].action_decision:
            if history[-1].action_decision.action.action_type == "type":
                self._recent_screenshots.clear()

        prev_page_id = self._last_page_identity.get(milestone.id, "")

        with _Timer(self._timings, self._timings_order, "checker", self._token_usage):
            check = self._single_check(milestone, observation, history)
        self._last_check = check
        print(f"  [SingleCheck] {check.status}: {check.reason}")

        if check.loading:
            print("  [Loading] 检测到加载状态，等待下一帧...")
            return SupervisorStep(
                should_act=False, instruction=None, stop=False,
                goal_completed=False, is_loading=True, summary="页面加载中，等待...",
                **_ctx(milestone, None),
            )

        current_page_id = check.page_identity or ""
        self._last_page_identity[milestone.id] = current_page_id

        # Programmatic page-change signal: the browser URL is ground truth, so a changed URL means
        # the previous action navigated — a definite EFFECT / page change. Use it to suppress the
        # pixel-based false positives below (false no_effect, false sim-stuck on a visually-similar
        # new page). None on iphone/android (url-less) → url_changed stays False, no effect there.
        cur_url = observation.url
        url_changed = bool(cur_url and self._last_url is not None and cur_url != self._last_url)
        if cur_url is not None:
            self._last_url = cur_url
        if url_changed:
            print(f"  [URLChanged] {cur_url} → 已跳页(确定性)，抑制 no_effect/sim_stuck 误判")

        # Second structural progress signal: the interactive-state fingerprint (form values +
        # focus). Filling a form field-by-field keeps pixels near-identical and produces
        # near-identical instructions ("在X输入框输入Y") — text similarity then misreads real
        # progress as a loop (20260612_103356: 8-step form fill escalated to stuck). A changed
        # fingerprint is ground truth that the last action DID something.
        cur_dom = observation.dom_state
        self._dom_changed = bool(cur_dom and self._last_dom_state is not None and cur_dom != self._last_dom_state)
        if cur_dom is not None:
            self._last_dom_state = cur_dom
        if self._dom_changed and not url_changed:
            print("  [DOMChanged] 交互状态指纹有变化(表单/焦点在推进)，抑制 stuck/重复误判")

        # VERIFY FIRST, before consuming the prior turn's off-target / no-effect signals.
        # An action that ACTUALLY satisfied the milestone advances even when the verifiers
        # misreported it — TargetVerify false off-target (tap hit the right tab but was read
        # as the wrong one) or settle false no-effect (the screen did change but under the
        # diff threshold). Only when NOT done do those signals route to replan; otherwise the
        # fast-paths (placed before the checker) would skip verification and replan a
        # milestone the action already completed. See logs/.../android/20260611_085000.
        if check.status == "done":
            return self._advance(milestone, observation, history)

        # Off-target last action (post-action targeting verify said the tap missed) — and the
        # milestone is NOT done (checked above) → route straight into replan. Catches "screen
        # changed but to the wrong element" (e.g. 搜索框 tap hit 转账 tab), which SimStuck can't.
        last_tv = history[-1].target_verify if history else None
        if last_tv is not None and not last_tv.on_target:
            print(f"  [OffTarget] 上一步误中「{last_tv.actual_element}」，已先验收(未完成)→ replan")
            stuck = _SingleCheckResult(
                status="stuck",
                reason=f"上一步没有打开预期元素，当前停留在「{last_tv.actual_element}」相关状态",
                stuck_reason=f"上一步没有到达预期元素，当前显示「{last_tv.actual_element}」相关状态",
                summary="",
            )
            return self._handle_stuck(milestone, stuck, None, observation, history)

        # Ineffective last tap: target_verify said on_target (hit the intended element) yet
        # settle saw zero screen change → the tap did nothing (re-tapped an already-active tab
        # / inert element). Milestone not done (checked above) → replan now instead of waiting
        # ~3 frames for SimStuck. Complements off_target above (wrong-element vs
        # right-element-but-no-effect). Gated on tap so gestures (which legitimately may not
        # move much) never trigger it.
        last_turn = history[-1] if history else None
        if (
            last_turn is not None
            and getattr(last_turn, "no_effect", False)
            and not url_changed  # URL changed = the tap DID navigate → not a no-effect
            and not self._dom_changed  # interactive-state fingerprint moved = the tap DID something
            and last_turn.action_decision is not None
            and last_turn.action_decision.action.action_type in ("tap", "click")
            and (last_tv is None or last_tv.on_target)
        ):
            tapped = last_turn.action_decision.action.description or "目标元素"
            print(f"  [NoEffect] 上一步点击「{tapped}」落点正确但屏幕零变化，已先验收(未完成)→ replan")
            stuck = _SingleCheckResult(
                status="stuck",
                reason=f"点击「{tapped}」后页面没有出现验收条件所需的可见变化",
                stuck_reason=f"点击「{tapped}」后仍未看到目标状态，应尝试其他可见入口",
                summary="",
            )
            return self._handle_stuck(milestone, stuck, None, observation, history)

        prev_action = history[-1].action_decision.action if history and history[-1].action_decision else None

        # 连续调值类（picker/步进器收敛）：重复同一列滚动是正常的，但动作区连续不动不是正常进展。
        # 因此保留 SimStuck 的「全局+动作局部均无变化」判据；只有指令重复类 RepStuck 被抑制。
        # ValueStall 作为语义兜底：当画面在动但 checker 读到的当前值长期不变，也判停滞。
        if milestone.is_iterative:
            sim_stuck = None if url_changed else self._check_screen_similarity(observation, self._action_center(prev_action))
            if sim_stuck is not None:
                print(f"  [Stuck] {sim_stuck.status}: {sim_stuck.reason}")
                return self._handle_stuck(
                    milestone, sim_stuck, check.read_instruction, observation, history,
                    page_changed=False,
                    prev_page_id=prev_page_id, current_page_id=current_page_id,
                )
            self._last_check_summary[milestone.id] = check.summary
            stall = self._check_value_stall(milestone, check)
            if stall is not None:
                return self._handle_stuck(
                    milestone, stall, check.read_instruction, observation, history,
                    page_changed=False,
                    prev_page_id=prev_page_id, current_page_id=current_page_id,
                )
            return self._plan_single(milestone, check, observation, history)

        sim_stuck = None if (url_changed or self._dom_changed) else self._check_screen_similarity(observation, self._action_center(prev_action))
        self._last_check_summary[milestone.id] = check.summary

        rep_stuck = self._check_instruction_repetition(history, milestone.id) if not sim_stuck else None
        # Stepping a picker / value means repeating the SAME column scroll. The two-tier
        # _check_screen_similarity already returns stuck when the touched region is NOT moving,
        # so reaching here (sim_stuck is None) means the region IS changing — a repeated
        # value-adjust scroll is progress, not a loop. Don't let repetition flag it.
        if rep_stuck is not None and self._is_value_adjust(prev_action):
            print("  [RepStuck] 已抑制：调值类重复滚动属正常（动作区在变）")
            rep_stuck = None
        # Same exemption from ground truth: the interactive-state fingerprint changed since
        # last turn, so the "similar" instructions are advancing through a form, not looping.
        if rep_stuck is not None and self._dom_changed:
            print("  [RepStuck] 已抑制：交互状态指纹在变化（填表推进中）")
            rep_stuck = None
        if sim_stuck or rep_stuck:
            stuck = sim_stuck or rep_stuck
            assert stuck is not None
            print(f"  [Stuck] {stuck.status}: {stuck.reason}")
            page_changed = sim_stuck is None
            return self._handle_stuck(
                milestone, stuck, check.read_instruction, observation, history,
                page_changed=page_changed,
                prev_page_id=prev_page_id,
                current_page_id=current_page_id,
            )
        return self._plan_single(milestone, check, observation, history)

    def _plan_single(
        self,
        milestone: Milestone,
        check: _SingleCheckResult,
        observation: Observation,
        history: list[PolicyTurn],
    ) -> SupervisorStep:
        with _Timer(self._timings, self._timings_order, "planner", self._token_usage):
            plan = self._invoke_planner(milestone, check, observation, history)
        if self._is_sequence(plan.instruction):
            print("  [Planner] 多步序列，重试...")
            with _Timer(self._timings, self._timings_order, "planner", self._token_usage):
                plan = self._invoke_planner(
                    milestone, check, observation, history,
                    extra="你刚才输出了多个步骤，请只返回当前屏幕上马上要做的一个操作。",
                )
        # 连续调值类豁免「指令重复=失败」升级：对 picker 而言重复"向上拖月份列"直到到位本就
        # 是正确做法，单步式的重复检测会误把它判成撞墙→死路。其停滞由 _check_value_stall 兜。
        if not milestone.is_iterative and self._is_repeated_instruction(plan.instruction, milestone.id, history):
            if self._dom_changed:
                # Ground truth beats text similarity: the interactive-state fingerprint moved
                # since last turn, so the prior "similar" instruction WORKED (form filling
                # produces legitimately alike instructions). Let the plan through unchanged.
                print("  [Planner] 指令相似但交互状态在推进（DOM 有变化），放行")
            else:
                print("  [Planner] 指令重复已失败操作，重试...")
                with _Timer(self._timings, self._timings_order, "planner", self._token_usage):
                    plan = self._invoke_planner(
                        milestone, check, observation, history,
                        extra=(
                            "你刚才的指令与之前未达成验收条件的操作相同。"
                            "请仔细查看截图，找一个不同的 UI 元素或操作路径。"
                        ),
                    )
                if self._is_repeated_instruction(plan.instruction, milestone.id, history):
                    print("  [Planner] 重试仍重复，升级为 stuck 处理")
                    stuck_check = _SingleCheckResult(
                        status="stuck",
                        reason="连续给出相似操作，当前页面仍未满足验收条件",
                        stuck_reason="连续给出相似指令但目标未达成，需要改用当前截图中的其他可见入口或操作顺序",
                        issues=["连续操作策略过于相似"],
                        summary=check.summary,
                    )
                    return self._handle_stuck(milestone, stuck_check, check.read_instruction, observation, history)
        # 结构化 drag 距离：当 planner 给出本列的当前值/目标值时，按差几格算出 drag_steps，
        # 让 action policy 据此放大拖动幅度（差 20 格用 large 粗调、接近后自动收回 small 精调）。
        # 这条结构化通路绕开「从 instruction 文本正则抠数字」的脆弱性——指令只写了目标值
        # （如「直到显示21日」缺当前值）时，正则抠不到距离会退化成一格一格挪、跑满轮数也到不了。
        drag_steps = self._picker_drag_steps(plan)
        drag_column = getattr(plan, "drag_column", None)
        if drag_steps == 0 and drag_column:
            print("  [Planner] picker 已到目标值但仍要求滚动，重试...")
            with _Timer(self._timings, self._timings_order, "planner", self._token_usage):
                plan = self._invoke_planner(
                    milestone,
                    check,
                    observation,
                    history,
                    extra=(
                        "你刚才要求滚动 picker，但结构化字段显示该列当前值已经等于目标值（steps=0）。"
                        "禁止继续滚动/拖动这个 picker 列，也不要为了“确认精确”微调。"
                        "请改为当前子目标剩余的一个动作：如果需要提交结果，就点击保存/确认/应用/完成/下一步等按钮；"
                        "如果还有其他未到位的 picker 列，只能操作那一列；如果验收已经满足则停止。"
                    ),
                )
            drag_steps = self._picker_drag_steps(plan)
            drag_column = getattr(plan, "drag_column", None)
            if drag_steps == 0 and drag_column:
                print("  [Planner] 重试仍为零步 picker，清空 picker hints 避免执行层强制滚动")
                plan.direction = None
                plan.drag_column = None
                plan.drag_current_value = None
                plan.drag_target_value = None
                drag_steps = None
        print(f"  [Planner] {plan.instruction}")
        drag_column = getattr(plan, "drag_column", None)
        if drag_steps is not None and drag_column:
            print(f"  [Planner] hints: direction={plan.direction} column={drag_column} steps={drag_steps}")
        elif plan.direction or drag_column:
            print(f"  [Planner] hints: direction={plan.direction} column={drag_column}")
        if plan.direction in ("increase", "decrease") and drag_column:
            self._fix_picker_direction(plan)
        self._last_plan = plan
        milestone.status = "running"
        return SupervisorStep(
            should_act=bool(plan.instruction),
            instruction=plan.instruction or None,
            stop=False,
            goal_completed=False,
            summary=plan.summary,
            direction=plan.direction,
            drag_column=getattr(plan, "drag_column", None),
            drag_steps=drag_steps,
            is_home_screen=_is_home_identity(check.page_identity),
            **_ctx(milestone, check.read_instruction),
        )

    # ── Loop machine ──────────────────────────────────────────────────

    def _run_loop_turn(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
    ) -> SupervisorStep:
        self._scroll_counts[milestone.id] = self._scroll_counts.get(milestone.id, 0) + 1
        scroll_count = self._scroll_counts[milestone.id]

        if milestone.scroll_budget > 0:
            budget = milestone.scroll_budget
        elif milestone.observable_boundary:
            budget = 10
        else:
            budget = MAX_SCROLL_PER_MILESTONE
        if scroll_count > budget:
            print(f"  [Loop] 滚动预算耗尽（{scroll_count}/{budget}，observable={milestone.observable_boundary}）→ 结束收集")
            if not _has_successful_scroll_for(history, milestone.id):
                stuck = _SingleCheckResult(
                    status="stuck",
                    reason="滚动预算耗尽，但尚未观测到任何成功执行的纵向滚动",
                    stuck_reason="无法区分页面边界与无效滚动，且缺少有效滚动证据",
                    issues=["没有成功滚动记录"],
                    summary="滚动未取得可验证进展",
                )
                read_inst = None if self.task_type == "action" else _default_read_instruction(milestone)
                return self._handle_stuck(milestone, stuck, read_inst, observation, history)
            return self._advance(milestone, observation, history)

        sim_stuck = self._check_screen_similarity(observation)
        last_read_added = bool(
            history
            and history[-1].supervisor.milestone_id == milestone.id
            and history[-1].read_added_content
        )
        if sim_stuck:
            if sim_stuck.frozen:
                if not _has_successful_scroll_for(history, milestone.id):
                    print("  [Loop] 屏幕冻结且无成功滚动证据 → 判为无效滚动，触发重规划")
                    read_inst = None if self.task_type == "action" else _default_read_instruction(milestone)
                    return self._handle_stuck(milestone, sim_stuck, read_inst, observation, history)
                print("  [Loop] 屏幕冻结（≥99%），即使 reader 返回新内容也结束收集")
                return self._advance(milestone, observation, history)
            if not last_read_added:
                if not _has_successful_scroll_for(history, milestone.id):
                    print("  [Loop] 截图连续无变化且无成功滚动证据 → 判为无效滚动，触发重规划")
                    read_inst = None if self.task_type == "action" else _default_read_instruction(milestone)
                    return self._handle_stuck(milestone, sim_stuck, read_inst, observation, history)
                print("  [Loop] 截图连续无变化且无新增内容 → 判为边界，结束收集")
                return self._advance(milestone, observation, history)
            print("  [Loop] 截图相似但上一轮读到了新内容，继续收集")

        with _Timer(self._timings, self._timings_order, "loop_check", self._token_usage):
            frame = self._loop_check(milestone, observation, history)
        self._last_check = None  # loop milestones use _LoopFrameResult, not _SingleCheckResult
        print(f"  [LoopFrame] boundary={frame.boundary_reached}, should_stop={frame.should_stop}")

        # 采集启动阶段（首次成功滚动之前）拦截 loading 帧：刚应用筛选/进入列表时，页面常还在
        # 「加载中」、列表体仍显示旧内容（如范围外的更晚日期）。若读入会污染 content_notes 并把
        # stitch 基线钉在旧帧上，导致后续真实内容拼接错位、漏采中段（实测 20260607_105731：
        # 读了加载中的旧帧 → 漏掉 5/24-28、混入 5/29）。返回 is_loading 等待帧，由 runner 短路
        # 跳过本帧（不读、不喂 stitch、不计轮数）。一旦开始滚动，内容已确认渲染，不再判 loading。
        if frame.loading and not _has_successful_scroll_for(history, milestone.id):
            print("  [Loop] 采集启动帧仍在加载中 → 等待重渲染，不读取本帧")
            self._scroll_counts[milestone.id] -= 1  # 加载等待不计入滚动预算
            return SupervisorStep(
                should_act=False, stop=False, goal_completed=False, is_loading=True,
                summary="采集页加载中，等待...",
                **_ctx(milestone, None),
            )

        if frame.should_stop:
            print(f"  [Loop] 停止条件触发：{frame.stop_reason}")
        if self.task_type == "action":
            read_inst = None
        else:
            read_inst = frame.read_instruction or _default_read_instruction(milestone)

        if frame.should_stop:
            if _has_collected(history, milestone.id):
                print("  [Loop] 已触发停止条件且有采集内容 → 结束收集")
                final_read = _ctx(milestone, read_inst, frame.collection_scope)
                if milestone.scroll_stop_condition:
                    final_read["collection_summary"] = (
                        f"停止条件「{milestone.scroll_stop_condition}」已触发"
                        f"（{frame.stop_reason}）"
                    )
                return self._advance(
                    milestone, observation, history,
                    final_read=final_read,
                )
            # 停止条件触发、但本采集子目标尚未采到任何内容。
            if not _has_successful_scroll_for(history, milestone.id):
                # ⚠️ 还没滚动过就报"停止/到底"不可信：刚进入采集子目标的首帧，列表常一屏可见、
                # 下方却还有内容（这正是 force_complete 误判、零采集即完成的根源——筛选结果一屏
                # 显示完就被判 done，实则可滚）。强制先滚一次采集、用真实滚动验证边界，绝不在
                # 零滚动+零采集时结束收集。落到下方滚动规划块。
                print("  [Loop] 停止条件触发但尚未滚动过且零采集 → 先强制滚动一次验证边界，不判完成")
            else:
                stuck = _SingleCheckResult(
                    status="stuck",
                    reason=f"停止条件已触发但尚未采集到目标内容：{frame.stop_reason}",
                    stuck_reason="停止条件触发且没有可用采集结果",
                    summary=frame.summary,
                )
                return self._handle_stuck(milestone, stuck, read_inst, observation, history)

        if frame.boundary_reached and _last_scroll_was_for(history, milestone.id):
            print("  [Loop] 确认列表边界 → 结束收集")
            return self._advance(milestone, observation, history)

        milestone.status = "running"
        loop_summary_prefix = "继续滚动查找目标" if self.task_type == "action" else "继续滚动收集内容"
        if _last_scroll_was_for(history, milestone.id):
            return SupervisorStep(
                should_act=True,
                instruction="继续滚动",
                preformed_action=history[-1].action_decision,
                stop=False,
                goal_completed=False,
                summary=f"{loop_summary_prefix}。{frame.summary}",
                read_instruction=read_inst,
                allow_read=bool(read_inst),
                milestone_id=milestone.id,
                milestone_kind=milestone.kind,
                completion_strategy=milestone.completion_strategy,
                collection_scope=frame.collection_scope,
            )

        with _Timer(self._timings, self._timings_order, "loop_scroll", self._token_usage):
            plan = self._invoke_loop_scroll(milestone, frame, observation)
        print(f"  [LoopScroll] {plan.instruction}")
        return SupervisorStep(
            should_act=True,
            instruction=plan.instruction,
            stop=False,
            goal_completed=False,
            summary=plan.summary,
            read_instruction=read_inst,
            allow_read=bool(read_inst),
            milestone_id=milestone.id,
            milestone_kind=milestone.kind,
            completion_strategy=milestone.completion_strategy,
            collection_scope=frame.collection_scope,
        )

    # ── Shared: advance, stuck, terminal ─────────────────────────────

    def _advance(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
        final_read: Optional[dict] = None,
    ) -> SupervisorStep:
        done_name = milestone.name
        milestone.status = "done"
        self._current_id = self._next_milestone()
        self._recent_screenshots.clear()
        print(f"  子目标「{done_name}」已完成")

        pre_existing = not any(
            t.executed for t in history
            if t.supervisor.milestone_id == milestone.id
        )
        if pre_existing:
            print(f"  [PreExisting] 子目标「{done_name}」未执行任何动作即判完成，目标状态在会话前已存在")

        if self._current_id is None:
            # final_read(_ctx) 已含 milestone_id/kind/completion_strategy；显式再传会撞车
            # （TypeError: got multiple values for 'milestone_id'）。合并为一个 ctx：无
            # final_read 时只带这三个 milestone 字段，有则其同名键(同值)覆盖、并补 read_* 等。
            ctx = {
                "milestone_id": milestone.id,
                "milestone_kind": milestone.kind,
                "completion_strategy": milestone.completion_strategy,
                **(final_read or {}),
            }
            return SupervisorStep(
                should_act=False, stop=True, stop_reason="所有子目标已完成",
                goal_completed=True, pre_existing=pre_existing,
                summary=f"子目标「{done_name}」已完成，任务全部完成。",
                **ctx,
            )

        next_ms = self._milestones[self._current_id]
        print(f"  开始执行「{next_ms.name}」")

        if final_read:
            return SupervisorStep(
                should_act=False, stop=False, goal_completed=False,
                summary=f"子目标「{done_name}」已完成，下一子目标「{next_ms.name}」待执行。",
                **final_read,
            )

        if _is_loop(next_ms):
            return self._run_loop_turn(next_ms, observation, history)

        # Freshly entered navigation milestone: it depends on the just-completed
        # one, so it is in_progress by construction. Skip its initial done-check
        # and plan the first nav action directly — this drops the 2nd checker
        # call on advance turns (the user-visible "2 轮 checker" latency). The
        # frame is known-loaded here (we only reach _advance after a done check
        # on this same frame). A residual already-done state is caught by the
        # next turn's normal check; re-tapping a nav target is idempotent so the
        # extra action is harmless. Other kinds keep the check — re-running an
        # action milestone could double-execute (re-send/re-submit), and
        # collection/verification need the checker's read_instruction.
        if next_ms.kind == "navigation":
            print("  [SkipCheck] 新进入导航子目标，跳过首次验收，直接规划")
            # Save the done check for the completed milestone before overwriting _last_check.
            if self._last_check is not None:
                self._milestone_done_checks[milestone.id] = self._last_check
            synthetic = _SingleCheckResult(
                status="in_progress",
                reason=f"刚进入子目标「{next_ms.name}」，默认未完成",
                summary="",
            )
            self._last_check = synthetic
            self._last_page_identity[next_ms.id] = ""
            return self._plan_single(next_ms, synthetic, observation, history)

        return self._run_single_turn(next_ms, observation, history)

    def _handle_stuck(
        self,
        milestone: Milestone,
        check: _SingleCheckResult,
        read_inst: Optional[str],
        observation: Observation,
        history: list[PolicyTurn],
        page_changed: bool = True,
        prev_page_id: str = "",
        current_page_id: str = "",
    ) -> SupervisorStep:
        self._recent_screenshots.clear()
        skip_retry = False
        if history and history[-1].supervisor and history[-1].supervisor.milestone_id == milestone.id:
            if history[-1].supervisor.instruction and not history[-1].executed:
                print(f"  [Replan] 上一轮指令未执行，不计入重试次数")
                skip_retry = True
            elif page_changed:
                truly_new_page = bool(prev_page_id and current_page_id and prev_page_id != current_page_id)
                if truly_new_page:
                    print(f"  [Replan] 页面已跳转（{prev_page_id} → {current_page_id}），不计入重试次数")
                    skip_retry = True
                    before = len(self._global_constraints)
                    self._global_constraints = [
                        c for c in self._global_constraints
                        if not (
                            c.startswith("指令「")
                            and ("禁止重复此指令" in c or "未达成目标" in c)
                        )
                    ]
                    cleared = before - len(self._global_constraints)
                    if cleared:
                        print(f"  [Replan] 清除 {cleared} 条旧页面操作约束")
                else:
                    print(f"  [Replan] 屏幕变化但页面未变（{current_page_id or '未知'}），计入重试次数")
        if not skip_retry:
            milestone.retry_count += 1

        self._record_failure_constraint(milestone, check, history)

        if milestone.retry_count >= MAX_RETRIES:
            fallback = self._try_filter_fallback(milestone, can_degrade=True, read_inst=read_inst)
            if fallback:
                return fallback
            return self._fail(milestone, check, read_inst)

        print(f"  [Replan] 第 {milestone.retry_count} 次重试...")
        # 连续调值类停滞时的纠偏方向：继续用拖动（picker 本就只能拖），换一种连续策略——
        # 加大拖动幅度 / 换调整的列 / 调整顺序（如先把"日"调到合法范围再调"月"，破非法日期回弹死锁）。
        # 绝不退化成点击 picker 滚轮数值（iOS 滚轮不认点击），也不要判"组件不支持拖拽"。
        iter_extra = (
            "⚠️ 当前子目标是连续调值类（靠滚轮/步进器反复拖动逼近目标值）。停滞≠不支持拖拽。"
            "必须继续用拖动，换一种连续策略：加大拖动幅度、或换要调整的列、或调整顺序"
            "（如把'日'先调到目标月份的合法范围内、再调'月'，以破解非法日期被回弹的死锁）。"
            "禁止改用点击 picker 滚轮上的具体数值（iOS 滚轮不响应点击），禁止判定'组件不支持滚动/拖拽'。"
        ) if milestone.is_iterative else ""
        with _Timer(self._timings, self._timings_order, "replanner", self._token_usage):
            replan = self._invoke_replanner(milestone, check, observation, history, extra=iter_extra)
        self._last_replan = replan
        print(f"  [Replan] 诊断={replan.diagnosis}, 策略={replan.strategy}")

        # Persist diagnosis as a global constraint so the regular Planner (not just
        # the next Replan prompt) avoids immediately repeating the same unproductive path.
        if replan.diagnosis:
            path_constraint = f"⚠️ 之前未达成目标的路径：{replan.diagnosis}。除非当前截图出现新的明确证据，否则不要重复。"
            if path_constraint not in self._global_constraints:
                self._global_constraints.append(path_constraint)

        if replan.strategy == "force_complete":
            print(f"  [Replan] replanner 判定验收条件已满足，强制完成")
            return self._advance(milestone, observation, history)

        if replan.strategy == "escalate_human":
            fallback = self._try_filter_fallback(
                milestone, can_degrade=replan.can_degrade_to_collection, read_inst=read_inst,
            )
            if fallback:
                return fallback
            milestone.status = "failed"
            self._current_id = self._next_milestone()
            return SupervisorStep(
                should_act=False,
                stop=self._current_id is None,
                stop_reason=replan.escalation_message or "升级人工介入",
                goal_completed=False,
                summary=replan.diagnosis,
                **_ctx(milestone, read_inst),
            )

        milestone.status = "running"
        return SupervisorStep(
            should_act=bool(replan.instruction),
            instruction=replan.instruction or None,
            stop=False,
            goal_completed=False,
            summary=f"子目标「{milestone.name}」尚未达成，第 {milestone.retry_count} 次调整策略。{replan.diagnosis}",
            is_home_screen=_is_home_identity(self._last_check.page_identity) if self._last_check else False,
            **_ctx(milestone, read_inst),
        )

    def _fail(self, milestone: Milestone, check: _SingleCheckResult, read_inst: Optional[str]) -> SupervisorStep:
        milestone.status = "failed"
        self._current_id = self._next_milestone()
        print(f"  子目标「{milestone.name}」失败")
        if self._current_id is None:
            return SupervisorStep(
                should_act=False, stop=True,
                stop_reason=f"子目标「{milestone.name}」重试 {MAX_RETRIES} 次后失败",
                goal_completed=False, summary=check.reason,
                **_ctx(milestone, read_inst),
            )
        return SupervisorStep(
            should_act=False, stop=False, goal_completed=False,
            summary=f"子目标「{milestone.name}」失败，跳过继续下一个。",
            **_ctx(self._milestones[self._current_id], read_inst),
        )

    def _terminal_step(self) -> SupervisorStep:
        failed = [m for m in self._milestones.values() if m.status == "failed"]
        pending = [m for m in self._milestones.values() if m.status == "pending"]
        if failed or pending:
            return SupervisorStep(
                should_act=False, stop=True, goal_completed=False,
                stop_reason=f"无可执行子目标；失败：{'、'.join(m.name for m in failed) or '无'}；未完成：{'、'.join(m.name for m in pending) or '无'}",
                summary="任务未完成，存在失败或依赖未满足的子目标。",
            )
        return SupervisorStep(
            should_act=False, stop=True, stop_reason="所有子目标已完成",
            goal_completed=True, summary="任务完成",
        )

    def _try_filter_fallback(
        self,
        milestone: Milestone,
        can_degrade: bool,
        read_inst: Optional[str],
    ) -> Optional[SupervisorStep]:
        if milestone.kind != "filter" or not can_degrade:
            return None
        dependent = next(
            (self._milestones[mid] for mid in self._order
             if self._milestones[mid].status == "pending"
             and milestone.id in self._milestones[mid].depends_on
             and self._milestones[mid].kind == "collection"),
            None,
        )
        if dependent is None:
            return None
        if _is_loop(dependent):
            filter_intent = milestone.success_condition
            dependent.scroll_stop_condition = (
                f"当可见内容不再满足筛选条件「{filter_intent}」时停止滚动"
            )
            dependent.observable_boundary = False
            dependent.scroll_budget = 15
        milestone.status = "done"
        self._current_id = dependent.id
        self._recent_screenshots.clear()
        msg = (
            f"子目标「{milestone.name}」无法精确筛选，已降级为在「{dependent.name}」阶段收集并过滤。"
        )
        if msg not in self._global_constraints:
            self._global_constraints.append(msg)
        print(f"  [Fallback] {msg}")
        return SupervisorStep(
            should_act=False, stop=False, goal_completed=False, summary=msg,
            **_ctx(dependent, read_inst),
        )

    # ── LLM invocations ───────────────────────────────────────────────

    def _record_failure_constraint(
        self,
        milestone: Milestone,
        check: _SingleCheckResult,
        history: list[PolicyTurn],
    ) -> None:
        reason = check.stuck_reason or check.reason
        if "planner 陷入重复" in reason or "连续给出相似指令" in reason:
            return
        # 连续调值类不记「禁止重复此指令」：会把唯一可靠的拖动操作拉黑，逼系统退化成点击 picker
        # （iOS 滚轮不认点击）。停滞时的纠偏靠 replan 换连续策略（换列/加大步长/调顺序），而非禁操作。
        if milestone.is_iterative:
            return
        last_action = next(
            (t for t in reversed(history)
             if t.supervisor and t.supervisor.milestone_id == milestone.id
             and t.supervisor.instruction and t.executed),
            None,
        )
        if not last_action:
            return
        instruction = last_action.supervisor.instruction
        constraint = f"指令「{instruction}」未达成目标：{reason}。优先尝试当前截图中不同的可见入口。"
        if constraint not in self._global_constraints:
            self._global_constraints.append(constraint)
            print(f"  [Constraint] {constraint}")

    def _single_check(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
        extra: str = "",
    ) -> _SingleCheckResult:
        app_name = self._app_name
        if not app_name:
            for t in reversed(history):
                if t.supervisor and t.supervisor.app_name:
                    app_name = t.supervisor.app_name
                    break
        return run_checker(
            milestone, observation, history,
            app_name=app_name,
            task_type=self.task_type,
            constraints=self._global_constraints,
            extra=extra,
            prompts=self._prompts,
            check_knowledge=self._check_knowledge,
        )

    def _loop_check(
        self,
        milestone: Milestone,
        observation: Observation,
        history: list[PolicyTurn],
    ) -> _LoopFrameResult:
        return run_loop_check(
            milestone, observation, history, constraints=self._global_constraints,
            prompts=self._prompts,
        )

    def _select_sections(self, milestone: Milestone, check: _SingleCheckResult) -> list[str]:
        """Resolve which knowledge sections to inject this turn, via the KnowledgeSelector.

        Cache key = (milestone id, normalized page_identity): selection only changes when
        the page or the milestone changes, so most turns reuse the cached stems and cost
        nothing. Empty selections are cached too (a page with no relevant sections should
        not retry every turn). On selector failure nothing is cached — the turn falls back
        to the zero-cost page_identity fuzzy match and the next turn retries the LLM."""
        if self._pk is None:
            return []
        page_id = check.page_identity or ""
        key = (milestone.id, _norm_page(page_id))
        if key in self._selector_cache:
            return self._selector_cache[key]
        try:
            with _Timer(self._timings, self._timings_order, "selector", self._token_usage):
                sel = run_selector(
                    self._goal, milestone, page_id,
                    self._pk.selector_manifest(),
                    prompts=self._prompts,
                )
            stems = self._pk.by_ids(sel.section_ids)
            if stems or sel.section_ids:
                names = "、".join(stems) if stems else "（ID 未命中）"
                print(f"  [Selector] {names}" + (f" — {sel.reason}" if sel.reason else ""))
            self._selector_cache[key] = stems
            return stems
        except Exception as exc:  # noqa: BLE001 — selector must never block the planner
            print(f"  [Selector] 调用失败，回退 page_identity 模糊匹配：{exc}")
            return self._pk.pick([], page_id)

    def _elements_for(self, milestone: Milestone, check: _SingleCheckResult) -> Optional[str]:
        """Element knowledge for instruction generation: the per-section bodies the
        KnowledgeSelector picked for this (milestone, page) — cached — when section knowledge
        exists, else the full _elements.md blob as fallback. Shared by planner and replanner so
        both inject the same focused slice instead of the whole 600+-line elements blob."""
        if self._pk:
            self._last_sections_loaded = self._select_sections(milestone, check)
            return self._pk.bodies(self._last_sections_loaded)
        return self._elements_knowledge

    def _invoke_planner(
        self,
        milestone: Milestone,
        check: _SingleCheckResult,
        observation: Observation,
        history: list[PolicyTurn],
        extra: str = "",
    ) -> _PlanResult:
        elements = self._elements_for(milestone, check)
        return run_planner(
            milestone, check, observation, history,
            constraints=self._global_constraints,
            extra=extra,
            app_knowledge=self._app_knowledge,
            elements_knowledge=elements,
            prompts=self._prompts,
        )

    def _invoke_loop_scroll(
        self,
        milestone: Milestone,
        frame: _LoopFrameResult,
        observation: Observation,
    ) -> _PlanResult:
        prompt = self._prompts.loop_scroll.format(
            milestone_name=milestone.name,
            milestone_desc=milestone.description,
            constraints=json.dumps(self._global_constraints, ensure_ascii=False),
            frame_summary=frame.summary,
        )
        plan_schema = self._prompts.plan_result_schema or _PlanResult
        return invoke_structured(self._llm(), self._msgs(prompt, observation), plan_schema)

    def _invoke_replanner(
        self,
        milestone: Milestone,
        check: _SingleCheckResult,
        observation: Observation,
        history: list[PolicyTurn],
        extra: str = "",
    ) -> _ReplanResult:
        tried = sorted({
            t.supervisor.instruction
            for t in history
            if t.supervisor
            and t.supervisor.instruction
            and t.supervisor.milestone_id == milestone.id
        })
        tried_text = "\n".join(f"  - 「{i}」" for i in tried) if tried else "  （无）"
        done_lines = [
            f"  - [{m.id}] {m.name}（已完成，不要退回到该状态）"
            for m in self._milestones.values()
            if m.status == "done" and m.id != milestone.id
        ]
        done_context = "\n".join(done_lines) if done_lines else "  （无）"
        prompt = self._prompts.replan.format(
            milestone_name=milestone.name,
            milestone_desc=milestone.description,
            success_condition=milestone.success_condition,
            stuck_reason=check.stuck_reason or check.reason,
            issues=json.dumps(check.issues, ensure_ascii=False),
            retry_count=milestone.retry_count,
            constraints=json.dumps(self._global_constraints, ensure_ascii=False),
            failure_hints=json.dumps(milestone.failure_hints, ensure_ascii=False),
            completed_milestones=done_context,
            history_text=_format_history(history),
            tried_instructions=tried_text,
        )
        if extra:
            prompt += f"\n\n## 输出修正要求\n{extra}"
        msgs = self._msgs(prompt, observation)
        _inject_knowledge(msgs, self._app_knowledge, self._elements_for(milestone, check))
        result = invoke_structured(self._llm(), msgs, _ReplanResult)
        if self._is_sequence(result.instruction):
            print("  [Replan] 多步序列，重试...")
            result = self._invoke_replanner(
                milestone, check, observation, history,
                extra="你刚才输出了多个步骤，请只返回一个原子操作。",
            )
        return result

    # ── Stuck detection ───────────────────────────────────────────────

    def _check_screen_similarity(
        self, observation: Observation, action_center: Optional[tuple[float, float]] = None
    ) -> Optional[_SingleCheckResult]:
        self._recent_screenshots.append((observation.png_bytes, action_center))
        if len(self._recent_screenshots) > STUCK_SCREEN_WINDOW:
            self._recent_screenshots.pop(0)
        if len(self._recent_screenshots) < STUCK_SCREEN_WINDOW:
            return None

        # Two-tier per adjacent step: a step is "no change" ONLY when the whole frame stayed
        # similar (全局, 灰度) AND the region the action touched did not change (局部, 1-SSIM). A
        # picker step keeps global ~99.9% but moves the touched region (1-SSIM ~0.24) → NOT
        # no-change → not stuck.
        frames = self._recent_screenshots
        gsims: list[float] = []
        locs: list[Optional[float]] = []
        for i in range(1, len(frames)):
            gs, lc = region_change(frames[i - 1][0], frames[i][0], frames[i][1])
            gsims.append(gs)
            locs.append(lc)

        def _no_change(gs: float, lc: Optional[float]) -> bool:
            # 全局相似(灰度) 且 局部未发生结构变化(1-SSIM ≤ 生效阈值)。lc=None(无坐标)→只看全局。
            return gs >= STUCK_SCREEN_SIMILARITY and (lc is None or lc <= CHANGE_SSIM_DIST_THR)

        if all(_no_change(gs, lc) for gs, lc in zip(gsims, locs)):
            gstr = ", ".join(f"{g:.2%}" for g in gsims)
            lstr = ", ".join("∅" if l is None else f"{l:.3f}" for l in locs)
            frozen = max(gsims) >= STUCK_SCREEN_FROZEN
            tag = "屏幕冻结（局部+全局均无变化）" if frozen else "连续无变化（局部+全局）"
            print(f"  [SimStuck] 全局[{gstr}] 局部[{lstr}] → {tag}")
            return _SingleCheckResult(
                status="stuck",
                reason=f"连续 {STUCK_SCREEN_WINDOW} 帧没有看到与目标相关的页面变化",
                stuck_reason="上一步操作后页面没有出现新内容或目标状态，需要尝试其他可见入口",
                issues=["连续多帧未看到页面状态推进"],
                summary="屏幕连续无变化",
                frozen=frozen,
            )
        # AB-cycle: whole-state oscillation (current ≈ 2-back globally but ≠ adjacent). Uses
        # the global tier — oscillation is a full-state flip, not a local nudge.
        sim_2back, _ = region_change(frames[-1][0], frames[-3][0])
        sim_adj, _ = region_change(frames[-1][0], frames[-2][0])
        if sim_2back >= STUCK_SCREEN_SIMILARITY and sim_adj < STUCK_SCREEN_SIMILARITY:
            print(f"  [SimStuck] 2back={sim_2back:.2%}, adj={sim_adj:.2%} → AB 循环")
            return _SingleCheckResult(
                status="stuck",
                reason="页面在两个可见状态之间来回切换，验收条件仍未出现",
                stuck_reason="页面在两个状态之间反复切换，需要换路径或先关闭当前弹窗/面板",
                issues=["页面状态来回切换"],
                summary="页面在两个状态之间反复切换",
            )
        return None

    def _check_instruction_repetition(
        self,
        history: list[PolicyTurn],
        milestone_id: str,
    ) -> Optional[_SingleCheckResult]:
        recent_insts = [
            t.supervisor.instruction
            for t in history[-STUCK_REPEAT_WINDOW:]
            if t.supervisor
            and t.supervisor.instruction
            and t.supervisor.milestone_id == milestone_id
        ]
        if len(recent_insts) < STUCK_REPEAT_WINDOW:
            return None
        base_words = set(recent_insts[-1].split())
        sims = [
            len(base_words & set(inst.split())) / max(len(base_words), len(set(inst.split())), 1)
            for inst in recent_insts[:-1]
        ]
        if all(s >= STUCK_REPEAT_WORD_OVERLAP for s in sims):
            sim_str = ", ".join(f"{s:.2%}" for s in sims)
            print(f"  [RepStuck] {sim_str} → 指令连续重复")
            return _SingleCheckResult(
                status="stuck",
                reason=f"连续 {STUCK_REPEAT_WINDOW} 步给出相似指令，当前页面仍未满足验收条件",
                stuck_reason="连续相似指令未达成目标，需要改用当前截图中的其他可见入口或操作顺序",
                issues=["连续操作策略过于相似"],
                summary="操作陷入重复循环",
            )
        return None

    @staticmethod
    def _action_center(action) -> Optional[tuple[float, float]]:
        """The action's center as normalized 0-1000 ``(x, y)``, or None when it carries no
        coordinates (press_enter / home / back / stop). tap / scroll / drag all have x/y; the
        局部 stuck tier uses this to inspect the screen region the agent actually touched."""
        if action is None:
            return None
        x, y = getattr(action, "x", None), getattr(action, "y", None)
        if x is None or y is None:
            return None
        return (float(x), float(y))

    @staticmethod
    def _is_value_adjust(action) -> bool:
        """Is this a picker / value adjustment, where repeating the SAME instruction is the
        normal mechanism (not a stuck loop)? android pickers are ``scroll``; iphone pickers
        are ``drag target_area=picker_*``. Used to suppress instruction-repetition stuck once
        the two-tier screen check has confirmed the touched region IS moving.

        ``getattr``: AndroidAction has no ``target_area`` field — the scroll branch covers
        android, the drag branch covers iphone."""
        if action is None:
            return False
        ta = getattr(action, "target_area", "") or ""
        return action.action_type == "scroll" or (
            action.action_type == "drag" and ta.startswith("picker_")
        )

    @staticmethod
    def _extract_progress_value(check: _SingleCheckResult) -> str:
        """从 checker 输出抽出「当前值」作为连续操作的进展度量。

        连续调值 section 要求 checker 在 missing_evidence 写「当前值=...」；优先取它，
        取不到则从 reason/summary 抽常见时间 picker 值，再退回 summary。返回归一化字符串供逐轮比对。
        """
        for ev in check.missing_evidence or []:
            m = re.search(r"当前值\s*[=:：]\s*(.+)", ev.strip())
            if m:
                return re.sub(r"\s+", "", m.group(1))
        text = f"{check.reason or ''}\n{check.summary or ''}"
        time_match = re.search(r"(上午|下午|AM|PM)?\s*0?(\d{1,2})\s*[:：]\s*0?(\d{1,2})", text, flags=re.IGNORECASE)
        if time_match:
            period = (time_match.group(1) or "").upper()
            hour = int(time_match.group(2))
            minute = int(time_match.group(3))
            return f"{period}{hour:02d}:{minute:02d}"
        hour_match = re.search(r"小时(?:列)?(?:中间(?:高亮|选中)?行?|选中值|显示)?(?:为|=|显示为)?['「“]?\s*0?(\d{1,2})", text)
        minute_match = re.search(r"分钟(?:列)?(?:中间(?:高亮|选中)?行?|选中值|显示)?(?:为|=|显示为)?['「“]?\s*0?(\d{1,2})", text)
        if hour_match and minute_match:
            return f"{int(hour_match.group(1)):02d}:{int(minute_match.group(1)):02d}"
        return re.sub(r"\s+", "", check.summary or "")

    def _check_value_stall(
        self, milestone: Milestone, check: _SingleCheckResult,
    ) -> Optional[_SingleCheckResult]:
        """连续调值类专用卡住判据：被监控值连续 STUCK_VALUE_STALL_WINDOW 轮不变 = 真停滞。

        替代 SimStuck/RepStuck——对 picker 而言屏幕冻结、反复拖同一下都是正常的，唯有
        「当前值多轮没朝目标动」才是真卡住（方向错、步长恒不到一格、非法值回弹等）。
        """
        val = self._extract_progress_value(check)
        window = self._progress_values.setdefault(milestone.id, [])
        window.append(val)
        if len(window) > STUCK_VALUE_STALL_WINDOW:
            window.pop(0)
        if len(window) < STUCK_VALUE_STALL_WINDOW or not val:
            return None
        if len(set(window)) == 1:
            print(f"  [ValueStall] 连续 {STUCK_VALUE_STALL_WINDOW} 轮当前值停留「{val}」，未朝目标推进")
            # 触发后清空窗口：replan 会换新策略，让它重新攒满 STUCK_VALUE_STALL_WINDOW 轮再判，
            # 否则窗口仍满、下一轮值没立刻变就又立即 stall，新策略只有 1 轮机会、很快耗尽重试。
            window.clear()
            return _SingleCheckResult(
                status="stuck",
                reason=f"连续 {STUCK_VALUE_STALL_WINDOW} 轮当前值停留在「{val}」，调整未朝目标推进",
                stuck_reason="连续调值无进展：当前值多轮未变化",
                issues=["监控值多轮未朝目标推进（疑似方向错/步长不足/非法值回弹）"],
                summary=check.summary,
            )
        return None

    # ── Decompose & routing ───────────────────────────────────────────

    _MAX_DECOMPOSE_RETRIES = 2

    def _decompose(self, goal: str, observation: Observation) -> None:
        self._goal = goal  # kept for the KnowledgeSelector prompt context
        cfg = resolve_llm_config("supervisor.decompose")
        if not cfg.model:
            cfg = resolve_llm_config("supervisor")
        print(f"Supervisor: {cfg.provider} / {cfg.model}")
        llm = ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url)

        # @<file> references in the goal: read once here (retries reuse the same section,
        # no duplicate file reads / log lines).
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
            # @config raw text must reach the execution-time channel DETERMINISTICALLY.
            # LLM distillation into global_constraints proved unstable (same config: 8
            # fields one run, 0 the next), leaving planners with dangling "按配置设置"
            # references and no values. Constraints flow to checker/planner/replan every
            # turn, so append the raw section as one entry (capped — huge files would
            # bloat every per-turn prompt).
            _CAP = 3000
            snippet = (
                file_section if len(file_section) <= _CAP
                else file_section[:_CAP] + "\n…（配置过长已截断，其余以分解结果为准）"
            )
            self._global_constraints.append(snippet)

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
        msgs = self._msgs(self._prompts.decompose, observation)
        user_parts: list[dict] = [{"type": "text", "text": f"用户任务：{goal}"}]
        if file_section:
            user_parts.append({"type": "text", "text": f"\n{file_section}"})
        if self._app_knowledge:
            user_parts.append({"type": "text", "text": f"\n## 应用导航知识\n{self._app_knowledge}"})
        # Decomposition needs page/flow structure (navigation: _app + _deploy + _skill), not the
        # pixel-level element catalog — the 600+-line _elements.md blob was pure token weight here
        # (the planner/replanner carry elements, progressively, where instructions are generated).
        if feedback:
            fb = "\n".join(f"  - {i}" for i in feedback)
            user_parts.append({"type": "text", "text": f"\n上一轮分解存在以下问题，请修正：\n{fb}"})
        msgs[1].content = user_parts + msgs[1].content
        resp = invoke_structured(llm, msgs, _DecomposeResponse)

        self._global_constraints = resp.global_constraints
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

        # ── Acceptance-shape guards (deterministic; the prompt states these, the model obeys
        # them only ~60-70% of the time — so enforce them in the validate→feedback→retry loop) ──
        for m in self._milestones.values():
            sc = m.success_condition
            # 增量验收:条件式/幂等任务下「新增了N个」在已满足场景永不成立,应写终态(「至少N个…」)。
            if re.search(r"新增了|增加了|多出", sc):
                issues.append(
                    f"子目标「{m.name}」验收用了增量表述（{sc[:30]}…）：改写为完成后应处于的终态"
                    "（如「列表中至少有 N 个符合要求的条目」），不要写相对变化"
                )
            # 中间态验收:action 类不能只验「弹出/展开/聚焦」等过程态,要验最终可见结果。
            if m.kind == "action" and re.search(r"(弹出|展开|聚焦|打开).{0,6}(窗口|弹窗|对话框|下拉|面板)(?!.*(成功|完成|已|结果))", sc):
                issues.append(
                    f"子目标「{m.name}」验收停在中间态（{sc[:30]}…）：action 验收要写操作的最终可见结果"
                    "（提交后的成功提示/状态更新/结果），不是「弹出某弹窗」这类过程态"
                )

        # 记录类任务必须有读取/采集落点：goal 要求「记录/报告原因」却没有 collection 或读取语义
        # 的 milestone → 结果不会被读出来（实测 connectivity run output 含糊其辞）。
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
            MilestoneSupervisorPolicy._goal_period_constraint(goal),
            MilestoneSupervisorPolicy._goal_repeat_constraint(goal),
            MilestoneSupervisorPolicy._goal_named_value_constraint(goal),
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
            if global_text not in self._global_constraints:
                self._global_constraints.append(global_text)
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
            # 误标拯救：带读取/记录产出的「verification」其实是任务脉络的终读步（查看订单状态/
            # 记录检测结果——schema alias 还会把 kind=report/summary 归一成 verification 送进
            # 这里），一刀切删除会吞掉最后一环（~30% 分解中招）。救成 collection/read_once；
            # 纯验证项（验证 X 已完成,无读取产出）照删——checker 本就逐步验收,它们是冗余。
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
                        f"全局约束：{json.dumps(self._global_constraints, ensure_ascii=False)}"
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

    def _llm(self) -> ChatOpenAI:
        return _make_llm()

    def _msgs(self, system_prompt: str, observation: Observation) -> list:
        return _build_msgs(system_prompt, observation.png_bytes, image_resize=self._prompts.image_resize)

    @staticmethod
    def _picker_drag_steps(plan: _PlanResult) -> Optional[int]:
        """从 planner 的结构化当前值/目标值算出 picker drag 要走的格数（绝对值）。

        仅在 drag_column 已填、且当前值与目标值都给出时生效。除返回格数外，还用这两个数
        直接定方向——同一列的数字比较永远有效，比从 instruction 文本正则抠数字更可靠（正则在
        指令只写目标值、如「直到显示21日」时会抠不到当前值而失效）。非 picker drag 或信息
        不全时返回 None，由下游回退到旧的文本解析路径。
        """
        if not getattr(plan, "drag_column", None):
            return None
        cur = getattr(plan, "drag_current_value", None)
        tgt = getattr(plan, "drag_target_value", None)
        if cur is None or tgt is None:
            return None
        if tgt != cur:
            column = (getattr(plan, "drag_column", None) or "").strip().lower()
            if column == "minute":
                forward = (tgt - cur) % 60
                backward = (cur - tgt) % 60
                plan.direction = "increase" if forward <= backward else "decrease"
                return min(forward, backward)
            if column == "hour":
                forward = (tgt - cur) % 12
                backward = (cur - tgt) % 12
                plan.direction = "increase" if forward <= backward else "decrease"
                return min(forward, backward)
            plan.direction = "increase" if tgt > cur else "decrease"
        return abs(tgt - cur)

    @staticmethod
    def _fix_picker_direction(plan: _PlanResult) -> None:
        col = getattr(plan, "drag_column", None) or ""
        col_suffix = {"year": "年", "month": "月", "day": "日"}.get(col, "")
        if not col_suffix:
            return
        # 跨边界保护：拖某列时，若指令里**更高位的列**（拖 day→看 月/年，拖 month→看 年）
        # 出现两个不同的值，说明目标跨越了该列边界（如 3月31日→4月7日，月份 3≠4）。此时单看
        # 本列数字（31 vs 7）会得出相反方向，必须跳过翻转——正确做法是先对齐高位列（见 PLAN_PROMPT）。
        higher_suffixes = {"day": ["月", "年"], "month": ["年"], "year": []}.get(col, [])
        for hs in higher_suffixes:
            hvals = re.findall(rf"(\d+){hs}", plan.instruction)
            if len(hvals) >= 2 and len(set(hvals[:2])) > 1:
                print(f"  [Planner] direction fix 跳过：跨{hs}边界（{hvals[0]}{hs}→{hvals[1]}{hs}），本列数字比较无效")
                return
        nums = re.findall(rf"(\d+){col_suffix}", plan.instruction)
        if len(nums) < 2:
            return
        cur, tgt = int(nums[0]), int(nums[1])
        if tgt == cur:
            return
        correct = "increase" if tgt > cur else "decrease"
        if plan.direction != correct:
            print(f"  [Planner] direction fix: {plan.direction} → {correct} ({cur}→{tgt})")
            plan.direction = correct
            if correct == "increase":
                for old, new in [("向下拖动", "向上拖动"), ("下拉", "上拉"), ("往下", "往上")]:
                    plan.instruction = plan.instruction.replace(old, new)
            else:
                for old, new in [("向上拖动", "向下拖动"), ("上拉", "下拉"), ("往上", "往下")]:
                    plan.instruction = plan.instruction.replace(old, new)

    @staticmethod
    def _is_sequence(instruction: str) -> bool:
        text = instruction.strip()
        markers = ("操作序列", "步骤", "\n1.", "\n2.", "1.", "2.", "；2", ";2")
        return any(m in text for m in markers)

    def _is_repeated_instruction(
        self, instruction: str, milestone_id: str, history: list[PolicyTurn],
    ) -> bool:
        from difflib import SequenceMatcher
        _strip = lambda s: re.sub(r"[，。、；：""''《》\s（）\(\)]", "", s.strip())
        _scroll_words = ("滚动", "滑动", "拖动", "拖拽", "scroll", "drag")
        n_new = _strip(instruction)

        stuck_tried: set[str] = set()
        all_tried: list[str] = []
        for idx, t in enumerate(history):
            sv = t.supervisor
            if not sv or not sv.instruction:
                continue
            if sv.milestone_id == milestone_id:
                all_tried.append(sv.instruction)
            next_sv = history[idx + 1].supervisor if idx + 1 < len(history) else None
            if (
                next_sv
                and ("卡住" in (next_sv.summary or "") or "重试" in (next_sv.summary or ""))
            ):
                stuck_tried.add(sv.instruction)

        def _similar(new: str, old: str) -> bool:
            n_old = _strip(old)
            return bool(n_new and n_old) and SequenceMatcher(None, new, n_old).ratio() >= 0.6

        for old in stuck_tried:
            if _similar(n_new, old):
                return True

        if any(w in instruction for w in _scroll_words):
            return False
        similar_count = sum(1 for old in all_tried if _similar(n_new, old))
        return similar_count >= 2
