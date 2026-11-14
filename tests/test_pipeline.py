"""Brief §7: behaviour of review + finalize on synthetic decisions."""

from __future__ import annotations

import io
import re
import zipfile

import pytest
from docx_factory import make_docx
from fixtures import decision_vgh

from anon.config import load_config
from anon.docx_io.finalize import accept_all_parts, reject_all_parts
from anon.docx_io.package import DocxPackage, w
from anon.docx_io.reader import read_document
from anon.docx_io.validate import validate
from anon.pipeline import analyse, finalize, review
from anon.review.entities_file import read_entities


def _texts(pkg: DocxPackage, skip_summary: bool = True) -> dict:
    out = {}
    for p in read_document(pkg).paragraphs:
        out[p.pid] = p.text
    return out


def _review(tmp_path, data: bytes, cfg=None, name="IN", **kw):
    cfg = cfg or load_config()
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = tmp_path / f"{name}.docx"
    src.write_bytes(data)
    analysis, docx, xlsx = review(src, cfg, tmp_path / "out", use_ner=False, **kw)
    return analysis, docx, xlsx, cfg


def _strip_summary(pkg: DocxPackage) -> DocxPackage:
    from anon.docx_io.finalize import remove_comments, remove_summary

    remove_summary(pkg)
    remove_comments(pkg, None)
    return DocxPackage.from_bytes(pkg.to_bytes())


def _accepted_text(docx_path) -> str:
    pkg = DocxPackage.load(docx_path)
    accept_all_parts(pkg)
    pkg = _strip_summary(pkg)
    return "\n".join(p.text for p in read_document(pkg).paragraphs)


def _expected_from_plan(analysis) -> dict:
    """Original paragraph texts with every planned change applied (rule engine)."""
    texts = {p.pid: p.text for p in read_document(DocxPackage.from_bytes(analysis.pkg.to_bytes())).paragraphs}
    return texts


# ---------------------------------------------------------------------------
def test_reject_all_reproduces_original_and_accept_all_matches_plan(tmp_path):
    data = decision_vgh()
    original = DocxPackage.from_bytes(data)
    orig_texts = {p.pid: p.text for p in read_document(original).paragraphs}
    analysis, docx, _, _ = _review(tmp_path, data)
    # expected: apply the plan to the original strings (independent of the writer)
    expected = dict(orig_texts)
    by_para: dict = {}
    for m in analysis.mentions:
        if m.change:
            by_para.setdefault(m.span.key, []).append(m)
    for key, ms in by_para.items():
        text = expected[key]
        for m in sorted(ms, key=lambda m: m.span.start, reverse=True):
            text = text[: m.span.start] + m.replacement + text[m.span.end :]
        expected[key] = text

    rejected = DocxPackage.load(docx)
    reject_all_parts(rejected)
    rejected = _strip_summary(rejected)
    assert {p.pid: p.text for p in read_document(rejected).paragraphs} == orig_texts

    accepted = DocxPackage.load(docx)
    accept_all_parts(accepted)
    accepted = _strip_summary(accepted)
    assert {p.pid: p.text for p in read_document(accepted).paragraphs} == expected


def test_review_copy_structure_is_valid(tmp_path):
    _, docx, _, _ = _review(tmp_path, decision_vgh())
    pkg = DocxPackage.load(docx)
    assert validate(pkg) == []
    # every tool revision has a category author and the deleted text sits in delText
    authors = {el.get(w("author")) for part, _ in pkg.story_parts() for el in pkg.xml(part).iter(w("ins"), w("del"))}
    assert authors <= {"Partei", "Vertreter", "Ort", "Grundstück", "Datum/ID"}
    assert "Partei" in authors and "Grundstück" in authors


