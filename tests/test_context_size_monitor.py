"""Context-size monitoring in the report: each module's input tokens shown as a share of the
model context window (CONTEXT_WINDOW), colored by pressure. This is the knowledge-inflation
monitor — a big prompt (e.g. the full elements blob in decompose) shows a high %.
"""

from __future__ import annotations

from gui_agent.reports.metrics import CONTEXT_WINDOW, _ctx_color


def test_ctx_color_thresholds():
    assert _ctx_color(int(CONTEXT_WINDOW * 0.10)) == "#64748b"  # calm
    assert _ctx_color(int(CONTEXT_WINDOW * 0.80)) == "#f59e0b"  # ≥75% amber
    assert _ctx_color(int(CONTEXT_WINDOW * 0.95)) == "#dc2626"  # ≥90% red
