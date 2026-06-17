"""Context-size monitoring in the report: each module's input tokens shown as a share of the
model context window (CONTEXT_WINDOW), colored by pressure. This is the knowledge-inflation
monitor — a big prompt (e.g. the full elements blob in decompose) shows a high %.
"""

from __future__ import annotations

from gui_agent.reports.metrics import CONTEXT_WINDOW, _ctx_color
from gui_agent.reports.runner_html import _ctx_pct_tag


def test_ctx_color_thresholds():
    assert _ctx_color(int(CONTEXT_WINDOW * 0.10)) == "#64748b"  # calm
    assert _ctx_color(int(CONTEXT_WINDOW * 0.80)) == "#f59e0b"  # ≥75% amber
    assert _ctx_color(int(CONTEXT_WINDOW * 0.95)) == "#dc2626"  # ≥90% red


def test_ctx_pct_tag_inline_percent():
    assert _ctx_pct_tag(0) == ""
    assert _ctx_pct_tag(int(CONTEXT_WINDOW * 0.001)) == ""   # rounds to 0% → suppressed
    tag = _ctx_pct_tag(int(CONTEXT_WINDOW * 0.5))
    assert "50%" in tag and "[" not in tag and "width" not in tag  # small inline %, no brackets, no bar
