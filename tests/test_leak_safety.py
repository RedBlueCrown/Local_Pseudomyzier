"""Regression tests for leak paths found in an independent review.

Every test describes a way an original value could have reached the clean
export without the leak check failing."""

from __future__ import annotations

import io
import zipfile

import pytest
from docx_factory import make_docx
from lxml import etree

from anon.config import load_config
from anon.docx_io.finalize import remove_comments
from anon.docx_io.package import DocxError, DocxPackage, w
from anon.docx_io.reader import read_document
from anon.pipeline import analyse, finalize, review
from anon.review.entities_file import read_entities

RUBRUM = "Beschwerdeführer:\tAlbert Kaiser, Dorfstrasse 5, 9490 Vaduz"


def _review(tmp_path, data: bytes, cfg=None):
    cfg = cfg or load_config()
    src = tmp_path / "in.docx"
    src.write_bytes(data)
    analysis, docx, xlsx = review(src, cfg, tmp_path / "out", use_ner=False)
    return analysis, DocxPackage.load(docx), read_entities(xlsx), cfg


def _blob(data: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return b"".join(zf.read(n) for n in zf.namelist()).lower()


def _unwrap_del(d):
    for t in d.iter(w("delText")):
        t.tag = w("t")
    parent = d.getparent()
    idx = parent.index(d)
    for c in list(d):
        parent.insert(idx, c)
        idx += 1
    parent.remove(d)


def test_rejecting_only_the_deletion_blocks(tmp_path):
    _, pkg, rows, cfg = _review(tmp_path, make_docx(body=[RUBRUM, "I. Sachverhalt", "Tel. +423 232 12 34."]))
    root = pkg.xml("word/document.xml")
    for d in list(root.iter(w("del"))):
        if d.get(w("author")) == "Datum/ID":
            _unwrap_del(d)  # reviewer rejected the deletion but kept the "***" insertion
    result = finalize(pkg.to_bytes(), rows, cfg)
    assert result.data is None
    assert any("Löschung abgelehnt" in p for p in result.report.problems)


def test_editing_the_inserted_text_blocks(tmp_path):
    _, pkg, rows, cfg = _review(tmp_path, make_docx(body=[RUBRUM, "I. Sachverhalt", "Herr Albert Kaiser kam."]))
    root = pkg.xml("word/document.xml")
    ins = [i for i in root.iter(w("ins")) if i.get(w("author")) == "Partei"][-1]
    ins.find(w("r")).find(w("t")).text = "A (Albert Kaiser)"  # typed with tracking off
    result = finalize(pkg.to_bytes(), rows, cfg)
    assert result.data is None
    assert any("verändert" in p for p in result.report.problems)


@pytest.mark.parametrize("planted", [" Kaiser", " Kaisers", " KAISER", " Albert Kaiser"])
def test_bare_surname_and_variants_are_leak_terms(tmp_path, planted):
    _, pkg, rows, cfg = _review(tmp_path, make_docx(body=[RUBRUM, "I. Sachverhalt", ["Text", {"footnote": 1}]],
                                                    footnotes={1: ["Fussnote."]}))
    t = list(pkg.xml("word/footnotes.xml").iter(w("t")))[-1]
    t.text += planted
    assert finalize(pkg.to_bytes(), rows, cfg).data is None


def test_dashes_do_not_hide_names(tmp_path):
    body = [RUBRUM, "I. Sachverhalt", "Der Antrag—so Albert Kaiser—sei dringend; Strecke Buchs–Vaduz."]
    _, pkg, rows, cfg = _review(tmp_path, make_docx(body=body))
    remove_comments(pkg, None)
    result = finalize(pkg.to_bytes(), rows, cfg)
    assert result.data is not None
    assert b"kaiser" not in _blob(result.data) and b"buchs" not in _blob(result.data)


def test_decomposed_umlauts_are_composed(tmp_path):
    body = ["Beschwerdeführer:\tHans Müller, Dorfstrasse 5, 9490 Vaduz", "I. Sachverhalt",
            "Hans Müller beantragte; Eingabe von Müller."]
    analysis, pkg, rows, cfg = _review(tmp_path, make_docx(body=body))
    result = finalize(pkg.to_bytes(), rows, cfg)
    assert result.data is not None
    text = "\n".join(p.text for p in read_document(DocxPackage.from_bytes(result.data)).paragraphs)
    assert "A beantragte; Eingabe von A." in text and "̈" not in text


def _raw_paragraph(pkg: DocxPackage, xml: str) -> None:
    body = pkg.xml("word/document.xml").find(w("body"))
    ns = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
          'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"')
    body.insert(len(body) - 1, etree.fromstring(f"<w:p {ns}>{xml}</w:p>"))


