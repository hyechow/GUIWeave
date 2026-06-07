"""Manual DFS test: step-by-step DFS exploration with user control.

Usage:
    uv run python scripts/test_dfs_manual.py [--max-depth 3]

Flow:
    1. Connect to phone, take root screenshot.
    2. Show numbered tap targets; user picks which element to tap.
    3. Detect navigation; if navigated → recurse into child page.
    4. At any point user can: tap next element, go back, or quit.
    5. Dedup (PageIdentity) flags already-visited pages.
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from PIL import Image

from _vis import open_annotated, parse_items, print_items

from policy_expr.adapters.iphone.perception import LivePhoneSession
from policy_expr.adapters.iphone.executor import logical_xy
from policy_expr.recon.back_nav import return_to_initial, BACK_SETTLE_SECONDS, make_nav_context
from policy_expr.recon.page_parser import PageParser
from policy_expr.recon.page_identity import PageIdentity
from policy_expr.recon.cascade_matcher import get_matcher

SETTLE = BACK_SETTLE_SECONDS


def pixel_diff_ratio(png_a: bytes, png_b: bytes, threshold: int = 30) -> float:
    """Return ratio of pixels that differ between two PNGs. 0.0 = identical."""
    import numpy as np
    a = np.array(Image.open(io.BytesIO(png_a)).convert("RGB"))
    b = np.array(Image.open(io.BytesIO(png_b)).convert("RGB"))
    if a.shape != b.shape:
        return 1.0
    diff = np.abs(a.astype(int) - b.astype(int)).max(axis=2)
    return float((diff > threshold).sum()) / diff.size


# Pixel diff > 1% means page changed
_NAV_THRESHOLD = 0.01


# ── User interaction ──────────────────────────────────────────────────────────

def prompt_action(n_items: int, depth: int) -> str:
    """Return one of: tap_index, 'back', 'quit'."""
    while True:
        raw = input(f"\n  [L{depth}] 选元素(0-{n_items-1} / b=返回上级 / q=退出): ").strip().lower()
        if raw == "q":
            return "quit"
        if raw == "b":
            return "back"
        if raw.isdigit() and 0 <= int(raw) < n_items:
            return raw
        print(f"  无效输入")


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Manual DFS test")
    ap.add_argument("--max-depth", type=int, default=3)
    args = ap.parse_args()

    parser = PageParser()
    dedup = PageIdentity(get_matcher())

    out_dir = ROOT / "logs" / "dfs_manual"
    if out_dir.exists():
        import shutil
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    with LivePhoneSession() as phone:
        screenshot = phone.screenshot
        client = phone.client
        assert client is not None

        def explore(depth: int, nav_stack: list, page_name: str = "root") -> None:
            """Interactive DFS at current depth. PRE/POST: phone on this page."""
            prefix = "  " * depth
            png_bytes = screenshot()

            # ── Overlay detection (前置于去重，命中则强制新页面) ──
            _overlay = None
            if len(nav_stack) >= 2:
                import numpy as np
                from PIL import Image as _PIL
                from policy_expr.adapters.iphone.overlay_detect import detect_overlay
                _before = nav_stack[-2][0]
                def _b2rgb(b: bytes):
                    return np.array(_PIL.open(io.BytesIO(b)).convert("RGB"))
                _r = detect_overlay(_b2rgb(_before), _b2rgb(png_bytes))
                if _r.type != "none":
                    _overlay = _r
                    print(f"\n{prefix}[L{depth}] 🔲 overlay={_overlay.type}  bbox={_overlay.bbox}  → 强制新页面")

            # Dedup check
            candidate_emb = get_matcher().embed_visual(png_bytes)
            dedup_result = dedup.check(png_bytes, precomputed=candidate_emb)

            if dedup_result.is_duplicate and not _overlay:
                matched = dedup_result.matched_name or "?"
                print(f"\n{prefix}[L{depth}] ⊘ 已访问过的页面「{matched}」"
                      f" (phase={dedup_result.phase}, vis={dedup_result.best_visual_sim:.3f})")
                return

            # New page
            # knowledge = parser.analyze_screen(png_bytes)  # unused, parse_items does its own call
            fingerprint = get_matcher()._generate_fingerprint(png_bytes)
            for line in fingerprint.splitlines():
                if line.startswith("用途：") or line.startswith("用途:"):
                    desc = line.split("：", 1)[-1].split(":", 1)[-1].strip()
                    break
            else:
                desc = fingerprint[:20].strip()
            name = desc[:20].replace("/", "-").replace("\\", "-")
            dedup.add(png_bytes, name=name, emb=candidate_emb)
            print(f"\n{prefix}[L{depth}] ★ 新页面「{desc}」(library={len(dedup)})")

            # Save screenshot
            page_dir = out_dir / f"L{depth}_{name}"
            page_dir.mkdir(exist_ok=True)
            (page_dir / "initial.png").write_bytes(png_bytes)

            # Parse tappable elements
            if _overlay and _overlay.bbox:
                # 裁剪到弹窗区域后再解析，避免 LLM 把背景元素误识别为弹窗内容
                from PIL import Image as _PIL
                ox1, oy1, ox2, oy2 = _overlay.bbox
                _full_img = _PIL.open(io.BytesIO(png_bytes))
                _W, _H = _full_img.size
                _crop = _full_img.crop((ox1, oy1, ox2, oy2))
                _buf = io.BytesIO(); _crop.save(_buf, format="PNG")
                items = parse_items(_buf.getvalue(), parser, filter_back=True)
                # crop-relative 0-1000 → full-image 0-1000
                items = [
                    (
                        (ox1 + ax / 1000 * (ox2 - ox1)) / _W * 1000,
                        (oy1 + ay / 1000 * (oy2 - oy1)) / _H * 1000,
                        label, etype,
                    )
                    for ax, ay, label, etype in items
                ]
                print(f"{prefix}  [overlay] 裁剪解析，共 {len(items)} 个元素")
            else:
                items = parse_items(png_bytes, parser, filter_back=True)
            if not items:
                print(f"{prefix}  无可交互元素")
                return

            visited: set[int] = set()

            while True:
                img_path = open_annotated(png_bytes, items, f"dfs_d{depth}",
                                          visited, zoom_bbox=_overlay.bbox if _overlay else None)
                print(f"{prefix}  标注图: {img_path}")
                print_items(items, visited)

                action = prompt_action(len(items), depth)

                if action == "quit":
                    print(f"{prefix}  退出")
                    raise SystemExit(0)

                if action == "back":
                    print(f"{prefix}  返回上级")
                    return

                # Tap element
                idx = int(action)
                visited.add(idx)
                ax, ay, label, etype = items[idx]
                lx, ly = logical_xy(ax, ay)
                _nav_ctx = make_nav_context(label, etype)
                print(f"{prefix}  → tap [{label}] ({etype}) at ({ax:.0f},{ay:.0f})")
                client.tap(lx, ly)
                time.sleep(SETTLE)
                after_bytes = screenshot()

                # Detect navigation via pixel diff
                diff_ratio = pixel_diff_ratio(png_bytes, after_bytes)
                unchanged = diff_ratio < _NAV_THRESHOLD
                if unchanged:
                    print(f"{prefix}    页面未变化 (pixel_diff={diff_ratio:.4f})")
                    continue

                print(f"{prefix}    ✓ 页面跳转 (pixel_diff={diff_ratio:.4f})")
                safe_label = label.replace("/", "-").replace("\\", "-")
                (page_dir / f"tap_{idx:02d}_{safe_label[:10]}.png").write_bytes(after_bytes)

                # Check depth limit
                if depth >= args.max_depth:
                    print(f"{prefix}    已达最大深度 {args.max_depth}，不进入子页面")
                    # Go back
                    ok, back_log = return_to_initial(
                        client, screenshot, nav_stack,
                        before_back_bytes=after_bytes,
                        nav_context=_nav_ctx,
                    )
                    if ok:
                        print(f"{prefix}    ✓ 已返回当前页")
                    else:
                        print(f"{prefix}    ✗ 返回失败，请手动恢复后继续")
                    continue

                # Enter child page
                parent_bytes, _ = nav_stack[-1]
                child_nav_stack = nav_stack[:-1] + [(parent_bytes, (lx, ly)), (after_bytes, None)]

                print(f"\n{'='*60}")
                print(f"{prefix}  ↓ 进入子页面 (depth {depth+1})")
                print(f"{'='*60}")

                explore(depth + 1, child_nav_stack, page_name=f"{name}/{label}")

                # Return to current page
                print(f"\n{'='*60}")
                print(f"{prefix}  ↑ 返回 L{depth}「{desc[:20]}」")
                print(f"{'='*60}")

                ok, back_log = return_to_initial(
                    client, screenshot, nav_stack,
                    before_back_bytes=screenshot(),
                    nav_context=_nav_ctx,
                )
                if ok:
                    print(f"{prefix}  ✓ 已返回")
                else:
                    print(f"{prefix}  ✗ 返回失败，请手动操作使手机回到当前页面后继续")
                    input(f"{prefix}  按回车继续...")

                # Refresh current page state (page may have changed slightly)
                png_bytes = screenshot()

        # ── Start ──
        root_png = screenshot()
        nav_stack: list[tuple[bytes, tuple[float, float] | None]] = [(root_png, None)]

        print(f"\n{'='*60}")
        print("  手动 DFS 测试")
        print(f"  max_depth={args.max_depth}  输出目录={out_dir}")
        print(f"{'='*60}")

        try:
            explore(0, nav_stack)
        except SystemExit:
            pass

        # Summary
        print(f"\n{'='*60}")
        print(f"  探索结束，共发现 {len(dedup)} 个不同页面")
        print(f"  输出目录: {out_dir}")
        for i, (_, _, name) in enumerate(dedup._library):
            print(f"    [{i}] {name}")
        print(f"{'='*60}")


if __name__ == "__main__":
    main()
