"""Browser v2 complete-page-read primitive.

Two paths, same interface:
  1. **Semantic tree** (``obs.semantic_tree``): scroll-free, DOM-exact, browser-only.
  2. **Visual scroll loop**: scroll-observe-read, works on all platforms, used as
     fallback when the semantic tree is absent or returns None (non-semantic DOM,
     canvas, shadow-DOM, virtual lists, iPhone/Android).

Callers use ``read_page_complete``.  The two inner paths are exposed separately
for testing and for future individual wiring.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from hashlib import sha1
from pathlib import Path
from typing import Any

from gui_agent.core.runtime.traversal import TraversalSession, window_from_signal

# How much SSIM similarity (1−distance) constitutes "frozen frame" — page didn't
# scroll.  Must be > CHANGE_SSIM_DIST_THR (0.08) complement: 1−0.08 = 0.92 is
# "barely moved"; 0.97 is "essentially the same frame" (sticky header noise
# contributes ≤0.10 of the frame area, so a true scroll always drops below 0.97).
_FREEZE_SSIM_THR = 0.97
_DEFAULT_MAX_SCROLLS = 12
_POST_SCROLL_SETTLE_S = 0.4


def _frame_similarity(a: bytes, b: bytes) -> float:
    """Return SSIM similarity (0–1) between two PNG frames, resized to 160×320 gray."""
    import io

    import numpy as np
    from PIL import Image
    from skimage.metrics import structural_similarity as ssim

    size = (160, 320)
    ga = np.array(Image.open(io.BytesIO(a)).convert("L").resize(size))
    gb = np.array(Image.open(io.BytesIO(b)).convert("L").resize(size))
    dist = 1.0 - float(ssim(ga, gb, data_range=255))
    return 1.0 - dist  # return similarity, not distance


def _browser_scroll_signal(client: Any) -> dict[str, Any] | None:
    """Read the document scroll window as adapter evidence for a traversal session."""
    cdp = getattr(client, "_cdp_send", None)
    if not callable(cdp):
        return None
    try:
        res = cdp(
            "Runtime.evaluate",
            {
                "expression": (
                    "({top:window.scrollY, viewport:window.innerHeight, "
                    "extent:document.documentElement.scrollHeight})"
                ),
                "returnByValue": True,
            },
        )
        value = (res.get("result") or {}).get("value")
        if not isinstance(value, dict):
            return None
        top = float(value.get("top") or 0)
        viewport = float(value.get("viewport") or 0)
        extent = float(value.get("extent") or 0)
        at_end = top + viewport >= extent - 2
        return {
            "type": "scroll",
            "scroll_top": top,
            "at_scroll_start": top <= 0,
            "at_scroll_end": at_end,
            "can_scroll_more": not at_end,
            "can_scroll_back": top > 0,
        }
    except Exception:
        return None


def _browser_scroll_step(client: Any) -> tuple[int, int, int]:
    """Return (cx, cy, step_px) for one viewport-height scroll on browser.

    cx/cy  = viewport centre in CSS px (where to place the mouse so the wheel
             lands in the main scroll container, not a sidebar).
    step_px = viewport height (scroll exactly one screen per step, not a
              magic wheel-amount multiplied by some constant).
    Falls back to reasonable defaults if geometry cannot be read.
    """
    # viewport_size is a @property on PlaywrightDevice, not a plain method.
    try:
        vp = client.viewport_size  # property access — already a (w, h) tuple
        if isinstance(vp, tuple) and len(vp) == 2:
            w, h = int(vp[0]), int(vp[1])
            if w and h:
                return w // 2, h // 2, h
    except Exception:
        pass
    return 640, 400, 800  # safe fallback


def scroll_until_read(
    bundle: Any,
    platform: Any,
    log_dir: Path,
    returns: list[str],
    read_spec: str = "",
    check_knowledge: str = "",
    prepare_vision_prompt_png: Callable[[bytes], bytes] | None = None,
    context_reports: list[dict] | None = None,
    max_scrolls: int = _DEFAULT_MAX_SCROLLS,
) -> dict[str, str]:
    """Visual scroll loop: scroll-observe-read until all fields are found or boundary.

    Browser path (``client`` has ``viewport_size`` + ``_cdp_send``):
      - Scroll step  = one viewport height, at viewport centre.
      - Boundary     = DOM ``scrollY + innerHeight >= scrollHeight`` (exact, no SSIM).

    iPhone/Android fallback (no DOM geometry):
      - Scroll step  = ``amount=5`` wheel steps at a sensible centre estimate.
      - Boundary     = pixel-freeze (SSIM ≥ 0.97 between consecutive frames).

    Returns the best ``{field: value}`` found (partial if boundary hit first).
    Never raises.
    """
    from gui_agent.core.orchestrator.primitives.structured_read import structured_read

    client = getattr(platform, "client", None)
    is_browser = client is not None and hasattr(client, "viewport_size")

    traversal = TraversalSession(
        "visual-read",
        coverage="from_current",
        boundary_status="exhausted",
        no_progress_status="exhausted",
        max_moves=max_scrolls,
    )
    previous_png: bytes | None = None
    previous_content_key = ""
    reads: dict[str, str] = {}

    for scroll_i in range(max_scrolls + 2):
        obs_url = f"scroll_read_{scroll_i}.png"
        try:
            obs = bundle.make_perception(platform, log_dir / obs_url).observe()
        except Exception:
            break
        png = obs.png_bytes

        # Vision read on the current frame.
        try:
            reads = structured_read(
                png, returns,
                read_spec=read_spec,
                check_knowledge=check_knowledge,
                prepare_vision_prompt_png=prepare_vision_prompt_png,
                context_reports=context_reports,
            )
        except Exception:
            reads = {}

        if all(reads.get(f, "").strip() for f in returns):
            return reads

        content_key = sha1(png).hexdigest() if png else ""
        if not is_browser and previous_png is not None:
            try:
                if _frame_similarity(previous_png, png) >= _FREEZE_SSIM_THR:
                    content_key = previous_content_key
            except Exception:
                pass

        signal = _browser_scroll_signal(client) if is_browser else None
        if signal is None and isinstance(obs.viewport, dict):
            signal = obs.viewport
        if signal is None:
            signal = {"type": "scroll", "can_scroll_more": True, "at_scroll_end": False}
        window = window_from_signal(
            signal,
            surface_id=f"visual-read:{id(platform)}",
            content_key=content_key,
        )
        if window is None:
            break
        decision = traversal.observe(window)
        previous_png = png
        previous_content_key = content_key
        if decision.action in {"done", "exhausted", "ambiguous"}:
            break
        if decision.action == "wait":
            continue

        direction = "down" if decision.action == "move_forward" else "up"
        scroll_fn = getattr(client, "scroll", None) if client is not None else None
        if not callable(scroll_fn):
            break
        try:
            if is_browser:
                cx, cy, step_px = _browser_scroll_step(client)
                # Convert viewport-height pixels to the device's amount units.
                # device.scroll multiplies amount * _SCROLL_PX_PER_AMOUNT; pass
                # step_px directly via a JS window.scrollBy instead to avoid the
                # unit mismatch.
                cdp = getattr(client, "_cdp_send", None)
                if callable(cdp):
                    delta = step_px if direction == "down" else -step_px
                    cdp(
                        "Runtime.evaluate",
                        {
                            "expression": f"window.scrollBy({{top:{delta},behavior:'instant'}})",
                            "returnByValue": True,
                        },
                    )
                else:
                    scroll_fn(direction, amount=5, x=cx, y=cy)
            else:
                scroll_fn(direction, amount=5, x=640, y=400)
            time.sleep(_POST_SCROLL_SETTLE_S)
        except Exception:
            break

    return reads


def _read_from_form_controls(
    form_controls: list[dict],
    returns: list[str],
    read_spec: str = "",
    *,
    require_all: bool = True,
) -> dict[str, str] | None:
    """Match ``returns`` field names against form_controls labels → current DOM values.

    By default returns a complete ``{field: value}`` dict only when ALL fields are found.
    With ``require_all=False``, returns the partial DOM reads and lets the caller send only
    missing fields to AX/vision fallback.

    ``selected_text`` is preferred over ``value`` for native selects (human-readable
    option text vs the numeric option id stored in ``value``).
    """
    from difflib import SequenceMatcher

    from gui_agent.adapters.browser.semantic_page import _normalize
    from gui_agent.core.orchestrator.primitives.structured_read import read_spec_label_candidates

    result: dict[str, str] = {}
    for field in returns:
        candidates = [field]
        for candidate in read_spec_label_candidates(read_spec, field):
            if candidate not in candidates:
                candidates.append(candidate)
        best: dict | None = None
        best_score = 0.0
        for candidate in candidates:
            nf = _normalize(candidate)
            if not nf:
                continue
            for fc in form_controls:
                label = fc.get("label") or fc.get("name") or fc.get("id") or ""
                nl = _normalize(label)
                if not nl:
                    continue
                if nl == nf:
                    best = fc
                    best_score = 1.0
                    break
                score = SequenceMatcher(None, nf, nl).ratio()
                if score >= 0.75 and score > best_score:
                    best = fc
                    best_score = score
            if best_score >= 1.0:
                break
        if best is None:
            if require_all:
                return None
            continue
        if (best.get("kind") or "") == "native_select":
            # The SELECTED option text is authoritative for a <select>/<select multiple> (WebArena
            # task 185 Material). The primary value is the FIRST selected option — never the numeric
            # option id in `value`, and never the first *listed* option (the WS08 vision misread that
            # returned Burlap instead of the selected Cotton). selected_text joins all selected with
            # ", "; an empty selection reads "" so callers can branch/fail honestly instead of
            # guessing.
            selected = best.get("selected_text") or ""
            value = selected.split(",")[0].strip() if selected else ""
        else:
            value = best.get("selected_text") or best.get("value") or ""
        result[field] = str(value)
    return result


def read_page_complete(
    obs: Any,
    returns: list[str],
    read_spec: str = "",
    check_knowledge: str = "",
    prepare_vision_prompt_png: Callable[[bytes], bytes] | None = None,
    bundle: Any = None,
    platform: Any = None,
    log_dir: Path | None = None,
    context_reports: list[dict] | None = None,
    max_scrolls: int = _DEFAULT_MAX_SCROLLS,
) -> dict[str, str]:
    """Read ``returns`` fields from the current page, DOM path first, visual fallback.

    Priority:
      1. form_controls (``obs.form_controls``) — exact DOM values for standard
         input/select/textarea controls; scroll-free, no image processing.
      2. AX semantic tree (``obs.semantic_tree``) — for non-form structural content
         (table cells, headings, display text); scroll-free.
      3. Visual scroll loop — fallback for custom widgets (star ratings, pickers),
         non-semantic DOM, iPhone/Android (no DOM sensors on those platforms).
      4. Single-frame ``structured_read`` — if bundle/platform/log_dir not available.

    Paths 1 and 2 are browser-only (the fields are None on iPhone/Android) and
    silently skip when not populated, so callers never need to branch by platform.
    """
    if not returns:
        return {}

    # Path 1: form controls — exact DOM values for standard editable fields.
    dom_reads: dict[str, str] = {}
    form_controls = getattr(obs, "form_controls", None)
    if form_controls:
        dom_reads = _read_from_form_controls(
            form_controls,
            returns,
            read_spec=read_spec,
            require_all=False,
        ) or {}
        if all(field in dom_reads for field in returns):
            return dom_reads

    remaining = [field for field in returns if field not in dom_reads]

    # Path 2: AX semantic tree — non-form structural content (cells, headings).
    semantic_tree = getattr(obs, "semantic_tree", None)
    if semantic_tree:
        from gui_agent.adapters.browser.semantic_page import read_from_tree
        result = read_from_tree(semantic_tree, remaining, read_spec=read_spec)
        if result is not None:
            merged = {**result, **dom_reads}
            if all(field in merged for field in returns):
                return merged

    # Path 2: visual scroll loop.
    if bundle is not None and platform is not None and log_dir is not None:
        ppv = prepare_vision_prompt_png
        if ppv is None:
            ppv = getattr(bundle, "prepare_vision_prompt_png", None)
        visual_reads = scroll_until_read(
            bundle, platform, log_dir, remaining,
            read_spec=read_spec,
            check_knowledge=check_knowledge,
            prepare_vision_prompt_png=ppv,
            context_reports=context_reports,
            max_scrolls=max_scrolls,
        )
        return {**visual_reads, **dom_reads}

    # Path 3: single-frame vision fallback.
    from gui_agent.core.orchestrator.primitives.structured_read import structured_read
    ppv = prepare_vision_prompt_png
    if ppv is None:
        ppv = getattr(bundle, "prepare_vision_prompt_png", None)
    visual_reads = structured_read(
        obs.png_bytes, remaining,
        read_spec=read_spec,
        check_knowledge=check_knowledge,
        prepare_vision_prompt_png=ppv,
        context_reports=context_reports,
    )
    return {**visual_reads, **dom_reads}


_DEFAULT_MAX_PAGES = 20


def _move_bound_table(client: Any, table: dict[str, Any], direction: str) -> bool:
    """Move the pager/scroll container associated with ``table`` and no other surface."""

    cdp = getattr(client, "_cdp_send", None)
    path = str(table.get("path") or "").strip()
    traversal = table.get("traversal") if isinstance(table.get("traversal"), dict) else {}
    if not callable(cdp) or not path:
        return False
    expression = f"""(() => {{
      let surface;
      try {{ surface = document.querySelector({json.dumps(path)}); }} catch (_) {{ return false; }}
      if (!surface) return false;
      const disabled = (el) => !!(el && (
        el.disabled || el.matches('[disabled],[aria-disabled="true"],.disabled,[class*="disabled" i]')
      ));
      if ({json.dumps(traversal.get("type"))} === 'paged') {{
        const pagerSelector = [
          '.pager','.pagination','.pages','.page-numbers','.data-grid-paginator',
          '.admin__data-grid-pager','nav[aria-label*="pagination" i]'
        ].join(',');
        const buttonSelector = {json.dumps(direction)} === 'forward'
          ? '[aria-label*="next page" i],.next-page,.action-next,button[title*="Next" i],button[class*="next" i]'
          : '[aria-label*="previous page" i],.prev-page,.action-previous,button[title*="Previous" i],button[class*="previous" i]';
        for (let root = surface, depth = 0; root && depth < 7; root = root.parentElement, depth++) {{
          const pager = root.querySelector(pagerSelector);
          const button = pager && pager.querySelector(buttonSelector);
          if (button && !disabled(button)) {{ button.click(); return true; }}
        }}
        return false;
      }}
      for (let root = surface, depth = 0; root && depth < 6; root = root.parentElement, depth++) {{
        if (root.scrollHeight > root.clientHeight + 2) {{
          const delta = root.clientHeight * ({json.dumps(direction)} === 'forward' ? 1 : -1);
          root.scrollBy({{top: delta, behavior: 'instant'}});
          return true;
        }}
      }}
      window.scrollBy({{top: window.innerHeight * ({json.dumps(direction)} === 'forward' ? 1 : -1), behavior: 'instant'}});
      return true;
    }})()"""
    try:
        result = cdp("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        return bool(((result or {}).get("result") or {}).get("value"))
    except Exception:
        return False


def _single_paged_table(obs: Any, selected: dict[str, Any]) -> bool:
    tables = [table for table in getattr(obs, "tables", None) or [] if isinstance(table, dict)]
    paged = [
        table
        for table in tables
        if isinstance(table.get("traversal"), dict)
        and table["traversal"].get("type") == "paged"
    ]
    return len(paged) == 1 and paged[0] is selected


def _missing_grid_columns(rows: list[dict[str, str]], returns: list[str]) -> list[str]:
    """Declared ``returns`` columns that NO collected row carries a non-empty value for.

    A column absent from every row means the grid never rendered it and the AX extractor
    silently dropped the declared field (``read_grid_from_tree`` only emits keys for headers
    it matched). Distinct from a column that's present-but-blank in a row."""
    if not rows:
        return []
    return [f for f in returns if not any(str(r.get(f, "")).strip() for r in rows)]


