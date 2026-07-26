"""Compact live terminal rendering for coding-orchestrator phases."""

from __future__ import annotations

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

    def __call__(self, event: CodingEvent) -> None:
        data = event.data
        if event.kind == "generation_started":
            self._section(f"Generate · {data.get('phase') or 'initial'}")
            self.write("  … generating candidate.py")
        elif event.kind == "generation_completed":
            self.write(f"  ✓ generated in {data['seconds']:.2f}s")
            self._source(str(data["source"]))
        elif event.kind == "diagnostics":
            self._section(f"Static Review · {data['phase']}")
            diagnostics = data.get("diagnostics") or []
            for diagnostic in diagnostics:
                self.write(f"  ✗ {diagnostic}")
            if not diagnostics:
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
            if data.get("approved"):
                self.write(f"  ✓ Approved · {data['seconds']:.2f}s")
            elif data.get("error"):
                error = str(data["error"])
                if error.startswith(("[REVIEW_IO]", "[REVIEW_PROTOCOL]")):
                    self.write(
                        f"  △ Unavailable · deterministic validation retained · {error}"
                    )
                else:
                    self.write(f"  ✗ Invalid review · {error}")
            else:
                issues = data.get("issues") or []
                self.write(
                    f"  △ Rejected · {len(issues)} issue(s)"
                    f" · {data['seconds']:.2f}s"
                )
                for issue in issues:
                    self.write(f"  ✗ {issue}")
        elif event.kind == "finalized":
            self._section("Final")
            regeneration = str(data.get("repair_status") or "not_needed")
            suffix = (
                f" · regeneration {regeneration}"
                if regeneration != "not_needed"
                else ""
            )
            self.write(
                f"  {'✓' if data.get('status') == 'passed' else '✗'} "
                f"{data.get('status')}{suffix}"
            )
