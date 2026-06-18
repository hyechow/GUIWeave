"""Progressive (skill-like) knowledge: a manifest is always cheap to show, section bodies
load on demand.

A large manual reduces to one big ``_elements.md`` (e.g. 17K chars for 89 sections). Injecting
it whole into the planner EVERY turn is wasteful and dilutes attention — only the section
matching the current screen is relevant. So, like an Agent Skill (name + description always in
context, SKILL.md body read only when invoked), we keep:

  - a **manifest** (the list of section names) — small, shown to the per-turn checker;
  - the **section bodies** (the per-section page-knowledge .md files) — loaded only for the
    section(s) the checker flags as relevant to the current screen.

A dedicated KnowledgeSelector micro-decision (helpers.run_selector) reads the id'd manifest
and picks ``section_ids``; the planner then injects only those bodies. The policy caches the
selection per (milestone, page_identity), so the selector LLM only fires on page/milestone
changes. Leaf module: only ``re``.
"""

from __future__ import annotations

import re
from typing import Any

from gui_agent.context import ContextBlock, render_context_blocks

_MAX_SELECTED = 3  # cap injected bodies per turn

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split optional YAML frontmatter off a section's markdown: ``(meta, body)``.

    Sections may carry a legacy ``when:`` line or the richer ``selector_when:``
    line that goes into the selector manifest. The body fed to the planner must
    NOT include the raw frontmatter block. Files without frontmatter return
    ``({}, text)`` unchanged. The parser intentionally supports only scalars and
    simple dash lists, matching the knowledge metadata we author by hand."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, Any] = {}
    current_list_key: str | None = None
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if current_list_key and stripped.startswith("- "):
            meta.setdefault(current_list_key, []).append(_parse_scalar(stripped[2:].strip()))
            continue
        current_list_key = None
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not value:
            meta[key] = []
            current_list_key = key
        else:
            meta[key] = _parse_scalar(value)
    return meta, text[m.end():]


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"true", "false"}:
        return value == "true"
    if value.isdigit():
        return int(value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _norm(s: str) -> str:
    """Loose key: drop spaces / underscores / punctuation, lowercase — so the checker's
    paraphrase ("如何访问RoboTeam") still matches the file stem ("如何访问Robo_Team")."""
    return re.sub(r"[\W_]+", "", s, flags=re.UNICODE).lower()


def _tokens(s: str) -> set[str]:
    """Coarse tokens for fuzzy when-line overlap: punctuation-split words (len≥2) plus CJK
    bigrams, lowercased. `_norm` collapses boundaries so substring matching misses synonym
    rewrites (「创建启用虚拟机器人时」vs「新建机器人并设为启用」); token/bigram overlap bridges
    them. Coarse on purpose — this only feeds the deterministic knowledge FALLBACK."""
    s = (s or "").lower()
    words = {w for w in re.split(r"[\W_]+", s, flags=re.UNICODE) if len(w) >= 2 and not re.search(r"[一-鿿]", w)}
    cjk = "".join(re.findall(r"[一-鿿]+", s))
    bigrams = {cjk[i : i + 2] for i in range(len(cjk) - 1)}
    return words | bigrams


class ProgressiveKnowledge:
    """Holds per-section bodies; exposes a cheap manifest + on-demand body selection."""

    def __init__(self, sections: dict[str, str]):
        # Raw file text in; frontmatter parsed off here so callers stay plumbing-free:
        # selector_when/when feeds the selector manifest, the stripped body feeds the planner.
        self.sections: dict[str, str] = {}  # stem -> body markdown (frontmatter stripped)
        self.whens: dict[str, str] = {}     # stem -> when-to-consult one-liner ("" if absent)
        self.metadata: dict[str, dict[str, Any]] = {}  # stem -> parsed frontmatter metadata
        for stem, text in sections.items():
            meta, body = split_frontmatter(text)
            self.sections[stem] = body
            self.metadata[stem] = meta
            self.whens[stem] = str(meta.get("selector_when") or meta.get("when") or "")
        self._index = {_norm(k): k for k in self.sections}
        # Short ids (s01..sNN) for the KnowledgeSelector: the LLM returns ids, not section
        # names, so resolution is an exact table lookup — no paraphrase fuzzy-match misses
        # (查看↔查询). Ids only need to be stable within one manifest→response cycle, so
        # enumeration order at load time suffices; the .md files carry no id.
        self._ids = {f"s{i:02d}": stem for i, stem in enumerate(self.sections, 1)}

    def __bool__(self) -> bool:
        return bool(self.sections)

    def selector_manifest(self) -> str:
        """One line per section — ``[s12] title — when`` — for the KnowledgeSelector prompt.

        The ``when`` one-liner (from the section's frontmatter) is what lets the selector
        bridge synonym gaps a bare title loses on literal matching: 「如何使用机器人模拟器」
        never beats 「如何添加机器人」 for a 新建虚拟机器人 task until its when-line says
        创建/启用虚拟机器人时. Sections without frontmatter degrade to title-only lines."""
        lines = []
        for sid, stem in self._ids.items():
            title = stem.replace("_", " ")
            when = self.whens.get(stem, "")
            lines.append(f"[{sid}] {title} — {when}" if when else f"[{sid}] {title}")
        return "\n".join(lines)

    def by_ids(self, ids: list[str] | None) -> list[str]:
        """Resolve selector-returned ids to section stems: exact lookup, deduped, capped.
        Tolerates echo variants ('S01', '[s01]'); unknown ids are dropped silently."""
        out: list[str] = []
        seen: set[str] = set()
        for sid in ids or []:
            stem = self._ids.get(str(sid).strip().strip("[]").lower())
            if stem and stem not in seen:
                seen.add(stem)
                out.append(stem)
            if len(out) >= _MAX_SELECTED:
                break
        return out

    def _match(self, name: str) -> str | None:
        n = _norm(name)
        if not n:
            return None
        if n in self._index:
            return self._index[n]
        for nk, orig in self._index.items():  # 双向子串模糊匹配
            if n in nk or nk in n:
                return orig
        return None

    def pick(self, names: list[str] | None, page_identity: str = "") -> list[str]:
        """Resolve checker-named sections (+ page_identity fallback) to matched section stems
        (capped at ``_MAX_SELECTED``). Split out from :meth:`select` so the runtime can LOG the
        section names that were actually injected this turn (context.json turns[].sections_loaded)."""
        picked: list[str] = []
        seen: set[str] = set()
        for nm in list(names or []) + ([page_identity] if page_identity else []):
            key = self._match(nm)
            if key and key not in seen:
                seen.add(key)
                picked.append(key)
            if len(picked) >= _MAX_SELECTED:
                break
        return picked

    def match_signals(self, signals: list[str]) -> list[str]:
        """Deterministic section pick from free-text signals (page identity, milestone name,
        success_condition) — the fallback used when the LLM selector returns nothing. Keeps
        knowledge injection alive when page identity (the selector's main key) is weak.

        Two passes, both capped at ``_MAX_SELECTED``: (1) bidirectional substring match of each
        signal against section titles (reuses :meth:`_match`, the strongest signal); (2) token /
        CJK-bigram overlap of the combined signals against each section's selector_when line,
        ranked by overlap size — this bridges the synonym gaps a bare title misses."""
        raw = [s for s in signals if s and s.strip()]
        if not raw:
            return []
        picked: list[str] = []
        seen: set[str] = set()
        for nm in raw:
            key = self._match(nm)
            if key and key not in seen:
                seen.add(key)
                picked.append(key)
            if len(picked) >= _MAX_SELECTED:
                return picked
        sig_tokens = _tokens(" ".join(raw))
        if sig_tokens:
            scored: list[tuple[int, str]] = []
            for stem, when in self.whens.items():
                if stem in seen or not when:
                    continue
                overlap = len(sig_tokens & _tokens(when))
                if overlap:
                    scored.append((overlap, stem))
            for _, stem in sorted(scored, key=lambda x: (-x[0], x[1])):
                seen.add(stem)
                picked.append(stem)
                if len(picked) >= _MAX_SELECTED:
                    break
        return picked

    def bodies(self, stems: list[str]) -> str:
        """Concatenate the bodies of the given section stems (as returned by :meth:`pick`)."""
        return render_context_blocks(self.body_blocks(stems), include_headers=True)

    def body_blocks(self, stems: list[str]) -> list[ContextBlock]:
        """Return selected section bodies as source-tagged context blocks."""
        blocks: list[ContextBlock] = []
        for stem in stems:
            if stem not in self.sections:
                continue
            meta = self.metadata.get(stem, {})
            metadata = {
                str(k): v for k, v in meta.items()
                if k not in {"id", "source_type", "source", "ttl"} and v not in ("", None)
            }
            if metadata.get("selector_when") == metadata.get("when"):
                metadata.pop("when", None)
            content = f"## {stem.replace('_', ' ')}\n{self.sections[stem]}"
            blocks.append(ContextBlock(
                id=str(meta.get("id") or f"knowledge.section.{_norm(stem)}"),
                budget="high",  # the selector/fallback picked this — relevant, keep over history
                source_type=str(meta.get("source_type") or "knowledge_section"),
                source=str(meta.get("source") or "knowledge_base"),
                ttl=str(meta.get("ttl") or "session"),
                priority=50,
                metadata=metadata,
                content=content,
            ))
        return blocks

    def select(self, names: list[str] | None, page_identity: str = "") -> str:
        """Resolve checker-named sections (+ page_identity fallback) to concatenated bodies.

        Returns ``""`` when nothing matches — the planner then runs on the navigation overview
        alone (still describes the page), rather than re-injecting the whole elements blob.
        """
        return self.bodies(self.pick(names, page_identity))
