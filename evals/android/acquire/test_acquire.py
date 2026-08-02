"""LLM eval for the minimal shared cells -> records Acquire boundary."""

from __future__ import annotations

import json
import sys
from hashlib import sha1
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from gui_agent.core.run.structured_collection import CellStream, materialize_cell_records


CASES = Path(__file__).with_name("cases.json")
MASTODON_REPLAY = Path(__file__).with_name("mastodon_20260801.json")


def _cell(raw: dict) -> dict:
    text = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    return {
        "structural_key": str(raw["type"]),
        "content_key": sha1(text.encode()).hexdigest(),
        "texts": list(raw.get("texts") or []),
        "controls": [
            {"label": label}
            for label in raw.get("controls") or []
        ],
    }


def _collect(case: dict) -> list[dict]:
    stream = CellStream()
    previous: list[str] | None = None
    for raw_frame in case["frames"]:
        frame = [_cell(raw) for raw in raw_frame]
        signature = [cell["content_key"] for cell in frame]
        if signature != previous:
            stream.add(frame)
            previous = signature
    return materialize_cell_records(
        stream.cells, case["fields"], goal=case.get("goal", ""),
    )


def _replay_frames(frames: list[dict], fields: list[str], goal: str) -> list[dict]:
    """Apply the adapter's two-no-progress-confirmations move contract."""
    stream = CellStream()
    index = 0
    stream.add(frames[index]["cells"])
    while index + 1 < len(frames):
        before = frames[index]["content_key"]
        unchanged = 0
        while index + 1 < len(frames):
            index += 1
            if frames[index]["content_key"] != before:
                stream.add(frames[index]["cells"])
                break
            unchanged += 1
            if unchanged == 2:
                index = len(frames)
                break
    return materialize_cell_records(stream.cells, fields, goal=goal)


def _check(label: str, actual, expected) -> bool:
    try:
        value = actual()
        ok = value == expected
        detail = "" if ok else f"expected={expected!r} actual={value!r}"
    except Exception as exc:  # noqa: BLE001 - eval reports individual failures
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        print(f"         {detail}")
    return ok


def main() -> int:
    failed = 0
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    for case in cases:
        failed += not _check(
            case["label"], lambda case=case: _collect(case), case["expected"]["rows"],
        )

    replay = json.loads(MASTODON_REPLAY.read_text(encoding="utf-8"))
    for label, stride in (
        ("Mastodon 20260801 multi-window replay", 1),
        ("Mastodon stride-2 replay", 2),
    ):
        failed += not _check(
            label,
            lambda stride=stride: {
                name: _replay_frames(
                    frames[::stride],
                    replay["fields"],
                    f"Materialize post records from {name!r}",
                )
                for name, frames in replay["collections"].items()
            },
            replay["expected"],
        )

    total = len(cases) + 2
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