def _settle_click(client: Any) -> None:
    """Best-effort settle after a non-navigation click (opening a panel / toggling a column)."""
    fn = getattr(client, "wait_settled", None)
    if callable(fn):
        try:
            fn("click")
        except Exception:  # noqa: BLE001
            pass


def _heal_missing_columns(
    obs: Any,
    returns: list[str],
    rows: list[dict[str, str]],
    *,
    bundle: Any,
    platform: Any,
    client: Any,
    log_dir: Path | None,
) -> tuple[Any, list[dict[str, str]]] | tuple[None, None]:
    """Browser self-heal for declared grid columns the page isn't currently rendering.

    When some ``returns`` columns are missing from the collected rows, open the grid's
    "Columns" visibility control, enable the missing column toggles, re-observe, and re-extract.
    Returns ``(healed_obs, healed_rows)`` when the re-read now covers strictly more of the
    missing columns; otherwise ``(None, None)`` to mean "leave the original collection as-is"
    (the platform-general safety net in the foreach runner then fails honestly). Bounded to a
    single heal attempt; zero work when nothing is missing. General to any grid/column."""
    from gui_agent.adapters.browser.semantic_page import (
        find_column_toggle_refs,
        find_columns_control_ref,
        read_grid_from_tree,
    )

    missing = _missing_grid_columns(rows, returns)
    if not missing or client is None or bundle is None or log_dir is None:
        return None, None
    tree = getattr(obs, "semantic_tree", None)
    if not tree:
        return None, None
    cols_ref = find_columns_control_ref(tree)
    if cols_ref is None:
        return None, None

    try:
        client.click_by_ref(cols_ref)
        _settle_click(client)
        panel_obs = bundle.make_perception(platform, log_dir / "columns_panel.png").observe()
    except Exception:  # noqa: BLE001
        return None, None

    panel_tree = getattr(panel_obs, "semantic_tree", None) or tree
    toggles = find_column_toggle_refs(panel_tree, missing)
    if not toggles:
        # Couldn't locate the toggles. The panel is a harmless overlay (we won't re-read), so
        # just bail — never click Cancel/Reset or page blanks (would revert / mis-navigate).
        return None, None

    # Enabling a column checkbox updates the grid live — no Apply button, and per the
    # Admin_grid_controls knowledge the panel needn't (and shouldn't) be closed: it's an overlay
    # and the grid <table> stays in the AX tree, so we read straight through it. NEVER re-click
    # the control to "close" (toggle behavior is unreliable) nor Cancel/Reset (would revert the
    # column we just enabled).
    for ref in toggles:
        try:
            client.click_by_ref(ref)
            _settle_click(client)
        except Exception:  # noqa: BLE001
            pass

    try:
        healed_obs = bundle.make_perception(platform, log_dir / "grid_healed.png").observe()
    except Exception:  # noqa: BLE001
        return None, None
    healed_tree = getattr(healed_obs, "semantic_tree", None)
    if not healed_tree:
        return None, None
    new_rows = read_grid_from_tree(healed_tree, returns)
    if new_rows is None:
        return None, None
    if len(_missing_grid_columns(new_rows, returns)) < len(missing):
        return healed_obs, new_rows
    return None, None


