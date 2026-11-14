"""Leak check on the clean export.

Searches every story part and every other XML part of the clean file for
every original surface form and number (whole-token, normalised, case
insensitive). A hit is acceptable only if
  * the entity was set to "keep" by the reviewer,
  * it lies inside a tool proposal the reviewer rejected (tracked change gone,
    text unchanged) or a "Prüfen" comment the reviewer deleted/resolved,
  * it lies inside a keep-list span (judge name, citation, authority ...).
Everything else is a leak.

Reports never contain document text: only entity ids, categories and
locations (part, paragraph number).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

from ..detect.span import Span
from ..detect.text import NAME_LEFT, NAME_RIGHT, NUM_LEFT, NUM_RIGHT, is_numeric_form
from ..docx_io.finalize import ToolRange
from ..docx_io.package import DocxPackage, w
from ..docx_io.reader import Document, normalise_simple


@dataclass
class Term:
    text: str
    entity: str
    category: str
    kept: bool  # entity decision "keep" (reviewer)


@dataclass
class Hit:
    entity: str
    category: str
    part: str
    para: int | None
    status: str  # leak | kept_by_reviewer | rejected_by_reviewer | resolved_comment | keep_list | tool_replacement


@dataclass
class LeakReport:
    hits: list[Hit] = field(default_factory=list)

    @property
    def leaks(self) -> list[Hit]:
        return [h for h in self.hits if h.status == "leak"]

    @property
    def ok(self) -> bool:
        return not self.leaks

    def summary_lines(self) -> list[str]:
        lines = []
        counts: dict[str, int] = {}
        for h in self.hits:
            counts[h.status] = counts.get(h.status, 0) + 1
        for status, n in sorted(counts.items()):
            lines.append(f"{status}: {n}")
        for h in self.leaks:
            where = f"{h.part} §{h.para + 1}" if h.para is not None else h.part
            lines.append(f"LEAK {h.entity} ({h.category}) in {where}")
        return lines


_STRUCTURAL = re.compile(r"^[\d\s.,:;+%-]*$")


class _Matcher:
    """All terms folded into two regexes (names: case-insensitive, hyphen is a
    word character; numbers: digit boundaries). Longest term wins."""

    def __init__(self, terms: list[Term]):
        self.by_key: dict[str, list[Term]] = {}
        names, numbers = [], []
        for t in terms:
            text = " ".join(normalise_simple(t.text).split())
            if len(text) < 2:
                continue
            self.by_key.setdefault(text.casefold(), []).append(t)
            (numbers if is_numeric_form(text) else names).append(text)
        self.res = []
        if names:
            body = "|".join(re.escape(x).replace(r"\ ", r"\s+") for x in sorted(set(names), key=len, reverse=True))
            self.res.append(re.compile(NAME_LEFT + "(?:" + body + ")" + NAME_RIGHT, re.I))
        if numbers:
            body = "|".join(re.escape(x) for x in sorted(set(numbers), key=len, reverse=True))
            self.res.append(re.compile(NUM_LEFT + "(?:" + body + ")" + NUM_RIGHT))

    def finditer(self, text: str):
        for rx in self.res:
            for m in rx.finditer(text):
                terms = self.by_key.get(" ".join(m.group().split()).casefold(), [])
                if terms:
                    # a term the reviewer kept wins over one that was not
                    yield m, sorted(terms, key=lambda t: not t.kept)[0]


def _inside(ranges, part: str, para: int, s: int, e: int) -> bool:
    return any(r[0] == part and r[1] == para and r[2] <= s and e <= r[3] for r in ranges)


def check(pkg: DocxPackage, doc: Document, terms: list[Term], tool_ranges: list[ToolRange],
          pending: set[int], comment_states: dict[int, str], keep_spans: list[Span]) -> LeakReport:
    report = LeakReport()
    matcher = _Matcher(terms)
    rejected = [(r.part, r.para, r.start, r.end) for r in tool_ranges if r.kind == "R" and r.item not in pending]
    replaced = [(r.part, r.para, r.start, r.end) for r in tool_ranges if r.kind == "R" and r.item in pending]
    resolved = [(r.part, r.para, r.start, r.end) for r in tool_ranges
                if r.kind == "C" and comment_states.get(r.item, "deleted") != "open"]
    keeps = [(k.part, k.para, k.start, k.end) for k in keep_spans
             if k.meta.get("strength", "strong") == "strong"]
    story = {part for part, _ in pkg.story_parts()}

    for para in doc.paragraphs:
        for m, term in matcher.finditer(para.norm):
            s, e = para.to_orig(m.start(), m.end())
            if term.kept:
                status = "kept_by_reviewer"
            elif _inside(replaced, para.part, para.index, s, e):
                status = "tool_replacement"  # e.g. plot 10 renumbered to 10: text written by the tool
            elif _inside(rejected, para.part, para.index, s, e):
                status = "rejected_by_reviewer"
            elif _inside(resolved, para.part, para.index, s, e):
                status = "resolved_comment"
            elif _inside(keeps, para.part, para.index, s, e):
                status = "keep_list"
            else:
                status = "leak"
            report.hits.append(Hit(term.entity, term.category, para.part, para.index, status))

    # hidden places: text-bearing attributes and field codes in story parts,
    # all text and attributes elsewhere (properties, settings, relationships ...)
    for name in pkg.iter_xml_parts():
        root = pkg.xml(name)
        chunks: list[str] = []
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            chunks.extend(v for v in el.attrib.values() if not _STRUCTURAL.match(v))
            if name in story:
                if el.tag in (w("instrText"), w("delText"), w("delInstrText")) and el.text:
                    chunks.append(el.text)
            elif el.text and el.text.strip():
                chunks.append(el.text)
        if not chunks:
            continue
        blob = normalise_simple("\n".join(chunks))
        seen: set[str] = set()
        for m, term in matcher.finditer(blob):
            if term.entity in seen:
                continue
            seen.add(term.entity)
            report.hits.append(Hit(term.entity, term.category, name, None,
                                   "kept_by_reviewer" if term.kept else "leak"))
    return report


def scrub_hidden(pkg: DocxPackage, terms: list[str], stars: str) -> int:
    """Remove sensitive values from field codes and hyperlink targets of the
    clean export (they are invisible, so the review copy could not show them)."""
    if not terms:
        return 0
    n = 0
    pats = [re.compile(re.escape(t), re.I) for t in sorted(set(terms), key=len, reverse=True) if len(t) >= 3]

    def scrub(text: str) -> str:
        nonlocal n
        for p in pats:
            text, k = p.subn(stars, text)
            n += k
        return text

    for part, _ in pkg.story_parts():
        root = pkg.xml(part)
        for el in root.iter(w("instrText")):
            if el.text:
                el.text = scrub(el.text)
        for el in root.iter(w("fldSimple")):
            if el.get(w("instr")):
                el.set(w("instr"), scrub(el.get(w("instr"))))
        rels = {r.get("Id"): r for r in pkg.relationships(part)}
        for hl in list(root.iter(w("hyperlink"))):
            rid = hl.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            rel = rels.get(rid)
            if rel is None or rel.get("TargetMode") != "External":
                continue
            target = rel.get("Target", "")
            if scrub(target) != target:
                parent = hl.getparent()
                idx = parent.index(hl)
                for child in list(hl):
                    parent.insert(idx, child)
                    idx += 1
                parent.remove(hl)
                pkg.remove_relationship(part, rid)
        for el in root.iter():
            if isinstance(el.tag, str) and etree.QName(el).localname == "docPr":
                for attr in ("descr", "title", "name"):
                    if el.get(attr):
                        el.set(attr, scrub(el.get(attr)))
    return n
