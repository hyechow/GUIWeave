"""Planner-based back navigation: two-stage decision pipeline.

Architecture per round:
  1. Fast identity check (no LLM) — return if already at target or ancestor page
  2. Planner (1 LLM call) — sees current + target screenshots + history → instruction string
  3. Action Policy (1 LLM call) — sees current screenshot + instruction → ActionDecision with coords
  4. Execute (tap/scroll/home) + YOLO calibration
  5. Verify page change → update state → loop
"""

from __future__ import annotations

import base64
import hashlib
import re
import time
from collections.abc import Callable
from difflib import SequenceMatcher
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from llm.structured import invoke_structured
from policy_expr.config import resolve_llm_config
from policy_expr.executor import is_valid_tap, logical_xy
from policy_expr.policies.base import resize_to_logical_png
from policy_expr.policies.structured_output import StructuredOutputPolicy
from policy_expr.recon.back_nav import (
    BACK_SETTLE_SECONDS,
    _PIXEL_DIFF_THRESHOLD,
    _get_identity_comp,
    _match_stack,
    _navigate_forward,
    _pixel_diff_ratio,
    _yolo_detect_near,
    back_shot_path,
    make_nav_context,
    save_if_changed,
)
from policy_expr.schemas import ActionDecision, Observation


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_ROUNDS = 10
_MAX_CONSECUTIVE_NO_CHANGE = 3
_REPEAT_SIMILARITY_THRESHOLD = 0.6
_MAX_PAGE_VISITS = 3


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class _BackPlanResult(BaseModel):
    instruction: str = Field(description="下一步操作指令，描述要操作的 UI 元素，不包含坐标")
    rationale: str = Field(description="选择该操作的判断依据")


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

BACK_PLANNER_PROMPT = """\
你是 iPhone 应用返回导航规划器。根据两张截图（TARGET 和 CURRENT）以及触发操作描述，判断如何从 CURRENT 回到 TARGET。

## 判断流程（按顺序执行，满足即停）

### Q1：CURRENT 有浮层/弹窗覆盖吗？
检查 CURRENT 全屏：是否有弹窗、对话框、底部弹出面板或广告浮层叠在底层页面上方？
**即使目标页面或触发操作涉及 tab，只要 CURRENT 有弹窗就必须先处理弹窗，不要跳到 Q2。**

浮层必须满足：**大面积遮盖主要页面内容**（通常占屏幕 40% 以上），底层页面被遮住大部分，浮层有独立边框/阴影/半透明遮罩。

⚠️ 以下**不算**浮层：
- 小面积悬浮按钮（浮动购物车、小圆形广告、支付快捷入口等）
- 页面内嵌的小组件或卡片
- 底部固定的操作栏（即使看起来像独立面板）

→ 满足浮层条件时，根据弹窗内容选择操作：

**A. 确认/放弃类弹窗**（询问"是否保存""是否放弃修改""离开此页"等）
→ 优先选择能**离开当前页面**的选项（如"不保存"/"Discard""放弃"/"Abandon""离开"/"Leave""不保留"）
→ 如果弹窗的 × 按钮明确是"取消操作并返回"（而非"关闭弹窗留在当前页"），则点 × 也是合理的
→ 判断依据：观察弹窗整体语境，× 旁边是否有"取消"文字提示

**B. 其他弹窗**（广告、提示、授权请求等与回退无关的内容）
→ 指令 = "点击弹窗的关闭按钮"

→ 不满足浮层条件：进入 Q2。

### Q2：某一排 tab 栏的选中项发生了变化吗？
iPhone 应用通常同时存在两类 tab 栏，须分开判断：
- **底部导航 tab**：3-5 个固定入口（首页/消息/我/购物车等），在不同页面间通常保持不变，位于页面最底部
- **顶部分类 tab**：内容分类标签（推荐/热门/关注等），随内容区切换而变化，位于页面上方

逐排对比 TARGET 和 CURRENT，对每一排检查以下三点是否**同时**成立：
1. 该排在两图中均存在
2. 该排的高亮/选中项在两图中**确实不同**（两图相同则跳过该排）
3. 触发操作的名称能在该排选项里找到

**→ 某一排同时满足三点：指令 = "点击[顶部/底部] tab 栏中的「TARGET 中选中的 tab 名称」"**
→ 所有排均不满足：进入 Q3。

注意：
- 不要点击 CURRENT 中已处于选中状态的 tab，要点击 TARGET 中选中的那个
- 触发操作含「tab」不代表条件 3 自动满足，须逐字核实 tab 名称确实出现在该排选项里
- 如果 CURRENT 的页面结构与 TARGET 完全不同（有独立的导航栏、返回按钮），说明不是同页面的 tab 切换而是导航到了新页面，应走 Q3
- **同栏原则**：触发操作是顶部tab → 回退也点顶部tab栏；触发操作是底部tab → 回退也点底部tab栏。不要跨栏操作

### Q3：审视 CURRENT 页面的导航元素
仔细观察 CURRENT 页面，按以下优先级寻找可回退的 UI 元素：
**如果 CURRENT 有独立的返回箭头或关闭按钮，说明它是导航进入的新页面，即使触发操作涉及 tab 也应走 Q3。**

1. **左上角返回按钮**：是否存在 < ← 箭头或"返回"文字？→ 指令 = "点击左上角返回箭头"
2. **页面级关闭/取消按钮**：编辑页、发布页、详情页等通常有「取消」「关闭」「×」按钮 → 注意检查**左上角和右上角**两个位置 → 指令 = "点击「取消/关闭」按钮"
3. **右上角完成按钮**：iOS 常见模式，全屏详情页、编辑页右上角有「完成」「Done」→ 指令 = "点击右上角「完成」按钮"
4. **底部操作栏**：某些页面底部有「返回」「完成」等操作按钮 → 指令 = "点击底部的「返回/完成」按钮"

⚠️ 关键：必须根据 CURRENT 截图中**实际可见的 UI 元素**做判断，不要盲目假设有返回箭头。编辑类页面（表单填写、内容发布、设置编辑）通常没有 < 箭头，而是提供「取消」「关闭」或「完成」按钮。

## 规则
- 每步只输出一个原子操作
- 只描述要操作的 UI 元素，不要输出坐标
- 不重复之前失败的操作
- rationale 中简要说明你在 CURRENT 页面上看到了什么 UI 元素，以及为什么选择这个操作
"""


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _make_llm() -> ChatOpenAI:
    cfg = resolve_llm_config("back_nav")
    return ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url, temperature=0)


