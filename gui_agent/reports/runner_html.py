"""HTML rendering for runner reports."""

from __future__ import annotations

import json
import re
from datetime import datetime

from gui_agent.core.config import model_price, pricing_currency

from .html_utils import _attr, _safe
from .metrics import _fmt_cache, _fmt_tokens, _sum_cached_tokens, _sum_tokens, _token_cost
from .models import ReportData, ReportStep
from .orchestrator_html import (
    _coding_call_label,
    _coding_statement_id,
    _flatten_coding_inputs,
    _infer_coding_op,
    _render_coding_data_panel,
    _render_non_ui_detail,
    _render_program_section,
    coding_plan_expansion_by_sid,
    coding_source_line_by_call_id,
    has_coding_program,
)
from .prompt_html import _render_module_io_html

# ── Runner HTML generator ──────────────────────────────────────

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  :root {{
    --bg: #f1f5f9; --card: #fff; --border: #e2e8f0;
    --text: #1e293b; --muted: #64748b; --radius: 12px;
  }}
  body {{ font-family: -apple-system, "PingFang SC", sans-serif; background: var(--bg); color: var(--text); }}

  /* ── Layout: optional knowledge sidebar + scrolling main ── */
  .layout {{ display: flex; align-items: flex-start; }}
  .sidebar {{
    width: 240px; flex-shrink: 0; position: sticky; top: 0; height: 100vh;
    overflow-y: auto; background: var(--card); border-right: 1px solid var(--border);
    padding: 20px 0;
  }}
  .main {{ flex: 1; min-width: 0; padding: 24px; }}

  .sidebar-title {{ font-size: 11px; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; padding: 0 18px 12px; }}

  /* ── Knowledge (知识库注入) ── */
  .sidebar-knowledge {{ }}
  .sk-app {{ padding: 0 18px; font-size: 13px; font-weight: 600; color: #0891b2; }}
  .sk-meta {{ padding: 3px 18px 0; font-size: 10px; color: #94a3b8; font-family: monospace; line-height: 1.6; }}
  .sk-channels {{ padding: 6px 18px 0; display: flex; flex-wrap: nowrap; gap: 4px; }}
  .sk-ch {{ position: relative; font-size: 10px; padding: 1px 7px; border-radius: 4px; line-height: 1.6; white-space: nowrap; cursor: default; }}
  .sk-ch.on {{ background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0; }}
  .sk-ch.off {{ background: #f1f5f9; color: #cbd5e1; border: 1px solid #e2e8f0; }}
  .sk-ch:hover::after {{ content: attr(data-tip); position: absolute; left: 0; top: calc(100% + 4px); z-index: 20; background: #1e293b; color: #fff; font-size: 10px; font-family: monospace; white-space: nowrap; padding: 3px 7px; border-radius: 4px; pointer-events: none; box-shadow: 0 2px 6px rgba(0,0,0,0.2); }}
  .sk-sections-label {{ padding: 12px 18px 4px; font-size: 10px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }}
  .sk-item {{ padding: 6px 18px 6px 26px; font-size: 12px; color: #0e7490; cursor: pointer; position: relative; line-height: 1.4; transition: background 0.12s; }}
  .sk-item::before {{ content: "📖"; position: absolute; left: 7px; font-size: 10px; }}
  .sk-item:hover {{ background: #ecfeff; text-decoration: underline; }}
  .sk-modal-card {{ background: #fff; max-width: 720px; width: 86%; max-height: 80vh; border-radius: 10px; display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 12px 40px rgba(0,0,0,0.3); }}
  .sk-modal-head {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; padding: 14px 18px; border-bottom: 1px solid var(--border); font-weight: 600; color: #0891b2; }}
  .sk-modal-x {{ cursor: pointer; color: #94a3b8; font-size: 16px; flex-shrink: 0; }}
  .sk-modal-x:hover {{ color: var(--text); }}
  .sk-modal-body {{ margin: 0; padding: 18px; overflow-y: auto; white-space: pre-wrap; word-break: break-word; font-size: 12.5px; line-height: 1.7; color: var(--text); font-family: ui-monospace, SFMono-Regular, monospace; }}

  /* Header */
  .header {{ position: relative; max-width: 1080px; margin: 0 auto 20px; padding: 20px 24px; background: var(--card); border-radius: var(--radius); box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  .header h1 {{ font-size: 18px; font-weight: 700; margin-bottom: 4px; padding-right: 72px; }}
  .stats {{ color: var(--muted); font-size: 12px; }}
  .meta-strip {{ margin-top: 10px; padding: 10px 14px; background: #f8fafc; border-radius: 8px; font-size: 12px; color: #475569; line-height: 1.5; }}
  .meta-strip-label {{ font-weight: 600; color: #6366f1; margin-right: 4px; }}
  .compat-row {{ display:flex; gap:6px; flex-wrap:wrap; align-items:center; margin:4px 0; }}
  .compat-chip {{ display:inline-flex; align-items:center; height:20px; padding:0 8px; border-radius:999px; background:#f1f5f9; color:#64748b; border:1px solid #e2e8f0; font-size:10px; font-weight:700; font-family:ui-monospace, SFMono-Regular, monospace; }}
  .report-search-trigger {{ position:absolute; top:19px; right:22px; height:28px; padding:0 10px; border:1px solid #cbd5e1; border-radius:7px; background:#fff; color:#475569; font-size:11px; font-weight:700; cursor:pointer; }}
  .report-search-trigger:hover {{ background:#f8fafc; color:#334155; }}
  .report-search-overlay {{ display:none; position:fixed; inset:0; z-index:998; align-items:flex-start; justify-content:center; padding-top:72px; background:rgba(15,23,42,0.18); backdrop-filter: blur(2px); }}
  .report-search-overlay.show {{ display:flex; }}
  .report-search-panel {{ width:min(620px, calc(100vw - 32px)); background:#fff; border:1px solid #cbd5e1; border-radius:10px; padding:10px; box-shadow:0 18px 50px rgba(15,23,42,0.26); }}
  .report-search-row {{ display:flex; align-items:center; gap:6px; }}
  .report-search-input {{ flex:1; min-width:0; height:34px; border:1px solid #cbd5e1; border-radius:7px; padding:0 10px; font-size:13px; color:#1e293b; background:#fff; }}
  .report-search-input:focus {{ outline:2px solid #bfdbfe; border-color:#60a5fa; }}
  .report-search-btn {{ height:34px; min-width:34px; border:1px solid #cbd5e1; border-radius:7px; background:#fff; color:#475569; font-size:12px; cursor:pointer; }}
  .report-search-btn:hover {{ background:#f8fafc; }}
  .report-search-close {{ font-size:17px; line-height:1; color:#64748b; }}
  .report-search-count {{ min-width:48px; color:#64748b; font-size:11px; font-weight:700; text-align:right; font-family:ui-monospace, SFMono-Regular, monospace; }}
  .search-hit {{ outline:1px solid #facc15; background:#fffbeb !important; }}
  .search-current {{ outline:2px solid #f97316; box-shadow:0 0 0 3px rgba(249,115,22,0.12); }}
  /* ── #0 reviewed Python program ── */
  .prog-section {{ }}
  .prog-body {{ padding: 14px 20px; display: flex; flex-direction: column; gap: 5px; font-size: 13px; }}
  .prog-input {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding-bottom: 10px; margin-bottom: 6px; border-bottom: 1px dashed var(--border); color: var(--text); }}
  .prog-input-label {{ font-weight: 600; color: #6366f1; font-size: 11px; padding: 1px 7px; background: #eef2ff; border-radius: 10px; }}
  .prog-input-arrow {{ color: #94a3b8; font-size: 11px; font-family: monospace; }}
  .coding-source {{ margin: 0; padding: 14px 16px; overflow-x: auto; border: 1px solid #cbd5e1; border-radius: 8px; background: #0f172a; color: #e2e8f0; font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: pre; tab-size: 4; }}
  .coding-source-wrap {{ margin-top: 4px; border: 1px solid #e2e8f0; border-radius: 8px; background: #f8fafc; }}
  .coding-source-wrap > summary {{ cursor: pointer; padding: 8px 12px; font-size: 12px; font-weight: 600; color: #475569; list-style: none; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }}
  .coding-source-wrap > summary::-webkit-details-marker {{ display: none; }}
  .coding-source-wrap[open] > summary {{ border-bottom: 1px solid #e2e8f0; }}
  .coding-source-wrap .coding-source {{ border: 0; border-radius: 0 0 8px 8px; }}
  .coding-note {{ color: #64748b; font-size: 11px; }}
  .coding-src-legend {{
    padding: 6px 12px; font-size: 11px; color: #64748b; background: #f1f5f9;
    border-bottom: 1px solid #e2e8f0;
  }}
  .coding-source-annotated {{
    margin: 0; padding: 8px 0 10px; overflow-x: hidden; background: #0f172a; color: #e2e8f0;
    font: 12px/1.65 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; tab-size: 4;
  }}
  .coding-src-line {{
    display: grid; grid-template-columns: 40px minmax(0, 1fr);
    gap: 0 10px; align-items: start; padding: 0 12px; min-height: 22px;
  }}
  .coding-src-line:hover {{ background: rgba(148,163,184,0.08); }}
  .coding-src-line-hit {{
    background: rgba(56,189,248,0.07); box-shadow: inset 3px 0 #0ea5e9;
  }}
  .coding-src-ln {{
    color: #64748b; text-align: right; user-select: none; padding-top: 1px;
  }}
  .coding-src-code {{
    min-width: 0; color: #e2e8f0; white-space: pre-wrap;
    overflow-wrap: anywhere;
  }}
  .coding-src-api-token {{
    color: #7dd3fc; font-weight: 700; text-decoration: underline;
    text-decoration-color: #0369a1; text-underline-offset: 3px;
  }}
  .coding-src-api-token-run {{
    padding: 1px 4px; border-radius: 4px; background: #0369a1;
    color: #f0f9ff; text-decoration: none; box-shadow: 0 0 0 1px #38bdf8;
  }}
  .coding-src-chip {{
    display: inline-flex; align-items: center; gap: 4px;
    background: #1e293b; border: 1px solid #334155; border-radius: 999px;
    padding: 1px 6px 1px 2px; white-space: nowrap;
  }}
  .coding-src-chip-op {{
    font-size: 10px; color: #7dd3fc; font-weight: 600;
  }}
  .coding-src-chip-meta {{ font-size: 10px; color: #94a3b8; }}
  .coding-src-chip-plan {{ border-color: #0ea5e9; background: #0c4a6e; }}
  .coding-src-chip-pending {{
    border-style: dashed; border-color: #64748b; opacity: 0.9;
  }}
  .coding-src-chip-pending .coding-src-chip-op {{ color: #94a3b8; font-weight: 500; }}
  .coding-src-chip .coding-phase {{ transform: scale(0.92); transform-origin: center right; }}
  .statement-sc.coding-plan-sc {{
    color: #0369a1; font-size: 11px; width: 100%; margin-bottom: 2px;
  }}
  .coding-macro-verdict {{
    margin: 0 0 4px; padding: 8px 12px; border-radius: 8px;
    border: 1px solid #fecaca; background: #fef2f2; color: #991b1b;
    font-size: 12px; font-weight: 600; line-height: 1.45;
  }}
  .coding-plan-details > summary {{ color: #0369a1; }}
  .coding-data-details-body {{
    padding: 8px 10px 10px; display: flex; flex-direction: column; gap: 6px;
  }}
  .coding-call-link {{
    display: inline-flex; align-items: center; justify-content: center;
    min-width: 26px; height: 18px; padding: 0 5px; border-radius: 999px;
    background: #eef2ff; color: #4338ca; font-weight: 700; font-size: 10px;
    text-decoration: none; flex-shrink: 0;
  }}
  .coding-call-link:hover {{ background: #c7d2fe; }}
  .coding-src-index {{
    border-top: 1px solid #334155; background: #111c2e;
  }}
  .coding-src-index > summary {{
    display: flex; align-items: center; gap: 8px; padding: 8px 12px;
    color: #cbd5e1; font-size: 11px; font-weight: 700; cursor: pointer;
    list-style: none;
  }}
  .coding-src-index > summary::-webkit-details-marker {{ display: none; }}
  .coding-src-index > summary::before {{ content: "▸"; color: #7dd3fc; }}
  .coding-src-index[open] > summary::before {{ content: "▾"; }}
  .coding-src-index > summary span {{ color: #64748b; font-size: 10px; font-weight: 500; }}
  .coding-src-index[open] > summary {{ border-bottom: 1px solid #334155; }}
  .coding-src-index-body {{ padding: 6px 12px 9px; }}
  .coding-src-api-group {{
    margin: 4px 0; border: 1px solid #334155; border-radius: 7px;
    background: #0f172a; scroll-margin-top: 12px;
  }}
  .coding-src-api-group > summary {{
    display: flex; align-items: center; gap: 7px; padding: 6px 8px;
    color: #cbd5e1; cursor: pointer; list-style: none;
  }}
  .coding-src-api-group > summary::-webkit-details-marker {{ display: none; }}
  .coding-src-api-group > summary::before {{ content: "▸"; color: #64748b; }}
  .coding-src-api-group[open] > summary::before {{ content: "▾"; }}
  .coding-src-api-group[open] > summary {{ border-bottom: 1px solid #334155; }}
  .coding-src-api-group > summary code {{ color: #7dd3fc; font-size: 11px; }}
  .coding-src-api-meta {{ color: #94a3b8; font-size: 10px; }}
  .coding-src-api-group > summary .coding-phase {{ margin-left: auto; }}
  .coding-src-api-body {{ padding: 6px 8px 8px; }}
  .coding-src-instance {{
    display: flex; flex-wrap: wrap; align-items: center; gap: 5px; padding: 3px 0;
  }}
  .coding-src-instance-label {{
    width: 42px; color: #94a3b8; font-size: 10px; flex-shrink: 0;
  }}
  .coding-src-unmatched {{
    display: flex; flex-wrap: wrap; align-items: center; gap: 5px; padding: 5px 8px;
    color: #94a3b8; font-size: 10px;
  }}
  .coding-src-index-line {{
    width: 42px; color: #7dd3fc; font-size: 10px; font-weight: 700;
    text-decoration: none; flex-shrink: 0;
  }}
  /* Coding: data strip inside the existing statement card (between header and gallery). */
  .coding-stmt-data {{
    margin: 0; border: 0; border-bottom: 1px solid var(--border);
    background: #f8fafc; padding: 10px 20px 12px;
    display: flex; flex-direction: column; gap: 12px;
  }}
  .coding-data-block {{ display: flex; flex-direction: column; gap: 6px; }}
  .coding-data-block-plan {{
    padding: 8px 10px; border: 1px solid #bae6fd; border-radius: 8px;
    background: #f0f9ff;
  }}
  .coding-data-block-plan .coding-data-title {{ color: #0369a1; }}
  .coding-data-block-plan .coding-data-val {{ border-color: #bae6fd; }}
  .coding-data-title {{
    font-size: 11px; font-weight: 700; color: #64748b;
    text-transform: uppercase; letter-spacing: .04em;
  }}
  .coding-data-row {{
    display: grid; grid-template-columns: 88px minmax(0, 1fr);
    gap: 8px; align-items: start; font-size: 12px;
  }}
  .coding-data-key {{
    color: #64748b; font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 11px; padding-top: 5px;
  }}
  .coding-data-val {{
    color: var(--text); word-break: break-word; min-width: 0;
    background: #fff; border: 1px solid #e2e8f0; border-radius: 6px;
    padding: 4px 8px; font-size: 12px; line-height: 1.45;
  }}
  .coding-data-val code {{
    font: 11px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    background: transparent; border: 0; padding: 0; color: #0f766e;
  }}
  .coding-data-pre {{
    margin: 0; white-space: pre-wrap; word-break: break-word;
    font: 11px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    color: #0f172a; background: #fff; border: 1px solid #e2e8f0;
    border-radius: 6px; padding: 6px 8px; max-height: 240px; overflow: auto;
  }}
  .coding-data-details {{
    margin-top: 2px; border: 1px solid #e2e8f0; border-radius: 6px; background: #fff;
  }}
  .coding-data-details > summary {{
    cursor: pointer; list-style: none; padding: 6px 10px; font-size: 11px;
    font-weight: 600; color: #64748b; user-select: none;
  }}
  .coding-data-details > summary::-webkit-details-marker {{ display: none; }}
  .coding-data-details > summary::before {{ content: "▸ "; color: #94a3b8; }}
  .coding-data-details[open] > summary::before {{ content: "▾ "; }}
  .coding-data-details[open] > summary {{ border-bottom: 1px solid #e2e8f0; }}
  .coding-data-details .coding-data-pre {{
    border: 0; border-radius: 0 0 6px 6px; max-height: 320px;
  }}
  .coding-data-fail .coding-data-val {{ color: #991b1b; border-color: #fecaca; background: #fef2f2; }}
  .coding-phase {{ font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 999px; flex-shrink: 0; }}
  .coding-phase-ok {{ background: #dcfce7; color: #166534; }}
  .coding-phase-fail {{ background: #fee2e2; color: #991b1b; }}
  .coding-phase-warn {{ background: #fef3c7; color: #92400e; }}
  .coding-ctx-badge {{
    font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 20px;
    background: #ecfeff; color: #0e7490; border: 1px solid #a5f3fc;
    font-family: ui-monospace, SFMono-Regular, monospace;
  }}
  .nonui-log {{ margin-top: 12px; padding-top: 12px; border-top: 1px dashed var(--border); display:flex; flex-direction:column; gap:8px; }}
  .nonui-title {{ font-size: 11px; font-weight: 700; color: #0f766e; text-transform: uppercase; letter-spacing: .04em; }}
  .nonui-row {{ border: 1px solid #d1fae5; background: #f0fdfa; border-radius: 8px; padding: 10px; display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; }}
  .nonui-detail {{ border: 1px solid #d1fae5; background: #f0fdfa; border-radius: 8px; padding: 10px; display:flex; flex-direction:column; gap:8px; }}
  .nonui-main {{ min-width: 0; display:flex; flex-direction:column; gap:6px; }}
  .nonui-head {{ display:flex; align-items:center; gap:7px; flex-wrap:wrap; }}
  .nonui-n {{ font-family: monospace; font-size: 11px; font-weight: 700; color:#0f766e; }}
  .nonui-name {{ font-size: 13px; font-weight: 600; color:#134e4a; }}
  .nonui-status {{ font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 999px; }}
  .nonui-ok {{ background:#dcfce7; color:#166534; }}
  .nonui-fail {{ background:#fee2e2; color:#991b1b; }}
  .nonui-summary {{ color:#475569; font-size:12px; }}
  .nonui-sql {{ display:grid; grid-template-columns: 42px minmax(0, 1fr); gap:8px; align-items:start; }}
  .nonui-label {{ color:#0f766e; font-size:10px; font-weight:700; text-transform:uppercase; padding-top:2px; }}
  .nonui-code {{ margin:0; background:#ffffff; border:1px solid #ccfbf1; border-radius:6px; padding:7px 8px; color:#334155; font-size:11px; line-height:1.45; white-space:pre-wrap; word-break:break-word; font-family:ui-monospace, SFMono-Regular, monospace; }}
  .nonui-reads {{ display:flex; flex-direction:column; gap:4px; }}
  .nonui-read {{ display:grid; grid-template-columns: 140px minmax(0, 1fr); gap:8px; align-items:start; font-size:12px; }}
  .nonui-key {{ color:#0f766e; font-weight:600; word-break:break-word; }}
  .nonui-val {{ margin:0; color:#1e293b; font-family:ui-monospace, SFMono-Regular, monospace; white-space:pre-wrap; word-break:break-word; }}
  .nonui-shot img {{ width: 150px; max-height: 96px; object-fit: cover; border-radius: 7px; border: 1px solid #99f6e4; cursor: zoom-in; display:block; }}

  /* Run status badge */
  .run-status-badge {{ position:relative; display:inline-block; margin-left:8px; padding:2px 10px; border-radius:11px; font-size:13px; font-weight:700; vertical-align:middle; cursor:default; }}
  .run-status-badge-completed {{ background:#16a34a; color:#fff; }}
  .run-status-badge-failed {{ background:#dc2626; color:#fff; }}
  .run-status-badge-interrupted {{ background:#d97706; color:#fff; }}
  .run-status-badge-stopped {{ background:#dc2626; color:#fff; }}
  .run-status-badge:hover::after {{ content: attr(data-tip); position:absolute; left:0; top:calc(100% + 7px); z-index:100; width:max-content; max-width:520px; white-space:normal; line-height:1.45; padding:8px 10px; border-radius:7px; background:#111827; color:#fff; font-size:12px; font-weight:500; box-shadow:0 8px 24px rgba(15,23,42,0.22); }}
  .run-status-badge:hover::before {{ content:""; position:absolute; left:16px; top:calc(100% + 2px); z-index:101; border:5px solid transparent; border-bottom-color:#111827; }}

  /* Final reply / 最终回复 card */
  .result-card {{ max-width: 1080px; margin: 0 auto 20px; padding: 16px 20px; background: var(--card); border-radius: var(--radius); box-shadow: 0 1px 3px rgba(0,0,0,0.08); border-left: 4px solid #22c55e; }}
  .result-card-interrupted {{ border-left-color: #d97706; }}
  .result-card-failed {{ border-left-color: #dc2626; }}
  .result-card-stopped {{ border-left-color: #dc2626; }}
  .result-label {{ font-size: 11px; font-weight: 700; color: #16a34a; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 8px; }}
  .result-card-interrupted .result-label {{ color: #d97706; }}
  .result-card-failed .result-label {{ color: #dc2626; }}
  .result-card-stopped .result-label {{ color: #dc2626; }}
  .result-body {{ font-size: 14px; color: var(--text); line-height: 1.6; white-space: pre-wrap; word-break: break-word; }}

  /* WebArena final response */
  .wa-card {{ max-width:1080px; margin:0 auto 20px; padding:16px 20px; background:var(--card); border-radius:var(--radius); box-shadow:0 1px 3px rgba(0,0,0,0.08); border-left:4px solid #22c55e; }}
  .wa-card-error {{ border-left-color:#dc2626; }}
  .wa-head {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:10px; }}
  .wa-label {{ font-size:11px; font-weight:700; color:#047857; text-transform:uppercase; letter-spacing:0.06em; }}
  .wa-card-error .wa-label {{ color:#dc2626; }}
  .wa-chip {{ font-size:11px; font-weight:600; padding:2px 8px; border-radius:999px; background:#ecfdf5; color:#047857; border:1px solid #a7f3d0; }}
  .wa-chip-error {{ background:#fef2f2; color:#b91c1c; border-color:#fecaca; }}
  .wa-meta {{ font-size:11px; color:#94a3b8; font-family:monospace; margin-left:auto; }}
  .wa-grid {{ display:grid; grid-template-columns:120px 1fr; gap:6px 12px; font-size:13px; line-height:1.5; }}
  .wa-k {{ color:#64748b; font-weight:600; }}
  .wa-v {{ color:#1e293b; white-space:pre-wrap; word-break:break-word; }}
  .wa-assert {{ margin-top:4px; color:#b91c1c; }}

  /* Router / input-resolution row (shares the 模型配置 box style) */
  .prov-arrow {{ color: #94a3b8; margin: 0 6px; }}
  .prov-goal {{ color: #1e293b; font-weight: 500; }}
  .prov-via {{ display: inline-block; margin-left: 8px; font-size: 10px; padding: 1px 8px; border-radius: 10px; font-family: monospace; vertical-align: middle; }}
  .prov-via-router {{ background: #dcfce7; color: #166534; }}
  .prov-via-temporal {{ background: #dbeafe; color: #1d4ed8; }}
  .prov-via-none {{ background: #f1f5f9; color: #64748b; }}
  .prov-via-clarify {{ background: #fef9c3; color: #854d0e; }}
  .price-tip {{ position: relative; display: inline-block; margin-left: 8px; }}
  .price-chip {{ font-size: 11px; padding: 1px 8px; border-radius: 10px; background: #eef2ff; color: #4338ca; border: 1px solid #c7d2fe; cursor: help; }}
  .price-pop {{ display: none; position: absolute; z-index: 50; top: 100%; left: 0; margin-top: 6px; background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 8px 12px; box-shadow: 0 4px 14px rgba(0,0,0,0.12); white-space: nowrap; }}
  .price-tip:hover .price-pop {{ display: block; }}
  .price-pop table {{ border-collapse: collapse; font-size: 11px; color: #475569; }}
  .price-pop td {{ padding: 1px 16px 1px 0; }}
  .price-pop .pp-num {{ text-align: right; padding-right: 0; }}
  .price-pop .pp-head td {{ color: #94a3b8; }}

  /* StatementContract section */
  .statement {{ max-width: 1080px; margin: 0 auto 16px; background: var(--card); border-radius: var(--radius); box-shadow: 0 1px 3px rgba(0,0,0,0.08); overflow: hidden; }}
  .statement-header {{ padding: 10px 20px; background: #f8fafc; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .statement-header h2 {{ font-size: 14px; font-weight: 600; }}
  .statement-name {{ font-size: 13px; color: var(--text); }}
  .statement-desc {{ font-size: 11px; color: var(--muted); width: 100%; }}
  .statement-sc {{ font-size: 11px; color: #94a3b8; width: 100%; }}
  .statement-sc.coding-call-sc {{
    color: #0f766e; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 11px; line-height: 1.45; word-break: break-word;
  }}
  .statement-sc.coding-call-sc code {{
    font: inherit; background: #ecfeff; border: 1px solid #a5f3fc;
    border-radius: 4px; padding: 1px 6px;
  }}
  .checklist-badge {{ cursor:pointer; font-size:10px; font-weight:600; padding:2px 8px; border-radius:20px; user-select:none; display:inline-flex; align-items:center; gap:4px; }}
  .checklist-badge-ok {{ background:#dcfce7; color:#166534; }}
  .checklist-badge-partial {{ background:#e0f2fe; color:#0369a1; }}
  .checklist-badge:hover {{ filter:brightness(0.96); }}
  .cl-modal-body {{ margin:0; padding:14px 18px; overflow-y:auto; display:flex; flex-direction:column; gap:8px; }}
  .statement-checklist {{ width:100%; display:flex; flex-direction:column; gap:4px; margin-top:4px; }}
  .statement-check {{ display:flex; align-items:flex-start; gap:6px; font-size:11px; line-height:1.35; color:#475569; }}
  .statement-check-mark {{ display:inline-flex; align-items:center; justify-content:center; width:15px; height:15px; border-radius:50%; font-size:10px; font-weight:700; flex:0 0 auto; margin-top:0; }}
  .statement-check-text {{ word-break:break-word; }}
  .statement-check-evidence {{ color:#94a3b8; margin-left:6px; }}
  .statement-check-done .statement-check-mark {{ background:#dcfce7; color:#166534; }}
  .statement-check-pending .statement-check-mark {{ background:#e0f2fe; color:#0369a1; }}
  .statement-check-blocked .statement-check-mark {{ background:#fee2e2; color:#991b1b; }}
  .statement-check-skipped .statement-check-mark {{ background:#f1f5f9; color:#64748b; }}
  .statement-badge {{ font-size: 10px; padding: 2px 7px; border-radius: 20px; font-weight: 500; }}
  .statement-badge-navigation {{ background: #dbeafe; color: #1d4ed8; }}
  .statement-badge-action {{ background: #fef3c7; color: #92400e; }}
  .statement-badge-filter {{ background: #ede9fe; color: #5b21b6; }}
  .statement-badge-collection {{ background: #d1fae5; color: #065f46; }}
  .statement-badge-default {{ background: #f1f5f9; color: #475569; }}
  .statement-time {{ font-size: 11px; color: #475569; margin-left: auto; font-family: monospace; font-weight: 700; }}
  .coding-call-card {{
    max-width: 1080px; margin: 0 auto 16px; background: var(--card);
    border-radius: var(--radius); box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    overflow: hidden;
  }}
  .coding-call-card > summary {{
    cursor: pointer; list-style: none; padding: 12px 20px; background: #f8fafc;
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  }}
  .coding-call-card > summary::-webkit-details-marker {{ display: none; }}
  .coding-call-card > summary::before {{
    content: "▸"; color: #64748b; font-size: 12px; width: 10px;
  }}
  .coding-call-card[open] > summary::before {{ content: "▾"; }}
  .coding-call-card[open] > summary {{ border-bottom: 1px solid var(--border); }}
  .coding-call-index {{ font-size: 12px; font-weight: 700; color: #475569; }}
  .coding-call-title {{
    color: #0f766e; font: 600 12px/1.4 ui-monospace, SFMono-Regular, monospace;
  }}
  .coding-call-meta {{ margin-left: auto; color: #64748b; font-size: 11px; }}
  .coding-call-body {{ padding: 12px; background: #f8fafc; }}
  .coding-call-body > .statement {{
    margin: 0 auto 10px; border: 1px solid var(--border); box-shadow: none;
  }}
  .coding-call-body > .statement:last-child {{ margin-bottom: 0; }}

  /* Thumbnail gallery — one row per statement */
  .gallery {{ display: flex; gap: 6px; padding: 12px 16px; overflow-x: auto; }}
  .thumb {{ flex-shrink: 0; width: 152px; cursor: pointer; position: relative; border-radius: 8px; overflow: hidden; border: 2px solid transparent; transition: border-color 0.15s; }}
  .thumb:hover {{ border-color: #6366f1; }}
  .thumb.active {{ border-color: #4338ca; }}
  .thumb img {{ width: 100%; display: block; }}
  .thumb-label {{ position: absolute; bottom: 0; left: 0; right: 0; padding: 18px 7px 6px; font-size: 11px; line-height: 1.15; font-weight: 800; color: #fff; background: linear-gradient(to top, rgba(15,23,42,0.88), rgba(15,23,42,0.56) 58%, transparent); display:flex; align-items:center; gap:6px; text-shadow:0 1px 2px rgba(0,0,0,0.72); }}
  .thumb-time {{ font-size:10px; line-height:1.2; font-weight:800; color:#fff; font-family:ui-monospace, SFMono-Regular, monospace; margin-left:auto; background:rgba(15,23,42,0.72); border:1px solid rgba(255,255,255,0.42); border-radius:5px; padding:1px 4px; text-shadow:none; }}
  .thumb-status {{ position: absolute; top: 4px; right: 4px; width: 16px; height: 16px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 9px; color: #fff; font-weight: 700; }}
  .thumb-status-ok {{ background: #22c55e; }}
  .thumb-status-fail {{ background: #ef4444; }}
  .thumb-status-skip {{ background: #9ca3af; }}
  .thumb-action {{ position: absolute; top: 4px; left: 4px; width: 14px; height: 14px; border-radius: 50%; }}

  /* Detail panel — shown when thumbnail clicked */
  .detail {{ display: none; padding: 12px 20px; border-top: 1px solid var(--border); }}
  .detail.show {{ display: flex; gap: 20px; }}
  .detail-ss {{ width: 200px; flex-shrink: 0; }}
  .detail-ss img {{ width: 100%; border-radius: 8px; border: 1px solid var(--border); cursor: zoom-in; }}
  .detail-info {{ flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 6px; }}
  .detail-top {{ display: flex; align-items: center; gap: 8px; }}
  .detail-idx {{ display: inline-flex; width: 22px; height: 22px; align-items: center; justify-content: center; border-radius: 50%; font-size: 10px; font-weight: 700; color: #fff; flex-shrink: 0; }}
  .detail-at {{ font-size: 10px; padding: 1px 5px; border-radius: 3px; font-weight: 500; }}
  .detail-desc {{ font-size: 13px; font-weight: 500; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .detail-status {{ font-size: 11px; font-weight: 600; flex-shrink: 0; }}
  .detail-time {{ font-size: 13px; font-weight: 700; color: #334155; flex-shrink: 0; font-variant-numeric: tabular-nums; }}
  .detail-gap {{ font-size: 12px; font-weight: 800; color: #b45309; background: #fff7ed; border: 1px solid #fed7aa; padding: 1px 7px; border-radius: 10px; margin-left: 6px; flex-shrink: 0; font-variant-numeric: tabular-nums; }}
  .detail-instruction {{ font-size: 12px; color: var(--muted); }}
  .detail-summary {{ font-size: 12px; color: #475569; line-height: 1.4; }}
  .ctx-detail {{ margin-top: 2px; border: 1px solid #e0f2fe; background: #f8fdff; border-radius: 7px; padding: 7px 9px; }}
  .ctx-detail summary {{ cursor: pointer; color: #0369a1; font-size: 11px; font-weight: 700; }}
  .ctx-list {{ margin-top: 7px; display: flex; flex-direction: column; gap: 6px; }}
  .ctx-row {{ font-size: 11px; line-height: 1.45; color: #475569; font-family: ui-monospace, SFMono-Regular, monospace; overflow-wrap: anywhere; }}
  .ctx-row strong {{ color: #0f172a; font-family: -apple-system, "PingFang SC", sans-serif; }}
  .ctx-drop {{ color: #b45309; }}
  .ctx-keep {{ color: #047857; }}
  .prompt-detail {{ margin-top: 2px; border: 1px solid #dbe3ef; background: #fff; border-radius: 7px; overflow: hidden; }}
  .prompt-detail > summary.prompt-detail-head {{ cursor: pointer; list-style: none; display: flex; align-items: center; gap: 9px; min-width: 0; padding: 7px 9px; color: #334155; font-size: 11px; font-weight: 700; }}
  .prompt-detail > summary.prompt-detail-head::-webkit-details-marker {{ display: none; }}
  .prompt-detail > summary.prompt-detail-head::before {{ content: "▸"; color: #64748b; font-size: 10px; flex: 0 0 auto; }}
  .prompt-detail[open] > summary.prompt-detail-head::before {{ content: "▾"; }}
  .prompt-detail-title {{ flex: 0 0 auto; color: #1e293b; font-size: 12px; }}
  .prompt-detail-meta {{ margin-left: auto; min-width: 0; color: #334155; font-size: 10px; font-weight: 800; font-family: ui-monospace, SFMono-Regular, monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .prompt-list {{ padding: 0 9px 8px; display: flex; flex-direction: column; gap: 8px; }}
  .prompt-token-detail {{ border: 1px dashed #dbe3ef; background: #f8fafc; border-radius: 6px; overflow: hidden; }}
  .prompt-token-detail > summary {{ cursor: pointer; list-style: none; display: flex; align-items: center; gap: 7px; padding: 5px 7px; color: #64748b; font-size: 10px; font-weight: 800; font-family: ui-monospace, SFMono-Regular, monospace; }}
  .prompt-token-detail > summary::-webkit-details-marker {{ display: none; }}
  .prompt-token-detail > summary::before {{ content: "▸"; color: #94a3b8; font-size: 10px; }}
  .prompt-token-detail[open] > summary::before {{ content: "▾"; }}
  .prompt-token-summary {{ color: #94a3b8; font-weight: 700; }}
  .prompt-token-row {{ display:flex; flex-wrap:wrap; gap:5px; align-items:center; padding:0 7px 7px 21px; color:#64748b; font-size:10px; font-family:ui-monospace, SFMono-Regular, monospace; }}
  .prompt-token-chip {{ display:inline-flex; gap:5px; align-items:center; padding:1px 6px; border-radius:5px; background:#fff; border:1px solid #e2e8f0; }}
  .prompt-token-name {{ color:#475569; font-weight:800; }}
  .prompt-token-count {{ color:#64748b; font-weight:700; }}
  .prompt-call {{ border: 1px solid #e2e8f0; background: #fff; border-radius: 7px; overflow: hidden; }}
  .prompt-call > summary {{ color: #334155; font-size: 11px; font-weight: 700; cursor: pointer; list-style: none; display: flex; align-items: center; gap: 7px; min-width: 0; padding: 6px 8px; }}
  .prompt-call > summary::-webkit-details-marker {{ display: none; }}
  .prompt-call > summary::before {{ content: "▸"; color: #64748b; font-size: 10px; flex: 0 0 auto; }}
  .prompt-call[open] > summary::before {{ content: "▾"; }}
  .prompt-call-title {{ flex: 0 0 auto; color: #3730a3; font-family: ui-monospace, SFMono-Regular, monospace; }}
  .prompt-call-summary {{ display:block; min-width:0; padding:0; border:0; background:transparent; color:#334155; font-size:11px; font-weight:700; font-family:ui-monospace, SFMono-Regular, monospace; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .prompt-call-summary::before {{ content:"·"; color:#cbd5e1; margin-right:7px; }}
  .prompt-call-summary-ok {{ color:#047857; }}
  .prompt-call-summary-warn {{ color:#b45309; }}
  .prompt-call-summary-error {{ color:#b91c1c; }}
  .prompt-call-summary-muted {{ color:#94a3b8; }}
  .prompt-call-body {{ padding: 0 9px 9px; display: flex; flex-direction: column; gap: 10px; }}
  .prompt-role {{ border-left: 2px solid #c4b5fd; padding-left: 10px; display: flex; flex-direction: column; gap: 8px; }}
  .prompt-output {{ border-left-color: #22c55e; }}
  .prompt-output-missing {{ border-left-color: #cbd5e1; }}
  .prompt-role-title {{ align-self: flex-start; text-transform: uppercase; letter-spacing: .06em; color: #475569; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 999px; padding: 1px 8px; font-size: 10px; font-weight: 800; }}
  .prompt-output .prompt-role-title {{ color: #047857; background: #ecfdf5; border-color: #a7f3d0; }}
  .prompt-output-missing .prompt-role-title {{ color: #64748b; background: #f8fafc; border-color: #e2e8f0; }}
  .prompt-part {{ border: 1px solid #e2e8f0; border-radius: 6px; overflow: hidden; background: #fff; }}
  .prompt-part-head {{ display: flex; align-items: center; gap: 6px; flex-wrap: wrap; background: #f8fafc; border-bottom: 1px solid #e2e8f0; padding: 5px 8px; }}
  .prompt-part-no {{ display: inline-flex; align-items: center; justify-content: center; min-width: 18px; height: 18px; border-radius: 9px; background: #f1f5f9; color: #475569; font-size: 10px; font-weight: 800; font-family: ui-monospace, SFMono-Regular, monospace; }}
  .prompt-part-label {{ font-size: 11px; font-weight: 700; color: #334155; font-family: ui-monospace, SFMono-Regular, monospace; }}
  .prompt-part-meta {{ font-size: 10px; color: #94a3b8; font-family: ui-monospace, SFMono-Regular, monospace; }}
  .prompt-pre {{ margin: 0; max-height: 420px; overflow: auto; white-space: pre-wrap; word-break: break-word; padding: 8px 10px; color: #1e293b; font-size: 11.5px; line-height: 1.55; font-family: ui-monospace, SFMono-Regular, monospace; background: #fff; }}
  .prompt-pre-image {{ color: #64748b; background: #f8fafc; }}
  .prompt-pre-output {{ background: #f8fffb; }}
  .prompt-empty {{ color: #94a3b8; font-size: 11px; padding: 4px 0; }}
  .prompt-part-collapsed > summary.prompt-part-head {{ cursor:pointer; list-style:none; }}
  .prompt-part-collapsed > summary.prompt-part-head::-webkit-details-marker {{ display:none; }}
  .prompt-part-collapsed > summary.prompt-part-head::before {{ content:"▸"; color:#64748b; margin-right:2px; font-size:10px; }}
  .prompt-part-collapsed[open] > summary.prompt-part-head::before {{ content:"▾"; }}
  .prompt-schema {{ background:#fcfcff; }}
  .prompt-schema > summary.prompt-part-head {{ cursor:pointer; list-style:none; }}
  .prompt-schema > summary.prompt-part-head::-webkit-details-marker {{ display:none; }}
  .prompt-schema > summary.prompt-part-head::before {{ content:"▸"; color:#64748b; margin-right:2px; font-size:10px; }}
  .prompt-schema[open] > summary.prompt-part-head::before {{ content:"▾"; }}

  /* Timing bar */
  .timing-bar {{ display: flex; height: 5px; border-radius: 3px; overflow: hidden; background: #f1f5f9; margin-top: 2px; }}
  .timing-seg {{ height: 100%; min-width: 1px; }}
  .timing-labels {{ display: flex; gap: 8px; font-size: 11px; color: #64748b; font-weight: 600; margin-top: 2px; flex-wrap: wrap; }}
  .timing-label-dot {{ display: inline-block; width: 7px; height: 7px; border-radius: 2px; vertical-align: middle; margin-right: 2px; }}

  /* Action type colors */
  .at-tap {{ background: #dc3232; }} .at-type {{ background: #a032c8; }}
  .at-scroll {{ background: #32b432; }} .at-drag {{ background: #3296dc; }}
  .at-home {{ background: #3278dc; }} .at-press_enter {{ background: #dca000; }}
  .at-clear_text {{ background: #808080; }} .at-none {{ background: #c0c0c0; }}
  .at-upload {{ background: #ec4899; }} .at-navigate {{ background: #3b82f6; }}
  .at-back {{ background: #6366f1; }} .at-new_tab {{ background: #14b8a6; }}
  .at-select_tab {{ background: #f59e0b; }} .at-close_tab {{ background: #64748b; }}
  .at-select_option {{ background: #0ea5e9; }}

  .modal {{ display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.85); z-index: 999; justify-content: center; align-items: center; }}
  .modal.show {{ display: flex; }}
  /* Zoom shows the full-resolution raw screenshot for every frame (see zoomImg call sites), so a
     large cap is consistent and sharp across turns and the verification frame. */
  .modal img {{ max-width: 92vw; max-height: 90vh; border-radius: 8px; }}
</style>
</head>
<body>

<div class="layout">
  {sidebar_html}
  <main class="main">
    <div class="header">
      <h1>{title}{platform_badge}{phase_badge}</h1>
      <button class="report-search-trigger" type="button" onclick="openReportSearch()" title="搜索（/ 或 Cmd/Ctrl+K）">搜索</button>
      <div class="stats">{stats}</div>
      {provenance_html}
      {cost_note_html}
    </div>

    {program_html}
    {pages_html}
    {presentation_html}
    {result_html}
    {webarena_html}
    {mobileworld_html}
  </main>
</div>

<div class="modal" id="modal" onclick="this.classList.remove('show')">
  <img id="modal-img" src="">
</div>
<div class="modal" id="sk-modal" onclick="if(event.target===this)this.classList.remove('show')">
  <div class="sk-modal-card">
    <div class="sk-modal-head">
      <span id="sk-modal-title"></span>
      <span class="sk-modal-x" onclick="document.getElementById('sk-modal').classList.remove('show')">✕</span>
    </div>
    <pre class="sk-modal-body" id="sk-modal-body"></pre>
  </div>
</div>
<div class="modal" id="cl-modal" onclick="if(event.target===this)this.classList.remove('show')">
  <div class="sk-modal-card">
    <div class="sk-modal-head">
      <span>验收清单</span>
      <span class="sk-modal-x" onclick="document.getElementById('cl-modal').classList.remove('show')">✕</span>
    </div>
    <div class="cl-modal-body" id="cl-modal-body"></div>
  </div>
</div>
<div class="report-search-overlay" id="report-search-overlay" onclick="if(event.target===this)closeReportSearch()">
  <div class="report-search-panel">
    <div class="report-search-row">
      <input class="report-search-input" id="report-search" type="search"
        placeholder="搜索模块、字段、文本、block id" oninput="reportSearch(this.value)"
        onkeydown="if(event.key==='Enter') reportSearchNext(event.shiftKey ? -1 : 1)">
      <button class="report-search-btn" type="button" onclick="reportSearchNext(-1)">↑</button>
      <button class="report-search-btn" type="button" onclick="reportSearchNext(1)">↓</button>
      <span class="report-search-count" id="report-search-count"></span>
      <button class="report-search-btn report-search-close" type="button" onclick="closeReportSearch()">×</button>
    </div>
  </div>
</div>
<script>
function showDetail(id) {{
  var el = document.getElementById(id);
  if (el.classList.contains('show')) {{
    el.classList.remove('show');
    var thumb = document.querySelector('[data-detail="' + id + '"]');
    if (thumb) thumb.classList.remove('active');
    return;
  }}
  // Hide all details in same statement
  var ms = el.closest('.statement');
  ms.querySelectorAll('.detail').forEach(d => d.classList.remove('show'));
  ms.querySelectorAll('.thumb').forEach(t => t.classList.remove('active'));
  // Show selected
  el.classList.add('show');
  var thumb = document.querySelector('[data-detail="' + id + '"]');
  if (thumb) thumb.classList.add('active');
}}
function zoomImg(src) {{
  document.getElementById('modal-img').src = src;
  document.getElementById('modal').classList.add('show');
}}
function showSection(id, title) {{
  var src = document.getElementById(id);
  if (!src) return;
  document.getElementById('sk-modal-title').textContent = title;
  document.getElementById('sk-modal-body').textContent = src.textContent;
  document.getElementById('sk-modal').classList.add('show');
}}
function showChecklist(id) {{
  var src = document.getElementById(id);
  if (!src) return;
  document.getElementById('cl-modal-body').innerHTML = src.innerHTML;
  document.getElementById('cl-modal').classList.add('show');
}}
function openReportSearch() {{
  var overlay = document.getElementById('report-search-overlay');
  var input = document.getElementById('report-search');
  if (!overlay || !input) return;
  overlay.classList.add('show');
  window.setTimeout(function() {{
    input.focus();
    input.select();
  }}, 0);
}}
function closeReportSearch() {{
  var overlay = document.getElementById('report-search-overlay');
  if (overlay) overlay.classList.remove('show');
}}
var reportSearchHits = [];
var reportSearchIndex = -1;
function reportSearch(q) {{
  document.querySelectorAll('.search-hit,.search-current').forEach(function(n) {{
    n.classList.remove('search-hit');
    n.classList.remove('search-current');
  }});
  reportSearchHits = [];
  reportSearchIndex = -1;
  q = (q || '').trim().toLowerCase();
  var counter = document.getElementById('report-search-count');
  if (!q) {{
    if (counter) counter.textContent = '';
    return;
  }}
  var nodes = Array.prototype.slice.call(document.querySelectorAll(
    '.prompt-call,.prompt-part,.thumb,.coding-call-card,.coding-src-api-group,.nonui-row,.wa-card'
  ));
  reportSearchHits = nodes.filter(function(n) {{
    var hay = ((n.dataset.searchIndex || '') + ' ' + (n.textContent || '')).toLowerCase();
    return hay.indexOf(q) !== -1;
  }});
  reportSearchHits.forEach(function(n) {{ n.classList.add('search-hit'); }});
  if (counter) counter.textContent = reportSearchHits.length ? ('0/' + reportSearchHits.length) : '无命中';
  if (reportSearchHits.length) reportSearchNext(1);
}}
function reportSearchNext(delta) {{
  if (!reportSearchHits.length) return;
  reportSearchHits.forEach(function(n) {{ n.classList.remove('search-current'); }});
  reportSearchIndex = (reportSearchIndex + delta + reportSearchHits.length) % reportSearchHits.length;
  var node = reportSearchHits[reportSearchIndex];
  revealSearchNode(node);
  node.classList.add('search-current');
  var counter = document.getElementById('report-search-count');
  if (counter) counter.textContent = (reportSearchIndex + 1) + '/' + reportSearchHits.length;
  node.scrollIntoView({{behavior:'smooth', block:'center', inline:'nearest'}});
}}
function revealSearchNode(node) {{
  var cur = node;
  while (cur) {{
    if (cur.tagName && cur.tagName.toLowerCase() === 'details') cur.open = true;
    cur = cur.parentElement;
  }}
  var detail = node.closest('.detail');
  if (detail && !detail.classList.contains('show')) {{
    var ms = detail.closest('.statement');
    if (ms) {{
      ms.querySelectorAll('.detail').forEach(function(d) {{ d.classList.remove('show'); }});
      ms.querySelectorAll('.thumb').forEach(function(t) {{ t.classList.remove('active'); }});
    }}
    detail.classList.add('show');
    var thumb = document.querySelector('[data-detail="' + detail.id + '"]');
    if (thumb) thumb.classList.add('active');
  }}
}}
document.addEventListener('keydown', function(event) {{
  var target = event.target;
  var tag = target && target.tagName ? target.tagName.toLowerCase() : '';
  var typing = tag === 'input' || tag === 'textarea' || (target && target.isContentEditable);
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {{
    event.preventDefault();
    openReportSearch();
    return;
  }}
  if (!typing && event.key === '/') {{
    event.preventDefault();
    openReportSearch();
    return;
  }}
  if (event.key === 'Escape') {{
    closeReportSearch();
  }}
}});
</script>
</body>
</html>
"""

TIMING_COLORS: dict[str, str] = {
    "transition": "#3b82f6",
    "action_policy": "#22c55e",
    "read": "#0f766e",
    "tool_agent.master": "#7c3aed",
    "tool_agent.worker": "#2563eb",
    "tool_agent.perception": "#0891b2",
    "transform": "#16a34a",
}

KIND_BADGE = {
    "interact": "statement-badge-action",
    "acquire": "statement-badge-collection",
    "read": "statement-badge-collection",
    "command": "statement-badge-navigation",
}

AT_LABELS = {
    "tap": "点击", "type": "输入", "scroll": "滚动", "drag": "拖动",
    "home": "主屏", "press_enter": "回车", "clear_text": "清空", "none": "跳过",
    # browser-only actions — missing these left upload/nav/etc. thumbnails with empty
    # labels (T4 · "") since the badge lookup falls back to "" (log 20260616_222207).
    "upload": "上传", "navigate": "导航", "back": "后退",
    "new_tab": "新标签页", "select_tab": "切标签页", "close_tab": "关标签页",
    "select_option": "选项",
    "acquire": "采集", "read": "绑定", "command": "命令", "non_ui": "非交互",
}


def _short_mid(mid) -> str:
    """Display-only short id: leading number of a slug ('1_open_wechat' -> '1').

    The full id is still used for anchors/links; this only shortens what's shown
    so a long slug doesn't crowd out the statement name in the sidebar.
    """
    m = re.match(r"\s*(\d+)", str(mid))
    return m.group(1) if m else str(mid)


def _render_timing_html(timings: dict[str, float]) -> str:
    if not timings:
        return ""
    total = sum(timings.values())
    if total <= 0:
        return ""
    segs = ""
    labels = ""
    for tname, tval in timings.items():
        pct = tval / total * 100
        tc = TIMING_COLORS.get(tname, "#94a3b8")
        segs += f'<div class="timing-seg" style="width:{pct:.1f}%;background:{tc}" title="{tname}: {tval:.2f}s"></div>'
        labels += (
            f'<span><span class="timing-label-dot" style="background:{tc}"></span>'
            f'{tname} {tval:.1f}s</span>'
        )
    return (
        f'<div class="timing-bar">{segs}</div>'
        f'<div class="timing-labels">{labels} · LLM total {total:.1f}s</div>'
    )


def _module_total_seconds(step: ReportStep) -> float:
    return sum(float(v or 0) for v in (step.timings or {}).values())


def _turn_elapsed_seconds(step: ReportStep, prev_timestamp: str = "") -> tuple[float, str]:
    module_total = _module_total_seconds(step)
    wall_gap = _gap_seconds(prev_timestamp, step.timestamp) if prev_timestamp and step.timestamp else None
    if wall_gap is not None and wall_gap >= 0:
        return wall_gap, "wall_gap"
    if step.timestamp and not prev_timestamp and module_total > 0:
        return module_total, "first_turn_elapsed_estimate"
    if module_total > 0:
        return module_total, "module_total"
    return 0.0, ""


def _step_diagnostic_flags(step: ReportStep) -> list[tuple[str, str]]:
    label_counts: dict[str, int] = {}
    for report in step.llm_context or []:
        if isinstance(report, dict) and report.get("kind") == "prompt_snapshot":
            label = str(report.get("label") or "module")
            label_counts[label] = label_counts.get(label, 0) + 1
    flags: list[tuple[str, str]] = []
    repeated = [f"{k} x{v}" for k, v in label_counts.items() if v > 1]
    if repeated:
        flags.append(("warn", "重复 " + ", ".join(repeated[:2])))
    if step.no_effect:
        flags.append(("warn", "no_effect"))
    if "✗" in step.status:
        flags.append(("error", "failed"))
    if step.operation_mode == "non_interactive":
        executor = str((step.non_ui or {}).get("executor") or "")
        label = {
            "acquire": "Acquire 采集",
            "read": "Read 观察绑定",
            "command": "Command",
        }.get(executor, "non-UI")
        flags.append(("normal", label))
    return flags


def _render_thumb_time(step: ReportStep, prev_timestamp: str = "") -> tuple[str, str]:
    search_bits: list[str] = []
    module_total = _module_total_seconds(step)
    for name, val in (step.timings or {}).items():
        search_bits.append(f"{name} {float(val or 0):.1f}s")
    for _, text in _step_diagnostic_flags(step):
        search_bits.append(text)

    elapsed, elapsed_kind = _turn_elapsed_seconds(step, prev_timestamp)
    if elapsed_kind == "wall_gap":
        search_bits.append(f"wall_gap {elapsed:.1f}s")
        return (
            f'<span class="thumb-time" title="距上一个动作：{elapsed:.1f}s">+{elapsed:.0f}s</span>',
            " ".join(search_bits),
        )
    if elapsed_kind == "first_turn_elapsed_estimate":
        search_bits.append(f"first_turn_elapsed_estimate {elapsed:.1f}s")
        return (
            '<span class="thumb-time" '
            f'title="首轮：从编排完成后起算，按模块耗时估算：{elapsed:.1f}s">'
            f'+{elapsed:.0f}s</span>',
            " ".join(search_bits),
        )
    if elapsed <= 0:
        return "", " ".join(search_bits)
    return (
        f'<span class="thumb-time" title="模块耗时合计：{elapsed:.1f}s">{elapsed:.1f}s</span>',
        " ".join(search_bits),
    )


def _render_context_decisions_html(reports: list[dict]) -> str:
    if not reports:
        return ""
    rows: list[str] = []
    summaries: list[str] = []
    for report in reports:
        kind = report.get("kind")
        label = _safe(str(report.get("label") or kind or "context"))
        if kind == "context_budget":
            included = int(report.get("included_count") or len(report.get("included") or []))
            dropped = int(report.get("dropped_count") or len(report.get("dropped") or []))
            chars = int(report.get("kept_chars") or 0)
            toks = int(report.get("kept_tokens") or report.get("estimated_tokens") or 0)
            summaries.append(f"{label}: +{included}/-{dropped}")
            rows.append(
                f'<div class="ctx-row"><strong>{label}</strong> '
                f'<span class="ctx-keep">included={included}</span> '
                f'<span class="ctx-drop">dropped={dropped}</span> '
                f'kept={chars} chars/{toks} tok · max={report.get("max_chars")}</div>'
            )
            for block in (report.get("blocks") or []):
                mark = "keep" if block.get("included") else "drop"
                cls = "ctx-keep" if block.get("included") else "ctx-drop"
                rows.append(
                    f'<div class="ctx-row {cls}">  {mark} {_safe(str(block.get("id") or ""))} '
                    f'source={_safe(str(block.get("source") or ""))} '
                    f'type={_safe(str(block.get("source_type") or ""))} '
                    f'prio={block.get("priority")} ttl={_safe(str(block.get("ttl") or ""))} '
                    f'budget={_safe(str(block.get("budget") or ""))} '
                    f'{block.get("estimated_chars", 0)} chars/{block.get("estimated_tokens", 0)} tok · '
                    f'{_safe(str(block.get("truncation_reason") or block.get("reason") or ""))}</div>'
                )
        elif kind == "context_compression":
            before = int(report.get("before_chars") or 0)
            after = int(report.get("after_chars") or 0)
            saved = int(report.get("saved_chars") or max(0, before - after))
            compressed = int(report.get("compressed_count") or 0)
            dropped = int(report.get("dropped_count") or 0)
            summaries.append(
                f"{label}: {before}→{after} · c{compressed}/d{dropped}"
            )
            rows.append(
                f'<div class="ctx-row"><strong>{label}</strong> '
                f'<span class="ctx-keep">saved={saved} chars</span> '
                f'{before} → {after} chars · '
                f'strategy={_safe(str(report.get("strategy") or ""))}</div>'
            )
            for decision in report.get("blocks") or report.get("decisions") or []:
                action = str(decision.get("action") or "compressed")
                original = decision.get(
                    "original_chars",
                    decision.get("before_count", 0),
                )
                final = decision.get(
                    "final_chars",
                    decision.get("after_count", 0),
                )
                rows.append(
                    f'<div class="ctx-row '
                    f'{"ctx-drop" if action == "dropped" else "ctx-keep"}">  '
                    f'{_safe(action)} '
                    f'{_safe(str(decision.get("id") or decision.get("path") or ""))} · '
                    f'{_safe(str(decision.get("strategy") or decision.get("operation") or ""))} · '
                    f'{original} → {final} chars · '
                    f'{_safe(str(decision.get("reason") or ""))}</div>'
                )
    summary = _safe(" · ".join(summaries[:4]) + (" ..." if len(summaries) > 4 else ""))
    return (
        f'<details class="ctx-detail"><summary>上下文决策 · {summary}</summary>'
        f'<div class="ctx-list">{"".join(rows)}</div></details>'
    )


def _gap_seconds(prev_iso: str, cur_iso: str) -> float | None:
    """Seconds between two ISO timestamps, or None if either is unparseable."""
    if not prev_iso or not cur_iso:
        return None
    try:
        return (datetime.fromisoformat(cur_iso) - datetime.fromisoformat(prev_iso)).total_seconds()
    except ValueError:
        return None


def _render_step_detail(step: ReportStep, detail_id: str, prev_timestamp: str = "", extra_html: str = "") -> str:
    """Render the expandable detail panel for a step.

    prev_timestamp: the previous action's timestamp, to show the inter-action gap.
    extra_html: optional content appended at the end of the panel.
    """
    at_cls = f"at-{step.action_type}"
    at_label = AT_LABELS.get(step.action_type, step.action_type)

    # Status
    if "✓" in step.status:
        status_cls, status_text = "thumb-status-ok", "✓"
    elif "✗" in step.status:
        status_cls, status_text = "thumb-status-fail", "✗"
    else:
        status_cls, status_text = "thumb-status-skip", "—"

    action_detail = _safe(step.description)
    if step.action_text:
        action_detail += f' <span style="color:#a032c8">「{_safe(step.action_text[:30])}」</span>'
    if step.action_direction:
        dir_label = {"up": "↑", "down": "↓", "left": "←", "right": "→"}.get(step.action_direction, step.action_direction)
        action_detail += f' <span style="color:#32b432">{dir_label}</span>'

    instruction_html = ""
    if step.instruction:
        instruction_html = f'<div class="detail-instruction">指令：{_safe(step.instruction)}</div>'
    summary_html = ""
    if step.summary and step.summary != step.description:
        summary_html = f'<div class="detail-summary">{_safe(step.summary)}</div>'
    non_ui_html = _render_non_ui_detail(step.non_ui) if step.non_ui else ""

    snap_html = ""
    if step.snap and step.snap.get("original"):
        method = step.snap.get("method", "?").upper()
        orig = step.snap["original"]
        snapped = step.snap.get("snapped")
        snap_color = "#22d3ee" if method == "DOM" else "#f59e0b" if method == "YOLO" else "#22c55e"
        dist = ""
        if snapped:
            d = ((orig[0] - snapped[0]) ** 2 + (orig[1] - snapped[1]) ** 2) ** 0.5
            dist = f' · <span style="color:{snap_color}">距离 {d:.1f}</span>'
        snap_html = (
            f'<div class="detail-instruction">'
            f'<span style="color:{snap_color};font-weight:600">{method}</span> 吸附 '
            f'({orig[0]:.0f},{orig[1]:.0f})→({snapped[0]:.0f},{snapped[1]:.0f})'
            f'{dist}</div>'
        )

    # Progressive knowledge injected into the unified Transition this turn.
    sections_html = ""
    loaded = step.sections_loaded
    if loaded:
        names = "、".join(_safe(s.replace("_", " ")) for s in loaded)
        sections_html = (
            f'<div class="detail-instruction">'
            f'<span style="color:#0891b2;font-weight:600">📖 注入知识 {len(loaded)} 章节</span>：{names}'
            f'</div>'
        )

    ss_html = ""
    if step.annotated_before_url:
        # Thumbnail shows the annotated (downscaled) frame; zoom opens the FULL-RES ANNOTATED frame
        # so the action marker stays visible at full size (previously zoom fell back to the raw
        # screenshot and dropped the annotation — log 20260616_222207).
        zoom_src = step.annotated_full_url or step.raw_screenshot_url or step.annotated_before_url
        ss_html = f'<div class="detail-ss"><img src="{step.annotated_before_url}" onclick="zoomImg(\'{zoom_src}\')" alt="Turn"></div>'

    # Absolute wall-clock time the action executed (PolicyTurn.timestamp, captured
    # right after dispatch). Show the time-of-day prominently, full ISO on hover.
    time_html = ""
    if step.timestamp:
        ts = step.timestamp
        disp = ts.split("T", 1)[1] if "T" in ts else ts
        gap = _gap_seconds(prev_timestamp, ts)
        if gap is not None:
            gap_html = f'<span class="detail-gap" title="距上一个动作">+{gap:.0f}s</span>'
        else:
            first_gap = _module_total_seconds(step) if not prev_timestamp else 0.0
            gap_html = (
                '<span class="detail-gap" '
                f'title="首轮：从编排完成后起算，按模块耗时估算：{first_gap:.1f}s">'
                f'+{first_gap:.0f}s</span>'
                if first_gap > 0 else ""
            )
        time_html = f'<span class="detail-time" title="{_safe(ts)}">🕒 {_safe(disp)}</span>{gap_html}'

    return f"""
    <div class="detail" id="{detail_id}">
      {ss_html}
      <div class="detail-info">
        <div class="detail-top">
          <span class="detail-idx {at_cls}">{step.label.split()[-1]}</span>
          <span class="detail-at" style="background:#f1f5f9;color:#475569">{at_label}</span>
          <span class="detail-desc">{action_detail}</span>
          {time_html}
          <span class="detail-status {status_cls.replace('thumb-status', 'detail-status')}">{step.status}</span>
        </div>
        {instruction_html}
        {snap_html}
        {sections_html}
        {summary_html}
        {non_ui_html}
        {_render_module_io_html(step.llm_context, step.token_usage)}
        {_render_timing_html(step.timings)}
        {extra_html}
      </div>
    </div>"""


def _render_knowledge_html(k: dict, sections: list[dict] | None = None) -> str:
    """Sidebar section: which app knowledge was injected this run, how big it was, and the
    list of sections actually injected (≥1 turn) — each clickable to view its body in a modal.
    Per-turn injection is also marked on each turn card (📖). Empty when no knowledge matched."""
    if not k:
        return ""
    app = _safe(str(k.get("app_name", "")))
    sc = k.get("section_count", 0)
    nav = k.get("nav_chars", 0)
    _k = lambda n: f"{n / 1000:.1f}k" if n >= 1000 else str(n)  # noqa: E731
    out = [
        '<div class="sidebar-knowledge">',
        '<div class="sidebar-title">知识库</div>',
        f'<div class="sk-app">{app}</div>',
        f'<div class="sk-meta">{sc} 章节 · 导航 {_k(nav)} 字</div>',
    ]
    # Hand-maintained overlay channels — loaded (with size) vs absent. Human labels; the file
    # stem is in the hover title for traceability. Skipped entirely when no overlay exists.
    overlays = k.get("overlays") or {}
    if overlays:
        _CH = [("_check", "验收"), ("_deploy", "部署"), ("_skill", "技能"), ("_update", "更新")]
        out.append(f'<div class="sk-sections-label">动态知识 · {len(overlays)}</div>')
        chips = []
        for stem, label in _CH:
            if stem in overlays:
                chips.append(f'<span class="sk-ch on" data-tip="{stem}.md · {_k(overlays[stem])} 字">{label}</span>')
            else:
                chips.append(f'<span class="sk-ch off" data-tip="{stem}.md（未加载）">{label}</span>')
        out.append('<div class="sk-channels">' + "".join(chips) + "</div>")
    sections = sections or []
    if sections:
        out.append(f'<div class="sk-sections-label">已加载章节 · {len(sections)}</div>')
        bodies = []
        for i, s in enumerate(sections):
            title = _safe(str(s.get("title", "")))
            # this.textContent passes the title to the modal — no JS string escaping needed.
            out.append(
                f'<div class="sk-item" onclick="showSection(\'sk-body-{i}\', this.textContent)">'
                f'{title}</div>'
            )
            body = _safe(str(s.get("body") or "（未找到该章节的知识文件）"))
            bodies.append(f'<div id="sk-body-{i}">{body}</div>')
        out.append('<div id="sk-bodies" style="display:none">' + "".join(bodies) + "</div>")
    out.append("</div>")
    return "".join(out)


def _render_platform_badge(platform: str) -> str:
    """Small colored badge next to the title showing the run platform. Empty for
    old logs that predate the context.platform field."""
    if not platform:
        return ""
    icon = {"iphone": "📱", "browser": "🌐", "android": "🤖"}.get(platform, "🖥")
    label = {"iphone": "iPhone", "browser": "Browser", "android": "Android"}.get(
        platform, _safe(platform)
    )
    return (
        '<span class="platform-badge" style="display:inline-block;margin-left:10px;'
        "padding:2px 11px;border-radius:11px;background:#2f81f7;color:#fff;"
        'font-size:13px;font-weight:600;vertical-align:middle;">'
        f"{icon} {label}</span>"
    )


def _phase_meta(data: ReportData) -> tuple[str, str, str]:
    status = (data.phase or "").strip()
    if not status:
        status = data.phase or "stopped"
    if status == "completed":
        return "completed", "正常完成", "目标已确认完成"
    if status == "interrupted":
        return "interrupted", "用户中止", "按 ESC 或手动退出后，当前 turn 已安全收尾"
    if status == "failed":
        return "failed", "执行失败", "Program 因 statement 失败而终止"
    return "stopped", "未完成停止", "任务未确认完成"


def _render_phase_badge(data: ReportData) -> str:
    cls, label, detail = _phase_meta(data)
    reason = data.summary.strip()
    tip = f"停止原因：{reason}" if reason else detail
    tip_attr = _safe(tip).replace('"', "&quot;")
    return (
        f'<span class="run-status-badge run-status-badge-{cls}" '
        f'data-tip="{tip_attr}">{_safe(label)}</span>'
    )


def _render_webarena_result(webarena: dict) -> str:
    """Render the WebArena grading result plus the task context that produced it."""
    if not webarena:
        return ""
    response = webarena.get("agent_response") or {}
    if not isinstance(response, dict) or not response:
        return ""

    status = str(response.get("status") or "")
    ok = status.upper() == "SUCCESS"
    task_type = str(response.get("task_type") or "")
    retrieved = response.get("retrieved_data")
    error = response.get("error_details")
    task_id = webarena.get("task_id")
    sites = webarena.get("sites") or []
    sites_text = ", ".join(map(str, sites)) if isinstance(sites, list) else str(sites)
    start_url = str(webarena.get("start_url") or "")
    output_dir = str(webarena.get("task_output_dir") or "")
    eval_result = webarena.get("eval_result") if isinstance(webarena.get("eval_result"), dict) else {}
    eval_status = str(eval_result.get("status") or "")
    eval_score = eval_result.get("score")
    official_ok = eval_status.lower() == "success" if eval_status else ok
    evaluator_items = [item for item in (eval_result.get("evaluators_results") or []) if isinstance(item, dict)]
    primary_eval = evaluator_items[0] if evaluator_items else {}
    gt_value = None
    ours_value = retrieved
    expected = primary_eval.get("expected") if isinstance(primary_eval.get("expected"), dict) else {}
    actual_normalized = (
        primary_eval.get("actual_normalized")
        if isinstance(primary_eval.get("actual_normalized"), dict)
        else {}
    )
    if expected:
        gt_value = expected.get("retrieved_data")
    if actual_normalized and "retrieved_data" in actual_normalized:
        ours_value = actual_normalized.get("retrieved_data")

    def _json_inline(value) -> str:
        return _safe(json.dumps(value, ensure_ascii=False)) if value is not None else "null"

    primary_chip_cls = "wa-chip" if official_ok else "wa-chip wa-chip-error"
    card_cls = "wa-card" if official_ok else "wa-card wa-card-error"
    meta_bits = []
    if task_id not in (None, ""):
        meta_bits.append(f"task {task_id}")
    if sites_text:
        meta_bits.append(sites_text)
    if eval_result and eval_score is not None:
        meta_bits.append(f"score {eval_score}")
    meta_html = f'<span class="wa-meta">{_safe(" · ".join(meta_bits))}</span>' if meta_bits else ""

    rows = ""
    if eval_result:
        evaluator_names = [
            str(item.get("evaluator_name") or "Evaluator")
            for item in evaluator_items
        ]
        rows += f'<div class="wa-k">evaluator_name</div><div class="wa-v">{_safe(", ".join(evaluator_names) or "—")}</div>'
        rows += f'<div class="wa-k">task_type</div><div class="wa-v">{_safe(task_type or "—")}</div>'
        rows += f'<div class="wa-k">Answer</div><div class="wa-v">{_json_inline(gt_value)}</div>'
        rows += f'<div class="wa-k">Response</div><div class="wa-v">{_json_inline(ours_value)}</div>'
        assertion_bits: list[str] = []
        for item in evaluator_items:
            assertions = item.get("assertions") or []
            if assertions:
                for assertion in assertions[:3]:
                    if isinstance(assertion, dict):
                        msgs = assertion.get("assertion_msgs") or []
                        msg = "; ".join(map(str, msgs)) if isinstance(msgs, list) else str(msgs)
                        assertion_bits.append(f'{assertion.get("assertion_name") or "assertion"}: {msg}')
        if assertion_bits:
            rows += f'<div class="wa-k">Assertions</div><div class="wa-v wa-assert">{_safe("; ".join(assertion_bits))}</div>'
    else:
        rows += (
            f'<div class="wa-k">提交状态</div><div class="wa-v">{_safe(status or "UNKNOWN")}</div>'
            f'<div class="wa-k">task_type</div><div class="wa-v">{_safe(task_type or "—")}</div>'
            f'<div class="wa-k">retrieved_data</div><div class="wa-v">{_json_inline(retrieved)}</div>'
            f'<div class="wa-k">error_details</div><div class="wa-v">{_safe(str(error)) if error is not None else "null"}</div>'
    )
    if start_url:
        rows += f'<div class="wa-k">Start URL</div><div class="wa-v">{_safe(start_url)}</div>'
    if output_dir:
        rows += f'<div class="wa-k">Output Dir</div><div class="wa-v">{_safe(output_dir)}</div>'

    label = "WebArena" if eval_result else "WebArena 最终输出"
    primary_status = eval_status if eval_result else status
    return (
        f'<div class="{card_cls}">'
        f'<div class="wa-head">'
        f'<span class="wa-label">{_safe(label)}</span>'
        f'<span class="{primary_chip_cls}">{_safe(primary_status or "UNKNOWN")}</span>'
        f'{meta_html}'
        f'</div>'
        f'<div class="wa-grid">'
        f'{rows}'
        f'</div>'
        f'</div>'
    )


def _render_mobileworld_result(mobileworld: dict) -> str:
    """Render the MobileWorld official (state-based) grading verdict — same card format
    as the WebArena result, just sourced from the ``mobileworld`` context block."""
    if not mobileworld:
        return ""
    score = mobileworld.get("score")
    reason = str(mobileworld.get("reason") or "")
    task_name = str(mobileworld.get("task_name") or "")
    goal = str(mobileworld.get("goal") or "")
    base_url = str(mobileworld.get("base_url") or "")
    adb_serial = str(mobileworld.get("adb_serial") or "")
    graded = score is not None
    ok = graded and float(score) > 0
    status = ("SUCCESS" if ok else "FAIL") if graded else "NOT GRADED"

    chip_cls = "wa-chip" if ok else "wa-chip wa-chip-error"
    card_cls = "wa-card" if ok else "wa-card wa-card-error"
    meta_bits = []
    if task_name:
        meta_bits.append(task_name)
    if graded:
        meta_bits.append(f"score {score}")
    meta_html = f'<span class="wa-meta">{_safe(" · ".join(meta_bits))}</span>' if meta_bits else ""

    rows = ""
    if goal:
        rows += f'<div class="wa-k">Goal</div><div class="wa-v">{_safe(goal)}</div>'
    rows += f'<div class="wa-k">Score</div><div class="wa-v">{_safe(str(score) if graded else "—")}</div>'
    if reason:
        rows += f'<div class="wa-k">Reason</div><div class="wa-v">{_safe(reason)}</div>'
    if base_url:
        rows += f'<div class="wa-k">Backend</div><div class="wa-v">{_safe(base_url)}</div>'
    if adb_serial:
        rows += f'<div class="wa-k">adb</div><div class="wa-v">{_safe(adb_serial)}</div>'

    return (
        f'<div class="{card_cls}">'
        f'<div class="wa-head">'
        f'<span class="wa-label">MobileWorld</span>'
        f'<span class="{chip_cls}">{_safe(status)}</span>'
        f'{meta_html}'
        f'</div>'
        f'<div class="wa-grid">'
        f'{rows}'
        f'</div>'
        f'</div>'
    )


def _render_provenance(raw_input: str, goal: str, router: dict) -> str:
    """Header row showing the Router/input-resolution result for the run.

    Always rendered (when raw_input is known) so every report states the router
    status explicitly — router goal, a clarification request, a deterministic
    temporal rewrite, or '未经 router' for bin/runner direct runs. The <h1> title
    is the raw input; this row shows what it resolved to and how.
    """
    if not raw_input:
        return ""  # old logs without provenance — nothing to show

    changed = bool(goal) and goal != raw_input
    if router and router.get("needs_clarification"):
        via_cls, via_text = "prov-via-clarify", "需澄清"
        body = f'需要澄清：{_safe(router.get("clarification", "") or "—")}'
    elif router:
        via_cls, via_text = "prov-via-router", "router"
        body = (
            f'<span class="prov-goal">{_safe(goal)}</span>' if not changed else
            f'{_safe(raw_input)}<span class="prov-arrow">→</span>'
            f'<span class="prov-goal">{_safe(goal)}</span>'
        )
    elif changed:
        via_cls, via_text = "prov-via-temporal", "temporal · 未经 router"
        body = (
            f'{_safe(raw_input)}<span class="prov-arrow">→</span>'
            f'<span class="prov-goal">{_safe(goal)}</span>'
        )
    else:
        via_cls, via_text = "prov-via-none", "未经 router"
        body = f'<span class="prov-goal">{_safe(goal or raw_input)}</span>（输入未改写）'

    return (
        f'<div class="meta-strip">'
        f'<span class="meta-strip-label">Router</span>'
        f'{body}'
        f'<span class="prov-via {via_cls}">{via_text}</span>'
        f'</div>'
    )


def _render_presentation_stage(orchestrator: dict) -> str:
    presentation = orchestrator.get("presentation")
    if not isinstance(presentation, dict):
        return ""
    status = str(presentation.get("status") or "fallback")
    status_label = "LLM · 通过" if status == "generated" else "确定性降级"
    phase_cls = "coding-phase-ok" if status == "generated" else "coding-phase-warn"
    elapsed = float(presentation.get("elapsed_s") or 0)
    calls = int(presentation.get("llm_calls") or 0)
    raw_usage = (
        presentation.get("token_usage")
        if isinstance(presentation.get("token_usage"), dict)
        else {}
    )
    usage = {"tool_agent.presentation": raw_usage} if raw_usage else {}
    ti, to = _sum_tokens(usage)
    metrics = []
    if elapsed:
        metrics.append(f"{elapsed:.1f}s")
    if calls:
        metrics.append(f"{calls} call{'s' if calls != 1 else ''}")
    if ti or to:
        metrics.append(f"{_fmt_tokens(ti)}/{_fmt_tokens(to)} tok")
        metrics.append(f"≈{pricing_currency()}{_token_cost(usage):.4f}")
    metrics_html = (
        f'<span class="statement-time">{_safe(" · ".join(metrics))}</span>'
        if metrics else ""
    )
    digest = str(presentation.get("result_digest") or "")
    model = str(presentation.get("model") or "")
    error = str(presentation.get("error") or "")
    contract_bits = [
        "input: goal + public result + replay verdict",
        "capabilities: none",
    ]
    if digest:
        contract_bits.append(f"result sha256: {digest[:12]}…")
    if model:
        contract_bits.append(f"model: {model}")
    contract_text = "\n".join(contract_bits)
    error_html = (
        f'<pre class="nonui-code">{_safe(error)}</pre>' if error else ""
    )
    reports = [
        item for item in (presentation.get("context_reports") or [])
        if isinstance(item, dict)
    ]
    return (
        '<div class="statement" id="stage-presentation">'
        '<div class="statement-header">'
        '<h2>#P</h2>'
        '<span class="statement-name">Presentation · User-facing reply</span>'
        '<span class="statement-badge statement-badge-default">result → reply</span>'
        f'<span class="coding-phase {phase_cls}">{_safe(status_label)}</span>'
        f'{metrics_html}'
        '</div>'
        '<div class="nonui-detail">'
        '<div class="nonui-title">独立表达阶段</div>'
        f'<pre class="nonui-code">{_safe(contract_text)}</pre>'
        f'{error_html}'
        '</div>'
        f'{_render_module_io_html(reports, usage) if reports else ""}'
        '</div>'
    )

def generate_html(data: ReportData, grid: bool = False) -> str:
    stats_parts = [f"{k}: {v}" for k, v in data.stats.items()]
    orchestrator_timings = (
        data.orchestrator.get("timings")
        if isinstance(data.orchestrator.get("timings"), dict)
        else {}
    )
    presentation = (
        data.orchestrator.get("presentation")
        if isinstance(data.orchestrator.get("presentation"), dict)
        else {}
    )
    presentation_elapsed = float(presentation.get("elapsed_s") or 0)
    presentation_usage_raw = (
        presentation.get("token_usage")
        if isinstance(presentation.get("token_usage"), dict)
        else {}
    )
    presentation_usage = (
        {"tool_agent.presentation": presentation_usage_raw}
        if presentation_usage_raw else {}
    )
    llm_s = (
        sum(m.get("total_time", 0) for m in data.statements)
        + sum(float(value or 0) for value in orchestrator_timings.values())
        + presentation_elapsed
    )
    if data.wall_clock_s:
        # True end-to-end elapsed, split into LLM compute, settle waits, and "other"
        # (perception / action execution / scheduling overhead = wall − LLM − settle).
        other_s = max(0.0, data.wall_clock_s - llm_s - data.settle_s_total)
        stats_parts.append(
            f"elapsed: {data.wall_clock_s:.1f}s "
            f"(LLM {llm_s:.1f} · settle {data.settle_s_total:.1f} · other {other_s:.1f})"
        )
    else:
        stats_parts.append(f"LLM: {llm_s:.1f}s")  # old logs without wall_clock_s
    orchestrator_usage = (
        data.orchestrator.get("token_usage")
        if isinstance(data.orchestrator.get("token_usage"), dict)
        else {}
    )
    orchestrator_in, orchestrator_out = _sum_tokens(orchestrator_usage)
    presentation_in, presentation_out = _sum_tokens(presentation_usage)
    sess_in = (
        sum(int(m.get("input_tokens", 0)) for m in data.statements)
        + orchestrator_in
        + presentation_in
    )
    sess_out = (
        sum(int(m.get("output_tokens", 0)) for m in data.statements)
        + orchestrator_out
        + presentation_out
    )
    sess_cached = (
        sum(
            _sum_cached_tokens(step.token_usage)
            for page in data.pages
            for step in page.steps
        )
        + sum(_sum_cached_tokens(page.outcome_token_usage) for page in data.pages)
        + _sum_cached_tokens(orchestrator_usage)
        + _sum_cached_tokens(presentation_usage)
    )
    sess_cost = (
        sum(float(m.get("cost", 0)) for m in data.statements)
        + _token_cost(orchestrator_usage)
        + _token_cost(presentation_usage)
    )
    if sess_in or sess_out:
        cache = _fmt_cache(sess_in, sess_cached)
        cache_text = f" / {cache}" if cache else ""
        stats_parts.append(
            f"tokens: in {_fmt_tokens(sess_in)} / out {_fmt_tokens(sess_out)}{cache_text}"
            f"  |  cost: ≈{pricing_currency()}{sess_cost:.4f}"
        )
    stats_str = "  |  ".join(stats_parts)

    # Display ordinal per statement (#1, #2, …). StatementContract ids are descriptive slugs now;
    # keep the raw id for anchors while using a compact ordinal in statement headings.
    _mid_ordinal = {m.get("id", ""): i for i, m in enumerate(data.statements, 1)}
    def _mid_disp(mid: str) -> str:
        return str(_mid_ordinal[mid]) if mid in _mid_ordinal else _short_mid(mid)

    def _render_checklist(items: list[dict], cid: str) -> tuple[str, str]:
        if not items:
            return "", ""
        marks = {"done": "✓", "pending": "·", "blocked": "!", "skipped": "-"}
        done_n = sum(1 for it in items if str(it.get("status") or "") == "done")
        rows = []
        for item in items[:10]:
            status = str(item.get("status") or "pending")
            if status not in marks:
                status = "pending"
            evidence = item.get("evidence") or []
            evidence_text = str(evidence[0]) if evidence else ""
            evidence_html = (
                f'<span class="statement-check-evidence">{_safe(evidence_text)}</span>'
                if evidence_text else ""
            )
            rows.append(
                f'<div class="statement-check statement-check-{status}">'
                f'<span class="statement-check-mark">{marks[status]}</span>'
                f'<span class="statement-check-text">{_safe(str(item.get("text") or ""))}{evidence_html}</span>'
                f'</div>'
            )
        # A compact status badge that sits beside the kind badge; the rows live in a separate
        # hidden block (placed at the end of the statement) cloned into the shared #cl-modal popup
        # on click (showChecklist). Returns (badge_html, hidden_data_html).
        all_done = done_n == len(items)
        badge_cls = "checklist-badge-ok" if all_done else "checklist-badge-partial"
        badge = (
            f'<span class="checklist-badge {badge_cls}" onclick="showChecklist(\'{cid}\')">'
            f'✓ {done_n}/{len(items)} 验收</span>'
        )
        data = f'<div id="{cid}" class="checklist-data" style="display:none">{"".join(rows)}</div>'
        return badge, data

    coding_mode = has_coding_program(data.orchestrator)
    # Coding mode: index run_log by instance/statement id so each card can show a data strip.
    coding_run_by_key: dict[str, dict] = {}
    coding_plan_by_sid: dict[str, dict] = {}
    coding_line_by_call_id: dict[str, int] = {}
    if coding_mode:
        coding_plan_by_sid = coding_plan_expansion_by_sid(data.orchestrator)
        coding_line_by_call_id = coding_source_line_by_call_id(data.orchestrator)
        for entry in (data.orchestrator or {}).get("report_run_log") or []:
            if not isinstance(entry, dict):
                continue
            for key in (
                str(entry.get("instance_id") or ""),
                _coding_statement_id(entry),
            ):
                if key and key not in coding_run_by_key:
                    coding_run_by_key[key] = entry
    statements_by_key: dict[str, dict] = {}
    for item in data.statements or []:
        if not isinstance(item, dict):
            continue
        for key in (str(item.get("instance_id") or ""), str(item.get("id") or "")):
            if key and key not in statements_by_key:
                statements_by_key[key] = item

    pages_html = ""
    page_chunks: list[dict] = []
    prev_ts = ""  # carries across pages so the gap is vs the previous turn globally
    for page in data.pages:
        badge_cls = KIND_BADGE.get(page.statement_executor, "statement-badge-default")
        ms_in = sum(_sum_tokens(s.token_usage)[0] for s in page.steps)
        ms_out = sum(_sum_tokens(s.token_usage)[1] for s in page.steps)
        ms_cost = sum(_token_cost(s.token_usage) for s in page.steps)
        outcome_in, outcome_out = _sum_tokens(page.outcome_token_usage)
        ms_in += outcome_in
        ms_out += outcome_out
        ms_cost += _token_cost(page.outcome_token_usage)
        ms_tok_html = (
            f' · {_fmt_tokens(ms_in)}/{_fmt_tokens(ms_out)} tok · ≈{pricing_currency()}{ms_cost:.4f}'
            if (ms_in or ms_out) else ""
        )
        mid_safe = _safe(page.statement_id)       # full id — anchor target
        mid_disp = _safe(_mid_disp(page.statement_id))  # ordinal — heading

        thumbs_html = ""
        details_html = ""
        ms_elapsed = 0.0
        for si, step in enumerate(page.steps):
            turn_no = step.label.split()[-1]
            detail_id = f"detail-ms{mid_safe}-s{si}"
            turn_elapsed, _ = _turn_elapsed_seconds(step, prev_ts)
            ms_elapsed += turn_elapsed

            # Status indicator
            if "✓" in step.status:
                status_cls, status_text = "thumb-status-ok", "✓"
            elif "✗" in step.status:
                status_cls, status_text = "thumb-status-fail", "✗"
            else:
                status_cls, status_text = "thumb-status-skip", "—"

            at_cls = f"at-{step.action_type}"
            at_label = AT_LABELS.get(step.action_type, "")

            # Thumbnail
            if step.annotated_before_url:
                thumb_time_html, thumb_search = _render_thumb_time(step, prev_timestamp=prev_ts)
                thumb_search_index = _attr(
                    " ".join([
                        f"T{turn_no}",
                        at_label,
                        thumb_search,
                        step.description or "",
                        step.summary or "",
                    ])
                )
                thumbs_html += (
                    f'<div class="thumb" data-detail="{detail_id}" data-search-index="{thumb_search_index}" onclick="showDetail(\'{detail_id}\')">'
                    f'<img src="{step.annotated_before_url}" alt="Turn {turn_no}">'
                    f'<div class="thumb-action {at_cls}" title="{at_label}"></div>'
                    f'<div class="thumb-status {status_cls}">{status_text}</div>'
                    f'<div class="thumb-label">T{turn_no} · {at_label}{thumb_time_html}</div>'
                    f'</div>'
                )

            # Detail panel (hidden until clicked)
            details_html += _render_step_detail(
                step,
                detail_id,
                prev_timestamp=prev_ts,
            )
            if step.timestamp:
                prev_ts = step.timestamp
        ms_elapsed += sum(float(v or 0.0) for v in page.outcome_timings.values())

        # Avoid rendering the statement title twice when description == name.
        desc_html = (
            f'<div class="statement-desc">{_safe(page.statement_description)}</div>'
            if page.statement_description and page.statement_description.strip() != page.statement_name.strip()
            else ""
        )
        # Success criteria are replaced by the actual ctx.* call signature when structured
        # runtime metadata is available below.
        sc_html = (
            f'<div class="statement-sc">验收：{_safe(page.statement_success)}</div>'
            if page.statement_success else ""
        )
        checklist_badge, checklist_data = _render_checklist(page.checklist, f"cl-{mid_safe}")
        ms_time_html = (
            f'总耗时 {ms_elapsed:.1f}s'
            if ms_elapsed > 0
            else "耗时未记录"
        )
        ms_time_title = (
            "总耗时=本 statement 内缩略图时间求和；首轮从编排完成后起算，后续按 turn timestamp gap"
        )
        verify_thumb = ""
        verify_detail = ""
        if page.verify_url and page.steps:
            vd_id = f"detail-ms{mid_safe}-verify"
            _vt_border = "#22c55e40"
            _vt_status = '<div class="thumb-status thumb-status-ok">✓</div>'
            _vt_label = (
                '<div class="thumb-label" style="background:linear-gradient(transparent, '
                'rgba(34,197,94,0.7))">验收</div>'
            )
            verify_thumb = (
                f'<div class="thumb" data-detail="{vd_id}" onclick="showDetail(\'{vd_id}\')" style="border-color:{_vt_border}">'
                f'<img src="{page.verify_url}" alt="验收截图">'
                f'{_vt_status}'
                f'{_vt_label}'
                f'</div>'
            )
            ck = page.verify_outcome
            ck_status = ck.get("status", "")
            ck_reason = ck.get("reason", "")
            ck_identity = ck.get("page_identity", "")
            ck_summary = ck.get("summary", "")
            if ck_status == "done":
                badge = '<span style="background:#dcfce7;color:#166534;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">✓ done</span>'
            elif ck_status == "in_progress":
                badge = '<span style="background:#fef9c3;color:#854d0e;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">⏳ in_progress</span>'
            else:
                badge = ""
            identity_html = f'<span style="font-size:11px;color:#64748b;margin-left:6px">{_safe(ck_identity)}</span>' if ck_identity else ""
            reason_html = f'<div class="detail-instruction" style="margin-top:4px">{_safe(ck_reason)}</div>' if ck_reason else ""
            summary_html = f'<div class="detail-summary">{_safe(ck_summary)}</div>' if ck_summary and ck_summary != ck_reason else ""
            verify_detail = (
                f'<div class="detail" id="{vd_id}">'
                f'<div class="detail-ss"><img src="{page.verify_url}" onclick="zoomImg(this.src)" alt="验收截图"></div>'
                f'<div class="detail-info">'
                f'<div class="detail-top" style="flex-wrap:wrap;gap:8px">'
                f'<span style="font-size:11px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.04em">验收结果</span>'
                f'{badge}{identity_html}'
                f'</div>'
                f'{reason_html}'
                f'{summary_html}'
                f'{_render_module_io_html(page.outcome_context, page.outcome_token_usage)}'
                f'{_render_timing_html(page.outcome_timings)}'
                f'</div>'
                f'</div>'
            )
        turns_label = f"{len(page.steps)} turns" if page.steps else "无交互 turn"
        # Coding: statement-local data strip (ctx.op / inputs / outputs) above the gallery.
        data_panel_html = ""
        phase_chip = ""
        ctx_chip = ""
        page_call_id = ""
        page_call_op = ""
        page_call_label = ""
        page_call_phase = ""
        if coding_mode:
            run_entry = (
                coding_run_by_key.get(page.instance_id)
                or coding_run_by_key.get(page.statement_id)
                or {}
            )
            result = run_entry.get("result") if isinstance(run_entry.get("result"), dict) else {}
            stmt_meta = (
                statements_by_key.get(page.instance_id)
                or statements_by_key.get(page.statement_id)
                or {}
            )
            inputs = (
                run_entry.get("coding_payload")
                if isinstance(run_entry.get("coding_payload"), dict)
                else None
            )
            if not inputs and isinstance(stmt_meta.get("inputs"), dict):
                inputs = stmt_meta["inputs"]
            outputs = result.get("outputs") if isinstance(result.get("outputs"), dict) else None
            if not outputs and isinstance(stmt_meta.get("outputs"), dict):
                outputs = stmt_meta["outputs"]
            phase = str(result.get("phase") or stmt_meta.get("phase") or "")
            page_call_phase = phase
            if phase:
                phase_cls = {
                    "completed": "coding-phase-ok",
                    "exhausted": "coding-phase-fail",
                    "failed": "coding-phase-fail",
                }.get(phase, "coding-phase-warn")
                phase_chip = f'<span class="coding-phase {phase_cls}">{_safe(phase)}</span>'
            coding_op = str(run_entry.get("coding_op") or "")
            op = _infer_coding_op(
                coding_op=coding_op,
                executor=page.statement_executor,
                inputs=inputs if isinstance(inputs, dict) else {},
            )
            flat = _flatten_coding_inputs(
                coding_op=op,
                coding_payload=run_entry.get("coding_payload")
                if isinstance(run_entry.get("coding_payload"), dict) else None,
                inputs=inputs if isinstance(inputs, dict) else {},
            )
            if op == "reach" and not flat.get("goal") and page.statement_name:
                flat = {**flat, "goal": page.statement_name}
            call_label = _coding_call_label(op, flat) if op else ""
            plan_meta = (
                coding_plan_by_sid.get(page.statement_id)
                or coding_plan_by_sid.get(page.instance_id)
                or {}
            )
            page_call_id = str(
                run_entry.get("coding_call_id")
                or plan_meta.get("call_id")
                or ""
            )
            page_call_op = str(plan_meta.get("plan_op") or op or "")
            page_call_label = (
                _coding_call_label(page_call_op, flat)
                if page_call_op else call_label
            )
            # Single badge: plan+step when expanded, else ctx.op. Avoid double chips.
            plan_expanded = bool(
                plan_meta.get("plan_op")
                and (
                    int(plan_meta.get("plan_steps") or 0) > 1
                    or plan_meta.get("plan_op") != op
                )
            )
            if plan_expanded:
                plan_op = str(plan_meta.get("plan_op") or "")
                step = int(plan_meta.get("plan_step") or 1)
                steps = int(plan_meta.get("plan_steps") or 1)
                badge_text = f"ctx.{plan_op} {step}/{steps}"
                if op and op != plan_op:
                    badge_text += f" · {op}"
                ctx_chip = (
                    f'<span class="coding-ctx-badge" title="{_attr(str(plan_meta.get("label") or call_label))}">'
                    f'{_safe(badge_text)}</span>'
                )
            elif op:
                ctx_chip = (
                    f'<span class="coding-ctx-badge" title="{_attr(call_label)}">'
                    f'ctx.{_safe(op)}</span>'
                )
            # Header subtitle: only the step call signature (plan info lives in the badge).
            if call_label:
                sc_html = (
                    f'<div class="statement-sc coding-call-sc">'
                    f'<code>{_safe(call_label)}</code></div>'
                )
            call_snapshot = (
                stmt_meta.get("call")
                if isinstance(stmt_meta.get("call"), dict)
                else None
            )
            data_panel_html = _render_coding_data_panel(
                name=page.statement_name,
                success=page.statement_success,
                result=result or {
                    "phase": phase,
                    "summary": str(stmt_meta.get("last_summary") or ""),
                },
                outputs=outputs,
                inputs=inputs if isinstance(inputs, dict) else {},
                coding_op=coding_op,
                coding_payload=run_entry.get("coding_payload")
                if isinstance(run_entry.get("coding_payload"), dict) else None,
                executor=page.statement_executor,
                call=call_snapshot,
                plan_meta=plan_meta,
                # Call signature already shown under the header; avoid repeating it.
                omit_call_label=True,
            )
        gallery_html = (
            f'<div class="gallery">{thumbs_html}{verify_thumb}</div>'
            if thumbs_html or verify_thumb else ""
        )
        page_html = f"""
        <div class="statement" id="ms-{mid_safe}">
          <div class="statement-header">
            <h2>#{mid_disp}</h2>
            <span class="statement-name">{_safe(page.statement_name)}</span>
            <span class="statement-badge {badge_cls}">{_safe(page.statement_executor)}</span>
            {ctx_chip}
            {phase_chip}
            {checklist_badge}
            <span class="statement-time" title="{_safe(ms_time_title)}">{ms_time_html} · {turns_label}{ms_tok_html}</span>
            {desc_html}
            {sc_html}
          </div>
          {data_panel_html}
          {gallery_html}
          {details_html}
          {verify_detail}
          {checklist_data}
        </div>"""
        page_chunks.append({
            "call_id": page_call_id,
            "op": page_call_op,
            "label": page_call_label,
            "phase": page_call_phase,
            "html": page_html,
        })

    if coding_mode:
        call_groups: list[dict] = []
        for index, chunk in enumerate(page_chunks, 1):
            key = str(chunk.get("call_id") or f"statement:{index}")
            if call_groups and call_groups[-1]["key"] == key:
                call_groups[-1]["chunks"].append(chunk)
            else:
                call_groups.append({"key": key, "chunks": [chunk]})
        group_lines = [
            coding_line_by_call_id.get(str(group["key"]))
            for group in call_groups
        ]
        line_totals = {
            lineno: group_lines.count(lineno)
            for lineno in group_lines if lineno is not None
        }
        line_seen: dict[int, int] = {}
        for call_no, group in enumerate(call_groups, 1):
            chunks = group["chunks"]
            op = str(chunks[0].get("op") or "?")
            label = str(chunks[0].get("label") or f"ctx.{op}(…)")
            phases = [str(chunk.get("phase") or "") for chunk in chunks]
            if phases and all(phase == "completed" for phase in phases):
                phase = "completed"
                phase_cls = "coding-phase-ok"
            elif any(phase in {"failed", "exhausted"} for phase in phases):
                phase = next(
                    phase for phase in phases if phase in {"failed", "exhausted"}
                )
                phase_cls = "coding-phase-fail"
            else:
                phase = next((value for value in phases if value), "")
                phase_cls = "coding-phase-warn"
            phase_html = (
                f'<span class="coding-phase {phase_cls}">{_safe(phase)}</span>'
                if phase else ""
            )
            lineno = group_lines[call_no - 1]
            if lineno is not None:
                line_seen[lineno] = line_seen.get(lineno, 0) + 1
                call_ref = f"L{lineno}"
                if line_totals[lineno] > 1:
                    call_ref += f" · {line_seen[lineno]}/{line_totals[lineno]}"
            else:
                call_ref = f"call {call_no}"
            pages_html += (
                f'<details class="coding-call-card" id="ctx-call-{call_no}">'
                f'<summary>'
                f'<span class="coding-call-index">{call_ref}</span>'
                f'<code class="coding-call-title">{_safe(label)}</code>'
                f'{phase_html}'
                f'<span class="coding-call-meta">{len(chunks)} 个内部阶段 · 默认收起</span>'
                f'</summary>'
                f'<div class="coding-call-body">'
                f'{"".join(str(chunk["html"]) for chunk in chunks)}'
                f'</div></details>'
            )
    else:
        pages_html = "".join(str(chunk["html"]) for chunk in page_chunks)

    program_html = _render_program_section(data.orchestrator)
    presentation_html = _render_presentation_stage(data.orchestrator)

    # Model-config box with an inline "参考单价" chip that pops the rate table on hover.
    cost_note_html = ""
    if data.models or sess_in or sess_out:
        ccy = _safe(pricing_currency())

        # 参考单价 hover chip: rate table for the cost-contributing models.
        price_chip = ""
        if sess_in or sess_out:
            models_seen: dict[str, tuple[float, float]] = {}
            for k, mdl in (data.models or {}).items():
                if mdl and mdl not in models_seen:
                    models_seen[mdl] = model_price(mdl)
            if not models_seen:
                models_seen["default"] = model_price("")
            rows = "".join(
                f'<tr><td>{_safe(m)}</td>'
                f'<td class="pp-num">{pi}</td><td class="pp-num">{po}</td></tr>'
                for m, (pi, po) in models_seen.items()
            )
            price_chip = (
                f'<span class="price-tip"><span class="price-chip">参考单价 ⓘ</span>'
                f'<div class="price-pop">'
                f'<div style="color:#94a3b8;margin-bottom:4px">{ccy} / 百万 token</div>'
                f'<table><tr class="pp-head"><td>模型</td><td class="pp-num">输入</td>'
                f'<td class="pp-num">输出</td></tr>{rows}</table></div></span>'
            )

        # 当前模型配置：group config keys by the model they used; price chip rides along.
        if data.models:
            grouped: dict[str, list[str]] = {}
            for key, mdl in data.models.items():
                if mdl:
                    grouped.setdefault(mdl, []).append(key.replace("supervisor.", ""))
            parts = " · ".join(
                f'{_safe(m)}<span style="color:#94a3b8">（{_safe(", ".join(keys))}）</span>'
                for m, keys in grouped.items()
            )
            cost_note_html = (
                f'<div class="meta-strip"><span class="meta-strip-label">模型配置</span>'
                f'{parts}{price_chip}</div>'
            )
        elif price_chip:
            cost_note_html = f'<div class="meta-strip">{price_chip}</div>'

    result_html = ""
    if data.program_output or data.reply:
        status_cls, _, _ = _phase_meta(data)
        result_class = "result-card" if status_cls == "completed" else f"result-card result-card-{status_cls}"
        result_html = (
            f'<div class="{result_class}">'
            f'<div class="result-label">最终回复</div>'
            f'<div class="result-body">{_safe(data.reply or "（未生成）")}</div>'
            f'</div>'
        )

    knowledge_html = _render_knowledge_html(data.knowledge, data.knowledge_sections)
    sidebar_html = f'<nav class="sidebar">{knowledge_html}</nav>' if knowledge_html else ""

    return HTML_TEMPLATE.format(
        title=_safe(data.title),
        platform_badge=_render_platform_badge(data.platform),
        stats=stats_str,
        provenance_html=_render_provenance(data.raw_input, data.goal, data.router),
        webarena_html=_render_webarena_result(data.webarena),
        mobileworld_html=_render_mobileworld_result(data.mobileworld),
        program_html=program_html,
        presentation_html=presentation_html,
        phase_badge=_render_phase_badge(data),
        sidebar_html=sidebar_html,
        cost_note_html=cost_note_html,
        result_html=result_html,
        pages_html=pages_html,
    )
