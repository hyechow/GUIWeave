"""Browser action policy with vision interaction and native-control bridging.

Mirrors the iphone ``StructuredOutputPolicy`` LLM machinery — same config via
``gui_agent.core.config.resolve_llm_config('action_policy')``, same structured-output
call via ``llm.structured.invoke_structured`` into an ``ActionDecision``, same
``BaseActionPolicy`` base — but with a BROWSER system prompt: operate a web page
with a desktop pointer, output ONE action within the neutral action vocabulary
(tap / type / clear_text / press_enter / scroll / drag / navigate / back /
new_tab / select_tab / close_tab / select_option).

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
    matches_target_control,
    rendered_target_evidence,
    resolve_native_control_action,
    resolve_semantic_action,
    semantic_target_evidence,
)
from gui_agent.adapters.browser.target_binding import BrowserTargetBinder
from gui_agent.core.policies.base import BaseActionPolicy
from gui_agent.core.schemas import ActionEffectKind
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
class BrowserActionPolicy(BaseActionPolicy):
    """Vision policy for rendered interaction plus narrow native-control hooks."""

    name = "browser_vision"
    SYSTEM_PROMPT = SYSTEM_PROMPT
    decision_schema = BrowserActionDecision
    _target_binder = BrowserTargetBinder()

    def bind(self, step, observation, action_decision):
        return self._target_binder.bind(step, observation, action_decision)

    def resolve_action_effect(
        self,
        step,
        observation,
        action_decision,
        binding=None,
    ) -> ActionEffectKind:
        """Resolve the grounded target's effect from adapter structural evidence."""
        del binding
        intent = step.action_intent
        action = action_decision.action
        action_type = str(getattr(action, "action_type", "") or "").casefold()
        target_ref = str(getattr(intent, "target_ref", "") or "") if intent else ""
        primitive: ActionEffectKind | None = (
            "viewport"
            if action_type in {"scroll", "drag", "scroll_to_ref"}
            else "navigation"
            if action_type in {
                "navigate", "back", "new_tab", "select_tab", "close_tab"
            }
            else None
        )
        if primitive:
            return primitive
        if intent is None:
            return "unknown"

        def matches(item: object) -> bool:
            if not isinstance(item, dict):
                return False
            refs = {
                str(item.get(key) or "").strip()
                for key in ("ref", "id", "name")
            }
            if target_ref and target_ref in refs:
                return True
            candidate = (
                {**item, "label": item.get("key")}
                if not item.get("label") and item.get("key")
                else item
            )
            return matches_target_control(
                candidate,
                intent.target_control,
                allow_compound=intent.family in {"input", "select"},
            )

        candidates = [
            item
            for item in [
                *(getattr(observation, "semantic_tree", None) or []),
                *(getattr(observation, "form_controls", None) or []),
                *(getattr(observation, "form_control_state", None) or []),
            ]
            if matches(item)
        ]
        effects = {
            str(item.get("effect_kind") or "")
            for item in candidates
            if item.get("effect_kind")
        }
        if len(effects) == 1:
            return effects.pop()
        return "unknown"

    def _prepare_png(self, png_bytes: bytes) -> bytes:
        return _prepare_browser_png(png_bytes)

    def resolve_native_action(
        self,
        observation,
        *,
        target_control: str = "",
        target_value: str = "",
        target_ref: str = "",
        target_group_id: str = "",
        action_family: str = "",
        instruction: str = "",
    ):
        decision = resolve_semantic_action(
            getattr(observation, "semantic_tree", None),
            target_control=target_control,
            target_ref=target_ref,
            action_family=action_family,
            instruction=instruction,
        )
        if decision is None:
            controls = (
                getattr(observation, "form_control_state", None)
                or getattr(observation, "form_controls", None)
            ) if action_family == "iterate" else (
                getattr(observation, "form_controls", None)
                or getattr(observation, "form_control_state", None)
            )
            decision = resolve_native_control_action(
                controls,
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
        target_ref: str = "",
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
        target_ref: str = "",
        target_group_id: str = "",
        action_family: str = "",
    ) -> str:
        blocks = [
            rendered_target_evidence(
                getattr(observation, "form_controls", None),
                target_control=target_control,
                target_value=target_value,
                target_group_id=target_group_id,
                action_family=action_family,
            ),
            semantic_target_evidence(
                getattr(observation, "semantic_tree", None),
                target_control=target_control,
                target_ref=target_ref,
                action_family=action_family,
            ),
        ]
        return "\n\n".join(block for block in blocks if block)

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
        return decision
