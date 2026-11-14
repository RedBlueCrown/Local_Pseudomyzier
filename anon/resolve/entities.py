"""Cluster mentions into entities and find every further occurrence.

Persons cluster by surname with compatible given names (full name, initial +
surname, title + surname, genitive -s, maiden name). A bare surname joins the
single entity with that surname; if several share it, the mention becomes a
"Prüfen" comment. Organisations cluster by their name part (legal form
excluded). Values (places, plots, IDs, ...) form one entity per value.

After clustering every surface form (and its genitive) is searched again in
all story parts, whole-token only, so nothing outside the detector hits is
missed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import Config
from ..detect.span import NAME_KINDS, VALUE_CATEGORY, Span
from ..detect.merge import ROLE_PRIORITY
from ..detect.text import (
    NAME_LEFT, NAME_RIGHT, NUM_LEFT, NUM_RIGHT, PARTICLES, alternation, case_variants, genitive_forms,
    is_numeric_form, preceded_by_article,
)
from ..docx_io.reader import Document

VALUE_ENTITY_KIND = {
    "place": "place", "place_adj": "place", "fieldname": "place",
    "street": "address", "address": "address", "address_tail": "address",
    "plot": "plot", "birthdate": "birthdate", "id": "id", "product": "id",
}


@dataclass
class Mention:
    span: Span
    genitive: bool = False
    ambiguous: list[str] = field(default_factory=list)
    replacement: str | None = None
    change: bool = True
    comment: str | None = None
    item_id: int = 0
    order: tuple = ()


@dataclass
class Entity:
    id: str
    kind: str  # person | org | representative | place | address | plot | birthdate | id
    category: str  # party | representative | place | plot | id
    role: str | None
    forms: list[str] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)  # persons: fullest name tokens
    legal_form: str | None = None
    activity: str = ""
    sources: set[str] = field(default_factory=set)
    confidence: float = 0.0
    replacement: str = ""
    replacement_locked: bool = False
    decision: str = "auto"  # auto | review | keep
    decision_locked: bool = False
    mentions: list[Mention] = field(default_factory=list)
    leak_terms: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    first_order: tuple = ()
    llm_role: str | None = None
    force_review: bool = False

    @property
    def surname(self) -> str:
        return _surname(self.tokens) if self.tokens else ""

    @property
    def label(self) -> str:
        return self.forms[0] if self.forms else ""

    def add_form(self, form: str) -> None:
        form = re.sub(r"\s+", " ", form).strip()
        if form and form not in self.forms:
            self.forms.append(form)

    def sensitive_terms(self) -> list[str]:
        """Everything the leak check must not find in the clean export."""
        out = list(self.forms)
        if self.kind in ("person", "representative") and self.tokens:
            out.append(self.surname)
            out.append(self.surname.split()[-1])
        if self.kind == "id":
            for f in self.forms:  # "+423 232 12 34" also as "+4232321234"
                compact = re.sub(r"[\s./()-]", "", f)
                if compact != f and len(compact) >= 6:
                    out.append(compact)
        if self.kind in ("person", "representative"):
            out += [g for f in list(out) if not re.search(r"[&+]", f) for g in genitive_forms(f)]
        out += [f.replace("ß", "ss") for f in out if "ß" in f]
        out += self.leak_terms
        return [t for t in dict.fromkeys(out) if len(t.strip()) >= 2]


_PARTICLE_SET = {p for p in PARTICLES}


def _surname(tokens: list[str]) -> str:
    if not tokens:
        return ""
    i = len(tokens) - 1
    while i > 0 and tokens[i - 1].lower() in _PARTICLE_SET:
        i -= 1
    return " ".join(tokens[i:])


def _given(tokens: list[str]) -> list[str]:
    sur = _surname(tokens).split()
    return tokens[: len(tokens) - len(sur)]


def _given_compatible(a: list[str], b: list[str]) -> bool:
    if not a or not b:
        return True
    x, y = a[0], b[0]
    if x == y:
        return True
    if x.endswith(".") or y.endswith("."):
        return x[0] == y[0]
    return False


def _category(kind: str, role: str | None) -> str:
    if kind in ("person", "org", "representative"):
        return "representative" if role == "representative" else "party"
    return VALUE_CATEGORY.get(kind, "id")


def _value_key(span: Span) -> tuple[str, str]:
    kind = VALUE_ENTITY_KIND[span.kind]
    text = re.sub(r"\s+", " ", span.text).strip()
    if kind == "place":
        return kind, (span.meta.get("base") or text).casefold()
    if kind == "plot":
        return kind, span.meta.get("number", text)
    if kind in ("id", "birthdate"):
        return kind, re.sub(r"[\s]", "", text).casefold()
    return kind, text.casefold()


class EntityBuilder:
    def __init__(self, cfg: Config, doc: Document):
        self.cfg = cfg
        self.doc = doc
        self.entities: list[Entity] = []
        self._values: dict[tuple[str, str], Entity] = {}
        self._orgs: dict[str, Entity] = {}
        self._n = 0

    def _new(self, kind: str, role: str | None) -> Entity:
        self._n += 1
        e = Entity(id=f"E{self._n}", kind=kind, category=_category(kind, role), role=role)
        self.entities.append(e)
        return e

    def _order(self, span: Span) -> tuple:
        return self.doc.get(span.part, span.para).order_key(span.start)

    def _attach(self, e: Entity, span: Span, genitive: bool = False, ambiguous: list[str] | None = None) -> Mention:
        m = Mention(span, genitive=genitive, ambiguous=ambiguous or [], order=self._order(span))
        span.meta["entity"] = e.id
        span.meta["genitive"] = genitive
        if ambiguous:
            span.meta["ambiguous"] = ambiguous
            span.meta.setdefault("note", "mehrdeutig: " + ", ".join(ambiguous))
            span.confidence = min(span.confidence, 0.3)
        e.mentions.append(m)
        e.sources.add(span.source)
        if span.source != "rescan" and not ambiguous:
            e.confidence = max(e.confidence, span.confidence)
        if ROLE_PRIORITY.get(span.role, 0) > ROLE_PRIORITY.get(e.role, 0) and e.kind in ("person", "org"):
            e.role = span.role
            e.category = _category(e.kind, e.role)
        return m

    # -- building ----------------------------------------------------------
    def build(self, spans: list[Span]) -> list[Entity]:
        spans = sorted(spans, key=self._order)
        reps = [s for s in spans if s.kind == "rep_block"]
        persons = [s for s in spans if s.kind == "person"]
        orgs = [s for s in spans if s.kind == "org"]
        values = [s for s in spans if s.kind not in NAME_KINDS]
        for s in reps:
            self._add_rep(s)
        full = [s for s in persons if len(self._tokens(s)) > 1 and not s.meta.get("maiden")]
        partial = [s for s in persons if s not in full and not s.meta.get("maiden")]
        for s in full:
            self._add_person(s)
        for s in partial:
            self._add_person(s)
        for s in persons:
            if s.meta.get("maiden"):
                self._add_maiden(s)
        for s in orgs:
            self._add_org(s)
        for s in values:
            key = _value_key(s)
            e = self._values.get(key)
            if e is None:
                e = self._new(key[0], None)
                self._values[key] = e
            e.add_form(s.text)
            self._attach(e, s)
        return self.entities

    @staticmethod
    def _tokens(span: Span) -> list[str]:
        toks = span.meta.get("tokens") or span.text.split()
        return [t for t in toks if t]

    def _person_candidates(self, surname: str) -> list[Entity]:
        """Entities with this surname; particles are ignored ("von Kaiser" ~ "Kaiser")."""
        sn = surname.split()[-1].casefold() if surname else ""
        return [e for e in self.entities if e.kind in ("person", "representative") and e.tokens
                and e.surname.split()[-1].casefold() == sn]

    def _add_rep(self, s: Span) -> None:
        name = s.meta.get("name", s.text)
        toks = s.meta.get("person_tokens")
        existing = None
        if toks:
            cands = [e for e in self._person_candidates(_surname(toks)) if _given_compatible(_given(toks), _given(e.tokens))]
            existing = cands[0] if cands else None
        else:
            existing = next((e for e in self.entities if e.kind == "representative"
                             and name.casefold() in (f.casefold() for f in e.forms)), None)
        e = existing or self._new("representative", "representative")
        e.role, e.category = "representative", "representative"
        if toks and len(toks) >= len(e.tokens):
            e.tokens = list(toks)
        for f in s.meta.get("forms", [name]):
            e.add_form(f)
        if toks:
            e.add_form(" ".join(toks))
        self._attach(e, s)

    def _add_person(self, s: Span) -> None:
        toks = self._tokens(s)
        surname = _surname(toks)
        given = _given(toks)
        cands = self._person_candidates(surname)
        genitive = False
        if not cands and len(toks) == 1 and surname.endswith("s"):
            gen = self._person_candidates(surname[:-1])
            if len(gen) == 1:
                cands, genitive = gen, True
        compatible = [e for e in cands if _given_compatible(given, _given(e.tokens))]
        if given:
            exact = [e for e in compatible if _given(e.tokens) and _given(e.tokens)[0] == given[0]]
            compatible = exact or compatible
        if len(compatible) == 1 or (compatible and given):
            e = compatible[0]
            ambiguous: list[str] = []
        elif len(compatible) > 1:
            e = compatible[0]
            ambiguous = [c.id for c in compatible]
        else:
            role = s.role if s.role in ROLE_PRIORITY else "affected"
            e = self._new("representative" if role == "representative" else "person", role)
            ambiguous = []
        if len(toks) > len(e.tokens) and not genitive:
            e.tokens = list(toks)
        if not genitive:
            e.add_form(" ".join(toks))
            if s.meta.get("maiden"):
                e.add_form(s.meta["maiden"])
        if s.meta.get("maiden") and isinstance(s.meta["maiden"], str):
            e.add_form(s.meta["maiden"])
        self._attach(e, s, genitive=genitive, ambiguous=ambiguous)

    def _add_maiden(self, s: Span) -> None:
        para = self.doc.get(s.part, s.para)
        before = [e for e in self.entities if e.kind == "person" and any(
            m.span.key == s.key and 0 <= s.start - m.span.end <= 12 for m in e.mentions)]
        if before:
            e = before[0]
            e.add_form(s.text)
            self._attach(e, s)
        else:
            self._add_person(Span(s.part, s.para, s.start, s.end, para.text[s.start:s.end], "person", s.source,
                                  s.confidence, s.role, {"tokens": [s.text]}))

    def _add_org(self, s: Span) -> None:
        key = re.sub(r"\s+", " ", s.text).strip().casefold()
        e = self._orgs.get(key)
        if e is None:
            e = self._new("org", s.role or "affected")
            self._orgs[key] = e
        e.add_form(s.text)
        if s.meta.get("legal_form") and not e.legal_form:
            e.legal_form = s.meta["legal_form"]
        if s.meta.get("activity"):
            e.activity = s.meta["activity"]
        self._attach(e, s)


# ---------------------------------------------------------------------------
# Rescan
# ---------------------------------------------------------------------------
def _search_forms(e: Entity) -> list[tuple[str, bool, str]]:
    """(form, genitive, form_type) for whole-token search."""
    out: list[tuple[str, bool, str]] = []
    if e.kind in ("person", "representative", "org"):
        forms = list(e.forms)
        if e.kind != "org" and e.tokens:
            given = _given(e.tokens)
            sur = e.surname
            if given:
                forms.append(f"{given[0][0]}. {sur}")
                forms.append(" ".join(e.tokens))
            forms.append(sur)
        for f in dict.fromkeys(forms):
            ftype = "surname" if (e.tokens and f == e.surname and len(e.tokens) > 1) else "name"
            out.append((f, False, ftype))
            if e.kind != "org" and not re.search(r"[&+]", f):
                for g in genitive_forms(f):
                    out.append((g, True, ftype))
    else:
        for f in e.forms:
            out.append((f, False, "value"))
    return [(f, g, t) for f, g, t in out if len(f.strip()) >= 2]


def rescan(doc: Document, entities: list[Entity], cfg: Config, plot_head: re.Pattern | None) -> list[Span]:
    names: dict[str, list[tuple[Entity, bool, str]]] = {}
    numbers: dict[str, list[tuple[Entity, bool, str]]] = {}
    for e in entities:
        if e.decision == "keep" or e.kind == "address" and not e.forms:
            continue
        for form, gen, ftype in _search_forms(e):
            target = numbers if is_numeric_form(form) else names
            for variant in (case_variants(form) if target is names else [form]):
                bucket = target.setdefault(variant, [])
                if not any(x[0] is e for x in bucket):
                    bucket.append((e, gen, ftype))
    out: list[Span] = []
    name_re = re.compile(NAME_LEFT + alternation(list(names)) + NAME_RIGHT) if names else None
    num_re = re.compile(NUM_LEFT + alternation(list(numbers)) + NUM_RIGHT) if numbers else None
    nouns = cfg.gazetteers.surname_nouns
    rescan_plots = cfg.policy.get("rescan_plot_numbers", True)
    for para in doc.paragraphs:
        n = para.norm
        for rx, table in ((name_re, names), (num_re, numbers)):
            if rx is None:
                continue
            for m in rx.finditer(n):
                key = re.sub(r"\s+", " ", m.group())
                hits = table.get(key) or table.get(m.group())
                if not hits:
                    continue
                e, gen, ftype = hits[0]
                conf = e.confidence
                meta: dict = {"entity": e.id, "genitive": gen}
                if len(hits) > 1:
                    conf = min(conf, 0.3)
                    meta["ambiguous"] = [h[0].id for h in hits]
                    meta["note"] = "mehrdeutig: " + ", ".join(h[0].id for h in hits)
                if ftype == "surname" and m.group() in nouns and preceded_by_article(n, m.start()):
                    conf = min(conf, 0.4)
                    meta["note"] = "Nachname ist auch ein Wort (z. B. „der Richter“)"
                if e.kind == "plot":
                    ctx = n[max(0, m.start() - 60) : m.start()]
                    if not (plot_head and plot_head.search(ctx)) and not re.search(r"Nr\.\s*$", ctx):
                        if not rescan_plots:
                            continue
                        conf = min(conf, 0.5)
                        meta["note"] = "Zahl wie eine Grundstücksnummer, aber ohne Grundstückskontext"
                if e.kind == "birthdate":
                    conf = min(conf, 0.7)
                s, en = para.to_orig(m.start(), m.end())
                kind = {"person": "person", "representative": "person", "org": "org"}.get(e.kind, "rescan_value")
                if kind == "rescan_value":
                    kind = {"place": "place", "address": "address", "plot": "plot", "birthdate": "birthdate",
                            "id": "id"}[e.kind]
                role = e.role if e.kind in ("person", "representative", "org") else None
                out.append(Span(para.part, para.index, s, en, para.text[s:en], kind, "rescan", conf, role, meta))
    return out


def merge_into(dst: Entity, src: Entity, note: str) -> None:
    """Move forms and mentions of src into dst; src is emptied."""
    for f in src.forms:
        dst.add_form(f)
    for t in src.leak_terms:
        if t not in dst.leak_terms:
            dst.leak_terms.append(t)
    if len(src.tokens) > len(dst.tokens) and dst.kind in ("person", "representative"):
        dst.tokens = list(src.tokens)
    for m in src.mentions:
        m.span.meta["entity"] = dst.id
        dst.mentions.append(m)
    dst.sources |= src.sources
    dst.confidence = max(dst.confidence, src.confidence)
    src.forms, src.mentions = [], []
    dst.notes.append(note)
