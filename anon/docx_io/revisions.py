"""Write anonymisation edits as native Word tracked changes and comments.

For a change on [start, end) of a paragraph:
  1. split runs at the span boundaries (formatting copied, text unchanged),
  2. wrap the original runs in <w:del> (text moved to <w:delText>),
  3. add <w:ins> with one run carrying the original rPr + category highlight,
  4. surround everything with a hidden bookmark "_anonR_<n>" so that finalize
     can later tell where the tool proposed something the reviewer rejected.

Comment-only items get commentRangeStart/End + reference and a "_anonC_<n>"
bookmark; their comment text starts with "[#<n>]".

Reject All therefore restores exactly the original runs; Accept All yields the
original text with each span replaced.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone

from lxml import etree

from .package import CT_COMMENTS, REL_COMMENTS, W_NS, XML_NS, DocxPackage, w
from .reader import _RUN_CHARS, _RUN_OBJECTS, OBJECT, Document, Paragraph, t_text

BOOKMARK_CHANGE = "_anonR_"
BOOKMARK_COMMENT = "_anonC_"
COMMENT_MARKER = "[#{n}] "

# CT_RPr child order (ECMA-376). Word rejects out-of-order run properties.
RPR_ORDER = [
    "rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike", "outline", "shadow",
    "emboss", "imprint", "noProof", "snapToGrid", "vanish", "webHidden", "color", "spacing", "w", "kern",
    "position", "sz", "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign", "rtl", "cs",
    "em", "lang", "eastAsianLayout", "specVanish", "oMath", "rPrChange",
]
_RPR_INDEX = {w(t): i for i, t in enumerate(RPR_ORDER)}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


class IdAllocator:
    """One monotonic w:id source for revisions, bookmarks and comments,
    seeded from the largest numeric w:id anywhere in the package."""

    def __init__(self, pkg: DocxPackage):
        top = 0
        attr = f"{{{W_NS}}}id"
        for name in pkg.iter_xml_parts():
            if not name.startswith("word/"):
                continue
            try:
                root = pkg.xml(name)
            except Exception:
                continue
            for el in root.iter():
                val = el.get(attr) if isinstance(el.tag, str) else None
                if val is not None and val.lstrip("-").isdigit():
                    top = max(top, int(val))
        self._next = top + 1

    def next(self) -> int:
        n = self._next
        self._next += 1
        return n


def set_rpr_child(rpr: etree._Element, tag: str, attrs: dict[str, str]) -> etree._Element:
    """Insert or replace an rPr child at its schema position."""
    qtag = w(tag)
    existing = rpr.find(qtag)
    if existing is not None:
        existing.attrib.clear()
        for k, v in attrs.items():
            existing.set(w(k), v)
        return existing
    el = etree.Element(qtag)
    for k, v in attrs.items():
        el.set(w(k), v)
    target = _RPR_INDEX[qtag]
    for child in rpr:
        if _RPR_INDEX.get(child.tag, 10_000) > target:
            child.addprevious(el)
            break
    else:
        rpr.append(el)
    return el


def _set_text(el: etree._Element, text: str) -> None:
    el.text = text
    if text != text.strip() or "  " in text:
        el.set(f"{{{XML_NS}}}space", "preserve")
    else:
        el.attrib.pop(f"{{{XML_NS}}}space", None)


def _child_len(child: etree._Element) -> int:
    tag = child.tag
    if not isinstance(tag, str):
        return 0
    if tag == w("t"):
        return len(t_text(child))
    if tag in _RUN_CHARS or tag in _RUN_OBJECTS:
        return 1
    return 0


def _split_run(run: etree._Element, k: int) -> etree._Element:
    """Split `run` after k visible characters; returns the new right run."""
    right = etree.Element(w("r"))
    for key, val in run.attrib.items():
        right.set(key, val)
    rpr = run.find(w("rPr"))
    if rpr is not None:
        right.append(copy.deepcopy(rpr))
    pos = 0
    moving = False
    for child in [c for c in run if c.tag != w("rPr")]:
        length = _child_len(child)
        if moving:
            right.append(child)
        elif pos + length <= k:
            pos += length
        elif pos >= k:
            moving = True
            right.append(child)
        else:  # w:t straddling the cut
            cut = k - pos
            text = t_text(child)
            _set_text(child, text[:cut])
            new_t = etree.SubElement(right, w("t"))
            _set_text(new_t, text[cut:])
            moving = True
            pos += length
    run.addnext(right)
    return right


def _run_ranges(para: Paragraph) -> dict[int, tuple[etree._Element, int, int]]:
    ranges: dict[int, tuple[etree._Element, int, int]] = {}
    for seg in para.segments:
        if seg.run is None:
            continue
        key = id(seg.run)
        if key in ranges:
            run, s, _ = ranges[key]
            ranges[key] = (run, s, seg.end)
        else:
            ranges[key] = (seg.run, seg.start, seg.end)
    return ranges


def split_paragraph(doc: Document, para: Paragraph, offsets: list[int]) -> Paragraph:
    """Split runs so that every offset falls on a run boundary."""
    for off in sorted(set(offsets)):
        for run, s, e in _run_ranges(para).values():
            if s < off < e:
                _split_run(run, off - s)
                para = doc.refresh(para)
                break
    return para


@dataclass
class Edit:
    part: str
    para: int
    start: int
    end: int
    replacement: str | None  # None -> comment only
    author: str
    highlight: str | None
    item_id: int  # stable number shown in comments/summary
    comment: str | None = None


def _group_adjacent(runs: list[etree._Element]) -> list[list[etree._Element]]:
    groups: list[list[etree._Element]] = []
    for run in runs:
        if groups and groups[-1][-1].getnext() is run:
            groups[-1].append(run)
        else:
            groups.append([run])
    return groups


def _to_deleted(run: etree._Element) -> None:
    for t in run.findall(w("t")):
        t.tag = w("delText")
    for t in run.findall(w("instrText")):
        t.tag = w("delInstrText")


def _bookmark(name: str, bid: int) -> tuple[etree._Element, etree._Element]:
    start = etree.Element(w("bookmarkStart"))
    start.set(w("id"), str(bid))
    start.set(w("name"), name)
    end = etree.Element(w("bookmarkEnd"))
    end.set(w("id"), str(bid))
    return start, end


class CommentWriter:
    def __init__(self, pkg: DocxPackage):
        self.pkg = pkg
        main = pkg.main_part()
        parts = [p for p in pkg.related_parts(main, REL_COMMENTS) if pkg.has(p)]
        if parts:
            self.part = parts[0]
        else:
            self.part = "word/comments.xml"
            root = etree.Element(w("comments"), nsmap={"w": W_NS})
            pkg.add_part(self.part, root, CT_COMMENTS)
            pkg.add_relationship(main, REL_COMMENTS, "comments.xml")
        self.root = pkg.xml(self.part)

    def add(self, cid: int, author: str, date: str, text: str) -> None:
        c = etree.SubElement(self.root, w("comment"))
        c.set(w("id"), str(cid))
        c.set(w("author"), author)
        c.set(w("date"), date)
        c.set(w("initials"), author[:1].upper())
        for i, line in enumerate(text.split("\n")):
            p = etree.SubElement(c, w("p"))
            if i == 0:
                r0 = etree.SubElement(p, w("r"))
                etree.SubElement(r0, w("annotationRef"))
            r = etree.SubElement(p, w("r"))
            _set_text(etree.SubElement(r, w("t")), line)


class RevisionWriter:
    def __init__(self, doc: Document, date: str | None = None):
        self.doc = doc
        self.pkg = doc.pkg
        self.ids = IdAllocator(doc.pkg)
        self.date = date or now_iso()
        self._comments: CommentWriter | None = None
        self.skipped: list[Edit] = []

    @property
    def comments(self) -> CommentWriter:
        if self._comments is None:
            self._comments = CommentWriter(self.pkg)
        return self._comments

    def apply(self, edits: list[Edit]) -> None:
        by_para: dict[tuple[str, int], list[Edit]] = {}
        for e in edits:
            by_para.setdefault((e.part, e.para), []).append(e)
        for (part, idx), items in by_para.items():
            self._apply_paragraph(self.doc.get(part, idx), sorted(items, key=lambda e: e.start))

    def _apply_paragraph(self, para: Paragraph, edits: list[Edit]) -> None:
        last_end = -1
        valid: list[Edit] = []
        for e in edits:
            if e.start < last_end or e.start >= e.end or e.end > len(para.text):
                self.skipped.append(e)
                continue
            if e.replacement is not None and OBJECT in para.text[e.start : e.end]:
                # never delete drawings/fields/footnote marks: downgrade to comment
                e = Edit(e.part, e.para, e.start, e.end, None, e.author, None, e.item_id,
                         e.comment or "Enthält ein Objekt (Feld/Fussnote/Grafik); bitte manuell prüfen.")
            if e.replacement is None and para.kind in ("header", "footer", "comments"):
                self.skipped.append(e)  # no comments in headers/footers, none nested in comments
                continue
            valid.append(e)
            last_end = e.end
        if not valid:
            return
        offsets = [o for e in valid for o in (e.start, e.end)]
        para = split_paragraph(self.doc, para, offsets)
        plan = [(e, para.runs_between(e.start, e.end)) for e in valid]
        for e, runs in plan:
            got = "".join(
                para.text[s.start : s.end] for s in para.segments if s.run in runs
            )
            if not runs or got != para.text[e.start : e.end]:
                self.skipped.append(e)
                continue
            if e.replacement is None:
                self._comment(e, runs)
            else:
                self._change(e, runs)

    def _change(self, e: Edit, runs: list[etree._Element]) -> None:
        first_rpr = runs[0].find(w("rPr"))
        bm_id = self.ids.next()
        bm_start, bm_end = _bookmark(f"{BOOKMARK_CHANGE}{e.item_id}", bm_id)
        last_del = None
        for group in _group_adjacent(runs):
            d = etree.Element(w("del"))
            d.set(w("id"), str(self.ids.next()))
            d.set(w("author"), e.author)
            d.set(w("date"), self.date)
            group[0].addprevious(d)
            for run in group:
                d.append(run)
                _to_deleted(run)
            if last_del is None:
                d.addprevious(bm_start)
            last_del = d
        anchor = last_del
        if e.replacement:
            ins = etree.Element(w("ins"))
            ins.set(w("id"), str(self.ids.next()))
            ins.set(w("author"), e.author)
            ins.set(w("date"), self.date)
            r = etree.SubElement(ins, w("r"))
            rpr = copy.deepcopy(first_rpr) if first_rpr is not None else etree.Element(w("rPr"))
            for bad in rpr.findall(w("rPrChange")):
                rpr.remove(bad)
            if e.highlight and rpr.find(w("highlight")) is None:
                set_rpr_child(rpr, "highlight", {"val": e.highlight})
            if len(rpr):
                r.append(rpr)
            _set_text(etree.SubElement(r, w("t")), e.replacement)
            anchor.addnext(ins)
            anchor = ins
        anchor.addnext(bm_end)

    def _comment(self, e: Edit, runs: list[etree._Element]) -> None:
        cid = self.ids.next()
        bm_id = self.ids.next()
        bm_start, bm_end = _bookmark(f"{BOOKMARK_COMMENT}{e.item_id}", bm_id)
        cs = etree.Element(w("commentRangeStart"))
        cs.set(w("id"), str(cid))
        ce = etree.Element(w("commentRangeEnd"))
        ce.set(w("id"), str(cid))
        ref_run = etree.Element(w("r"))
        ref = etree.SubElement(ref_run, w("commentReference"))
        ref.set(w("id"), str(cid))
        runs[0].addprevious(bm_start)
        runs[0].addprevious(cs)
        runs[-1].addnext(ce)
        ce.addnext(ref_run)
        ref_run.addnext(bm_end)
        text = COMMENT_MARKER.format(n=e.item_id) + (e.comment or "Bitte prüfen.")
        self.comments.add(cid, e.author, self.date, text)
