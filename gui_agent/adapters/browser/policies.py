"""Browser action policy with vision interaction and native-control bridging.

Mirrors the iphone ``StructuredOutputPolicy`` LLM machinery — same config via
``gui_agent.core.config.resolve_llm_config('action_policy')``, same structured-output
call via ``llm.structured.invoke_structured`` into an ``ActionDecision``, same
``BaseActionPolicy`` base — but with a BROWSER system prompt: operate a web page
with a desktop pointer, output ONE action within the neutral action vocabulary
(tap / type / clear_text / press_enter / scroll / drag / navigate / back /
new_tab / select_tab / close_tab / select_option / stop).

Rendered controls are handled from the screenshot. Browser-native controls whose
interactive surface is absent from the screenshot may use adapter DOM evidence.
"""

from __future__ import annotations

import io
import re

from dotenv import load_dotenv

from gui_agent.adapters.browser.actions import BrowserActionDecision
from gui_agent.adapters.browser.control_grounding import (
    ground_rendered_action,
    rendered_target_evidence,
    resolve_native_control_action,
)
from gui_agent.adapters.browser.executor import _range_field_label
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
# 区间过滤器（From/To）填值：planner 指令里点名了某个「字段 from/to」框。把这条字段命名指令带进
# type 动作的 description，executor 的聚焦点吸附即可按 DOM 身份（name/label + from/to 角色）
# 命中相邻同形两框中的正确那个，而不是靠 vision 几乎相同的像素瞎猜。
_FILL_VERB_RE = re.compile(r"填入|填写|输入|设为|设置为|设置成|set\b", re.IGNORECASE)


class BrowserActionPolicy(BaseActionPolicy):
    """Vision policy for rendered interaction plus narrow native-control hooks."""

    name = "browser_vision"
    SYSTEM_PROMPT = SYSTEM_PROMPT
    decision_schema = BrowserActionDecision

    def _prepare_png(self, png_bytes: bytes) -> bytes:
        return _prepare_browser_png(png_bytes)

    def resolve_native_action(
        self,
        observation,
        *,
        target_control: str = "",
        target_value: str = "",
        target_group_id: str = "",
        action_family: str = "",
        instruction: str = "",
    ):
        decision = resolve_native_control_action(
            getattr(observation, "form_controls", None),
            target_control=target_control,
            target_value=target_value,
            target_group_id=target_group_id,
            action_family=action_family,
            instruction=instruction,
        )
        if decision is not None:
            action = decision.action
            print(
                "  [NativeAction] "
                f"{target_group_id}:{target_control} -> {action.action_type}"
            )
        return decision

    def ground_rendered_action(
        self,
        decision,
        observation,
        *,
        target_control: str = "",
        target_value: str = "",
        target_group_id: str = "",
        action_family: str = "",
    ):
        return ground_rendered_action(
            decision,
            getattr(observation, "form_controls", None),
            target_control=target_control,
            target_value=target_value,
            target_group_id=target_group_id,
            action_family=action_family,
        )

    def action_evidence_context(
        self,
        observation,
        *,
        target_control: str = "",
        target_value: str = "",
        target_group_id: str = "",
        action_family: str = "",
    ) -> str:
        return rendered_target_evidence(
            getattr(observation, "form_controls", None),
            target_control=target_control,
            target_value=target_value,
            target_group_id=target_group_id,
            action_family=action_family,
        )

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
            getattr(action, "action_type", None) in {"tap", "click"}
            and (instruction or "").strip()
        ):
            # The supervisor instruction is the target contract. Vision models sometimes emit a
            # generic description such as "execute tap", which strips the label needed by the
            # executor's DOM text retargeting. Preserve the authoritative instruction while
            # leaving the model-selected primitive and coordinate untouched.
            action = action.model_copy(update={"description": instruction.strip()})
            decision = decision.model_copy(update={"action": action})
        if (
            getattr(action, "action_type", None) == "type"
            and _FILL_VERB_RE.search(instruction or "")
            and _range_field_label(instruction or "")
        ):
            # Carry the planner's field-naming instruction (e.g.「把 Quantity to 设为 3」) into the
            # type action's description so the executor's focus-tap can retarget to the right
            # From/To input by field identity. The typed value remains in action.text untouched.
            action = action.model_copy(update={"description": instruction})
            decision = decision.model_copy(update={"action": action})
        return decision
