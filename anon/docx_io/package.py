"""Minimal OOXML package model on top of zipfile + lxml.

Only what the tool needs: parse parts lazily, find story parts through the
relationships, add/remove parts with their relationships and content types,
and write the package back without touching parts that were not modified.
"""

from __future__ import annotations

import io
import posixpath
import zipfile
from pathlib import Path
from typing import Callable, Iterable

from lxml import etree

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
W15_NS = "http://schemas.microsoft.com/office/word/2012/wordml"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
XML_NS = "http://www.w3.org/XML/1998/namespace"
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"

REL_BASE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
REL_OFFICE_DOCUMENT = REL_BASE + "officeDocument"
REL_HEADER = REL_BASE + "header"
REL_FOOTER = REL_BASE + "footer"
REL_FOOTNOTES = REL_BASE + "footnotes"
REL_ENDNOTES = REL_BASE + "endnotes"
REL_COMMENTS = REL_BASE + "comments"
REL_HYPERLINK = REL_BASE + "hyperlink"

CT_COMMENTS = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"

MAX_UNCOMPRESSED = 512 * 1024 * 1024  # refuse absurd archives (zip bombs)

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True, remove_blank_text=False)


def w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


def qn(prefix_tag: str) -> str:
    prefix, tag = prefix_tag.split(":")
    ns = {"w": W_NS, "r": R_NS, "w14": W14_NS, "w15": W15_NS, "mc": MC_NS, "xml": XML_NS, "m": M_NS}[prefix]
    return f"{{{ns}}}{tag}"


def parse_xml(data: bytes) -> etree._Element:
    return etree.fromstring(data, _PARSER)


class DocxError(Exception):
    pass


