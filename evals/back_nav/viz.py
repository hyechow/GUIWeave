"""Visualize back_nav eval cases + LLM test results as a static HTML report.

Usage:
    uv run python evals/back_nav/viz.py             # run LLM + open in browser
    uv run python evals/back_nav/viz.py --no-llm    # skip LLM, screenshots only
    uv run python evals/back_nav/viz.py --no-open   # write file without opening
"""

from __future__ import annotations

import base64
import html as _html
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

from policy_expr.adapters.iphone.recon.back_nav import infer_back_action

CASES_FILE = Path(__file__).parent / "cases.json"
EVAL_DIR   = Path(__file__).resolve().parent
OUT_HTML   = EVAL_DIR / "report.html"

TYPE_COLOR = {"A": "#ff9f0a", "B": "#30d158", "C": "#0a84ff"}
ZONE_LABEL = {
    "bottom_tab":  "底部 tab",
    "top_tab":     "顶部 tab",
    "back_btn":    "左上角返回",
    "popup_close": "弹窗关闭",
}


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


def _yolo_boxes(png_bytes: bytes) -> list[dict]:
    try:
        from policy_expr.adapters.iphone.recon.yolo_calibrator import YoloCalibrator
        cal = YoloCalibrator.from_png(png_bytes)
        if cal is None:
            return []
        return [
            {
                "x1": b.x1 / cal.img_w * 100,
                "y1": b.y1 / cal.img_h * 100,
                "x2": b.x2 / cal.img_w * 100,
                "y2": b.y2 / cal.img_h * 100,
                "nx": b.cx / cal.img_w * 1000,
                "ny": b.cy / cal.img_h * 1000,
                "conf": round(float(b.conf), 2),
                "top_left": (b.cx / cal.img_w * 1000) < 150 and (b.cy / cal.img_h * 1000) < 200,
            }
            for b in cal.boxes
        ]
    except Exception:
        return []


def _check_y_zone(bx: float, by: float, zone: str) -> bool:
    if zone == "bottom_tab":  return by > 800
    if zone == "top_tab":     return 50 < by < 600
    if zone == "back_btn":    return bx < 250 and by < 300
    if zone == "popup_close": return bx > 0 and by > 0
    return True


def _crosshair(bx: float, by: float, ok: bool) -> str:
    """Return HTML for a tap-point crosshair overlay at normalized (bx, by)."""
    left, top = bx / 10, by / 10
    color = "#34c759" if ok else "#ff3b30"
    return f"""
<div class="crosshair" style="left:{left:.2f}%;top:{top:.2f}%">
  <div class="ch-h" style="background:{color}"></div>
  <div class="ch-v" style="background:{color}"></div>
  <div class="ch-ring" style="border-color:{color}"></div>
</div>"""


def _result_badge(ok: bool, label: str) -> str:
    bg = "#1a3a20" if ok else "#3a1a1a"
    text = "#34c759" if ok else "#ff3b30"
    tag = "PASS" if ok else "FAIL"
    return (f'<span class="result-badge" style="background:{bg};color:{text}">'
            f'{tag} {_html.escape(label)}</span>')


