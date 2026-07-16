#!/usr/bin/env python
"""Backward-compat shim — the replay harness now lives at ``replay/run.py``.

Existing invocations (``uv run python scripts/replay_supervisor_turn.py ...``) keep working;
prefer ``uv run python -m replay ...`` going forward.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from replay.run import main  # noqa: E402

raise SystemExit(main())