def read_grid_complete(
    obs: Any,
    returns: list[str],
    bundle: Any = None,
    platform: Any = None,
    log_dir: Path | None = None,
    max_pages: int = _DEFAULT_MAX_PAGES,
    limit: int | None = None,
) -> list[dict[str, str]] | None:
    """Code-level row-collection primitive for browser: extract all grid rows from the AX
    semantic tree, following pagination links without consuming interactive turns.

    Returns ``None`` when not applicable (no semantic tree, no matching table, no
    column match) — the caller should fall back to ``react_until_collected``.
    Returns an empty list when the table exists but contains no data rows.
    Returns the full collected row list otherwise.

    Single-page grids read all rows in one shot. Multi-window grids use the shared
    ``TraversalSession`` through ``ListTraversalRuntime`` and bind movement to the DOM table
    that produced the rows. A page-level scroll or another grid's pager cannot drive this
    collection. An AX pager ref is used only as a compatibility fallback when the selected
    table is the page's sole paginated surface.
    """
    from gui_agent.adapters.browser.semantic_page import (
        find_next_page_ref,
        find_prev_page_ref,
        read_grid_from_tree,
    )
    from gui_agent.core.orchestrator.traversal.list_runtime import ListTraversalRuntime

    semantic_tree = getattr(obs, "semantic_tree", None)
    rows = read_grid_from_tree(semantic_tree, returns) if semantic_tree else None
    if rows is None:
        # AX-tree path found no matching grid. Some plain DOM data-tables aren't exposed as
        # grids in the AX tree even though the DOM <table> scan (read_tables) captures them —
        # notably a drilled order/detail page's "Items Ordered" table. Fall back to the DOM
        # table snapshot projection before giving up. These detail tables don't paginate, so a
        # single-page projection is already complete (pagination below is AX-tree-driven and is
        # correctly skipped for this branch). rows_from_tables returns None when nothing matches
        # → caller still falls back to interactive react_until_collected.
        from gui_agent.core.orchestrator.traversal.list_runtime import rows_from_tables

        rows = rows_from_tables(getattr(obs, "tables", None), returns)
        if rows is None:
            return None

    # Self-heal: if the grid didn't render some declared columns (the AX extractor silently
    # drops unmatched fields), enable them via the grid's "Columns" control and re-read BEFORE
    # paginating, so every page is collected with the full column set. No-op (zero extra
    # observes) when all declared columns are already present.
    _client0 = getattr(platform, "client", None) if platform is not None else None
    _healed_obs, _healed_rows = _heal_missing_columns(
        obs, returns, rows, bundle=bundle, platform=platform, client=_client0, log_dir=log_dir,
    )
    if _healed_rows is not None:
        obs = _healed_obs
        rows = _healed_rows
        semantic_tree = getattr(obs, "semantic_tree", None)

    client = getattr(platform, "client", None) if platform is not None else None
    if client is None or bundle is None or log_dir is None or not getattr(obs, "tables", None):
        return rows  # semantic single-page path; no bound DOM surface is available

    runtime = ListTraversalRuntime(var="grid", returns=list(returns), limit=limit)
    for move_n in range(max_pages + 1):
        decision = runtime.update(obs)
        if decision.action == "done":
            return runtime.rows[:limit] if limit else runtime.rows
        if decision.action in {"fallback", "schema_mismatch"}:
            return None

        table = runtime.current_table
        if table is None:
            return None
        moved = decision.action == "wait"
        if decision.action in {"paginate_next", "paginate_prev", "scroll_down"}:
            direction = "backward" if decision.action == "paginate_prev" else "forward"
            moved = _move_bound_table(client, table, direction)
            # Compatibility fallback for an adapter that exposes AX refs but no DOM-eval path.
            # It is safe only when the selected table is the sole paginated surface.
            if not moved and decision.action.startswith("paginate") and _single_paged_table(obs, table):
                ref = (
                    find_prev_page_ref(getattr(obs, "semantic_tree", None) or [])
                    if direction == "backward"
                    else find_next_page_ref(getattr(obs, "semantic_tree", None) or [])
                )
                if ref is not None:
                    try:
                        client.click_by_ref(ref)
                        moved = True
                    except Exception:
                        pass
        if not moved:
            return None

        settle = getattr(client, "wait_settled", None)
        if callable(settle):
            try:
                settle("navigate" if decision.action.startswith("paginate") else "scroll")
            except Exception:
                pass
        try:
            obs = bundle.make_perception(
                platform,
                log_dir / f"grid_window_{move_n + 2}.png",
            ).observe()
        except Exception:
            return None

    return None
