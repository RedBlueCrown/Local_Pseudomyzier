"""Render a .docx (as read by docx_io.reader) to HTML for the review screen.

Every paragraph becomes <p data-pid="part|index">. Proposed changes are
<mark data-key=...> elements with the replacement in a trailing
<span class="rep" data-skip>. Elements that stand for characters that are not
plain text (tabs, line breaks, footnote marks, objects) carry data-len, so
the browser can turn a text selection back into exact character offsets of
the paragraph text used by the pipeline:
  text node        -> its length
  [data-len=n]     -> n, children ignored
  [data-skip]      -> 0, children ignored
"""

from __future__ import annotations

import html
from dataclasses import dataclass

from lxml import etree

from ..docx_io.package import DocxPackage, w
from ..docx_io.reader import Document, Paragraph

SECTION_TITLES = {"header": "Kopfzeile", "document": "Entscheidung", "footnotes": "Fussnoten",
                  "endnotes": "Endnoten", "footer": "Fusszeile", "comments": "Kommentare im Original"}
SECTION_ORDER = ["header", "document", "footnotes", "endnotes", "footer", "comments"]


@dataclass
class Mark:
    start: int
    end: int
    key: str
    cls: str
    rep: str
    title: str = ""


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _on(rpr, tag: str) -> bool:
    el = rpr.find(w(tag)) if rpr is not None else None
    return el is not None and el.get(w("val")) not in ("0", "false", "off", "none")


def _run_format(run) -> tuple[bool, bool, bool, str]:
    if run is None:
        return (False, False, False, "")
    rpr = run.find(w("rPr"))
    u = rpr.find(w("u")) if rpr is not None else None
    va = rpr.find(w("vertAlign")) if rpr is not None else None
    return (_on(rpr, "b"), _on(rpr, "i"), u is not None and u.get(w("val")) != "none",
            va.get(w("val"), "") if va is not None else "")


def _wrap(text_html: str, fmt: tuple) -> str:
    b, i, u, va = fmt
    if va == "superscript":
        text_html = f"<sup>{text_html}</sup>"
    elif va == "subscript":
        text_html = f"<sub>{text_html}</sub>"
    if u:
        text_html = f"<u>{text_html}</u>"
    if i:
        text_html = f"<em>{text_html}</em>"
    if b:
        text_html = f"<strong>{text_html}</strong>"
    return text_html


def _para_attrs(p: Paragraph) -> str:
    classes = []
    style = ""
    ppr = p.element.find(w("pPr"))
    if ppr is not None:
        jc = ppr.find(w("jc"))
        if jc is not None and jc.get(w("val")) in ("center", "right", "both", "end"):
            classes.append({"center": "al-c", "right": "al-r", "end": "al-r", "both": "al-j"}[jc.get(w("val"))])
        ps = ppr.find(w("pStyle"))
        if ps is not None and any(k in (ps.get(w("val")) or "").lower() for k in ("heading", "berschrift", "title", "titel")):
            classes.append("hd")
        ind = ppr.find(w("ind"))
        if ind is not None:
            left = ind.get(w("left")) or ind.get(w("start"))
            if left and left.lstrip("-").isdigit() and int(left) > 0:
                style = f' style="margin-left:{min(int(left) // 15, 240)}px"'
    if not p.text.strip():
        classes.append("empty")
    cls = f' class="{" ".join(classes)}"' if classes else ""
    return f'{cls}{style} data-pid="{_esc(p.part)}|{p.index}"'


def paragraph_html(p: Paragraph, marks: list[Mark], notes: dict[tuple[str, str], str]) -> str:
    marks = sorted(marks, key=lambda m: m.start)
    bounds = sorted({0, len(p.text)} | {m.start for m in marks} | {m.end for m in marks})

    def mark_at(pos: int) -> int:
        for idx, m in enumerate(marks):
            if m.start <= pos < m.end:
                return idx
        return -1

    pieces: list[tuple[int, str]] = []
    for seg in p.segments:
        if seg.kind == "text":
            pts = [seg.start] + [b for b in bounds if seg.start < b < seg.end] + [seg.end]
            fmt = _run_format(seg.run)
            for a, b in zip(pts, pts[1:]):
                pieces.append((mark_at(a), _wrap(_esc(p.text[a:b]), fmt)))
        elif seg.kind == "char":
            ch = p.text[seg.start]
            if ch == "\t":
                h = '<span class="tab" data-len="1"></span>'
            elif ch == "\n":
                h = '<br data-len="1">'
            else:
                h = _esc(ch)
            pieces.append((mark_at(seg.start), h))
        else:
            tag = etree.QName(seg.node).localname if isinstance(seg.node.tag, str) else ""
            if tag in ("footnoteReference", "endnoteReference"):
                num = notes.get((tag, seg.node.get(w("id"))), "*")
                h = f'<sup class="fnref" data-len="1">{_esc(num)}</sup>'
            elif tag in ("drawing", "pict", "AlternateContent", "object", "embeddedObject"):
                h = '<span class="obj" data-len="1" title="Grafik oder Textfeld">&#9635;</span>'
            else:
                h = '<span data-len="1"></span>'
            pieces.append((mark_at(seg.start), h))
    out: list[str] = []
    i = 0
    while i < len(pieces):
        mi = pieces[i][0]
        if mi < 0:
            out.append(pieces[i][1])
            i += 1
            continue
        buf = []
        while i < len(pieces) and pieces[i][0] == mi:
            buf.append(pieces[i][1])
            i += 1
        m = marks[mi]
        rep = _esc(m.rep) if m.rep else '<span class="del">entfernt</span>'
        out.append(f'<mark class="m {m.cls}" data-key="{_esc(m.key)}" title="{_esc(m.title)}">'
                   f'<span class="orig">{"".join(buf)}</span><span class="rep" data-skip="1">{rep}</span></mark>')
    return f"<p{_para_attrs(p)}>{''.join(out) or '<br data-len=0>'}</p>"


