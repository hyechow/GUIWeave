"""Live terminal rendering for coding-orchestrator artifacts."""

from __future__ import annotations

import difflib
from collections.abc import Callable

from .models import CodingEvent


class CodingTerminalRenderer:
    def __init__(
        self,
        *,
        write: Callable[[str], None] = print,
        prefix: str = "[coding]",
    ) -> None:
        self.write = write
        self.prefix = prefix

    def _section(self, title: str) -> None:
        self.write(f"{self.prefix} {title} " + "─" * max(4, 46 - len(title)))

    def _source(self, source: str) -> None:
        for number, line in enumerate(source.splitlines(), 1):
            self.write(f"{number:>4} │ {line}")

    def _diff(
        self,
        before: str,
        after: str,
        *,
        tofile: str,
    ) -> bool:
        rendered = list(difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="candidate.py",
            tofile=tofile,
            lineterm="",
        ))
        for line in rendered:
            self.write(f"  {line}")
        return bool(rendered)

    @staticmethod
    def _review_candidate(source: str, edits: list[dict[str, str]]) -> str | None:
        candidate = source
        for edit in edits:
            search = str(edit.get("search") or "")
            replacement = str(edit.get("replacement") or "")
            if not search or candidate.count(search) != 1:
                return None
            candidate = candidate.replace(search, replacement, 1)
        return candidate

    def __call__(self, event: CodingEvent) -> None:
        data = event.data
        if event.kind == "generation_started":
            self._section("Generate")
            self.write("  … generating candidate.py")
        elif event.kind == "generation_completed":
            self.write(f"  ✓ generated in {data['seconds']:.2f}s")
            self._source(str(data["source"]))
        elif event.kind == "diagnostics":
            self._section(f"Static Review · {data['phase']}")
            diagnostics = data.get("diagnostics") or []
            if diagnostics:
                for diagnostic in diagnostics:
                    self.write(f"  ✗ {diagnostic}")
            else:
                self.write("  ✓ no static diagnostics")
        elif event.kind == "probe":
            self._section(f"Execution Probe · {data['phase']}")
            status = data.get("status")
            if status == "skipped":
                self.write("  – skipped because static diagnostics remain")
            elif status == "passed":
                operations = ", ".join(data.get("operations") or []) or "local Python"
                self.write(f"  ✓ passed · {operations}")
            else:
                error = str(data.get("error") or "").strip().splitlines()
                self.write(f"  ✗ {error[-1] if error else 'execution failed'}")
        elif event.kind == "review_started":
            self._section(f"Reviewer · pass {data['pass_index']}")
            self.write("  … reviewing candidate and evidence")
        elif event.kind == "review_completed":
            edits = data.get("edits") or []
            if data.get("approved"):
                self.write(f"  ✓ Approved · {data['seconds']:.2f}s")
            elif data.get("error"):
                self.write(f"  ✗ Invalid review · {data['error']}")
                self.write(f"  raw: {data.get('text') or ''}")
            else:
                self.write(
                    f"  △ Changes requested · {len(edits)} edit(s)"
                    f" · {data['seconds']:.2f}s"
                )
                source = str(data.get("source") or "")
                proposed = self._review_candidate(source, edits)
                if proposed is not None and self._diff(
                    source,
                    proposed,
                    tofile="candidate.py (review)",
                ):
                    return
                for index, edit in enumerate(edits, 1):
                    self.write(f"  Edit {index}")
                    if not self._diff(
                        str(edit.get("search") or ""),
                        str(edit.get("replacement") or ""),
                        tofile=f"candidate.py (review edit {index})",
                    ):
                        self.write("  – no source change")
        elif event.kind == "repair_completed":
            self._section("Apply Repair")
            display_after = (
                data.get("after")
                if data.get("status") in {"accepted", "partial"}
                else data.get("proposed")
            )
            rendered = self._diff(
                str(data.get("before") or ""),
                str(display_after or ""),
                tofile=(
                    "candidate.py (applied)"
                    if data.get("status") in {"accepted", "partial"}
                    else "candidate.py (proposed)"
                ),
            )
            if not rendered:
                self.write("  – no applicable source change")
            marker = {
                "accepted": "✓",
                "partial": "△",
                "rejected": "✗",
            }.get(str(data.get("status")), "✗")
            self.write(f"  {marker} {data.get('status')}")
            if data.get("error"):
                self.write(f"  {data['error']}")
            for diagnostic in data.get("candidate_diagnostics") or []:
                self.write(f"  ✗ {diagnostic}")
            candidate_error = str(data.get("candidate_error") or "").strip().splitlines()
            if candidate_error:
                self.write(f"  ✗ {candidate_error[-1]}")
        elif event.kind == "finalized":
            self._section("Final")
            repair_status = data.get("repair_status")
            suffix = f" · repair {repair_status}" if repair_status != "none" else ""
            self.write(
                f"  {'✓' if data.get('status') == 'passed' else '✗'} "
                f"{data.get('status')}{suffix}"
            )
