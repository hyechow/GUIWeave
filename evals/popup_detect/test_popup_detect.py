"""Popup detection eval: detect_fullscreen_popup + fingerprint form accuracy."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

import numpy as np
from PIL import Image

from policy_expr.adapters.iphone.overlay_detect import detect_fullscreen_popup
from policy_expr.adapters.iphone.recon.cascade_matcher import PageFingerprint, get_matcher  # noqa: F401

CASES_FILE = Path(__file__).parent / "cases.json"
EVAL_DIR = Path(__file__).resolve().parent

passed = 0
failed = 0


def _report(label: str, ok: bool, got, expected, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:52s}  got={str(got):8s}  exp={str(expected):8s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _load_png(path: Path) -> bytes:
    return path.read_bytes()


def _to_rgb(png: bytes) -> np.ndarray:
    return np.array(Image.open(__import__("io").BytesIO(png)).convert("RGB"), dtype=np.float32)


def main() -> None:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    matcher = get_matcher()

    fp_cache: dict[str, "PageFingerprint"] = {}

    print(f"\n=== Popup Detection Eval ({len(cases)} cases) ===\n")

    # ── Part 1: detect_fullscreen_popup ─────────────────────────────────────
    print("── detect_fullscreen_popup (pixel heuristic) ──")
    for c in cases:
        img_path = EVAL_DIR / c["img"]
        if not img_path.exists():
            print(f"  [SKIP] {c['id']}  (file not found: {img_path})")
            continue
        png = _load_png(img_path)
        img = _to_rgb(png)
        h, w = img.shape[:2]
        W = 40
        edge_mean = float(np.mean([img[:W].mean(), img[-W:].mean(),
                                   img[:, :W].mean(), img[:, -W:].mean()]))
        center_mean = float(img[h // 4:3 * h // 4, w // 4:3 * w // 4].mean())
        ratio = edge_mean / max(center_mean, 1.0)
        result = detect_fullscreen_popup(img)
        exp = c["expected_close_popup"]
        ok = result == exp
        _report(c["id"], ok, result, exp, f"ratio={ratio:.3f}")

    # ── Part 2: fingerprint form classification ──────────────────────────────
    print(f"\n── generate_fingerprint form (calls LLM, {len(cases)} cases) ──")
    t_start = time.time()
    for c in cases:
        img_path = EVAL_DIR / c["img"]
        if not img_path.exists():
            print(f"  [SKIP] {c['id']}")
            continue
        key = c["img"]
        if key not in fp_cache:
            png = _load_png(img_path)
            fp_cache[key] = matcher.generate_fingerprint(png)
        fp = fp_cache[key]
        got = fp.form
        exp = c["expected_form"]
        ok = got == exp
        _report(c["id"], ok, got, exp,
                f"content={fp.content}  purpose={fp.purpose[:20]}")
    elapsed = time.time() - t_start
    print(f"  (LLM calls: {elapsed:.1f}s total)")

    # ── Summary ──────────────────────────────────────────────────────────────
    total = passed + failed
    print(f"\n{'─'*60}")
    print(f"  PASSED {passed}/{total}   FAILED {failed}/{total}")
    print(f"{'─'*60}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
