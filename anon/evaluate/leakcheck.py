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

import io
import re
import zipfile
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


BLOCKING = {"leak", "open_review", "partial_review"}
# keep-list roles that may excuse a hit. Heuristic citation matches (cited
# authors / case names) do not: a party name there needs a reviewer decision.
ALLOWED_KEEP_ROLES = {"judge", "clerk", "case_number", "citation", "authority", "static", "court_seat"}


@dataclass
class LeakReport:
    hits: list[Hit] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # blocking, not tied to a term
    unchecked: list[str] = field(default_factory=list)  # parts that cannot be searched (images ...)

    @property
    def leaks(self) -> list[Hit]:
        return [h for h in self.hits if h.status in BLOCKING]

    @property
    def ok(self) -> bool:
        return not self.leaks and not self.problems

    def summary_lines(self) -> list[str]:
        lines = list(self.problems)
        counts: dict[str, int] = {}
        for h in self.hits:
            counts[h.status] = counts.get(h.status, 0) + 1
        for status, n in sorted(counts.items()):
            lines.append(f"{status}: {n}")
        label = {"leak": "LEAK", "open_review": "OFFENER PRÜFHINWEIS", "partial_review": "TEILWEISE GEPRÜFT"}
        for h in self.leaks:
            where = f"{h.part} §{h.para + 1}" if h.para is not None else h.part
            lines.append(f"{label[h.status]} {h.entity} ({h.category}) in {where}")
        if self.unchecked:
            lines.append("nicht prüfbar (manuell kontrollieren): " + ", ".join(self.unchecked))
        return lines


_STRUCTURAL = re.compile(r"^(?:-?\d+|[0-9A-Fa-f]{8}|\d+(?:\.\d+)?(?:pt|cm|mm|in|%|emu)?|)$")


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


def _norm_cmp(text: str) -> str:
    return " ".join(normalise_simple(text).split()).casefold()


_TEXT_ATTRS = {"tooltip", "descr", "title", "name", "Target", "instr", "alias", "tag", "lastValue",
               "docLocation", "author", "initials", "val"}
_ALT_TEXT = re.compile(r"\.(?:html?|mht|mhtml|rtf|txt|xml)$", re.I)
_EMBEDDED_PACKAGE = re.compile(r"\.(?:docx|docm|xlsx|xlsm|pptx|pptm)$", re.I)
_MEDIA = re.compile(r"\.(?:png|jpe?g|gif|bmp|tiff?|emf|wmf|svg|bin|ole|odg|wdp)$", re.I)


def _hidden_chunks(root, story: bool) -> list[str]:
    """Text a reader of the paragraph text does not see: attributes, field
    codes, math, ruby; for non-story parts all text."""
    chunks: list[str] = []
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for k, v in el.attrib.items():
            if k.startswith("{http://www.w3.org/2000/xmlns"):
                continue
            local = k.rsplit("}", 1)[-1]
            if local in _TEXT_ATTRS or not _STRUCTURAL.match(v):
                chunks.append(v)
        if story:
            local = etree.QName(el).localname
            ruby = any(etree.QName(a).localname == "ruby" for a in el.iterancestors())
            if el.text and (el.tag in (w("instrText"), w("delText"), w("delInstrText")) or
                            (local == "t" and (el.tag != w("t") or ruby))):
                chunks.append(el.text)  # m:t (math) and w:t inside w:ruby
        elif el.text and el.text.strip():
            chunks.append(el.text)
    return chunks


