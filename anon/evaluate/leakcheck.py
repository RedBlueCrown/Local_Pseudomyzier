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
    status: str  # leak | kept_by_reviewer | rejected_by_reviewer | resolved_comment | keep_list


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


def _regex(term: str) -> re.Pattern:
    t = normalise_simple(term).strip()
    if is_numeric_form(t):
        return re.compile(NUM_LEFT + re.escape(t) + NUM_RIGHT)
    body = re.escape(t).replace(r"\ ", r"\s+")
    return re.compile(NAME_LEFT + body + NAME_RIGHT, re.I)


def _inside(ranges, part: str, para: int, s: int, e: int) -> bool:
    return any(r[0] == part and r[1] == para and r[2] <= s and e <= r[3] for r in ranges)


def check(pkg: DocxPackage, doc: Document, terms: list[Term], tool_ranges: list[ToolRange],
          pending: set[int], comment_states: dict[int, str], keep_spans: list[Span]) -> LeakReport:
    report = LeakReport()
    compiled = [(t, _regex(t.text)) for t in terms if len(t.text.strip()) >= 2]
    rejected = [(r.part, r.para, r.start, r.end) for r in tool_ranges if r.kind == "R" and r.item not in pending]
    resolved = [(r.part, r.para, r.start, r.end) for r in tool_ranges
                if r.kind == "C" and comment_states.get(r.item, "deleted") != "open"]
    keeps = [(k.part, k.para, k.start, k.end) for k in keep_spans
             if k.meta.get("strength", "strong") == "strong"]
    story = {part for part, _ in pkg.story_parts()}

    for para in doc.paragraphs:
        for term, rx in compiled:
            for m in rx.finditer(para.norm):
                s, e = para.to_orig(m.start(), m.end())
                if term.kept:
                    status = "kept_by_reviewer"
                elif _inside(rejected, para.part, para.index, s, e):
                    status = "rejected_by_reviewer"
                elif _inside(resolved, para.part, para.index, s, e):
                    status = "resolved_comment"
                elif _inside(keeps, para.part, para.index, s, e):
                    status = "keep_list"
                else:
                    status = "leak"
                report.hits.append(Hit(term.entity, term.category, para.part, para.index, status))

    # hidden places: attributes and field codes in story parts, everything elsewhere
    for name in pkg.iter_xml_parts():
        root = pkg.xml(name)
        chunks: list[str] = []
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            chunks.extend(v for k, v in el.attrib.items() if not k.startswith("{http://www.w3.org/2000/xmlns"))
            if name in story:
                if el.tag in (w("instrText"), w("delText"), w("delInstrText")) and el.text:
                    chunks.append(el.text)
            elif el.text and el.text.strip():
                chunks.append(el.text)
        if not chunks:
            continue
        blob = normalise_simple("\n".join(chunks))
        for term, rx in compiled:
            if rx.search(blob):
                status = "kept_by_reviewer" if term.kept else "leak"
                report.hits.append(Hit(term.entity, term.category, name, None, status))
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
