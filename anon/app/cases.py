"""Cases (one uploaded decision each), app settings and background analysis.

Everything lives in a local work directory (default ~/.anon):
  settings.yaml              user settings (LLM model, policy switches)
  cases/<id>/original.docx   the uploaded decision
  cases/<id>/state.json      progress, cached NER/LLM results, reviewer edits
  cases/<id>/clean.docx      after a successful export
Case data contains original values: "Abschliessen" deletes the folder, and
cases older than `retention_days` are purged at start-up.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import yaml

from ..config import Config, load_config
from ..detect.llm import HostNotAllowed, LLMClient, LLMUnavailable, check_url
from ..detect.ner import NerDetector, model_dir
from ..pipeline import Analysis, ManualEdit, analyse, mention_key, normalise_decision
from ..review.entities_file import EntityRow
from .render import Mark, render_sections

log = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "llm": {"enabled": False, "base_url": "http://localhost:11434/v1", "model": "qwen3:8b"},
    "use_ner": True,
    "policy": {"third_parties": "letters", "representatives": "stars", "birth_dates": "stars",
               "company_keep_activity": False},
    "retention_days": 14,
}
POLICY_CHOICES = {
    "third_parties": ["letters", "keep"],
    "representatives": ["stars", "letters"],
    "birth_dates": ["stars", "generalise"],
    "company_keep_activity": [True, False],
}
MAX_UPLOAD = 50 * 1024 * 1024
CATEGORY_LABEL = {"party": "Person/Firma", "representative": "Vertreter", "place": "Ort", "plot": "Grundstück",
                  "id": "Datum/ID", "manual": "Manuell"}
NEW_ENTITY = {  # UI choice -> (category, kind)
    "person": ("party", "person"), "org": ("party", "org"), "representative": ("representative", "representative"),
    "place": ("place", "place"), "plot": ("plot", "plot"), "id": ("id", "id"),
}


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class Settings:
    def __init__(self, workdir: Path, config_path: Path | None):
        self.path = workdir / "settings.yaml"
        self.config_path = config_path
        self.data = copy.deepcopy(DEFAULT_SETTINGS)
        if self.path.exists():
            user = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            for key in ("llm", "policy"):
                self.data[key].update({k: v for k, v in (user.get(key) or {}).items() if k in self.data[key]})
            for key in ("use_ner", "retention_days"):
                if key in user:
                    self.data[key] = user[key]

    def config(self) -> Config:
        cfg = load_config(self.config_path)
        cfg.rules["llm"].update(self.data["llm"])
        cfg.policy.update(self.data["policy"])
        return cfg

    def update(self, data: dict) -> None:
        cfg = load_config(self.config_path)
        llm = data.get("llm") or {}
        if "base_url" in llm:
            # the allow-list comes from the config file (IT), never from the UI
            check_url(str(llm["base_url"]), cfg.rules["llm"].get("allowed_hosts", []))
        for k in ("enabled", "base_url", "model"):
            if k in llm:
                self.data["llm"][k] = bool(llm[k]) if k == "enabled" else str(llm[k]).strip()
        for k, v in (data.get("policy") or {}).items():
            if k in POLICY_CHOICES and v in POLICY_CHOICES[k]:
                self.data["policy"][k] = v
        if "use_ner" in data:
            self.data["use_ner"] = bool(data["use_ner"])
        if "retention_days" in data:
            self.data["retention_days"] = max(1, min(365, int(data["retention_days"])))
        self.path.write_text(yaml.safe_dump(self.data, allow_unicode=True), encoding="utf-8")


# ---------------------------------------------------------------------------
class Case:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.state: dict = json.loads((path / "state.json").read_text(encoding="utf-8"))
        self._analysis: Analysis | None = None
        self._analysis_key: str | None = None

    @property
    def id(self) -> str:
        return self.state["id"]

    def save(self) -> None:
        self.state["updated"] = now()
        _write_json(self.path / "state.json", self.state)

    def original(self) -> bytes:
        return (self.path / "original.docx").read_bytes()

    def summary(self) -> dict:
        s = self.state
        return {"id": s["id"], "name": s["name"], "created": s["created"], "updated": s.get("updated"),
                "status": s["status"], "stage": s.get("stage"), "progress": s.get("progress", 0),
                "error": s.get("error"), "open": s.get("open_questions"), "exported": s.get("exported")}

    # -- reviewer edits -------------------------------------------------------
    def rows(self) -> list[EntityRow]:
        overrides = self.state.get("overrides", [])
        ids = {json.dumps(o["forms"]): f"U{i + 1}" for i, o in enumerate(overrides)}
        rows = []
        for i, o in enumerate(overrides):
            merge = ids.get(json.dumps(o.get("merge_with"))) if o.get("merge_with") else None
            rows.append(EntityRow(id=f"U{i + 1}", category=o["category"], role=None, forms=list(o["forms"]),
                                  replacement=o.get("replacement"), decision=o.get("decision"), merge_into=merge,
                                  kind=o.get("kind")))
        return rows

    def override(self, forms: list[str], category: str, **changes) -> None:
        with self.lock:
            overrides = self.state.setdefault("overrides", [])
            entry = next((o for o in overrides if o["forms"] == forms and o["category"] == category), None)
            if entry is None:
                entry = {"forms": forms, "category": category}
                overrides.append(entry)
            entry.update({k: v for k, v in changes.items() if v is not None})
            self._analysis = None
            self.save()

    def manual(self) -> list[ManualEdit]:
        return [ManualEdit(m["part"], m["para"], m["start"], m["end"], m["replacement"])
                for m in self.state.get("manual", [])]

    # -- analysis ---------------------------------------------------------------
    def analysis(self, cfg: Config) -> Analysis:
        key = json.dumps([self.state.get("overrides"), cfg.policy], sort_keys=True, default=str)
        with self.lock:
            if self._analysis is None or self._analysis_key != key:
                self._analysis = self.fresh_analysis(cfg)
                self._analysis_key = key
            return self._analysis

    def fresh_analysis(self, cfg: Config) -> Analysis:
        """Re-analysis from the original with cached NER/LLM results: no model call."""
        return analyse(self.original(), cfg, use_ner=False, ner_cache=self.state.get("ner_cache") or [],
                       llm_cache=self.state.get("llm_cache"), rows=self.rows())


class CaseStore:
    def __init__(self, workdir: Path, settings: Settings):
        self.dir = workdir / "cases"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self._cases: dict[str, Case] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analyse")
        self._ner: NerDetector | None = None
        self._ner_lock = threading.Lock()
        for p in sorted(self.dir.iterdir()):
            if (p / "state.json").exists():
                try:
                    case = Case(p)
                except (OSError, ValueError):
                    continue
                if case.state["status"] in ("queued", "running"):
                    case.state.update(status="error", error="Analyse wurde unterbrochen (App beendet).")
                    case.save()
                self._cases[case.id] = case

    def list(self) -> list[dict]:
        return sorted((c.summary() for c in self._cases.values()), key=lambda s: s["created"], reverse=True)

    def get(self, case_id: str) -> Case:
        if not re.fullmatch(r"[0-9a-f]{32}", case_id or "") or case_id not in self._cases:
            raise KeyError(case_id)
        return self._cases[case_id]

    def delete(self, case_id: str) -> None:
        case = self.get(case_id)
        shutil.rmtree(case.path, ignore_errors=True)
        del self._cases[case_id]

    def purge(self, days: int) -> int:
        limit = time.time() - days * 86400
        old = [c.id for c in self._cases.values()
               if datetime.fromisoformat(c.state.get("updated") or c.state["created"]).timestamp() < limit]
        for cid in old:
            self.delete(cid)
        return len(old)

    def create(self, name: str, data: bytes, use_llm: bool) -> Case:
        case_id = uuid.uuid4().hex
        path = self.dir / case_id
        path.mkdir()
        (path / "original.docx").write_bytes(data)
        state = {"id": case_id, "name": name, "created": now(), "updated": now(), "status": "queued",
                 "stage": "Wartet", "progress": 0.0, "error": None, "options": {"use_llm": use_llm},
                 "overrides": [], "decisions": {}, "manual": [], "passages": {}}
        _write_json(path / "state.json", state)
        case = Case(path)
        self._cases[case_id] = case
        self._executor.submit(self._run, case)
        return case

    def ner(self, cfg: Config) -> NerDetector:
        with self._ner_lock:
            if self._ner is None:
                self._ner = NerDetector.from_config(cfg)
            return self._ner

    def _run(self, case: Case) -> None:
        cfg = self.settings.config()
        last = [0.0]

        def progress(stage: str, fraction: float) -> None:
            case.state.update(stage=stage, progress=round(fraction, 3))
            if time.time() - last[0] > 0.5 or fraction >= 1:
                last[0] = time.time()
                case.save()

        try:
            case.state.update(status="running", stage="Start", progress=0.01)
            case.save()
            llm = None
            if case.state["options"].get("use_llm") and cfg.rules["llm"].get("enabled"):
                try:
                    llm = LLMClient.from_config(cfg)
                    llm.ping()
                except (LLMUnavailable, HostNotAllowed) as exc:
                    case.state.setdefault("notes", []).append(f"Sprachmodell nicht erreichbar ({exc}); ohne LLM.")
                    llm = None
            use_ner = bool(self.settings.data.get("use_ner", True))
            ner = self.ner(cfg) if use_ner else None
            a = analyse(case.original(), cfg, use_ner=use_ner, ner=ner, llm=llm, progress=progress)
            case.state.update(ner_cache=a.ner_cache, llm_cache=a.llm_cache,
                              engines={"ner": a.stats.get("ner"), "llm": a.stats.get("llm"),
                                       "model": cfg.rules["llm"]["model"] if llm else None},
                              status="ready", stage="Fertig", progress=1.0)
            case.state["open_questions"] = sum(1 for m in a.mentions if not m.change)
            case.save()
        except Exception as exc:  # report, never leak document text into the log
            log.error("analysis failed: %s", type(exc).__name__)
            case.state.update(status="error", error=_friendly_error(exc))
            case.save()


def _friendly_error(exc: Exception) -> str:
    text = str(exc)
    if "Strict" in text:
        return text
    if "zip" in text or "docx" in text.lower():
        return "Die Datei ist kein gültiges Word-Dokument (.docx)."
    return f"Analyse fehlgeschlagen ({type(exc).__name__})."


# ---------------------------------------------------------------------------
# Payload for the review screen
# ---------------------------------------------------------------------------
def _context(text: str, start: int, end: int, width: int = 60) -> list[str]:
    """Words around a question, so it can be decided from the side panel."""
    before = text[max(0, start - width):start]
    after = text[end:end + width]
    if start > width and " " in before:
        before = before[before.index(" ") + 1:]
    if end + width < len(text) and " " in after:
        after = after[:after.rindex(" ")]
    clean = lambda s: " ".join(s.replace("\ufffc", " ").split())  # noqa: E731
    pre = ("…" if start > width else "") + clean(before) + (" " if before[-1:].isspace() else "")
    post = (" " if after[:1].isspace() else "") + clean(after) + ("…" if end + width < len(text) else "")
    return [pre, post]


def case_payload(case: Case, cfg: Config) -> dict:
    a = case.analysis(cfg)
    decisions = case.state.get("decisions", {})
    manual = case.state.get("manual", [])
    ents = {e.id: e for e in a.entities}
    manual_ranges = [(m["part"], m["para"], m["start"], m["end"]) for m in manual]

    def covered(s) -> bool:
        return any(p == s.part and q == s.para and x < s.end and s.start < y for p, q, x, y in manual_ranges)

    marks: dict[tuple[str, int], list[Mark]] = {}
    mentions = []
    open_q = 0
    for m in a.mentions:
        s = m.span
        if covered(s):
            continue
        e = ents[s.meta["entity"]]
        key = mention_key(s)
        kind = "change" if m.change else "question"
        decision = normalise_decision(m, decisions.get(key))
        if kind == "question" and decision == "open":
            open_q += 1
        category = "place" if s.kind == "address_tail" else e.category
        reason = m.reason
        mentions.append({"key": key, "item": m.item_id, "entity": e.id, "category": category, "kind": kind,
                         "text": s.text, "replacement": m.replacement or "", "reason": reason,
                         "pid": f"{s.part}|{s.para}", "decision": decision,
                         "context": _context(a.doc.get(s.part, s.para).text, s.start, s.end) if not m.change else None})
        marks.setdefault(s.key, []).append(
            Mark(s.start, s.end, key, f"c-{category} k-{kind}", m.replacement or "",
                 f"{CATEGORY_LABEL.get(category, category)}: {e.label}"))
    for i, me in enumerate(manual):
        marks.setdefault((me["part"], me["para"]), []).append(
            Mark(me["start"], me["end"], f"manual:{i}", "c-manual k-manual", me["replacement"], "Manuelle Änderung"))
    passages = []
    pstate = case.state.get("passages", {})
    for p in a.passages:
        key = f"{p.part}|{p.para}|{p.start}|{p.end}"
        passages.append({"key": key, "pid": f"{p.part}|{p.para}", "start": p.start, "end": p.end,
                         "quote": a.doc.get(p.part, p.para).text[p.start:p.end], "reason": p.reason,
                         "proposal": p.proposal, "status": pstate.get(key, "open")})
    entities = []
    for e in a.entities:
        n_changes = sum(1 for m in e.mentions if m.change)
        entities.append({"id": e.id, "label": e.label or "(Zeile im Rubrum)", "forms": e.forms,
                         "category": e.category, "kind": e.kind, "role": e.role, "replacement": e.replacement,
                         "decision": e.decision, "count": len(e.mentions), "changes": n_changes,
                         "sources": sorted(e.sources), "confidence": round(e.confidence, 2), "notes": e.notes})
    case.state["open_questions"] = open_q
    return {
        "case": case.summary(),
        "sections": render_sections(a.pkg, a.doc, marks),
        "mentions": mentions,
        "entities": entities,
        "manual": [{"key": f"manual:{i}", **m} for i, m in enumerate(manual)],
        "passages": passages,
        "kept": sorted({p.name for p in a.keep_people}),
        "notes": a.notes + case.state.get("notes", []),
        "engines": case.state.get("engines", {}),
        "open_questions": open_q,
    }


def ner_available(cfg: Config) -> bool:
    import importlib.util  # noqa: PLC0415

    for backend in cfg.rules["ner"].get("backends", []):
        pkg = {"gliner2": "gliner2", "gliner": "gliner"}.get(backend, backend)
        if model_dir(cfg, backend).exists() and importlib.util.find_spec(pkg) is not None:
            return True
    return False
