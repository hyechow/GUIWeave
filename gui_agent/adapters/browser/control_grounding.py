"""Use DOM control identity without replacing ordinary rendered interaction."""

from __future__ import annotations

import math
import re

from gui_agent.adapters.browser.actions import BrowserAction, BrowserActionDecision


def _norm(value: object) -> str:
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def _control_aliases(control: dict) -> set[str]:
    label = str(control.get("label") or "").strip()
    group_field = str(control.get("group_field") or "").strip()
    kind = str(control.get("kind") or "").strip().casefold()
    values = {
        str(control.get("name") or "").strip(),
        str(control.get("id") or "").strip(),
        label,
    }
    if kind in {"a", "button", "link", "section_toggle"}:
        values.add(str(control.get("value") or "").strip())
    if label and group_field:
        values.update((f"{group_field} {label}", f"{label} {group_field}"))
    elif group_field:
        values.add(group_field)
    return {_norm(value) for value in values if _norm(value)}


def matches_target_control(
    control: dict,
    target: str,
    *,
    allow_compound: bool = True,
) -> bool:
    target_key = _norm(target)
    aliases = _control_aliases(control)
    if target_key in aliases:
        return True
    if not allow_compound:
        return False
    group_key = _norm(control.get("group_field"))
    compound = {alias for alias in aliases if group_key and group_key in alias}
    return bool(target_key and any(alias in target_key for alias in compound))


def _in_group(control: dict, target_group_id: str) -> bool:
    actual = str(control.get("group_id") or "")
    if target_group_id == "__form__":
        return not actual
    return actual == target_group_id


def _matches_ref(control: dict, target_ref: str) -> bool:
    if not target_ref:
        return True
    return target_ref in {
        str(control.get(key) or "").strip()
        for key in ("ref", "id", "name")
    }


def rendered_target_evidence(
    controls: list[dict] | None,
    *,
    target_control: str,
    target_value: str,
    target_group_id: str,
    action_family: str,
    target_ref: str = "",
) -> str:
    """Describe one exact rendered target to the vision action policy.

    This is evidence, not execution: the vision policy still selects the primitive and the DOM
    grounder may only correct a compatible action. The block prevents a same-value adjacent field
    from being mistaken for the declared target when labels are visually repetitive.
    """
    if action_family not in {"input", "select"}:
        return ""
    if not target_control:
        return ""
    candidates = [
        control
        for control in controls or []
        if isinstance(control, dict)
        and (not target_group_id or _in_group(control, target_group_id))
        and _matches_ref(control, target_ref)
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
        f"- declared_target={target_control!r}; "
        f"group={(target_group_id or 'unique-on-frame')!r}; "
        f"matched_control={display_name!r}; kind={kind!r}{center}{viewport}\n"
        f"- current_value={current!r}; requested_value={str(target_value)!r}\n"
        "只判断 matched_control 自身是否达到 requested_value；相邻字段出现相同文本不代表该目标完成。"
        "若该目标可见且 current_value 不同，执行指令要求的动作，不要拒绝该动作。"
    )


def semantic_target_evidence(
    nodes: list[dict] | None,
    *,
    target_control: str,
    target_ref: str = "",
    action_family: str,
) -> str:
    """Expose one exact document-semantic action target without claiming visibility."""
    if action_family not in {"activate", "navigate"} or not target_control:
        return ""
    target = _norm(target_control)
    candidates = [
        node
        for node in nodes or []
        if isinstance(node, dict)
        and str(node.get("role") or "").lower()
        in {"button", "link", "menuitem", "menuitemcheckbox", "menuitemradio", "tab"}
        and _norm(node.get("key")) == target
        and (
            not target_ref
            or str(node.get("ref") or "").strip() == str(target_ref).strip()
        )
    ]
    if len(candidates) != 1:
        return ""
    node = candidates[0]
    return (
        "## 结构化动作目标证据（浏览器可访问性树；不代表当前视口可见）\n"
        f"- declared_target={target_control!r}; "
        f"matched_document_target={str(node.get('key') or '')!r}; "
        f"role={str(node.get('role') or '')!r}; "
        f"ref={str(node.get('ref') or '')!r}\n"
        "该信号只证明文档中存在这个具名入口，不证明它在截图视口内。"
        "只有截图中可见时才能点击；若不可见，应先滚动使其进入视口。"
        "不得用其他可见按钮、标题或列表行替代。"
    )