def _note_numbers(doc: Document) -> dict[tuple[str, str], str]:
    nums: dict[tuple[str, str], str] = {}
    counters = {"footnoteReference": 0, "endnoteReference": 0}
    for p in doc.paragraphs:
        if p.kind != "document":
            continue
        for seg in p.segments:
            if seg.kind == "object" and isinstance(seg.node.tag, str):
                tag = etree.QName(seg.node).localname
                if tag in counters:
                    key = (tag, seg.node.get(w("id")))
                    if key not in nums:
                        counters[tag] += 1
                        nums[key] = str(counters[tag])
    return nums


def render_part(pkg: DocxPackage, doc: Document, part: str, kind: str, marks: dict[tuple[str, int], list[Mark]],
                notes: dict[tuple[str, str], str]) -> str:
    root = pkg.xml(part)
    by_el = {p.element: p for p in doc.paragraphs if p.part == part}
    done: set[int] = set()

    def para(el) -> str:
        p = by_el.get(el)
        if p is None or p.index in done:
            return ""
        done.add(p.index)
        h = paragraph_html(p, marks.get(p.pid, []), notes)
        nested = [n for n in el.iter(w("p")) if n is not el and n in by_el]
        inner = "".join(para(n) for n in nested)
        if inner:
            h += f'<div class="textbox" title="Textfeld">{inner}</div>'
        return h

    def table(tbl) -> str:
        rows = []
        for tr in tbl.findall(w("tr")):
            cells = []
            for tc in tr.findall(w("tc")):
                span = tc.find(f"{w('tcPr')}/{w('gridSpan')}")
                colspan = f' colspan="{span.get(w("val"))}"' if span is not None else ""
                cells.append(f"<td{colspan}>{blocks(tc)}</td>")
            rows.append(f"<tr>{''.join(cells)}</tr>")
        return f'<table class="doc-table">{"".join(rows)}</table>'

    def blocks(container) -> str:
        out = []
        for child in container:
            tag = child.tag
            if tag == w("p"):
                out.append(para(child))
            elif tag == w("tbl"):
                out.append(table(child))
            elif tag in (w("sdt"), w("customXml")):
                content = child.find(w("sdtContent")) if tag == w("sdt") else child
                if content is not None:
                    out.append(blocks(content))
            elif tag in (w("footnote"), w("endnote")):
                if child.get(w("type")) in ("separator", "continuationSeparator", "continuationNotice"):
                    for p in child.iter(w("p")):
                        if p in by_el:
                            done.add(by_el[p].index)
                    continue
                ref = "footnoteReference" if tag == w("footnote") else "endnoteReference"
                num = notes.get((ref, child.get(w("id"))), "")
                out.append(f'<div class="note"><span class="note-num">{_esc(num)}</span>'
                           f'<div class="note-body">{blocks(child)}</div></div>')
            elif tag == w("comment"):
                out.append(f'<div class="note"><div class="note-body">{blocks(child)}</div></div>')
        return "".join(out)

    container = root.find(w("body")) if kind == "document" else root
    body = blocks(container if container is not None else root)
    rest = [p for p in doc.paragraphs if p.part == part and p.index not in done and p.text.strip()]
    if rest:  # never hide text from the reviewer
        body += '<div class="unplaced">' + "".join(paragraph_html(p, marks.get(p.pid, []), notes)
                                                 for p in rest) + "</div>"
    return body


def render_sections(pkg: DocxPackage, doc: Document, marks: dict[tuple[str, int], list[Mark]]) -> list[dict]:
    notes = _note_numbers(doc)
    parts = pkg.story_parts()
    sections = []
    for kind in SECTION_ORDER:
        same = [p for p, k in parts if k == kind]
        for n, part in enumerate(same, 1):
            if not any(p.text.strip() for p in doc.paragraphs if p.part == part):
                continue
            title = SECTION_TITLES[kind] + (f" {n}" if len(same) > 1 and kind in ("header", "footer") else "")
            sections.append({"kind": kind, "part": part, "title": title,
                             "html": render_part(pkg, doc, part, kind, marks, notes)})
    return sections
