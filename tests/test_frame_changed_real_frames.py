"""Real-frame regression for frame_changed (action-effect detection), driven by
tests/fixtures/frame_analysis/cases.json + captured device frames.

The before/after PNGs are gitignored (tests/**/*.png) — local fixtures, like the eval
screenshots — so each case SKIPS when its frames aren't present (fresh clone / CI). Where
present it asserts frame_changed reads the true before/after relationship: the
20260611_085000 闹钟-tab switch (whole-frame grayscale mean diff < 8 yet a real page change)
-> changed=True, and identical frames -> changed=False.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gui_agent.core.frame_analysis import frame_changed

_DIR = Path(__file__).parent / "fixtures" / "frame_analysis"
_CASES_FILE = _DIR / "cases.json"
_CASES = json.loads(_CASES_FILE.read_text(encoding="utf-8")) if _CASES_FILE.exists() else []


@pytest.mark.parametrize("case", _CASES, ids=[c["label"] for c in _CASES])
def test_frame_changed_on_real_frames(case):
    before, after = _DIR / case["before"], _DIR / case["after"]
    if not (before.exists() and after.exists()):
        pytest.skip(f"fixture frames not present (gitignored): {case['before']} / {case['after']}")
    assert frame_changed(before.read_bytes(), after.read_bytes()) is case["expected_changed"]
