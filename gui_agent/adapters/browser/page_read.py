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

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

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


def _browser_at_scroll_end(client: Any) -> bool:
    """True when the page cannot scroll further down (DOM-exact, no image processing).

    Uses ``window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2``
    (2 px tolerance for sub-pixel rounding).  Falls back to False on any error so
    the scroll loop continues rather than stopping too early.
    """
    cdp = getattr(client, "_cdp_send", None)
    if not callable(cdp):
        return False
    try:
        res = cdp(
            "Runtime.evaluate",
            {
                "expression": (
                    "window.scrollY + window.innerHeight"
                    " >= document.documentElement.scrollHeight - 2"
                ),
                "returnByValue": True,
            },
        )
        return bool((res.get("result") or {}).get("value"))
    except Exception:
        return False


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
    from gui_agent.core.orchestrator.structured_read import structured_read

    client = getattr(platform, "client", None)
    is_browser = client is not None and hasattr(client, "viewport_size")

    prev_png: bytes | None = None
    reads: dict[str, str] = {}

    for scroll_i in range(max_scrolls + 1):
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

        # Boundary check.
        at_end = False
        if is_browser:
            at_end = _browser_at_scroll_end(client)
        else:
            # iPhone/Android: pixel-freeze via SSIM.
            if obs.viewport is not None:
                at_end = bool(obs.viewport.get("at_scroll_end"))
            if not at_end and prev_png is not None:
                try:
                    at_end = _frame_similarity(prev_png, png) >= _FREEZE_SSIM_THR
                except Exception:
                    pass

        if at_end:
            break

        # Scroll one step.
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
                    cdp(
                        "Runtime.evaluate",
                        {
                            "expression": f"window.scrollBy({{top:{step_px},behavior:'instant'}})",
                            "returnByValue": True,
                        },
                    )
                else:
                    scroll_fn("down", amount=5, x=cx, y=cy)
            else:
                scroll_fn("down", amount=5, x=640, y=400)
            time.sleep(_POST_SCROLL_SETTLE_S)
        except Exception:
            break

        prev_png = png

    return reads


def _read_from_form_controls(
    form_controls: list[dict],
    returns: list[str],
) -> dict[str, str] | None:
    """Match ``returns`` field names against form_controls labels → current DOM values.

    Returns a complete ``{field: value}`` dict only when ALL fields are found.
    Returns ``None`` if any field has no matching form control (caller falls through
    to the AX tree or visual path).

    ``selected_text`` is preferred over ``value`` for native selects (human-readable
    option text vs the numeric option id stored in ``value``).
    """
    from difflib import SequenceMatcher

    from gui_agent.adapters.browser.semantic_page import _normalize

    result: dict[str, str] = {}
    for field in returns:
        nf = _normalize(field)
        best: dict | None = None
        best_score = 0.0
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
        if best is None:
            return None
        if (best.get("kind") or "") == "native_select":
            # The SELECTED option text is authoritative for a <select>/<select multiple> (WebArena
            # task 185 Material). The primary value is the FIRST selected option — never the numeric
            # option id in `value`, and never the first *listed* option (the WS08 vision misread that
            # returned Burlap instead of the selected Cotton). selected_text joins all selected with
            # ", "; an empty selection reads "" so the caller re-locates instead of guessing.
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
    form_controls = getattr(obs, "form_controls", None)
    if form_controls:
        result = _read_from_form_controls(form_controls, returns)
        if result is not None:
            return result

    # Path 2: AX semantic tree — non-form structural content (cells, headings).
    semantic_tree = getattr(obs, "semantic_tree", None)
    if semantic_tree:
        from gui_agent.adapters.browser.semantic_page import read_from_tree
        result = read_from_tree(semantic_tree, returns, read_spec=read_spec)
        if result is not None:
            return result

    # Path 2: visual scroll loop.
    if bundle is not None and platform is not None and log_dir is not None:
        ppv = prepare_vision_prompt_png
        if ppv is None:
            ppv = getattr(bundle, "prepare_vision_prompt_png", None)
        return scroll_until_read(
            bundle, platform, log_dir, returns,
            read_spec=read_spec,
            check_knowledge=check_knowledge,
            prepare_vision_prompt_png=ppv,
            context_reports=context_reports,
            max_scrolls=max_scrolls,
        )

    # Path 3: single-frame vision fallback.
    from gui_agent.core.orchestrator.structured_read import structured_read
    ppv = prepare_vision_prompt_png
    if ppv is None:
        ppv = getattr(bundle, "prepare_vision_prompt_png", None)
    return structured_read(
        obs.png_bytes, returns,
        read_spec=read_spec,
        check_knowledge=check_knowledge,
        prepare_vision_prompt_png=ppv,
        context_reports=context_reports,
    )


