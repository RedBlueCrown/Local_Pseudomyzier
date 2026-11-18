"""Local web app: API flow and security checks."""

from __future__ import annotations

import io
import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from docx_factory import make_docx  # noqa: E402

from anon.app.server import create_app  # noqa: E402
from anon.docx_io.package import DocxPackage  # noqa: E402
from anon.docx_io.reader import read_document  # noqa: E402

BODY = [
    "Beschwerdeführer:\tAlbert Kaiser, Dorfstrasse 5, 9490 Vaduz",
    "I. Sachverhalt",
    "Albert Kaiser besitzt das Vaduzer Grundstück Nr. 4716. Die Planken des Stegs sind morsch. "
    "Quell bestätigte dies.",
]


@pytest.fixture()
def client(tmp_path):
    app = create_app(tmp_path / "work", token="T", extra_hosts={"testserver"})
    c = TestClient(app)
    c.headers.update({"X-Anon-Token": "T"})
    return c


def _upload(client, body=BODY) -> str:
    r = client.post("/api/cases", files={"file": ("Urteil.docx", make_docx(body=body), "application/octet-stream")},
                    data={"use_llm": "false"})
    assert r.status_code == 200, r.text
    case_id = r.json()["id"]
    for _ in range(100):
        s = client.get(f"/api/cases/{case_id}").json()
        if s["status"] in ("ready", "error"):
            break
        time.sleep(0.1)
    assert s["status"] == "ready", s
    return case_id


def _text(data: bytes) -> str:
    return "\n".join(p.text for p in read_document(DocxPackage.from_bytes(data)).paragraphs)


def test_security_token_and_host(tmp_path):
    app = create_app(tmp_path / "w", token="T", extra_hosts={"testserver"})
    c = TestClient(app)
    assert c.get("/api/cases").status_code == 403                      # no token
    assert c.get("/api/cases", headers={"X-Anon-Token": "x"}).status_code == 403
    assert c.get("/api/cases", headers={"X-Anon-Token": "T", "Host": "evil.example"}).status_code == 403
    page = c.get("/")
    assert page.status_code == 200 and 'content="T"' in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]


def test_settings_refuse_remote_llm(client):
    r = client.put("/api/settings", json={"llm": {"base_url": "https://api.example.com/v1"}})
    assert r.status_code == 400
    r = client.put("/api/settings", json={"llm": {"enabled": True, "base_url": "http://localhost:11434/v1",
                                                   "model": "qwen3:8b"}})
    assert r.status_code == 200 and r.json()["llm"]["model"] == "qwen3:8b"


def test_review_flow_and_export(client):
    case_id = _upload(client)
    review = client.get(f"/api/cases/{case_id}/review").json()
    assert review["sections"] and 'data-pid="word/document.xml|0"' in review["sections"][0]["html"]
    questions = [m for m in review["mentions"] if m["kind"] == "question"]
    assert questions and review["open_questions"] == len(questions)
    # export refuses while questions are open
    assert client.post(f"/api/cases/{case_id}/export").json()["ok"] is False
    # answer: keep the ambiguous "Planken"
    client.post(f"/api/cases/{case_id}/decisions", json={"keys": [q["key"] for q in questions], "decision": "dismiss"})
    # reject the place adjective change, keep everything else
    adj = next(m for m in review["mentions"] if m["text"] == "Vaduzer")
    client.post(f"/api/cases/{case_id}/decisions", json={"keys": [adj["key"]], "decision": "reject"})
    # add a missed name by selecting it
    para = next(m for m in review["mentions"] if m["text"] == "Vaduzer")["pid"]
    text = "Albert Kaiser besitzt das Vaduzer Grundstück Nr. 4716. Die Planken des Stegs sind morsch. Quell bestätigte dies."
    s = text.index("Quell")
    review = client.post(f"/api/cases/{case_id}/entities", json={"pid": para, "start": s, "end": s + 5,
                                                                 "type": "person"}).json()
    assert any(e["label"] == "Quell" for e in review["entities"])
    # manual edit of a passage
    s2 = text.index("Die Planken des Stegs sind morsch.")
    review = client.post(f"/api/cases/{case_id}/manual", json={"pid": para, "start": s2,
                                                               "end": s2 + len("Die Planken des Stegs sind morsch."),
                                                               "replacement": "[…]"}).json()
    assert review["open_questions"] == 0
    r = client.post(f"/api/cases/{case_id}/export").json()
    assert r["ok"], r
    clean = client.get(f"/api/cases/{case_id}/download/clean")
    assert clean.status_code == 200
    out = _text(clean.content)
    assert "A besitzt das Vaduzer Grundstück Nr. 1. […] B bestätigte dies." in out
    assert "Kaiser" not in out and "Quell" not in out
    review_copy = client.get(f"/api/cases/{case_id}/download/review")
    assert review_copy.status_code == 200 and len(review_copy.content) > 1000
    assert client.get(f"/api/cases/{case_id}/download/entities").status_code == 200
    # closing the case deletes its data
    assert client.delete(f"/api/cases/{case_id}").json()["ok"]
    assert client.get(f"/api/cases/{case_id}").status_code == 404