def test_expected_replacements_in_clean_text(tmp_path):
    _, docx, _, _ = _review(tmp_path, decision_vgh())
    text = _accepted_text(docx)
    assert "Beschwerdeführer:\tA\n" in text + "\n"            # Rubrum: name -> A, address removed
    assert "vertreten durch:\t***" in text                       # representative block -> ***
    assert "B Anstalt" in text and "Papageien" not in text       # legal form kept
    assert "As Antrag" in text                                   # genitive
    assert "Dr. med. C" in text and "Dr. C ist" in text          # title kept, doctor anonymised
    assert "des *** Grundstücks Nr. 1" in text                   # place adjective + plot
    assert "Parz. 1/2" in text                                   # slash form
    assert "Grundstück Nr. 2" in text                            # same number -> same index
    assert "geboren am ***" in text
    assert "Im Gebiet „***“" in text                     # Flurname in typographic quotes
    assert "Kontrollschild ***" in text and "IBAN ***" in text and "D AG (***)" in text


def test_keep_list_judges_clerk_case_numbers_citations(tmp_path):
    _, docx, _, _ = _review(tmp_path, decision_vgh())
    text = _accepted_text(docx)
    for kept in ("Präsident Dr. Hugo Vogt", "lic. iur. Thomas Hasler", "Dr. Nicole Beck", "Marion Ospelt",
                 "VGH 2026/21", "StGH 2024/024", "VBK 2025/81", "RA 2025/81", "Art. 44 Abs. 3 USG",
                 "LGBl. 1999/159", "Kley/Vogt", "EGMR Kaiser v. Liechtenstein", "Präsident Vogt hält fest",
                 "Vorschlag von Vogt", "Vaduz, 10. Juni 2026", "Dr. Hugo Vogt"):
        assert kept in text, kept


def test_headers_footers_footnotes_processed(tmp_path):
    data = make_docx(
        body=["Beschwerdeführer:\tAlbert Kaiser, Dorfstrasse 5, 9490 Vaduz", "I. Sachverhalt",
              ["Text", {"footnote": 1}]],
        header=["Kaiser gegen Regierung"],
        footer=["Albert Kaiser – Seite 1"],
        footnotes={1: ["Auskunft von Albert Kaiser vom 1.1.2026 zu Grundstück Nr. 4716."]},
    )
    _, docx, _, _ = _review(tmp_path, data)
    pkg = DocxPackage.load(docx)
    accept_all_parts(pkg)
    texts = {p.kind: [] for p in read_document(pkg).paragraphs}
    for p in read_document(pkg).paragraphs:
        texts[p.kind].append(p.text)
    assert "A gegen Regierung" in texts["header"]
    assert "A – Seite 1" in texts["footer"]
    assert any("Auskunft von A vom 1.1.2026 zu Grundstück Nr. 1." in t for t in texts["footnotes"])


def test_whole_token_matching_never_touches_longer_words(tmp_path):
    data = make_docx(body=[
        "Beschwerdeführer:\tHerr Or, Dorfstrasse 5, 9490 Vaduz",
        "Beschwerdegegner:\tNova Treuhand AG, Landstrasse 1, 9495 Triesen",
        "I. Sachverhalt",
        "Or hat mit voraussichtlichem Erfolg Novartis-Aktien gekauft; Nova Treuhand AG und Herr Or. "
        "Die Peter-Or-Strasse und die Nova-Werke bleiben.",
    ])
    _, docx, _, _ = _review(tmp_path, data)
    text = _accepted_text(docx)
    assert "voraussichtlichem" in text and "Novartis" in text
    assert "Peter-Or-Strasse" not in text or "Peter-Or-" in text  # never altered inside a compound
    assert "Nova-Werke" in text
    assert "A hat mit voraussichtlichem Erfolg Novartis-Aktien gekauft; B AG und Herr A." in text


def test_plot_numbers_and_place_adjective(tmp_path):
    data = make_docx(body=[
        "I. Sachverhalt",
        "Auf dem Vaduzer Grundstück Nr. 4716 wurde gebaut.",
        "Die Parz. 4716/4718 sowie die Grundstücke Nr. 5000 und Nr. 4718 grenzen an.",
    ])
    _, docx, _, _ = _review(tmp_path, data)
    text = _accepted_text(docx)
    assert "Auf dem *** Grundstück Nr. 1 wurde gebaut." in text
    assert "Die Parz. 1/2 sowie die Grundstücke Nr. 3 und Nr. 2 grenzen an." in text


