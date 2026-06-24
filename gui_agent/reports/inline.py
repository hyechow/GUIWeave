"""Self-contained report.html: inline screenshots as recompressed base64 data URIs.

A run's ``report.html`` references screenshots by relative path (``src="screenshot_
turn_3.png"``), so it only renders next to its image files. This rewrites those
references into ``data:image/jpeg;base64,…`` URIs — producing ONE self-contained
file that renders anywhere (and is small enough to commit) without the loose images.

Images are recompressed (WebP by default — ~50-60% smaller than JPEG on UI
screenshots) and width-capped (default 640px) so the inlined file stays small
enough to commit; WebP data URIs render in every modern browser. Run as a CLI:

    uv run python -m gui_agent.reports.inline <run_dir> [-o out.html] \
        [--format webp|jpeg] [--max-width N] [--quality Q]
"""

from __future__ import annotations

import argparse
import base64
import io
import re
import sys
from pathlib import Path

# Only ``src="…png|jpg|jpeg|webp"`` refs appear in the report (no href full-res, no
# CSS url()), so this single pattern covers every image the report renders.
_IMG_SRC = re.compile(r'src="([^"]+\.(?:png|jpe?g|webp))"', re.IGNORECASE)


# PIL save-format → (data-URI mime, extra save kwargs). WebP method=6 = slowest/
# smallest encode; JPEG optimize=True = re-pack Huffman tables.
_FORMATS = {
    "webp": ("image/webp", {"method": 6}),
    "jpeg": ("image/jpeg", {"optimize": True}),
}


def _encode_image(path: Path, max_width: int, quality: int, fmt: str) -> str | None:
    """Read an image, optionally downscale, return a base64 data URI (or None)."""
    try:
        from PIL import Image

        im = Image.open(path).convert("RGB")
    except Exception:  # noqa: BLE001 — missing/corrupt image: leave the ref untouched
        return None
    if max_width and im.width > max_width:
        height = round(im.height * max_width / im.width)
        im = im.resize((max_width, height), Image.Resampling.LANCZOS)
    mime, extra = _FORMATS[fmt]
    buf = io.BytesIO()
    im.save(buf, format=fmt.upper(), quality=quality, **extra)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def inline_report(
    run_dir: str | Path,
    out_path: str | Path | None = None,
    *,
    fmt: str = "webp",
    max_width: int = 640,
    quality: int = 60,
) -> Path:
    """Rewrite ``run_dir/report.html`` image refs to inlined data URIs.

    Writes to ``out_path`` (default: overwrite the run's report.html) and returns it.
    Missing images keep their original ref so the file degrades gracefully.
    """
    run_dir = Path(run_dir)
    src_html = run_dir / "report.html"
    html = src_html.read_text(encoding="utf-8")

    cache: dict[str, str] = {}

    def _repl(match: re.Match) -> str:
        ref = match.group(1)
        if ref.startswith("data:"):
            return match.group(0)
        if ref not in cache:
            img_path = run_dir / ref
            uri = _encode_image(img_path, max_width, quality, fmt) if img_path.exists() else None
            cache[ref] = uri or ref  # fall back to the original ref when unreadable
        return f'src="{cache[ref]}"'

    new_html = _IMG_SRC.sub(_repl, html)
    out = Path(out_path) if out_path else src_html
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(new_html, encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inline a report.html's screenshots as base64")
    parser.add_argument("run_dir", help="run directory containing report.html + screenshots")
    parser.add_argument("-o", "--out", help="output path (default: overwrite run_dir/report.html)")
    parser.add_argument("--format", choices=sorted(_FORMATS), default="webp", help="inline image codec (default webp)")
    parser.add_argument("--max-width", type=int, default=640, help="downscale wider images to this px (0 = keep)")
    parser.add_argument("--quality", type=int, default=60, help="codec quality 1-95 (default 60)")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    if not (run_dir / "report.html").exists():
        print(f"[inline] no report.html in {run_dir}", file=sys.stderr)
        return 1
    out = inline_report(run_dir, args.out, fmt=args.format, max_width=args.max_width, quality=args.quality)
    kb = out.stat().st_size / 1024
    print(f"[inline] {out}  ({kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