def _case_html(c: dict, idx: int, result: dict | None) -> str:
    before_path = (EVAL_DIR / c["before"]).resolve()
    after_path  = (EVAL_DIR / c["after"]).resolve()
    exp = c["expected"]

    before_ok = before_path.exists()
    after_ok  = after_path.exists()
    before_b64 = _b64(before_path) if before_ok else ""
    after_b64  = _b64(after_path)  if after_ok  else ""

    # YOLO overlay on AFTER
    yolo_boxes = _yolo_boxes(after_path.read_bytes()) if after_ok else []
    yolo_overlay = ""
    for b in yolo_boxes:
        cls = "yolo-box-highlight" if b["top_left"] else "yolo-box"
        title = f"({b['nx']:.0f},{b['ny']:.0f}) conf={b['conf']}"
        yolo_overlay += (
            f'<div class="{cls}" style="left:{b["x1"]:.2f}%;top:{b["y1"]:.2f}%;'
            f'width:{b["x2"]-b["x1"]:.2f}%;height:{b["y2"]-b["y1"]:.2f}%;" '
            f'title="{_html.escape(title)}"></div>'
        )

    # Crosshair from LLM result
    crosshair_html = ""
    result_html = ""
    if result is not None:
        action = result.get("action")
        skipped = result.get("skipped", False)

        if skipped:
            result_html = '<div class="result-row"><span class="result-badge" style="background:#2c2c2e;color:#888">SKIP 文件缺失</span></div>'
        elif action is None:
            ok_type = not exp["can_go_back"]
            result_html = (
                '<div class="result-row">'
                + _result_badge(ok_type, f"can_go_back=False (exp={exp['page_type']})")
                + '</div>'
            )
        else:
            ok_type = action.page_type == exp["page_type"]
            zone = exp.get("y_zone", "")
            ok_zone = _check_y_zone(action.back_x, action.back_y, zone) if zone else True

            crosshair_html = _crosshair(action.back_x, action.back_y, ok_type and ok_zone)

            result_html = f"""
<div class="result-row">
  {_result_badge(ok_type, f"page_type: got {action.page_type}, exp {exp['page_type']}")}
  {_result_badge(ok_zone, f"y_zone={zone}  ({action.back_x:.0f},{action.back_y:.0f})") if zone else ""}
  <span class="method-text">{_html.escape(action.method)}</span>
</div>"""

    type_badge_color = TYPE_COLOR.get(exp["page_type"], "#888")
    zone_text = ZONE_LABEL.get(exp.get("y_zone", ""), exp.get("y_zone", "—"))
    note = _html.escape(c.get("note", ""))

    img_before = (
        f'<img src="data:image/png;base64,{before_b64}">'
        if before_ok else '<div class="img-missing">图片缺失</div>'
    )
    img_after = (
        f'<img src="data:image/png;base64,{after_b64}">'
        if after_ok else '<div class="img-missing">图片缺失</div>'
    )

    return f"""
<div class="case" id="case-{idx}">
  <div class="case-header">
    <span class="case-idx">#{idx+1}</span>
    <span class="case-id">{_html.escape(c['id'])}</span>
    <span class="type-badge" style="background:{type_badge_color}">Expected: Type {exp['page_type']}</span>
    <span class="zone-badge">{zone_text}</span>
  </div>

  <div class="nav-context">触发操作：{_html.escape(c.get('nav_context','—'))}</div>
  {f'<div class="note">{note}</div>' if note else ''}
  {result_html}

  <div class="screenshots">
    <div class="ss-wrap">
      <div class="ss-label before-label">BEFORE</div>
      <div class="img-wrap">{img_before}</div>
    </div>
    <div class="arrow">→</div>
    <div class="ss-wrap">
      <div class="ss-label after-label">AFTER</div>
      <div class="img-wrap">
        {img_after}
        {yolo_overlay}
        {crosshair_html}
      </div>
    </div>
  </div>
</div>"""


