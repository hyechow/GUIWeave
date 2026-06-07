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

from policy_expr.adapters.iphone.executor import ActionExecutor
from policy_expr.adapters.iphone.recon.icon_detector import IconBbox
from policy_expr.adapters.iphone.recon.yolo_calibrator import YoloCalibrator
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
            sx, sy = ex._snap(tx, ty, png_bytes=png, hint=hint, action=act,
                              is_home_screen=c.get("is_home", False))
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

    # 4. Snap cap: a full-width banner containing a top-left point must not snap
    #    to its far-off center; a closer margin candidate wins instead.
    banner = IconBbox(61, 166, 940, 343, 0.56)       # center ~500,255, width ~880
    arrow = IconBbox(28, 115, 127, 162, 0.56)        # the real back arrow ~77,138
    cal6 = YoloCalibrator([banner, arrow], img_w=1000, img_h=1000)
    r = cal6.nearest(80, 200)                        # point below arrow, inside banner
    ok = r is not None and abs(r[0] - 77) < 15 and abs(r[1] - 138) < 15
    _report("synthetic: wide banner over snap-cap rejected",
            ok, "" if ok else f"got {r} (expected back arrow ~77,138, not banner center)")

    # 5. Home-screen search capsule regression (agent-loop/20260603_173539 turn1):
    #    LLM correctly pointed to the search capsule at (500, 821). The nearest YOLO
    #    icon (dock Mail icon) sits at (397, 915) — distance 139.9 units. Dock icons
    #    are large (~250 normalized units wide); the expanded bbox reaches the capsule
    #    area and triggers a Tier-3 margin hit.
    #    With the home-screen cap of 80 the snap must be blocked (None returned).
    #    With old cap=150 the dock icon center (dist 139.9) passes the filter and
    #    wins via the margin tier (bbox expanded by +50 covers y=821).
    # Dock icon is wide — use realistic size (~250 units wide, 220 tall)
    mail_dock = IconBbox(272, 790, 522, 1000, 0.82)  # center ≈ (397, 895), dist≈140
    cal7 = YoloCalibrator([mail_dock], img_w=1000, img_h=1000)
    # Old behaviour (max_snap_move=150): distance 139.9 < 150, Tier-1 containment fires
    r_old = cal7.nearest(500, 821, max_snap_move=150.0)
    _report("synthetic: home-screen search capsule — old cap=150 snaps to dock icon",
            r_old is not None,
            "" if r_old is not None else "unexpected: old cap=150 should have snapped")
    # New behaviour (max_snap_move=80): center dist 139.9 > 80 → blocked → None
    r_new = cal7.nearest(500, 821, max_snap_move=80.0)
    _report("synthetic: home-screen search capsule — new cap=80 blocks distant dock icon",
            r_new is None,
            "" if r_new is None else f"snapped to {r_new} (dist ~140 should exceed cap=80)")


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
