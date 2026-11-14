"""Configuration loading: rules.yaml + keeplist.yaml + gazetteers/*.txt.

A user config (``--config my.yaml``) is deep-merged over the packaged
defaults. It may contain a top-level ``keeplist`` key (merged over
keeplist.yaml) and a ``gazetteer_dirs`` list of extra directories whose
``*.txt`` files are appended to the packaged gazetteers of the same name.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parent
GAZETTEER_DIR = CONFIG_DIR / "gazetteers"


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


@dataclass(frozen=True)
class GazetteerEntry:
    form: str
    ambiguous: bool
    kind: str  # "place" | "place_adj" | "fieldname" | "product"
    base: str  # the place this form belongs to


@dataclass
class Gazetteers:
    places: list[GazetteerEntry] = field(default_factory=list)
    fieldnames: list[GazetteerEntry] = field(default_factory=list)
    products: list[GazetteerEntry] = field(default_factory=list)
    first_names: set[str] = field(default_factory=set)
    stopwords: set[str] = field(default_factory=set)
    street_generic: set[str] = field(default_factory=set)
    surname_nouns: set[str] = field(default_factory=set)

    @property
    def municipalities(self) -> list[str]:
        return [e.form for e in self.places if e.kind == "place"]


def _read_lines(paths: list[Path]) -> list[str]:
    lines: list[str] = []
    for p in paths:
        if not p.exists():
            continue
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                lines.append(line)
    return lines


def _parse_place_lines(lines: list[str]) -> list[GazetteerEntry]:
    entries: list[GazetteerEntry] = []
    for line in lines:
        parts = [p.strip() for p in line.split("|")]
        base_raw = parts[0]
        base = base_raw.lstrip("?").strip()
        entries.append(GazetteerEntry(base, base_raw.startswith("?"), "place", base))
        for adj in parts[1:]:
            if not adj:
                continue
            form = adj.lstrip("?").strip()
            entries.append(GazetteerEntry(form, adj.startswith("?"), "place_adj", base))
    return entries


def _simple_entries(lines: list[str], kind: str) -> list[GazetteerEntry]:
    out = []
    for line in lines:
        form = line.lstrip("?").strip()
        out.append(GazetteerEntry(form, line.startswith("?"), kind, form))
    return out


def load_gazetteers(extra_dirs: list[Path] | None = None) -> Gazetteers:
    dirs = [GAZETTEER_DIR] + [Path(d) for d in (extra_dirs or [])]

    def files(name: str) -> list[Path]:
        return [d / name for d in dirs]

    return Gazetteers(
        places=_parse_place_lines(_read_lines(files("places_li.txt")) + _read_lines(files("places_foreign.txt"))),
        fieldnames=_simple_entries(_read_lines(files("fieldnames.txt")), "fieldname"),
        products=_simple_entries(_read_lines(files("products.txt")), "product"),
        first_names=set(_read_lines(files("first_names.txt"))),
        stopwords=set(_read_lines(files("name_stopwords.txt"))),
        street_generic=set(_read_lines(files("street_generic.txt"))),
        surname_nouns=set(_read_lines(files("surname_nouns.txt"))),
    )


@dataclass
class Config:
    rules: dict[str, Any]
    keeplist: dict[str, Any]
    gazetteers: Gazetteers

    # Convenience accessors -------------------------------------------------
    @property
    def policy(self) -> dict[str, Any]:
        return self.rules["policy"]

    @property
    def detect(self) -> dict[str, Any]:
        return self.rules["detect"]

    @property
    def categories(self) -> dict[str, dict[str, Any]]:
        return self.rules["categories"]

    @property
    def stars(self) -> str:
        return self.policy.get("stars", "***")

    @property
    def review_threshold(self) -> float:
        return float(self.policy.get("review_threshold", 0.6))

    def author(self, category: str) -> str:
        return self.categories[category]["author"]

    def highlight(self, category: str) -> str | None:
        return self.categories[category].get("highlight")

    @property
    def tool_authors(self) -> set[str]:
        return {c["author"] for c in self.categories.values()}

    @property
    def tool_highlights(self) -> dict[str, str]:
        return {c["author"]: c["highlight"] for c in self.categories.values() if c.get("highlight")}


def load_config(user_config: str | Path | None = None) -> Config:
    rules = yaml.safe_load((CONFIG_DIR / "rules.yaml").read_text(encoding="utf-8"))
    keeplist = yaml.safe_load((CONFIG_DIR / "keeplist.yaml").read_text(encoding="utf-8"))
    extra_dirs: list[Path] = []
    if user_config:
        path = Path(user_config)
        user = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if "keeplist" in user:
            keeplist = _deep_merge(keeplist, user.pop("keeplist"))
        extra_dirs = [path.parent / d for d in user.pop("gazetteer_dirs", [])]
        rules = _deep_merge(rules, user)
    return Config(rules=rules, keeplist=keeplist, gazetteers=load_gazetteers(extra_dirs))
