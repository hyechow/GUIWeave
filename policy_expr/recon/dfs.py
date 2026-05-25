"""DFS page exploration: depth-first traversal with post-order knowledge generation."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from policy_expr.executor import logical_xy
from policy_expr.perception import try_resume_mac
from policy_expr.recon.back_nav import make_nav_context, manual_recover as _manual_recover
from policy_expr.recon.planned_back_nav import planned_return_to_initial as return_to_initial
from policy_expr.recon.page_identity import PageIdentity
from policy_expr.recon.utils import ProbeAbortedError
from policy_expr.trace import Tracer

# Module-level HUD status callback; set by explore_dfs, read by _probe_page_dfs.
_status_cb: Callable[[str], None] | None = None


def _safe_tap(client, lx: float, ly: float) -> str:
    """Tap with automatic Mac popup recovery. Returns tap response string."""
    resp = client.tap(lx, ly)
    if "paused" in resp.lower():
        print(f"    Mac 弹窗阻断，尝试恢复...")
        if try_resume_mac(client):
            time.sleep(0.5)
            resp = client.tap(lx, ly)
    return resp


@dataclass
class DfsPageNode:
    """One page in the DFS tree. Carries all state needed for deferred knowledge gen."""
    page_name: str
    parent: str | None
    via_tap: str | None
    depth: int
    nav_chain: list[tuple[float, float, str]]  # (lx, ly, label) from root
    out_dir: Path
    recon_result: object | None = None  # ReconResult, lazy import to avoid cycle
    children: list[DfsPageNode] = field(default_factory=list)
    error: dict | None = None
    knowledge_generated: bool = False


def explore_dfs(phone, app_log_dir: Path, max_depth: int = 0,
                sample: int = 0, mode: str | None = None,
                target_dir: str | None = None,
                status_cb: Callable[[str], None] | None = None) -> list[DfsPageNode]:
    """Top-level DFS exploration.

    Phone must be on the root page.
    mode: None=initial, "add"=add to existing app, "update"=re-probe target page.
    target_dir: add=parent page dir name; update=page dir to overwrite.
    status_cb: optional callable(text) for live status updates (e.g. HUD).
    Returns tree of explored pages (children attached).
    """
    global _status_cb
    _prev_cb = _status_cb
    _status_cb = status_cb
    try:
        return _explore_dfs_impl(phone, app_log_dir, max_depth, sample, mode, target_dir)
    finally:
        _status_cb = _prev_cb


def _explore_dfs_impl(phone, app_log_dir: Path, max_depth: int = 0,
                      sample: int = 0, mode: str | None = None,
                      target_dir: str | None = None) -> list[DfsPageNode]:
    from policy_expr.recon.page_parser import PageParser
    from policy_expr.recon.cascade_matcher import get_matcher

    app = app_log_dir.name
    app_log_dir.mkdir(parents=True, exist_ok=True)

    tracer = Tracer()
    trace_path = app_log_dir / "trace.json"
    visited_page_entries: list[tuple[str, bytes]] = []
    dedup = PageIdentity(get_matcher())
    chain_to_page: dict[tuple, str] = {}

    # Parse root page identity
    png_bytes = phone.screenshot()
    knowledge = PageParser().analyze_screen(png_bytes)
    page_name, _fingerprint, _root_form = _page_name_from_fingerprint(png_bytes)

    # update mode: use target_dir as page_name and out_dir
    inherited_parent: str = ""
    if mode == "update" and target_dir:
        page_name = target_dir
        # Inherit parent_page from existing recon_result to avoid overwriting it
        old_result = app_log_dir / page_name / "recon_result.json"
        if old_result.exists():
            import json as _json
            inherited_parent = _json.loads(old_result.read_text("utf-8")).get("parent_page", "")

    nav_stack: list[tuple[bytes, tuple[float, float] | None]] = [(png_bytes, None)]

    # Record root
    print(f"  [已探测 0 页] ✓ 根页面 — {page_name}")
    dedup.add(png_bytes, name=page_name, form=_root_form)
    visited_page_entries.append((page_name, png_bytes))
    tracer.record_page(page_name, None, None, 0)
    tracer.save(trace_path)

    # Map root's empty chain to page name so children can find their parent
    chain_to_page[()] = page_name

    _root_parent = target_dir if mode == "add" else (inherited_parent or None)
    root_node = DfsPageNode(
        page_name=page_name,
        parent=_root_parent,
        via_tap=None, depth=0,
        nav_chain=[], out_dir=app_log_dir / page_name,
    )

    root_ctx = (knowledge.page, png_bytes, app_log_dir / page_name)

    # Probe root page with DFS callback
    def _on_root_element_tapped(area, after_bytes, navigated, tap_index, tap_result, selected_tab=""):
        """Callback for root page exploration. Enter child pages if depth allows."""
        print(f"    [DEBUG ROOT] _on_root_element_tapped called: area={area.label}, navigated={navigated}, max_depth={max_depth}")
        if not navigated or max_depth <= 0:
            print(f"    [DEBUG ROOT] Skipping child entry (navigated={navigated}, max_depth={max_depth})")
            return True  # Continue probing

        # Enter child page
        print(f"\n  → 进入子页面「{area.label}」")
        print(f"    [DEBUG ROOT] Entering child page: max_depth={max_depth}, will pass {max_depth - 1} to child")
        time.sleep(1.5)  # 等待子页面加载稳定
        ax, ay = area.center_xy
        lx, ly = logical_xy(ax, ay)
        child_chain = [(lx, ly, area.label)]

        # Build child nav_stack with parent forward_coords
        parent_bytes, _ = nav_stack[-1]
        child_nav_stack = nav_stack[:-1] + [(parent_bytes, (lx, ly)), (after_bytes, None)]

        # Recursive DFS into child
        child_node, child_dedup = _dfs_recursive(
            phone, child_chain, child_nav_stack, root_ctx,
            chain_to_page, dedup, visited_page_entries, tracer, trace_path,
            app_log_dir, max_depth - 1, sample,
        )

        if child_node is not None:
            root_node.children.append(child_node)

        # Return to root page
        print(f"\n  ← 返回「{page_name}」")
        child_elements = (child_dedup or {}).get("initial_elements")
        ok, back_log = return_to_initial(
            phone.client, phone.screenshot, nav_stack,
            before_back_bytes=phone.screenshot(),
            nav_context=make_nav_context(area.label, area.element_type),
            target_label=page_name,
            after_elements=child_elements,
            status_cb=_status_cb,
            selected_tab=selected_tab,
        )
        if not ok:
            recovered = _manual_recover(
                phone.client, phone.screenshot, nav_stack,
                len(nav_stack) - 1,
                prompt=f"子页面探索后无法返回 {page_name}",
            )
            if not recovered:
                raise ProbeAbortedError(
                    f"DFS: 无法从子页面返回 {page_name}",
                    failed_tap=-1, failed_element="",
                    back_attempts=back_log,
                )

        # Update tap result
        tap_result.child_status = _child_status(child_node, max_depth - 1)
        tap_result.back_attempts = back_log
        if child_dedup:
            tap_result.identity = child_dedup

        return True  # Continue probing next element

    try:
        root_node.recon_result, root_node.out_dir = _probe_page_dfs(
            phone, knowledge, png_bytes, root_node.out_dir,
            sample=sample, nav_stack=nav_stack,
            on_element_tapped=_on_root_element_tapped,
            parent_page=root_node.parent or "",
        )
        _update_recon_log(app_log_dir, page_name, mode or "initial")
    except ProbeAbortedError as e:
        tracer.record_error(page_name, e)
        tracer.save(trace_path)
        raise

    # DFS into children is now handled inside _probe_page_dfs via callback

    return [root_node]


def _dfs_recursive(
    phone,
    nav_chain: list[tuple[float, float, str]],
    nav_stack: list[tuple[bytes, tuple[float, float] | None]],
    root_ctx: tuple,
    chain_to_page: dict[tuple, str],
    dedup: "PageIdentity",
    visited_page_entries: list[tuple[str, bytes]],
    tracer: Tracer,
    trace_path: Path,
    app_log_dir: Path,
    max_depth: int,
    sample: int,
) -> tuple[DfsPageNode | None, dict]:
    """Recursive DFS step. Phone is on the target page.

    PRE:  phone is on this page (nav_stack top)
    POST: phone is back on this page (after all children explored)

    Returns (node, dedup_info) where dedup_info describes identity of this page.
    """
    app = app_log_dir.name

    # Take screenshot first for mini-program check (before expensive LLM parse)
    png_bytes = phone.screenshot()

    # Mini-program fast exit: GUIClip capsule detection — no LLM needed
    from policy_expr.recon.page_parser import detect_miniprogram
    close_xy = detect_miniprogram(png_bytes)
    if close_xy is not None:
        print(f"  [小程序] 检测到小程序，跳过探测，直接关闭")
        _tap_close_xy(phone, close_xy)
        return None, {"is_new": False, "phase": "miniprogram"}

    # Full-screen popup: pixel pre-check → LLM locate → YOLO snap → tap
    from policy_expr.recon.popup_nav import close_popup
    if close_popup(phone.client, phone.screenshot, png_bytes):
        png_bytes = phone.screenshot()

    # ── Overlay detection (前置于去重) ──
    _overlay = _check_overlay(nav_stack, png_bytes)
    if _overlay:
        print(f"  [overlay] 检测到 {_overlay.type}  bbox={_overlay.bbox}")

    # ── Dedup BEFORE LLM parse (saves LLM call for duplicate pages) ──
    from policy_expr.recon.cascade_matcher import get_matcher
    candidate_emb = get_matcher().embed_visual(png_bytes)
    dedup_result = dedup.check(png_bytes, precomputed=candidate_emb)
    lib_n = dedup_result.library_size

    chain_key = tuple(nav_chain)
    parent_page = chain_to_page.get(chain_key[:-1]) if chain_key else None
    via_tap = nav_chain[-1][2] if nav_chain else None
    _src = parent_page or ""
    _tap = via_tap or ""

    # ── Dedup result + terminal output ──
    n = dedup_result.library_size
    is_dup = dedup_result.is_duplicate
    _print_dedup(n, dedup_result)

    if is_dup:
        tracer.record_transition(_src, _tap, "duplicate", "skipped_visited")
        tracer.save(trace_path)
        dedup_info = {
            "is_new": False,
            "phase": dedup_result.phase,
            "visual_sim": round(dedup_result.best_visual_sim, 4),
            "text_sim": round(dedup_result.best_text_sim, 4) if dedup_result.best_text_sim else None,
            "library_size": n,
            "matched_name": dedup_result.matched_name,
        }
        return None, dedup_info

    # ── New page: parse elements + fingerprint concurrently ──
    import re as _re
    import concurrent.futures as _cf
    from policy_expr.recon.page_parser import PageParser

    with _cf.ThreadPoolExecutor(max_workers=2) as _ex:
        _fut_know = _ex.submit(PageParser().analyze_screen, png_bytes)
        _fut_fp   = _ex.submit(get_matcher().generate_fingerprint, png_bytes)
        knowledge = _fut_know.result()
        _fp       = _fut_fp.result()

    page_name   = _re.sub(r'[\\/:*?"<>|\s]+', '_', _fp.purpose) or "page"
    fingerprint = f"用途：{_fp.purpose}\n内容区：{_fp.content}\n页面形态：{_fp.form}"
    print(f"  [fingerprint] form={_fp.form} content={_fp.content} purpose={_fp.purpose}")

    # Overlay page (form C/D/E/F/G): register for dedup but skip probing
    if _fp.form not in ("A", "B"):
        print(f"  [overlay_skip] form={_fp.form}，弹窗页入库去重、跳过探测")
        dedup.add(png_bytes, name=page_name, emb=candidate_emb, form=_fp.form)
        return None, {"is_new": False, "phase": "overlay_skip"}

    # Normal page: register in dedup and explore
    dedup.add(png_bytes, name=page_name, emb=candidate_emb, form=_fp.form)

    chain_to_page[chain_key] = page_name
    visited_page_entries.append((page_name, png_bytes))
    tracer.record_page(page_name, parent_page, via_tap, len(nav_chain))
    tracer.save(trace_path)

    # Dedup info to embed into ReconResult
    dedup_info = {
        "is_new": True,
        "phase": dedup_result.phase,
        "visual_sim": round(dedup_result.best_visual_sim, 4),
        "text_sim": round(dedup_result.best_text_sim, 4) if dedup_result.best_text_sim else None,
        "library_size": n,
        "page_name": page_name,
        "fingerprint": fingerprint,
        # Parsed elements for this page (0-1000 coords) — used by back_nav LLM
        "initial_elements": [
            {"label": a.label, "element_type": a.element_type,
             "x": a.center_xy[0], "y": a.center_xy[1]}
            for a in knowledge.areas
        ],
    }

    out_dir = app_log_dir / page_name
    node = DfsPageNode(
        page_name=page_name,
        parent=parent_page, via_tap=via_tap,
        depth=len(nav_chain), nav_chain=list(nav_chain),
        out_dir=out_dir,
    )

    if max_depth == 0:
        print(f"  [max_depth=0] 跳过探测，仅记录页面")
        tracer.record_transition(_src, _tap, page_name, "depth_limit")
        tracer.save(trace_path)
        _update_recon_log(app_log_dir, page_name, "initial")
        return node, dedup_info
    else:
        tracer.record_transition(_src, _tap, page_name, "entered")
        tracer.save(trace_path)
        try:
            # DFS-style incremental probing with callback
            def _on_element_tapped(area, after_bytes, navigated, tap_index, tap_result, selected_tab=""):
                """Called after each element tap. Enter child page if navigated."""
                print(f"    [DEBUG] _on_element_tapped called: area={area.label}, navigated={navigated}, max_depth={max_depth}")
                if not navigated or max_depth <= 0:
                    print(f"    [DEBUG] Skipping child entry (navigated={navigated}, max_depth={max_depth})")
                    return True  # Continue probing

                # Enter child page
                print(f"\n  → 进入子页面「{area.label}」")
                print(f"    [DEBUG] Entering child page: max_depth={max_depth}, will pass {max_depth - 1} to child")
                time.sleep(1.5)  # 等待子页面加载稳定
                ax, ay = area.center_xy
                lx, ly = logical_xy(ax, ay)
                child_chain = nav_chain + [(lx, ly, area.label)]

                # Build child nav_stack with parent forward_coords
                parent_bytes, _ = nav_stack[-1]
                child_nav_stack = nav_stack[:-1] + [(parent_bytes, (lx, ly)), (after_bytes, None)]

                # Recursive DFS into child
                child_node, child_dedup = _dfs_recursive(
                    phone, child_chain, child_nav_stack, root_ctx,
                    chain_to_page, dedup, visited_page_entries, tracer, trace_path,
                    app_log_dir, max_depth - 1, sample,
                )

                if child_node is not None:
                    node.children.append(child_node)

                # Return to current page
                print(f"\n  ← 返回「{page_name}」")
                child_elements = (child_dedup or {}).get("initial_elements")
                ok, back_log = return_to_initial(
                    phone.client, phone.screenshot, nav_stack,
                    before_back_bytes=phone.screenshot(),
                    nav_context=make_nav_context(area.label, area.element_type),
                    target_label=page_name,
                    after_elements=child_elements,
                    selected_tab=selected_tab,
                    status_cb=_status_cb,
                )
                if not ok:
                    recovered = _manual_recover(
                        phone.client, phone.screenshot, nav_stack,
                        len(nav_stack) - 1,
                        prompt=f"子页面探索后无法返回 {page_name}",
                    )
                    if not recovered:
                        raise ProbeAbortedError(
                            f"DFS: 无法从子页面返回 {page_name}",
                            failed_tap=-1, failed_element="",
                            back_attempts=back_log,
                        )

                # Update tap result
                tap_result.child_status = _child_status(child_node, max_depth - 1)
                tap_result.back_attempts = back_log
                if child_dedup:
                    tap_result.identity = child_dedup

                return True  # Continue probing next element

            node.recon_result, node.out_dir = _probe_page_dfs(
                phone, knowledge, png_bytes, out_dir,
                sample=sample, nav_stack=nav_stack,
                on_element_tapped=_on_element_tapped,
                parent_page=node.parent or "",
                fingerprint=fingerprint,
            )
            _update_recon_log(app_log_dir, page_name, "initial")
        except ProbeAbortedError as e:
            tracer.record_error(page_name, e)
            tracer.save(trace_path)
            node.error = {"message": str(e)}
            return node, dedup_info

    # POST: phone on this page
    return node, dedup_info


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _child_status(child_node, depth: int) -> str:
    """Derive child exploration status from _dfs_recursive return value."""
    if child_node is None:
        return "duplicate"
    if child_node.recon_result is not None:
        return "new_explored"
    if child_node.error:
        return "error"
    return "new_depth_limit"


def _tap_close_xy(phone, close_xy: list[float]) -> None:
    """Tap the capsule × at the given 0-1000 coordinates and wait."""
    ax, ay = close_xy[0], close_xy[1]
    lx, ly = logical_xy(ax, ay)
    print(f"  [小程序] 胶囊× @ ({ax:.0f}, {ay:.0f})  →  逻辑({lx:.0f}, {ly:.0f})")
    _safe_tap(phone.client, lx, ly)
    time.sleep(1.5)



def _probe_page_dfs(phone, knowledge, png_bytes, out_dir: Path,
                    sample: int = 0,
                    nav_stack: list | None = None,
                    on_element_tapped: callable | None = None,
                    parent_page: str = "",
                    fingerprint: str = "") -> tuple:
    """DFS-style incremental probing: tap each element one by one, with callback after each tap.

    Args:
        on_element_tapped: callback(area, after_bytes, navigated, tap_index) -> bool
            Returns True to continue probing, False to stop.

    Returns (ReconResult, out_dir).
    """
    import random
    from policy_expr.executor import logical_xy
    from policy_expr.recon import viz_result
    from policy_expr.recon.utils import TapResult, ReconResult
    from llm.structured import get_llm_call_count

    llm_count_start = get_llm_call_count()

    page = knowledge.page
    areas = knowledge.areas

    # Skip back buttons (their behavior is known)
    areas = [a for a in areas if a.element_type != "back_button"]

    effective_limit = sample if sample > 0 else 0  # max navigated taps, 0 = unlimited
    random.shuffle(areas)

    print(f"\n{'=' * 60}")
    print(f"点击探测: {len(areas)} 个可交互区域")
    print(f"{'=' * 60}")

    result = ReconResult(
        elements_count=len(page.interactive_elements),
        initial_screenshot_path=str(out_dir / "initial.png"),
        parent_page=parent_page,
    )

    # Save initial screenshot
    out_dir.mkdir(parents=True, exist_ok=True)
    img_path = out_dir / "initial.png"
    img_path.write_bytes(png_bytes)
    viz_result(knowledge, png_bytes, "initial", out_dir)

    # Detect selected tab for planner context (1 LLM call per page)
    from policy_expr.recon.planned_back_nav import detect_selected_tab
    result.selected_tab = detect_selected_tab(png_bytes) or ""

    # Append fingerprint to initial_result.json
    if fingerprint:
        init_json = out_dir / "initial_result.json"
        if init_json.exists():
            data = json.loads(init_json.read_text("utf-8"))
            data["fingerprint"] = fingerprint
            init_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    tap_dir = out_dir / "tap"
    tap_dir.mkdir(parents=True, exist_ok=True)
    result_path = out_dir / "recon_result.json"

    top_level = len(nav_stack) - 1 if nav_stack else 0

    effective_count = 0
    if _status_cb:
        _status_cb(f"探测 {out_dir.name}  0/{len(areas)}")

    for i, area in enumerate(areas, 1):
        ax, ay = area.center_xy
        lx, ly = logical_xy(ax, ay)
        etype = area.element_type or ""
        print(f"\n  [{i}/{len(areas)}] 「{area.label}」 ({etype}) @ ({ax:.0f},{ay:.0f}) → ({lx:.0f},{ly:.0f})")

        if _status_cb:
            eff_tag = f" ({effective_count}/{effective_limit} 有效)" if effective_limit else ""
            _status_cb(f"探测 {out_dir.name}  {i}/{len(areas)}{eff_tag}  {area.label}")

        tap_response = _safe_tap(phone.client, lx, ly)

        tap_ok = "failed" not in tap_response.lower() and "interrupted" not in tap_response.lower() and "paused" not in tap_response.lower()
        print(f"    结果: {tap_response}")
        time.sleep(2.0)

        after_bytes = phone.screenshot()
        safe_label = _sanitize_filename(area.label)
        after_path = tap_dir / f"tap_{i:02d}_{safe_label}.png"
        if after_bytes:
            after_path.write_bytes(after_bytes)
            print(f"    截图: {after_path}")

        navigated = False
        if after_bytes and nav_stack:
            from policy_expr.recon.back_nav import _get_change_comp
            comp = _get_change_comp()
            nav_result, nav_reason = comp.detect_navigation(nav_stack[top_level][0], after_bytes)
            if not nav_result:
                print(f"    {nav_reason}，跳过")
            else:
                navigated = True

        if navigated:
            effective_count += 1

        tap_result = TapResult(
            index=i,
            element_type="area",
            label=area.label,
            x=ax,
            y=ay,
            tap_ok=tap_ok,
            screenshot_path=str(after_path),
            navigated=navigated,
        )
        result.taps.append(tap_result)

        # Callback for DFS to enter child pages
        if on_element_tapped:
            should_continue = on_element_tapped(area, after_bytes, navigated, i, tap_result, selected_tab=result.selected_tab)
            if not should_continue:
                print(f"    [DFS] 探测中断（callback 返回 False）")
                break

        # Stop when effective limit reached
        if effective_limit and effective_count >= effective_limit:
            print(f"  [采样] 已达 {effective_limit} 个有效点击，停止探测")
            break

    # Final save after all taps and callbacks complete
    result.save(result_path)
    ok = sum(1 for t in result.taps if t.tap_ok and t.navigated)
    llm_used = get_llm_call_count() - llm_count_start
    print(f"  探测完成: {len(result.taps)} 个元素 (成功导航 {ok}, LLM 调用 {llm_used} 次)")
    return result, out_dir


def _count_nodes(nodes: list[DfsPageNode]) -> int:
    return sum(1 + _count_nodes(n.children) for n in nodes)


def _page_name_from_fingerprint(png_bytes: bytes) -> tuple[str, str, str]:
    """Generate a stable page name from the fingerprint purpose field.

    Returns (page_name, fingerprint_text, form).
    """
    import re
    from policy_expr.recon.cascade_matcher import get_matcher
    matcher = get_matcher()
    fp = matcher.generate_fingerprint(png_bytes)
    name = re.sub(r'[\\/:*?"<>|\s]+', '_', fp.purpose)
    fingerprint_text = f"用途：{fp.purpose}\n内容区：{fp.content}\n页面形态：{fp.form}"
    return name or "page", fingerprint_text, fp.form


def _sanitize_filename(label: str) -> str:
    """Sanitize a label for use as a filename component."""
    import re
    return re.sub(r'[\\/:*?"<>|\s]+', '_', label)[:40] or "item"


def _update_recon_log(app_log_dir: Path, page_dir: str, trigger: str) -> None:
    """Append/update one page entry in recon_log.json."""
    log_path = app_log_dir / "recon_log.json"
    log: dict = {}
    if log_path.exists():
        log = json.loads(log_path.read_text("utf-8"))

    log[page_dir] = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "trigger": trigger,
    }
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Post-exploration similarity report
# ---------------------------------------------------------------------------

def compute_pairwise_similarity(
    page_entries: list[tuple[str, bytes]],
    threshold: float = 0.5,
    save_path: Path | None = None,
) -> list[dict]:
    """Compute pairwise similarity for all visited pages and print a report.

    Args:
        page_entries: list of (page_name, png_bytes) in visit order.
        threshold: pairs with similarity >= threshold are flagged in the report.
        save_path: if given, save the full pair list as JSON.

    Returns:
        list of {"page_a", "page_b", "similarity"} sorted by similarity desc.
    """
    import json
    from policy_expr.recon.page_compare import PageComparator

    comparator = PageComparator()
    n = len(page_entries)
    pairs: list[dict] = []

    for i in range(n):
        for j in range(i + 1, n):
            name_a, bytes_a = page_entries[i]
            name_b, bytes_b = page_entries[j]
            sim = comparator.raw_similarity(bytes_a, bytes_b)
            pairs.append({"page_a": name_a, "page_b": name_b, "similarity": round(sim, 4)})

    pairs.sort(key=lambda x: x["similarity"], reverse=True)

    if save_path:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(json.dumps({"threshold": threshold, "pairs": pairs}, ensure_ascii=False, indent=2))

    return pairs


# ---------------------------------------------------------------------------
# Dedup structured logging
# ---------------------------------------------------------------------------
# Dedup terminal output
# ---------------------------------------------------------------------------

def _check_overlay(nav_stack, png_bytes: bytes):
    """Detect popup/keyboard overlay against the previous nav_stack frame.

    Returns OverlayResult if an overlay is found, None otherwise.
    """
    import io
    import numpy as np
    from PIL import Image as _PIL
    from policy_expr.overlay_detect import detect_overlay

    if not nav_stack or len(nav_stack) < 2:
        return None
    before_bytes = nav_stack[-2][0]  # parent page before tap

    def _to_rgb(b: bytes) -> np.ndarray:
        return np.array(_PIL.open(io.BytesIO(b)).convert("RGB"))

    result = detect_overlay(_to_rgb(before_bytes), _to_rgb(png_bytes))
    return result if result.type != "none" else None


def _print_dedup(n: int, result) -> None:
    """Print dedup verdict to terminal."""
    is_dup = result.is_duplicate
    tag = "✓ 新页面" if not is_dup else "✗ 重复"
    detail = ""
    if is_dup:
        detail = f"匹配「{result.matched_name}」"
        if result.phase == "visual_shortcut":
            detail += f" visual={result.best_visual_sim:.3f}"
        elif result.phase == "text_match":
            detail += f" text={result.best_text_sim:.3f}"
    else:
        detail = f"visual={result.best_visual_sim:.3f}"
        if result.best_text_sim is not None:
            detail += f" text={result.best_text_sim:.3f}"

    print(f"  [已探测 {n} 页] {tag} — {detail}")
