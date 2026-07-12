"""Use DOM control identity without replacing ordinary rendered interaction."""

from __future__ import annotations

from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision


def _norm(value: object) -> str:
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def _control_aliases(control: dict) -> set[str]:
    label = str(control.get("label") or "").strip()
    group_field = str(control.get("group_field") or "").strip()
    values = {
        str(control.get("name") or "").strip(),
        str(control.get("id") or "").strip(),
        label,
    }
    if label and group_field:
        values.update((f"{group_field} {label}", f"{label} {group_field}"))
    elif group_field:
        values.add(group_field)
    return {_norm(value) for value in values if _norm(value)}


def matches_target_control(control: dict, target: str) -> bool:
    target_key = _norm(target)
    aliases = _control_aliases(control)
    if target_key in aliases:
        return True
    group_key = _norm(control.get("group_field"))
    compound = {alias for alias in aliases if group_key and group_key in alias}
    return bool(target_key and any(alias in target_key for alias in compound))


def _in_group(control: dict, target_group_id: str) -> bool:
    actual = str(control.get("group_id") or "")
    if target_group_id == "__form__":
        return not actual
    return actual == target_group_id


def rendered_target_evidence(
    controls: list[dict] | None,
    *,
    target_control: str,
    target_value: str,
    target_group_id: str,
    action_family: str,
) -> str:
    """Describe one exact rendered target to the vision action policy.

    This is evidence, not execution: the vision policy still selects the primitive and the DOM
    grounder may only correct a compatible action. The block prevents a same-value adjacent field
    from being mistaken for the declared target when labels are visually repetitive.
    """
    if action_family not in {"input", "select"}:
        return ""
    if not target_control or not target_group_id:
        return ""
    candidates = [
        control
        for control in controls or []
        if isinstance(control, dict)
        and _in_group(control, target_group_id)
        and matches_target_control(control, target_control)
    ]
    if len(candidates) != 1:
        return ""
    control = candidates[0]
    kind = str(control.get("kind") or "")
    current = str(
        control.get("selected_text")
        or control.get("value")
        or control.get("current")
        or ""
    )
    label = str(control.get("label") or "").strip()
    group_field = str(control.get("group_field") or "").strip()
    display_name = " ".join(part for part in (group_field, label) if part) or target_control
    rect = control.get("rect") or {}
    center = ""
    if all(isinstance(rect.get(key), (int, float)) for key in ("x", "y")):
        center = f"; center=({float(rect['x']):g},{float(rect['y']):g})"
    in_viewport = control.get("in_viewport")
    viewport = "" if in_viewport is None else f"; in_viewport={str(bool(in_viewport)).lower()}"
    return (
        "## 结构化目标证据（浏览器 DOM，字段身份与当前值优先于截图文字邻接）\n"
        f"- declared_target={target_control!r}; group={target_group_id!r}; "
        f"matched_control={display_name!r}; kind={kind!r}{center}{viewport}\n"
        f"- current_value={current!r}; requested_value={str(target_value)!r}\n"
        "只判断 matched_control 自身是否达到 requested_value；相邻字段出现相同文本不代表该目标完成。"
        "若该目标可见且 current_value 不同，执行指令要求的动作，不要返回 stop。"
    )


def resolve_native_control_action(
    controls: list[dict] | None,
    *,
    target_control: str,
    target_value: str,
    target_group_id: str,
    action_family: str,
    instruction: str = "",
) -> BrowserActionDecision | None:
    """Directly execute a target only when it is a browser-native select.

    Native select options are not reliably rendered into the page screenshot, so the visual
    policy cannot interact with them faithfully. Ordinary inputs, textareas, buttons, and scroll
    targets remain visual interactions and must go through the action policy.
    """
    if action_family != "select":
        return None
    if not target_control or not target_value or not target_group_id:
        return None

    candidates: list[dict] = []
    for control in controls or []:
        if not isinstance(control, dict):
            continue
        if target_group_id and not _in_group(control, target_group_id):
            continue
        if not matches_target_control(control, target_control):
            continue
        kind = str(control.get("kind") or "").lower()
        if kind != "native_select":
            continue
        candidates.append(control)
    if len(candidates) != 1:
        return None

    control = candidates[0]
    if control.get("in_viewport") is False:
        return None
    rect = control.get("rect") or {}
    if not all(isinstance(rect.get(key), (int, float)) for key in ("x", "y")):
        return None
    x = float(rect["x"])
    y = float(rect["y"])
    if not (0 <= x <= 1000 and 0 <= y <= 1000):
        return None
    label = str(control.get("label") or target_control).strip()
    group = str(control.get("group_field") or target_group_id).strip()
    action = BrowserAction(
        action_type="select_option",
        x=x,
        y=y,
        text=target_value,
        description=instruction or f"设置 {target_control} 为 {target_value}",
        snap={
            "method": "semantic_dom",
            "original": [x, y],
            "snapped": [x, y],
            "info": f"{group} {label}".strip(),
        },
    )
    return BrowserActionDecision(action=action)


def ground_rendered_action(
    decision: BrowserActionDecision,
    controls: list[dict] | None,
    *,
    target_control: str,
    target_value: str,
    target_group_id: str,
    action_family: str,
) -> BrowserActionDecision:
    """Correct a visual action's coordinate when one rendered input owns the target.

    The visual policy still chooses the primitive and value. DOM evidence may only ground an
    already compatible ``type`` action; it never synthesizes input, selection, or scrolling.
    """
    action = decision.action
    if action_family != "input" or action.action_type != "type":
        return decision
    if not target_control or not target_value or not target_group_id:
        return decision
    if str(action.text or "") != str(target_value):
        return decision

    candidates: list[dict] = []
    for control in controls or []:
        if not isinstance(control, dict):
            continue
        if not _in_group(control, target_group_id):
            continue
        if not matches_target_control(control, target_control):
            continue
        kind = str(control.get("kind") or "").lower()
        if not any(token in kind for token in ("input", "textarea")):
            continue
        if control.get("in_viewport") is False:
            continue
        candidates.append(control)
    if len(candidates) != 1:
        return decision

    control = candidates[0]
    rect = control.get("rect") or {}
    if not all(isinstance(rect.get(key), (int, float)) for key in ("x", "y")):
        return decision
    x = float(rect["x"])
    y = float(rect["y"])
    if not (0 <= x <= 1000 and 0 <= y <= 1000):
        return decision

    label = str(control.get("label") or target_control).strip()
    group = str(control.get("group_field") or target_group_id).strip()
    grounded = action.model_copy(update={
        "x": x,
        "y": y,
        "snap": {
            "method": "semantic_dom",
            "original": [action.x, action.y],
            "snapped": [x, y],
            "info": f"{group} {label}".strip(),
        },
    })
    return decision.model_copy(update={"action": grounded})
