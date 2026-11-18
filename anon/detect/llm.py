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
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..config import Config
from ..docx_io.reader import OBJECT, Document, normalise_simple
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
    def __init__(self, base_url: str, model: str, allowed_hosts: list[str], timeout: float = 180.0,
                 no_think: bool | None = None):
        self.allowed_hosts = list(allowed_hosts)
        self.base_url = check_url(base_url, self.allowed_hosts)
        self.model = model
        self.timeout = timeout
        self.no_think = ("qwen3" in model.lower()) if no_think is None else no_think
        # ProxyHandler({}) ignores HTTP(S)_PROXY: requests go straight to the local host.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    @classmethod
    def from_config(cls, cfg: Config) -> "LLMClient":
        llm = cfg.rules["llm"]
        no_think = llm.get("no_think", "auto")
        return cls(llm["base_url"], llm["model"], llm.get("allowed_hosts", ["localhost", "127.0.0.1", "::1"]),
                   float(llm.get("timeout_s", 180)), None if no_think == "auto" else bool(no_think))

    def models(self) -> list[str]:
        data = self._request("/models", timeout=5)
        return sorted(m.get("id", "") for m in data.get("data", []) if m.get("id"))

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
        if self.no_think:
            payload["messages"][1]["content"] += "\n/no_think"  # Qwen3: answer directly, no reasoning trace
        resp = self._request("/chat/completions", payload)
        try:
            content = resp["choices"][0]["message"]["content"] or ""
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content).strip()
            return json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMUnavailable("malformed response") from exc


# ---------------------------------------------------------------------------
# Tasks
#
# run_llm() talks to the model and returns a plain, serialisable LLMResult
# (keyed by surface forms, not by entity ids), so an app can cache it and
# re-apply it with apply_llm() after the reviewer edits entities, without
# calling the model again.
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
        "type": "object", "additionalProperties": False,
        "required": ["paragraph", "quote", "reason", "proposal"],
        "properties": {"paragraph": {"type": "integer"}, "quote": {"type": "string"}, "reason": {"type": "string"},
                       "proposal": {"type": "string"}},
    }}},
}
PASSAGE_PROMPT = (
    "Richtlinie 2.4/2.5: Finde Passagen, die eine Person trotz Anonymisierung der Namen indirekt identifizierbar "
    "machen (z. B. Beruf + Ort, Funktion in einem Verein, besondere Ereignisse, Familienverhältnisse, Krankheit, "
    "Kindsentführung, Suizid, Adoption, Geschlechtsanpassung, Abstammung). Namen, Adressen, Daten und Nummern werden "
    "bereits separat anonymisiert und sind KEINE Passagen. Gib für jede Passage die Absatznummer, ein wörtliches "
    "Zitat, eine kurze Begründung und eine neutrale Formulierung oder '[…]' an. Leere Liste, wenn nichts.\n\n"
)


@dataclass
class PassageSuggestion:
    part: str
    para: int
    start: int
    end: int
    reason: str
    proposal: str


@dataclass
class LLMResult:
    groups: list[list[str]] = field(default_factory=list)   # first forms judged to be the same entity
    roles: dict[str, list] = field(default_factory=dict)    # first form -> [role, confidence]
    passages: list[dict] = field(default_factory=list)      # part, para, start, end, reason, proposal
    error: str | None = None
    calls: int = 0

    def to_dict(self) -> dict:
        return {"groups": self.groups, "roles": self.roles, "passages": self.passages, "error": self.error,
                "calls": self.calls}

    @classmethod
    def from_dict(cls, data: dict) -> "LLMResult":
        return cls(data.get("groups", []), data.get("roles", {}), data.get("passages", []), data.get("error"),
                   data.get("calls", 0))


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


def _name_entities(entities) -> list:
    return [e for e in entities if e.kind in ("person", "org", "representative") and e.forms and e.mentions]


def _passage_batches(doc: Document, max_chars: int) -> list[list]:
    batches: list[list] = []
    current: list = []
    size = 0
    for para in doc.paragraphs:
        if para.kind not in ("document", "footnotes") or len(para.text.strip()) < 80:
            continue
        if current and size + len(para.text) > max_chars:
            batches.append(current)
            current, size = [], 0
        current.append(para)
        size += len(para.text)
    if current:
        batches.append(current)
    return batches


def _locate(para, quote: str) -> tuple[int, int] | None:
    quote = quote.strip()
    if len(quote) < 8:
        return None
    idx = para.text.find(quote)
    if idx >= 0:
        return idx, idx + len(quote)
    idx_n = para.norm.find(normalise_simple(quote))
    if idx_n < 0:
        return None  # not verbatim -> discarded
    return para.to_orig(idx_n, idx_n + len(normalise_simple(quote)))


