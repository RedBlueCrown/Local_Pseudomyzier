"""Structural checks that catch what makes Word show a repair prompt.

Not a full schema validation; it checks the invariants this tool can break:
well-formed parts, content types and relationship targets, unique revision /
bookmark / comment ids, deleted text only in w:delText, attributes on
revisions, run-property order, comment anchors and comment entries.
"""

from __future__ import annotations

from lxml import etree

from .package import CT_NS, PKG_REL_NS, DocxPackage, w
from .revisions import _RPR_INDEX


def validate(pkg: DocxPackage) -> list[str]:
    problems: list[str] = []
    for name in pkg.iter_xml_parts():
        try:
            pkg.xml(name)
        except etree.XMLSyntaxError as exc:
            problems.append(f"{name}: not well-formed ({exc})")
    if problems:
        return problems
    ct = pkg.xml("[Content_Types].xml")
    overrides = {el.get("PartName", "").lstrip("/") for el in ct.findall(f"{{{CT_NS}}}Override")}
    defaults = {el.get("Extension", "").lower() for el in ct.findall(f"{{{CT_NS}}}Default")}
    for name in pkg.names:
        if name == "[Content_Types].xml":
            continue
        if name not in overrides and name.rsplit(".", 1)[-1].lower() not in defaults:
            problems.append(f"{name}: no content type")
    for name in overrides:
        if not pkg.has(name):
            problems.append(f"content type override for missing part {name}")
    for rels in pkg.all_rels_files():
        src = pkg.source_of_rels(rels)
        for rel in pkg.xml(rels).findall(f"{{{PKG_REL_NS}}}Relationship"):
            if rel.get("TargetMode") == "External":
                continue
            target = pkg.resolve(src, rel.get("Target", ""))
            if not pkg.has(target):
                problems.append(f"{rels}: relationship {rel.get('Id')} -> missing {target}")

    seen: dict[str, dict[str, str]] = {"rev": {}, "bookmark": {}}
    comment_refs: set[str] = set()
    for part, _ in pkg.story_parts():
        root = pkg.xml(part)
        for el in root.iter(w("ins"), w("del")):
            parent = el.getparent()
            if parent is not None and parent.tag in (w("rPr"), w("trPr")):
                continue
            for attr in ("id", "author", "date"):
                if el.get(w(attr)) is None:
                    problems.append(f"{part}: {etree.QName(el).localname} without w:{attr}")
            rid = el.get(w("id"))
            if rid in seen["rev"]:
                problems.append(f"{part}: duplicate revision id {rid}")
            seen["rev"][rid] = part
            if el.tag == w("del") and list(el.iter(w("t"))):
                problems.append(f"{part}: w:t inside w:del (must be w:delText)")
            if el.tag == w("ins") and list(el.iter(w("delText"))) and not list(el.iter(w("del"))):
                problems.append(f"{part}: w:delText inside w:ins")
        for el in root.iter(w("bookmarkStart")):
            bid = el.get(w("id"))
            if bid in seen["bookmark"]:
                problems.append(f"{part}: duplicate bookmark id {bid}")
            seen["bookmark"][bid] = part
        for rpr in root.iter(w("rPr")):
            idx = [_RPR_INDEX.get(c.tag, 10_000) for c in rpr if c.tag in _RPR_INDEX]
            if idx != sorted(idx):
                problems.append(f"{part}: run properties out of schema order")
                break
        starts = {e.get(w("id")) for e in root.iter(w("commentRangeStart"))}
        ends = {e.get(w("id")) for e in root.iter(w("commentRangeEnd"))}
        refs = {e.get(w("id")) for e in root.iter(w("commentReference"))}
        if starts != ends:
            problems.append(f"{part}: unbalanced comment ranges")
        comment_refs |= refs
    comments = [n for n in pkg.names if n.startswith("word/comments") and n.endswith("comments.xml")]
    defined: set[str] = set()
    for c in comments:
        defined |= {e.get(w("id")) for e in pkg.xml(c).iter(w("comment"))}
    missing = comment_refs - defined
    if missing:
        problems.append(f"comment references without comment: {sorted(missing)}")
    return problems
