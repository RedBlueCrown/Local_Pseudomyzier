"""Cover table at the start of the review copy.

It is plain content (not a tracked change) wrapped in the bookmark
ANON_SUMMARY; `anon finalize` removes everything inside that bookmark.
"""

from __future__ import annotations

from lxml import etree

from ..config import Config
from ..docx_io.finalize import SUMMARY_BOOKMARK
from ..docx_io.package import DocxPackage, w
from ..docx_io.revisions import IdAllocator, set_rpr_child
from ..resolve.entities import Entity

ROLE_DE = {"party": "Partei", "affected": "betroffene Person/Firma", "expert_or_doctor": "Arzt/Gutachter/Zeuge",
           "representative": "Vertreter", None: ""}
KIND_DE = {"person": "Person", "org": "Firma", "representative": "Vertreter", "place": "Ort",
           "address": "Adresse", "plot": "Grundstück", "birthdate": "Geburtsdatum", "id": "Kennung"}
DECISION_DE = {"auto": "Änderung", "review": "Prüfen (Kommentar)", "keep": "beibehalten"}
COLOUR_DE = {"yellow": "gelb", "cyan": "türkis", "green": "grün", "magenta": "magenta", "lightGray": "hellgrau",
             "blue": "blau", "red": "rot", "darkYellow": "dunkelgelb"}


def _run(text: str, bold: bool = False, size: int | None = None, highlight: str | None = None) -> etree._Element:
    r = etree.Element(w("r"))
    rpr = etree.SubElement(r, w("rPr"))
    if bold:
        set_rpr_child(rpr, "b", {})
    if size:
        set_rpr_child(rpr, "sz", {"val": str(size)})
    if highlight:
        set_rpr_child(rpr, "highlight", {"val": highlight})
    if not len(rpr):
        r.remove(rpr)
    t = etree.SubElement(r, w("t"))
    t.text = text
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return r


def _para(*runs: etree._Element) -> etree._Element:
    p = etree.Element(w("p"))
    for r in runs:
        p.append(r)
    return p


def _cell(text: str, width: int, bold: bool = False, shade: str | None = None) -> etree._Element:
    tc = etree.Element(w("tc"))
    tcpr = etree.SubElement(tc, w("tcPr"))
    tcw = etree.SubElement(tcpr, w("tcW"))
    tcw.set(w("w"), str(width))
    tcw.set(w("type"), "dxa")
    if shade:
        shd = etree.SubElement(tcpr, w("shd"))
        shd.set(w("val"), "clear")
        shd.set(w("color"), "auto")
        shd.set(w("fill"), shade)
    for line in text.split("\n") or [""]:
        tc.append(_para(_run(line, bold=bold, size=18)))
    return tc


def _table(rows: list[list[str]], widths: list[int]) -> etree._Element:
    tbl = etree.Element(w("tbl"))
    tblpr = etree.SubElement(tbl, w("tblPr"))
    tblw = etree.SubElement(tblpr, w("tblW"))
    tblw.set(w("w"), "0")
    tblw.set(w("type"), "auto")
    borders = etree.SubElement(tblpr, w("tblBorders"))
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        b = etree.SubElement(borders, w(side))
        b.set(w("val"), "single")
        b.set(w("sz"), "4")
        b.set(w("space"), "0")
        b.set(w("color"), "999999")
    grid = etree.SubElement(tbl, w("tblGrid"))
    for wd in widths:
        g = etree.SubElement(grid, w("gridCol"))
        g.set(w("w"), str(wd))
    for i, row in enumerate(rows):
        tr = etree.SubElement(tbl, w("tr"))
        if i == 0:
            trpr = etree.SubElement(tr, w("trPr"))
            etree.SubElement(trpr, w("tblHeader"))
        for text, wd in zip(row, widths):
            tr.append(_cell(text, wd, bold=(i == 0), shade="E7E6E6" if i == 0 else None))
    return tbl


def insert_summary(pkg: DocxPackage, entities: list[Entity], cfg: Config, notes: list[str],
                   ids: IdAllocator) -> None:
    body = pkg.xml(pkg.main_part()).find(w("body"))
    bid = str(ids.next())
    start = etree.Element(w("bookmarkStart"))
    start.set(w("id"), bid)
    start.set(w("name"), SUMMARY_BOOKMARK)
    end = etree.Element(w("bookmarkEnd"))
    end.set(w("id"), bid)

    blocks: list[etree._Element] = []
    title = _para(_run("Anonymisierung – Prüfübersicht (wird beim Finalisieren automatisch entfernt)", bold=True,
                       size=26))
    title.insert(0, start)
    blocks.append(title)
    legend = [_run("Änderungen sind Überarbeitungen; Autor = Kategorie: ", size=18)]
    for cat in ("party", "representative", "place", "plot", "id"):
        c = cfg.categories[cat]
        legend.append(_run(f" {c['author']} ", size=18, highlight=c.get("highlight")))
        legend.append(_run(" ", size=18))
    legend.append(_run(f"   Unsichere Stellen: Kommentare von „{cfg.author('review')}“.", size=18))
    blocks.append(_para(*legend))
    blocks.append(_para(_run(
        "Prüfen: Änderung annehmen/ablehnen. Kommentar „Prüfen“: im Text ersetzen oder Kommentar löschen/erledigen. "
        "Grössere Korrekturen in der Datei *.entities.xlsx vornehmen und die Prüfkopie neu erzeugen.", size=18)))
    rows = [["ID", "Original (Formen)", "Art / Rolle", "Ersetzung", "Vorkommen", "Quelle", "Entscheidung"]]
    for e in entities:
        changes = sum(1 for m in e.mentions if m.change)
        forms = e.forms[:4] + (["…"] if len(e.forms) > 4 else [])
        kind = KIND_DE.get(e.kind, e.kind)
        role = ROLE_DE.get(e.role, e.role or "")
        rows.append([
            e.id, "\n".join(forms), f"{kind}\n{role}" if role and e.kind in ("person", "org") else kind,
            e.replacement if e.decision != "keep" else "—",
            f"{len(e.mentions)} ({changes} geändert)", ", ".join(sorted(e.sources)),
            DECISION_DE.get(e.decision, e.decision),
        ])
    blocks.append(_table(rows, [700, 3000, 1600, 1000, 1200, 1300, 1300]))
    if notes:
        blocks.append(_para(_run("Hinweise:", bold=True, size=18)))
        for n in notes:
            blocks.append(_para(_run("• " + n, size=18)))
    last = _para()
    br = etree.SubElement(etree.SubElement(last, w("r")), w("br"))
    br.set(w("type"), "page")
    last.append(end)
    # point bookmark: survives editors that collapse a range spanning a table
    end_id = str(ids.next())
    mark = etree.SubElement(last, w("bookmarkStart"))
    mark.set(w("id"), end_id)
    mark.set(w("name"), SUMMARY_BOOKMARK + "_END")
    mark_end = etree.SubElement(last, w("bookmarkEnd"))
    mark_end.set(w("id"), end_id)
    blocks.append(last)
    for i, el in enumerate(blocks):
        body.insert(i, el)