def check(pkg: DocxPackage, doc: Document, terms: list[Term], tool_ranges: list[ToolRange],
          items: dict, comment_states: dict[int, str], keep_spans: list[Span],
          valid_replacements: set[str] | None = None) -> LeakReport:
    """`items`: ItemState per tool change still pending before acceptance
    (finalize.tool_item_states), `comment_states`: item -> open/done."""
    report = LeakReport()
    matcher = _Matcher(terms)
    valid = {_norm_cmp(v) for v in (valid_replacements or set())}
    consistent: dict[int, str] = {}
    for item, st in sorted(items.items()):
        if st.has_ins and not st.has_del:
            report.problems.append(f"Änderung #{item}: Löschung abgelehnt, Einfügung angenommen – Original und "
                                   f"Ersetzung stehen nebeneinander. Bitte die ganze Änderung annehmen oder ablehnen.")
        elif st.has_ins and valid and _norm_cmp(st.ins_text) not in valid:
            report.problems.append(f"Änderung #{item}: eingefügter Text wurde verändert. Bitte die Änderung "
                                   f"ablehnen und Korrekturen über die Entitäten-Datei vornehmen.")
        else:
            consistent[item] = _norm_cmp(st.ins_text)
    replaced = [(r.part, r.para, r.start, r.end, r.item) for r in tool_ranges
                if r.kind == "R" and r.item in consistent]
    partial = [(r.part, r.para, r.start, r.end) for r in tool_ranges if r.kind == "R" and r.item in items
               and r.item not in consistent]
    rejected = [(r.part, r.para, r.start, r.end) for r in tool_ranges if r.kind == "R" and r.item not in items]
    resolved = [(r.part, r.para, r.start, r.end) for r in tool_ranges
                if r.kind == "C" and comment_states.get(r.item, "deleted") != "open"]
    open_c = [(r.part, r.para, r.start, r.end) for r in tool_ranges
              if r.kind == "C" and comment_states.get(r.item) == "open"]
    keeps = [(k.part, k.para, k.start, k.end) for k in keep_spans
             if k.meta.get("strength", "strong") == "strong" and k.role in ALLOWED_KEEP_ROLES]
    story = {part for part, _ in pkg.story_parts()}

    for para in doc.paragraphs:
        for m, term in matcher.finditer(para.norm):
            s, e = para.to_orig(m.start(), m.end())
            own = [r for r in replaced if r[0] == para.part and r[1] == para.index and r[2] <= s and e <= r[3]]
            if term.kept:
                status = "kept_by_reviewer"
            elif own:
                # only the tool's own inserted text (e.g. plot 10 renumbered to 10)
                status = "tool_replacement" if _norm_cmp(m.group()) == consistent[own[0][4]] else "partial_review"
            elif _inside(partial, para.part, para.index, s, e):
                status = "partial_review"
            elif _inside(rejected, para.part, para.index, s, e):
                status = "rejected_by_reviewer"
            elif _inside(resolved, para.part, para.index, s, e):
                status = "resolved_comment"
            elif _inside(open_c, para.part, para.index, s, e):
                status = "open_review"
            elif _inside(keeps, para.part, para.index, s, e):
                status = "keep_list"
            else:
                status = "leak"
            report.hits.append(Hit(term.entity, term.category, para.part, para.index, status))

    for name in pkg.names:
        blobs: list[str] = []
        if pkg.is_xml(name):
            blobs = _hidden_chunks(pkg.xml(name), name in story)
        elif _ALT_TEXT.search(name):  # altChunk content (HTML/RTF/text)
            raw = pkg.read(name).decode("utf-8", errors="replace")
            blobs = [re.sub(r"<[^>]+>|\\[a-z]+-?\d* ?", " ", raw)]
        elif _EMBEDDED_PACKAGE.search(name):
            try:  # embedded Office package: search all of its XML
                with zipfile.ZipFile(io.BytesIO(pkg.read(name))) as zf:
                    for n in zf.namelist():
                        if n.endswith((".xml", ".rels")):
                            blobs.append(re.sub(r"<[^>]+>", " ", zf.read(n).decode("utf-8", errors="replace")))
            except (zipfile.BadZipFile, OSError):
                report.unchecked.append(name)
        elif _MEDIA.search(name) or name.startswith("word/embeddings/"):
            report.unchecked.append(name)
        if not blobs:
            continue
        blob = normalise_simple("\n".join(blobs))
        seen: set[str] = set()
        for _m, term in matcher.finditer(blob):
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
    pats = [re.compile(r"(?<![A-Za-z0-9ÄÖÜäöüß])" + re.escape(t) + r"(?![A-Za-z0-9ÄÖÜäöüß])", re.I)
            for t in sorted(set(terms), key=len, reverse=True) if len(t) >= 3]

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
            if hl.get(w("tooltip")):
                hl.set(w("tooltip"), scrub(hl.get(w("tooltip"))))
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
