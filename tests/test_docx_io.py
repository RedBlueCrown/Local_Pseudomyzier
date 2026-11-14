"""Reader / revision writer / accept-reject round trips on raw OOXML."""

from docx_factory import make_docx

from anon.docx_io.finalize import accept_all_parts, reject_all_parts
from anon.docx_io.package import DocxPackage, w
from anon.docx_io.reader import OBJECT, read_document
from anon.docx_io.revisions import Edit, RevisionWriter


def _texts(pkg):
    return {p.pid: p.text for p in read_document(pkg).paragraphs}


def _sample():
    return make_docx(
        body=[
            ["Beschwerdeführer:\t", ("Albert ", {"b": True}), ("Kai", {"b": True}), ("ser", {"i": True}),
             ", Dorfstrasse 5\n9490 Vaduz"],
            ["Der Text", {"footnote": 1}, " folgt."],
            {"table": [["Kläger", "Berta Muster"]]},
            ["Box: ", {"textbox": ["Im Kasten steht Albert Kaiser."]}],
            [{"hyperlink": "mailto:a.kaiser@example.li", "text": "a.kaiser@example.li"}],
        ],
        header=["Kopf Albert Kaiser"],
        footer=[["Fuss ", {"field": "PAGE", "result": "1"}]],
        footnotes={1: ["Fussnote zu Albert Kaiser."]},
    )


def test_reader_text_and_parts():
    pkg = DocxPackage.from_bytes(_sample())
    doc = read_document(pkg)
    kinds = {p.kind for p in doc.paragraphs}
    assert {"document", "footnotes", "header", "footer"} <= kinds
    texts = [p.text for p in doc.paragraphs]
    assert "Beschwerdeführer:\tAlbert Kaiser, Dorfstrasse 5\n9490 Vaduz" in texts
    assert f"Der Text{OBJECT} folgt." in texts
    assert "Im Kasten steht Albert Kaiser." in texts
    assert f"Fuss {OBJECT}{OBJECT}1{OBJECT}" in texts  # begin/separate/end are objects, instrText hidden
    fn = next(p for p in doc.paragraphs if p.kind == "footnotes" and "Fussnote" in p.text)
    main = next(p for p in doc.paragraphs if "Der Text" in p.text)
    assert fn.anchor[0] == main.index


def test_normalisation_maps_back():
    pkg = DocxPackage.from_bytes(make_docx(body=["Gebiet „Riet“ und Nr. 4716–Par­zelle"]))
    p = read_document(pkg).paragraphs[0]
    assert p.norm == 'Gebiet "Riet" und Nr. 4716\u2013Parzelle'
    ns = p.norm.index("Riet")
    s, e = p.to_orig(ns, ns + 4)
    assert p.text[s:e] == "Riet"
    ns = p.norm.index("Parzelle")
    s, e = p.to_orig(ns, ns + len("Parzelle"))
    assert p.text[s:e] == "Par­zelle"


def _apply(pkg, needle_edits):
    doc = read_document(pkg)
    edits = []
    n = 0
    for para in doc.paragraphs:
        for needle, repl, author in needle_edits:
            start = para.text.find(needle)
            while start >= 0:
                n += 1
                edits.append(Edit(para.part, para.index, start, start + len(needle), repl, author, "yellow", n))
                start = para.text.find(needle, start + len(needle))
    # drop overlaps (simple)
    edits.sort(key=lambda e: (e.part, e.para, e.start, -(e.end - e.start)))
    clean, last = [], {}
    for e in edits:
        if e.start >= last.get((e.part, e.para), -1):
            clean.append(e)
            last[(e.part, e.para)] = e.end
    writer = RevisionWriter(doc)
    writer.apply(clean)
    return writer


