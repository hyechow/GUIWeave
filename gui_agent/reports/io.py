"""Persistence helpers for report HTML files."""

from __future__ import annotations

from pathlib import Path

from .models import AppReconData, ReportData
from .recon_html import generate_recon_html
from .runner_html import generate_html

def save_report(data: ReportData, output_path: Path, grid: bool = False) -> Path:
    html = generate_html(data, grid=grid)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def save_recon_report(data: AppReconData, output_path: Path) -> Path:
    html = generate_recon_html(data)
    output_path.write_text(html, encoding="utf-8")
    return output_path
