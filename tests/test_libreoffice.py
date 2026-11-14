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
    result = finalize(docx.read_bytes(), read_entities(xlsx), cfg)
    assert result.data is not None
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
    assert authors <= {"Partei", "Vertreter", "Ort", "Grundstück", "Datum/ID"}
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


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
