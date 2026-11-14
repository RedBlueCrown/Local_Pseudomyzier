"""Evaluation on a synthetic gold pair that mimics the human patterns of the
real gold files: a treating doctor left unchanged, a name the tool cannot
know, a spacing slip, and a guideline-2.4 passage deletion."""

from __future__ import annotations

import json

from docx_factory import make_docx

from anon.config import load_config
from anon.evaluate.compare import _find_pairs, compare_pair, run_evaluation
from anon.pipeline import analyse, finalize, write_review
from anon.review.entities_file import read_entities

INPUT = [
    "Beschwerdeführer:\tAlbert Kaiser, Dorfstrasse 5, 9490 Vaduz",
    "I. Sachverhalt",
    "Albert Kaiser ist Eigentümer des Grundstücks Nr. 4716 in Vaduz.",
    "Hausarzt Dr. med. Rudolf Nova bestätigte dies. Quell sah zu.",
    "Kaiser ist der einzige Tierarzt im ganzen Dorf und allseits bekannt.",
]
GOLD = [
    "Beschwerdeführer:\tA",
    "I. Sachverhalt",
    "A ist Eigentümer des Grundstücks Nr.1 in ***.",
    "Hausarzt Dr. med. Rudolf Nova bestätigte dies. B sah zu.",
    "[…]",
]


def _tool(tmp_path, data):
    cfg = load_config()
    analysis = analyse(data, cfg, use_ner=False)
    docx, xlsx = tmp_path / "r.docx", tmp_path / "e.xlsx"
    write_review(analysis, cfg, docx, xlsx)
    result = finalize(docx.read_bytes(), read_entities(xlsx), cfg)
    assert result.data is not None
    return analysis, result.data


def test_compare_pair_classifies_changes(tmp_path):
    data = make_docx(body=INPUT)
    analysis, tool = _tool(tmp_path, data)
    res = compare_pair(data, make_docx(body=GOLD), tool, analysis, "synthetic")
    by_status = {}
    for c in res.gold:
        by_status.setdefault(c.status, []).append(c)
    missed = [" ".join(c.before) for c in by_status.get("missed", [])]
    assert missed == ["Quell"]
    assert len(by_status["passage"]) == 1
    matched = by_status["matched"]
    assert any(not c.exact and "4716" in c.before for c in matched)        # "Nr.1" slip: found, not exact
    assert any(c.exact and c.before == ["Vaduz"] for c in matched)
    extras = [" ".join(c.before) for c in res.tool if c.status == "extra"]
    assert extras == ["Rudolf Nova"]                                        # gold left the doctor unchanged
    assert (res.letters_agree, res.letters_total) == (1, 1)


def test_run_evaluation_discovers_pairs_and_unseen(tmp_path):
    root = tmp_path / "testdata"
    root.mkdir()
    (root / "VGH 2026.21 pseudonymisiert.docx").write_bytes(make_docx(body=INPUT))
    (root / "VGH 2026.21 anonymisiert.docx").write_bytes(make_docx(body=GOLD))
    (root / "StGH_2024-024.docx").write_bytes(make_docx(body=INPUT[:3]))
    (root / "Interne Richlinien OGH_Anonymisierung.docx").write_bytes(make_docx(body=["Richtlinie"]))
    pairs, unseen = _find_pairs(root)
    assert [p[0].name for p in pairs] == ["VGH 2026.21 pseudonymisiert.docx"]
    assert [p.name for p in unseen] == ["StGH_2024-024.docx"]
    report = run_evaluation(root, tmp_path / "eval", load_config(), use_ner=False)
    text = report.read_text(encoding="utf-8")
    assert "Recall nach Kategorie" in text and "StGH_2024-024" in text and "Verpasst" in text
    data = json.loads((tmp_path / "eval" / "report.json").read_text(encoding="utf-8"))
    assert data[0]["leak_ok"] is True
    assert (tmp_path / "eval" / "VGH 2026.21 pseudonymisiert" / "VGH 2026.21 pseudonymisiert.review.docx").exists()
