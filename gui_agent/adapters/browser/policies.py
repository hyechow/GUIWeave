"""Vision-only browser action policy: screenshot + instruction -> one Action.

Mirrors the iphone ``StructuredOutputPolicy`` LLM machinery — same config via
``gui_agent.core.config.resolve_llm_config('action_policy')``, same structured-output
call via ``llm.structured.invoke_structured`` into an ``ActionDecision``, same
``BaseActionPolicy`` base — but with a BROWSER system prompt: operate a web page
with a desktop pointer, output ONE action within the neutral action vocabulary
(tap / type / clear_text / press_enter / scroll / drag / navigate / back / stop).

VISION-ONLY: the screenshot is sent as-is (optionally downscaled if very large) —
it is NOT the iphone 2x retina image, so ``resize_to_logical_png`` is deliberately
NOT used. No iPhone picker, no home-screen / springboard concepts, no DOM.
"""

from __future__ import annotations

import io

from dotenv import load_dotenv

from gui_agent.core.policies.base import BaseActionPolicy
from gui_agent.adapters.browser.actions import BrowserActionDecision

load_dotenv()


SYSTEM_PROMPT = """\
你是一个网页操作执行器，通过桌面鼠标指针和键盘操作浏览器中的网页。
用户会提供当前网页的截图和一个具体的操作指令。你只需要找到目标元素并输出对应的单个动作。

坐标使用归一化坐标系：截图左上角为 (0,0)，右下角为 (1000,1000)，覆盖整个浏览器视口。

可用动作（只能从中选一个）：
- tap：点击链接、按钮、菜单项、复选框、标签页等可点击元素。填写目标中心的 x/y。
- type：在输入框/文本域中填写文字。填写输入框中心的 x/y 和 text，它会自动先点击聚焦、清空原有内容、再输入。
  只要指令意图是「输入/填写/录入某段文字」（如「在标题栏输入X」「正文输入Y」），就必须直接输出 type，
  同时给出 x/y 和 text，一步到位。绝不要为了「先聚焦/先激活输入框」而单独输出一个 tap——type 本身就会
  点击聚焦；单独 tap 一个空输入框往往屏幕零变化、被判无效，白白浪费一步并触发重规划。
  只有当指令明确说明输入框已经聚焦时，type 才可以只填写 text、不填写 x/y。
- press_enter：提交表单/确认搜索/换行。输入文字后需要提交时使用，无需坐标。禁止用 tap 去点提交按钮来代替回车提交。
- clear_text：清空当前聚焦输入框的内容，无需坐标。
- scroll：滚动页面以显示更多内容。填写 direction（down 看下方、up 看上方、right 看右侧、left 看左侧）、amount（small/medium/large）；
  局部滚动容器需填写 x/y 作为滚动锚点，落在要滚动的区域内。
- drag：拖动滑块、调整控件、拖拽元素。填写起点 x/y。
- navigate：直接让浏览器跳转到某个网址。只要指令意图是「打开/进入/前往某网站」「访问/在地址栏输入某网址」
  「导航到 X」，且你知道目标网址（如 feishu.cn），就一律用 navigate、填 url（可省略 https://）、无需坐标。
  这类「去往某网址」的目标一律优先 navigate；不要把网址当普通文字 type 进页面里的搜索框（站内搜索不会跳到该网站）。
- back：浏览器历史后退（等同点浏览器后退按钮，回到上一个页面），无需坐标。
- stop：当指令含义是「停止」「无需操作」「目标已完成」，或目标元素确实不在当前截图中时使用，无需坐标。

约束：
- amount 表示滚动幅度：small（细调）、medium（普通翻看）、large（快速翻页）。
- 普通整页滚动可不填 x/y；局部滚动容器、分栏区域必须填写 x/y 落在该容器中心。
- 不要填写 to_x/to_y/duration_ms（拖动除外，drag 由你给出起点，终点由执行层处理或你按需提供）。
- description 用中文简要说明操作目标，必须与指令中的目标元素名称一致。

## 目标元素不可见时的处理
如果仔细检查截图后发现指令要求操作的元素确实不在当前可见区域：
- 如果可以通过滚动显示出来，输出 scroll。
- 如果确实不存在于当前页面，将 not_found_reason 填写为具体原因（如「当前页面无该按钮，可见的有 A、B、C」），
  action 使用 stop，description 说明找不到目标。
"""

# Above this longest-edge size, downscale the screenshot before sending to the
# vision model (cost / latency). Browser screenshots are NOT iphone 2x retina, so
# we do NOT halve unconditionally — only cap very large captures.
_MAX_EDGE = 1600


def _prepare_browser_png(png_bytes: bytes) -> bytes:
    """Vision-only image prep: send the raw screenshot, downscaled only if huge.

    Unlike the iphone path (fixed 2x retina -> halve), browser viewports vary, so
    we never assume a scale factor; we only cap the longest edge to ``_MAX_EDGE``.
    """
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


class BrowserActionPolicy(BaseActionPolicy):
    """Vision-based browser action policy. Uses the shared BaseActionPolicy.decide()
    template; the iphone picker hints (direction/drag_column/drag_steps) are accepted
    by that signature and ignored (browser has no picker)."""

    name = "browser_vision"
    SYSTEM_PROMPT = SYSTEM_PROMPT
    decision_schema = BrowserActionDecision

    def _prepare_png(self, png_bytes: bytes) -> bytes:
        return _prepare_browser_png(png_bytes)
