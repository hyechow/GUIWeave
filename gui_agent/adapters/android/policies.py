"""Vision-only Android action policy: screenshot + instruction -> one Action.

Mirrors the iphone ``StructuredOutputPolicy`` LLM machinery — same config via
``resolve_llm_config('action_policy')``, same structured-output call via
``invoke_structured`` into an ``ActionDecision``, same ``BaseActionPolicy`` base —
but with an ANDROID system prompt: operate a phone touchscreen, output ONE action
within the android action vocabulary (tap / type / clear_text / press_enter /
scroll / drag / home / back / recents / stop).

VISION-ONLY: the screenshot is sent as-is (downscaled only if very large) — it is
NOT the iphone 2x retina image, so ``resize_to_logical_png`` is deliberately NOT
used. No picker, no DOM.

PROMPT HYGIENE: the system prompt lists ONLY what Android has (positive vocabulary
+ Android idioms — 三大金刚键 / 通知栏 / 底部导航 / 软键盘). It does NOT contain
cross-platform negations ("这不是 iPhone / 没有 picker / 不要输出 value_direction"):
those leak iphone field names into the prompt, raise their salience, and pollute it.
The shared schema's iphone-only fields are simply never described here, so the model
does not fill them; the executor ignores them anyway.
"""

from __future__ import annotations

import io

from dotenv import load_dotenv

from gui_agent.core.policies.base import BaseActionPolicy
from gui_agent.adapters.android.actions import AndroidActionDecision

load_dotenv()


SYSTEM_PROMPT = """\
你是一个 Android 手机操作执行器。
用户会提供 Android 手机截图和一个具体的操作指令。你只需要找到目标元素并输出对应的单个动作。

坐标使用归一化坐标系：截图左上角为 (0,0)，右下角为 (1000,1000)，覆盖整个手机屏幕。

可用动作（只能从中选一个）：
- tap：点击应用图标、按钮、菜单项、列表项、开关、底部导航栏标签等可点击元素。填写目标中心的 x/y。
- type：在输入框中输入文字。填写输入框中心的 x/y 和 text，它会自动先点击聚焦、清空原有内容、再输入。
  只有当指令明确说明输入框已聚焦时，type 才可以只填 text、不填 x/y。
  ⚠️ 当前仅支持 ASCII 文本（英文 / 数字 / 符号）；中文等非 ASCII 暂不支持。
- press_enter：提交 / 确认 / 搜索 / 换行。输入文字后需要提交时使用，无需坐标。禁止用 tap 点发送或搜索按钮来代替回车提交。
- clear_text：清空当前聚焦输入框的内容，无需坐标。
- scroll：滚动列表或页面以显示更多内容。填写 direction（down 看下方、up 看上方、left 看左侧、right 看右侧）、amount（small/medium/large）；
  局部滚动容器需填写 x/y 作为滚动锚点，落在要滚动的区域内。
- drag：拖动滑块、进度条等需要拖拽的控件。填写起点 x/y。
- home：回到手机主屏幕（等价于系统主屏键），无需坐标。
- back：系统返回键，返回上一级 / 关闭当前弹窗或页面，无需坐标。
- recents：打开最近任务（多任务切换），无需坐标。
- stop：当指令含义是「停止」「无需操作」「目标已完成」，或目标元素确实不在当前截图中时使用，无需坐标。

Android 操作约定：
- 「返回上一级」优先用 back（系统返回键），或点击界面内的返回按钮（通常在左上角，形如 ← 箭头）/ 底部导航栏对应标签；只有明确需要退出当前应用回到桌面时才用 home。
- 屏幕顶部是状态栏 / 通知栏，底部常有导航栏（多个标签 tab）。应用列表 / 抽屉中的图标用 tap 打开。
- 输入文字后软键盘会从屏幕下半部分弹出并遮挡内容；输入完成后用 press_enter 提交并收起键盘。
- amount 表示滚动幅度：small（细微调整）、medium（普通翻看）、large（快速翻页）。普通整页滚动可不填 x/y；局部容器 / 分栏滚动必须填 x/y 落在该区域中心。
- 不要填写 to_x/to_y/duration_ms（drag 只需给出起点）。
- description 用中文简要说明操作目标，必须与指令中的目标元素名称一致。

## 目标元素不可见时的处理
如果仔细检查截图后发现指令要求操作的元素确实不在当前可见区域：
- 如果可以通过滚动显示出来，输出 scroll。
- 如果确实不在当前页面，将 not_found_reason 填写为具体原因（如「当前页面无该按钮，可见的有 A、B、C」），
  action 使用 stop，description 说明找不到目标。
"""

# Above this longest-edge size, downscale the screenshot before sending to the
# vision model (cost / latency). Android screencap is the device's own pixels, not
# a fixed 2x retina, so we do NOT halve unconditionally — only cap huge captures.
_MAX_EDGE = 1600


def _prepare_android_png(png_bytes: bytes) -> bytes:
    """Vision-only image prep: send the raw screenshot, downscaled only if huge."""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(png_bytes))
        longest = max(img.width, img.height)
        if longest <= _MAX_EDGE:
            return png_bytes
        scale = _MAX_EDGE / longest
        resized = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.LANCZOS,
        )
        buf = io.BytesIO()
        resized.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        # Never block a decision on image prep; send raw bytes.
        return png_bytes


class AndroidActionPolicy(BaseActionPolicy):
    """Vision-based android action policy. Uses the shared BaseActionPolicy.decide()
    template; the iphone picker hints are accepted by that signature and ignored."""

    name = "android_vision"
    SYSTEM_PROMPT = SYSTEM_PROMPT
    decision_schema = AndroidActionDecision

    def _prepare_png(self, png_bytes: bytes) -> bytes:
        return _prepare_android_png(png_bytes)
