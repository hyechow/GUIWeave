"""Report builder: generate HTML visualization for exploration/execution logs."""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


# Token cost: priced per MODULE using that module's model rate (config.yaml `pricing`).
# Different modules may run different models (e.g. action_policy=35b, output=flash),
# so cost is summed module-by-module rather than from one flat rate.
from gui_agent.core.config import model_price, pricing_currency

# _Timer module name → llm config key. checker/planner/replanner/loop_* all share the
# supervisor model; decompose and action_policy have their own keys.
_MODULE_CFG: dict[str, str] = {
    "checker": "supervisor", "planner": "supervisor", "replanner": "supervisor",
    "loop_check": "supervisor", "loop_scroll": "supervisor",
    "decompose": "supervisor.decompose", "action_policy": "action_policy",
}


def _sum_tokens(token_usage: dict) -> tuple[int, int]:
    """Sum per-module {input, output} into a (total_input, total_output) pair."""
    ti = sum(int(v.get("input", 0)) for v in (token_usage or {}).values())
    to = sum(int(v.get("output", 0)) for v in (token_usage or {}).values())
    return ti, to


# Set from context.json's run-level `models` map (config_key → model). Missing → "",
# which model_price() prices at the configured default — no active-config fallback.
_MODELS_MAP: dict[str, str] = {}


def _module_model(module: str) -> str:
    return _MODELS_MAP.get(_MODULE_CFG.get(module, module), "")


def _token_cost(token_usage: dict) -> float:
    """Cost (currency units) for one token_usage dict, summed per module by its model price."""
    total = 0.0
    for module, tu in (token_usage or {}).items():
        pin, pout = model_price(_module_model(module))
        total += int(tu.get("input", 0)) / 1_000_000 * pin + int(tu.get("output", 0)) / 1_000_000 * pout
    return total


def _fmt_tokens(n: int) -> str:
    return f"{n/1000:.1f}k" if n >= 1000 else str(n)


CONTEXT_WINDOW = 128_000  # model context budget to monitor against (peak single-call input)
_CTX_WARN = 0.75          # amber ≥ 75% of the window
_CTX_DANGER = 0.90        # red   ≥ 90%


def _ctx_color(used: int) -> str:
    """Color for a context-size figure by its share of CONTEXT_WINDOW."""
    frac = used / CONTEXT_WINDOW if CONTEXT_WINDOW else 0
    return "#dc2626" if frac >= _CTX_DANGER else "#f59e0b" if frac >= _CTX_WARN else "#64748b"


# ── Action types & colors ──────────────────────────────────────

ACTION_COLORS: dict[str, tuple[int, int, int]] = {
    "tap": (220, 50, 50),
    "home": (50, 120, 220),
    "swipe": (50, 180, 50),
    "text": (160, 50, 200),
    "back": (220, 160, 0),
    "none": (128, 128, 128),
}

DEFAULT_COLOR = ACTION_COLORS["tap"]

COLOR_NAVIGATED = (34, 197, 94)    # green
COLOR_NO_CHANGE = (156, 163, 175)  # gray

DOT_RADIUS = 14
FONT_SIZE = 16


def _font(size: int = FONT_SIZE):
    try:
        return ImageFont.truetype("/System/Library/Fonts/PingFang.ttc", size)
    except Exception:
        return ImageFont.load_default()


_REPORT_MAX_W = 640  # resize annotated images to this width before saving


def _save_report_img(src: "Image.Image | bytes", path: Path, quality: int = 75) -> None:
    """Save an image as JPEG to disk, resizing to _REPORT_MAX_W if wider."""
    if isinstance(src, bytes):
        img = Image.open(io.BytesIO(src)).convert("RGB")
    else:
        img = src.convert("RGB")
    w, h = img.size
    if w > _REPORT_MAX_W:
        img = img.resize((_REPORT_MAX_W, round(h * _REPORT_MAX_W / w)), Image.Resampling.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="JPEG", quality=quality, optimize=True)


def _load_img(path: Path) -> Image.Image:
    return Image.open(path).convert("RGBA")