class DocxPackage:
    def __init__(self, files: dict[str, bytes], order: list[str]):
        self._files = files
        self._order = order
        self._roots: dict[str, etree._Element] = {}

    # Loading / saving ------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "DocxPackage":
        return cls.from_bytes(Path(path).read_bytes())

    @classmethod
    def from_bytes(cls, data: bytes) -> "DocxPackage":
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise DocxError("not a .docx (zip) file") from exc
        total = sum(i.file_size for i in zf.infolist())
        if total > MAX_UNCOMPRESSED:
            raise DocxError("archive too large")
        files: dict[str, bytes] = {}
        order: list[str] = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            files[info.filename] = zf.read(info)
            order.append(info.filename)
        if "[Content_Types].xml" not in files:
            raise DocxError("missing [Content_Types].xml")
        return cls(files, order)

    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        names = list(self._order)
        if "[Content_Types].xml" in names:
            names.remove("[Content_Types].xml")
            names.insert(0, "[Content_Types].xml")
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in names:
                zf.writestr(name, self.read(name))
        return buf.getvalue()

    def save(self, path: str | Path) -> None:
        Path(path).write_bytes(self.to_bytes())

    def copy(self) -> "DocxPackage":
        return DocxPackage.from_bytes(self.to_bytes())

    # Part access -------------------------------------------------------------
    @property
    def names(self) -> list[str]:
        return list(self._order)

    def has(self, name: str) -> bool:
        return name in self._files

    def read(self, name: str) -> bytes:
        if name in self._roots:
            return etree.tostring(self._roots[name], xml_declaration=True, encoding="UTF-8", standalone=True)
        return self._files[name]

    def xml(self, name: str) -> etree._Element:
        if name not in self._roots:
            if name not in self._files:
                raise KeyError(name)
            self._roots[name] = parse_xml(self._files[name])
        return self._roots[name]

    def set_xml(self, name: str, root: etree._Element) -> None:
        if name not in self._files:
            self._order.append(name)
        self._files[name] = b""
        self._roots[name] = root

    def write(self, name: str, data: bytes) -> None:
        if name not in self._files:
            self._order.append(name)
        self._files[name] = data
        self._roots.pop(name, None)

    def is_xml(self, name: str) -> bool:
        return name.endswith(".xml") or name.endswith(".rels")

    # Content types -----------------------------------------------------------
    def _ct_root(self) -> etree._Element:
        return self.xml("[Content_Types].xml")

    def content_type(self, name: str) -> str | None:
        root = self._ct_root()
        for el in root.findall(f"{{{CT_NS}}}Override"):
            if el.get("PartName", "").lstrip("/") == name:
                return el.get("ContentType")
        ext = name.rsplit(".", 1)[-1].lower()
        for el in root.findall(f"{{{CT_NS}}}Default"):
            if el.get("Extension", "").lower() == ext:
                return el.get("ContentType")
        return None

    def set_override(self, name: str, content_type: str) -> None:
        root = self._ct_root()
        for el in root.findall(f"{{{CT_NS}}}Override"):
            if el.get("PartName", "").lstrip("/") == name:
                el.set("ContentType", content_type)
                return
        el = etree.SubElement(root, f"{{{CT_NS}}}Override")
        el.set("PartName", "/" + name)
        el.set("ContentType", content_type)

    def _remove_override(self, name: str) -> None:
        root = self._ct_root()
        for el in root.findall(f"{{{CT_NS}}}Override"):
            if el.get("PartName", "").lstrip("/") == name:
                root.remove(el)

    # Relationships -----------------------------------------------------------
    @staticmethod
    def rels_name(part: str) -> str:
        directory, base = posixpath.split(part)
        return posixpath.join(directory, "_rels", base + ".rels")

    @staticmethod
    def source_of_rels(rels_name: str) -> str:
        directory, base = posixpath.split(rels_name)
        parent = posixpath.dirname(directory)
        return posixpath.join(parent, base[: -len(".rels")])

    def relationships(self, part: str) -> list[etree._Element]:
        name = self.rels_name(part)
        if not self.has(name):
            return []
        return list(self.xml(name).findall(f"{{{PKG_REL_NS}}}Relationship"))

    def resolve(self, part: str, target: str) -> str:
        if target.startswith("/"):
            return target.lstrip("/")
        return posixpath.normpath(posixpath.join(posixpath.dirname(part), target))

    def related_parts(self, part: str, rel_type: str) -> list[str]:
        out = []
        for rel in self.relationships(part):
            if rel.get("Type") == rel_type and rel.get("TargetMode") != "External":
                out.append(self.resolve(part, rel.get("Target", "")))
        return out

    def add_relationship(self, part: str, rel_type: str, target: str, external: bool = False) -> str:
        name = self.rels_name(part)
        if not self.has(name):
            root = etree.Element(f"{{{PKG_REL_NS}}}Relationships", nsmap={None: PKG_REL_NS})
            self.set_xml(name, root)
        root = self.xml(name)
        used = {rel.get("Id") for rel in root}
        n = 1
        while f"rId{n}" in used:
            n += 1
        rid = f"rId{n}"
        el = etree.SubElement(root, f"{{{PKG_REL_NS}}}Relationship")
        el.set("Id", rid)
        el.set("Type", rel_type)
        el.set("Target", target)
        if external:
            el.set("TargetMode", "External")
        return rid

    def remove_relationship(self, part: str, rid: str) -> None:
        name = self.rels_name(part)
        if not self.has(name):
            return
        root = self.xml(name)
        for rel in list(root):
            if rel.get("Id") == rid:
                root.remove(rel)

    def all_rels_files(self) -> list[str]:
        return [n for n in self._order if n.endswith(".rels")]

    # Adding / removing parts -------------------------------------------------
    def add_part(self, name: str, root: etree._Element, content_type: str) -> None:
        self.set_xml(name, root)
        self.set_override(name, content_type)

    def remove_part(self, name: str) -> None:
        """Remove a part, its own rels, its content-type override and every
        relationship pointing to it."""
        if name not in self._files:
            return
        for rels in self.all_rels_files():
            src = self.source_of_rels(rels)
            root = self.xml(rels)
            for rel in list(root):
                if rel.get("TargetMode") == "External":
                    continue
                if self.resolve(src if src != "" else "", rel.get("Target", "")) == name:
                    root.remove(rel)
        own_rels = self.rels_name(name)
        for n in (name, own_rels):
            if n in self._files:
                del self._files[n]
                self._roots.pop(n, None)
                self._order.remove(n)
        self._remove_override(name)

    def remove_parts(self, predicate: Callable[[str], bool]) -> list[str]:
        removed = [n for n in list(self._order) if predicate(n)]
        for n in removed:
            self.remove_part(n)
        return removed

    # Story parts -------------------------------------------------------------
    def main_part(self) -> str:
        for rel in self.relationships(""):
            if rel.get("Type") == REL_OFFICE_DOCUMENT:
                return self.resolve("", rel.get("Target", ""))
        if self.has("word/document.xml"):
            return "word/document.xml"
        raise DocxError("no main document part")

    def story_parts(self) -> list[tuple[str, str]]:
        """(part name, kind) in processing order. kind is one of
        document | footnotes | endnotes | header | footer | comments."""
        main = self.main_part()
        out: list[tuple[str, str]] = [(main, "document")]
        for rel_type, kind in (
            (REL_FOOTNOTES, "footnotes"),
            (REL_ENDNOTES, "endnotes"),
            (REL_HEADER, "header"),
            (REL_FOOTER, "footer"),
            (REL_COMMENTS, "comments"),
        ):
            for part in self.related_parts(main, rel_type):
                if self.has(part) and (part, kind) not in out:
                    out.append((part, kind))
        return out

    def iter_xml_parts(self) -> Iterable[str]:
        for name in self._order:
            if self.is_xml(name):
                yield name
