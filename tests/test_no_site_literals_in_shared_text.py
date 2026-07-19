"""Guard: model-visible SHARED text must stay site-neutral (no Magento/WebArena literals).

The orchestrator/decomposer + statement prompts, the runtime ``ContextBlock`` content, and the
core schema field descriptions are SHARED across every platform and site. Site-specific facts —
Magento admin-grid column names (Salable Quantity, Grand Total, Customer Email, Store View),
configurable-product mechanics, WebArena task vocabulary (WS08, …) — belong in app knowledge
(``knowledge/browser/<site>/``) and the WebArena-only namespace (``tasks/webarena/*``, the browser
adapter's ``webarena.py``), NOT on this shared surface. Leaking them here turns one site's facts
into a global planning prior that biases every other site.

This is the deterministic backstop for the "what (knowledge) vs how (shared prompt)" boundary: it
fails if a banned site literal reappears in the shared model-visible surface. When it fails, move
the fact into app knowledge and genericize the shared text (use neutral placeholders like
``<字段>`` / ``attribute`` / ``detail_url`` / ``parent entity``).

Scope note: this guards the *model-visible* shared surface only. Browser-adapter code that
deliberately implements a Magento/admin-grid capability (``adapters/browser/filter_state.py``,
``table_reader.py``, ``executor.py`` datepicker, …) is intentionally NOT scanned — that is where
site-specific behavior is *allowed* to live, behind the adapter boundary.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

# Case-SENSITIVE on purpose: bans the Title-Case site-attribute usage (a leaked proper noun),
# while leaving generic lowercase prose alone — e.g. an example constraint like ``quantity=0`` or
# the Chinese 数量/库存 stays legal; only the Magento column ``Quantity``/``Material`` is banned.
_BANNED = (
    "Magento",
    "WebArena",
    "WS08",
    "Material",
    "Quantity",
    "Salable",
    "Grand Total",
    "Customer Email",
    "Store View",
    "Configurable Product",
    "Bill-to",
    "Action_url",
    "Acme",
    "-SIZE-COLOR",
    "Type=Configurable",
)


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    # 1. runtime ContextBlock content + any other model-visible context builders
    files += sorted((_ROOT / "gui_agent" / "context").rglob("*.py"))
    # 2. core schema field descriptions (can reach the LLM tool schema / reports)
    files.append(_ROOT / "gui_agent" / "core" / "schemas" / "__init__.py")
    # 3. structured-output schema descriptions used by the orchestrator LLM
    files.append(_ROOT / "gui_agent" / "core" / "orchestrator" / "decomposer.py")
    # 4. shared statement/orchestrator/router prompts — the WebArena namespace is allowlisted
    for md in sorted((_ROOT / "gui_agent" / "prompts").rglob("*.md")):
        if "webarena" in md.parts:
            continue
        files.append(md)
    return [f for f in files if f.is_file()]


def test_shared_model_visible_text_is_site_neutral():
    violations: list[str] = []
    for f in _scanned_files():
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            for word in _BANNED:
                if word in line:
                    rel = f.relative_to(_ROOT)
                    violations.append(f"{rel}:{lineno}: banned site literal {word!r} -> {line.strip()[:100]}")
    assert not violations, (
        "Site-specific literal leaked into shared model-visible text. Move the fact into "
        "knowledge/browser/<site>/ and genericize the shared surface with neutral placeholders.\n"
        "Allowed homes for site facts: tasks/webarena/*, adapters/browser/ (behind the adapter "
        "boundary), knowledge/, and explicit tests.\n  " + "\n  ".join(violations)
    )
