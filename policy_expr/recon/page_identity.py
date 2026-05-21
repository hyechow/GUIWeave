"""Page identity: determine whether a page is new or already known.

Cascade logic:
  1. Visual embedding (GUIClip, fast, no LLM).
     - If max_visual >= visual_shortcut → known page (save LLM call).
  2. Semantic fingerprint (form + content + purpose).
     - Form exact match AND content exact match AND purpose embed >= 0.90 → known page.
     - Otherwise → new page, add to library.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from policy_expr.recon.cascade_matcher import CascadeMatcher, PageEmbedding, PageFingerprint


@dataclass
class IdentityResult:
    is_duplicate: bool
    reason: str
    best_visual_sim: float = 0.0
    best_text_sim: float | None = None
    matched_index: int | None = None
    matched_name: str | None = None
    library_size: int = 0
    phase: str = ""  # "visual_shortcut" / "semantic_match" / "new_page"


class PageIdentity:
    """Semantic-primary page identity for BFS/DFS exploration."""

    def __init__(
        self,
        matcher: CascadeMatcher,
        visual_shortcut: float = 0.95,
    ):
        self._matcher = matcher
        self._vs = visual_shortcut
        self._library: list[tuple[PageEmbedding, bytes, str, PageFingerprint | None]] = []

    def __len__(self) -> int:
        return len(self._library)

    def check(self, png: bytes, precomputed: PageEmbedding | None = None) -> IdentityResult:
        """Check if png is a known page in the library."""
        n = len(self._library)
        if n == 0:
            return IdentityResult(False, "empty_library", library_size=0, phase="new_page")

        # Phase 1: visual (fast, no LLM)
        candidate = precomputed or self._matcher.embed_visual(png)
        vis_sims = [self._matcher.visual_sim(candidate, e) for e, _, _, _ in self._library]
        best_idx = max(range(n), key=lambda i: vis_sims[i])
        max_vis = vis_sims[best_idx]

        if max_vis >= self._vs:
            return IdentityResult(
                True, f"visual_shortcut({max_vis:.3f})",
                max_vis, matched_index=best_idx,
                matched_name=self._library[best_idx][2],
                library_size=n, phase="visual_shortcut",
            )

        # Phase 2: semantic fingerprint (form + content + purpose)
        fp_candidate = self._matcher.generate_fingerprint(png)

        best_sem_idx = -1
        best_sem_reason = ""
        for i in range(n):
            emb, epng, name, fp = self._library[i]
            if fp is None:
                fp = self._matcher.generate_fingerprint(epng)
                self._library[i] = (emb, epng, name, fp)
            same, reason = self._matcher.semantic_sim(fp_candidate, fp)
            if same:
                best_sem_idx = i
                best_sem_reason = reason
                break

        if best_sem_idx >= 0:
            return IdentityResult(
                True, best_sem_reason,
                max_vis, matched_index=best_sem_idx,
                matched_name=self._library[best_sem_idx][2],
                library_size=n, phase="semantic_match",
            )

        return IdentityResult(
            False, f"no_match(vis={max_vis:.3f})",
            max_vis, library_size=n, phase="new_page",
        )

    def add(self, png: bytes, name: str = "", emb: PageEmbedding | None = None) -> PageEmbedding:
        """Add page to library and return its embedding."""
        if emb is None:
            emb = self._matcher.embed_visual(png)
        # Generate fingerprint lazily on first check, store None for now
        self._library.append((emb, png, name, None))
        return emb

    def check_and_add(self, png: bytes, name: str = "") -> IdentityResult:
        """Check for identity; add to library only if not a known page."""
        result = self.check(png)
        if not result.is_duplicate:
            self.add(png, name)
        return result

    def to_json(self) -> list[dict]:
        """Export library entries as JSON-serializable list."""
        return [
            {"index": i, "name": name}
            for i, (_, _, name, _) in enumerate(self._library)
        ]