def resolve_semantic_action(
    nodes: list[dict] | None,
    *,
    target_control: str,
    target_ref: str,
    action_family: str,
    instruction: str = "",
) -> BrowserActionDecision | None:
    """Ground one unique semantic target, transporting it before activation if offscreen."""
    if action_family not in {"activate", "navigate", "iterate"}:
        return None
    if target_ref:
        candidates = [
            node
            for node in nodes or []
            if isinstance(node, dict)
            and str(node.get("ref") or "").strip() == str(target_ref).strip()
        ]
    else:
        target = _norm(target_control)
        candidates = [
            node
            for node in nodes or []
            if isinstance(node, dict)
            and target
            and _norm(node.get("key")) == target
        ]
    if len(candidates) != 1:
        return None
    node = candidates[0]
    resolved_ref = str(node.get("ref") or "").strip()
    if action_family == "iterate" or node.get("in_viewport") is False:
        try:
            backend_node_id = int(resolved_ref)
        except (TypeError, ValueError):
            return None
        return BrowserActionDecision(
            action=BrowserAction(
                action_type="scroll_to_ref",
                target_ref=backend_node_id,
                description=instruction or f"将 {target_control} 移入视口",
            )
        )
    link_url = str(node.get("url") or "").strip()
    is_document_link = bool(
        str(node.get("role") or "").casefold() == "link"
        and link_url
        and not link_url.casefold().startswith("javascript:")
        and not link_url.rstrip().endswith("#")
    )
    if action_family == "navigate" or (
        action_family == "activate" and is_document_link
    ):
        if is_document_link:
            return BrowserActionDecision(
                action=BrowserAction(
                    action_type="navigate",
                    url=link_url,
                    description=instruction or f"打开 {target_control}",
                )
            )
        return None
    point = node.get("point")
    if node.get("in_viewport") is not True or not isinstance(point, dict):
        return None
    if not all(isinstance(point.get(axis), (int, float)) for axis in ("x", "y")):
        return None
    return BrowserActionDecision(
        action=BrowserAction(
            action_type="tap",
            x=float(point["x"]),
            y=float(point["y"]),
            description=instruction or f"点击 {target_control}",
        )
    )


def resolve_native_control_action(
    controls: list[dict] | None,
    *,
    target_control: str,
    target_value: str,
    target_group_id: str,
    action_family: str,
    instruction: str = "",
    target_ref: str = "",
) -> BrowserActionDecision | None:
    """Directly execute native selection or transport an offscreen form control.

    Native select options are not reliably rendered into the page screenshot, so the visual
    policy cannot interact with them faithfully. Ordinary inputs, textareas, buttons, and scroll
    targets remain visual interactions and must go through the action policy.
    """
    if action_family not in {"select", "iterate"}:
        return None
    if not target_control or (action_family == "select" and not target_value):
        return None

    candidates: list[dict] = []
    for control in controls or []:
        if not isinstance(control, dict):
            continue
        if target_group_id and not _in_group(control, target_group_id):
            continue
        if not _matches_ref(control, target_ref):
            continue
        if not matches_target_control(control, target_control):
            continue
        kind = str(control.get("kind") or "").lower()
        if action_family == "select" and kind != "native_select":
            continue
        candidates.append(control)
    if len(candidates) != 1:
        return None

    control = candidates[0]
    if action_family == "iterate":
        position = str(control.get("viewport_pos") or "").strip().casefold()
        rect = control.get("rect") or {}
        if not position and isinstance(rect.get("y"), (int, float)) and rect["y"] < 0:
            position = "above"
        if position not in {"above", "below"}:
            return None
        direction = "up" if position == "above" else "down"
        return BrowserActionDecision(
            action=BrowserAction(
                action_type="scroll",
                direction=direction,
                amount="medium",
                description=instruction or f"将 {target_control} 移入视口",
            )
        )
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
    target_ref: str = "",
) -> BrowserActionDecision:
    """Correct a visual action's coordinate when one rendered input owns the target.

    The visual policy still chooses the primitive and value. DOM evidence may only ground an
    already compatible ``type`` action; it never synthesizes input, selection, or scrolling.
    """
    action = decision.action
    if action_family != "input" or action.action_type != "type":
        return decision
    if not target_control or not target_value:
        return decision
    if str(action.text or "") != str(target_value):
        return decision

    candidates: list[dict] = []
    for control in controls or []:
        if not isinstance(control, dict):
            continue
        if target_group_id and not _in_group(control, target_group_id):
            continue
        if not _matches_ref(control, target_ref):
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