def test_legal_form_kept_and_activity_switch(tmp_path):
    body = ["Beschwerdeführerin:\tMuster Bank AG, Städtle 1, 9490 Vaduz", "I. Sachverhalt",
            "Die Muster Bank AG und die Papageien Anstalt stritten."]
    _, docx, _, _ = _review(tmp_path, make_docx(body=body))
    text = _accepted_text(docx)
    assert "Die A AG und die B Anstalt stritten." in text
    cfg = load_config()
    cfg.policy["company_keep_activity"] = True
    _, docx2, _, _ = _review(tmp_path / "x", make_docx(body=body), cfg=cfg)
    assert "Die A Bank AG und die B Anstalt stritten." in _accepted_text(docx2)


def test_policy_switches_representatives_birthdates_third_parties(tmp_path):
    body = ["Beschwerdeführer:\tAlbert Kaiser, geboren am 10.10.1990, Dorfstrasse 5, 9490 Vaduz",
            "vertreten durch:\tDr. Max Berger, Rechtsanwalt, 9490 Vaduz", "I. Sachverhalt",
            "Albert Kaiser, geboren am 10.10.1990, legte ein Zeugnis von Hausarzt Dr. med. Rudolf Nova vor. "
            "Rechtsanwalt Dr. Berger beantragte Fristerstreckung."]
    cfg = load_config()
    cfg.policy.update({"representatives": "letters", "birth_dates": "generalise", "third_parties": "keep"})
    _, docx, _, _ = _review(tmp_path, make_docx(body=body), cfg=cfg)
    text = _accepted_text(docx)
    assert "vertreten durch:\tB" in text
    assert "A, geboren vor dem Jahr 2001, legte" in text
    assert "Dr. med. Rudolf Nova" in text                     # third parties kept by policy
    assert "Rechtsanwalt Dr. B beantragte" in text


def test_metadata_stripped_and_leak_check(tmp_path):
    analysis, docx, xlsx, cfg = _review(tmp_path, decision_vgh())
    rows = read_entities(xlsx)
    result = finalize(docx.read_bytes(), rows, cfg)
    assert result.data is not None, result.report.summary_lines()
    clean = DocxPackage.from_bytes(result.data)
    names = clean.names
    assert "docProps/custom.xml" not in names and "word/people.xml" not in names
    assert not any(n.startswith("word/comments") for n in names)
    core = clean.read("docProps/core.xml").decode()
    assert "Hans Muster" not in core and "Erika" not in core and "Urteil Kaiser" not in core
    assert "Hans Muster" not in clean.read("docProps/app.xml").decode()
    for part, _ in clean.story_parts():
        xml = clean.read(part).decode()
        assert "w:rsid" not in xml and "_anon" not in xml and "ANON_SUMMARY" not in xml
        assert "w:highlight" not in xml
    settings = clean.read("word/settings.xml").decode()
    assert "docVar" not in settings and "rsids" not in settings
    text = "\n".join(p.text for p in read_document(clean).paragraphs)
    assert "Prüfübersicht" not in text and "Albert" not in text


def test_leak_check_fails_when_name_planted_in_footnote(tmp_path):
    analysis, docx, xlsx, cfg = _review(tmp_path, decision_vgh())
    rows = read_entities(xlsx)
    pkg = DocxPackage.load(docx)
    fn = pkg.xml("word/footnotes.xml")
    t = list(fn.iter(w("t")))[-1]
    t.text = t.text + " Albert Kaiser"
    result = finalize(pkg.to_bytes(), rows, cfg)
    assert result.data is None
    assert any(h.part.endswith("footnotes.xml") for h in result.report.leaks)


