"""Span merging, keep-list filtering and overlap resolution.

Keep-list semantics (keep always wins, with two refinements):
  * strong person keeps (judge/clerk names): any overlap drops the candidate;
  * weak person keeps (a judge's bare surname): a longer candidate that
    contains it wins ("Peter Vogt" vs judge "Vogt"); the same extent becomes a
    conflict (comment "Prüfen"); partial overlap drops the candidate;
  * pattern keeps (case numbers, citations, cited names, authorities,
    countries): candidates inside or partially overlapping are dropped; a
    rule/Rubrum candidate strictly containing the keep survives (an address
    ending in "Fürstentum Liechtenstein"), an NER/LLM one does not.

Overlaps between candidates: Rubrum spans first, then rules/rescans, then
NER/LLM; within a tier the longest span wins, then the most confident.
"""

from __future__ import annotations

from collections import defaultdict

from .span import NAME_KINDS, Span

ROLE_PRIORITY = {"party": 5, "representative": 4, "expert_or_doctor": 3, "affected": 2, None: 0}
_PERSON_KEEP_ROLES = {"judge", "clerk"}


def _by_para(spans: list[Span]) -> dict[tuple[str, int], list[Span]]:
    out: dict[tuple[str, int], list[Span]] = defaultdict(list)
    for s in spans:
        out[s.key].append(s)
    return out


def apply_keep(candidates: list[Span], keeps: list[Span]) -> list[Span]:
    index = _by_para(keeps)
    out: list[Span] = []
    for c in candidates:
        verdict = "ok"
        conflict_with = None
        for k in index.get(c.key, ()):
            if not c.overlaps(k):
                continue
            strength = k.meta.get("strength", "strong")
            if k.role in _PERSON_KEEP_ROLES:
                if strength == "strong":
                    verdict = "drop"
                elif c.contains(k) and not c.same_extent(k):
                    continue
                elif c.same_extent(k):
                    if verdict != "drop":
                        verdict, conflict_with = "conflict", k
                else:
                    verdict = "drop"
            else:
                heuristic = k.role in ("cited_author", "cited_case")
                if heuristic and c.source in ("rubrum", "reviewer"):
                    continue  # heuristic citation pattern never overrides the Rubrum structure
                if c.contains(k) and not c.same_extent(k) and c.source not in ("ner", "llm"):
                    continue
                if heuristic and c.source == "rescan" and c.kind in NAME_KINDS:
                    # a known party name inside what looks like a citation: ask, do not guess
                    if verdict != "drop":
                        verdict, conflict_with = "cited", k
                    continue
                verdict = "drop"
            if verdict == "drop":
                break
        if verdict == "drop":
            continue
        if verdict == "conflict":
            c.confidence = min(c.confidence, 0.3)
            c.meta["conflict"] = f"auch Name einer Gerichtsperson ({conflict_with.meta.get('person', '')})"
        elif verdict == "cited":
            c.confidence = min(c.confidence, 0.3)
            c.meta["conflict"] = "steht in einem Zitat (Autor oder zitierte Entscheidung?)"
        out.append(c)
    return out


def _tier(s: Span) -> int:
    if s.source in ("rubrum", "reviewer"):
        return 0
    if s.source in ("ner", "llm"):
        return 2
    return 1


def dedupe(spans: list[Span]) -> list[Span]:
    """Fold spans with identical extent and the same kind class."""
    groups: dict[tuple, list[Span]] = defaultdict(list)
    for s in spans:
        cls = "name" if s.kind in NAME_KINDS else s.kind
        groups[(s.part, s.para, s.start, s.end, cls)].append(s)
    out: list[Span] = []
    for items in groups.values():
        best = max(items, key=lambda s: (-_tier(s), s.confidence))
        for other in items:
            if other is best:
                continue
            if ROLE_PRIORITY.get(other.role, 0) > ROLE_PRIORITY.get(best.role, 0) and best.kind in NAME_KINDS:
                best.role = other.role
            for k, v in other.meta.items():
                best.meta.setdefault(k, v)
            if other.source == "rescan" and best.source != "rescan":
                best.meta.setdefault("entity", other.meta.get("entity"))
        flagged = [s for s in items if s.meta.get("conflict") or s.meta.get("ambiguous")]
        if flagged:
            best.confidence = min(s.confidence for s in flagged)
            for key in ("conflict", "ambiguous", "note"):
                val = next((s.meta[key] for s in flagged if s.meta.get(key)), None)
                if val:
                    best.meta[key] = val
        else:
            # a detector saw the local context; a rescan only inherits the
            # entity's confidence, so it only counts where no detector fired
            direct = [s.confidence for s in items if s.source != "rescan"]
            best.confidence = max(direct) if direct else max(s.confidence for s in items)
        best.meta["sources"] = sorted({s.source for s in items})
        out.append(best)
    return out


def resolve_overlaps(spans: list[Span]) -> tuple[list[Span], dict[int, list[Span]]]:
    """Greedy selection; returns (selected, swallowed) where swallowed maps
    id(selected span) -> spans it displaced (used for leak-check terms)."""
    spans = dedupe(spans)
    order = sorted(spans, key=lambda s: (_tier(s), -len(s), -s.confidence, s.start))
    chosen: dict[tuple[str, int], list[Span]] = defaultdict(list)
    swallowed: dict[int, list[Span]] = defaultdict(list)
    selected: list[Span] = []
    for s in order:
        if len(s) <= 0:
            continue
        clash = next((c for c in chosen[s.key] if c.overlaps(s)), None)
        if clash is not None:
            swallowed[id(clash)].append(s)
            continue
        chosen[s.key].append(s)
        selected.append(s)
    selected.sort(key=lambda s: (s.part, s.para, s.start))
    return selected, swallowed
