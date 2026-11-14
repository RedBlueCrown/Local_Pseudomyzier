"""Independent check with a real word processor (LibreOffice, headless).

* the review copy loads and converts to PDF,
* LibreOffice sees one redline per tracked change, authored by category,
* LibreOffice "Reject All" gives the original text, "Accept All" gives the
  text of our clean export.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fixtures import decision_vgh

from anon.config import load_config
from anon.pipeline import finalize, review
from anon.review.entities_file import read_entities

SYSTEM_PY = shutil.which("python3", path="/usr/bin") or "/usr/bin/python3"


def _have_lo() -> bool:
    if not shutil.which("soffice"):
        return False
    probe = subprocess.run([SYSTEM_PY, "-c", "import uno"], capture_output=True)
    return probe.returncode == 0


pytestmark = [pytest.mark.libreoffice, pytest.mark.skipif(not _have_lo(), reason="LibreOffice/python3-uno missing")]


def test_libreoffice_accept_reject_and_pdf(tmp_path):
    cfg = load_config()
    src = tmp_path / "VGH.docx"
    src.write_bytes(decision_vgh())
    _, docx, xlsx = review(src, cfg, tmp_path / "out", use_ner=False)
    from anon.docx_io.finalize import remove_comments
    from anon.docx_io.package import DocxPackage

    reviewed = DocxPackage.load(docx)
    remove_comments(reviewed, None)  # reviewer: open "Prüfen" items need no change
    result = finalize(reviewed.to_bytes(), read_entities(xlsx), cfg)
    assert result.data is not None, result.report.summary_lines()
    clean = tmp_path / "clean.docx"
    clean.write_bytes(result.data)

    out_json = tmp_path / "lo.json"
    helper = Path(__file__).with_name("lo_uno.py")
    run = subprocess.run([SYSTEM_PY, str(helper), str(out_json), str(src), str(docx), str(clean)],
                         capture_output=True, timeout=240)
    assert run.returncode == 0, run.stderr.decode(errors="replace")[-2000:]
    data = json.loads(out_json.read_text(encoding="utf-8"))
    orig, rev, cln = data[str(src)], data[str(docx)], data[str(clean)]

    authors = {a for a, _ in rev["redlines"]}
    assert authors <= {"Partei", "Vertreter", "Ort", "Grundstück", "Datum/ID", "Prüfen"}
    assert {"Partei", "Grundstück", "Ort"} <= authors
    first = orig["loaded"].splitlines()[0]
    rejected = rev["reject"][rev["reject"].index(first):]  # drop the summary block
    accepted = rev["accept"][rev["accept"].index(first):]
    assert rejected == orig["loaded"]
    assert accepted == cln["loaded"]
    assert cln["redlines"] == [] and cln["comments"] == 0

    pdf = subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(tmp_path), str(docx)],
                         capture_output=True, timeout=240)
    assert pdf.returncode == 0
    assert (tmp_path / "VGH.review.pdf").stat().st_size > 1000


def test_reviewer_in_libreoffice_then_finalize(tmp_path):
    """Reviewer rejects all 'Ort' changes, accepts the rest and deletes the
    'Prüfen' comments in LibreOffice, saves as .docx; finalize must accept the
    reviewer's decisions (bookmarks/markers survive another editor's save)."""
    from docx_factory import make_docx

    cfg = load_config()
    src = tmp_path / "in.docx"
    src.write_bytes(make_docx(body=[
        "Beschwerdeführer:\tAlbert Kaiser, Dorfstrasse 5, 9490 Vaduz",
        {"table": [["Beschwerdegegnerin:", "Papageien Anstalt, Landstrasse 12, 9495 Triesen"]]},
        "I. Sachverhalt",
        "Albert Kaiser besitzt das Vaduzer Grundstück Nr. 4716; die Planken des Stegs sind morsch.",
    ]))
    _, docx, xlsx = review(src, cfg, tmp_path / "out", use_ner=False)
    saved = tmp_path / "saved.docx"
    helper = Path(__file__).with_name("lo_uno.py")
    run = subprocess.run([SYSTEM_PY, str(helper), "--simulate", str(docx), str(saved), "Ort"],
                         capture_output=True, timeout=240)
    assert run.returncode == 0, run.stderr.decode(errors="replace")[-2000:]
    result = finalize(saved.read_bytes(), read_entities(xlsx), cfg)
    assert result.data is not None, result.report.summary_lines()
    statuses = {h.status for h in result.report.hits}
    assert {"rejected_by_reviewer", "resolved_comment"} <= statuses
    from anon.docx_io.package import DocxPackage
    from anon.docx_io.reader import read_document

    text = "\n".join(p.text for p in read_document(DocxPackage.from_bytes(result.data)).paragraphs)
    assert "Prüfübersicht" not in text
    assert "A besitzt das Vaduzer Grundstück Nr. 1; die Planken des Stegs sind morsch." in text


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