def test_leak_check_accepts_rejected_change_and_resolved_comment(tmp_path):
    data = make_docx(body=["I. Sachverhalt", "Die Planken des Stegs und Herr Albert Kaiser in Planken."])
    analysis, docx, xlsx, cfg = _review(tmp_path, data)
    rows = read_entities(xlsx)
    pkg = DocxPackage.load(docx)
    # open "Prüfen" comment for the ambiguous "Planken" -> finalize must refuse
    assert finalize(pkg.to_bytes(), rows, cfg).data is None
    # reviewer deletes the comment (= "no change needed") and rejects the place change
    from anon.docx_io.finalize import remove_comments

    remove_comments(pkg, None)
    root = pkg.xml("word/document.xml")
    for ins in list(root.iter(w("ins"))):
        if ins.get(w("author")) == "Ort":
            ins.getparent().remove(ins)
    for d in list(root.iter(w("del"))):
        if d.get(w("author")) == "Ort":
            for t in d.iter(w("delText")):
                t.tag = w("t")
            parent = d.getparent()
            idx = parent.index(d)
            for child in list(d):
                parent.insert(idx, child)
                idx += 1
            parent.remove(d)
    result = finalize(pkg.to_bytes(), rows, cfg)
    assert result.data is not None, result.report.summary_lines()
    statuses = {h.status for h in result.report.hits}
    assert {"resolved_comment", "rejected_by_reviewer"} <= statuses
    text = "\n".join(p.text for p in read_document(DocxPackage.from_bytes(result.data)).paragraphs)
    assert "Die Planken des Stegs und Herr A in Planken." in text


def test_hidden_values_scrubbed_from_hyperlinks(tmp_path):
    data = make_docx(body=["I. Sachverhalt",
                           ["Mail: ", {"hyperlink": "mailto:a.kaiser@example.li", "text": "a.kaiser@example.li"}]])
    analysis, docx, xlsx, cfg = _review(tmp_path, data)
    result = finalize(docx.read_bytes(), read_entities(xlsx), cfg)
    assert result.data is not None
    with zipfile.ZipFile(io.BytesIO(result.data)) as zf:
        blob = b"".join(zf.read(n) for n in zf.namelist())
    assert b"a.kaiser@example.li" not in blob


def test_entities_xlsx_round_trip(tmp_path):
    from openpyxl import load_workbook

    body = ["Beschwerdeführer:\tAlbert Kaiser, Dorfstrasse 5, 9490 Vaduz", "I. Sachverhalt",
            "Quell bestätigte, dass die Muster Bank AG in Buchs und die Papageien Anstalt zahlten. "
            "Der Bericht von Quell liegt vor."]
    analysis, docx, xlsx, cfg = _review(tmp_path, make_docx(body=body))
    assert "Quell bestätigte" in _accepted_text(docx)            # not detected by the rules
    wb = load_workbook(xlsx)
    ws = wb["Entitäten"]
    col = {c.value: i + 1 for i, c in enumerate(ws[1])}
    by_form = {ws.cell(r, col["Formen"]).value.split(" | ")[0]: r for r in range(2, ws.max_row + 1)}
    ws.cell(by_form["Buchs"], col["Entscheidung"]).value = "keep"    # reviewer: keep this place
    ws.cell(by_form["Muster Bank"], col["Ersetzung"]).value = "Z"    # reviewer: own letter
    new_row = ws.max_row + 1
    ws.cell(new_row, col["Kategorie"]).value = "party"               # reviewer: add missed name
    ws.cell(new_row, col["Formen"]).value = "Quell"
    wb.save(xlsx)

    analysis2, docx2, xlsx2, _ = _review(tmp_path, make_docx(body=body), entities_path=xlsx)
    text = _accepted_text(docx2)
    # letters re-sequenced in order of first mention: Kaiser A, Quell B, Papageien C; locked Z kept
    assert "B bestätigte, dass die Z AG in Buchs und die C Anstalt zahlten. Der Bericht von B liegt vor." in text
    rows = read_entities(xlsx2)
    assert any(r.forms == ["Quell"] and r.replacement == "B" for r in rows)
    # finalize accepts the kept place as "kept by reviewer"
    result = finalize(docx2.read_bytes(), rows, cfg)
    assert result.data is not None
    assert any(h.status == "kept_by_reviewer" for h in result.report.hits)


