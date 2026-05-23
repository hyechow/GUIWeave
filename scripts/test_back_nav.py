"""Back-navigation test: script-driven multi-level navigation + return_to_initial.

Usage:
    uv run python scripts/test_back_nav.py

Flow:
    1. Connect to phone, take root screenshot.
    2. Render annotated image with numbered tap targets; open it automatically.
    3. User types a number → script taps that element, confirms navigation.
    4. Repeat for desired depth (q to stop and start back-nav test).
    5. Call return_to_initial; print result.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from _vis import open_annotated, parse_items, print_items

from policy_expr.perception import LivePhoneSession, try_resume_mac
from policy_expr.executor import logical_xy
from policy_expr.recon.page_compare import make_comparator
from policy_expr.recon.page_parser import PageParser
from policy_expr.recon.back_nav import return_to_initial, BACK_SETTLE_SECONDS, make_nav_context
from policy_expr.recon.dfs import _page_name_from_fingerprint

MAX_DEPTH = 5
SETTLE = BACK_SETTLE_SECONDS


def prompt_choice(n: int) -> int | None:
    while True:
        raw = input("\n  编号（q=结束导航 / 开始回退测试）: ").strip()
        if raw.lower() == "q":
            return None
        if raw.isdigit() and 0 <= int(raw) < n:
            return int(raw)
        print(f"  请输入 0–{n-1} 之间的数字")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = PageParser()
    change_comp = make_comparator("edge_iou")

    def safe_tap(client, lx, ly):
        """Tap with Mac popup recovery."""
        resp = client.tap(lx, ly)
        if "paused" in resp.lower():
            print("  Mac 弹窗阻断，尝试恢复...")
            if try_resume_mac():
                time.sleep(0.5)
                resp = client.tap(lx, ly)
        return resp

    with LivePhoneSession() as phone:
        screenshot = phone.screenshot
        assert phone.client is not None

        # Root
        print("\n" + "="*60)
        print("  [root] 截取当前页面作为回退目标")
        print("="*60)
        root_png = screenshot()
        nav_stack: list[tuple[bytes, tuple[float, float] | None]] = [(root_png, None)]
        # tap_labels[i] = label of element tapped to go from L_i → L_{i+1}
        tap_labels: list[str] = []
        current_png = root_png

        # Navigate down
        for depth in range(1, MAX_DEPTH + 1):
            print(f"\n{'='*60}")
            print(f"  [depth {depth}] 解析页面元素")
            print("="*60)

            items = parse_items(current_png, parser)
            if not items:
                print("  未解析到可交互元素，停止")
                break

            img_path = open_annotated(current_png, items, f"back_nav_d{depth}")
            print(f"  已标注截图: {img_path}")
            print_items(items)

            choice = prompt_choice(len(items))
            if choice is None:
                print("  开始回退测试")
                break

            ax, ay = items[choice][0], items[choice][1]
            label = items[choice][2]
            etype = items[choice][3]
            lx, ly = logical_xy(ax, ay)

            print(f"\n  → tap [{etype}]「{label}」at ({ax:.0f}, {ay:.0f})")
            safe_tap(phone.client, lx, ly)
            time.sleep(SETTLE)
            after_png = screenshot()

            unchanged, sim = change_comp.no_change_score(current_png, after_png)
            if unchanged:
                print(f"  页面未变化 (edge_iou={sim:.3f})，请重选")
                continue

            print(f"  ✓ 导航成功 (edge_iou={sim:.3f})")
            prev_png, _ = nav_stack[-1]
            nav_stack[-1] = (prev_png, (lx, ly))
            nav_stack.append((after_png, None))
            # Include element type so back_nav LLM can correctly classify (e.g. tab → type B)
            nav_context_str = make_nav_context(label, etype)
            tap_labels.append(nav_context_str)
            current_png = after_png

            if depth == MAX_DEPTH:
                print(f"\n  已达最大深度 {MAX_DEPTH}")

        # Back-nav test
        depth_reached = len(nav_stack) - 1
        if depth_reached == 0:
            print("\n  未曾导航，退出")
            return

        # Ask how many levels to go back (default 1)
        while True:
            raw = input(f"\n  回退几层？(1–{depth_reached}，默认 1): ").strip()
            if raw == "":
                back_n = 1
                break
            if raw.isdigit() and 1 <= int(raw) <= depth_reached:
                back_n = int(raw)
                break
            print(f"  请输入 1–{depth_reached} 之间的数字")

        # nav_stack[-1] is the target page for return_to_initial.
        # To go back n levels: target = nav_stack[-n-1], so pass nav_stack[:-n].
        target_stack = nav_stack[:-back_n]

        # nav_context = how we got FROM the target level INTO the next level
        # = tap_labels[target_level], where target_level = depth_reached - back_n
        target_level = depth_reached - back_n
        nav_context = tap_labels[target_level] if target_level < len(tap_labels) else ""

        target_label, _, _ = _page_name_from_fingerprint(target_stack[-1][0])

        print(f"\n{'='*60}")
        print(f"  [回退测试] 当前深度 L{depth_reached}，回退 {back_n} 层 → L{target_level}")
        if nav_context:
            print(f"  nav_context: 「{nav_context}」")
        if target_label:
            print(f"  target_label: 「{target_label}」")
        print("="*60)
        for i in range(len(target_stack)):
            coords = target_stack[i][1]
            target_mark = " ← 目标" if i == len(target_stack) - 1 else ""
            print(f"  L{i}: {'→ ' + str(coords) if coords else '(无 forward 坐标)'}{target_mark}")

        # Prepare debug output dir
        import json, datetime
        import shutil
        out_dir = ROOT / "logs" / "test_back_nav"
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        print(f"  调试目录: {out_dir}")

        # Save nav_stack reference screenshots so we can see what each level looks like
        for i in range(len(target_stack)):
            ref_png = target_stack[i][0]
            ref_path = out_dir / f"ref_L{i}{'_target' if i == len(target_stack)-1 else ''}.png"
            ref_path.write_bytes(ref_png)
        print(f"  参考截图: ref_L0.png … ref_L{len(target_stack)-1}_target.png")

        print()
        before_back = screenshot()
        success, log = return_to_initial(
            client=phone.client,
            screenshot=screenshot,
            nav_stack=target_stack,
            before_back_bytes=before_back,
            out_dir=out_dir,
            nav_context=nav_context,
            target_label=target_label,
        )

        # Save structured log
        log_data = {
            "success": success,
            "depth_reached": depth_reached,
            "back_n": back_n,
            "target_level": depth_reached - back_n,
            "nav_context": nav_context,
            "target_label": target_label,
            "steps": log,
        }
        log_path = out_dir / "log.json"
        log_path.write_text(json.dumps(log_data, ensure_ascii=False, indent=2))
        print(f"  日志已保存: {log_path}")

        print(f"\n{'='*60}")
        target_level = depth_reached - back_n
        print(f"  结果: {'✓ 成功回到 L' + str(target_level) if success else '✗ 未能回到 L' + str(target_level)}")
        print("="*60)
        for i, entry in enumerate(log):
            s = entry.get("strategy", "?")
            r = entry.get("result", "")
            score = entry.get("score", "")
            score_str = f" ({score:.3f})" if isinstance(score, float) else ""
            llm_method = entry.get("llm_method", "")
            llm_str = f"  [{llm_method}]" if llm_method else ""
            coords = entry.get("coords", [])
            coord_str = f"({coords[0]},{coords[1]})" if coords else ""
            mark = "✓" if entry.get("success") else "·"
            print(f"  {mark} [{i+1:02d}] {s:16s} {coord_str:12s} → {r}{score_str}{llm_str}")

        # Generate trace visualization
        render_nav_trace(out_dir, log, target_stack)


# ── Trace visualization ────────────────────────────────────────────────────

_STRATEGY_COLORS = {
    "fixed": "#007AFF",
    "fixed+YOLO": "#5AC8FA",
    "LLM_1": "#FF9500",
    "LLM_2": "#FFCC00",
    "LLM_3": "#FF3B30",
    "LLM_1+YOLO": "#C87800",
    "LLM_2+YOLO": "#C8AA00",
    "LLM_3+YOLO": "#C83228",
    "forward": "#34C759",
}

# Page badge colors — deterministic by hash
_PAGE_PALETTE = [
    "#4A90D9", "#D94A7B", "#7BD94A", "#D9B84A",
    "#9B4AD9", "#4AD9C4", "#D96A4A", "#4A5CD9",
]


def _page_color(page_hash: str) -> str:
    if not page_hash or not all(c in "0123456789abcdef" for c in page_hash):
        return "#666"
    idx = int(page_hash, 16) % len(_PAGE_PALETTE)
    return _PAGE_PALETTE[idx]


def _b64(png_bytes: bytes) -> str:
    import base64
    return base64.b64encode(png_bytes).decode()


def render_nav_trace(out_dir: Path, log: list[dict],
                     nav_stack: list[tuple]) -> None:
    """Render navigation trace as self-contained HTML with page flow."""
    if not log:
        return

    # Collect before screenshots (R{n}_before_*.png), keyed by round number
    before_by_round: dict[int, bytes] = {}
    for f in out_dir.glob("R*_before_*.png"):
        try:
            n = int(f.name.split("_")[0][1:])  # "R10_before_..." → 10
            before_by_round[n] = f.read_bytes()
        except (ValueError, IndexError):
            pass

    # Target page
    target_b64 = ""
    target_path = out_dir / "ref_L0_target.png"
    if not target_path.exists():
        targets = sorted(out_dir.glob("ref*_target.png"))
        if targets:
            target_path = targets[0]
    if target_path.exists():
        target_b64 = _b64(target_path.read_bytes())

    # Assign page names: collect all unique page hashes, give short names
    pages_seen = {}  # hash → "P0", "P1", ...
    counter = 0
    for entry in log:
        for key in ("page_from", "page_to"):
            h = entry.get(key, "")
            if h and h not in pages_seen:
                pages_seen[h] = f"P{counter}"
                counter += 1

    # Build flow HTML
    flow_html = ""
    prev_to = None  # previous step's page_to, for connecting lines
    for i, entry in enumerate(log):
        strategy = entry.get("strategy", "?")
        color = _STRATEGY_COLORS.get(strategy, "#999")
        tap_xy = entry.get("tap_xy")
        result = entry.get("result", "")
        score = entry.get("score", "")
        success = entry.get("success", False)
        llm_method = entry.get("llm_method", "")
        screenshot_path = entry.get("screenshot", "")
        page_from = entry.get("page_from", "?")
        page_to = entry.get("page_to", "")

        from_name = pages_seen.get(page_from, page_from[:6])
        to_name = pages_seen.get(page_to, page_to[:6] if page_to else "?")
        from_color = _page_color(page_from) if page_from else "#666"
        to_color = _page_color(page_to) if page_to else "#666"

        # Before image — look up by round_num to avoid sort-order mismatch
        rn = entry.get("round_num")
        before_b64_str = _b64(before_by_round[rn]) if rn in before_by_round else ""

        # After image
        after_b64_str = ""
        if screenshot_path and Path(screenshot_path).exists():
            after_b64_str = _b64(Path(screenshot_path).read_bytes())

        # Crosshair (percentage positioning)
        crosshair = ""
        if tap_xy:
            left = tap_xy[0] / 10
            top = tap_xy[1] / 10
            crosshair = f"""<div class="crosshair" style="left:{left:.1f}%;top:{top:.1f}%"><div class="ch-h"></div><div class="ch-v"></div><div class="ch-ring"></div></div>"""

        # Connector line (if previous step's page_to == this step's page_from)
        connector = ""
        if prev_to and prev_to == page_from:
            connector = """<div class="connector"></div>"""
        prev_to = page_to

        mark = "✓" if success else ""
        tag = "step-ok" if success else "step-fail"
        score_str = f" <small>({score:.3f})</small>" if isinstance(score, float) else ""
        method_str = f" · {llm_method}" if llm_method else ""

        flow_html += f"""
        {connector}
        <div class="step {tag}" style="--strategy:{color}; --from:{from_color}; --to:{to_color}">
          <div class="step-head">
            <span class="page-badge" style="background:{from_color}">{from_name}</span>
            <span class="step-arrow">─
              <span class="step-strategy">{strategy}</span>
              <span class="step-method">{method_str}</span> ─▶
            </span>
            <span class="page-badge" style="background:{to_color}">{to_name}</span>
            <span class="step-result">{result}{score_str} {mark}</span>
          </div>
          <div class="step-body">
            <div class="ss-wrap">
              <div class="ss-label" style="background:{from_color}">{from_name}</div>
              {"<img src='data:image/png;base64," + before_b64_str + "' loading='lazy'>" if before_b64_str else "<div class='placeholder'>无截图</div>"}
              {crosshair}
            </div>
            <div class="mid-arrow">{mark if mark else "→"}</div>
            <div class="ss-wrap">
              <div class="ss-label" style="background:{to_color}">{to_name}</div>
              {"<img src='data:image/png;base64," + after_b64_str + "' loading='lazy'>" if after_b64_str else "<div class='placeholder'>无截图</div>"}
            </div>
          </div>
        </div>"""

    # Target section
    target_section = ""
    if target_b64:
        target_section = """
        <div class="target-bar">
          <span class="page-badge target-badge">TARGET</span>
          <div class="ss-wrap" style="display:inline-block;vertical-align:top">
            <img src="data:image/png;base64,""" + target_b64 + """" style="height:180px;border-radius:4px">
          </div>
        </div>"""

    # Page index — one thumbnail per unique page
    page_index = ""
    page_first_img = {}  # hash → b64 of first appearance
    for i, entry in enumerate(log):
        pf = entry.get("page_from", "")
        rn = entry.get("round_num")
        if pf and pf not in page_first_img and rn in before_by_round:
            page_first_img[pf] = _b64(before_by_round[rn])
        pt = entry.get("page_to", "")
        if pt and pt not in page_first_img:
            sp = entry.get("screenshot", "")
            if sp and Path(sp).exists():
                page_first_img[pt] = _b64(Path(sp).read_bytes())

    if page_first_img:
        items = []
        for h, name in pages_seen.items():
            img = page_first_img.get(h, "")
            c = _page_color(h)
            img_tag = f"<img src='data:image/png;base64,{img}' style='height:100px;border-radius:4px'>" if img else "<div class='placeholder' style='height:100px;width:50px'>?</div>"
            items.append(f"""<div class="page-item"><div class="page-badge" style="background:{c}">{name}</div><div class="page-hash">{h}</div>{img_tag}</div>""")
        page_index = f"""<div class="page-index">{"  ".join(items)}</div>"""

    html = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>Back-nav trace</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #1a1a2e; color: #eee; margin: 0; padding: 20px; }
  h1 { font-size: 16px; color: #888; margin: 0 0 12px; }
  .page-index { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; padding: 12px; background: #1e1e36; border-radius: 8px; }
  .page-item { text-align: center; }
  .page-hash { font-size: 10px; color: #666; font-family: monospace; }
  .page-badge { display: inline-block; padding: 2px 10px; border-radius: 10px; font-size: 12px; font-weight: 700; color: #fff; margin: 4px 0; }
  .target-badge { background: #34C759; }
  .target-bar { margin-bottom: 12px; padding: 8px 12px; background: #1e3a1e; border-radius: 8px; border-left: 3px solid #34C759; }

  .connector { width: 2px; height: 8px; background: #444; margin-left: 60px; }
  .step { margin-bottom: 2px; border-radius: 8px; background: #252540; border-left: 3px solid var(--strategy); }
  .step-ok { border-left-color: #34C759; }
  .step-head { padding: 6px 12px; background: #1e1e36; display: flex; align-items: center; gap: 8px; font-size: 13px; }
  .step-arrow { color: #aaa; }
  .step-strategy { font-family: monospace; color: var(--strategy); font-weight: 600; }
  .step-method { color: #888; font-size: 12px; }
  .step-result { color: #888; font-size: 12px; margin-left: auto; }
  .step-result small { opacity: 0.6; }
  .step-body { display: flex; align-items: center; gap: 8px; padding: 8px 12px; overflow-x: auto; }

  .ss-wrap { position: relative; flex-shrink: 0; }
  .ss-wrap img { height: 180px; border-radius: 4px; display: block; }
  .ss-label { position: absolute; top: 4px; left: 4px; font-size: 10px; padding: 1px 6px; border-radius: 4px; color: #fff; font-weight: 700; z-index: 2; }
  .placeholder { height: 180px; width: 90px; background: #333; border-radius: 4px; display: flex; align-items: center; justify-content: center; color: #555; font-size: 11px; }
  .mid-arrow { font-size: 20px; color: var(--strategy); flex-shrink: 0; width: 24px; text-align: center; }

  .crosshair { position: absolute; pointer-events: none; z-index: 1; }
  .ch-h, .ch-v { position: absolute; background: rgba(255,255,0,0.8); }
  .ch-h { width: 36px; height: 2px; top: -1px; left: -18px; }
  .ch-v { width: 2px; height: 36px; left: -1px; top: -18px; }
  .ch-ring { position: absolute; width: 14px; height: 14px; border: 2px solid rgba(255,255,0,0.9); border-radius: 50%; top: -7px; left: -7px; }
</style>
</head>
<body>
  <h1>Back-nav trace · """ + str(len(log)) + """ steps</h1>
  """ + page_index + """
  """ + target_section + """
  """ + flow_html + """
</body>
</html>"""

    out_path = out_dir / "nav_trace.html"
    out_path.write_text(html, encoding="utf-8")
    subprocess.Popen(["open", str(out_path)])
    print(f"\n  导航轨迹可视化: {out_path}")


if __name__ == "__main__":
    main()
