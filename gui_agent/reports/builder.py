"""Build report data models from exploration and execution logs."""

from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image

from .images import (
    _load_img,
    _save_report_img,
    annotate_action,
    annotate_back_attempts_img,
    annotate_recon_taps,
)
from .metrics import _MODELS_MAP, _sum_tokens, _token_cost
from .models import (
    AppReconData,
    ReconFlow,
    ReconPageInfo,
    ReconTap,
    ReportData,
    ReportPage,
    ReportStep,
)
from .orchestrator_html import _attach_non_ui_screenshots, _synthetic_non_ui_steps

_REPO_ROOT = Path(__file__).resolve().parents[2]

def _normalize_error(raw: str | dict | None) -> dict | None:
    """Normalize error to {message, failed_tap?, failed_element?, back_attempts?} or None."""
    if not raw:
        return None
    if isinstance(raw, str):
        return {"message": raw}
    return raw  # already a dict from ProbeAbortedError


# ── Recon builder ─────────────────────────────────────────────

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

        # Load knowledge files for content lookup. Knowledge lives at the repo root under
        # knowledge/<platform>/<app>/ (recon is iPhone-only today); anchor on this file's repo
        # root rather than log_dir depth, which varies by report type.
        knowledge_dir = _REPO_ROOT / "knowledge" / "iphone" / app_name
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
        milestone_states = ctx.get("milestone_states") if isinstance(ctx.get("milestone_states"), dict) else {}
        # Title is the user's ORIGINAL input; the resolved goal is shown as provenance.
        # Old logs without raw_input fall back to the goal.
        data.raw_input = ctx.get("raw_input") or ""
        data.goal = ctx.get("goal", "")
        data.router = ctx.get("router") or {}
        data.platform = ctx.get("platform") or ""
        run_state = ctx.get("run") or {}
        data.output = run_state.get("output") or ctx.get("output") or ""
        data.stop_reason = run_state.get("stop_reason") or ctx.get("stop_reason") or ""
        data.run_status = run_state.get("status") or ctx.get("run_status") or ""
        data.goal_completed = bool(run_state.get("goal_completed", ctx.get("goal_completed", False)))
        data.knowledge = ctx.get("knowledge") or {}
        data.orchestrator = ctx.get("orchestrator") or {}
        _attach_non_ui_screenshots(data.orchestrator, run_dir)
        data.webarena = ctx.get("webarena") or {}
        data.mobileworld = ctx.get("mobileworld") or {}
        if data.webarena and not data.webarena.get("eval_result"):
            output_dir = str(data.webarena.get("task_output_dir") or "")
            if output_dir:
                eval_path = Path(output_dir) / "eval_result.json"
                if eval_path.exists():
                    try:
                        data.webarena["eval_result_path"] = str(eval_path)
                        data.webarena["eval_result"] = json.loads(eval_path.read_text(encoding="utf-8"))
                    except Exception:
                        pass
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
            kdir = (_REPO_ROOT / "knowledge"
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
            operation_mode = str(turn.get("operation_mode") or "interactive")
            non_ui = turn.get("non_ui") if isinstance(turn.get("non_ui"), dict) else None
            ad = turn.get("action_decision") or {}
            action = ad.get("action") or {}
            atype = (non_ui.get("kind") if non_ui else action.get("action_type")) or "none"
            x = action.get("x")
            y = action.get("y")
            desc = (non_ui.get("name") if non_ui else action.get("description")) or ""
            sup = turn.get("supervisor") or {}
            summary = (non_ui.get("summary") if non_ui else sup.get("summary")) or ""
            executed = bool(turn.get("executed", False))

            total_actions += 1
            if executed:
                total_executed += 1

            ss_name = str(
                (non_ui or {}).get("observation_url")
                or turn.get("observation_url")
                or f"screenshot_turn_{idx}.png"
            )
            ss_path = run_dir / ss_name
            if not ss_path.exists() and operation_mode != "non_interactive":
                for fallback_idx in range(int(idx or 0) - 1, 0, -1):
                    fallback_path = run_dir / f"screenshot_turn_{fallback_idx}.png"
                    if fallback_path.exists():
                        ss_path = fallback_path
                        ss_name = fallback_path.name
                        break
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
                # Full-resolution annotated frame for click-to-zoom: the thumbnail uses the
                # downscaled ann.jpg, but zoom shows the action marker at full size (previously
                # zoom fell back to the raw screenshot and dropped the annotation).
                full_ann_path = run_dir / f"screenshot_turn_{idx}_ann_full.jpg"
                _save_report_img(annotated_img, full_ann_path, max_w=None)
                annotated_full_url = full_ann_path.name
            elif ss_path.exists():
                annotated_url = ss_path.name
                annotated_full_url = ss_path.name  # no action coordinate → raw, unannotated
            else:
                annotated_url = ""
                annotated_full_url = ""

            raw_url = ss_path.name if ss_path.exists() else ""

            status = "✓" if executed else "✗"
            if operation_mode == "non_interactive":
                status = "✓ non-UI" if executed else "✗ non-UI"
            if atype == "none":
                status = "— skip"

            all_steps.append(ReportStep(
                label=f"Turn {idx}",
                action_type=atype,
                x=x,
                y=y,
                description=desc or summary,
                annotated_before_url=annotated_url,
                annotated_full_url=annotated_full_url,
                raw_screenshot_url=raw_url,
                after_url=None,
                status=status,
                timestamp=turn.get("timestamp", ""),
                index=idx,
                milestone_id=sup.get("milestone_id", ""),
                milestone_kind=sup.get("milestone_kind", ""),
                instruction=sup.get("instruction", ""),
                summary=summary,
                replan_directive=sup.get("replan_directive") or "",
                stop_reason=sup.get("stop_reason") or "",
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
                llm_context=turn.get("llm_context") or [],
                operation_mode=operation_mode,
                non_ui=non_ui,
                no_effect=bool(turn.get("no_effect")),
                replan=turn.get("replan") if isinstance(turn.get("replan"), dict) else None,
            ))

        existing_non_ui: set[tuple[str, str]] = set()
        for step in all_steps:
            if step.operation_mode == "non_interactive" and isinstance(step.non_ui, dict):
                existing_non_ui.add((str(step.non_ui.get("var") or ""), str(step.non_ui.get("name") or step.description)))
        synthetic_non_ui = _synthetic_non_ui_steps(
            data.orchestrator,
            run_dir,
            start_index=len(all_steps) + 1,
            existing=existing_non_ui,
        )
        all_steps.extend(synthetic_non_ui)
        total_actions += len(synthetic_non_ui)
        total_executed += sum(1 for step in synthetic_non_ui if "✓" in step.status)

        # Build milestone lookup from persisted decomposition
        ms_lookup: dict[str, dict] = {}
        for ms in ctx.get("milestones", []):
            mid = ms.get("id", "")
            state = milestone_states.get(mid) if mid else None
            merged = dict(ms)
            if isinstance(state, dict):
                for key in (
                    "status", "retry_count", "done_check", "checklist", "reads",
                    "last_summary", "last_turn_index", "pre_existing", "collection_summary",
                ):
                    value = state.get(key)
                    if value not in (None, "", [], {}):
                        merged[key] = value
            ms_lookup[mid] = merged


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
                        milestone_kind=ms_meta.get("kind", "") or current_page_steps[0].milestone_kind,
                        milestone_name=ms_meta.get("name", "") or current_page_steps[0].description,
                        milestone_description=ms_meta.get("description", "") or current_page_steps[0].summary,
                        success_condition=ms_meta.get("success_condition", ""),
                        checklist=ms_meta.get("checklist", []) or [],
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
                milestone_kind=ms_meta.get("kind", "") or current_page_steps[0].milestone_kind,
                milestone_name=ms_meta.get("name", "") or current_page_steps[0].description,
                milestone_description=ms_meta.get("description", "") or current_page_steps[0].summary,
                success_condition=ms_meta.get("success_condition", ""),
                checklist=ms_meta.get("checklist", []) or [],
            ))

        # Build milestones summary
        for page in pages:
            ms_steps = page.steps
            ms_state = ms_lookup.get(page.milestone_id, {})
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
                "status": ms_state.get("status", ""),
                "retry_count": ms_state.get("retry_count", 0),
                "reads": ms_state.get("reads", {}),
                "checklist": ms_state.get("checklist", []),
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
            # A milestone abandoned as INFEASIBLE never produces a done_check, so its 验收 slot is
            # blank. Surface the Feasibility kick-back verdict (why + the re-decompose directive) as
            # that milestone's terminal acceptance instead.
            kb = next((s for s in page.steps if s.replan_directive or s.stop_reason.startswith("milestone 不可行")), None)
            if kb is not None:
                # Acceptance display only — this milestone was judged infeasible. (The inline #0↻N
                # program card is placed separately, by the re-decompose's at_turn, in runner_html.)
                page.kickback = {
                    "reason": kb.stop_reason or kb.summary,
                    "directive": kb.replan_directive,
                }

        data.pages = pages
        data.milestones = milestones_info
        # Decompose summary: list all milestones with names
        ms_parts = []
        for ms in milestones_info:
            ms_parts.append(f"#{ms['id']} {ms['name']}（{ms['kind']}）")
        data.decompose_summary = " → ".join(ms_parts) if ms_parts else ""
        data.stats = {
            "turns": len(all_steps),
            "executed": total_executed,
        }
        return data