def _compatible_with_action(control: dict, action_type: str) -> bool:
    kind = str(control.get("kind") or "").casefold()
    if action_type == "type":
        return any(token in kind for token in ("input", "textarea", "textbox", "editor"))
    if action_type == "select_option":
        return kind in {"native_select", "select", "selectmenu", "listbox", "combobox"}
    return action_type in {"tap", "click"}


def _matches_described_control_type(description: str, control: dict) -> bool:
    """Keep semantic snapping from changing the Worker's requested control family.

    A correct visual point may target a data-row link that is absent from the form
    control inventory.  In that case a nearby filter input is not a valid fallback,
    even if its column label also appears in the action description.  Explicit type
    words are treated as constraints; with no type word, geometry remains unchanged.
    """

    words = set(re.findall(r"[a-z0-9]+", (description or "").casefold()))
    kind = str(control.get("kind") or "").casefold()
    if "link" in words:
        return kind in {"a", "link"}
    if "button" in words:
        return kind in {"button", "input_button", "submit", "section_toggle"}
    if words.intersection({"checkbox", "radio"}):
        return any(token in kind for token in ("checkbox", "radio"))
    if words.intersection({"input", "textbox", "textarea", "field"}):
        return any(token in kind for token in ("input", "textbox", "textarea", "editor"))
    if words.intersection({"select", "dropdown", "combobox", "listbox"}):
        return kind in {"native_select", "select", "selectmenu", "listbox", "combobox"}
    return True


def _contains_visible_name(description: str, visible_name: object) -> bool:
    """Return whether prose names one visible label as a complete phrase."""
    name = str(visible_name or "").strip()
    normalized_name = _norm(name)
    if (
        not description
        or not name
        or len(normalized_name) < 2
        or (name.isascii() and len(normalized_name) < 3)
    ):
        return False
    if name.isascii():
        name_tokens = re.findall(r"[a-z0-9]+", name.casefold())
        description_tokens = re.findall(r"[a-z0-9]+", description.casefold())
        width = len(name_tokens)
        return bool(
            name_tokens
            and any(
                description_tokens[index:index + width] == name_tokens
                for index in range(len(description_tokens) - width + 1)
            )
        )
    return _norm(name) in _norm(description)


def _description_names_control(description: str, control: dict) -> bool:
    """Match only rendered names, never DOM ids, refs, or selectors."""
    names = {
        str(control.get("label") or "").strip(),
        str(control.get("group_field") or "").strip(),
    }
    kind = str(control.get("kind") or "").casefold()
    if kind in {"a", "button", "link", "section_toggle"}:
        names.add(str(control.get("value") or "").strip())
    if kind == "clickable_row":
        names.update(
            str(value or "").strip()
            for value in (control.get("row_values") or [])
        )
    return any(_contains_visible_name(description, name) for name in names if name)


