"""Local web app: upload -> review in the browser -> clean export.

Runs on 127.0.0.1 only. Every /api call needs the token that is embedded in
the page at start-up (another website in the same browser cannot read it),
and the Host header must be local (protection against DNS rebinding).
"""

from __future__ import annotations

import logging
import re
import secrets
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ..detect.llm import HostNotAllowed, LLMClient, LLMUnavailable, check_url
from ..docx_io.package import DocxError, DocxPackage
from ..pipeline import export_clean, finalize
from ..review.entities_file import read_entities, write_entities
from .cases import (
    CATEGORY_LABEL, MAX_UPLOAD, NEW_ENTITY, POLICY_CHOICES, Case, CaseStore, Settings, case_payload, ner_available,
    now,
)

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent / "static"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DECISIONS = {"accept", "reject", "apply", "dismiss", "open"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def create_app(workdir: Path, config_path: Path | None = None, *, token: str | None = None,
               extra_hosts: set[str] | None = None) -> FastAPI:
    workdir.mkdir(parents=True, exist_ok=True)
    settings = Settings(workdir, config_path)
    store = CaseStore(workdir, settings)
    purged = store.purge(int(settings.data.get("retention_days", 14)))
    if purged:
        log.warning("%d old case(s) deleted (retention)", purged)
    app = FastAPI(title="Anonymisierung", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = token or secrets.token_urlsafe(32)
    allowed_hosts = LOCAL_HOSTS | (extra_hosts or set())

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        if hostname not in allowed_hosts:
            return Response("Forbidden host", status_code=403)
        if request.url.path.startswith("/api/") and \
                not secrets.compare_digest(request.headers.get("x-anon-token", ""), app.state.token):
            return JSONResponse({"detail": "token"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; form-action 'self'")
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8").replace("{{TOKEN}}", app.state.token)

    def _case(case_id: str) -> Case:
        try:
            return store.get(case_id)
        except KeyError:
            raise HTTPException(404, "Fall nicht gefunden") from None

    def _ready(case: Case) -> Case:
        if case.state["status"] not in ("ready", "exported"):
            raise HTTPException(409, "Analyse noch nicht abgeschlossen")
        return case

    def _changed(case: Case) -> None:
        """A reviewer edit makes an earlier export stale."""
        with case.lock:
            if case.state["status"] == "exported":
                case.state["status"] = "ready"
                case.state.pop("exported", None)
                (case.path / "clean.docx").unlink(missing_ok=True)
                case.save()

    # -- status & settings ----------------------------------------------------
    @app.get("/api/status")
    def status() -> dict:
        cfg = settings.config()
        llm = cfg.rules["llm"]
        info = {"enabled": bool(llm.get("enabled")), "model": llm.get("model"), "base_url": llm.get("base_url"),
                "reachable": False, "error": None, "model_found": None}
        if info["enabled"]:
            try:
                client = LLMClient.from_config(cfg)
                models = client.models()
                info["reachable"] = True
                info["model_found"] = (not models) or llm.get("model") in models
            except (LLMUnavailable, HostNotAllowed) as exc:
                info["error"] = str(exc)
        return {"ner": ner_available(cfg) and settings.data.get("use_ner", True), "llm": info,
                "cases": len(store.list())}

    @app.get("/api/settings")
    def get_settings() -> dict:
        cfg = settings.config()
        return {**settings.data, "choices": POLICY_CHOICES,
                "allowed_hosts": cfg.rules["llm"].get("allowed_hosts", []), "ner_available": ner_available(cfg)}

    @app.put("/api/settings")
    def put_settings(data: dict = Body(...)) -> dict:
        try:
            settings.update(data)
        except HostNotAllowed as exc:
            raise HTTPException(400, f"Adresse nicht erlaubt: {exc}") from None
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None
        return get_settings()

    @app.post("/api/llm/models")
    def llm_models(data: dict = Body(...)) -> dict:
        cfg = settings.config()
        hosts = cfg.rules["llm"].get("allowed_hosts", [])
        try:
            url = check_url(str(data.get("base_url", "")), hosts)
            models = LLMClient(url, str(data.get("model") or "x"), hosts, timeout=5).models()
            return {"ok": True, "models": models}
        except HostNotAllowed as exc:
            return {"ok": False, "error": f"Adresse nicht erlaubt ({exc}). Erlaubt: {', '.join(hosts)}"}
        except LLMUnavailable as exc:
            return {"ok": False, "error": f"Keine Verbindung ({exc}). Läuft Ollama/vLLM?"}

    # -- cases ------------------------------------------------------------------
    @app.get("/api/cases")
    def list_cases() -> list[dict]:
        return store.list()

    @app.post("/api/cases")
    async def upload(file: UploadFile = File(...), use_llm: bool = Form(False)) -> dict:
        name = Path(file.filename or "Dokument.docx").name
        if not name.lower().endswith(".docx"):
            raise HTTPException(400, "Bitte eine Word-Datei (.docx) hochladen.")
        data = await file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "Datei zu gross (max. 50 MB).")
        try:
            DocxPackage.from_bytes(data).main_part()
        except DocxError as exc:
            raise HTTPException(400, str(exc)) from None
        case = store.create(name, data, use_llm=use_llm and settings.data["llm"]["enabled"])
        return case.summary()

    @app.get("/api/cases/{case_id}")
    def case_status(case_id: str) -> dict:
        return _case(case_id).summary()

    @app.delete("/api/cases/{case_id}")
    def delete_case(case_id: str) -> dict:
        _case(case_id)
        store.delete(case_id)
        return {"ok": True}

    @app.get("/api/cases/{case_id}/review")
    def review(case_id: str) -> dict:
        case = _ready(_case(case_id))
        return case_payload(case, settings.config())

    @app.post("/api/cases/{case_id}/decisions")
    def decide(case_id: str, data: dict = Body(...)) -> dict:
        case = _ready(_case(case_id))
        decision = data.get("decision")
        keys = [str(k) for k in data.get("keys", [])]
        if decision not in DECISIONS or not keys:
            raise HTTPException(400, "Ungültige Entscheidung")
        with case.lock:
            decisions = case.state.setdefault("decisions", {})
            for k in keys:
                if decision == "open":
                    decisions.pop(k, None)
                else:
                    decisions[k] = decision
            case.save()
        _changed(case)
        payload = case_payload(case, settings.config())
        case.save()
        return {"decisions": {m["key"]: m["decision"] for m in payload["mentions"]},
                "open_questions": payload["open_questions"]}

    @app.post("/api/cases/{case_id}/entities/{entity_id}")
    def entity_action(case_id: str, entity_id: str, data: dict = Body(...)) -> dict:
        case = _ready(_case(case_id))
        cfg = settings.config()
        a = case.analysis(cfg)
        ent = {e.id: e for e in a.entities}.get(entity_id)
        if ent is None:
            raise HTTPException(404, "Entität nicht gefunden")
        action, value = data.get("action"), data.get("value")
        forms = list(ent.forms)
        if not forms:
            raise HTTPException(400, "Diese Zeile kann nur als Ganzes angenommen/abgelehnt werden.")
        if action == "replacement":
            value = str(value or "").strip()
            if not value or len(value) > 40:
                raise HTTPException(400, "Ersetzung 1–40 Zeichen")
            case.override(forms, ent.category, replacement=value)
        elif action in ("keep", "anonymise"):
            case.override(forms, ent.category, decision="keep" if action == "keep" else "auto")
        elif action == "merge":
            target = {e.id: e for e in a.entities}.get(str(value))
            if target is None or target is ent or target.category != ent.category:
                raise HTTPException(400, "Zusammenführen nur innerhalb derselben Kategorie")
            case.override(list(target.forms), target.category)
            case.override(forms, ent.category, merge_with=list(target.forms))
        elif action == "decide_all":
            if value not in DECISIONS:
                raise HTTPException(400, "Ungültige Entscheidung")
            from ..pipeline import mention_key  # noqa: PLC0415

            with case.lock:
                d = case.state.setdefault("decisions", {})
                for m in a.mentions:
                    if m.span.meta.get("entity") == ent.id:
                        if value == "open":
                            d.pop(mention_key(m.span), None)
                        else:
                            d[mention_key(m.span)] = value
                case.save()
        else:
            raise HTTPException(400, "Unbekannte Aktion")
        _changed(case)
        return case_payload(case, cfg)

    def _span(case: Case, data: dict) -> tuple[str, int, int, int, str]:
        cfg = settings.config()
        a = case.analysis(cfg)
        try:
            part, para = str(data["pid"]).rsplit("|", 1)
            p = a.doc.get(part, int(para))
            start, end = int(data["start"]), int(data["end"])
        except (KeyError, ValueError):
            raise HTTPException(400, "Ungültige Textstelle") from None
        if not (0 <= start < end <= len(p.text)):
            raise HTTPException(400, "Ungültige Textstelle")
        return part, int(para), start, end, p.text[start:end]

    @app.post("/api/cases/{case_id}/entities")
    def new_entity(case_id: str, data: dict = Body(...)) -> dict:
        case = _ready(_case(case_id))
        cfg = settings.config()
        kind = data.get("type")
        if kind not in NEW_ENTITY:
            raise HTTPException(400, "Unbekannte Kategorie")
        _, _, _, _, text = _span(case, data)
        text = " ".join(text.split()).strip(" ,;:.()\"'„“")
        if kind == "org":
            legal = "|".join(sorted(cfg.detect["legal_forms"], key=len, reverse=True))
            text = re.sub(rf"\s+(?:{legal})$", "", text).strip()
        if len(text) < 2:
            raise HTTPException(400, "Bitte mindestens zwei Zeichen markieren.")
        category, ent_kind = NEW_ENTITY[kind]
        case.override([text], category, kind=ent_kind, decision="auto")
        _changed(case)
        return case_payload(case, cfg)

    @app.post("/api/cases/{case_id}/manual")
    def add_manual(case_id: str, data: dict = Body(...)) -> dict:
        case = _ready(_case(case_id))
        part, para, start, end, text = _span(case, data)
        replacement = str(data.get("replacement", ""))[:2000]
        with case.lock:
            manual = case.state.setdefault("manual", [])
            if any(m["part"] == part and m["para"] == para and m["start"] < end and start < m["end"] for m in manual):
                raise HTTPException(400, "Überschneidet eine andere manuelle Änderung.")
            manual.append({"part": part, "para": para, "start": start, "end": end, "replacement": replacement,
                           "text": text})
            case.save()
        _changed(case)
        return case_payload(case, settings.config())

    @app.delete("/api/cases/{case_id}/manual/{index}")
    def delete_manual(case_id: str, index: int) -> dict:
        case = _ready(_case(case_id))
        with case.lock:
            manual = case.state.setdefault("manual", [])
            if not 0 <= index < len(manual):
                raise HTTPException(404, "Nicht gefunden")
            manual.pop(index)
            case.save()
        _changed(case)
        return case_payload(case, settings.config())

    @app.post("/api/cases/{case_id}/passages")
    def passage_status(case_id: str, data: dict = Body(...)) -> dict:
        case = _ready(_case(case_id))
        if data.get("status") not in ("open", "done"):
            raise HTTPException(400, "Ungültiger Status")
        with case.lock:
            case.state.setdefault("passages", {})[str(data.get("key"))] = data["status"]
            case.save()
        return {"ok": True}

    # -- export ------------------------------------------------------------------
    @app.post("/api/cases/{case_id}/export")
    def export(case_id: str) -> dict:
        case = _ready(_case(case_id))
        cfg = settings.config()
        payload = case_payload(case, cfg)
        if payload["open_questions"]:
            return {"ok": False, "open_questions": payload["open_questions"],
                    "problems": [f"{payload['open_questions']} offene Frage(n): bitte zuerst entscheiden."]}
        result, review_bytes = export_clean(case.fresh_analysis(cfg), cfg, case.state.get("decisions", {}),
                                            case.manual())
        (case.path / "review.docx").write_bytes(review_bytes)
        lines = result.report.summary_lines()
        if result.data is None:
            hits = [{"pid": f"{h.part}|{h.para}" if h.para is not None else None, "status": h.status,
                     "category": CATEGORY_LABEL.get(h.category, h.category), "entity": h.entity}
                    for h in result.report.leaks]
            return {"ok": False, "problems": lines, "hits": hits}
        (case.path / "clean.docx").write_bytes(result.data)
        case.state["exported"] = now()
        case.state["status"] = "exported"
        case.save()
        return {"ok": True, "report": lines, "unchecked": result.report.unchecked,
                "kept": sum(1 for h in result.report.hits if h.status != "tool_replacement")}

    @app.get("/api/cases/{case_id}/download/{what}")
    def download(case_id: str, what: str):
        case = _ready(_case(case_id))
        stem = Path(case.state["name"]).stem
        if what == "clean":
            path = case.path / "clean.docx"
            if not path.exists():
                raise HTTPException(404, "Noch nicht exportiert")
            return FileResponse(path, media_type=DOCX_MIME, filename=f"{stem} anonymisiert.docx")
        if what == "review":
            path = case.path / "review.docx"
            if not path.exists():
                raise HTTPException(404, "Noch nicht exportiert")
            return FileResponse(path, media_type=DOCX_MIME, filename=f"{stem}.review.docx")
        if what == "entities":
            a = case.analysis(settings.config())
            path = case.path / "entities.xlsx"
            write_entities(path, a.entities)
            return FileResponse(path, media_type=XLSX_MIME, filename=f"{stem}.entities.xlsx")
        raise HTTPException(404, "Unbekannt")

    # -- review done in Word -------------------------------------------------------
    @app.post("/api/word/finalize")
    async def word_finalize(review: UploadFile = File(...), entities: UploadFile = File(...)):
        data = await review.read(MAX_UPLOAD + 1)
        xlsx = await entities.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD or len(xlsx) > MAX_UPLOAD:
            raise HTTPException(413, "Datei zu gross")
        with tempfile.TemporaryDirectory() as tmp:
            xp = Path(tmp) / "e.xlsx"
            xp.write_bytes(xlsx)
            try:
                rows = read_entities(xp)
            except Exception as exc:
                raise HTTPException(400, f"Entitäten-Datei ungültig ({type(exc).__name__})") from None
        try:
            result = finalize(data, rows, settings.config())
        except DocxError as exc:
            raise HTTPException(400, str(exc)) from None
        if result.data is None:
            return JSONResponse({"ok": False, "problems": result.report.summary_lines()}, status_code=422)
        stem = re.sub(r"\.review$", "", Path(review.filename or "Dokument").stem)
        name = f"{stem} anonymisiert.docx"
        ascii_name = name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
        return Response(result.data, media_type=DOCX_MIME, headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"})

    app.state.store = store
    app.state.settings = settings
    return app
