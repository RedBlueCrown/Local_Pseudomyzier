"""Optional local LLM (OpenAI-compatible endpoint: Ollama, vLLM).

Hard rules
  * Only hosts on the allow-list (localhost, 127.0.0.1, ::1, configured
    internal hosts) are contacted. Proxy environment variables are ignored,
    redirects are refused, user-info tricks are rejected.
  * The LLM never writes document text. It classifies roles, groups name
    variants and suggests guideline-2.4 passages; all document edits remain
    deterministic rule code. Suggested passages are anchored by exact string
    search in the paragraph; anything that does not match is discarded.
  * Temperature 0, JSON-schema structured output.
  * If the endpoint is down the run continues without LLM (fail safe).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..config import Config
from ..docx_io.reader import OBJECT, Document
from ..resolve.entities import merge_into

log = logging.getLogger(__name__)

ROLES = ["party", "affected_third", "representative", "judge", "clerk", "cited_author", "authority",
         "expert_or_doctor", "other"]
KEEP_ROLES = {"judge", "clerk", "cited_author", "authority"}


class HostNotAllowed(ValueError):
    pass


class LLMUnavailable(RuntimeError):
    pass


def check_url(url: str, allowed_hosts: list[str]) -> str:
    """Return the normalised base URL or raise HostNotAllowed."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise HostNotAllowed(f"scheme {parts.scheme!r} not allowed")
    if parts.username or parts.password or "@" in parts.netloc or "\\" in url:
        raise HostNotAllowed("credentials in URL not allowed")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise HostNotAllowed("no host")
    allowed = {h.lower().strip("[]").rstrip(".") for h in allowed_hosts}
    if host not in allowed:
        raise HostNotAllowed(f"host {host!r} is not on the LLM allow-list")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and not (ip.is_loopback or ip.is_private or ip.is_link_local):
        raise HostNotAllowed(f"address {host} is not an internal address")
    return url.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise HostNotAllowed(f"redirect refused ({code})")


class LLMClient:
    def __init__(self, base_url: str, model: str, allowed_hosts: list[str], timeout: float = 180.0):
        self.allowed_hosts = list(allowed_hosts)
        self.base_url = check_url(base_url, self.allowed_hosts)
        self.model = model
        self.timeout = timeout
        # ProxyHandler({}) ignores HTTP(S)_PROXY: requests go straight to the local host.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    @classmethod
    def from_config(cls, cfg: Config) -> "LLMClient":
        llm = cfg.rules["llm"]
        return cls(llm["base_url"], llm["model"], llm.get("allowed_hosts", ["localhost", "127.0.0.1", "::1"]),
                   float(llm.get("timeout_s", 180)))

    def _request(self, path: str, payload: dict | None = None, timeout: float | None = None) -> dict:
        url = check_url(self.base_url + path, self.allowed_hosts)
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                     headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HostNotAllowed:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, json.JSONDecodeError) as exc:
            raise LLMUnavailable(type(exc).__name__) from exc

    def ping(self) -> None:
        self._request("/models", timeout=5)

    def chat_json(self, system: str, user: str, name: str, schema: dict) -> dict:
        payload = {
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}},
        }
        resp = self._request("/chat/completions", payload)
        try:
            content = resp["choices"][0]["message"]["content"]
            return json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMUnavailable("malformed response") from exc


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
SYSTEM = ("Du unterstützt die Anonymisierung liechtensteinischer Gerichtsentscheidungen nach der internen Richtlinie "
          "des OGH. Antworte ausschliesslich im verlangten JSON-Format. Zitate müssen wörtlich aus dem Text stammen.")

ROLE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["role", "confidence"],
    "properties": {"role": {"type": "string", "enum": ROLES}, "confidence": {"type": "number"}},
}
CLUSTER_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["groups"],
    "properties": {"groups": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}},
}
PASSAGE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["passages"],
    "properties": {"passages": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["quote", "reason", "proposal"],
        "properties": {"quote": {"type": "string"}, "reason": {"type": "string"}, "proposal": {"type": "string"}},
    }}},
}


@dataclass
class PassageSuggestion:
    part: str
    para: int
    start: int
    end: int
    reason: str
    proposal: str