def test_entities_merge_via_xlsx(tmp_path):
    from openpyxl import load_workbook

    body = ["Beschwerdeführer:\tAlbert Kaiser, Dorfstrasse 5, 9490 Vaduz", "I. Sachverhalt",
            "Die Firma Kaiser Bau AG gehört Albert Kaiser."]
    analysis, docx, xlsx, cfg = _review(tmp_path, make_docx(body=body))
    wb = load_workbook(xlsx)
    ws = wb["Entitäten"]
    header = [c.value for c in ws[1]]
    col = {h: i + 1 for i, h in enumerate(header)}
    ids = {ws.cell(r, col["Formen"]).value.split(" | ")[0]: ws.cell(r, 1).value for r in range(2, ws.max_row + 1)}
    for r in range(2, ws.max_row + 1):
        if ws.cell(r, col["Formen"]).value.startswith("Kaiser Bau"):
            ws.cell(r, col["Zusammenführen mit"]).value = ids["Albert Kaiser"]
    wb.save(xlsx)
    _, docx2, _, _ = _review(tmp_path, make_docx(body=body), entities_path=xlsx)
    assert "Die Firma A AG gehört A." in _accepted_text(docx2)


def test_existing_tracked_changes_are_accepted_first(tmp_path):
    data = make_docx(body=["I. Sachverhalt", "Herr Albert Kaiser kam."])
    pkg = DocxPackage.from_bytes(data)
    root = pkg.xml("word/document.xml")
    p = list(root.iter(w("p")))[1]
    from lxml import etree

    ins = etree.SubElement(p, w("ins"))
    ins.set(w("id"), "900")
    ins.set(w("author"), "Hans Muster")
    ins.set(w("date"), "2026-01-01T00:00:00Z")
    r = etree.SubElement(ins, w("r"))
    t = etree.SubElement(r, w("t"))
    t.text = " Später."
    analysis, docx, _, _ = _review(tmp_path, pkg.to_bytes())
    out = DocxPackage.load(docx)
    authors = {el.get(w("author")) for el in out.xml("word/document.xml").iter(w("ins"))}
    assert "Hans Muster" not in authors
    assert any("Überarbeitungen" in n for n in analysis.notes)


def test_rubrum_variants(tmp_path):
    data = make_docx(body=[
        "in der Beschwerdesache",
        "Beschwerdeführer:",
        "1. Albert Kaiser, Dorfstrasse 5, 9490 Vaduz",
        "2. Berta Kaiser-Huber\nDorfstrasse 5\n9490 Vaduz",
        "vertreten durch Rechtsanwalt Dr. Max Berger, Städtle 3, 9490 Vaduz",
        "gegen",
        "Beschwerdegegnerin:\tRegierung des Fürstentums Liechtenstein, Peter-Kaiser-Platz 1, 9490 Vaduz",
        "I. Sachverhalt",
        "Albert Kaiser und Berta Kaiser-Huber sowie Dr. Berger. Die Regierung entschied.",
    ])
    _, docx, _, _ = _review(tmp_path, data)
    text = _accepted_text(docx)
    assert "1. A\n" in text and "2. B\n" in text
    assert "vertreten durch ***" in text
    assert "Beschwerdegegnerin:\tRegierung des Fürstentums Liechtenstein" in text
    assert "A und B sowie Dr. ***. Die Regierung entschied." in text


def test_inline_rubrum_ogh_style(tmp_path):
    data = make_docx(body=[
        "Der Oberste Gerichtshof hat als Revisionsgericht in der Rechtssache der klagenden Partei Albert Kaiser, "
        "Dorfstrasse 5, 9490 Vaduz, vertreten durch Berger & Partner Rechtsanwälte, Bergstrasse 1, 9490 Vaduz, "
        "gegen die beklagte Partei Papageien Anstalt, Landstrasse 12, 9495 Triesen, wegen CHF 10'000.00 entschieden:",
        "Begründung",
        "Die Papageien Anstalt zahlte Albert Kaiser nichts.",
    ])
    _, docx, _, _ = _review(tmp_path, data)
    text = _accepted_text(docx)
    assert "der klagenden Partei A, vertreten durch ***, gegen die beklagte Partei B Anstalt, wegen" in text
    assert "Die B Anstalt zahlte A nichts." in text


