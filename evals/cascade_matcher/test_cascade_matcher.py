"""Recon eval runner: CascadeMatcher fingerprint accuracy + PageIdentity dedup."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from policy_expr.adapters.iphone.recon.cascade_matcher import CascadeMatcher
from policy_expr.adapters.iphone.recon.page_identity import PageIdentity

CASES_FILE = Path(__file__).parent / "cases.json"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
EVAL_DIR = Path(__file__).resolve().parent
RECON_LOG_DIR = EVAL_DIR / "data"

passed = 0
failed = 0
fp_cache: dict = {}


def _report(label: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    passed += ok
    failed += not ok
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {label:55s}"
    if detail:
        line += f"  {detail}"
    print(line)


def _get_fp(matcher: CascadeMatcher, screenshot: str):
    if screenshot not in fp_cache:
        png = (PROJECT_ROOT / screenshot).read_bytes()
        fp_cache[screenshot] = matcher.generate_fingerprint(png)
    return fp_cache[screenshot]


def _get_fp_safe(matcher: CascadeMatcher, screenshot: str, label: str):
    try:
        return _get_fp(matcher, screenshot)
    except Exception as e:
        _report(label, False, f"exception generating fingerprint: {e}")
        return None


def test_fingerprint(matcher: CascadeMatcher, cases: list) -> None:
    print("\n── Fingerprint ──")
    form_correct = 0
    content_correct = 0
    n = len(cases)
    latencies = []

    for c in cases:
        png = (PROJECT_ROOT / c["screenshot"]).read_bytes()
        t0 = time.time()
        try:
            fp = matcher.generate_fingerprint(png)
        except Exception as e:
            _report(c["label"], False, f"exception: {e}")
            continue
        latencies.append(time.time() - t0)

        expected = c["expected"]
        details = []
        if fp.form == expected["form"]:
            form_correct += 1
        else:
            details.append(f"form: expected {expected['form']!r}, got {fp.form!r}")
        if "content" in expected:
            if fp.content == expected["content"]:
                content_correct += 1
            else:
                details.append(f"content: expected {expected['content']!r}, got {fp.content!r}")

        ok = len(details) == 0
        extra = f"→ form={fp.form} content={fp.content} purpose={fp.purpose[:20]}"
        _report(c["label"], ok, ("; ".join(details) + "  " + extra) if details else extra)

    content_cases = sum(1 for c in cases if "content" in c["expected"])
    avg_lat = sum(latencies) / len(latencies) if latencies else 0.0
    print(
        f"\n  form accuracy: {form_correct}/{n} ({100*form_correct//n}%)"
        f"  content accuracy: {content_correct}/{content_cases} ({100*content_correct//content_cases if content_cases else 0}%)"
        f"  avg latency: {avg_lat:.1f}s"
    )


def test_similarity(matcher: CascadeMatcher, cases: list) -> None:
    print("\n── Similarity ──")
    tp = tn = fp = fn = 0

    for c in cases:
        fp1 = _get_fp_safe(matcher, c["screenshot_a"], c["label"])
        fp2 = _get_fp_safe(matcher, c["screenshot_b"], c["label"])
        if fp1 is None or fp2 is None:
            continue
        try:
            is_same, reason = matcher.semantic_sim(fp1, fp2)
        except Exception as e:
            _report(c["label"], False, f"exception: {e}")
            continue

        expected_same = c["expected"]["is_same"]
        ok = is_same == expected_same
        if is_same and expected_same:
            tp += 1
        elif not is_same and not expected_same:
            tn += 1
        elif is_same and not expected_same:
            fp += 1
        else:
            fn += 1
        _report(c["label"], ok, f"{'same' if is_same else 'diff'} | {reason}")

    total = tp + tn + fp + fn
    precision = tp / (tp + fp) if (tp + fp) > 0 else float("nan")
    recall = tp / (tp + fn) if (tp + fn) > 0 else float("nan")
    print(
        f"\n  confusion: TP={tp} TN={tn} FP={fp} FN={fn} / {total}"
        f"  precision={precision:.2f}  recall={recall:.2f}"
    )


def test_dedup(matcher: CascadeMatcher) -> None:
    print("\n── Dedup ──")

    new_total = new_correct = 0
    dup_total = dup_correct = 0
    phase_counts: dict = {}
    same_page_sims: list = []
    diff_page_sims: list = []

    for app_dir in sorted(RECON_LOG_DIR.iterdir()):
        if not app_dir.is_dir():
            continue
        trace_file = app_dir / "trace.json"
        if not trace_file.exists():
            continue

        trace_pages = json.loads(trace_file.read_text(encoding="utf-8"))["pages"]
        pages = [p for p in trace_pages if (app_dir / p["page"] / "initial.png").exists()]
        if not pages:
            continue

        app_name = app_dir.name
        identity = PageIdentity(matcher)
        page_pngs: list[tuple[str, bytes]] = []
        app_new_correct = app_new_total = app_dup_correct = app_dup_total = 0
        app_phases: dict = {}

        # Phase 1: new page detection — each page should NOT be a duplicate
        for p in pages:
            png = (app_dir / p["page"] / "initial.png").read_bytes()
            page_pngs.append((p["page"], png))
            label = f"{app_name}/{p['page'][:20]}-新页应不重复"
            try:
                result = identity.check_and_add(png, name=p["page"])
            except Exception as e:
                _report(label, False, f"exception: {e}")
                app_new_total += 1
                continue
            ok = not result.is_duplicate
            app_new_total += 1
            app_new_correct += ok
            _report(label, ok, f"phase={result.phase} vis={result.best_visual_sim:.3f}")

        # Phase 2: duplicate detection — re-checking each page should find a match
        for page_name, png in page_pngs:
            label = f"{app_name}/{page_name[:20]}-重复应被识别"
            try:
                result = identity.check(png)
            except Exception as e:
                _report(label, False, f"exception: {e}")
                app_dup_total += 1
                continue
            ok = result.is_duplicate
            app_dup_total += 1
            app_dup_correct += ok
            app_phases[result.phase] = app_phases.get(result.phase, 0) + 1
            phase_counts[result.phase] = phase_counts.get(result.phase, 0) + 1
            if result.phase == "visual_shortcut":
                same_page_sims.append(result.best_visual_sim)
            _report(label, ok, f"phase={result.phase} vis={result.best_visual_sim:.3f}")

        # Pairwise visual_sim between all different-page pairs within this app
        embeddings = [matcher.embed_visual(png) for _, png in page_pngs]
        for i in range(len(embeddings)):
            for j in range(i + 1, len(embeddings)):
                sim = matcher.visual_sim(embeddings[i], embeddings[j])
                diff_page_sims.append(sim)

        new_total += app_new_total
        new_correct += app_new_correct
        dup_total += app_dup_total
        dup_correct += app_dup_correct
        print(
            f"  [{app_name}] new={app_new_correct}/{app_new_total}"
            f"  dup={app_dup_correct}/{app_dup_total}"
            f"  phases={app_phases}"
        )

    # Visual sim statistics
    if same_page_sims:
        avg_same = sum(same_page_sims) / len(same_page_sims)
        min_same = min(same_page_sims)
        print(f"\n  same-page visual_sim:  avg={avg_same:.3f}  min={min_same:.3f}  (n={len(same_page_sims)})")
    if diff_page_sims:
        avg_diff = sum(diff_page_sims) / len(diff_page_sims)
        max_diff = max(diff_page_sims)
        flag = "  *** FALSE POSITIVE RISK ***" if max_diff > 0.90 else ""
        print(f"  diff-page visual_sim:  avg={avg_diff:.3f}  max={max_diff:.3f}  (n={len(diff_page_sims)}){flag}")

    print(f"\n  new_correct={new_correct}/{new_total}  dup_correct={dup_correct}/{dup_total}")


# Tap destinations that are intra-page state changes rather than new pages:
# tab switches, input-mode toggles, transient overlays. Confirmed by visual
# inspection — screenshot content is the same logical page as their parent.
SAME_PAGE_VARIANTS: dict[str, set[str]] = {
    "微信": {
        "查看与特定联系人的聊天历史",           # 聊天页底部栏切换为语音输入模式
        "查看与联系人的即时通讯记录",            # 同上
        "浏览微信游戏平台的推荐内容与功能菜单",   # 游戏列表页弹出小型下拉菜单
    },
    "拼多多": {
        "浏览促销会场并选购商品",              # 超级满减页切到「精选」tab
        "参与电商促销活动的领券与选购",         # 同页切到「多人团」tab
    },
}


def test_dedup_taps(matcher: CascadeMatcher) -> None:
    """Extended dedup test using tap screenshots from recon_result.json.

    For each app:
      1. Build PageIdentity from initial.png set (known pages).
      2. For each tap where navigated=True and tap_ok=True:
         - destination in known set or SAME_PAGE_VARIANTS → expect DUP
         - destination not in known set → expect NEW, then add to library
         - destination unknown (old DUP with blank page_name) → skip
    """
    print("\n── Dedup (tap screenshots) ──")

    new_total = new_correct = 0
    dup_total = dup_correct = 0
    skipped = 0
    phase_counts: dict = {}

    for app_dir in sorted(RECON_LOG_DIR.iterdir()):
        if not app_dir.is_dir():
            continue
        trace_file = app_dir / "trace.json"
        if not trace_file.exists():
            continue

        trace_pages = json.loads(trace_file.read_text(encoding="utf-8"))["pages"]
        initial_pages = [p["page"] for p in trace_pages
                         if (app_dir / p["page"] / "initial.png").exists()]
        if not initial_pages:
            continue

        app_name = app_dir.name
        known_names = set(initial_pages) | SAME_PAGE_VARIANTS.get(app_name, set())
        identity = PageIdentity(matcher)

        # Seed library with all initial.png pages
        for name in initial_pages:
            identity.add((app_dir / name / "initial.png").read_bytes(), name=name)

        app_new_correct = app_new_total = app_dup_correct = app_dup_total = app_skip = 0

        # Walk all recon_result.json files for this app
        for result_file in sorted(app_dir.rglob("recon_result.json")):
            d = json.loads(result_file.read_text(encoding="utf-8"))
            parent_page = d.get("parent_page", result_file.parent.name)

            for tap in d.get("taps", []):
                if not tap.get("tap_ok") or not tap.get("navigated"):
                    continue
                shot_path = Path(tap.get("screenshot", ""))
                # Remap absolute path to data dir
                try:
                    rel = shot_path.relative_to(Path("logs/recon").resolve())
                    shot_path = app_dir / rel
                except ValueError:
                    shot_path = Path(tap["screenshot"])
                if not shot_path.exists():
                    # Try local data path directly
                    shot_path = EVAL_DIR / "data" / app_name / result_file.parent.name / "tap" / shot_path.name
                if not shot_path.exists():
                    app_skip += 1
                    continue

                dst_name = tap.get("identity", {}).get("page_name", "").strip()

                if dst_name == "":
                    # Old DUP with no page_name recorded — skip
                    app_skip += 1
                    continue

                png = shot_path.read_bytes()
                expect_dup = dst_name in known_names
                label_short = f"{app_name}/{parent_page[:15]}→{dst_name[:15]}"

                try:
                    result = identity.check(png)
                except Exception as e:
                    _report(label_short, False, f"exception: {e}")
                    (app_dup_total if expect_dup else app_new_total).__add__(1)
                    continue

                if expect_dup:
                    ok = result.is_duplicate
                    app_dup_total += 1
                    app_dup_correct += ok
                    dup_total += 1
                    dup_correct += ok
                else:
                    ok = not result.is_duplicate
                    app_new_total += 1
                    app_new_correct += ok
                    new_total += 1
                    new_correct += ok
                    if ok:
                        identity.add(png, name=dst_name)
                        known_names.add(dst_name)

                phase_counts[result.phase] = phase_counts.get(result.phase, 0) + 1
                _report(
                    label_short, ok,
                    f"{'dup' if expect_dup else 'new'} expect | phase={result.phase}"
                    f" vis={result.best_visual_sim:.3f}",
                )

        skipped += app_skip
        print(
            f"  [{app_name}] new={app_new_correct}/{app_new_total}"
            f"  dup={app_dup_correct}/{app_dup_total}"
            f"  skipped={app_skip}"
        )

    print(f"\n  new_correct={new_correct}/{new_total}  dup_correct={dup_correct}/{dup_total}"
          f"  skipped={skipped}  phases={phase_counts}")


def main():
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    fp_cases = [c for c in cases if c["group"] == "fingerprint"]
    sim_cases = [c for c in cases if c["group"] == "similarity"]

    print("── CascadeMatcher Recon Eval ──")

    matcher = CascadeMatcher()
    test_fingerprint(matcher, fp_cases)
    test_similarity(matcher, sim_cases)
    test_dedup(matcher)
    test_dedup_taps(matcher)

    print(f"\n{passed + failed} tests: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
