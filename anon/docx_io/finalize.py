"""XML-level operations for the clean export.

accept_all / reject_all work on any story part and understand content
revisions (ins/del/moveFrom/moveTo, nested), paragraph-mark revisions,
table-row revisions and property changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from lxml import etree

from .package import W14_NS, W15_NS, DocxPackage, REL_COMMENTS, w
from .reader import Document, Paragraph
from .revisions import BOOKMARK_CHANGE, BOOKMARK_COMMENT

SUMMARY_BOOKMARK = "ANON_SUMMARY"

_PROP_CHANGES = {
    w(t)
    for t in ("rPrChange", "pPrChange", "sectPrChange", "tblPrChange", "tblPrExChange", "trPrChange",
              "tcPrChange", "tblGridChange", "numberingChange")
}
_RANGE_MARKERS = {
    w(t)
    for t in ("moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd",
              "customXmlInsRangeStart", "customXmlInsRangeEnd", "customXmlDelRangeStart",
              "customXmlDelRangeEnd", "customXmlMoveFromRangeStart", "customXmlMoveFromRangeEnd",
              "customXmlMoveToRangeStart", "customXmlMoveToRangeEnd")
}
_DEL = {w("del"), w("moveFrom")}
_INS = {w("ins"), w("moveTo")}


def _remove(el: etree._Element) -> None:
    parent = el.getparent()
    if parent is not None:
        parent.remove(el)


def _unwrap(el: etree._Element) -> None:
    parent = el.getparent()
    if parent is None:
        return
    idx = parent.index(el)
    for child in list(el):
        parent.insert(idx, child)
        idx += 1
    parent.remove(el)


def _mark_context(el: etree._Element) -> str | None:
    """'para' for paragraph-mark revisions, 'row' for table-row revisions."""
    parent = el.getparent()
    if parent is None:
        return None
    if parent.tag == w("rPr") and parent.getparent() is not None and parent.getparent().tag == w("pPr"):
        return "para"
    if parent.tag == w("rPr"):
        return "run-prop"
    if parent.tag == w("trPr"):
        return "row"
    return None


def _merge_following(p: etree._Element, marks: set[str]) -> None:
    """Join paragraph p with the next one(s) while the joined mark is deleted."""
    while True:
        nxt = p.getnext()
        if nxt is None or nxt.tag != w("p"):
            return
        nxt_ppr = nxt.find(w("pPr"))
        carry = False
        if nxt_ppr is not None:
            rpr = nxt_ppr.find(w("rPr"))
            if rpr is not None and any(rpr.find(t) is not None for t in marks):
                carry = True
        for child in list(nxt):
            if child.tag != w("pPr"):
                p.append(child)
        _remove(nxt)
        if not carry:
            return


def _restore_old_props(change: etree._Element) -> None:
    parent = change.getparent()
    old = change.find(parent.tag) if parent is not None else None
    if parent is None:
        return
    if change.tag in (w("rPrChange"), w("pPrChange")) and old is not None:
        keep = {w("rPr"), w("sectPr"), w("pPrChange")} if change.tag == w("pPrChange") else set()
        for child in list(parent):
            if child is not change and child.tag not in keep:
                parent.remove(child)
        for i, child in enumerate(list(old)):
            parent.insert(i, child)
    _remove(change)


def _resolve(root: etree._Element, accept: bool) -> int:
    count = 0
    drop, keep = (_DEL, _INS) if accept else (_INS, _DEL)
    for el in list(root.iter(*drop)):
        if _mark_context(el) is None:
            _remove(el)
            count += 1
    for el in list(root.iter(*keep)):
        if _mark_context(el) is None:
            if not accept:
                for t in el.iter(w("delText")):
                    t.tag = w("t")
                for t in el.iter(w("delInstrText")):
                    t.tag = w("instrText")
            _unwrap(el)
            count += 1
    # table rows
    for el in list(root.iter(*drop)):
        if _mark_context(el) == "row":
            tr = el.getparent().getparent()
            _remove(tr)
            count += 1
    for el in list(root.iter(*keep)):
        if _mark_context(el) == "row":
            _remove(el)
            count += 1
    # cells
    for el in list(root.iter(w("cellIns"), w("cellDel"), w("cellMerge"))):
        tc = el.getparent().getparent() if el.getparent() is not None else None
        deleted_cell = (el.tag == w("cellDel") and accept) or (el.tag == w("cellIns") and not accept)
        if deleted_cell and tc is not None and tc.tag == w("tc"):
            _remove(tc)
        else:
            _remove(el)
        count += 1
    # paragraph marks: process joined marks in document order
    for el in list(root.iter(*keep)):
        if _mark_context(el) in ("para", "run-prop"):
            _remove(el)
            count += 1
    for el in list(root.iter(*drop)):
        ctx = _mark_context(el)
        if ctx == "para":
            p = el.getparent().getparent().getparent()
            _remove(el)
            if p is not None and p.getparent() is not None:
                _merge_following(p, drop)
            count += 1
        elif ctx == "run-prop":
            _remove(el)
            count += 1
    for el in list(root.iter(*_PROP_CHANGES)):
        if accept:
            _remove(el)
        else:
            _restore_old_props(el)
        count += 1
    for el in list(root.iter(*_RANGE_MARKERS)):
        _remove(el)
    return count


def accept_all(root: etree._Element) -> int:
    return _resolve(root, accept=True)


def reject_all(root: etree._Element) -> int:
    return _resolve(root, accept=False)


def accept_all_parts(pkg: DocxPackage) -> int:
    return sum(accept_all(pkg.xml(part)) for part, _ in pkg.story_parts())


def reject_all_parts(pkg: DocxPackage) -> int:
    return sum(reject_all(pkg.xml(part)) for part, _ in pkg.story_parts())


def count_revisions(pkg: DocxPackage) -> int:
    n = 0
    for part, _ in pkg.story_parts():
        root = pkg.xml(part)
        n += sum(1 for _ in root.iter(*_DEL, *_INS, *_PROP_CHANGES))
    return n


# ---------------------------------------------------------------------------
# Summary block
# ---------------------------------------------------------------------------
def remove_summary(pkg: DocxPackage) -> bool:
    """Remove the summary block: every top-level body element from the first
    to the last one carrying an ANON_SUMMARY* bookmark (start or matching end).
    Word keeps the range bookmark intact; LibreOffice collapses a bookmark
    spanning a table into point bookmarks, hence the extra ANON_SUMMARY_END
    point bookmark in the last summary paragraph."""
    root = pkg.xml(pkg.main_part())
    body = root.find(w("body"))
    if body is None:
        return False
    starts = [b for b in body.iter(w("bookmarkStart")) if b.get(w("name"), "").startswith(SUMMARY_BOOKMARK)]
    if not starts:
        return False
    ids = {b.get(w("id")) for b in starts}
    ends = [b for b in body.iter(w("bookmarkEnd")) if b.get(w("id")) in ids]

    def top(el: etree._Element) -> etree._Element:
        while el.getparent() is not body:
            el = el.getparent()
        return el

    children = list(body)
    positions = [children.index(top(el)) for el in starts + ends]
    i, j = min(positions), max(positions)
    for el in children[i : j + 1]:
        if el.tag == w("sectPr"):
            continue
        body.remove(el)
    return True


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------
_MARKER_RE = re.compile(r"^\[#(\d+)\]")


def comments_part(pkg: DocxPackage) -> str | None:
    parts = [p for p in pkg.related_parts(pkg.main_part(), REL_COMMENTS) if pkg.has(p)]
    return parts[0] if parts else None


def _comment_text(c: etree._Element) -> str:
    return "".join(t.text or "" for t in c.iter(w("t")))


def tool_comment_states(pkg: DocxPackage) -> dict[int, str]:
    """item id -> 'open' | 'done' for every tool comment still present.
    A comment belongs to an item if its anchor lies inside the item's
    _anonC_ bookmark or its text starts with "[#n]"."""
    part = comments_part(pkg)
    if part is None:
        return {}
    done_para: set[str] = set()
    for ext in [n for n in pkg.names if n.startswith("word/commentsExtended")]:
        for el in pkg.xml(ext).iter(f"{{{W15_NS}}}commentEx"):
            if el.get(f"{{{W15_NS}}}done") in ("1", "true"):
                done_para.add(el.get(f"{{{W15_NS}}}paraId"))
    comment_state: dict[str, str] = {}
    marker_item: dict[str, int] = {}
    for c in pkg.xml(part).iter(w("comment")):
        para_ids = {p.get(f"{{{W14_NS}}}paraId") for p in c.iter(w("p"))}
        comment_state[c.get(w("id"))] = "done" if para_ids & done_para else "open"
        m = _MARKER_RE.match(_comment_text(c).strip())
        if m:
            marker_item[c.get(w("id"))] = int(m.group(1))
    states: dict[int, str] = {}
    for cid, item in marker_item.items():
        states[item] = comment_state[cid]
    for story, _ in pkg.story_parts():
        if story == part:
            continue
        open_items: dict[str, int] = {}
        for el in pkg.xml(story).iter():
            if el.tag == w("bookmarkStart") and el.get(w("name"), "").startswith(BOOKMARK_COMMENT):
                mm = _BM_RE.match(el.get(w("name"), ""))
                if mm:
                    open_items[el.get(w("id"))] = int(mm.group(1))
            elif el.tag == w("bookmarkEnd"):
                open_items.pop(el.get(w("id")), None)
            elif el.tag in (w("commentRangeStart"), w("commentReference")) and open_items:
                cid = el.get(w("id"))
                if cid in comment_state:
                    for item in open_items.values():
                        if states.get(item) != "open":
                            states[item] = comment_state[cid]
    return states


def remove_comments(pkg: DocxPackage, authors: set[str] | None) -> int:
    """Remove comments (all if authors is None, else those by these authors)
    with their anchors; drop the comment parts when none remain."""
    part = comments_part(pkg)
    if part is None:
        return 0
    root = pkg.xml(part)
    remove_ids: set[str] = set()
    removed_para_ids: set[str] = set()
    for c in list(root.iter(w("comment"))):
        if authors is None or c.get(w("author")) in authors or _MARKER_RE.match(_comment_text(c).strip()):
            remove_ids.add(c.get(w("id")))
            removed_para_ids |= {p.get(f"{{{W14_NS}}}paraId") for p in c.iter(w("p"))}
            _remove(c)
    for story, _ in pkg.story_parts():
        if not pkg.has(story):
            continue
        sroot = pkg.xml(story)
        for el in list(sroot.iter(w("commentRangeStart"), w("commentRangeEnd"))):
            if el.get(w("id")) in remove_ids:
                _remove(el)
        for ref in list(sroot.iter(w("commentReference"))):
            if ref.get(w("id")) in remove_ids:
                run = ref.getparent()
                _remove(ref)
                if run is not None and run.tag == w("r") and all(c.tag == w("rPr") for c in run):
                    _remove(run)
    extra = [n for n in pkg.names if re.match(r"word/comments(Extended|Ids|Extensible)\d*\.xml$", n)]
    if len(root) == 0:
        pkg.remove_part(part)
        for n in extra:
            pkg.remove_part(n)
    else:
        for n in extra:
            xroot = pkg.xml(n)
            for el in list(xroot):
                attrs = set(el.attrib.values())
                if attrs & removed_para_ids:
                    xroot.remove(el)
    return len(remove_ids)


# ---------------------------------------------------------------------------
# Tool bookmarks
# ---------------------------------------------------------------------------
@dataclass
class ToolRange:
    kind: str  # "R" (change) | "C" (comment)
    item: int
    part: str
    para: int
    start: int
    end: int


_BM_RE = re.compile(rf"^(?:{BOOKMARK_CHANGE}|{BOOKMARK_COMMENT})(\d+)$")


def tool_ranges(doc: Document) -> list[ToolRange]:
    out: list[ToolRange] = []
    for para in doc.paragraphs:
        opened: dict[str, tuple[str, int, int]] = {}
        for m in para.markers:
            el = m.element
            if el.tag == w("bookmarkStart"):
                name = el.get(w("name"), "")
                mm = _BM_RE.match(name)
                if mm:
                    kind = "R" if name.startswith(BOOKMARK_CHANGE) else "C"
                    opened[el.get(w("id"))] = (kind, int(mm.group(1)), m.offset)
            elif el.tag == w("bookmarkEnd") and el.get(w("id")) in opened:
                kind, item, start = opened.pop(el.get(w("id")))
                out.append(ToolRange(kind, item, para.part, para.index, start, m.offset))
        # a start without end in the same paragraph is ignored: it must not
        # widen what the leak check accepts
    return out


@dataclass
class ItemState:
    has_del: bool = False
    has_ins: bool = False
    ins_text: str = ""


def tool_item_states(pkg: DocxPackage, tool_authors: set[str]) -> dict[int, ItemState]:
    """For every _anonR_ item that still has a tool revision: whether the
    deletion and the insertion are still there and what the insertion says.
    Must run BEFORE accepting. An item missing here was fully accepted or
    fully rejected by the reviewer."""
    from .reader import t_text  # noqa: PLC0415

    states: dict[int, ItemState] = {}
    for part, _ in pkg.story_parts():
        open_ids: dict[str, int] = {}
        for el in pkg.xml(part).iter():
            tag = el.tag
            if tag == w("bookmarkStart"):
                name = el.get(w("name"), "")
                mm = _BM_RE.match(name)
                if mm and name.startswith(BOOKMARK_CHANGE):
                    open_ids[el.get(w("id"))] = int(mm.group(1))
            elif tag == w("bookmarkEnd"):
                open_ids.pop(el.get(w("id")), None)
            elif (tag in _DEL or tag in _INS) and open_ids and el.get(w("author")) in tool_authors:
                if _mark_context(el) is not None:
                    continue
                for item in open_ids.values():
                    st = states.setdefault(item, ItemState())
                    if tag in _DEL:
                        st.has_del = True
                    else:
                        st.has_ins = True
                        st.ins_text += "".join(t_text(t) for t in el.iter(w("t")))
    return states


def pending_tool_changes(pkg: DocxPackage, tool_authors: set[str]) -> set[int]:
    return set(tool_item_states(pkg, tool_authors))


def strip_tool_highlights(pkg: DocxPackage, colours: set[str]) -> int:
    """Remove category highlights from runs inside _anonR_ bookmarks."""
    n = 0
    for part, _ in pkg.story_parts():
        open_ids: set[str] = set()
        for el in pkg.xml(part).iter():
            if el.tag == w("bookmarkStart") and el.get(w("name"), "").startswith(BOOKMARK_CHANGE):
                open_ids.add(el.get(w("id")))
            elif el.tag == w("bookmarkEnd"):
                open_ids.discard(el.get(w("id")))
            elif el.tag == w("highlight") and open_ids and el.get(w("val")) in colours:
                rpr = el.getparent()
                _remove(el)
                if rpr is not None and len(rpr) == 0 and rpr.getparent() is not None:
                    _remove(rpr)
                n += 1
    return n


def remove_tool_bookmarks(pkg: DocxPackage) -> int:
    n = 0
    for part, _ in pkg.story_parts():
        root = pkg.xml(part)
        ids = set()
        for el in list(root.iter(w("bookmarkStart"))):
            if el.get(w("name"), "").startswith(("_anon", SUMMARY_BOOKMARK)):
                ids.add(el.get(w("id")))
                _remove(el)
                n += 1
        for el in list(root.iter(w("bookmarkEnd"))):
            if el.get(w("id")) in ids:
                _remove(el)
    return n


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------
_SETTINGS_HEAD = ["writeProtection", "view", "zoom", "removePersonalInformation", "removeDateAndTime"]


def strip_metadata(pkg: DocxPackage, *, strip_custom_xml: bool = True, remove_glossary: bool = True,
                   remove_thumbnail: bool = True) -> dict[str, int]:
    stats: dict[str, int] = {}
    # rsid attributes everywhere in word/
    rsid = 0
    for name in list(pkg.iter_xml_parts()):
        if not name.startswith("word/") or name.endswith(".rels"):
            continue
        root = pkg.xml(name)
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            for attr in [a for a in el.attrib if a.startswith(f"{{{w('')[1:-1]}}}rsid")]:
                del el.attrib[attr]
                rsid += 1
    stats["rsid_attributes"] = rsid

    settings = "word/settings.xml"
    if pkg.has(settings):
        root = pkg.xml(settings)
        rid_attr = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        for tag in ("rsids", "docVars", "attachedTemplate", "mailMerge", "trackRevisions"):
            for el in root.findall(w(tag)):
                for sub in el.iter():
                    if sub.get(rid_attr):
                        pkg.remove_relationship(settings, sub.get(rid_attr))
                root.remove(el)
        for ns in (W14_NS, W15_NS):
            for el in root.findall(f"{{{ns}}}docId"):
                root.remove(el)  # links the clean file to its source document
        if root.find(w("removePersonalInformation")) is None:
            el = etree.Element(w("removePersonalInformation"))
            pos = 0
            for i, child in enumerate(root):
                if child.tag in {w(t) for t in _SETTINGS_HEAD[:3]}:
                    pos = i + 1
            root.insert(pos, el)

    core = "docProps/core.xml"
    if pkg.has(core):
        root = pkg.xml(core)
        for child in list(root):
            root.remove(child)
    app = "docProps/app.xml"
    if pkg.has(app):
        root = pkg.xml(app)
        for child in list(root):
            local = etree.QName(child).localname
            if local in ("Company", "Manager", "HyperlinkBase", "TitlesOfParts", "HeadingPairs", "Template"):
                root.remove(child)
    removed = pkg.remove_parts(lambda n: n in ("docProps/custom.xml", "word/people.xml"))
    if remove_thumbnail:
        removed += pkg.remove_parts(lambda n: n.startswith("docProps/thumbnail"))
    if remove_glossary:
        removed += pkg.remove_parts(lambda n: n.startswith("word/glossary/") and not n.endswith(".rels"))
    for part, _ in pkg.story_parts():
        for el in pkg.xml(part).iter(w("date")):
            el.attrib.pop(w("fullDate"), None)  # date content control keeps the original date
    if strip_custom_xml:
        removed += pkg.remove_parts(lambda n: n.startswith("customXml/") and not n.endswith(".rels"))
        for part, _ in pkg.story_parts():
            for el in list(pkg.xml(part).iter(w("dataBinding"))):
                _remove(el)
    # remaining comments: anonymous
    cpart = comments_part(pkg)
    if cpart:
        for c in pkg.xml(cpart).iter(w("comment")):
            c.set(w("author"), "")
            c.set(w("initials"), "")
    for part, _ in pkg.story_parts():
        for el in pkg.xml(part).iter(*_DEL, *_INS):
            el.set(w("author"), "")
    stats["parts_removed"] = len(removed)
    return stats


def paragraph_texts(doc: Document, kinds: tuple[str, ...] | None = None) -> dict[tuple[str, int], str]:
    return {p.pid: p.text for p in doc.paragraphs if kinds is None or p.kind in kinds}


def iter_paragraphs(doc: Document) -> list[Paragraph]:
    return doc.paragraphs


def reject_items(pkg: DocxPackage, items: set[int], tool_authors: set[str]) -> int:
    """Reject the tool's revisions inside the _anonR_ bookmarks of `items`
    (the bookmark stays, so the leak check knows the reviewer rejected it)."""
    if not items:
        return 0
    n = 0
    for part, _ in pkg.story_parts():
        open_ids: dict[str, int] = {}
        targets: list[etree._Element] = []
        for el in pkg.xml(part).iter():
            if el.tag == w("bookmarkStart"):
                mm = _BM_RE.match(el.get(w("name"), ""))
                if mm and el.get(w("name"), "").startswith(BOOKMARK_CHANGE) and int(mm.group(1)) in items:
                    open_ids[el.get(w("id"))] = int(mm.group(1))
            elif el.tag == w("bookmarkEnd"):
                open_ids.pop(el.get(w("id")), None)
            elif (el.tag in _DEL or el.tag in _INS) and open_ids and el.get(w("author")) in tool_authors \
                    and _mark_context(el) is None:
                targets.append(el)
        for el in targets:
            if el.tag in _DEL:
                for t in el.iter(w("delText")):
                    t.tag = w("t")
                for t in el.iter(w("delInstrText")):
                    t.tag = w("instrText")
                _unwrap(el)
            else:
                _remove(el)
            n += 1
    return n
