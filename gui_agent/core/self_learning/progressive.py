"""Progressive (skill-like) knowledge: select relevant section bodies on demand.

A large manual reduces to one big ``_elements.md`` (e.g. 17K chars for 89 sections). Injecting
it whole into the Transition every turn is wasteful and dilutes attention — only the section
matching the current screen is relevant. So, like an Agent Skill (name + description always in
context, SKILL.md body read only when invoked), we keep section metadata and bodies separately.

The live policy uses deterministic signal matching over route, title and Statement contract,
then injects the selected bodies into the single LLM Transition call. This module contains no
control transition and no mutable execution phase. Leaf module: only ``re``.
"""

from __future__ import annotations

import re
from typing import Any

from gui_agent.context import ContextBlock, render_context_blocks

_MAX_SELECTED = 3  # cap injected bodies per turn
_ENGLISH_STOPWORDS = frozenset({
    "a", "all", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "is", "it", "of", "on", "or", "that", "the", "their", "this", "to",
    "was", "with",
})

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split optional YAML frontmatter off a section's markdown: ``(meta, body)``.

    Sections may carry a ``when:`` line or the richer ``selector_when:``
    matching hint. The body fed to Transition must
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
    """Loose key: drop spaces / underscores / punctuation, lowercase — so a model's
    paraphrase ("如何访问RoboTeam") still matches the file stem ("如何访问Robo_Team")."""
    return re.sub(r"[\W_]+", "", s, flags=re.UNICODE).lower()


def _tokens(s: str) -> set[str]:
    """Coarse tokens for fuzzy when-line overlap: punctuation-split words (len≥2) plus CJK
    bigrams, lowercased. `_norm` collapses boundaries so substring matching misses synonym
    rewrites (「创建启用虚拟机器人时」vs「新建机器人并设为启用」); token/bigram overlap bridges
    them. Coarse on purpose — this only feeds the deterministic knowledge FALLBACK."""
    s = (s or "").lower()
    words = {
        w for w in re.split(r"[\W_]+", s, flags=re.UNICODE)
        if len(w) >= 2
        and w not in _ENGLISH_STOPWORDS
        and not re.search(r"[一-鿿]", w)
    }
    cjk = "".join(re.findall(r"[一-鿿]+", s))
    bigrams = {cjk[i : i + 2] for i in range(len(cjk) - 1)}
    return words | bigrams


class ProgressiveKnowledge:
    """Holds per-section bodies and deterministic signal-based retrieval."""

    def __init__(self, sections: dict[str, str]):
        # Raw file text in; frontmatter parsed off here so callers stay plumbing-free:
        # selector_when/when feeds deterministic matching; the stripped body feeds Transition.
        self.sections: dict[str, str] = {}  # stem -> body markdown (frontmatter stripped)
        self.whens: dict[str, str] = {}     # stem -> when-to-consult one-liner ("" if absent)
        self.metadata: dict[str, dict[str, Any]] = {}  # stem -> parsed frontmatter metadata
        for stem, text in sections.items():
            meta, body = split_frontmatter(text)
            self.sections[stem] = body
            self.metadata[stem] = meta
            self.whens[stem] = str(meta.get("selector_when") or meta.get("when") or "")
        self._index = {_norm(k): k for k in self.sections}

    def __bool__(self) -> bool:
        return bool(self.sections)

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

    def match_signals(
        self,
        signals: list[str],
        *,
        min_overlap: int = 1,
        match_titles: bool = True,
        match_whens: bool = True,
    ) -> list[str]:
        """Deterministically select sections from route/title/statement signals.

        Two passes, both capped at ``_MAX_SELECTED``: (1) bidirectional substring match of each
        signal against section titles (reuses :meth:`_match`, the strongest signal); (2) token /
        CJK-bigram overlap of the combined signals against each section's selector_when line,
        weighted toward terms that distinguish one section from the rest — this bridges the
        synonym gaps a bare title misses without letting generic words dominate."""
        raw = [s for s in signals if s and s.strip()]
        if not raw:
            return []
        picked: list[str] = []
        seen: set[str] = set()
        if match_titles:
            for nm in raw:
                key = self._match(nm)
                if key and key not in seen:
                    seen.add(key)
                    picked.append(key)
                if len(picked) >= _MAX_SELECTED:
                    return picked
        if not match_whens:
            return picked
        sig_tokens = _tokens(" ".join(raw))
        if sig_tokens:
            section_tokens = {
                stem: _tokens(when)
                for stem, when in self.whens.items()
                if when
            }
            frequency = {
                token: sum(token in tokens for tokens in section_tokens.values())
                for token in sig_tokens
            }
            scored: list[tuple[float, int, str]] = []
            for stem, when in self.whens.items():
                if stem in seen or not when:
                    continue
                overlap = sig_tokens & section_tokens[stem]
                if len(overlap) >= min_overlap:
                    score = sum(1 / frequency[token] for token in overlap)
                    scored.append((score, len(overlap), stem))
            for _, _, stem in sorted(scored, key=lambda x: (-x[0], -x[1], x[2])):
                seen.add(stem)
                picked.append(stem)
                if len(picked) >= _MAX_SELECTED:
                    break
        return picked

    def bodies(self, stems: list[str]) -> str:
        """Concatenate bodies selected by :meth:`match_signals`."""
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
                budget="high",  # deterministic retrieval picked this section
                source_type=str(meta.get("source_type") or "knowledge_section"),
                source=str(meta.get("source") or "knowledge_base"),
                ttl=str(meta.get("ttl") or "session"),
                priority=50,
                metadata=metadata,
                content=content,
            ))
        return blocks
