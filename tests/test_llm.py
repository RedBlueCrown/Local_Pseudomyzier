"""LLM client: host allow-list, fail-safe behaviour, and that LLM output never
changes text outside detected spans."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from docx_factory import make_docx
from typer.testing import CliRunner

from anon.config import load_config
from anon.detect.llm import HostNotAllowed, LLMClient, LLMUnavailable, check_url
from anon.docx_io.finalize import accept_all_parts, reject_all_parts
from anon.docx_io.package import DocxPackage, w
from anon.docx_io.reader import read_document
from anon.pipeline import review

DEFAULT = ["localhost", "127.0.0.1", "::1"]


@pytest.mark.parametrize("url", ["http://localhost:11434/v1", "http://127.0.0.1:8000/v1", "http://[::1]:8000/v1",
                                 "https://LOCALHOST/v1"])
def test_allowlist_accepts_local(url):
    assert check_url(url, DEFAULT)


@pytest.mark.parametrize("url", [
    "https://api.openai.com/v1",
    "http://localhost.evil.com/v1",
    "http://localhost@evil.com/v1",
    "http://user:pw@localhost/v1",
    "http://evil.com#@localhost/v1",
    "http://evil.com\\@localhost/v1",
    "file:///etc/passwd",
    "ftp://localhost/v1",
    "http://10.0.0.5:8000/v1",           # internal, but not configured
])
def test_allowlist_rejects_everything_else(url):
    with pytest.raises(HostNotAllowed):
        check_url(url, DEFAULT)


def test_configured_internal_host_allowed_but_public_ip_never():
    assert check_url("http://llm.gericht.local:8000/v1", DEFAULT + ["llm.gericht.local"])
    assert check_url("http://10.0.0.5:8000/v1", DEFAULT + ["10.0.0.5"])
    with pytest.raises(HostNotAllowed):
        check_url("http://8.8.8.8/v1", DEFAULT + ["8.8.8.8"])


def test_client_refuses_construction_for_remote_host():
    with pytest.raises(HostNotAllowed):
        LLMClient("https://api.openai.com/v1", "gpt", DEFAULT)


# ---------------------------------------------------------------------------
# Fake OpenAI-compatible server on 127.0.0.1
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    calls: list = []
    redirect_to: str | None = None

    def log_message(self, *a):  # silence
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.redirect_to:
            self.send_response(302)
            self.send_header("Location", self.redirect_to)
            self.end_headers()
            return
        self._send({"data": [{"id": "fake"}]})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        name = payload["response_format"]["json_schema"]["name"]
        user = payload["messages"][1]["content"]
        type(self).calls.append((name, payload["temperature"]))
        if name == "role":
            content = {"role": "expert_or_doctor" if "Gutachten" in user else "party", "confidence": 0.9}
        elif name == "clusters":
            content = {"groups": []}
        else:
            content = {"passages": []}
            if "einzige Tierarzt" in user:
                content["passages"] = [
                    {"quote": "der einzige Tierarzt des Dorfes", "reason": "Beruf + Ort identifizieren",
                     "proposal": "[…]"},
                    {"quote": "ein erfundenes Zitat, das nicht im Text steht", "reason": "x", "proposal": "y"},
                ]
        self._send({"choices": [{"message": {"content": json.dumps(content)}}]})


@pytest.fixture()
def fake_llm():
    _Handler.calls = []
    _Handler.redirect_to = None
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1", _Handler
    server.shutdown()


def test_redirect_to_remote_is_refused(fake_llm):
    url, handler = fake_llm
    handler.redirect_to = "http://example.com/v1/models"
    client = LLMClient(url, "fake", DEFAULT)
    with pytest.raises(HostNotAllowed):
        client.ping()


def test_proxy_environment_is_ignored(fake_llm, monkeypatch):
    url, _ = fake_llm
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    LLMClient(url, "fake", DEFAULT).ping()  # would fail if routed through the proxy


def _doc() -> bytes:
    return make_docx(body=[
        "Beschwerdeführer:\tAlbert Kaiser, Dorfstrasse 5, 9490 Vaduz",
        "I. Sachverhalt",
        "Albert Kaiser ist der einzige Tierarzt des Dorfes und wurde von Frau Maria Huber verklagt. "
        "Das Gutachten von Dr. med. Rudolf Nova liegt vor.",
    ])


def _accept_text(path) -> str:
    from anon.docx_io.finalize import remove_comments, remove_summary

    pkg = DocxPackage.load(path)
    accept_all_parts(pkg)
    remove_summary(pkg)
    remove_comments(pkg, None)
    return "\n".join(p.text for p in read_document(DocxPackage.from_bytes(pkg.to_bytes())).paragraphs)


def test_llm_suggestions_are_comments_only_and_text_unchanged(tmp_path, fake_llm):
    url, handler = fake_llm
    cfg = load_config()
    src = tmp_path / "in.docx"
    src.write_bytes(_doc())
    _, plain_docx, _ = review(src, cfg, tmp_path / "plain", use_ner=False)
    client = LLMClient(url, "fake", DEFAULT)
    analysis, llm_docx, _ = review(src, cfg, tmp_path / "llm", use_ner=False, llm=client)
    assert all(t == 0 for _, t in handler.calls)
    assert {n for n, _ in handler.calls} == {"role", "clusters", "passages"}
    # only the verbatim quote survives, as a comment
    assert len(analysis.passages) == 1
    comments = DocxPackage.load(llm_docx).xml("word/comments.xml")
    texts = ["".join(t.text or "" for t in c.iter(w("t"))) for c in comments.iter(w("comment"))]
    assert any("Richtlinie 2.4" in t and "einzige Tierarzt" in t for t in texts)
    # LLM changes no text: accepted text identical with and without LLM
    assert _accept_text(llm_docx) == _accept_text(plain_docx)
    rejected = DocxPackage.load(llm_docx)
    reject_all_parts(rejected)
    assert "der einzige Tierarzt des Dorfes" in "\n".join(p.text for p in read_document(rejected).paragraphs)
    nova = next(e for e in analysis.entities if "Rudolf Nova" in e.forms)
    assert nova.role == "expert_or_doctor"


def test_endpoint_down_falls_back_to_no_llm(tmp_path):
    client = LLMClient("http://127.0.0.1:9/v1", "fake", DEFAULT, timeout=2)
    with pytest.raises(LLMUnavailable):
        client.ping()
    cfg_file = tmp_path / "cfg.yaml"
    cfg_file.write_text("llm:\n  enabled: true\n  base_url: http://127.0.0.1:9/v1\n", encoding="utf-8")
    src = tmp_path / "in.docx"
    src.write_bytes(_doc())
    from anon.cli import app

    result = CliRunner().invoke(app, ["review", str(src), "-o", str(tmp_path / "o"), "--config", str(cfg_file),
                                      "--no-ner"])
    assert result.exit_code == 0, result.output
    assert "LLM nicht erreichbar" in result.output and "LLM: aus" in result.output
    assert (tmp_path / "o" / "in.review.docx").exists()


def test_cli_no_llm_produces_valid_review_copy(tmp_path):
    from anon.cli import app
    from anon.docx_io.validate import validate

    src = tmp_path / "in.docx"
    src.write_bytes(_doc())
    result = CliRunner().invoke(app, ["review", str(src), "-o", str(tmp_path / "o"), "--no-llm", "--no-ner"])
    assert result.exit_code == 0, result.output
    assert validate(DocxPackage.load(tmp_path / "o" / "in.review.docx")) == []