def _invoke_planner(
    target_png: bytes,
    current_png: bytes,
    nav_context: str = "",
    history: list[dict] | None = None,
    target_label: str = "",
    target_description: str = "",
) -> _BackPlanResult:
    """Ask the planner for the next navigation instruction."""
    llm = _make_llm()

    target_b64 = base64.b64encode(resize_to_logical_png(target_png)).decode()
    current_b64 = base64.b64encode(resize_to_logical_png(current_png)).decode()

    context_text = (
        "第一张(TARGET)是要回到的目标页面，第二张(CURRENT)是当前所在页面。"
        "请给出从 CURRENT 回到 TARGET 的下一步操作指令。"
    )
    if target_label:
        context_text += f"\n\n目标页面名称：「{target_label}」"
    if nav_context:
        context_text += f"\n\n触发操作：{nav_context}"
    if history:
        context_text += "\n\n之前的尝试："
        for i, h in enumerate(history[-6:], 1):
            inst = h.get("instruction", "?")
            result = h.get("result", "?")
            context_text += f"\n- 第{i}次：指令「{inst}」→ {result}"
        # Detect loop: multiple actions changed the page but didn't reach target
        changed_count = sum(1 for h in history if h.get("result") == "页面变化")
        if changed_count >= 4:
            context_text += (
                "\n\n⚠️ 你已经执行了多次操作且每次页面都有变化，但仍未到达目标页面。"
                "你可能陷入了交替循环（在两个页面之间来回切换）。"
                "请仔细审视 CURRENT 截图，选择一个**不同的**操作来打破循环。"
                "特别检查：如果当前有确认弹窗，不要点击 ×，而要点击弹窗中的具体选项（如「不保存」「放弃」）。"
            )

    messages = [
        SystemMessage(content=BACK_PLANNER_PROMPT),
        HumanMessage(content=[
            {"type": "text", "text": context_text},
            {"type": "text", "text": "TARGET（目标页面）:"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{target_b64}"}},
            {"type": "text", "text": "CURRENT（当前页面）:"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{current_b64}"}},
        ]),
    ]
    return invoke_structured(llm, messages, _BackPlanResult)


# ---------------------------------------------------------------------------
# Stuck detection helpers
# ---------------------------------------------------------------------------

def _is_repeated_instruction(instruction: str, history: list[dict], allow_once: bool = True) -> bool:
    """Check if instruction repeats a previously failed one.
    allow_once permits one retry of the same instruction.
    """
    failed = [h for h in history
              if "失败" in h.get("result", "") or "未变化" in h.get("result", "")]
    if not failed:
        return False
    clean_new = re.sub(r"[，。、；：""''《》\s（）\(\)]", "", instruction.strip())
    match_count = 0
    for h in failed:
        old = h.get("instruction", "")
        clean_old = re.sub(r"[，。、；：""''《》\s（）\(\)]", "", old.strip())
        if clean_new and clean_old:
            ratio = SequenceMatcher(None, clean_new, clean_old).ratio()
            if ratio >= _REPEAT_SIMILARITY_THRESHOLD:
                match_count += 1
    if match_count == 0:
        return False
    if allow_once and match_count <= 1:
        return False  # Allow one retry
    return True


def _page_hash(png_bytes: bytes) -> str:
    return hashlib.md5(png_bytes).hexdigest()[:6]


def _nav_print(msg: str, *, page_hash: str, round_num: int):
    print(f"    [nav2:{page_hash}] R{round_num} {msg}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def planned_return_to_initial(
    client,
    screenshot: Callable[[], bytes],
    nav_stack: list[tuple[bytes, tuple[float, float] | None]],
    before_back_bytes: bytes | None = None,
    out_dir: Path | None = None,
    tap_index: int = 0,
    nav_context: str = "",
    target_label: str = "",
    after_elements: list[dict] | None = None,
    debug_fn: Callable | None = None,
    status_cb: Callable[[str], None] | None = None,
) -> tuple[bool, list[dict]]:
    """Navigate back to the initial page using a planner-based loop.

    Drop-in replacement for back_nav.return_to_initial().
    """
    log: list[dict] = []
    id_comp = _get_identity_comp()
    top_level = len(nav_stack) - 1
    initial_bytes = nav_stack[top_level][0]

    action_policy = StructuredOutputPolicy()
    current_bytes = before_back_bytes or initial_bytes

    # Page visit counts for loop detection
    page_visit_counts: dict[str, int] = {}
    history: list[dict] = []
    consecutive_no_change = 0

    # Annotate nav_context with tab position from tap coordinates
    annotated_context = nav_context
    if nav_context and "tab" in nav_context:
        tap_coords = nav_stack[top_level][1]
        if tap_coords:
            _, tap_y = tap_coords
            if tap_y > 800:
                annotated_context = nav_context.replace("tab", "底部tab", 1)
            elif tap_y < 350:
                annotated_context = nav_context.replace("tab", "顶部tab", 1)

    for round_num in range(1, _MAX_ROUNDS + 1):
        ph = _page_hash(current_bytes)
        _nav_print(f"开始规划", page_hash=ph, round_num=round_num)
        if status_cb:
            status_cb(f"← 回退 R{round_num} 规划中")

        # Save before screenshot for trace
        if out_dir:
            before_path = out_dir / f"R{round_num}_before_{ph}.png"
            before_path.write_bytes(current_bytes)

        # ── Step 1: Fast identity check (no LLM) ──
        matched_level = _match_stack(id_comp, nav_stack, current_bytes)
        if matched_level >= 0:
            is_initial = matched_level == top_level
            level_desc = "initial" if is_initial else f"L{matched_level}"
            sim = id_comp.raw_similarity(nav_stack[matched_level][0], current_bytes)
            _nav_print(f"已匹配 {level_desc} ({sim:.3f})", page_hash=ph, round_num=round_num)
            if not is_initial:
                _navigate_forward(client, nav_stack, matched_level, screenshot,
                                  log=log, tap_index=tap_index, out_dir=out_dir)
            return True, log

        # ── Step 2: Planner (1 LLM call) ──
        plan = _invoke_planner(
            initial_bytes, current_bytes,
            nav_context=annotated_context,
            history=history,
            target_label=target_label,
        )
        _nav_print(f"指令: {plan.instruction} ({plan.rationale})",
                   page_hash=ph, round_num=round_num)

        # Reject repeated instructions
        if _is_repeated_instruction(plan.instruction, history):
            _nav_print("指令重复已失败操作，重试...", page_hash=ph, round_num=round_num)
            plan = _invoke_planner(
                initial_bytes, current_bytes,
                nav_context=annotated_context,
                history=history,
                target_label=target_label,
            )
            if _is_repeated_instruction(plan.instruction, history):
                _nav_print("重试仍重复，放弃", page_hash=ph, round_num=round_num)
                log.append({"round": round_num, "instruction": plan.instruction,
                            "result": "重复指令放弃", "success": False})
                break

        # ── Step 3: Action Policy (1 LLM call) ──
        obs = Observation(png_bytes=current_bytes, source="planned_back_nav")
        decision = action_policy.decide(obs, plan.instruction)

        if decision.not_found_reason:
            _nav_print(f"目标不可见: {decision.not_found_reason}",
                       page_hash=ph, round_num=round_num)
            history.append({"instruction": plan.instruction, "result": f"元素不可见: {decision.not_found_reason}"})
            log.append({"round": round_num, "strategy": "planner",
                        "instruction": plan.instruction,
                        "result": f"元素不可见: {decision.not_found_reason}",
                        "success": False})
            continue

        action = decision.action

        # ── Step 4: Execute ──
        if action.action_type == "home":
            _nav_print("执行返回主屏", page_hash=ph, round_num=round_num)
            result = client.press_home()
            if "Failed" in result:
                from policy_expr.executor import WIN_W, WIN_H
                result = client.tap(WIN_W / 2, WIN_H - 16)
            tap_xy = None
        elif action.action_type in ("tap", "click") and action.x is not None and action.y is not None:
            ax, ay = action.x, action.y
            # YOLO calibration
            yolo_point = _yolo_detect_near(current_bytes, ax, ay)
            if yolo_point:
                _nav_print(f"YOLO校准: ({ax:.0f},{ay:.0f}) → ({yolo_point[0]:.0f},{yolo_point[1]:.0f})",
                           page_hash=ph, round_num=round_num)
                ax, ay = yolo_point
            if not is_valid_tap(ax, ay):
                _nav_print(f"坐标越界 ({ax:.0f},{ay:.0f})", page_hash=ph, round_num=round_num)
                history.append({"instruction": plan.instruction, "result": "坐标越界"})
                log.append({"round": round_num, "strategy": "planner",
                            "instruction": plan.instruction,
                            "result": "坐标越界", "success": False})
                continue
            lx, ly = logical_xy(ax, ay)
            tap_xy = (ax, ay)
            _nav_print(f"执行点击 ({lx:.0f},{ly:.0f})", page_hash=ph, round_num=round_num)
            result = client.tap(lx, ly)
        elif action.action_type == "scroll" and action.direction:
            _execute_scroll(action)
            tap_xy = (action.x or 500, action.y or 500)
            result = "scroll"
        else:
            _nav_print(f"不支持的动作: {action.action_type}", page_hash=ph, round_num=round_num)
            history.append({"instruction": plan.instruction, "result": f"不支持: {action.action_type}"})
            log.append({"round": round_num, "strategy": "planner",
                        "instruction": plan.instruction,
                        "result": f"不支持的动作: {action.action_type}",
                        "success": False})
            continue

        # Check tap failure
        if isinstance(result, str) and ("failed" in result.lower() or "interrupted" in result.lower()):
            _nav_print(f"执行失败: {result}", page_hash=ph, round_num=round_num)
            history.append({"instruction": plan.instruction, "result": f"执行失败: {result}"})
            log.append({"round": round_num, "strategy": "planner",
                        "instruction": plan.instruction,
                        "result": f"执行失败: {result}", "success": False})
            continue

        # ── Step 5: Verify ──
        time.sleep(BACK_SETTLE_SECONDS)
        after_bytes = screenshot()
        after_ph = _page_hash(after_bytes)

        # Fast check: reached target or ancestor page?
        matched_level = _match_stack(id_comp, nav_stack, after_bytes)
        if matched_level >= 0:
            is_initial = matched_level == top_level
            level_desc = "initial" if is_initial else f"L{matched_level}"
            sim = id_comp.raw_similarity(nav_stack[matched_level][0], after_bytes)
            _nav_print(f"→ 匹配 {level_desc} ({sim:.3f})", page_hash=ph, round_num=round_num)
            if status_cb:
                status_cb(f"← 回退 R{round_num} ✓ {'回到初始页' if is_initial else f'→ {level_desc}'}")
            save_path = back_shot_path(out_dir, tap_index, len(log) + 1)
            shot_str = save_if_changed(after_bytes, save_path)
            log.append({
                "round": round_num, "strategy": "planner",
                "instruction": plan.instruction, "rationale": plan.rationale,
                "result": level_desc, "score": round(sim, 3), "success": is_initial,
                "page_from": ph, "page_to": after_ph, "screenshot": shot_str,
                **({"tap_xy": [round(tap_xy[0]), round(tap_xy[1])]} if tap_xy else {}),
            })
            if not is_initial:
                _navigate_forward(client, nav_stack, matched_level, screenshot,
                                  log=log, tap_index=tap_index, out_dir=out_dir)
            return True, log

        # Did anything change? Simple pixel diff — planner decides what happened
        diff_ratio = _pixel_diff_ratio(current_bytes, after_bytes)
        if diff_ratio < _PIXEL_DIFF_THRESHOLD:
            consecutive_no_change += 1
            _nav_print(f"未变化 (pixel={diff_ratio:.4f})", page_hash=ph, round_num=round_num)
            history.append({"instruction": plan.instruction, "result": "未变化"})
            log.append({
                "round": round_num, "strategy": "planner",
                "instruction": plan.instruction, "rationale": plan.rationale,
                "result": "未变化", "success": False,
                "page_from": ph, "page_to": after_ph,
                **({"tap_xy": [round(tap_xy[0]), round(tap_xy[1])]} if tap_xy else {}),
            })
            if consecutive_no_change >= _MAX_CONSECUTIVE_NO_CHANGE:
                _nav_print("连续无变化，放弃", page_hash=ph, round_num=round_num)
                break
            continue

        # Something changed — update state, let planner decide next step
        consecutive_no_change = 0
        _nav_print(f"页面变化 (pixel={diff_ratio:.4f})", page_hash=ph, round_num=round_num)

        save_path = back_shot_path(out_dir, tap_index, len(log) + 1)
        shot_str = save_if_changed(after_bytes, save_path)

        page_visit_counts[after_ph] = page_visit_counts.get(after_ph, 0) + 1
        if page_visit_counts[after_ph] >= _MAX_PAGE_VISITS:
            _nav_print(f"页面循环 ({after_ph} 访问{page_visit_counts[after_ph]}次)，放弃",
                       page_hash=ph, round_num=round_num)
            log.append({
                "round": round_num, "strategy": "planner",
                "instruction": plan.instruction, "rationale": plan.rationale,
                "result": f"页面循环 ({after_ph})", "success": False,
                "page_from": ph, "page_to": after_ph, "screenshot": shot_str,
                **({"tap_xy": [round(tap_xy[0]), round(tap_xy[1])]} if tap_xy else {}),
            })
            break

        history.append({"instruction": plan.instruction, "result": "页面变化"})
        log.append({
            "round": round_num, "strategy": "planner",
            "instruction": plan.instruction, "rationale": plan.rationale,
            "result": "变化", "success": False,
            "page_from": ph, "page_to": after_ph, "screenshot": shot_str,
            **({"tap_xy": [round(tap_xy[0]), round(tap_xy[1])]} if tap_xy else {}),
        })
        current_bytes = after_bytes

    _nav_print("回退失败", page_hash=_page_hash(current_bytes), round_num=round_num)
    if status_cb:
        status_cb(f"← 回退失败 (R{round_num})")
    return False, log


# ---------------------------------------------------------------------------
# Scroll helper
# ---------------------------------------------------------------------------

def _execute_scroll(action) -> None:
    """Execute a scroll action via Quartz events."""
    import subprocess
    from policy_expr.executor import (
        WIN_W, WIN_H, SCROLL_TICKS, SCROLL_DELTA, SCROLL_INTERVAL,
        _find_iphone_window, _quartz_hover,
    )
    from Quartz import (
        CGEventCreateScrollWheelEvent,
        CGEventSetLocation,
        CGEventPost,
        kCGScrollEventUnitPixel,
        kCGHIDEventTap,
    )

    origin = _find_iphone_window()
    if origin is None:
        print("  iPhone Mirroring 窗口未找到，无法滚轮")
        return
    wx, wy = origin

    subprocess.run(["osascript", "-e", 'tell application "iPhone Mirroring" to activate'],
                   capture_output=True)
    time.sleep(0.3)

    ax = action.x if action.x is not None else 500
    ay = action.y if action.y is not None else 500
    sx = wx + ax / 1000 * WIN_W
    sy = wy + ay / 1000 * WIN_H

    direction = (action.direction or "").strip().lower()
    if direction in ("up", "向上", "upward"):
        delta = SCROLL_DELTA
    elif direction in ("down", "向下", "downward"):
        delta = -SCROLL_DELTA
    elif direction in ("left", "向左", "leftward"):
        delta = -SCROLL_DELTA
    elif direction in ("right", "向右", "rightward"):
        delta = SCROLL_DELTA
    else:
        return

    _quartz_hover(sx, sy)
    time.sleep(0.1)

    for i in range(SCROLL_TICKS):
        ev = CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitPixel, 1, delta)
        CGEventSetLocation(ev, (sx, sy))
        CGEventPost(kCGHIDEventTap, ev)
        if i < SCROLL_TICKS - 1:
            time.sleep(SCROLL_INTERVAL)
