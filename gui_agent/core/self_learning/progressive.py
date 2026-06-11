"""Progressive (skill-like) knowledge: a manifest is always cheap to show, section bodies
load on demand.

A large manual reduces to one big ``_elements.md`` (e.g. 17K chars for 89 sections). Injecting
it whole into the planner EVERY turn is wasteful and dilutes attention — only the section
matching the current screen is relevant. So, like an Agent Skill (name + description always in
context, SKILL.md body read only when invoked), we keep:

  - a **manifest** (the list of section names) — small, shown to the per-turn checker;
  - the **section bodies** (the per-section page-knowledge .md files) — loaded only for the
    section(s) the checker flags as relevant to the current screen.

The checker (which already reads the screen each turn) picks ``relevant_sections``; the same
turn's planner then injects only those bodies via :meth:`ProgressiveKnowledge.select`. Zero
extra LLM calls. Leaf module: only ``re``.
"""

from __future__ import annotations

import re

_MAX_SELECTED = 3  # cap injected bodies per turn


def _norm(s: str) -> str:
    """Loose key: drop spaces / underscores / punctuation, lowercase — so the checker's
    paraphrase ("如何访问RoboTeam") still matches the file stem ("如何访问Robo_Team")."""
    return re.sub(r"[\W_]+", "", s, flags=re.UNICODE).lower()


class ProgressiveKnowledge:
    """Holds per-section bodies; exposes a cheap manifest + on-demand body selection."""

    def __init__(self, sections: dict[str, str]):
        self.sections = sections  # stem -> body markdown
        self._index = {_norm(k): k for k in sections}

    def __bool__(self) -> bool:
        return bool(self.sections)

    def manifest_text(self) -> str:
        names = "\n".join(f"- {k.replace('_', ' ')}" for k in self.sections)
        return (
            "## 可用页面知识章节\n"
            "下面是本应用各功能页面的知识章节清单(仅名称)。请判断**当前屏幕/下一步操作**最相关的 "
            "1~3 个章节,把它们的名字原样填进 relevant_sections;不确定就少填或留空。完整内容会在生成"
            f"指令时按需调出,无需你复述。\n{names}"
        )

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