def test_entity_actions_keep_replacement_merge(client):
    body = ["Beschwerdeführer:\tAlbert Kaiser, Dorfstrasse 5, 9490 Vaduz", "I. Sachverhalt",
            "Albert Kaiser und die Kaiser Bau AG in Buchs."]
    case_id = _upload(client, body)
    review = client.get(f"/api/cases/{case_id}/review").json()
    ents = {e["label"]: e for e in review["entities"]}
    review = client.post(f"/api/cases/{case_id}/entities/{ents['Buchs']['id']}", json={"action": "keep"}).json()
    assert {e["label"]: e for e in review["entities"]}["Buchs"]["decision"] == "keep"
    ents = {e["label"]: e for e in review["entities"]}
    review = client.post(f"/api/cases/{case_id}/entities/{ents['Albert Kaiser']['id']}",
                         json={"action": "replacement", "value": "X"}).json()
    ents = {e["label"]: e for e in review["entities"]}
    assert ents["Albert Kaiser"]["replacement"] == "X"
    review = client.post(f"/api/cases/{case_id}/entities/{ents['Kaiser Bau']['id']}",
                         json={"action": "merge", "value": ents["Albert Kaiser"]["id"]}).json()
    assert not any(e["label"] == "Kaiser Bau" for e in review["entities"])
    r = client.post(f"/api/cases/{case_id}/export").json()
    assert r["ok"], r
    out = _text(client.get(f"/api/cases/{case_id}/download/clean").content)
    assert "X und die X AG in Buchs." in out


def test_upload_rejects_non_docx(client):
    r = client.post("/api/cases", files={"file": ("x.docx", b"not a zip", "application/octet-stream")})
    assert r.status_code == 400
    r = client.post("/api/cases", files={"file": ("x.pdf", b"%PDF", "application/pdf")})
    assert r.status_code == 400


def test_word_finalize_path(client, tmp_path):
    from anon.config import load_config
    from anon.pipeline import review as run_review

    src = tmp_path / "in.docx"
    src.write_bytes(make_docx(body=BODY[:2] + ["Albert Kaiser kam."]))
    _, docx, xlsx = run_review(src, load_config(), tmp_path / "o", use_ner=False)
    r = client.post("/api/word/finalize", files={"review": ("Urteil Müller – 2026.review.docx", docx.read_bytes()),
                                                 "entities": ("in.entities.xlsx", xlsx.read_bytes())})
    assert r.status_code == 200 and "A kam." in _text(r.content)
    # non-ASCII file names survive (RFC 5987) instead of breaking the header
    assert "filename*=UTF-8''Urteil%20M%C3%BCller%20%E2%80%93%202026%20anonymisiert.docx" in \
        r.headers["content-disposition"]
    _ = io


def test_decide_all_and_stale_export(client):
    """"Anonymise all" on an entity with uncertain occurrences applies them
    (accept on a question means apply); any later edit invalidates the export."""
    case_id = _upload(client)
    review = client.get(f"/api/cases/{case_id}/review").json()
    q = next(m for m in review["mentions"] if m["kind"] == "question")
    assert q["context"] and q["text"] not in q["context"][0]  # words around the question for the side panel
    review = client.post(f"/api/cases/{case_id}/entities/{q['entity']}",
                         json={"action": "decide_all", "value": "accept"}).json()
    assert all(m["decision"] == "apply" for m in review["mentions"] if m["entity"] == q["entity"])
    rest = [m["key"] for m in review["mentions"] if m["decision"] == "open"]
    if rest:
        client.post(f"/api/cases/{case_id}/decisions", json={"keys": rest, "decision": "dismiss"})
    r = client.post(f"/api/cases/{case_id}/export").json()
    assert r["ok"], r
    assert q["text"] not in _text(client.get(f"/api/cases/{case_id}/download/clean").content)
    assert client.get(f"/api/cases/{case_id}").json()["status"] == "exported"
    # a later change: export is stale, clean file gone until exported again
    client.post(f"/api/cases/{case_id}/decisions", json={"keys": [q["key"]], "decision": "dismiss"})
    assert client.get(f"/api/cases/{case_id}").json()["status"] == "ready"
    assert client.get(f"/api/cases/{case_id}/download/clean").status_code == 404