def _snippets(doc: Document, entity, n: int, width: int) -> list[str]:
    out = []
    for m in entity.mentions[: n * 3]:
        para = doc.get(m.span.part, m.span.para)
        s, e = m.span.start, m.span.end
        a, b = max(0, s - width // 2), min(len(para.text), e + width // 2)
        text = para.text[a:s] + "⟦" + para.text[s:e] + "⟧" + para.text[e:b]
        text = text.replace(OBJECT, " ")
        if text not in out:
            out.append(text)
        if len(out) >= n:
            break
    return out


def classify_roles(client: LLMClient, doc: Document, entities, cfg: Config) -> list[str]:
    notes = []
    llm = cfg.rules["llm"]
    for e in entities:
        if e.kind not in ("person", "org", "representative") or not e.mentions:
            continue
        snippets = _snippets(doc, e, int(llm.get("max_snippets", 3)), int(llm.get("snippet_chars", 300)))
        user = ("Welche Rolle hat die mit ⟦…⟧ markierte Person/Organisation im Verfahren?\n"
                "Rollen: " + ", ".join(ROLES) + "\n\n" + "\n---\n".join(snippets))
        res = client.chat_json(SYSTEM, user, "role", ROLE_SCHEMA)
        role, conf = res.get("role"), float(res.get("confidence", 0))
        if role not in ROLES:
            continue
        e.llm_role = role
        if role in KEEP_ROLES and conf >= 0.5:
            e.force_review = True
            e.notes.append(f"LLM: Rolle wahrscheinlich '{role}' – prüfen, ob nicht zu anonymisieren")
        elif role == "expert_or_doctor":
            e.role = "expert_or_doctor"
        elif role in ("party", "affected_third", "representative") and conf >= 0.8 \
                and e.confidence < cfg.review_threshold:
            e.confidence = cfg.review_threshold
            e.notes.append("LLM bestätigt Personenbezug")
    return notes


def cluster_variants(client: LLMClient, entities) -> list[str]:
    names = [e for e in entities if e.kind in ("person", "org", "representative") and e.forms]
    if len(names) < 2:
        return []
    listing = "\n".join(f"{e.id}: " + " | ".join(e.forms) for e in names)
    res = client.chat_json(SYSTEM, "Welche IDs bezeichnen dieselbe Person/Organisation? Gib Gruppen von IDs zurück "
                                   "(nur Gruppen mit mindestens zwei IDs).\n" + listing, "clusters", CLUSTER_SCHEMA)
    by_id = {e.id: e for e in names}
    notes = []
    for group in res.get("groups", []):
        members = [by_id[i] for i in group if i in by_id]
        if len(members) < 2:
            continue
        head = members[0]
        for other in members[1:]:
            if _compatible(head, other):
                merge_into(head, other, f"LLM: {other.id} zusammengeführt")
                notes.append(f"LLM: zwei Namensvarianten zusammengeführt ({head.id})")
            else:
                head.notes.append(f"LLM: möglicherweise identisch mit {other.id}")
                other.notes.append(f"LLM: möglicherweise identisch mit {head.id}")
    return notes


def _compatible(a, b) -> bool:
    if a.kind != b.kind:
        return False
    fa = {f.casefold() for f in a.forms}
    fb = {f.casefold() for f in b.forms}
    if a.tokens and b.tokens and a.surname.casefold() == b.surname.casefold():
        return True
    return any(x in y or y in x for x in fa for y in fb)


def suggest_passages(client: LLMClient, doc: Document) -> list[PassageSuggestion]:
    out: list[PassageSuggestion] = []
    for para in doc.paragraphs:
        if para.kind not in ("document", "footnotes") or len(para.text) < 80:
            continue
        text = para.text.replace(OBJECT, " ")
        res = client.chat_json(
            SYSTEM,
            "Richtlinie 2.4: Finde Passagen, die eine Person trotz Anonymisierung indirekt identifizierbar machen "
            "(z. B. Beruf + Ort, besondere Ereignisse, Familienverhältnisse). Zitiere wörtlich, begründe kurz und "
            "schlage eine neutrale Formulierung oder '[…]' vor. Leere Liste, wenn nichts.\n\nText:\n" + text,
            "passages", PASSAGE_SCHEMA)
        for p in res.get("passages", []):
            quote = (p.get("quote") or "").strip()
            if len(quote) < 8:
                continue
            idx = para.text.find(quote)
            if idx < 0:
                idx_n = para.norm.find(quote)
                if idx_n < 0:
                    continue  # not verbatim -> discarded
                s, e = para.to_orig(idx_n, idx_n + len(quote))
            else:
                s, e = idx, idx + len(quote)
            out.append(PassageSuggestion(para.part, para.index, s, e, str(p.get("reason", ""))[:300],
                                         str(p.get("proposal", ""))[:300]))
    return out


def enrich_entities(client: LLMClient, doc: Document, entities, cfg: Config) -> list[str]:
    """Run all LLM tasks; on any endpoint failure fall back silently (warning)."""
    try:
        notes = cluster_variants(client, entities)
        notes += classify_roles(client, doc, [e for e in entities if e.forms], cfg)
        client.passages = suggest_passages(client, doc)
        return notes
    except LLMUnavailable as exc:
        log.warning("LLM failed during processing (%s): continuing without LLM results", exc)
        client.passages = []
        return ["LLM während der Verarbeitung nicht erreichbar: Ergebnisse ohne LLM."]

