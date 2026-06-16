"""Page identity: determine whether a page is new or already known.

Cascade logic:
  1. Visual embedding (GUIClip, fast, no LLM).
     - If max_visual >= visual_shortcut → known page (save LLM call).
  2. Semantic fingerprint (form + content + purpose).
     - Form exact match AND content exact match AND purpose embed >= 0.90 → known page.
     - Otherwise → new page, add to library.
"""

from __future__ import annotations

from dataclasses import dataclass

from gui_agent.adapters.iphone.recon.cascade_matcher import CascadeMatcher, PageEmbedding, PageFingerprint

# Form groups: pages (A/B) are full standalone screens; overlays (C/D/E/F/G) sit on top.
# Visual shortcut is filtered by group so an overlay never short-circuits against a page.
_OVERLAY_FORMS = frozenset("CDEFG")


def _form_to_group(form: str | None) -> str | None:
    """Map form letter to "page" / "overlay", or None if unknown."""
    if form is None:
        return None
    return "overlay" if form in _OVERLAY_FORMS else "page"


def _infer_form_group(png: bytes) -> str | None:
    """Pixel-only form group guess (no LLM). Returns "overlay", "page", or None."""
    import io
    import numpy as np
    from PIL import Image as _PIL
    from gui_agent.adapters.iphone.overlay_detect import detect_fullscreen_popup

    img = np.array(_PIL.open(io.BytesIO(png)).convert("RGB"), dtype=np.float32)
    if detect_fullscreen_popup(img):
        return "overlay"
    # Bottom-panel heuristic: bottom 35% significantly brighter than top 35%
    h = img.shape[0]
    top_mean = float(img[:int(h * 0.35)].mean())
    bot_mean = float(img[int(h * 0.65):].mean())
    if bot_mean > top_mean * 1.25 and bot_mean > 160:
        return "overlay"
    return None  # unknown — don't filter


# IdentityResult moved to gui_agent.core.schemas.recon (S3); re-exported here for back-compat.
from gui_agent.core.schemas.recon import IdentityResult


# Library entry: (embedding, png_bytes, name, fingerprint | None, form_group | None)
_LibEntry = tuple[PageEmbedding, bytes, str, PageFingerprint | None, str | None]


class PageIdentity:
    """Semantic-primary page identity for BFS/DFS exploration."""

    def __init__(
        self,
        matcher: CascadeMatcher,
        visual_shortcut: float = 0.95,
    ):
        self._matcher = matcher
        self._vs = visual_shortcut
        self._library: list[_LibEntry] = []

    def __len__(self) -> int:
        return len(self._library)

    def check(self, png: bytes, precomputed: PageEmbedding | None = None) -> IdentityResult:
        """Check if png is a known page in the library."""
        n = len(self._library)
        if n == 0:
            return IdentityResult(False, "empty_library", library_size=0, phase="new_page")

        candidate_group = _infer_form_group(png)

        # Phase 1: visual shortcut (fast, no LLM) — filtered by form group
        candidate = precomputed or self._matcher.embed_visual(png)
        # Only compare against same-group entries; unknown group entries always included
        vis_candidates = [
            i for i, (_, _, _, _, g) in enumerate(self._library)
            if candidate_group is None or g is None or g == candidate_group
        ]
        if vis_candidates:
            vis_sims = {i: self._matcher.visual_sim(candidate, self._library[i][0])
                        for i in vis_candidates}
            best_idx = max(vis_candidates, key=lambda i: vis_sims[i])
            max_vis = vis_sims[best_idx]
        else:
            max_vis = 0.0
            best_idx = 0

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
            emb, epng, name, fp, grp = self._library[i]
            if fp is None:
                fp = self._matcher.generate_fingerprint(epng)
                self._library[i] = (emb, epng, name, fp, grp)
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

    def add(self, png: bytes, name: str = "", emb: PageEmbedding | None = None,
            form: str | None = None) -> PageEmbedding:
        """Add page to library. form letter (A-G) is stored as form_group for match filtering."""
        if emb is None:
            emb = self._matcher.embed_visual(png)
        group = _form_to_group(form) if form else _infer_form_group(png)
        self._library.append((emb, png, name, None, group))
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
            {"index": i, "name": name, "form_group": grp}
            for i, (_, _, name, _, grp) in enumerate(self._library)
        ]
