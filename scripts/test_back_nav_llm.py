"""Test back-nav LLM prompt with two screenshots.

Usage:
    uv run python scripts/test_back_nav_llm.py <before.png> <after.png> [nav_context]

If no args given, uses logs/test_back_nav/ data automatically.
"""

import io
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont

from policy_expr.recon.back_nav import infer_back_action


def annotate_tap(png: bytes, x: float, y: float, label: str, color: tuple) -> bytes:
    """Draw a crosshair + label on screenshot."""
    img = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img, "RGBA")

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size=18)
    except Exception:
        font = ImageFont.load_default()

    cx = int(x / 1000 * w)
    cy = int(y / 1000 * h)
    R = 14

    # Crosshair
    draw.line([(cx - R * 2, cy), (cx + R * 2, cy)], fill=(*color, 200), width=3)
    draw.line([(cx, cy - R * 2), (cx, cy + R * 2)], fill=(*color, 200), width=3)
    draw.ellipse([cx - R, cy - R, cx + R, cy + R],
                 outline=(*color, 255), width=2)

    # Label
    draw.text((cx + R + 4, cy - 10), label, fill=(*color, 255), font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_comparison(before_png: bytes, after_png: bytes,
                    before_label: str, after_label: str,
                    tap_png: bytes | None = None) -> bytes:
    """Three-column layout: BEFORE | AFTER | TAP RESULT (same height)."""
    b_img = Image.open(io.BytesIO(before_png)).convert("RGB")
    a_img = Image.open(io.BytesIO(after_png)).convert("RGB")

    # Resize all to same height
    h = min(b_img.height, a_img.height, 800)
    imgs = [b_img, a_img]
    if tap_png:
        imgs.append(Image.open(io.BytesIO(tap_png)).convert("RGB"))

    resized = []
    for img in imgs:
        w = int(img.width * h / img.height)
        resized.append(img.resize((w, h)))

    gap = 12
    header_h = 28
    total_w = sum(im.width for im in resized) + gap * (len(resized) + 1)
    total_h = h + header_h + gap * 2

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size=15)
    except Exception:
        font = ImageFont.load_default()

    labels = ["BEFORE (target)", "AFTER (current)", "LLM TAP"]
    colors = [(100, 180, 255), (255, 160, 100), (0, 255, 136)]

    canvas = Image.new("RGB", (total_w, total_h), (30, 30, 30))
    draw = ImageDraw.Draw(canvas)

    x_off = gap
    for i, img in enumerate(resized):
        draw.text((x_off, gap), labels[i], fill=colors[i], font=font)
        canvas.paste(img, (x_off, header_h + gap))
        x_off += img.width + gap

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def main():
    debug_dir = ROOT / "logs" / "test_back_nav_llm"
    debug_dir.mkdir(parents=True, exist_ok=True)

    if len(sys.argv) >= 3:
        before_path, after_path = Path(sys.argv[1]), Path(sys.argv[2])
        nav_context = sys.argv[3] if len(sys.argv) > 3 else ""
    else:
        data_dir = ROOT / "logs" / "test_back_nav"
        if not data_dir.exists():
            print(f"请先运行 test_back_nav.py 生成数据，或手动指定图片路径")
            sys.exit(1)

        before_path = None
        for p in sorted(data_dir.glob("ref_L*_target.png")):
            before_path = p
        if before_path is None:
            refs = sorted(data_dir.glob("ref_L*.png"))
            if refs:
                before_path = refs[0]

        after_path = data_dir / "llm_after.png"
        if not after_path.exists():
            after_path = data_dir / "before_back.png"

        context_path = data_dir / "llm_context.json"
        if context_path.exists():
            import json
            ctx = json.loads(context_path.read_text())
            nav_context = ctx.get("nav_context", "")
        else:
            nav_context = ""

        if before_path is None or not after_path.exists():
            print(f"logs/test_back_nav/ 中未找到所需截图")
            print(f"  需要: ref_L*_target.png + llm_after.png")
            sys.exit(1)

    before_png = before_path.read_bytes()
    after_png = after_path.read_bytes()

    print(f"BEFORE: {before_path.name}")
    print(f"AFTER:  {after_path.name}")
    print(f"nav_context: 「{nav_context}」")
    print()

    action = infer_back_action(before_png, after_png, nav_context=nav_context, debug_dir=debug_dir)

    print()
    if action is None:
        print("结果: LLM 未能识别返回动作")
        tap_png = after_png  # No annotation, just show original
    else:
        print(f"结果: can_go_back={action.can_go_back}")
        print(f"  坐标: ({action.back_x:.0f}, {action.back_y:.0f})")
        print(f"  方法: {action.method}")

        # Annotate tap on after image
        tap_png = annotate_tap(after_png, action.back_x, action.back_y,
                               f"({action.back_x:.0f},{action.back_y:.0f}) {action.method}",
                               (0, 255, 136))

    comp_png = make_comparison(before_png, after_png, "target", "current", tap_png)
    out_path = debug_dir / "result.png"
    out_path.write_bytes(comp_png)

    subprocess.Popen(["open", str(out_path)])
    print(f"\n可视化: {out_path}")


if __name__ == "__main__":
    main()