def build_html(cases: list[dict], results: list[dict | None], elapsed: float | None) -> str:
    # Summary counts
    if results and any(r is not None for r in results):
        total_checks = sum(
            2 if c["expected"].get("y_zone") else 1
            for c in cases
        )
        passed_checks = 0
        for c, r in zip(cases, results):
            if not r or r.get("skipped") or r.get("action") is None:
                continue
            action = r["action"]
            ok_type = action.page_type == c["expected"]["page_type"]
            zone = c["expected"].get("y_zone", "")
            ok_zone = _check_y_zone(action.back_x, action.back_y, zone) if zone else None
            passed_checks += ok_type
            if ok_zone is not None:
                passed_checks += ok_zone
        failed_checks = total_checks - passed_checks
        time_str = f" · {elapsed:.1f}s" if elapsed else ""
        summary_html = f"""
<div class="summary">
  <span class="sum-pass">PASSED {passed_checks}/{total_checks}</span>
  <span class="sum-fail">FAILED {failed_checks}/{total_checks}</span>
  <span class="sum-time">{time_str}</span>
</div>"""
    else:
        summary_html = '<div class="summary"><span class="sum-time">（仅展示数据，未运行 LLM）</span></div>'

    cases_html = "\n".join(_case_html(c, i, results[i] if results else None) for i, c in enumerate(cases))

    type_legend = "".join(
        f'<span class="legend-item"><span class="type-badge" style="background:{col}">Type {t}</span> '
        f'{"弹窗/浮层" if t=="A" else "Tab切换" if t=="B" else "普通跳转"}</span>'
        for t, col in TYPE_COLOR.items()
    )

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>Back Nav Eval — {len(cases)} cases</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, sans-serif; background: #111; color: #e0e0e0;
         padding: 24px; font-size: 13px; }}
  h1 {{ font-size: 18px; font-weight: 700; color: #fff; margin-bottom: 4px; }}
  .subtitle {{ color: #666; margin-bottom: 12px; font-size: 12px; }}

  .summary {{ display: flex; align-items: center; gap: 16px; margin-bottom: 20px;
              background: #1c1c1e; border-radius: 8px; padding: 10px 16px; }}
  .sum-pass {{ color: #34c759; font-weight: 700; font-size: 15px; }}
  .sum-fail {{ color: #ff3b30; font-weight: 700; font-size: 15px; }}
  .sum-time {{ color: #666; font-size: 12px; }}

  .legend {{ display: flex; gap: 16px; margin-bottom: 20px; flex-wrap: wrap; }}
  .legend-item {{ display: flex; align-items: center; gap: 6px; color: #aaa; font-size: 12px; }}

  .case {{ background: #1c1c1e; border-radius: 12px; padding: 16px 18px;
           margin-bottom: 20px; border: 1px solid #2c2c2e; }}
  .case-header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px;
                  flex-wrap: wrap; }}
  .case-idx {{ color: #555; font-size: 11px; font-weight: 700; min-width: 24px; }}
  .case-id {{ font-weight: 700; color: #fff; font-size: 14px; flex: 1; }}
  .type-badge {{ font-size: 11px; font-weight: 700; padding: 2px 10px;
                 border-radius: 10px; color: #000; white-space: nowrap; }}
  .zone-badge {{ font-size: 11px; background: #2c2c2e; color: #aaa;
                 padding: 2px 10px; border-radius: 10px; white-space: nowrap; }}

  .nav-context {{ color: #aaa; margin-bottom: 6px; font-size: 12px; }}
  .note {{ color: #666; font-size: 11px; margin-bottom: 8px;
           border-left: 2px solid #333; padding-left: 8px; line-height: 1.5; }}

  .result-row {{ display: flex; align-items: center; gap: 8px; margin-bottom: 10px;
                 flex-wrap: wrap; }}
  .result-badge {{ font-size: 11px; font-weight: 700; padding: 3px 10px;
                   border-radius: 6px; white-space: nowrap; }}
  .method-text {{ color: #888; font-size: 11px; font-style: italic; }}

  .screenshots {{ display: flex; align-items: flex-start; gap: 12px; margin-top: 10px; }}
  .arrow {{ color: #444; font-size: 24px; padding-top: 80px; flex-shrink: 0; }}
  .ss-wrap {{ display: flex; flex-direction: column; align-items: flex-start; flex-shrink: 0; }}
  .ss-label {{ font-size: 10px; font-weight: 700; padding: 2px 8px;
               border-radius: 4px; margin-bottom: 4px; display: inline-block; }}
  .before-label {{ background: #0a84ff; color: #fff; }}
  .after-label  {{ background: #ff9f0a; color: #000; }}
  .img-wrap {{ position: relative; display: inline-block; }}
  .img-wrap img {{ height: 320px; border-radius: 8px; display: block; }}
  .img-missing {{ height: 320px; width: 150px; background: #2c2c2e; border-radius: 8px;
                  display: flex; align-items: center; justify-content: center;
                  color: #555; font-size: 11px; }}

  .yolo-box {{ position: absolute; border: 1.5px solid rgba(80,120,255,0.4);
               pointer-events: none; border-radius: 2px; }}
  .yolo-box-highlight {{ position: absolute; border: 2px solid rgba(255,60,60,0.85);
                         pointer-events: none; border-radius: 2px;
                         box-shadow: 0 0 6px rgba(255,60,60,0.4); }}

  .crosshair {{ position: absolute; pointer-events: none; z-index: 10; }}
  .ch-h {{ position: absolute; width: 44px; height: 2px; top: -1px; left: -22px; }}
  .ch-v {{ position: absolute; width: 2px; height: 44px; left: -1px; top: -22px; }}
  .ch-ring {{ position: absolute; width: 16px; height: 16px; border: 2.5px solid;
              border-radius: 50%; top: -8px; left: -8px; }}
</style>
</head>
<body>
  <h1>Back Nav Eval</h1>
  <div class="subtitle">
    AFTER 截图上：蓝框 = 所有 YOLO 检测图标 · 红框 = 左上角返回候选 · 十字 = LLM 预测点击位置（绿=正确，红=错误）
  </div>
  {summary_html}
  <div class="legend">{type_legend}</div>
  {cases_html}
</body>
</html>"""


def main() -> None:
    run_llm = "--no-llm" not in sys.argv
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))

    results: list[dict | None] = []
    elapsed = None

    if run_llm:
        print(f"运行 LLM 推断（{len(cases)} cases）…")
        t0 = time.time()
        for i, c in enumerate(cases):
            before_path = (EVAL_DIR / c["before"]).resolve()
            after_path  = (EVAL_DIR / c["after"]).resolve()
            if not before_path.exists() or not after_path.exists():
                print(f"  [{i+1}/{len(cases)}] SKIP {c['id']} (文件缺失)")
                results.append({"skipped": True})
                continue
            print(f"  [{i+1}/{len(cases)}] {c['id']}", end=" … ", flush=True)
            action = infer_back_action(
                before_path.read_bytes(),
                after_path.read_bytes(),
                nav_context=c.get("nav_context", ""),
            )
            exp = c["expected"]
            ok_type = (action.page_type == exp["page_type"]) if action else not exp["can_go_back"]
            print("PASS" if ok_type else "FAIL")
            results.append({"action": action})
        elapsed = time.time() - t0
    else:
        results = [None] * len(cases)

    print(f"生成报告 → {OUT_HTML}", end=" … ", flush=True)
    html_content = build_html(cases, results, elapsed)
    OUT_HTML.write_text(html_content, encoding="utf-8")
    print("完成")

    if "--no-open" not in sys.argv:
        import subprocess
        subprocess.run(["open", str(OUT_HTML)], check=False)


if __name__ == "__main__":
    main()
