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

_MAX_SELECTED = 3  # cap injected bodies per turn

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split optional YAML frontmatter off a section's markdown: ``(meta, body)``.

    Sections carry a ``when:`` line (何时需要查阅本节) that goes into the selector
    manifest; the body fed to the planner must NOT include the raw frontmatter block.
    Files without frontmatter return ``({}, text)`` unchanged."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, text[m.end():]


def _norm(s: str) -> str:
    """Loose key: drop spaces / underscores / punctuation, lowercase — so the checker's
    paraphrase ("如何访问RoboTeam") still matches the file stem ("如何访问Robo_Team")."""
    return re.sub(r"[\W_]+", "", s, flags=re.UNICODE).lower()


class ProgressiveKnowledge:
    """Holds per-section bodies; exposes a cheap manifest + on-demand body selection."""

    def __init__(self, sections: dict[str, str]):
        # Raw file text in; frontmatter parsed off here so callers stay plumbing-free:
        # `when:` (何时查阅) feeds the selector manifest, the stripped body feeds the planner.
        self.sections: dict[str, str] = {}  # stem -> body markdown (frontmatter stripped)
        self.whens: dict[str, str] = {}     # stem -> when-to-consult one-liner ("" if absent)
        for stem, text in sections.items():
            meta, body = split_frontmatter(text)
            self.sections[stem] = body
            self.whens[stem] = meta.get("when", "")
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

    def bodies(self, stems: list[str]) -> str:
        """Concatenate the bodies of the given section stems (as returned by :meth:`pick`)."""
        return "\n\n".join(
            f"## {k.replace('_', ' ')}\n{self.sections[k]}" for k in stems if k in self.sections
        )

    def select(self, names: list[str] | None, page_identity: str = "") -> str:
        """Resolve checker-named sections (+ page_identity fallback) to concatenated bodies.

        Returns ``""`` when nothing matches — the planner then runs on the navigation overview
        alone (still describes the page), rather than re-injecting the whole elements blob.
        """
        return self.bodies(self.pick(names, page_identity))