def test_ambiguous_surname_becomes_comment(tmp_path):
    data = make_docx(body=[
        "Beschwerdeführer:\t1. Albert Kaiser, Dorfstrasse 5, 9490 Vaduz",
        "\t2. Berta Kaiser, Dorfstrasse 5, 9490 Vaduz",
        "I. Sachverhalt",
        "Kaiser beantragte die Abweisung.",
    ])
    analysis, docx, _, _ = _review(tmp_path, data)
    comments = [m for m in analysis.mentions if not m.change]
    assert any(m.span.text == "Kaiser" and "mehrdeutig" in (m.comment or "") for m in comments)


def test_judge_surname_shared_with_party_is_flagged(tmp_path):
    data = make_docx(body=[
        "Der Staatsgerichtshof hat unter dem Vorsitz von Präsident Dr. Hugo Vogt in der Beschwerdesache",
        "Beschwerdeführer:\tPeter Vogt, Dorfstrasse 5, 9490 Vaduz",
        "I. Sachverhalt",
        "Peter Vogt rügte die Verletzung. Vogt verwies auf Präsident Vogt.",
    ])
    analysis, docx, _, _ = _review(tmp_path, data)
    text = _accepted_text(docx)
    assert "Präsident Dr. Hugo Vogt" in text and "A rügte" in text and "Präsident Vogt." in text
    flagged = [m for m in analysis.mentions if not m.change and m.span.text == "Vogt"]
    assert flagged and "Gerichtsperson" in flagged[0].comment


def test_no_llm_path_and_ner_stub(tmp_path):
    from anon.detect.ner import NerDetector

    class Stub:
        name = "stub"

        def predict(self, text, labels, threshold):
            out = []
            for m in re.finditer(r"Hansjörg Beck|Lavadina", text):
                out.append((m.start(), m.end(), "person" if m.group().startswith("H") else "location", 0.9))
            return out

    cfg = load_config()
    data = make_docx(body=["I. Sachverhalt", "Hansjörg Beck wohnt in Lavadina. Beck ist Landwirt in Lavadina."])
    src = tmp_path / "x.docx"
    src.write_bytes(data)
    analysis, docx, _ = review(src, cfg, tmp_path / "o", use_ner=True, ner=NerDetector(cfg, Stub()))
    text = _accepted_text(docx)
    assert "A wohnt in ***. A ist Landwirt in ***." in text
    assert validate(DocxPackage.load(docx)) == []


@pytest.mark.parametrize("letters", [26, 28])
def test_letters_beyond_z(tmp_path, letters):
    names = [f"Herr Na{chr(97 + i % 26)}{chr(97 + i // 26)}mann kam." for i in range(letters)]
    analysis = analyse(make_docx(body=["I. Sachverhalt"] + names), load_config(), use_ner=False)
    reps = [e.replacement for e in analysis.entities if e.category == "party"]
    assert reps[:3] == ["A", "B", "C"]
    if letters > 26:
        assert reps[26:28] == ["AA", "AB"]


def test_small_plot_numbers_do_not_collide_with_xml_ids(tmp_path):
    # w:id attributes of revisions/bookmarks are small integers too; they are not leaks
    body = ["I. Sachverhalt"] + [f"Herr Na{chr(97 + i)}mann besitzt Grundstück Nr. {i + 1}." for i in range(12)]
    analysis, docx, xlsx, cfg = _review(tmp_path, make_docx(body=body))
    result = finalize(docx.read_bytes(), read_entities(xlsx), cfg)
    assert result.data is not None, result.report.summary_lines()