def _explicit_target_position(description: str, control: dict) -> int | None:
    """Return the first ``visible-name + control-type`` target phrase position.

    Relative descriptions often name neighboring controls (for example, "the Filters button
    beside Default View dropdown").  The first explicit name/type phrase is the atomic target;
    later names are layout context.  This stronger signal may safely repair a large coordinate
    miss, while ordinary name mentions remain subject to bounded nearby grounding.
    """

    description_tokens = re.findall(r"[a-z0-9]+", (description or "").casefold())
    kind = str(control.get("kind") or "").casefold()
    allowed_types: set[str]
    if kind in {"a", "link"}:
        allowed_types = {"link", "option", "item"}
    elif kind in {"button", "input_button", "submit", "section_toggle"}:
        allowed_types = {"button", "option", "item", "toggle"}
    elif any(token in kind for token in ("checkbox", "radio")):
        allowed_types = {"checkbox", "radio", "option", "row", "control"}
    elif any(token in kind for token in ("input", "textbox", "textarea", "editor")):
        allowed_types = {"input", "field", "textbox", "textarea", "editor"}
    elif kind in {"native_select", "select", "selectmenu", "listbox", "combobox"}:
        allowed_types = {"select", "dropdown", "combobox", "listbox", "field"}
    elif kind == "clickable_row":
        allowed_types = {"row", "item", "record"}
    else:
        return None
    names = {
        str(control.get("label") or "").strip(),
        str(control.get("group_field") or "").strip(),
    }
    if kind in {"a", "button", "link", "section_toggle"}:
        names.add(str(control.get("value") or "").strip())
    if kind == "clickable_row":
        names.update(
            str(value or "").strip()
            for value in (control.get("row_values") or [])
        )
    positions: list[int] = []
    for name in names:
        name_tokens = re.findall(r"[a-z0-9]+", name.casefold())
        if not name_tokens:
            continue
        width = len(name_tokens)
        for index in range(len(description_tokens) - width):
            if description_tokens[index:index + width] != name_tokens:
                continue
            following = description_tokens[index + width:index + width + 3]
            # Natural descriptions commonly say "Password text input" or "Status native
            # select".  The optional adjective does not change the named control family.
            if any(token in allowed_types for token in following):
                positions.append(index)
                continue
            # Row instructions commonly put the control family before a predicate carrying the
            # visible identity: "the row where Attribute Code is 'size'".  Keep the wider
            # preceding window specific to rows; other control families accept only adjacent
            # wording so a layout-context mention cannot become a far-reaching retarget.
            preceding_width = 12 if kind == "clickable_row" else 3
            preceding = description_tokens[max(0, index - preceding_width):index]
            if any(token in allowed_types for token in preceding):
                positions.append(index)
    return min(positions) if positions else None


