"""NER layer: GLiNER2 PII model (Fastino) with the urchade GLiNER PII model as
fallback, CPU only, weights loaded from ./models (never from the network).

The models are optional. If neither the Python package nor local weights are
available, NER is skipped with a warning and the rules still run.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Protocol

from ..config import Config
from ..docx_io.reader import OBJECT, Document
from .span import Span
from .text import titles_regex

log = logging.getLogger(__name__)


class NerBackend(Protocol):
    name: str

    def predict(self, text: str, labels: list[str], threshold: float) -> list[tuple[int, int, str, float]]:
        """Return (start, end, label, score) for one text chunk."""


def model_dir(cfg: Config, backend: str) -> Path:
    model_id = cfg.rules["ner"]["models"][backend]
    return Path(cfg.rules["ner"].get("models_dir", "./models")) / model_id.replace("/", "__")


def _offline_env() -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


class Gliner2Backend:
    name = "gliner2"

    def __init__(self, path: Path):
        _offline_env()
        from gliner2 import AutoExtractor  # type: ignore  # noqa: PLC0415

        self.model = AutoExtractor.from_pretrained(str(path), map_location="cpu")

    def predict(self, text, labels, threshold):
        res = self.model.extract_entities(text, labels, include_confidence=True, include_spans=True,
                                          threshold=threshold)
        out = []
        for label, items in res.get("entities", {}).items():
            for it in items:
                if isinstance(it, dict) and "start" in it:
                    out.append((it["start"], it["end"], label, float(it.get("confidence", 1.0))))
        return out


class GlinerBackend:
    name = "gliner"

    def __init__(self, path: Path):
        _offline_env()
        from gliner import GLiNER  # type: ignore  # noqa: PLC0415

        self.model = GLiNER.from_pretrained(str(path), local_files_only=True, map_location="cpu")

    def predict(self, text, labels, threshold):
        ents = self.model.predict_entities(text, labels, threshold=threshold)
        return [(e["start"], e["end"], e["label"], float(e["score"])) for e in ents]


_BACKENDS = {"gliner2": Gliner2Backend, "gliner": GlinerBackend}


def load_backend(cfg: Config) -> NerBackend | None:
    for name in cfg.rules["ner"].get("backends", ["gliner2", "gliner"]):
        path = model_dir(cfg, name)
        if not path.exists():
            log.info("NER backend %s: no local weights", name)
            continue
        try:
            return _BACKENDS[name](path)
        except ImportError:
            log.warning("NER backend %s: Python package not installed (pip install .[ner])", name)
        except Exception as exc:  # pragma: no cover - depends on model files
            log.warning("NER backend %s failed to load: %s", name, type(exc).__name__)
    return None


def _chunks(text: str, size: int) -> list[tuple[int, str]]:
    if len(text) <= size:
        return [(0, text)]
    out = []
    pos = 0
    while pos < len(text):
        end = min(len(text), pos + size)
        if end < len(text):
            cut = max(text.rfind(". ", pos, end), text.rfind("; ", pos, end), text.rfind("\n", pos, end))
            if cut > pos + size // 2:
                end = cut + 1
        out.append((pos, text[pos:end]))
        pos = end
    return out


class NerDetector:
    def __init__(self, cfg: Config, backend: NerBackend | None = None):
        self.cfg = cfg
        ner = cfg.rules["ner"]
        self.labels: dict[str, str] = ner["labels"]
        self.threshold = float(ner.get("threshold", 0.5))
        self.chunk = int(ner.get("chunk_chars", 1500))
        self.backend = backend
        self.titles = re.compile(rf"(?:{titles_regex(cfg.detect['titles'])}|"
                                 rf"(?:{'|'.join(cfg.detect['honorifics'])}))\s*")
        self.legal_end = re.compile(
            rf"\s+(?:{'|'.join(sorted(cfg.detect['legal_forms'], key=len, reverse=True))})$"
        )

    @classmethod
    def from_config(cls, cfg: Config) -> "NerDetector":
        backend = load_backend(cfg) if cfg.rules["ner"].get("enabled", True) else None
        if backend is None and cfg.rules["ner"].get("enabled", True):
            log.warning("NER unavailable: continuing with rules only (run `anon setup-models`)")
        return cls(cfg, backend)

    @property
    def available(self) -> bool:
        return self.backend is not None

    def detect(self, doc: Document) -> list[Span]:
        if self.backend is None:
            return []
        out: list[Span] = []
        labels = list(self.labels)
        for para in doc.paragraphs:
            text = para.norm
            if len(text.strip()) < 3:
                continue
            for offset, chunk in _chunks(text, self.chunk):
                for s, e, label, score in self.backend.predict(chunk, labels, self.threshold):
                    kind = self.labels.get(label)
                    if kind is None:
                        continue
                    ns, ne = offset + s, offset + e
                    span = self._clean(para, ns, ne, kind, score)
                    if span is not None:
                        out.append(span)
        log.info("NER: %d spans", len(out))
        return out

    def _clean(self, para, ns: int, ne: int, kind: str, score: float) -> Span | None:
        text = para.norm[ns:ne]
        if OBJECT in text or not text.strip():
            return None
        meta = {}
        if kind in ("person", "org"):
            while True:
                m = self.titles.match(para.norm, ns, ne)
                if not m or m.end() == ns:
                    break
                ns = m.end()
            if kind == "org":
                lf = self.legal_end.search(para.norm[ns:ne])
                if lf:
                    meta["legal_form"] = lf.group().strip()
                    ne = ns + lf.start()
            text = para.norm[ns:ne]
            if kind == "person":
                meta["tokens"] = text.split()
        while ne > ns and para.norm[ne - 1] in " ,;.:":
            ne -= 1
        if ne - ns < 2:
            return None
        s, e = para.to_orig(ns, ne)
        role = "affected" if kind in ("person", "org") else None
        return Span(para.part, para.index, s, e, para.text[s:e], kind, "ner", round(score, 3), role, meta)