def annotate_tap(
    img: Image.Image,
    points: list[tuple[float, float, int]],
) -> Image.Image:
    """Draw numbered tap points on image. points: (x_pct, y_pct, index)."""
    img = img.copy()
    draw = ImageDraw.Draw(img, "RGBA")
    w, h = img.size
    font = _font(FONT_SIZE)

    for x_pct, y_pct, idx in points:
        cx = int(x_pct / 1000 * w)
        cy = int(y_pct / 1000 * h)
        color = DEFAULT_COLOR
        draw.ellipse(
            [cx - DOT_RADIUS, cy - DOT_RADIUS, cx + DOT_RADIUS, cy + DOT_RADIUS],
            fill=(*color, 200),
            outline=(255, 255, 255, 255),
            width=2,
        )
        text = str(idx)
        bbox = font.getbbox(text)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((cx - tw // 2, cy - th // 2), text, fill=(255, 255, 255, 255), font=font)

    return img


_ACTION_COLORS_FULL: dict[str, tuple[int, int, int]] = {
    "tap": (220, 50, 50),
    "type": (160, 50, 200),
    "scroll": (50, 180, 50),
    "drag": (50, 150, 220),
    "home": (50, 120, 220),
    "press_enter": (220, 160, 0),
    "clear_text": (128, 128, 128),
}


def annotate_action(
    img: Image.Image,
    action_type: str,
    x: float | None,
    y: float | None,
    idx: int,
    direction: str | None = None,
    text: str | None = None,
    to_x: float | None = None,
    to_y: float | None = None,
    snap: dict | None = None,
) -> Image.Image:
    """Draw action annotation on image with type-specific visuals."""
    img = img.copy()
    draw = ImageDraw.Draw(img, "RGBA")
    w, h = img.size
    font = _font(FONT_SIZE)
    small_font = _font(12)
    color = _ACTION_COLORS_FULL.get(action_type, DEFAULT_COLOR)

    if x is None or y is None:
        return img
    # When snap exists, draw main marker at snapped position (where action actually executed)
    if snap and snap.get("snapped"):
        cx = int(snap["snapped"][0] / 1000 * w)
        cy = int(snap["snapped"][1] / 1000 * h)
    else:
        cx = int(x / 1000 * w)
        cy = int(y / 1000 * h)

    if action_type in ("scroll", "drag"):
        # Draw start circle + arrow to end
        draw.ellipse(
            [cx - DOT_RADIUS, cy - DOT_RADIUS, cx + DOT_RADIUS, cy + DOT_RADIUS],
            fill=(*color, 200),
            outline=(255, 255, 255, 255),
            width=2,
        )
        # Direction arrow or end point
        if to_x is not None and to_y is not None:
            ex = int(to_x / 1000 * w)
            ey = int(to_y / 1000 * h)
            draw.line([(cx, cy), (ex, ey)], fill=(*color, 220), width=3)
            draw.ellipse(
                [ex - 8, ey - 8, ex + 8, ey + 8],
                fill=(*color, 180),
                outline=(255, 255, 255, 255),
                width=2,
            )
        elif direction:
            arrow_len = 60
            dx, dy = 0, 0
            if direction == "up": dy = -arrow_len
            elif direction == "down": dy = arrow_len
            elif direction == "left": dx = -arrow_len
            elif direction == "right": dx = arrow_len
            ex, ey = cx + dx, cy + dy
            draw.line([(cx, cy), (ex, ey)], fill=(*color, 220), width=3)
            # Arrowhead
            import math
            angle = math.atan2(dy, dx)
            a1 = angle + 2.5
            a2 = angle - 2.5
            for a in (a1, a2):
                ax = ex - int(12 * math.cos(a))
                ay = ey - int(12 * math.sin(a))
                draw.line([(ex, ey), (ax, ay)], fill=(*color, 220), width=2)
        # Label
        label = str(idx)
        bbox = font.getbbox(label)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((cx - tw // 2, cy - th // 2), label, fill=(255, 255, 255, 255), font=font)

    elif action_type == "type":
        # Circle + text bubble
        draw.ellipse(
            [cx - DOT_RADIUS, cy - DOT_RADIUS, cx + DOT_RADIUS, cy + DOT_RADIUS],
            fill=(*color, 200),
            outline=(255, 255, 255, 255),
            width=2,
        )
        label = str(idx)
        bbox = font.getbbox(label)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((cx - tw // 2, cy - th // 2), label, fill=(255, 255, 255, 255), font=font)
        # Text bubble above
        if text:
            display = text[:20] + ("…" if len(text) > 20 else "")
            tbbox = small_font.getbbox(display)
            btw, bth = tbbox[2] - tbbox[0], tbbox[3] - tbbox[1]
            pad = 4
            bx = cx - btw // 2 - pad
            by = cy - DOT_RADIUS - bth - 14
            draw.rounded_rectangle(
                [bx, by, bx + btw + pad * 2, by + bth + pad * 2],
                radius=4, fill=(255, 255, 255, 230), outline=(*color, 200), width=1,
            )
            draw.text((bx + pad, by + pad), display, fill=(*color, 255), font=small_font)

    else:
        # Default: circle with index (tap, home, press_enter, etc.)
        draw.ellipse(
            [cx - DOT_RADIUS, cy - DOT_RADIUS, cx + DOT_RADIUS, cy + DOT_RADIUS],
            fill=(*color, 200),
            outline=(255, 255, 255, 255),
            width=2,
        )
        label = str(idx)
        if action_type == "press_enter":
            label = "↵"
        elif action_type == "home":
            label = "⌂"
        bbox = font.getbbox(label)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((cx - tw // 2, cy - th // 2), label, fill=(255, 255, 255, 255), font=font)

    # Draw snap visualization: original (LLM) vs snapped (YOLO/OCR/DOM)
    if snap and snap.get("original"):
        method = snap.get("method", "?").lower()
        # orange=YOLO, cyan=DOM(browser), green=OCR
        snap_color = (
            (34, 211, 238, 220) if method == "dom"
            else (255, 165, 0, 220) if method == "yolo"
            else (0, 200, 100, 220)
        )
        ox = int(snap["original"][0] / 1000 * w)
        oy = int(snap["original"][1] / 1000 * h)
        # Solid circle at original (LLM-predicted) position
        draw.ellipse(
            [ox - DOT_RADIUS, oy - DOT_RADIUS, ox + DOT_RADIUS, oy + DOT_RADIUS],
            outline=snap_color, width=3,
        )
        # Line connecting original to snapped
        draw.line([(ox, oy), (cx, cy)], fill=snap_color, width=2)

    return img


def annotate_recon_taps(
    img: Image.Image,
    points: list[tuple[float, float, int, bool]],
) -> Image.Image:
    """Draw recon tap points: green=navigated, gray=no change. points: (x_pct, y_pct, index, navigated)."""
    img = img.copy()
    draw = ImageDraw.Draw(img, "RGBA")
    w, h = img.size
    font = _font(FONT_SIZE)

    for x_pct, y_pct, idx, navigated in points:
        cx = int(x_pct / 1000 * w)
        cy = int(y_pct / 1000 * h)
        color = COLOR_NAVIGATED if navigated else COLOR_NO_CHANGE
        draw.ellipse(
            [cx - DOT_RADIUS, cy - DOT_RADIUS, cx + DOT_RADIUS, cy + DOT_RADIUS],
            fill=(*color, 210),
            outline=(255, 255, 255, 255),
            width=2,
        )
        text = str(idx)
        bbox = font.getbbox(text)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((cx - tw // 2, cy - th // 2), text, fill=(255, 255, 255, 255), font=font)

    return img


LOGICAL_W = 318  # iPhone Mirroring logical pixel width (matches executor.WIN_W)

STRATEGY_COLORS: dict[str, tuple[int, int, int]] = {
    "fixed": (99, 102, 241),   # indigo
    "YOLO":    (234, 179,  8),   # amber
    "LLM":     (59, 130, 246),   # blue
    "LLM+YOLO":(139,  92, 246),  # violet
}


def annotate_back_attempts_img(
    img: Image.Image,
    attempts: list[dict],
) -> Image.Image:
    """Draw numbered back-attempt circles: green=success, red=fail, by strategy color."""
    img = img.copy()
    draw = ImageDraw.Draw(img, "RGBA")
    w, _ = img.size
    scale = w / LOGICAL_W
    font = _font(13)

    for i, a in enumerate(attempts, 1):
        coords = a.get("coords", [])
        if len(coords) < 2:
            continue
        cx = int(coords[0] * scale)
        cy = int(coords[1] * scale)
        success = a.get("success", False)
        strategy = a.get("strategy", "")
        color = (34, 197, 94) if success else STRATEGY_COLORS.get(strategy, (239, 68, 68))
        draw.ellipse(
            [cx - DOT_RADIUS, cy - DOT_RADIUS, cx + DOT_RADIUS, cy + DOT_RADIUS],
            fill=(*color, 220),
            outline=(255, 255, 255, 255),
            width=2,
        )
        text = str(i)
        bbox = font.getbbox(text)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((cx - tw // 2, cy - th // 2), text, fill=(255, 255, 255, 255), font=font)

    return img


def img_to_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


# ── JSON renderer ──────────────────────────────────────────

def _json_val_html(v) -> str:
    if v is None:
        return '<span class="jv-null">null</span>'
    if isinstance(v, bool):
        return f'<span class="jv-bool">{"true" if v else "false"}</span>'
    if isinstance(v, (int, float)):
        return f'<span class="jv-num">{v}</span>'
    if isinstance(v, str):
        safe = v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f'<span class="jv-str">"{safe}"</span>'
    return str(v)


_OVERLAY_FORMS = frozenset("CDEFG")
_FORM_LABELS = {"C": "弹窗", "D": "底部面板", "E": "侧边抽屉", "F": "菜单", "G": "键盘页"}


def _form_from_fingerprint(fingerprint: str) -> str | None:
    """Extract form letter from fingerprint text '...页面形态：X'."""
    import re as _re
    m = _re.search(r'页面形态[：:]\s*([A-G])', fingerprint or "")
    return m.group(1) if m else None


def _render_identity_json(identity: dict) -> str:
    items = list(identity.items())
    rows = ""
    for i, (k, v) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        rows += (
            f'<div class="jrow">'
            f'<span class="jk">"{k}"</span>'
            f'<span class="jpunct">: </span>'
            f'{_json_val_html(v)}'
            f'<span class="jpunct">{comma}</span>'
            f'</div>'
        )
    return f'<div class="jobj"><span class="jpunct">{{</span>{rows}<span class="jpunct">}}</span></div>'


# ── Runner data classes ────────────────────────────────────────

@dataclass
class ReportStep:
    label: str
    action_type: str  # tap, home, swipe, text, none, scroll, drag
    x: float | None   # 0-1000 normalized
    y: float | None
    description: str
    annotated_before_url: str  # path to annotated screenshot
    raw_screenshot_url: str = ""  # path to raw screenshot (no annotations)
    after_url: str | None = None      # screenshot after action
    status: str = ""  # ✓ ✗ ↩ ""
    timestamp: str = ""  # ISO timestamp
    milestone_id: str = ""
    milestone_kind: str = ""
    instruction: str = ""
    summary: str = ""
    timings: dict[str, float] = field(default_factory=dict)
    token_usage: dict[str, dict[str, int]] = field(default_factory=dict)  # per-module {input, output}
    llm_calls: int = 0
    action_direction: str | None = None
    action_text: str | None = None
    action_to_x: float | None = None
    action_to_y: float | None = None
    snap: dict | None = None
    sections_loaded: list[str] = field(default_factory=list)    # progressive knowledge injected into the planner this turn
    relevant_sections: list[str] = field(default_factory=list)  # sections the checker flagged relevant (requested)


@dataclass
class ReportPage:
    title: str
    steps: list[ReportStep] = field(default_factory=list)
    milestone_id: str = ""
    milestone_kind: str = ""
    milestone_name: str = ""
    milestone_description: str = ""
    success_condition: str = ""
    verify_url: str = ""  # verification screenshot (first turn of next milestone)
    verify_checker: dict = field(default_factory=dict)  # checker result from the last turn (done/in_progress)


@dataclass
class ReportData:
    title: str
    pages: list[ReportPage] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    milestones: list[dict] = field(default_factory=list)
    decompose_summary: str = ""  # First-turn supervisor summary with decomposition info
    models: dict[str, str] = field(default_factory=dict)  # config_key → model used this run
    raw_input: str = ""  # original human input (title); empty for old logs
    goal: str = ""       # resolved goal that drove the run
    router: dict = field(default_factory=dict)  # RouterResult dict; empty for bin/runner path
    output: str = ""     # final reply / 最终输出 of the run
    platform: str = ""   # run platform (iphone/browser); empty for old logs
    wall_clock_s: float = 0.0    # true end-to-end runner elapsed (context.wall_clock_s); 0 for old logs
    settle_s_total: float = 0.0  # Σ per-turn settle waits (post-action screen-settle)
    knowledge: dict = field(default_factory=dict)  # injected app-knowledge summary {app_name, nav_chars, elements_chars, section_count}
    knowledge_sections: list[dict] = field(default_factory=list)  # sections injected ≥1 turn: {stem, title, body} (body read from knowledge dir for the click-to-view modal)


# ── Recon data classes ─────────────────────────────────────────

@dataclass
class ReconTap:
    index: int
    label: str
    x: float
    y: float
    navigated: bool
    after_url: str | None  # screenshot after tap
    back_seq: list[dict] = field(default_factory=list)  # {"src": data_url, "subtitle": str, "success": bool}
    identity: dict = field(default_factory=dict)  # {"is_new", "phase", "visual_sim", "text_sim", "library_size", "matched_name"}


@dataclass
class ReconFlow:
    target_page: str
    target_description: str
    flow_description: str


@dataclass
class ReconPageInfo:
    name: str            # directory name
    title: str           # from recon_result.json
    page_type: str       # list, chat, detail, etc.
    description: str
    elements_count: int
    signature: str
    annotated_url: str   # initial.png with all taps annotated
    parent: str = ""          # parent page name (for leaf/overlay pages)
    taps: list[ReconTap] = field(default_factory=list)
    flows: list[ReconFlow] = field(default_factory=list)
    knowledge: str = ""
    error: dict | None = None  # structured error if probe was aborted
    error_annotated_url: str = ""  # failed-tap screenshot annotated with back-attempt coords


@dataclass
class AppReconData:
    app_name: str
    pages: list[ReconPageInfo] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    trace: list[dict] | None = None  # from trace.json if available
    dup_warnings: list[dict] = field(default_factory=list)


@dataclass
class NavNode:
    name: str
    page: ReconPageInfo | None  # None = unexplored (not yet visited by BFS)
    via_tap: str | None = None  # label of the tap that led to this page
    children: list["NavNode"] = field(default_factory=list)


def _build_nav_tree(pages: list[ReconPageInfo], trace: list[dict] | None = None) -> list[NavNode]:
    """Build navigation tree. Uses trace if available, otherwise infers from flows."""
    node_map: dict[str, NavNode] = {p.name: NavNode(name=p.name, page=p) for p in pages}
    has_parent: set[str] = set()

    if trace:
        # Identify root pages (parent is None/missing) upfront so re-visits
        # as children of other pages don't prevent them from being roots.
        root_pages: set[str] = set()
        for entry in trace:
            if not entry.get("parent"):
                root_pages.add(entry.get("page", ""))

        # Accurate: use recorded BFS parent-child relationships
        for entry in trace:
            page_name = entry.get("page", "")
            parent_name = entry.get("parent")
            via_tap = entry.get("via_tap")
            if page_name not in node_map or not parent_name or parent_name not in node_map:
                continue
            # Never assign a parent to a root page — it stays at the top of the tree.
            if page_name in root_pages:
                continue
            child = node_map[page_name]
            parent = node_map[parent_name]
            # Only assign one parent per node to keep a proper tree (no cycles).
            if page_name not in has_parent and child not in parent.children:
                child.via_tap = via_tap
                parent.children.append(child)
                has_parent.add(page_name)
    # Fallback: leaf/overlay pages not in trace — attach via page.parent
    for p in pages:
        if p.name in has_parent or not p.parent:
            continue
        if p.parent in node_map and p.name in node_map:
            node_map[p.parent].children.append(node_map[p.name])
            has_parent.add(p.name)

    return [node_map[p.name] for p in pages if p.name not in has_parent]


def _render_tree_html(nodes: list[NavNode], _visited: frozenset[str] = frozenset()) -> str:
    if not nodes:
        return ""
    items = ""
    for node in nodes:
        if node.page is not None:
            if node.page.name in _visited:
                continue  # cycle guard
            is_leaf = node.page.page_type == "leaf"
            type_label = PAGE_TYPE_LABELS.get(node.page.page_type, node.page.page_type)
            slug = _slug(node.page.name)
            badge = f'<span class="tree-chip">{type_label}</span>' if type_label and not is_leaf else ''
            child_visited = _visited | {node.page.name}
            sub = f'<ul>{_render_tree_html(node.children, child_visited)}</ul>' if node.children else ''
            link_cls = "tree-link tree-link-error" if node.page.error else "tree-link tree-link-leaf" if is_leaf else "tree-link"
            error_dot = '<span class="tree-error-dot">⚠</span>' if node.page.error else ''
            items += (
                f'<li class="tree-node">'
                f'<a class="{link_cls}" href="#{slug}">{error_dot}{node.page.title}{badge}</a>'
                f'{sub}</li>'
            )
        else:
            items += (
                f'<li class="tree-node">'
                f'<span class="tree-leaf">· {node.name}</span>'
                f'</li>'
            )
    return items


def _dfs_order(nodes: list[NavNode], depth: int = 0) -> list[tuple[NavNode, int]]:
    """DFS traversal returning (node, depth) pairs in pre-order."""
    result = []
    for node in nodes:
        result.append((node, depth))
        if node.children:
            result.extend(_dfs_order(node.children, depth + 1))
    return result


# ── Recon builder ─────────────────────────────────────────────

def _normalize_error(raw: str | dict | None) -> dict | None:
    """Normalize error to {message, failed_tap?, failed_element?, back_attempts?} or None."""
    if not raw:
        return None
    if isinstance(raw, str):
        return {"message": raw}
    return raw  # already a dict from ProbeAbortedError


def _render_error_html(error: dict | None, annotated_url: str = "") -> str:
    if not error:
        return ""
    msg = error.get("message", "探测中断")
    failed_tap = error.get("failed_tap")
    failed_el = error.get("failed_element", "")
    attempts: list[dict] = error.get("back_attempts", [])

    meta = ""
    if failed_tap and failed_tap > 0 and failed_el:
        meta = f'<div class="error-meta">第 {failed_tap} 个元素「{failed_el}」点击后无法返回</div>'

    timeline = ""
    if attempts:
        steps = ""
        for i, a in enumerate(attempts, 1):
            strategy = a.get("strategy", "?")
            coords = a.get("coords", [])
            coord_str = f"({coords[0]},{coords[1]})" if coords else ""
            result_text = a.get("result", "")
            score = a.get("score")
            score_str = f"{score:.3f}" if score is not None else ""
            score_html = f'<span class="back-score">{score_str}</span>' if score_str else ""
            success = a.get("success", False)
            if strategy == "retry":
                steps += (
                    f'<div class="back-step back-step-retry">'
                    f'<span class="back-num">↻</span>'
                    f'<span class="back-strategy">retry</span>'
                    f'<span class="back-coords"></span>'
                    f'<span class="back-result">{result_text}</span>'
                    f'{score_html}'
                    f'</div>'
                )
                continue
            step_cls = "back-step back-step-ok" if success else "back-step"
            steps += (
                f'<div class="{step_cls}">'
                f'<span class="back-num">{i}</span>'
                f'<span class="back-strategy">{strategy}</span>'
                f'<span class="back-coords">{coord_str}</span>'
                f'<span class="back-result">{result_text}</span>'
                f'{score_html}'
                f'</div>'
            )
        timeline = f'<div class="back-timeline">{steps}</div>'

    img_html = ""
    if annotated_url:
        img_html = (
            f'<div class="error-screenshot">'
            f'<img src="{annotated_url}" title="点击放大" onclick="openModal(this.src)">'
            f'<div class="error-screenshot-caption">返回尝试位置（数字=尝试顺序）</div>'
            f'</div>'
        )

    detail_html = (
        f'{meta}'
        f'<div class="error-body">'
        f'{img_html}'
        f'{timeline}'
        f'</div>'
    ) if (meta or img_html or timeline) else ""

    if detail_html:
        return (
            f'<details class="error-banner">'
            f'<summary class="error-title">⚠ 探测中断：{msg}</summary>'
            f'{detail_html}'
            f'</details>'
        )
    return f'<div class="error-banner"><div class="error-title">⚠ 探测中断：{msg}</div></div>'


class ReconReportBuilder:
    def build(self, log_dir: Path) -> AppReconData:
        # log_dir is either the app dir (logs/recon/微信) or a single page dir
        if (log_dir / "recon_result.json").exists():
            # Single page directory
            page_dirs = [log_dir]
        else:
            # App directory — iterate its page subdirectories
            page_dirs = sorted(p for p in log_dir.iterdir() if p.is_dir())

        app_name = log_dir.name
        pages: list[ReconPageInfo] = []
        total_taps = 0
        total_navigated = 0

        # Load trace.json early (needed for error lookup during page iteration)
        trace_data: list[dict] | None = None
        trace_path = log_dir / "trace.json"
        if trace_path.exists():
            _raw = json.loads(trace_path.read_text(encoding="utf-8"))
            # Support both old format (list) and new format (dict with pages/transitions)
            trace_data = _raw if isinstance(_raw, list) else _raw.get("pages", [])

        # Index trace errors by page name for quick lookup
        trace_errors: dict[str, str] = {}
        if trace_data:
            for entry in trace_data:
                if entry.get("error"):
                    trace_errors[entry["page"]] = entry["error"]

        for pd in page_dirs:
            initial_path = pd / "initial.png"
            if not initial_path.exists():
                continue  # nothing to show at all

            result_path = pd / "recon_result.json"
            result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}

            # Page identity: prefer page_meta.json (exported), fall back to initial_result.json
            page_type = ""
            page_title = pd.name
            description = result.get("description", "")

            meta_path = pd / "page_meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                page_title = meta.get("page_title", pd.name)
                page_type = meta.get("page_type", "")
                description = meta.get("description", description)
            else:
                init_result_path = pd / "initial_result.json"
                if init_result_path.exists():
                    init_data = json.loads(init_result_path.read_text(encoding="utf-8"))
                    description = description or init_data.get("page", {}).get("description", "")

            elements_count = result.get("elements_count", 0)
            signature = ""

            # Annotate screenshot with tap points, save to disk (not embedded in HTML).
            initial_img = _load_img(initial_path)
            raw_taps = result.get("taps", [])
            tap_points = [(t["x"], t["y"], t["index"], t.get("navigated", False)) for t in raw_taps]
            annotated_img = annotate_recon_taps(initial_img, tap_points)
            ann_path = pd / "initial_tap_ann.jpg"
            _save_report_img(annotated_img, ann_path)
            annotated_url = str(ann_path.relative_to(log_dir))

            # Build ReconTap list
            taps: list[ReconTap] = []
            for tap in raw_taps:
                total_taps += 1
                navigated = tap.get("navigated", False)
                if navigated:
                    total_navigated += 1
                tap_path = Path(tap.get("screenshot", ""))
                # Raw tap screenshot already on disk — just use a relative path.
                after_url = str(tap_path.relative_to(log_dir)) if tap_path.is_file() else None

                # Build full back-navigation sequence for navigated taps
                back_seq: list[dict] = []
                if navigated and after_url:
                    tap_idx = tap["index"]
                    tap_dir = pd / "tap"
                    # Step 1: initial page with single tap marker (annotated, save to disk)
                    single_point = [(tap["x"], tap["y"], tap_idx, True)]
                    before_img = annotate_recon_taps(initial_img.copy(), single_point)
                    seq0_path = tap_dir / f"tap_{tap_idx:02d}_seq0.jpg"
                    _save_report_img(before_img, seq0_path)
                    back_seq.append({"src": str(seq0_path.relative_to(log_dir)), "subtitle": "", "success": None})
                    # Step 2: navigated page, annotate with first back-attempt coords if available
                    back_attempts_raw = tap.get("back_attempts", [])
                    if back_attempts_raw and tap_path.is_file():
                        after_img = _load_img(tap_path)
                        after_ann = annotate_back_attempts_img(after_img, [back_attempts_raw[0]])
                        seq1_path = tap_dir / f"tap_{tap_idx:02d}_seq1.jpg"
                        _save_report_img(after_ann, seq1_path)
                        step2_src = str(seq1_path.relative_to(log_dir))
                        first_strategy = back_attempts_raw[0].get('strategy', '')
                        step2_sub = "重新进入子页面" if first_strategy == "forward" else f"回退策略: {first_strategy}"
                    else:
                        step2_src = after_url
                        step2_sub = "已导航"
                    back_seq.append({"src": step2_src, "subtitle": step2_sub, "success": None})
                    # Steps 3+: each back attempt (with screenshot, or retry markers)
                    # Forward steps without a screenshot are collapsed into one terminal
                    # step at the end (they all land on the initial page anyway).
                    pending_forward: list[dict] = []
                    for attempt in tap.get("back_attempts", []):
                        strategy = attempt.get("strategy", "")
                        result_txt = attempt.get("result", "")
                        score = attempt.get("score")
                        score_str = f" {score:.3f}" if score is not None else ""
                        success = attempt.get("success", False)
                        if strategy == "retry":
                            back_seq.append({
                                "src": back_seq[-1]["src"] if back_seq else "",
                                "subtitle": f"↻ {result_txt}{score_str}",
                                "success": None,
                                "is_retry": True,
                            })
                            continue
                        shot = Path(attempt.get("screenshot", ""))
                        if not shot.is_file():
                            if strategy == "forward" and success:
                                pending_forward.append(attempt)
                            continue
                        if strategy == "forward":
                            subtitle = f"{result_txt}（已恢复）"
                        else:
                            subtitle = f"{result_txt}{score_str}"
                        back_seq.append({
                            "src": str(shot.relative_to(log_dir)),
                            "subtitle": subtitle,
                            "success": success,
                        })

                    # Collapse pending no-screenshot forward steps into one terminal step.
                    # Extract the full path: "L0→L1", "L1→L2" → "L0→L1→L2"
                    if pending_forward:
                        steps = [a.get("result", "") for a in pending_forward]
                        levels = [steps[0].split("→")[0]] + [s.split("→")[-1] for s in steps]
                        path_str = "→".join(levels)
                        back_seq.append({
                            "src": str(initial_path.relative_to(log_dir)),
                            "subtitle": f"{path_str}（已恢复）",
                            "success": True,
                        })

                taps.append(ReconTap(
                    index=tap["index"],
                    label=tap.get("label", ""),
                    x=tap.get("x", 0),
                    y=tap.get("y", 0),
                    navigated=navigated,
                    after_url=after_url,
                    back_seq=back_seq,
                    identity=tap.get("identity", {}),
                ))

            # Load knowledge
            knowledge = ""
            knowledge_path = pd / "knowledge.md"
            if knowledge_path.exists():
                knowledge = knowledge_path.read_text(encoding="utf-8")

            page_error = _normalize_error(trace_errors.get(
                pd.name,
                None if result_path.exists() else "探测中断，结果未保存",
            ))

            # Annotate failed-tap screenshot with back-attempt coords
            error_annotated_url = ""
            if page_error:
                back_attempts = page_error.get("back_attempts", [])
                if back_attempts:
                    failed_tap_idx = page_error.get("failed_tap", -1)
                    shot_bytes: bytes | None = None
                    if failed_tap_idx and failed_tap_idx > 0:
                        for tap in raw_taps:
                            if tap.get("index") == failed_tap_idx:
                                tp = Path(tap.get("screenshot", ""))
                                if tp.is_file():
                                    shot_bytes = tp.read_bytes()
                                break
                    if shot_bytes is None:
                        shot_bytes = initial_path.read_bytes()
                    err_img = Image.open(io.BytesIO(shot_bytes)).convert("RGBA")
                    err_img = annotate_back_attempts_img(err_img, back_attempts)
                    err_ann_path = pd / "error_tap_ann.jpg"
                    _save_report_img(err_img, err_ann_path)
                    error_annotated_url = str(err_ann_path.relative_to(log_dir))

            pages.append(ReconPageInfo(
                name=pd.name,
                title=page_title,
                page_type=page_type,
                description=description,
                elements_count=elements_count,
                signature=signature,
                annotated_url=annotated_url,
                taps=taps,
                flows=[],
                knowledge=knowledge,
                error=page_error,
                error_annotated_url=error_annotated_url,
            ))

        # Leaf pages: discovered via parent taps but not probed (depth_limit)
        existing_names = {p.name for p in pages}
        probed_titles = {p.title for p in pages}  # for dedup by title
        dup_warnings: list[dict] = []  # leaf pages skipped due to title match

        # Load leaf title mapping (raw_name → {title, type}) if exported
        leaf_meta_path = log_dir / "leaf_meta.json"
        leaf_meta: dict[str, dict] = {}
        if leaf_meta_path.exists():
            leaf_meta = json.loads(leaf_meta_path.read_text(encoding="utf-8"))

        # Load knowledge files for content lookup
        knowledge_dir = log_dir.parent.parent.parent / "knowledge" / app_name
        knowledge_files: dict[str, str] = {}  # safe_title → content
        if knowledge_dir.exists():
            for kfile in knowledge_dir.glob("*.md"):
                knowledge_files[kfile.stem] = kfile.read_text(encoding="utf-8")

        for pd in page_dirs:
            result_path = pd / "recon_result.json"
            if not result_path.exists():
                continue
            result = json.loads(result_path.read_text(encoding="utf-8"))
            for tap in result.get("taps", []):
                identity = tap.get("identity") or {}
                is_overlay = identity.get("phase") == "overlay_skip"
                is_depth_limit = tap.get("child_status") == "new_depth_limit"
                if not is_depth_limit and not is_overlay:
                    continue

                if is_overlay:
                    # Overlay taps have no page_name; use composite key
                    meta_key = f"overlay::{pd.name}::{tap.get('label', '')}"
                    leaf_name = meta_key
                    leaf_description = ""
                else:
                    leaf_name = identity.get("page_name", "")
                    if not leaf_name:
                        continue
                    meta_key = leaf_name
                    leaf_description = identity.get("description", "")

                if leaf_name in existing_names:
                    continue
                existing_names.add(leaf_name)

                tap_shot = Path(tap.get("screenshot", ""))
                ann_url = ""
                if tap_shot.is_file():
                    try:
                        ann_url = str(tap_shot.relative_to(log_dir))
                    except ValueError:
                        pass

                # Look up short title from leaf_meta.json (exported)
                meta_entry = leaf_meta.get(meta_key, {})
                leaf_title = meta_entry.get("title", leaf_name if not is_overlay else tap.get("label", "弹窗"))
                leaf_type = meta_entry.get("type", "modal" if is_overlay else "")

                # Skip if same title as a probed page — record as warning
                if leaf_title in probed_titles:
                    dup_warnings.append({
                        "title": leaf_title,
                        "parent": pd.name,
                        "label": tap.get("label", ""),
                        "text_sim": identity.get("text_sim"),
                        "visual_sim": identity.get("visual_sim"),
                    })
                    continue

                # Load knowledge content by matching title to knowledge file
                safe_title = leaf_title.replace("/", "_").replace(" ", "_")
                leaf_knowledge = knowledge_files.get(safe_title, "")
                if not leaf_knowledge and leaf_description:
                    parent_name = result.get("parent_page", "")
                    leaf_knowledge = (
                        f"---\napp: {app_name}\npage_title: {leaf_title}\n"
                        f"page_type: {leaf_type}\nparent_page: {parent_name}\n---\n\n"
                        f"# {leaf_title}\n\n{leaf_description}"
                    )

                pages.append(ReconPageInfo(
                    name=leaf_name,
                    title=leaf_title,
                    page_type="leaf",
                    description=leaf_description,
                    elements_count=0,
                    signature="",
                    annotated_url=ann_url,
                    parent=pd.name,
                    taps=[],
                    flows=[],
                    knowledge=leaf_knowledge,
                    error=None,
                ))

        data = AppReconData(
            app_name=app_name,
            pages=pages,
            stats={
                "pages": len(pages),
                "taps_probed": total_taps,
                "navigated": total_navigated,
                "no_change": total_taps - total_navigated,
            },
            trace=trace_data,
            dup_warnings=dup_warnings,
        )
        return data


# ── Runner builder ─────────────────────────────────────────────

class RunnerReportBuilder:
    def build(self, run_dir: Path) -> ReportData:
        data = ReportData(title=run_dir.name)
        ctx_path = run_dir / "context.json"
        if not ctx_path.exists():
            return data

        ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
        # Title is the user's ORIGINAL input; the resolved goal is shown as provenance.
        # Old logs without raw_input fall back to the goal.
        data.raw_input = ctx.get("raw_input") or ""
        data.goal = ctx.get("goal", "")
        data.router = ctx.get("router") or {}
        data.platform = ctx.get("platform") or ""
        data.output = ctx.get("output") or ""
        data.knowledge = ctx.get("knowledge") or {}
        data.wall_clock_s = ctx.get("wall_clock_s") or 0.0
        data.title = data.raw_input or ctx.get("goal", run_dir.name)

        # Run-level model record; cost is priced against these (not the active config).
        data.models = ctx.get("models", {}) or {}
        _MODELS_MAP.clear()
        _MODELS_MAP.update(data.models)

        turns = ctx.get("turns", [])
        data.settle_s_total = sum((t.get("settle_s") or 0) for t in turns)

        # Sections injected into the planner at least once this run (ordered by first appearance),
        # with bodies read from the knowledge dir so the sidebar can show them on click.
        loaded_order: list[str] = []
        _seen_sec: set[str] = set()
        for _t in turns:
            for _s in (_t.get("sections_loaded") or []):
                if _s not in _seen_sec:
                    _seen_sec.add(_s)
                    loaded_order.append(_s)
        if loaded_order and data.knowledge:
            kdir = (Path(__file__).resolve().parents[1] / "knowledge"
                    / (data.platform or "iphone") / str(data.knowledge.get("app_name", "")))
            for stem in loaded_order:
                fp = kdir / f"{stem}.md"
                body = fp.read_text(encoding="utf-8") if fp.exists() else ""
                data.knowledge_sections.append(
                    {"stem": stem, "title": stem.replace("_", " "), "body": body}
                )

        total_actions = 0
        total_executed = 0
        all_steps: list[ReportStep] = []

        for turn in turns:
            idx = turn.get("index", 0)
            ad = turn.get("action_decision") or {}
            action = ad.get("action") or {}
            atype = action.get("action_type", "none")
            x = action.get("x")
            y = action.get("y")
            desc = action.get("description", "")
            sup = turn.get("supervisor") or {}
            summary = sup.get("summary", "")
            executed = turn.get("executed", False)

            total_actions += 1
            if executed:
                total_executed += 1

            ss_path = run_dir / f"screenshot_turn_{idx}.png"
            if ss_path.exists() and x is not None and y is not None:
                img = _load_img(ss_path)
                annotated_img = annotate_action(
                    img, atype, x, y, idx,
                    direction=action.get("direction"),
                    text=action.get("text"),
                    to_x=action.get("to_x"),
                    to_y=action.get("to_y"),
                    snap=action.get("snap"),
                )
                ann_path = run_dir / f"screenshot_turn_{idx}_ann.jpg"
                _save_report_img(annotated_img, ann_path)
                annotated_url = ann_path.name
            elif ss_path.exists():
                annotated_url = ss_path.name
            else:
                annotated_url = ""

            raw_url = ss_path.name if ss_path.exists() else ""

            status = "✓" if executed else "✗"
            if atype == "none":
                status = "— skip"

            all_steps.append(ReportStep(
                label=f"Turn {idx}",
                action_type=atype,
                x=x,
                y=y,
                description=desc or summary,
                annotated_before_url=annotated_url,
                raw_screenshot_url=raw_url,
                after_url=None,
                status=status,
                timestamp=turn.get("timestamp", ""),
                milestone_id=sup.get("milestone_id", ""),
                milestone_kind=sup.get("milestone_kind", ""),
                instruction=sup.get("instruction", ""),
                summary=summary,
                timings=turn.get("timings", {}),
                token_usage=turn.get("token_usage", {}),
                llm_calls=turn.get("llm_calls", 0),
                action_direction=action.get("direction"),
                action_text=action.get("text"),
                action_to_x=action.get("to_x"),
                action_to_y=action.get("to_y"),
                snap=action.get("snap"),
                sections_loaded=turn.get("sections_loaded") or [],
                relevant_sections=(turn.get("checker") or {}).get("relevant_sections") or [],
            ))

        # Build milestone lookup from persisted decomposition
        ms_lookup: dict[str, dict] = {}
        for ms in ctx.get("milestones", []):
            ms_lookup[ms.get("id", "")] = ms


        # Group steps by milestone
        pages: list[ReportPage] = []
        current_mid = ""
        current_page_steps: list[ReportStep] = []
        milestones_info: list[dict] = []

        for step in all_steps:
            mid = step.milestone_id or "_no_milestone"
            if mid != current_mid:
                if current_page_steps:
                    ms_meta = ms_lookup.get(current_mid, {})
                    pages.append(ReportPage(
                        title=ms_meta.get("name", f"Milestone {current_mid}"),
                        steps=current_page_steps,
                        milestone_id=current_mid,
                        milestone_kind=current_page_steps[0].milestone_kind,
                        milestone_name=ms_meta.get("name", ""),
                        milestone_description=ms_meta.get("description", ""),
                        success_condition=ms_meta.get("success_condition", ""),
                    ))
                current_mid = mid
                current_page_steps = []
            current_page_steps.append(step)

        if current_page_steps:
            ms_meta = ms_lookup.get(current_mid, {})
            pages.append(ReportPage(
                title=ms_meta.get("name", f"Milestone {current_mid}"),
                steps=current_page_steps,
                milestone_id=current_mid,
                milestone_kind=current_page_steps[0].milestone_kind,
                milestone_name=ms_meta.get("name", ""),
                milestone_description=ms_meta.get("description", ""),
                success_condition=ms_meta.get("success_condition", ""),
            ))

        # Build milestones summary
        for page in pages:
            ms_steps = page.steps
            ms_timings: dict[str, float] = {}
            ms_in = ms_out = 0
            for s in ms_steps:
                for k, v in s.timings.items():
                    ms_timings[k] = ms_timings.get(k, 0) + v
                si, so = _sum_tokens(s.token_usage)
                ms_in += si
                ms_out += so
            milestones_info.append({
                "id": page.milestone_id,
                "name": page.milestone_name,
                "kind": page.milestone_kind,
                "description": page.milestone_description,
                "success_condition": page.success_condition,
                "turns": f"{ms_steps[0].label.split()[-1]}-{ms_steps[-1].label.split()[-1]}",
                "total_time": sum(ms_timings.values()),
                "timings": ms_timings,
                "input_tokens": ms_in,
                "output_tokens": ms_out,
                "cost": sum(_token_cost(s.token_usage) for s in ms_steps),
            })

        # Set verification screenshot + checker: screenshot from next milestone's first turn;
        # checker from ms_lookup[id].done_check (saved by runner when milestone completes).
        for i, page in enumerate(pages):
            if i + 1 < len(pages):
                next_first = pages[i + 1].steps[0] if pages[i + 1].steps else None
                if next_first and next_first.raw_screenshot_url:
                    page.verify_url = next_first.raw_screenshot_url
            page.verify_checker = ms_lookup.get(page.milestone_id, {}).get("done_check", {})

        data.pages = pages
        data.milestones = milestones_info
        # Decompose summary: list all milestones with names
        ms_parts = []
        for ms in milestones_info:
            ms_parts.append(f"#{ms['id']} {ms['name']}（{ms['kind']}）")
        data.decompose_summary = " → ".join(ms_parts) if ms_parts else ""
        data.stats = {
            "turns": len(turns),
            "executed": total_executed,
        }
        return data


# ── Recon HTML generator ───────────────────────────────────────

PAGE_TYPE_LABELS: dict[str, str] = {
    "list": "列表",
    "chat": "聊天",
    "detail": "详情",
    "form": "表单",
    "modal": "弹窗",
    "home": "首页",
    "other": "其他",
}

RECON_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{app_name} — Recon Report</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  :root {{
    --green: #22c55e;
    --gray: #9ca3af;
    --blue: #3b82f6;
    --bg: #f1f5f9;
    --card: #ffffff;
    --border: #e2e8f0;
    --text: #1e293b;
    --muted: #64748b;
    --radius: 12px;
  }}
  body {{ font-family: -apple-system, "PingFang SC", "Helvetica Neue", sans-serif; background: var(--bg); color: var(--text); }}

  /* ── Layout ── */
  .layout {{ display: flex; min-height: 100vh; }}
  .sidebar {{
    width: 220px; flex-shrink: 0; position: sticky; top: 0; height: 100vh;
    background: var(--card); border-right: 1px solid var(--border);
    overflow-y: auto; padding: 20px 0;
  }}
  .main {{ flex: 1; padding: 24px; max-width: 900px; }}

  /* ── Sidebar ── */
  .sidebar-title {{ font-size: 10px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; padding: 0 16px 10px; }}
  .sidebar-stats {{ padding: 14px 16px; margin-top: 8px; border-top: 1px solid var(--border); font-size: 12px; color: var(--muted); line-height: 2; }}
  .sidebar-stats strong {{ color: var(--text); }}
  .sidebar-warnings {{ margin-top: 8px; border-top: 1px solid var(--border); }}
  .sidebar-warnings-title {{ padding: 10px 16px; font-size: 11px; font-weight: 600; color: #b45309; cursor: pointer; user-select: none; list-style: none; display: flex; align-items: center; gap: 6px; }}
  .sidebar-warnings-title::before {{ content: "▶"; font-size: 8px; transition: transform 0.15s; }}
  details.sidebar-warnings[open] .sidebar-warnings-title::before {{ transform: rotate(90deg); }}
  .warn-item {{ padding: 6px 16px; border-top: 1px solid var(--border); }}
  .warn-title {{ font-size: 11px; color: var(--text); font-weight: 500; }}
  .warn-detail {{ font-size: 10px; color: var(--muted); margin-top: 2px; }}

  /* ── Nav tree ── */
  .nav-tree, .nav-tree ul {{ list-style: none; margin: 0; padding: 0; }}
  .nav-tree {{ padding: 0 8px; }}
  .nav-tree > li {{ padding-left: 4px; }}
  .nav-tree ul {{
    margin-left: 14px;
    padding-left: 10px;
    border-left: 1px solid var(--border);
  }}
  .tree-node {{ position: relative; padding: 1px 0; }}
  .nav-tree ul > .tree-node::before {{
    content: "";
    position: absolute;
    left: -11px;
    top: 14px;
    width: 10px;
    height: 1px;
    background: var(--border);
  }}
  .tree-link {{
    display: flex; align-items: center; gap: 5px;
    padding: 5px 8px; border-radius: 6px;
    font-size: 12px; color: var(--text); text-decoration: none;
    transition: background 0.12s;
  }}
  .tree-link:hover {{ background: var(--bg); }}
  .tree-link-error {{ color: #dc2626; }}
  .tree-link-error:hover {{ background: #fef2f2; }}
  .tree-link-leaf {{ color: var(--gray); }}
  .tree-link-leaf:hover {{ background: #f9fafb; }}
  .tree-error-dot {{ font-size: 10px; flex-shrink: 0; }}
  .tree-leaf {{
    display: block; padding: 4px 8px;
    font-size: 11px; color: var(--gray); font-style: italic;
  }}
  .tree-chip {{
    font-size: 9px; padding: 1px 4px; border-radius: 3px; flex-shrink: 0;
    background: #e0e7ff; color: #4338ca;
  }}

  /* ── Header ── */
  .header {{ margin-bottom: 24px; }}
  .header h1 {{ font-size: 22px; font-weight: 700; margin-bottom: 4px; }}
  .header .subtitle {{ font-size: 13px; color: var(--muted); }}

  /* ── Tree connectors (right panel) ── */
  .tree-item {{ position: relative; }}
  .tree-children {{
    margin-left: 20px;
    padding-left: 28px;
    border-left: 2px solid var(--border);
  }}
  .tree-children > .tree-item {{ position: relative; }}
  .tree-children > .tree-item::before {{
    content: "";
    position: absolute;
    left: -28px;
    top: 32px;
    width: 28px;
    height: 2px;
    background: var(--border);
  }}

  /* ── Page card ── */
  .page-card {{
    background: var(--card); border-radius: var(--radius);
    border: 1px solid var(--border); margin-bottom: 16px;
    overflow: hidden;
  }}
  .page-card-error {{ border-color: #fca5a5; border-left: 3px solid #ef4444; }}
  .page-card-error .page-card-header {{ background: #fff5f5; }}
  .page-breadcrumb {{
    padding: 6px 20px;
    font-size: 11px; color: var(--muted);
    background: #f8fafc; border-bottom: 1px solid var(--border);
  }}
  .bc-sep {{ color: #cbd5e1; margin: 0 5px; }}
  .bc-current {{ color: var(--text); font-weight: 600; }}
  .page-card-header {{
    padding: 12px 20px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  }}
  .page-card-header h2 {{ font-size: 15px; font-weight: 600; }}
  .type-badge {{
    font-size: 11px; padding: 2px 8px; border-radius: 20px; font-weight: 500;
    background: #dbeafe; color: #1d4ed8;
  }}
  .page-sig {{ font-size: 11px; color: var(--muted); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .via-tap {{ font-size: 11px; color: #7c3aed; background: #ede9fe; padding: 2px 7px; border-radius: 10px; white-space: nowrap; flex-shrink: 0; }}
  .page-card-desc {{ padding: 10px 20px; font-size: 13px; color: var(--muted); border-bottom: 1px solid var(--border); background: #f8fafc; }}

  /* ── Page body ── */
  .page-card-body {{ display: flex; gap: 0; }}
  .screenshot-col {{
    width: 260px; flex-shrink: 0; padding: 16px;
    border-right: 1px solid var(--border);
  }}
  .screenshot-col img {{
    width: 100%; border-radius: 8px; border: 1px solid var(--border);
    cursor: zoom-in; display: block;
  }}
  .legend {{ display: flex; gap: 12px; margin-top: 8px; font-size: 11px; color: var(--muted); justify-content: center; }}
  .legend-dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; vertical-align: middle; margin-right: 3px; }}
  .info-col {{ flex: 1; min-width: 0; padding: 16px; display: flex; flex-direction: column; gap: 16px; }}

  /* ── Section labels ── */
  .section-label {{ font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }}

  /* ── Tap list ── */
  .tap-list {{ display: flex; flex-direction: column; }}
  .tap-item {{ padding: 7px 2px; border-bottom: 1px solid var(--border); }}
  .tap-item:last-child {{ border-bottom: none; }}
  .tap-item-main {{ display: flex; align-items: center; gap: 8px; min-width: 0; }}
  .tap-item-detail {{
    margin-top: 4px; margin-left: 30px;
    display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  }}
  .tap-num {{ width: 22px; flex-shrink: 0; text-align: center; font-size: 11px; font-weight: 600; color: var(--muted); }}
  .tap-label {{ font-size: 13px; font-weight: 500; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .tap-nav {{ font-size: 12px; color: var(--green); flex-shrink: 0; }}
  .tap-none {{ font-size: 12px; color: var(--gray); flex-shrink: 0; }}
  .tap-identity {{
    font-size: 11px; padding: 1px 7px; border-radius: 8px; flex-shrink: 0;
  }}
  .tap-identity-new {{ background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe; }}
  .tap-identity-dup {{ background: #fefce8; color: #a16207; border: 1px solid #fde68a; }}
  .tap-identity-overlay {{ background: #fdf4ff; color: #7e22ce; border: 1px solid #e9d5ff; }}
  .tap-identity-desc {{ font-size: 11px; color: var(--muted); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }}
  .tap-identity-scores {{ font-size: 11px; color: #94a3b8; font-family: monospace; flex-shrink: 0; }}
  .tap-preview-btn {{
    font-size: 11px; padding: 2px 8px; border-radius: 4px; border: 1px solid var(--border);
    background: var(--bg); color: var(--muted); cursor: pointer; flex-shrink: 0;
    transition: all 0.15s; white-space: nowrap; margin-left: auto;
  }}
  .tap-preview-btn:hover {{ background: #dbeafe; border-color: #93c5fd; color: #1d4ed8; }}

  /* ── Identity JSON panel ── */
  .tap-identity {{ cursor: pointer; user-select: none; }}
  .tap-identity::after {{ content: " ▾"; font-size: 9px; opacity: 0.6; }}
  .tap-identity.open::after {{ content: " ▴"; }}
  .tap-json-panel {{
    display: none; margin: 6px 0 2px 30px;
    background: #f8fafc; border: 1px solid var(--border); border-radius: 6px;
    padding: 8px 12px; font-family: "SF Mono", "Fira Code", monospace; font-size: 11px;
    line-height: 1.7;
  }}
  .tap-json-panel.show {{ display: block; }}
  .jobj {{ display: flex; flex-direction: column; gap: 0; }}
  .jrow {{ padding-left: 14px; }}
  .jk {{ color: #7c3aed; }}
  .jpunct {{ color: #94a3b8; }}
  .jv-str {{ color: #16a34a; }}
  .jv-num {{ color: #2563eb; }}
  .jv-bool {{ color: #d97706; }}
  .jv-null {{ color: #94a3b8; font-style: italic; }}

  /* ── Error banner ── */
  .error-banner {{
    font-size: 12px; color: #991b1b;
    background: #fef2f2; border-bottom: 1px solid #fecaca;
  }}
  .error-title {{
    font-weight: 600; list-style: none;
    padding: 10px 20px;
    display: flex; align-items: center; gap: 6px; cursor: pointer;
  }}
  .error-title::-webkit-details-marker {{ display: none; }}
  .error-title::before {{ content: "▶"; font-size: 9px; transition: transform 0.15s; margin-right: 2px; }}
  details.error-banner[open] .error-title::before {{ transform: rotate(90deg); }}
  .error-meta {{ color: #b91c1c; margin-bottom: 8px; font-size: 11px; padding: 0 20px; }}
  .error-body {{ padding: 0 20px 12px; }}
  .error-body {{ display: flex; gap: 16px; align-items: flex-start; }}
  .error-screenshot {{ flex-shrink: 0; width: 120px; }}
  .error-screenshot img {{
    width: 100%; border-radius: 6px; border: 1px solid #fecaca;
    cursor: zoom-in; display: block;
  }}
  .error-screenshot-caption {{ font-size: 10px; color: #b91c1c; text-align: center; margin-top: 4px; }}
  .back-timeline {{ display: flex; flex-direction: column; gap: 4px; flex: 1; }}
  .back-step {{
    display: flex; align-items: center; gap: 8px;
    font-size: 11px; padding: 4px 8px; border-radius: 6px;
    background: #fff7f7; border: 1px solid #fecaca;
  }}
  .back-step-ok {{ background: #f0fdf4; border-color: #bbf7d0; color: #166534; }}
  .back-step-retry {{ background: #fff7ed; border-color: #fed7aa; color: #9a3412; }}
  .back-num {{ width: 18px; height: 18px; border-radius: 50%; background: #fecaca; color: #991b1b; font-weight: 700; font-size: 10px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }}
  .back-step-ok .back-num {{ background: #bbf7d0; color: #166534; }}
  .back-step-retry .back-num {{ background: #fed7aa; color: #c2410c; font-size: 12px; }}
  .back-strategy {{ font-weight: 600; width: 70px; flex-shrink: 0; color: #991b1b; }}
  .back-step-ok .back-strategy {{ color: #166534; }}
  .back-step-retry .back-strategy {{ color: #ea580c; }}
  .back-coords {{ color: #64748b; width: 72px; flex-shrink: 0; font-family: monospace; }}
  .back-result {{ flex: 1; color: #64748b; }}
  .back-score {{ font-family: monospace; color: #b91c1c; margin-left: auto; flex-shrink: 0; }}
  .back-step-ok .back-score {{ color: #166534; }}

  /* ── Flows ── */
  .flows-list {{ display: flex; flex-direction: column; gap: 8px; }}
  .flow-item {{ padding: 8px 10px; background: #f0fdf4; border-radius: 8px; border: 1px solid #bbf7d0; }}
  .flow-target {{ font-size: 13px; font-weight: 600; color: #15803d; }}
  .flow-desc {{ font-size: 12px; color: #166534; margin-top: 2px; line-height: 1.4; }}

  /* ── Knowledge ── */
  details {{ border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }}
  summary {{
    padding: 8px 12px; font-size: 12px; font-weight: 600; color: var(--muted);
    cursor: pointer; user-select: none; list-style: none; display: flex; align-items: center; gap: 6px;
    background: #f8fafc;
  }}
  summary::before {{ content: "▶"; font-size: 9px; transition: transform 0.15s; }}
  details[open] summary::before {{ transform: rotate(90deg); }}
  .knowledge-body {{ padding: 12px; font-size: 12px; color: #475569; line-height: 1.7; white-space: pre-wrap; background: white; }}

  /* ── Modal ── */
  .modal {{
    display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    background: rgba(0,0,0,0.88); z-index: 1000;
    justify-content: center; align-items: center; gap: 16px;
  }}
  .modal.show {{ display: flex; }}
  #modal-simple {{ display: flex; align-items: center; gap: 16px; }}
  .modal-panel {{
    background: white; border-radius: 12px; padding: 12px;
    display: flex; flex-direction: column; align-items: center; gap: 8px;
    max-height: 90vh; border: 2px solid transparent;
  }}
  .modal-panel img {{ max-height: calc(90vh - 60px); max-width: 320px; border-radius: 8px; object-fit: contain; }}
  .modal-label {{ font-size: 11px; color: #64748b; text-align: center; max-width: 280px; }}
  .modal-close {{
    position: fixed; top: 20px; right: 24px; font-size: 28px; color: white;
    cursor: pointer; line-height: 1; opacity: 0.8; z-index: 1001;
  }}
  .modal-close:hover {{ opacity: 1; }}
  .tap-seq-btn {{ background: #ede9fe; border-color: #c4b5fd; color: #6d28d9; }}
  .tap-seq-btn:hover {{ background: #ddd6fe; border-color: #a78bfa; color: #5b21b6; }}
</style>
</head>
<body>

<div class="layout">
  <nav class="sidebar">
    <div class="sidebar-title">{app_name}</div>
    {sidebar_items}
    <div class="sidebar-stats">
      <strong>{pages}</strong> 页面<br>
      <strong>{taps_probed}</strong> 点击测试<br>
      <span style="color:var(--green)">●</span> <strong>{navigated}</strong> 导航成功<br>
      <span style="color:var(--gray)">●</span> <strong>{no_change}</strong> 无变化
    </div>
    {warnings_html}
  </nav>

  <main class="main">
    <div class="header">
      <h1>{app_name}</h1>
      <div class="subtitle">Recon Report · {pages} 个页面 · {taps_probed} 次点击探测</div>
    </div>

    {pages_html}
  </main>
</div>

<!-- Modal -->
<div class="modal" id="modal" onclick="closeModal(event)">
  <div class="modal-close" onclick="document.getElementById('modal').classList.remove('show')">✕</div>
  <!-- Simple zoom mode -->
  <div id="modal-simple" style="display:none">
    <div class="modal-panel" id="modal-before">
      <img id="modal-before-img" src="">
      <div class="modal-label">操作前</div>
    </div>
    <div id="modal-arrow" style="font-size:28px;color:white;display:none">→</div>
    <div class="modal-panel" id="modal-after-panel" style="display:none">
      <img id="modal-after-img" src="">
      <div class="modal-label" id="modal-after-label"></div>
    </div>
  </div>
  <!-- Sequence mode -->
  <div id="modal-seq-wrap" style="display:none;overflow-x:auto;max-width:96vw;padding:8px">
    <div id="modal-seq" style="display:flex;align-items:flex-start;gap:12px;min-width:max-content"></div>
  </div>
</div>

<script>
function openModal(beforeSrc, afterSrc, label) {{
  document.getElementById('modal-simple').style.display = 'flex';
  document.getElementById('modal-seq-wrap').style.display = 'none';
  document.getElementById('modal-before-img').src = beforeSrc;
  var afterPanel = document.getElementById('modal-after-panel');
  var arrow = document.getElementById('modal-arrow');
  if (afterSrc) {{
    document.getElementById('modal-after-img').src = afterSrc;
    document.getElementById('modal-after-label').textContent = label || '操作后';
    afterPanel.style.display = '';
    arrow.style.display = '';
  }} else {{
    afterPanel.style.display = 'none';
    arrow.style.display = 'none';
  }}
  document.getElementById('modal').classList.add('show');
}}
function openSeq(key) {{
  var steps = (window._seqs || {{}})[key] || [];
  var seq = document.getElementById('modal-seq');
  seq.innerHTML = '';
  steps.forEach(function(step, i) {{
    if (i > 0) {{
      var arr = document.createElement('div');
      arr.style.cssText = 'font-size:22px;color:rgba(255,255,255,0.5);align-self:center;flex-shrink:0';
      arr.textContent = '→';
      seq.appendChild(arr);
    }}
    var panel = document.createElement('div');
    panel.className = 'modal-panel';
    if (step.is_retry) panel.style.borderColor = 'rgba(251,146,60,0.6)';
    else if (step.success === true) panel.style.borderColor = 'rgba(34,197,94,0.5)';
    else if (step.success === false) panel.style.borderColor = 'rgba(239,68,68,0.4)';
    var img = document.createElement('img');
    img.src = step.src;
    img.style.cursor = 'default';
    if (step.is_retry) img.style.opacity = '0.35';
    var lbl = document.createElement('div');
    lbl.className = 'modal-label';
    lbl.style.whiteSpace = 'nowrap';
    lbl.textContent = '步骤 ' + (i + 1) + (step.subtitle ? ': ' + step.subtitle : '');
    if (step.is_retry) lbl.style.color = '#fb923c';
    else if (step.success === true) lbl.style.color = '#4ade80';
    else if (step.success === false) lbl.style.color = '#f87171';
    panel.appendChild(img);
    panel.appendChild(lbl);
    seq.appendChild(panel);
  }});
  document.getElementById('modal-simple').style.display = 'none';
  document.getElementById('modal-seq-wrap').style.display = '';
  document.getElementById('modal').classList.add('show');
}}
function showTapResult(btn) {{
  var pageCard = btn.closest('.page-card');
  var annotatedImg = pageCard.querySelector('.screenshot-col img');
  openModal(annotatedImg.src, btn.dataset.after || null, btn.dataset.label);
}}
function closeModal(e) {{
  if (e.target === document.getElementById('modal')) {{
    document.getElementById('modal').classList.remove('show');
  }}
}}
document.addEventListener('keydown', function(e) {{
  if (e.key === 'Escape') document.getElementById('modal').classList.remove('show');
}});
function toggleIdentity(id, chip) {{
  var panel = document.getElementById(id);
  var show = panel.classList.toggle('show');
  chip.classList.toggle('open', show);
}}
</script>
</body>
</html>
"""


def _slug(name: str) -> str:
    return re.sub(r"[^\w]", "-", name)


def _render_page_card_html(node: NavNode, path: list[str]) -> str:
    """Recursively render a page card and all its explored children."""
    if node.page is None:
        return ""

    page = node.page
    type_label = PAGE_TYPE_LABELS.get(page.page_type, page.page_type)
    slug = _slug(page.name)
    path_with_self = path + [page.title]

    # Breadcrumb: full path for any non-root page
    breadcrumb_html = ""
    if path:
        parts = ""
        for seg in path:
            parts += f'<span class="bc-seg">{seg}</span><span class="bc-sep">›</span>'
        parts += f'<span class="bc-current">{page.title}</span>'
        breadcrumb_html = f'<div class="page-breadcrumb">{parts}</div>'

    via_html = ""
    if node.via_tap:
        via_html = f'<span class="via-tap">← {node.via_tap}</span>'

    # Match flows to navigated taps by checking if tap.label appears in flow_description
    flow_by_tap: dict[int, ReconFlow] = {}
    used_flows: set[int] = set()
    for tap in page.taps:
        if not tap.navigated or not tap.label:
            continue
        for fi, flow in enumerate(page.flows):
            if fi not in used_flows and tap.label in flow.flow_description:
                flow_by_tap[tap.index] = flow
                used_flows.add(fi)
                break
    # Merged tap + flow table
    tap_rows = ""
    seq_scripts = ""
    for tap in page.taps:
        # Build identity chip (clickable → expands JSON panel)
        identity_html = ""
        identity_json_panel = ""
        ident = tap.identity
        if ident and tap.navigated:
            is_new = ident.get("is_new", True)
            phase = ident.get("phase", "")
            matched_name = ident.get("matched_name")
            json_id = f"ident-{_slug(page.name)}-{tap.index}"
            json_html = _render_identity_json(ident)
            identity_json_panel = f'<div class="tap-json-panel" id="{json_id}">{json_html}</div>'

            if is_new:
                form = _form_from_fingerprint(ident.get("fingerprint", ""))
                is_overlay = form in _OVERLAY_FORMS if form else False
                if is_overlay:
                    form_label = _FORM_LABELS.get(form or "", "弹窗页")
                    identity_html = (
                        f'<span class="tap-identity tap-identity-overlay"'
                        f' onclick="toggleIdentity(\'{json_id}\', this)">◈ {form_label}</span>'
                    )
                else:
                    phase_label = {"new_page": "新页面", "visual_shortcut": "新页面", "text_match": "新页面"}.get(phase, phase)
                    identity_html = (
                        f'<span class="tap-identity tap-identity-new"'
                        f' onclick="toggleIdentity(\'{json_id}\', this)">✦ {phase_label}</span>'
                    )
            else:
                phase_label = {"visual_shortcut": "视觉命中", "semantic_match": "语义命中", "text_match": "语义命中"}.get(phase, phase)
                identity_html = (
                    f'<span class="tap-identity tap-identity-dup"'
                    f' onclick="toggleIdentity(\'{json_id}\', this)">⟳ {phase_label}</span>'
                )

        if tap.navigated:
            seq_key = f"{_slug(page.name)}-{tap.index}"
            if len(tap.back_seq) > 1:
                import json as _json
                seq_json = _json.dumps(tap.back_seq)
                seq_scripts += f'_seqs[{_json.dumps(seq_key)}]={seq_json};\n'
                btn = (
                    f'<button class="tap-preview-btn tap-seq-btn"'
                    f" onclick=\"openSeq('{seq_key}')\">查看全过程 ({len(tap.back_seq)})</button>"
                )
            elif tap.after_url:
                tap_lbl = f"tap_{tap.index}: {tap.label}"
                btn = (
                    f'<button class="tap-preview-btn"'
                    f' data-label="{tap_lbl}" data-after="{tap.after_url}"'
                    f' onclick="showTapResult(this)">查看结果</button>'
                )
            else:
                btn = ""

            tap_rows += f"""
            <div class="tap-item">
              <div class="tap-item-main">
                <span class="tap-num">{tap.index}</span>
                <span class="tap-label" title="{tap.label}">{tap.label or "—"}</span>
                <span class="tap-nav">✓ 导航成功</span>
                {btn}
              </div>
              {"<div class='tap-item-detail'>" + identity_html + "</div>" if identity_html else ""}
              {identity_json_panel}
            </div>"""
        else:
            tap_rows += f"""
            <div class="tap-item">
              <div class="tap-item-main">
                <span class="tap-num">{tap.index}</span>
                <span class="tap-label" title="{tap.label}">{tap.label or "—"}</span>
                <span class="tap-none">— 无变化</span>
              </div>
            </div>"""

    nav_count = sum(1 for t in page.taps if t.navigated)
    tap_section = f"""
        <div>
          <div class="section-label">点击探测 ({nav_count}/{len(page.taps)} 导航成功)</div>
          <div class="tap-list">{tap_rows}</div>
        </div>"""

    flows_section = ""

    knowledge_section = ""
    if page.knowledge.strip():
        safe_knowledge = (
            page.knowledge
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        knowledge_section = f"""
            <details open>
              <summary>页面知识摘要</summary>
              <div class="knowledge-body">{safe_knowledge}</div>
            </details>"""

    card_cls = "page-card page-card-error" if page.error else "page-card"
    seq_init = f"<script>if(!window._seqs)window._seqs={{}};\n{seq_scripts}</script>" if seq_scripts else ""
    card_html = f"""
        {seq_init}<div class="{card_cls}" id="{slug}">
          {breadcrumb_html}
          <div class="page-card-header">
            <h2>{page.title}</h2>
            {f'<span class="type-badge">{type_label}</span>' if type_label and page.page_type != "leaf" else ''}
            {via_html}
          </div>
          {'<div class="page-card-desc">' + page.description + '</div>' if page.description else ''}
          {_render_error_html(page.error, page.error_annotated_url)}
          <div class="page-card-body">
            <div class="screenshot-col">
              <img src="{page.annotated_url}" onclick="openModal(this.src)" title="点击放大">
              <div class="legend">
                <span><span class="legend-dot" style="background:var(--green)"></span>导航成功</span>
                <span><span class="legend-dot" style="background:var(--gray)"></span>无变化</span>
              </div>
            </div>
            <div class="info-col">
              {tap_section}
              {flows_section}
              {knowledge_section}
            </div>
          </div>
        </div>"""

    # Recursively render explored children
    children_html = "".join(
        _render_page_card_html(child, path_with_self)
        for child in node.children
        if child.page is not None
    )
    children_section = f'<div class="tree-children">{children_html}</div>' if children_html else ""

    return f'<div class="tree-item">{card_html}{children_section}</div>'


def generate_recon_html(data: AppReconData) -> str:
    roots = _build_nav_tree(data.pages, trace=data.trace)
    tree_html = _render_tree_html(roots)
    sidebar_items = f'<ul class="nav-tree">{tree_html}</ul>'

    pages_html = "".join(_render_page_card_html(root, []) for root in roots)

    # Build warnings HTML
    warnings_html = ""
    if data.dup_warnings:
        items = ""
        for w in data.dup_warnings:
            sims = ""
            if w.get("text_sim") is not None:
                sims += f' text_sim={w["text_sim"]:.3f}'
            if w.get("visual_sim") is not None:
                sims += f' visual_sim={w["visual_sim"]:.3f}'
            safe_title = w["title"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            safe_parent = w["parent"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            safe_label = w["label"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            items += (
                f'<div class="warn-item">'
                f'<div class="warn-title">⊘ {safe_title}</div>'
                f'<div class="warn-detail">← {safe_parent} · [{safe_label}]{sims}</div>'
                f'</div>'
            )
        warnings_html = (
            f'<details class="sidebar-warnings">'
            f'<summary class="sidebar-warnings-title">⚠ {len(data.dup_warnings)} 个重复页</summary>'
            f'{items}'
            f'</details>'
        )

    return RECON_HTML_TEMPLATE.format(
        app_name=data.app_name,
        sidebar_items=sidebar_items,
        warnings_html=warnings_html,
        pages_html=pages_html,
        **data.stats,
    )


# ── Runner HTML generator ──────────────────────────────────────

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  :root {{
    --bg: #f1f5f9; --card: #fff; --border: #e2e8f0;
    --text: #1e293b; --muted: #64748b; --radius: 12px;
  }}
  body {{ font-family: -apple-system, "PingFang SC", sans-serif; background: var(--bg); color: var(--text); }}

  /* ── Layout: sticky outline sidebar + scrolling main ── */
  .layout {{ display: flex; align-items: flex-start; }}
  .sidebar {{
    width: 240px; flex-shrink: 0; position: sticky; top: 0; height: 100vh;
    overflow-y: auto; background: var(--card); border-right: 1px solid var(--border);
    padding: 20px 0;
  }}
  .main {{ flex: 1; min-width: 0; padding: 24px; }}

  /* ── Outline (子目标分解) ── */
  .sidebar-title {{ font-size: 11px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; padding: 0 18px 12px; }}
  .outline {{ display: flex; flex-direction: column; }}
  .outline-item {{ display: block; text-decoration: none; color: inherit; padding: 8px 16px 8px 18px; border-left: 2px solid transparent; transition: background 0.12s, border-color 0.12s; }}
  .outline-item:hover {{ background: #f8fafc; }}
  .outline-item.active {{ border-left-color: #6366f1; background: #eef2ff; }}
  .outline-top {{ display: flex; align-items: baseline; gap: 6px; }}
  .outline-id {{ font-weight: 700; color: #4338ca; font-size: 12px; flex-shrink: 0; }}
  .outline-name {{ font-size: 12px; color: var(--text); font-weight: 500; line-height: 1.35; }}
  .outline-meta {{ display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 4px; padding-left: 22px; font-size: 10px; color: #94a3b8; font-family: monospace; }}
  .sidebar-empty {{ padding: 4px 18px; font-size: 12px; color: var(--muted); }}

  /* ── Knowledge (知识库注入) ── */
  .sidebar-knowledge {{ margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--border); }}
  .sk-app {{ padding: 0 18px; font-size: 13px; font-weight: 600; color: #0891b2; }}
  .sk-meta {{ padding: 3px 18px 0; font-size: 10px; color: #94a3b8; font-family: monospace; line-height: 1.6; }}
  .sk-sections-label {{ padding: 12px 18px 4px; font-size: 10px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }}
  .sk-item {{ padding: 6px 18px 6px 26px; font-size: 12px; color: #0e7490; cursor: pointer; position: relative; line-height: 1.4; transition: background 0.12s; }}
  .sk-item::before {{ content: "📖"; position: absolute; left: 7px; font-size: 10px; }}
  .sk-item:hover {{ background: #ecfeff; text-decoration: underline; }}
  .sk-modal-card {{ background: #fff; max-width: 720px; width: 86%; max-height: 80vh; border-radius: 10px; display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 12px 40px rgba(0,0,0,0.3); }}
  .sk-modal-head {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 14px 18px; border-bottom: 1px solid var(--border); font-weight: 600; color: #0891b2; }}
  .sk-modal-x {{ cursor: pointer; color: #94a3b8; font-size: 16px; flex-shrink: 0; }}
  .sk-modal-x:hover {{ color: var(--text); }}
  .sk-modal-body {{ margin: 0; padding: 18px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; font-size: 12.5px; line-height: 1.7; color: var(--text); font-family: ui-monospace, SFMono-Regular, monospace; }}

  /* Header */
  .header {{ max-width: 1080px; margin: 0 auto 20px; padding: 20px 24px; background: var(--card); border-radius: var(--radius); box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  .header h1 {{ font-size: 18px; font-weight: 700; margin-bottom: 4px; }}
  .stats {{ color: var(--muted); font-size: 12px; }}
  .decompose {{ margin-top: 10px; padding: 10px 14px; background: #f8fafc; border-radius: 8px; font-size: 12px; color: #475569; line-height: 1.5; }}
  .decompose-label {{ font-weight: 600; color: #6366f1; margin-right: 4px; }}

  /* Final output / 最终输出 card */
  .result-card {{ max-width: 1080px; margin: 0 auto 20px; padding: 16px 20px; background: var(--card); border-radius: var(--radius); box-shadow: 0 1px 3px rgba(0,0,0,0.08); border-left: 4px solid #22c55e; }}
  .result-label {{ font-size: 11px; font-weight: 700; color: #16a34a; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 8px; }}
  .result-body {{ font-size: 14px; color: var(--text); line-height: 1.6; white-space: pre-wrap; word-break: break-word; }}

  /* Router / input-resolution row (shares the 模型配置 box style) */
  .prov-arrow {{ color: #94a3b8; margin: 0 6px; }}
  .prov-goal {{ color: #1e293b; font-weight: 500; }}
  .prov-via {{ display: inline-block; margin-left: 8px; font-size: 10px; padding: 1px 8px; border-radius: 10px; font-family: monospace; vertical-align: middle; }}
  .prov-via-router {{ background: #dcfce7; color: #166534; }}
  .prov-via-temporal {{ background: #dbeafe; color: #1d4ed8; }}
  .prov-via-none {{ background: #f1f5f9; color: #64748b; }}
  .prov-via-clarify {{ background: #fef9c3; color: #854d0e; }}
  .price-tip {{ position: relative; display: inline-block; margin-left: 8px; }}
  .price-chip {{ font-size: 11px; padding: 1px 8px; border-radius: 10px; background: #eef2ff; color: #4338ca; border: 1px solid #c7d2fe; cursor: help; }}
  .price-pop {{ display: none; position: absolute; z-index: 50; top: 100%; left: 0; margin-top: 6px; background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 8px 12px; box-shadow: 0 4px 14px rgba(0,0,0,0.12); white-space: nowrap; }}
  .price-tip:hover .price-pop {{ display: block; }}
  .price-pop table {{ border-collapse: collapse; font-size: 11px; color: #475569; }}
  .price-pop td {{ padding: 1px 16px 1px 0; }}
  .price-pop .pp-num {{ text-align: right; padding-right: 0; }}
  .price-pop .pp-head td {{ color: #94a3b8; }}

  /* Milestone section */
  .milestone {{ max-width: 1080px; margin: 0 auto 16px; background: var(--card); border-radius: var(--radius); box-shadow: 0 1px 3px rgba(0,0,0,0.08); overflow: hidden; }}
  .milestone-header {{ padding: 10px 20px; background: #f8fafc; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .milestone-header h2 {{ font-size: 14px; font-weight: 600; }}
  .milestone-name {{ font-size: 13px; color: var(--text); }}
  .milestone-desc {{ font-size: 11px; color: var(--muted); width: 100%; }}
  .milestone-sc {{ font-size: 10px; color: #94a3b8; width: 100%; }}
  .milestone-badge {{ font-size: 10px; padding: 2px 7px; border-radius: 20px; font-weight: 500; }}
  .milestone-badge-navigation {{ background: #dbeafe; color: #1d4ed8; }}
  .milestone-badge-action {{ background: #fef3c7; color: #92400e; }}
  .milestone-badge-filter {{ background: #ede9fe; color: #5b21b6; }}
  .milestone-badge-collection {{ background: #d1fae5; color: #065f46; }}
  .milestone-badge-default {{ background: #f1f5f9; color: #475569; }}
  .milestone-time {{ font-size: 11px; color: var(--muted); margin-left: auto; font-family: monospace; }}

  /* Thumbnail gallery — one row per milestone */
  .gallery {{ display: flex; gap: 6px; padding: 12px 16px; overflow-x: auto; }}
  .thumb {{ flex-shrink: 0; width: 140px; cursor: pointer; position: relative; border-radius: 8px; overflow: hidden; border: 2px solid transparent; transition: border-color 0.15s; }}
  .thumb:hover {{ border-color: #6366f1; }}
  .thumb.active {{ border-color: #4338ca; }}
  .thumb img {{ width: 100%; display: block; }}
  .thumb-label {{ position: absolute; bottom: 0; left: 0; right: 0; padding: 3px 6px; font-size: 10px; font-weight: 600; color: #fff; background: linear-gradient(transparent, rgba(0,0,0,0.7)); }}
  .thumb-status {{ position: absolute; top: 4px; right: 4px; width: 16px; height: 16px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 9px; color: #fff; font-weight: 700; }}
  .thumb-status-ok {{ background: #22c55e; }}
  .thumb-status-fail {{ background: #ef4444; }}
  .thumb-status-skip {{ background: #9ca3af; }}
  .thumb-action {{ position: absolute; top: 4px; left: 4px; width: 14px; height: 14px; border-radius: 50%; }}

  /* Detail panel — shown when thumbnail clicked */
  .detail {{ display: none; padding: 12px 20px; border-top: 1px solid var(--border); }}
  .detail.show {{ display: flex; gap: 20px; }}
  .detail-ss {{ width: 200px; flex-shrink: 0; }}
  .detail-ss img {{ width: 100%; border-radius: 8px; border: 1px solid var(--border); cursor: zoom-in; }}
  .detail-info {{ flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 6px; }}
  .detail-top {{ display: flex; align-items: center; gap: 8px; }}
  .detail-idx {{ display: inline-flex; width: 22px; height: 22px; align-items: center; justify-content: center; border-radius: 50%; font-size: 10px; font-weight: 700; color: #fff; flex-shrink: 0; }}
  .detail-at {{ font-size: 10px; padding: 1px 5px; border-radius: 3px; font-weight: 500; }}
  .detail-desc {{ font-size: 13px; font-weight: 500; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .detail-status {{ font-size: 11px; font-weight: 600; flex-shrink: 0; }}
  .detail-time {{ font-size: 13px; font-weight: 600; color: #64748b; flex-shrink: 0; font-variant-numeric: tabular-nums; }}
  .detail-gap {{ font-size: 11px; font-weight: 600; color: #f59e0b; background: #fffbeb; padding: 1px 6px; border-radius: 10px; margin-left: 6px; flex-shrink: 0; font-variant-numeric: tabular-nums; }}
  .detail-instruction {{ font-size: 12px; color: var(--muted); }}
  .detail-summary {{ font-size: 12px; color: #475569; line-height: 1.4; }}

  /* Timing bar */
  .timing-bar {{ display: flex; height: 5px; border-radius: 3px; overflow: hidden; background: #f1f5f9; margin-top: 2px; }}
  .timing-seg {{ height: 100%; min-width: 1px; }}
  .timing-labels {{ display: flex; gap: 8px; font-size: 11px; color: #94a3b8; margin-top: 2px; flex-wrap: wrap; }}
  .timing-label-dot {{ display: inline-block; width: 7px; height: 7px; border-radius: 2px; vertical-align: middle; margin-right: 2px; }}

  /* Action type colors */
  .at-tap {{ background: #dc3232; }} .at-type {{ background: #a032c8; }}
  .at-scroll {{ background: #32b432; }} .at-drag {{ background: #3296dc; }}
  .at-home {{ background: #3278dc; }} .at-press_enter {{ background: #dca000; }}
  .at-clear_text {{ background: #808080; }} .at-none {{ background: #c0c0c0; }}

  .modal {{ display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); z-index: 999; justify-content: center; align-items: center; }}
  .modal.show {{ display: flex; }} .modal img {{ max-width: 90%; max-height: 90%; border-radius: 8px; }}
</style>
</head>
<body>

<div class="layout">
  <nav class="sidebar">
    <div class="sidebar-title">子目标分解</div>
    <div class="outline">{outline_html}</div>
    {knowledge_html}
  </nav>
  <main class="main">
    <div class="header">
      <h1>{title}{platform_badge}</h1>
      <div class="stats">{stats}</div>
      {provenance_html}
      {cost_note_html}
    </div>

    {pages_html}
    {result_html}
  </main>
</div>

<div class="modal" id="modal" onclick="this.classList.remove('show')">
  <img id="modal-img" src="">
</div>
<div class="modal" id="sk-modal" onclick="if(event.target===this)this.classList.remove('show')">
  <div class="sk-modal-card">
    <div class="sk-modal-head">
      <span id="sk-modal-title"></span>
      <span class="sk-modal-x" onclick="document.getElementById('sk-modal').classList.remove('show')">✕</span>
    </div>
    <pre class="sk-modal-body" id="sk-modal-body"></pre>
  </div>
</div>
<script>
// Scroll-spy: highlight the outline item whose milestone is near the top.
(function() {{
  var items = Array.prototype.slice.call(document.querySelectorAll('.outline-item'));
  if (!items.length || !('IntersectionObserver' in window)) return;
  var map = {{}};
  items.forEach(function(it) {{ map[it.dataset.target] = it; }});
  var obs = new IntersectionObserver(function(entries) {{
    entries.forEach(function(e) {{
      if (!e.isIntersecting) return;
      items.forEach(function(i) {{ i.classList.remove('active'); }});
      var it = map[e.target.id];
      if (it) it.classList.add('active');
    }});
  }}, {{ rootMargin: '-15% 0px -75% 0px', threshold: 0 }});
  items.forEach(function(it) {{
    var sec = document.getElementById(it.dataset.target);
    if (sec) obs.observe(sec);
  }});
}})();
function showDetail(id) {{
  var el = document.getElementById(id);
  if (el.classList.contains('show')) {{
    el.classList.remove('show');
    var thumb = document.querySelector('[data-detail="' + id + '"]');
    if (thumb) thumb.classList.remove('active');
    return;
  }}
  // Hide all details in same milestone
  var ms = el.closest('.milestone');
  ms.querySelectorAll('.detail').forEach(d => d.classList.remove('show'));
  ms.querySelectorAll('.thumb').forEach(t => t.classList.remove('active'));
  // Show selected
  el.classList.add('show');
  var thumb = document.querySelector('[data-detail="' + id + '"]');
  if (thumb) thumb.classList.add('active');
}}
function zoomImg(src) {{
  document.getElementById('modal-img').src = src;
  document.getElementById('modal').classList.add('show');
}}
function showSection(id, title) {{
  var src = document.getElementById(id);
  if (!src) return;
  document.getElementById('sk-modal-title').textContent = title;
  document.getElementById('sk-modal-body').textContent = src.textContent;
  document.getElementById('sk-modal').classList.add('show');
}}
</script>
</body>
</html>
"""

TIMING_COLORS: dict[str, str] = {
    "decompose": "#6366f1",
    "checker": "#f59e0b",
    "planner": "#3b82f6",
    "replanner": "#ef4444",
    "loop_check": "#8b5cf6",
    "loop_scroll": "#06b6d4",
    "action_policy": "#22c55e",
}

KIND_BADGE = {
    "navigation": "milestone-badge-navigation",
    "action": "milestone-badge-action",
    "filter": "milestone-badge-filter",
    "collection": "milestone-badge-collection",
}

AT_LABELS = {
    "tap": "点击", "type": "输入", "scroll": "滚动", "drag": "拖动",
    "home": "主屏", "press_enter": "回车", "clear_text": "清空", "none": "跳过",
}


def _safe(text: str | None) -> str:
    if not text:
        return ""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _short_mid(mid) -> str:
    """Display-only short id: leading number of a slug ('1_open_wechat' -> '1').

    The full id is still used for anchors/links; this only shortens what's shown
    so a long slug doesn't crowd out the milestone name in the sidebar.
    """
    m = re.match(r"\s*(\d+)", str(mid))
    return m.group(1) if m else str(mid)


def _render_timing_html(timings: dict[str, float]) -> str:
    if not timings:
        return ""
    total = sum(timings.values())
    if total <= 0:
        return ""
    segs = ""
    labels = ""
    for tname, tval in timings.items():
        pct = tval / total * 100
        tc = TIMING_COLORS.get(tname, "#94a3b8")
        segs += f'<div class="timing-seg" style="width:{pct:.1f}%;background:{tc}" title="{tname}: {tval:.2f}s"></div>'
        labels += (
            f'<span><span class="timing-label-dot" style="background:{tc}"></span>'
            f'{tname} {tval:.1f}s</span>'
        )
    return (
        f'<div class="timing-bar">{segs}</div>'
        f'<div class="timing-labels">{labels} · total {total:.1f}s</div>'
    )


def _ctx_pct_tag(n: int) -> str:
    """A small, spaced ``N%`` — an input-token count as a share of the model context window,
    colored by pressure (gray <75%, amber ≥75%, red ≥90%). A subordinate annotation after each
    module's input count (e.g. ``checker 3.9k 3% /136``), kept light so it doesn't crowd the
    numbers. Empty when it rounds to 0%."""
    if not n:
        return ""
    pct = n / CONTEXT_WINDOW * 100 if CONTEXT_WINDOW else 0
    if pct < 0.5:
        return ""
    return f'<span style="font-size:0.84em;color:{_ctx_color(n)};margin-right:4px">{pct:.0f}%</span>'


def _render_token_html(token_usage: dict) -> str:
    """Per-module token usage (input <ctx%> / output) + turn total + estimated cost. The small
    % after each module's input is that prompt's share of the 128k context window."""
    ti, to = _sum_tokens(token_usage)
    if ti == 0 and to == 0:
        return ""
    sep = '<span style="color:#cbd5e1;margin:0 3px">/</span>'  # muted divider, recedes
    parts = []
    for name, tu in token_usage.items():
        mi, mo = int(tu.get("input", 0)), int(tu.get("output", 0))
        if mi == 0 and mo == 0:
            continue
        tc = TIMING_COLORS.get(name, "#94a3b8")
        parts.append(
            f'<span style="margin-right:12px;white-space:nowrap">'
            f'<span style="display:inline-block;width:7px;height:7px;border-radius:2px;background:{tc};margin-right:3px"></span>'
            f'{name} {_ctx_pct_tag(mi)}{_fmt_tokens(mi)}{sep}{_fmt_tokens(mo)}</span>'
        )
    return (
        f'<div style="display:flex;flex-wrap:wrap;gap:6px;font-size:11px;color:#94a3b8;margin-top:3px">'
        f'{"".join(parts)}'
        f'<span style="font-weight:600;color:#475569">Σ tok in {_fmt_tokens(ti)} · out {_fmt_tokens(to)} · ≈{pricing_currency()}{_token_cost(token_usage):.4f}</span>'
        f'</div>'
    )


def _gap_seconds(prev_iso: str, cur_iso: str) -> float | None:
    """Seconds between two ISO timestamps, or None if either is unparseable."""
    if not prev_iso or not cur_iso:
        return None
    try:
        return (datetime.fromisoformat(cur_iso) - datetime.fromisoformat(prev_iso)).total_seconds()
    except ValueError:
        return None


def _render_step_detail(step: ReportStep, detail_id: str, prev_timestamp: str = "") -> str:
    """Render the expandable detail panel for a step.

    prev_timestamp: the previous action's timestamp, to show the inter-action gap.
    """
    at_cls = f"at-{step.action_type}"
    at_label = AT_LABELS.get(step.action_type, step.action_type)

    # Status
    if "✓" in step.status:
        status_cls, status_text = "thumb-status-ok", "✓"
    elif "✗" in step.status:
        status_cls, status_text = "thumb-status-fail", "✗"
    else:
        status_cls, status_text = "thumb-status-skip", "—"

    action_detail = _safe(step.description)
    if step.action_text:
        action_detail += f' <span style="color:#a032c8">「{_safe(step.action_text[:30])}」</span>'
    if step.action_direction:
        dir_label = {"up": "↑", "down": "↓", "left": "←", "right": "→"}.get(step.action_direction, step.action_direction)
        action_detail += f' <span style="color:#32b432">{dir_label}</span>'

    instruction_html = ""
    if step.instruction:
        instruction_html = f'<div class="detail-instruction">指令：{_safe(step.instruction)}</div>'
    summary_html = ""
    if step.summary and step.summary != step.description:
        summary_html = f'<div class="detail-summary">{_safe(step.summary)}</div>'

    snap_html = ""
    if step.snap and step.snap.get("original"):
        method = step.snap.get("method", "?").upper()
        orig = step.snap["original"]
        snapped = step.snap.get("snapped")
        snap_color = "#22d3ee" if method == "DOM" else "#f59e0b" if method == "YOLO" else "#22c55e"
        dist = ""
        if snapped:
            d = ((orig[0] - snapped[0]) ** 2 + (orig[1] - snapped[1]) ** 2) ** 0.5
            dist = f' · <span style="color:{snap_color}">距离 {d:.1f}</span>'
        snap_html = (
            f'<div class="detail-instruction">'
            f'<span style="color:{snap_color};font-weight:600">{method}</span> 吸附 '
            f'({orig[0]:.0f},{orig[1]:.0f})→({snapped[0]:.0f},{snapped[1]:.0f})'
            f'{dist}</div>'
        )

    # Progressive knowledge injected into the planner this turn. cyan = sections injected;
    # amber = the planner ran and the checker flagged sections, but none matched (a fuzzy-match
    # miss worth surfacing). Silent when no planner ran (replan/done turns) or no knowledge.
    sections_html = ""
    loaded = step.sections_loaded
    requested = step.relevant_sections
    if loaded:
        names = "、".join(_safe(s.replace("_", " ")) for s in loaded)
        extra = (
            f' <span style="color:#94a3b8">(checker 请求 {len(requested)})</span>'
            if requested and len(requested) != len(loaded) else ""
        )
        sections_html = (
            f'<div class="detail-instruction">'
            f'<span style="color:#0891b2;font-weight:600">📖 注入知识 {len(loaded)} 章节</span>：{names}{extra}'
            f'</div>'
        )
    elif requested and "planner" in (step.timings or {}):
        names = "、".join(_safe(s.replace("_", " ")) for s in requested)
        sections_html = (
            f'<div class="detail-instruction">'
            f'<span style="color:#f59e0b;font-weight:600">📖 checker 请求 {len(requested)} 章节但未命中</span>：{names}'
            f'</div>'
        )

    ss_html = ""
    if step.annotated_before_url:
        ss_html = f'<div class="detail-ss"><img src="{step.annotated_before_url}" onclick="zoomImg(this.src)" alt="Turn"></div>'

    # Absolute wall-clock time the action executed (PolicyTurn.timestamp, captured
    # right after dispatch). Show the time-of-day prominently, full ISO on hover.
    time_html = ""
    if step.timestamp:
        ts = step.timestamp
        disp = ts.split("T", 1)[1] if "T" in ts else ts
        gap = _gap_seconds(prev_timestamp, ts)
        gap_html = f'<span class="detail-gap" title="距上一个动作">+{gap:.0f}s</span>' if gap is not None else ""
        time_html = f'<span class="detail-time" title="{_safe(ts)}">🕒 {_safe(disp)}</span>{gap_html}'

    return f"""
    <div class="detail" id="{detail_id}">
      {ss_html}
      <div class="detail-info">
        <div class="detail-top">
          <span class="detail-idx {at_cls}">{step.label.split()[-1]}</span>
          <span class="detail-at" style="background:#f1f5f9;color:#475569">{at_label}</span>
          <span class="detail-desc">{action_detail}</span>
          {time_html}
          <span class="detail-status {status_cls.replace('thumb-status', 'detail-status')}">{step.status}</span>
        </div>
        {instruction_html}
        {snap_html}
        {sections_html}
        {summary_html}
        {_render_timing_html(step.timings)}
        {_render_token_html(step.token_usage)}
      </div>
    </div>"""


def _render_knowledge_html(k: dict, sections: list[dict] | None = None) -> str:
    """Sidebar section: which app knowledge was injected this run, how big it was, and the
    list of sections actually injected (≥1 turn) — each clickable to view its body in a modal.
    Per-turn injection is also marked on each turn card (📖). Empty when no knowledge matched."""
    if not k:
        return ""
    app = _safe(str(k.get("app_name", "")))
    sc = k.get("section_count", 0)
    nav = k.get("nav_chars", 0)
    el = k.get("elements_chars", 0)
    _k = lambda n: f"{n / 1000:.1f}k" if n >= 1000 else str(n)  # noqa: E731
    out = [
        '<div class="sidebar-knowledge">',
        '<div class="sidebar-title">知识库</div>',
        f'<div class="sk-app">{app}</div>',
        f'<div class="sk-meta">{sc} 章节 · 导航 {_k(nav)} / 元素 {_k(el)} 字</div>',
    ]
    sections = sections or []
    if sections:
        out.append(f'<div class="sk-sections-label">已加载章节 · {len(sections)}</div>')
        bodies = []
        for i, s in enumerate(sections):
            title = _safe(str(s.get("title", "")))
            # this.textContent passes the title to the modal — no JS string escaping needed.
            out.append(
                f'<div class="sk-item" onclick="showSection(\'sk-body-{i}\', this.textContent)">'
                f'{title}</div>'
            )
            body = _safe(str(s.get("body") or "（未找到该章节的知识文件）"))
            bodies.append(f'<div id="sk-body-{i}">{body}</div>')
        out.append('<div id="sk-bodies" style="display:none">' + "".join(bodies) + "</div>")
    out.append("</div>")
    return "".join(out)


def _render_platform_badge(platform: str) -> str:
    """Small colored badge next to the title showing the run platform. Empty for
    old logs that predate the context.platform field."""
    if not platform:
        return ""
    icon = {"iphone": "📱", "browser": "🌐", "android": "🤖"}.get(platform, "🖥")
    label = {"iphone": "iPhone", "browser": "Browser", "android": "Android"}.get(
        platform, _safe(platform)
    )
    return (
        '<span class="platform-badge" style="display:inline-block;margin-left:10px;'
        "padding:2px 11px;border-radius:11px;background:#2f81f7;color:#fff;"
        'font-size:13px;font-weight:600;vertical-align:middle;">'
        f"{icon} {label}</span>"
    )


def _render_provenance(raw_input: str, goal: str, router: dict) -> str:
    """Header row showing the Router/input-resolution result for the run.

    Always rendered (when raw_input is known) so every report states the router
    status explicitly — router goal, a clarification request, a deterministic
    temporal rewrite, or '未经 router' for bin/runner direct runs. The <h1> title
    is the raw input; this row shows what it resolved to and how.
    """
    if not raw_input:
        return ""  # old logs without provenance — nothing to show

    changed = bool(goal) and goal != raw_input
    if router and router.get("needs_clarification"):
        via_cls, via_text = "prov-via-clarify", "需澄清"
        body = f'需要澄清：{_safe(router.get("clarification", "") or "—")}'
    elif router:
        via_cls, via_text = "prov-via-router", "router"
        body = (
            f'<span class="prov-goal">{_safe(goal)}</span>' if not changed else
            f'{_safe(raw_input)}<span class="prov-arrow">→</span>'
            f'<span class="prov-goal">{_safe(goal)}</span>'
        )
    elif changed:
        via_cls, via_text = "prov-via-temporal", "temporal · 未经 router"
        body = (
            f'{_safe(raw_input)}<span class="prov-arrow">→</span>'
            f'<span class="prov-goal">{_safe(goal)}</span>'
        )
    else:
        via_cls, via_text = "prov-via-none", "未经 router"
        body = f'<span class="prov-goal">{_safe(goal or raw_input)}</span>（输入未改写）'

    return (
        f'<div class="decompose">'
        f'<span class="decompose-label">Router</span>'
        f'{body}'
        f'<span class="prov-via {via_cls}">{via_text}</span>'
        f'</div>'
    )


def generate_html(data: ReportData, grid: bool = False) -> str:
    stats_parts = [f"{k}: {v}" for k, v in data.stats.items()]
    llm_s = sum(m.get("total_time", 0) for m in data.milestones)  # Σ LLM-module timings
    if data.wall_clock_s:
        # True end-to-end elapsed, split into LLM compute, settle waits, and "other"
        # (perception / action execution / scheduling overhead = wall − LLM − settle).
        other_s = max(0.0, data.wall_clock_s - llm_s - data.settle_s_total)
        stats_parts.append(
            f"elapsed: {data.wall_clock_s:.1f}s "
            f"(LLM {llm_s:.1f} · settle {data.settle_s_total:.1f} · other {other_s:.1f})"
        )
    else:
        stats_parts.append(f"LLM: {llm_s:.1f}s")  # old logs without wall_clock_s
    sess_in = sum(int(m.get("input_tokens", 0)) for m in data.milestones)
    sess_out = sum(int(m.get("output_tokens", 0)) for m in data.milestones)
    sess_cost = sum(float(m.get("cost", 0)) for m in data.milestones)
    if sess_in or sess_out:
        stats_parts.append(
            f"tokens: in {_fmt_tokens(sess_in)} / out {_fmt_tokens(sess_out)}"
            f"  |  cost: ≈{pricing_currency()}{sess_cost:.4f}"
        )
    stats_str = "  |  ".join(stats_parts)

    # Display ordinal per milestone (#1, #2, …). Milestone ids are descriptive slugs now
    # (e.g. 'navigate_to_riot_app'), which _short_mid can't shorten — showing the raw slug
    # crowds the name out of the sidebar. The ordinal is always short; the raw id stays for
    # the anchor. Falls back to _short_mid for ids not in the milestone list (e.g. _no_milestone).
    _mid_ordinal = {m.get("id", ""): i for i, m in enumerate(data.milestones, 1)}

    def _mid_disp(mid: str) -> str:
        return str(_mid_ordinal[mid]) if mid in _mid_ordinal else _short_mid(mid)

    # Sidebar outline (子目标分解): one clickable node per milestone, scroll-spy active.
    outline_parts = []
    for m in data.milestones:
        mid = _safe(m.get("id", "?"))           # full id — for the anchor/link
        mid_disp = _safe(_mid_disp(m.get("id", "?")))  # ordinal — for display
        name = _safe(m.get("name", ""))
        kind = m.get("kind", "")
        kind_safe = _safe(kind)
        turns = m.get("turns", "")
        t = m.get("total_time", 0)
        badge_cls = KIND_BADGE.get(kind, "milestone-badge-default")
        meta_bits = ""
        if kind_safe:
            meta_bits += f'<span class="milestone-badge {badge_cls}">{kind_safe}</span>'
        meta_bits += f'<span>{t:.1f}s</span>'
        if turns:
            meta_bits += f'<span>T{_safe(str(turns))}</span>'
        outline_parts.append(
            f'<a class="outline-item" href="#ms-{mid}" data-target="ms-{mid}">'
            f'<span class="outline-top">'
            f'<span class="outline-id">#{mid_disp}</span>'
            f'<span class="outline-name">{name or kind_safe}</span>'
            f'</span>'
            f'<span class="outline-meta">{meta_bits}</span>'
            f'</a>'
        )
    outline_html = "".join(outline_parts) or '<div class="sidebar-empty">无子目标</div>'

    # Per-milestone sections
    pages_html = ""
    prev_ts = ""  # carries across pages so the gap is vs the previous turn globally
    for page in data.pages:
        badge_cls = KIND_BADGE.get(page.milestone_kind, "milestone-badge-default")
        ms_time = sum(sum(s.timings.values()) for s in page.steps)
        ms_in = sum(_sum_tokens(s.token_usage)[0] for s in page.steps)
        ms_out = sum(_sum_tokens(s.token_usage)[1] for s in page.steps)
        ms_cost = sum(_token_cost(s.token_usage) for s in page.steps)
        ms_tok_html = (
            f' · {_fmt_tokens(ms_in)}/{_fmt_tokens(ms_out)} tok · ≈{pricing_currency()}{ms_cost:.4f}'
            if (ms_in or ms_out) else ""
        )
        mid_safe = _safe(page.milestone_id)       # full id — anchor target
        mid_disp = _safe(_mid_disp(page.milestone_id))  # ordinal — heading

        thumbs_html = ""
        details_html = ""
        for si, step in enumerate(page.steps):
            turn_no = step.label.split()[-1]
            detail_id = f"detail-ms{mid_safe}-s{si}"

            # Status indicator
            if "✓" in step.status:
                status_cls, status_text = "thumb-status-ok", "✓"
            elif "✗" in step.status:
                status_cls, status_text = "thumb-status-fail", "✗"
            else:
                status_cls, status_text = "thumb-status-skip", "—"

            at_cls = f"at-{step.action_type}"
            at_label = AT_LABELS.get(step.action_type, "")

            # Thumbnail
            if step.annotated_before_url:
                thumbs_html += (
                    f'<div class="thumb" data-detail="{detail_id}" onclick="showDetail(\'{detail_id}\')">'
                    f'<img src="{step.annotated_before_url}" alt="Turn {turn_no}">'
                    f'<div class="thumb-action {at_cls}" title="{at_label}"></div>'
                    f'<div class="thumb-status {status_cls}">{status_text}</div>'
                    f'<div class="thumb-label">T{turn_no} · {at_label}</div>'
                    f'</div>'
                )

            # Detail panel (hidden until clicked)
            details_html += _render_step_detail(step, detail_id, prev_timestamp=prev_ts)
            if step.timestamp:
                prev_ts = step.timestamp

        desc_html = f'<div class="milestone-desc">{_safe(page.milestone_description)}</div>' if page.milestone_description else ""
        sc_html = f'<div class="milestone-sc">验收：{_safe(page.success_condition)}</div>' if page.success_condition else ""
        verify_thumb = ""
        verify_detail = ""
        if page.verify_url:
            vd_id = f"detail-ms{mid_safe}-verify"
            verify_thumb = (
                f'<div class="thumb" data-detail="{vd_id}" onclick="showDetail(\'{vd_id}\')" style="border-color:#22c55e40">'
                f'<img src="{page.verify_url}" alt="验收截图">'
                f'<div class="thumb-status thumb-status-ok">✓</div>'
                f'<div class="thumb-label" style="background:linear-gradient(transparent, rgba(34,197,94,0.7))">验收</div>'
                f'</div>'
            )
            ck = page.verify_checker
            ck_status = ck.get("status", "")
            ck_reason = ck.get("reason", "")
            ck_identity = ck.get("page_identity", "")
            ck_summary = ck.get("summary", "")
            if ck_status == "done":
                badge = '<span style="background:#dcfce7;color:#166534;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">✓ done</span>'
            elif ck_status == "in_progress":
                badge = '<span style="background:#fef9c3;color:#854d0e;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">⏳ in_progress</span>'
            else:
                badge = ""
            identity_html = f'<span style="font-size:11px;color:#64748b;margin-left:6px">{_safe(ck_identity)}</span>' if ck_identity else ""
            reason_html = f'<div class="detail-instruction" style="margin-top:4px">{_safe(ck_reason)}</div>' if ck_reason else ""
            summary_html = f'<div class="detail-summary">{_safe(ck_summary)}</div>' if ck_summary and ck_summary != ck_reason else ""
            verify_detail = (
                f'<div class="detail" id="{vd_id}">'
                f'<div class="detail-ss"><img src="{page.verify_url}" onclick="zoomImg(this.src)" alt="验收截图"></div>'
                f'<div class="detail-info">'
                f'<div class="detail-top" style="flex-wrap:wrap;gap:8px">'
                f'<span style="font-size:11px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.04em">验收结果</span>'
                f'{badge}{identity_html}'
                f'</div>'
                f'{reason_html}'
                f'{summary_html}'
                f'</div>'
                f'</div>'
            )
        pages_html += f"""
        <div class="milestone" id="ms-{mid_safe}">
          <div class="milestone-header">
            <h2>#{mid_disp}</h2>
            <span class="milestone-name">{_safe(page.milestone_name)}</span>
            <span class="milestone-badge {badge_cls}">{_safe(page.milestone_kind)}</span>
            <span class="milestone-time">{ms_time:.1f}s · {len(page.steps)} turns{ms_tok_html}</span>
            {desc_html}
            {sc_html}
          </div>
          <div class="gallery">{thumbs_html}{verify_thumb}</div>
          {details_html}
          {verify_detail}
        </div>"""

    # Model-config box with an inline "参考单价" chip that pops the rate table on hover.
    cost_note_html = ""
    if data.models or sess_in or sess_out:
        ccy = _safe(pricing_currency())

        # 参考单价 hover chip: rate table for the cost-contributing models.
        price_chip = ""
        if sess_in or sess_out:
            models_seen: dict[str, tuple[float, float]] = {}
            for k in ("supervisor", "supervisor.decompose", "action_policy"):
                mdl = (data.models or {}).get(k)
                if mdl and mdl not in models_seen:
                    models_seen[mdl] = model_price(mdl)
            if not models_seen:
                models_seen["default"] = model_price("")
            rows = "".join(
                f'<tr><td>{_safe(m)}</td>'
                f'<td class="pp-num">{pi}</td><td class="pp-num">{po}</td></tr>'
                for m, (pi, po) in models_seen.items()
            )
            price_chip = (
                f'<span class="price-tip"><span class="price-chip">参考单价 ⓘ</span>'
                f'<div class="price-pop">'
                f'<div style="color:#94a3b8;margin-bottom:4px">{ccy} / 百万 token</div>'
                f'<table><tr class="pp-head"><td>模型</td><td class="pp-num">输入</td>'
                f'<td class="pp-num">输出</td></tr>{rows}</table></div></span>'
            )

        # 当前模型配置：group config keys by the model they used; price chip rides along.
        if data.models:
            grouped: dict[str, list[str]] = {}
            for key, mdl in data.models.items():
                if mdl:
                    grouped.setdefault(mdl, []).append(key.replace("supervisor.", ""))
            parts = " · ".join(
                f'{_safe(m)}<span style="color:#94a3b8">（{_safe(", ".join(keys))}）</span>'
                for m, keys in grouped.items()
            )
            cost_note_html = (
                f'<div class="decompose"><span class="decompose-label">模型配置</span>'
                f'{parts}{price_chip}</div>'
            )
        elif price_chip:
            cost_note_html = f'<div class="decompose">{price_chip}</div>'

    result_html = ""
    if data.output:
        result_html = (
            f'<div class="result-card">'
            f'<div class="result-label">最终输出</div>'
            f'<div class="result-body">{_safe(data.output)}</div>'
            f'</div>'
        )

    return HTML_TEMPLATE.format(
        title=_safe(data.title),
        platform_badge=_render_platform_badge(data.platform),
        stats=stats_str,
        provenance_html=_render_provenance(data.raw_input, data.goal, data.router),
        outline_html=outline_html,
        cost_note_html=cost_note_html,
        knowledge_html=_render_knowledge_html(data.knowledge, data.knowledge_sections),
        result_html=result_html,
        pages_html=pages_html,
    )


def save_report(data: ReportData, output_path: Path, grid: bool = False) -> Path:
    html = generate_html(data, grid=grid)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def save_recon_report(data: AppReconData, output_path: Path) -> Path:
    html = generate_recon_html(data)
    output_path.write_text(html, encoding="utf-8")
    return output_path