@pytest.mark.parametrize("xml", [
    '<m:oMath><m:r><m:t>Albert Kaiser</m:t></m:r></m:oMath>',
    '<w:r><w:ruby><w:rubyPr/><w:rt><w:r><w:t>Albert Kaiser</w:t></w:r></w:rt>'
    '<w:rubyBase><w:r><w:t>X</w:t></w:r></w:rubyBase></w:ruby></w:r>',
])
def test_math_and_ruby_text_are_checked(tmp_path, xml):
    _, pkg, rows, cfg = _review(tmp_path, make_docx(body=[RUBRUM, "I. Sachverhalt"]))
    _raw_paragraph(pkg, xml)
    assert finalize(pkg.to_bytes(), rows, cfg).data is None


def test_tooltips_tel_targets_and_date_controls_are_cleaned(tmp_path):
    body = [RUBRUM, "I. Sachverhalt", "Tel. +423 232 12 34, geboren am 10.10.1990.",
            ["Mail an ", {"hyperlink": "mailto:a.kaiser@example.li", "text": "ihn"}]]
    _, pkg, rows, cfg = _review(tmp_path, make_docx(body=body))
    _raw_paragraph(pkg, '<w:hyperlink w:anchor="x" w:tooltip="+423 232 12 34"><w:r><w:t>Anruf</w:t></w:r></w:hyperlink>'
                        '<w:sdt><w:sdtPr><w:date w:fullDate="1990-10-10T00:00:00Z"/></w:sdtPr>'
                        '<w:sdtContent><w:r><w:t>Datum</w:t></w:r></w:sdtContent></w:sdt>')
    remove_comments(pkg, None)
    result = finalize(pkg.to_bytes(), rows, cfg)
    assert result.data is not None, result.report.summary_lines()
    blob = _blob(result.data)
    assert b"232 12 34" not in blob and b"a.kaiser" not in blob and b"1990-10-10" not in blob


def test_heuristic_citation_does_not_whitelist_a_party(tmp_path):
    body = [RUBRUM, "I. Sachverhalt", "Kaiser beantragte. So Kaiser, die Behörde, 2019, Rz 3. Vgl. Kaiser/Huber, in: X."]
    analysis, pkg, rows, cfg = _review(tmp_path, make_docx(body=body))
    assert finalize(pkg.to_bytes(), rows, cfg).data is None  # open questions, not silent keeps
    remove_comments(pkg, None)  # reviewer confirms: citations
    assert finalize(pkg.to_bytes(), rows, cfg).data is not None


def test_president_of_a_party_is_not_a_judge(tmp_path):
    body = ["Der Staatsgerichtshof hat unter dem Vorsitz von Präsident Dr. Hugo Vogt in der Beschwerdesache",
            "Beschwerdeführer:\tTurnverein Musterdorf, vertreten durch seinen Präsidenten Albert Kaiser",
            "I. Sachverhalt", "Albert Kaiser bestätigte dies."]
    analysis = analyse(make_docx(body=body), load_config(), use_ner=False)
    assert "Albert Kaiser" not in {p.name for p in analysis.keep_people}
    assert any(m.span.text == "Albert Kaiser" and m.change for m in analysis.mentions)


def test_numbered_party_and_particle_forms(tmp_path):
    body = ["Beschwerdeführer:\t1. Albert von Kaiser, Dorfstrasse 5, 9490 Vaduz", "I. Sachverhalt",
            "Herr Kaiser beantragte; Albert von Kaiser bestätigte."]
    analysis = analyse(make_docx(body=body), load_config(), use_ner=False)
    persons = [e for e in analysis.entities if e.kind == "person"]
    assert len(persons) == 1 and not any(f.startswith("1.") for f in persons[0].forms)


def test_uncertain_header_item_is_a_pruefen_change(tmp_path):
    body = ["Beschwerdeführer:\t1. Albert Kaiser, Dorfstrasse 5, 9490 Vaduz",
            "\t2. Berta Kaiser, Dorfstrasse 5, 9490 Vaduz", "I. Sachverhalt"]
    _, pkg, rows, cfg = _review(tmp_path, make_docx(body=body, header=["Kaiser gegen Regierung"]))
    authors = {i.get(w("author")) for i in pkg.xml("word/header1.xml").iter(w("ins"))}
    assert authors == {"Prüfen"}


def test_no_tool_comments_inside_existing_comments(tmp_path):
    analysis, pkg, rows, cfg = _review(tmp_path, make_docx(body=[RUBRUM, "I. Sachverhalt", "Die Planken liegen."]))
    from anon.docx_io.validate import validate

    assert validate(pkg) == []


def test_strict_ooxml_gives_a_clear_error():
    pkg = DocxPackage.from_bytes(make_docx(body=["x"]))
    xml = pkg.read("word/document.xml").replace(b"http://schemas.openxmlformats.org/wordprocessingml/2006/main",
                                                 b"http://purl.oclc.org/ooxml/wordprocessingml/main")
    pkg.write("word/document.xml", xml)
    with pytest.raises(DocxError, match="Strict"):
        analyse(pkg.to_bytes(), load_config(), use_ner=False)
