"""Vision-only browser action policy: screenshot + instruction -> one Action.

Mirrors the iphone ``StructuredOutputPolicy`` LLM machinery — same config via
``gui_agent.core.config.resolve_llm_config('action_policy')``, same structured-output
call via ``llm.structured.invoke_structured`` into an ``ActionDecision``, same
``BaseActionPolicy`` base — but with a BROWSER system prompt: operate a web page
with a desktop pointer, output ONE action within the neutral action vocabulary
(tap / type / clear_text / press_enter / scroll / drag / navigate / back /
new_tab / select_tab / close_tab / select_option / stop).

VISION-ONLY: the screenshot is sent as-is (optionally downscaled if very large) —
it is NOT the iphone 2x retina image, so ``resize_to_logical_png`` is deliberately
NOT used. No iPhone picker, no home-screen / springboard concepts, no DOM.
"""

from __future__ import annotations

import io
import re

from dotenv import load_dotenv

from gui_agent.adapters.browser.actions import BrowserActionDecision
from gui_agent.core.policies.base import BaseActionPolicy
from gui_agent.prompts import load_prompt_text

load_dotenv()


SYSTEM_PROMPT = load_prompt_text("task.action_policy.browser")

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


# 上传控件特征词：instruction 命中说明指向一个会弹原生文件框的 dropzone / file-input，
# 而非普通按钮（普通「导入 X」按钮点开的是页面弹窗，不含这些词）。Vision LLM 偶尔对这类
# 控件误发 tap —— 原生框必然被 device 的 file-chooser 拦截取消（浪费一 turn），必须走 upload。
_UPLOAD_CONTROL_RE = re.compile(
    r"上传区域|点击上传|拖放|选择文件|文件选择器|drop\s*zone",
    re.IGNORECASE,
)
# instruction 里出现的本地文件绝对路径（supervisor 规则要求把真实路径原样带进 instruction）。
_FILE_PATH_RE = re.compile(r"(?:/[\w./+~-]+|~/[\w./+~-]+)")
_QUOTED_OPTION_RE = re.compile(r"[「『\"']([^「」『』\"']{1,40})[」』\"']")
_SELECT_OPTION_INTENT_RE = re.compile(
    r"选择|选中|设为|设置|筛选.*=|下拉.*选项|select\s+option",
    re.IGNORECASE,
)
_SELECT_CONTROL_CUE_RE = re.compile(
    r"下拉|选择框|筛选框|选项|option|select",
    re.IGNORECASE,
)
_OPTION_AFTER_EQUALS_RE = re.compile(
    r"(?:=|为|to)\s*([A-Za-z][\w ._/-]{0,39})",
    re.IGNORECASE,
)
_OPTION_AFTER_SELECT_RE = re.compile(
    r"(?:选择|选中|select)\s*([A-Za-z][\w ._/-]{0,39})",
    re.IGNORECASE,
)


def _option_text_from_instruction(instruction: str) -> str:
    text = (instruction or "").strip()
    if not text or not _SELECT_OPTION_INTENT_RE.search(text):
        return ""
    quoted = _QUOTED_OPTION_RE.findall(text)
    if quoted:
        return quoted[-1].strip()
    match = _OPTION_AFTER_EQUALS_RE.search(text)
    if match:
        return match.group(1).strip(" .,;，。；")
    match = _OPTION_AFTER_SELECT_RE.search(text)
    if match:
        value = match.group(1).strip(" .,;，。；")
        return "" if value.lower() in {"option", "options"} else value
    return ""


class BrowserActionPolicy(BaseActionPolicy):
    """Vision-based browser action policy."""

    name = "browser_vision"
    SYSTEM_PROMPT = SYSTEM_PROMPT
    decision_schema = BrowserActionDecision

    def _prepare_png(self, png_bytes: bytes) -> bytes:
        return _prepare_browser_png(png_bytes)

    def _postprocess(
        self, decision, instruction, *, direction=None, drag_column=None, drag_steps=None
    ):
        """Hard-guard: a tap aimed at a file-upload control is a no-op — the native
        chooser it opens is outside the page and gets cancelled by the device's
        file-chooser interceptor (see device._on_file_chooser). Rewrite such a tap to
        ``upload`` with the path the supervisor carried in the instruction, so the file
        is injected via the chooser instead. Fires ONLY when a real path is present —
        never fabricates one (per SYSTEM_PROMPT: 路径来自任务，不要自己编造)."""
        action = decision.action
        if (
            getattr(action, "action_type", None) == "tap"
            and _UPLOAD_CONTROL_RE.search(instruction or "")
        ):
            paths = _FILE_PATH_RE.findall(instruction or "")
            path = max(paths, key=len) if paths else None  # longest → real file path
            if path:
                action = action.model_copy(
                    update={"action_type": "upload", "file_path": path}
                )
                decision = decision.model_copy(update={"action": action})
        if (
            getattr(action, "action_type", None) == "tap"
            and _SELECT_CONTROL_CUE_RE.search(getattr(action, "description", "") or "")
        ):
            option_text = _option_text_from_instruction(
                f"{instruction or ''}\n{getattr(action, 'description', '') or ''}"
            )
            if option_text:
                action = action.model_copy(
                    update={"action_type": "select_option", "text": option_text}
                )
                decision = decision.model_copy(update={"action": action})
        return decision