def ground_action_to_nearest_control(
    decision: BrowserActionDecision,
    controls: list[dict] | None,
    *,
    viewport_size: tuple[int, int] | None = None,
) -> BrowserActionDecision:
    """Snap an enhanced-mode visual point to one nearby compatible control.

    The Worker owns only an approximate screenshot coordinate. DOM identity is
    never part of its protocol. The adapter uses current rendered geometry as a
    bounded execution aid and fails open to the original point when the nearest
    target is absent, far away, or ambiguous.
    """
    action = decision.action
    action_type = str(action.action_type or "")
    if (
        action_type not in {"tap", "click", "type", "select_option"}
        or action.x is None
        or action.y is None
    ):
        return decision
    viewport_width, viewport_height = viewport_size or (1000, 1000)
    if viewport_width <= 0 or viewport_height <= 0:
        viewport_width, viewport_height = 1000, 1000

    nearby: list[tuple[int, float, float, float, dict, float, float]] = []
    candidates: list[tuple[int, float, float, float, dict, float, float]] = []
    explicit_semantic: list[tuple[int, dict, float, float]] = []
    for control in controls or []:
        if not isinstance(control, dict) or not _compatible_with_action(control, action_type):
            continue
        if action_type in {"tap", "click"} and not _matches_described_control_type(
            action.description,
            control,
        ):
            continue
        if control.get("in_viewport") is False:
            continue
        rect = control.get("rect") or {}
        if not all(isinstance(rect.get(axis), (int, float)) for axis in ("x", "y")):
            continue
        cx, cy = float(rect["x"]), float(rect["y"])
        if not (0 <= cx < 1000 and 0 <= cy < 1000):
            continue
        width_px = max(0.0, float(rect.get("w") or 0.0))
        height_px = max(0.0, float(rect.get("h") or 0.0))
        if width_px > viewport_width * 0.9 or height_px > viewport_height * 0.6:
            continue
        target_position = _explicit_target_position(action.description, control)
        if target_position is not None:
            explicit_semantic.append((target_position, control, cx, cy))
        half_width = width_px / viewport_width * 500.0
        half_height = height_px / viewport_height * 500.0
        dx = max(abs(float(action.x) - cx) - half_width, 0.0)
        dy = max(abs(float(action.y) - cy) - half_height, 0.0)
        axis_misses = int(dx > 0) + int(dy > 0)
        edge_distance = math.hypot(dx, dy)
        center_distance = math.hypot(float(action.x) - cx, float(action.y) - cy)
        # A text-entry point can land near the edge of a wide rendered field. Keep
        # the form allowance only slightly wider than taps; compatibility and
        # ambiguity checks still have to identify one unique nearby field.
        form_action = action_type in {"type", "select_option"}
        max_edge_distance = 50.0 if form_action else 35.0
        max_center_distance = 220.0 if form_action else 180.0
        if edge_distance > max_edge_distance or center_distance > max_center_distance:
            continue
        area = max(1.0, half_width * half_height * 4.0)
        candidate = (axis_misses, edge_distance, area, center_distance, control, cx, cy)
        nearby.append(candidate)
        if center_distance <= (220.0 if form_action else 100.0):
            candidates.append(candidate)

    semantic = [
        candidate
        for candidate in nearby
        if _description_names_control(action.description, candidate[4])
    ]
    geometric_best = None
    if candidates:
        candidates.sort(key=lambda item: item[:4])
        geometric_best = candidates[0]
        if len(candidates) > 1:
            second = candidates[1]
            competing_centers = math.hypot(
                geometric_best[5] - second[5], geometric_best[6] - second[6]
            ) > 4.0
            if (
                geometric_best[0] == second[0]
                and abs(geometric_best[1] - second[1]) <= 4.0
                and competing_centers
            ):
                geometric_best = None

    explicit_best = None
    if explicit_semantic:
        explicit_semantic.sort(key=lambda item: item[0])
        if len(explicit_semantic) == 1 or explicit_semantic[0][0] < explicit_semantic[1][0]:
            explicit_best = explicit_semantic[0]

    if explicit_best is not None and (
        geometric_best is None or explicit_best[1] is not geometric_best[4]
    ):
        best = (0, 0.0, 0.0, 0.0, explicit_best[1], explicit_best[2], explicit_best[3])
        method = "control_semantic_geometry"
    elif len(semantic) == 1 and (
        geometric_best is None or semantic[0][4] is not geometric_best[4]
    ):
        best = semantic[0]
        method = "control_semantic_geometry"
    elif geometric_best is not None:
        best = geometric_best
        method = "control_geometry"
    else:
        return decision
    control, x, y = best[4], best[5], best[6]
    if abs(float(action.x) - x) <= 1 and abs(float(action.y) - y) <= 1:
        return decision
    label = str(
        control.get("group_field")
        or control.get("label")
        or control.get("name")
        or control.get("kind")
        or "control"
    ).strip()
    grounded = action.model_copy(update={
        "x": x,
        "y": y,
        "snap": {
            "method": method,
            "original": [action.x, action.y],
            "snapped": [x, y],
            "info": label,
        },
    })
    return decision.model_copy(update={"action": grounded})