def run_llm(client: LLMClient, doc: Document, entities, cfg: Config, progress=None) -> LLMResult:
    """All LLM tasks. Never raises for an unreachable model: the partial result
    carries `error` and the run continues without the missing parts."""
    llm = cfg.rules["llm"]
    names = _name_entities(entities)
    batches = _passage_batches(doc, int(llm.get("batch_chars", 3000)))
    total = (1 if len(names) > 1 else 0) + len(names) + len(batches)
    result = LLMResult()

    def tick(label: str) -> None:
        result.calls += 1
        if progress:
            progress(result.calls, total, label)

    try:
        if len(names) > 1:
            listing = "\n".join(f"{e.id}: " + " | ".join(e.forms) for e in names)
            res = client.chat_json(SYSTEM, "Welche IDs bezeichnen dieselbe Person/Organisation? Gib Gruppen von IDs "
                                           "zurück (nur Gruppen mit mindestens zwei IDs).\n" + listing,
                                   "clusters", CLUSTER_SCHEMA)
            by_id = {e.id: e for e in names}
            result.groups = [[by_id[i].forms[0] for i in g if i in by_id] for g in res.get("groups", [])]
            result.groups = [g for g in result.groups if len(g) > 1]
            tick("Namensvarianten")
        for e in names:
            snippets = _snippets(doc, e, int(llm.get("max_snippets", 3)), int(llm.get("snippet_chars", 300)))
            user = ("Welche Rolle hat die mit ⟦…⟧ markierte Person/Organisation im Verfahren?\n"
                    "Rollen: " + ", ".join(ROLES) + "\n\n" + "\n---\n".join(snippets))
            res = client.chat_json(SYSTEM, user, "role", ROLE_SCHEMA)
            if res.get("role") in ROLES:
                result.roles[e.forms[0]] = [res["role"], float(res.get("confidence", 0))]
            tick("Rollen")
        for batch in batches:
            text = "\n\n".join(f"[P{i + 1}] " + p.text.replace(OBJECT, " ") for i, p in enumerate(batch))
            res = client.chat_json(SYSTEM, PASSAGE_PROMPT + text, "passages", PASSAGE_SCHEMA)
            for item in res.get("passages", []):
                try:
                    para = batch[int(item.get("paragraph", 0)) - 1]
                except (IndexError, TypeError, ValueError):
                    continue
                span = _locate(para, str(item.get("quote", "")))
                if span is None:
                    continue
                result.passages.append({"part": para.part, "para": para.index, "start": span[0], "end": span[1],
                                        "reason": str(item.get("reason", ""))[:300],
                                        "proposal": str(item.get("proposal", ""))[:300]})
            tick("Textpassagen (2.4)")
    except LLMUnavailable as exc:
        log.warning("LLM failed during processing (%s): continuing without LLM results", exc)
        result.error = str(exc)
    return result


def _find(entities, form: str):
    for e in entities:
        if form in e.forms:
            return e
    return None


def apply_llm(result: LLMResult, doc: Document, entities, cfg: Config) -> tuple[list[str], list[PassageSuggestion]]:
    """Apply a (possibly cached) LLMResult. Deterministic; no model call."""
    notes: list[str] = []
    for group in result.groups:
        members = [m for m in (_find(entities, f) for f in group) if m is not None]
        members = list(dict.fromkeys(members))
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
    for form, (role, conf) in result.roles.items():
        e = _find(entities, form)
        if e is None or not e.forms:
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
    passages = []
    for p in result.passages:
        try:
            para = doc.get(p["part"], p["para"])
        except KeyError:
            continue
        if 0 <= p["start"] < p["end"] <= len(para.text):
            passages.append(PassageSuggestion(p["part"], p["para"], p["start"], p["end"], p["reason"], p["proposal"]))
    if result.error:
        notes.append("LLM während der Verarbeitung nicht erreichbar: Ergebnisse teilweise ohne LLM.")
    return notes, passages


def _compatible(a, b) -> bool:
    if a.kind != b.kind:
        return False
    fa = {f.casefold() for f in a.forms}
    fb = {f.casefold() for f in b.forms}
    if a.tokens and b.tokens and a.surname.casefold() == b.surname.casefold():
        return True
    return any(x in y or y in x for x in fa for y in fb)


def enrich_entities(client: LLMClient, doc: Document, entities, cfg: Config) -> list[str]:
    """Run and apply all LLM tasks (CLI path); the result is kept on the client."""
    result = run_llm(client, doc, entities, cfg)
    notes, passages = apply_llm(result, doc, entities, cfg)
    client.passages = passages
    client.result = result
    return notes
