"""Snap eval: validate the YOLO/OCR coordinate-snap strategy.

Two layers:
  1. Screenshot cases (cases.json) — run the real ActionExecutor._snap (YOLO +
     OCR arbitration) on labeled screenshots and assert the snap method and the
     snapped coordinate (within tolerance). Screenshots are gitignored, so cases
     SKIP when the image is absent.
  2. Synthetic geometric regressions — drive YoloCalibrator.nearest() with
     hand-built boxes to lock the tiering invariants (no screenshot, no LLM).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from policy_expr.executor import ActionExecutor
from policy_expr.recon.icon_detector import IconBbox
from policy_expr.recon.yolo_calibrator import YoloCalibrator
from policy_expr.schemas import Action

CASES_FILE = Path(__file__).parent / "cases.json"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

passed = 0
failed = 0


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:42s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _dist(a: tuple[float, float], b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def test_screenshot_cases() -> int:
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    skipped = 0
    for c in cases:
        path = PROJECT_ROOT / c["screenshot"]
        if not path.exists():
            print(f"  [SKIP] {c['label']:42s}  screenshot not found: {c['screenshot']}")
            skipped += 1
            continue
        png = path.read_bytes()
        cal = YoloCalibrator.from_png(png, conf=0.1)
        ex = ActionExecutor(phone=None, calibrator=cal)  # type: ignore[arg-type]
        tx, ty = c["target"]
        hint = c.get("hint", "")
        act = Action(action_type="tap", x=tx, y=ty, description=hint)
        try:
            sx, sy = ex._snap(tx, ty, png_bytes=png, hint=hint, action=act)
        except Exception as e:
            _report(c["label"], False, f"exception: {e}")
            continue

        exp = c["expected"]
        meta = act.snap or {}
        method = meta.get("method", "none")
        details = []
        if "method" in exp and method != exp["method"]:
            details.append(f"method: expected {exp['method']}, got {method}")
        if "snapped_near" in exp:
            tol = exp.get("tolerance", 30.0)
            d = _dist((sx, sy), exp["snapped_near"])
            if d > tol:
                details.append(f"snapped {(round(sx), round(sy))} is {d:.0f} from "
                               f"{exp['snapped_near']} (tol {tol:.0f})")
        ok = not details
        _report(c["label"], ok, "; ".join(details))
    if skipped:
        print(f"  ({skipped} skipped — screenshots not committed to git)")
    return skipped


def test_geometric_regressions() -> None:
    """Tiering invariants on YoloCalibrator.nearest(), no screenshot needed."""

    # 1. Small high-conf icon sitting inside a large button: a point inside ONLY
    #    the big button must snap to the big button, not be stolen by the icon;
    #    a point inside BOTH snaps to the tightest (the icon).
    big = IconBbox(100, 470, 500, 530, 0.60)   # center 300
    icon = IconBbox(440, 485, 480, 515, 0.80)  # center 460
    cal = YoloCalibrator([big, icon], img_w=1000, img_h=1000)
    r = cal.nearest(400, 500)
    _report("synthetic: point in big-button only → big",
            r is not None and abs(r[0] - 300) < 5,
            "" if (r and abs(r[0] - 300) < 5) else f"got {r}")
    r = cal.nearest(450, 500)
    _report("synthetic: point in both → tightest(icon)",
            r is not None and abs(r[0] - 460) < 5,
            "" if (r and abs(r[0] - 460) < 5) else f"got {r}")

    # 2. Wide low-conf misdetection containing the point must NOT hijack the snap;
    #    the correct tight high-conf box wins by distance via the margin tier.
    #    (geometry mirrors the real 「我的」tab failure: target 890,850)
    wide_bad = IconBbox(505, 842, 958, 901, 0.28)   # center 731, spans half-width
    correct = IconBbox(813, 895, 943, 961, 0.72)    # center 878 (the 我的 tab)
    cal2 = YoloCalibrator([wide_bad, correct], img_w=1000, img_h=1000)
    r = cal2.nearest(890, 850)
    ok = r is not None and abs(r[0] - 878) < 10
    _report("synthetic: wide low-conf misdetect rejected",
            ok, "" if ok else f"got {r} (expected near 878, not 731)")

    # 3. Confidence floor boundary: a low-conf box that contains the point and is
    #    also the closest center still loses containment, but can win by distance.
    only_low = IconBbox(800, 800, 980, 900, 0.30)   # center 890, contains 890,850
    cal3 = YoloCalibrator([only_low], img_w=1000, img_h=1000)
    r = cal3.nearest(890, 850)
    #    below conf floor → not a real_hit, but center dist 50 → margin tier rescues
    _report("synthetic: lone low-conf box still snaps via margin",
            r is not None and abs(r[0] - 890) < 5,
            "" if (r and abs(r[0] - 890) < 5) else f"got {r}")

    # 4. contains(): point inside a confident box → True (OCR override suppressed);
    #    point outside all boxes / inside only a weak box → False (OCR may run).
    icon = IconBbox(494, 570, 723, 669, 0.49)        # a home-screen app icon
    cal4 = YoloCalibrator([icon], img_w=1000, img_h=1000)
    _report("synthetic: contains() true inside confident icon",
            cal4.contains(609, 617) is True,
            "" if cal4.contains(609, 617) else "expected True")
    weak = IconBbox(505, 842, 958, 901, 0.28)        # wide low-conf misdetect
    cal5 = YoloCalibrator([weak], img_w=1000, img_h=1000)
    _report("synthetic: contains() false for weak box / outside",
            cal5.contains(890, 850) is False and cal4.contains(100, 100) is False,
            "" if (not cal5.contains(890, 850) and not cal4.contains(100, 100)) else "expected False")


def main():
    print("── Snap Eval ──")
    print(" Screenshot cases:")
    test_screenshot_cases()
    print(" Geometric regressions:")
    test_geometric_regressions()
    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