def test_reject_all_restores_original_and_accept_all_replaces():
    original = DocxPackage.from_bytes(_sample())
    before = _texts(original)
    review = DocxPackage.from_bytes(_sample())
    writer = _apply(review, [("Albert Kaiser", "A", "Partei"), ("Kaiser", "A", "Partei"),
                             (", Dorfstrasse 5\n9490 Vaduz", "", "Ort"), ("a.kaiser@example.li", "***", "Datum/ID")])
    assert not writer.skipped
    review = DocxPackage.from_bytes(review.to_bytes())  # serialise round trip
    # all deleted text sits in w:delText, never in w:t
    for part, _ in review.story_parts():
        for d in review.xml(part).iter(w("del")):
            assert not list(d.iter(w("t")))
    rejected = DocxPackage.from_bytes(review.to_bytes())
    reject_all_parts(rejected)
    assert _texts(rejected) == before
    accepted = DocxPackage.from_bytes(review.to_bytes())
    accept_all_parts(accepted)
    after = _texts(accepted)
    assert "Beschwerdeführer:\tA" in after.values()
    assert "Kopf A" in after.values()
    assert f"{OBJECT} Fussnote zu A." in after.values()
    assert "Im Kasten steht A." in after.values()
    assert "***" in after.values()


def test_split_keeps_formatting_and_highlight_order():
    pkg = DocxPackage.from_bytes(make_docx(body=[[("Herr Albert Kaiser kam", {"b": True, "u": True, "color": "FF0000"})]]))
    _apply(pkg, [("Kaiser", "A", "Partei")])
    root = pkg.xml("word/document.xml")
    ins = next(root.iter(w("ins")))
    rpr = ins.find(w("r")).find(w("rPr"))
    tags = [c.tag.split("}")[1] for c in rpr]
    assert tags == ["b", "color", "highlight", "u"]
    deleted = next(root.iter(w("del")))
    assert deleted.find(w("r")).find(w("rPr")).find(w("b")) is not None
    assert ins.get(w("author")) == "Partei"


def test_ids_unique_across_package():
    pkg = DocxPackage.from_bytes(_sample())
    _apply(pkg, [("Kaiser", "A", "Partei")])
    seen = []
    for part, _ in pkg.story_parts():
        for el in pkg.xml(part).iter(w("ins"), w("del"), w("bookmarkStart")):
            seen.append(int(el.get(w("id"))))
    assert len(seen) == len(set(seen))


def test_object_inside_span_becomes_comment():
    pkg = DocxPackage.from_bytes(make_docx(body=[["Albert", {"footnote": 1}, " Kaiser"]], footnotes={1: ["x"]}))
    doc = read_document(pkg)
    p = doc.paragraphs[0]
    writer = RevisionWriter(doc)
    writer.apply([Edit(p.part, p.index, 0, len(p.text), "A", "Partei", "yellow", 1)])
    root = pkg.xml("word/document.xml")
    assert not list(root.iter(w("del")))
    assert list(root.iter(w("commentRangeStart")))
    assert pkg.has("word/comments.xml")


def test_normalisation_nfc_and_separators():
    pkg = DocxPackage.from_bytes(make_docx(body=["Hans Mu\u0308ller\u2014so Albert\u200bKaiser"]))
    p = read_document(pkg).paragraphs[0]
    assert p.norm == "Hans M\u00fcller\u2013so Albert Kaiser"
    ns = p.norm.index("M\u00fcller")
    s, e = p.to_orig(ns, ns + len("M\u00fcller"))
    assert p.text[s:e] == "Mu\u0308ller"           # the combining mark is inside the span


def test_unpreserved_whitespace_is_not_text():
    from lxml import etree

    pkg = DocxPackage.from_bytes(make_docx(body=["x"]))
    root = pkg.xml("word/document.xml")
    p = next(root.iter(w("p")))
    for r in list(p):
        if r.tag == w("r"):
            p.remove(r)
    for text, preserve in (("Herr Albert Kaiser ", False), ("kam.", False)):
        r = etree.SubElement(p, w("r"))
        t = etree.SubElement(r, w("t"))
        t.text = text
    assert read_document(pkg).paragraphs[0].text == "Herr Albert Kaiserkam."
