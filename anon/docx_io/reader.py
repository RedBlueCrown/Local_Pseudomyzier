"""Read every story part of a .docx into paragraphs with character offset maps.

Paragraph text is the *visible* text in "accepted" view: inserted text counts,
deleted text does not. Every character maps back to the run and run child it
came from, so detections can be expressed as character offsets and the writer
can split runs exactly at those offsets.

Text conventions (identical for original and review copy, so texts compare):
  w:t                -> its text
  w:tab / w:ptab     -> "\\t"
  w:br / w:cr        -> "\\n"
  w:noBreakHyphen    -> U+2011, w:softHyphen -> U+00AD
  drawings, fields chars, symbols, footnote references, math ...
                     -> U+FFFC (OBJECT, never part of a detection)
  instrText, comment references, page-break hints -> nothing
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lxml import etree

from .package import M_NS, MC_NS, DocxPackage, w

OBJECT = "￼"

W_P = w("p")
W_R = w("r")
W_DEL = w("del")
W_MOVEFROM = w("moveFrom")

_SKIP_CONTAINERS = {w("pPr"), w("rPr"), w("sdtPr"), w("sdtEndPr"), w("smartTagPr"), w("customXmlPr")}
_MARKERS = {
    w(t)
    for t in (
        "bookmarkStart", "bookmarkEnd", "commentRangeStart", "commentRangeEnd", "permStart", "permEnd",
        "proofErr", "moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd",
        "customXmlInsRangeStart", "customXmlInsRangeEnd", "customXmlDelRangeStart", "customXmlDelRangeEnd",
    )
}
_PARA_OBJECTS = {f"{{{M_NS}}}oMath", f"{{{M_NS}}}oMathPara"}

_RUN_TEXT = {w("t")}
_RUN_CHARS = {
    w("tab"): "\t",
    w("ptab"): "\t",
    w("br"): "\n",
    w("cr"): "\n",
    w("noBreakHyphen"): "‑",
    w("softHyphen"): "­",
}
_RUN_OBJECTS = {
    w(t)
    for t in (
        "sym", "footnoteReference", "endnoteReference", "footnoteRef", "endnoteRef", "separator",
        "continuationSeparator", "drawing", "pict", "object", "fldChar", "pgNum", "dayShort", "dayLong",
        "monthShort", "monthLong", "yearShort", "yearLong", "ruby", "contentPart", "embeddedObject",
    )
} | {f"{{{MC_NS}}}AlternateContent"}


@dataclass
class Segment:
    start: int
    end: int
    run: etree._Element | None
    node: etree._Element
    kind: str  # "text" | "char" | "object"


@dataclass
class Marker:
    offset: int
    element: etree._Element


@dataclass
class Paragraph:
    part: str
    kind: str
    index: int
    element: etree._Element
    text: str
    segments: list[Segment]
    markers: list[Marker]
    norm: str = ""
    norm_map: list[int] = field(default_factory=list)  # norm index -> orig index (len(norm)+1)
    anchor: tuple = ()

    @property
    def pid(self) -> tuple[str, int]:
        return (self.part, self.index)

    def to_orig(self, ns: int, ne: int) -> tuple[int, int]:
        """Map a [ns, ne) span of the normalised text to original offsets."""
        if ne <= ns:
            o = self.norm_map[ns]
            return o, o
        return self.norm_map[ns], self.norm_map[ne - 1] + 1

    def order_key(self, offset: int) -> tuple:
        """Reading-order key of a character in this paragraph."""
        if self.kind == "document":
            return (0, self.index, offset, 0, 0)
        if self.kind in ("footnotes", "endnotes") and self.anchor:
            a_idx, a_off, seq = self.anchor
            return (0, a_idx, a_off, 1 + seq, offset)
        group = {"header": 1, "footer": 1, "comments": 2}.get(self.kind, 3)
        part_seq = self.anchor[0] if self.anchor else 0
        return (group, part_seq, self.index, offset, 0)

    def runs_between(self, start: int, end: int) -> list[etree._Element]:
        """Runs whose characters lie inside [start, end) (after splitting)."""
        out: list[etree._Element] = []
        for seg in self.segments:
            if seg.run is None or seg.end <= start or seg.start >= end:
                continue
            if seg.run not in out:
                out.append(seg.run)
        return out


# ---------------------------------------------------------------------------
# Normalisation for matching only
# ---------------------------------------------------------------------------
_REMOVE = {"­", "​", "‌", "‍", "⁠", "﻿"}
_MAP = {
    **{c: " " for c in "           "},
    **{c: "-" for c in "‐‑‒–—−"},
    **{c: '"' for c in "„“”«»″"},
    **{c: "'" for c in "‚‘’‹›′"},
}


def normalise(text: str) -> tuple[str, list[int]]:
    out: list[str] = []
    mapping: list[int] = []
    for i, ch in enumerate(text):
        if ch in _REMOVE:
            continue
        out.append(_MAP.get(ch, ch))
        mapping.append(i)
    mapping.append(len(text))
    return "".join(out), mapping


def normalise_simple(text: str) -> str:
    return normalise(text)[0]


# ---------------------------------------------------------------------------
# Paragraph extraction
# ---------------------------------------------------------------------------
def _run_segments(run: etree._Element, pos: int, segs: list[Segment], buf: list[str]) -> int:
    for child in run:
        tag = child.tag
        if not isinstance(tag, str):
            continue
        if tag in _RUN_TEXT:
            text = child.text or ""
            if text:
                segs.append(Segment(pos, pos + len(text), run, child, "text"))
                buf.append(text)
                pos += len(text)
        elif tag in _RUN_CHARS:
            segs.append(Segment(pos, pos + 1, run, child, "char"))
            buf.append(_RUN_CHARS[tag])
            pos += 1
        elif tag in _RUN_OBJECTS:
            segs.append(Segment(pos, pos + 1, run, child, "object"))
            buf.append(OBJECT)
            pos += 1
        # everything else (rPr, instrText, delText, commentReference,
        # lastRenderedPageBreak, annotationRef ...) is zero-width
    return pos


def _walk(container: etree._Element, pos: int, segs: list[Segment], markers: list[Marker], buf: list[str]) -> int:
    for child in container:
        tag = child.tag
        if not isinstance(tag, str):
            continue
        if tag == W_R:
            pos = _run_segments(child, pos, segs, buf)
        elif tag == W_P or tag in _SKIP_CONTAINERS:
            continue
        elif tag in (W_DEL, W_MOVEFROM):
            continue  # deleted content is not visible in accepted view
        elif tag in _MARKERS:
            markers.append(Marker(pos, child))
        elif tag in _PARA_OBJECTS:
            segs.append(Segment(pos, pos + 1, None, child, "object"))
            buf.append(OBJECT)
            pos += 1
        else:
            # hyperlink, ins, moveTo, smartTag, sdt/sdtContent, customXml,
            # fldSimple, bdo, dir ... -> descend
            pos = _walk(child, pos, segs, markers, buf)
    return pos


def extract_paragraph(p: etree._Element, part: str, kind: str, index: int) -> Paragraph:
    segs: list[Segment] = []
    markers: list[Marker] = []
    buf: list[str] = []
    _walk(p, 0, segs, markers, buf)
    text = "".join(buf)
    norm, mapping = normalise(text)
    return Paragraph(part, kind, index, p, text, segs, markers, norm, mapping)


def paragraph_text(p: etree._Element) -> str:
    buf: list[str] = []
    _walk(p, 0, [], [], buf)
    return "".join(buf)


@dataclass
class Document:
    pkg: DocxPackage
    paragraphs: list[Paragraph]

    def __post_init__(self) -> None:
        self._index = {p.pid: p for p in self.paragraphs}

    def get(self, part: str, index: int) -> Paragraph:
        return self._index[(part, index)]

    def by_part(self, part: str) -> list[Paragraph]:
        return [p for p in self.paragraphs if p.part == part]

    def refresh(self, para: Paragraph) -> Paragraph:
        """Re-extract one paragraph after its XML changed (e.g. run splits)."""
        new = extract_paragraph(para.element, para.part, para.kind, para.index)
        new.anchor = para.anchor
        idx = self.paragraphs.index(para)
        self.paragraphs[idx] = new
        self._index[new.pid] = new
        return new


def read_document(pkg: DocxPackage) -> Document:
    paragraphs: list[Paragraph] = []
    note_anchor: dict[tuple[str, str], tuple[int, int]] = {}
    part_seq = 0
    for part, kind in pkg.story_parts():
        root = pkg.xml(part)
        part_seq += 1
        note_seq: dict[str, int] = {}
        for idx, p in enumerate(root.iter(W_P)):
            para = extract_paragraph(p, part, kind, idx)
            if kind == "document":
                for seg in para.segments:
                    if seg.kind == "object" and seg.node.tag in (w("footnoteReference"), w("endnoteReference")):
                        note_kind = "footnotes" if seg.node.tag == w("footnoteReference") else "endnotes"
                        note_anchor.setdefault((note_kind, seg.node.get(w("id"))), (idx, seg.start))
            elif kind in ("footnotes", "endnotes"):
                note = next((a for a in p.iterancestors() if a.tag in (w("footnote"), w("endnote"))), None)
                note_id = note.get(w("id")) if note is not None else None
                anchor = note_anchor.get((kind, note_id))
                if anchor is not None:
                    seq = note_seq.get(note_id, 0)
                    note_seq[note_id] = seq + 1
                    para.anchor = (anchor[0], anchor[1], seq)
            else:
                para.anchor = (part_seq,)
            paragraphs.append(para)
    return Document(pkg, paragraphs)
