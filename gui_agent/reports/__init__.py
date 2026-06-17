"""HTML report generation for GUI agent runs and recon logs."""

from .builder import (
    ReconReportBuilder,
    RunnerReportBuilder,
)
from .io import save_recon_report, save_report
from .models import AppReconData

__all__ = [
    "AppReconData",
    "ReconReportBuilder",
    "RunnerReportBuilder",
    "save_recon_report",
    "save_report",
]