_DEFAULT_MAX_PAGES = 20


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

    Single-page grids: reads all rows in one shot (AX tree is scroll-position-independent).
    Paginated grids: driven by ``Observation.viewport`` (the page-level pager sensor in
    ``table_reader.py``) through the SAME ``TraversalController`` state machine the
    multi-turn ``react_until_collected``/``ListTraversalRuntime`` path uses — not a
    bespoke heuristic. This means a grid that loads mid-pagination (e.g. a saved
    UI-grid bookmark restoring a non-1 page) rewinds to page 1 for free via the
    controller's existing ``started_at_page``/``paginate_prev`` logic, instead of a
    second, narrower click-and-diff mechanism duplicating what the controller already
    does. When no viewport signal is available at all (no DOM pager sensor matched),
    this falls back to the legacy forward-only walk via the AX tree's next-page button.
    """
    from gui_agent.adapters.browser.semantic_page import (
        _row_dedup_key,
        find_next_page_ref,
        find_prev_page_ref,
        read_grid_from_tree,
    )
    from gui_agent.core.orchestrator.traversal_controller import TraversalController

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
        from gui_agent.core.orchestrator.list_traversal_runtime import rows_from_tables

        return rows_from_tables(getattr(obs, "tables", None), returns)

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

    # rows is not None ⇒ the AX tree produced this grid, so semantic_tree is truthy. The
    # pagination walk below is AX-tree-driven and relies on that invariant.
    assert semantic_tree is not None
    all_rows: list[dict[str, str]] = list(rows)
    seen_keys: set[str] = {_row_dedup_key(r) for r in all_rows}

    viewport = getattr(obs, "viewport", None)
    _on_page_1 = not isinstance(viewport, dict) or (
        (viewport.get("page_index") or 1) <= 1 and not viewport.get("has_prev_page")
    )
    # When grid is not on page 1 (e.g. left mid-pagination by a prior run),
    # disable limit so the controller rewinds + forward-collects all rows;
    # the downstream data_query's ORDER BY + LIMIT selects the correct row.
    _effective_limit = limit if _on_page_1 else None

    if _effective_limit and len(all_rows) >= _effective_limit:
        return all_rows[:_effective_limit]

    client = getattr(platform, "client", None) if platform is not None else None
    if client is None or bundle is None or log_dir is None:
        return all_rows  # single-page only, no pagination

    controller = TraversalController("grid") if isinstance(viewport, dict) else None

    for page_n in range(1, max_pages):
        if controller is not None:
            decision = controller.update(viewport)
            if decision == "done":
                break
            if decision == "paginate_prev":
                ref = find_prev_page_ref(semantic_tree)
            elif decision == "paginate_next":
                ref = find_next_page_ref(semantic_tree)
            else:
                # stay / set_page_size / unresolved unknown streak: the controller has
                # no driveable action this frame — fall back to the forward-only signal
                # so a pager the sensor can't fully parse still makes progress.
                ref = find_next_page_ref(semantic_tree)
        else:
            ref = find_next_page_ref(semantic_tree)

        if ref is None:
            break

        try:
            client.click_by_ref(ref)
        except Exception:
            break

        settle = getattr(client, "wait_settled", None)
        if callable(settle):
            try:
                settle("navigate")
            except Exception:
                pass

        obs_url = f"grid_page_{page_n + 1}.png"
        try:
            obs = bundle.make_perception(platform, log_dir / obs_url).observe()
        except Exception:
            break

        semantic_tree = getattr(obs, "semantic_tree", None)
        if not semantic_tree:
            break
        viewport = getattr(obs, "viewport", None)

        page_rows = read_grid_from_tree(semantic_tree, returns)
        if not page_rows:
            break

        new_count = 0
        for r in page_rows:
            k = _row_dedup_key(r)
            if k not in seen_keys:
                seen_keys.add(k)
                all_rows.append(r)
                new_count += 1

        if _effective_limit and len(all_rows) >= _effective_limit:
            break  # collected enough rows — no need to paginate further

        # Without a controller (no viewport signal at all), "no new rows" is the only
        # boundary signal we have. With a controller, a rewind step revisiting an
        # already-seen page is expected to add 0 rows — that's not "the end of the list".
        if controller is None and new_count == 0:
            break

    return all_rows
