"""Keep-list: what must never be anonymised.

* static patterns from keeplist.yaml (case numbers, statute and literature
  citations, names in cited ECHR/CJEU/EFTA decisions, cited authors,
  authorities, countries, the court-seat date line), all *strong*;
* judges and clerk, extracted dynamically from trigger words
  ("Präsident Dr. Hugo Vogt", "Richter: lic. iur. A. Beck, Dr. C. Frick",
  "Schriftführerin Marion Ospelt"). Their full names are *strong* keep spans
  everywhere; their bare surnames are *weak* (a party with the same surname
  turns such a mention into a "Prüfen" comment instead of a silent keep).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import Config
from ..docx_io.reader import Document, Paragraph
from .span import KEEP_KIND, Span
from .text import (
    CAPS_WORD, INITIAL, NAME_LEFT, NAME_RIGHT, NAME_WORD, PARTICLE, alternation, compile_list, form_regex,
    genitive_forms, titles_regex,
)

_TOKEN = re.compile(rf"{NAME_WORD}|{CAPS_WORD}|{INITIAL}")
_PARTICLE = re.compile(rf"{PARTICLE}(?=\s+[A-ZÄÖÜ])")
_DELIM = re.compile(r"[ \t]*(?:,|;|\bund\b|\bsowie\b|\n|\t)[ \t\n]*")


@dataclass
class KeepPerson:
    role: str  # judge | clerk
    name: str  # without titles
    tokens: list[str] = field(default_factory=list)

    @property
    def surname(self) -> str:
        return self.tokens[-1]

    @property
    def forms_strong(self) -> list[str]:
        if len(self.tokens) < 2:
            return []  # a bare surname is only a strong keep where its trigger stands
        out = [self.name]
        if len(self.tokens) > 1:
            initials = " ".join(t[0] + "." for t in self.tokens[:-1] if not t.endswith("."))
            out.append(f"{initials} {self.surname}")
        return out

    @property
    def forms_weak(self) -> list[str]:
        return [self.surname] + genitive_forms(self.surname)


def parse_name_list(norm: str, pos: int, titles_re: re.Pattern, stopwords: set[str], max_tokens: int = 5):
    """Parse "Dr. Hugo Vogt, lic. iur. Thomas Hasler und Dr. N. Beck" starting
    at pos. Returns [(start, end, tokens)] in normalised offsets."""
    names = []
    i = pos
    m = re.compile(r"[ \t]*:?[ \t]*").match(norm, i)
    i = m.end()
    while True:
        j = i
        while True:
            t = titles_re.match(norm, j)
            if not t:
                break
            j = t.end()
            ws = re.compile(r"[ ]*").match(norm, j)
            j = ws.end()
        toks: list[str] = []
        start = end = None
        while True:
            m = _TOKEN.match(norm, j)
            if m is None and toks:
                m = _PARTICLE.match(norm, j)
            if m is None or m.group() in stopwords:
                break
            toks.append(m.group())
            start = m.start() if start is None else start
            end = m.end()
            ws = re.compile(r" +").match(norm, end)
            if ws is None:
                break
            nxt = _TOKEN.match(norm, ws.end()) or _PARTICLE.match(norm, ws.end())
            if nxt is None or nxt.group() in stopwords:
                break
            j = ws.end()
        real = [t for t in toks if not t.endswith(".")]
        if not real or len(toks) > max_tokens or toks[-1].endswith("."):
            break
        names.append((start, end, toks))
        d = _DELIM.match(norm, end)
        if d is None:
            break
        i = d.end()
    return names


class KeepList:
    def __init__(self, cfg: Config):
        kl = cfg.keeplist
        self.cfg = cfg
        self.groups: list[tuple[str, list[re.Pattern]]] = [
            ("case_number", compile_list(kl.get("case_numbers", []))),
            ("citation", compile_list(kl.get("citations", []))),
            ("cited_case", compile_list(kl.get("cited_case_names", []))),
            ("cited_author", compile_list(kl.get("cited_authors", []))),
            ("authority", compile_list(kl.get("authorities", []))),
            ("static", [re.compile(NAME_LEFT + p + NAME_RIGHT) for p in kl.get("static", [])]),
            ("court_seat", compile_list(kl.get("court_seat_lines", []), re.M)),
        ]
        self.authority_res = compile_list(kl.get("authorities", []))
        if cfg.policy.get("keep_municipal_authorities", True):
            prefixes = "(?:" + "|".join(kl.get("municipal_authority_prefixes", [])) + ")"
            places = alternation([e.form for e in cfg.gazetteers.places if e.kind == "place"])
            muni = re.compile(rf"\b{prefixes}\s+{places}{NAME_RIGHT}")
            self.groups.append(("authority", [muni]))
            self.authority_res.append(muni)
        self.judge_triggers = compile_list(kl["judges"]["triggers"])
        self.clerk_triggers = compile_list(kl["judges"]["clerk_triggers"])
        self.postfix = [(role, re.compile(rx)) for role, rx in kl["judges"].get("postfix_roles", {}).items()]
        self.titles_re = re.compile(titles_regex(cfg.detect["titles"]) + r"(?=[\s,]|$)")
        self.stopwords = cfg.gazetteers.stopwords

    # -- static ---------------------------------------------------------------
    def pattern_spans(self, doc: Document) -> list[Span]:
        out: list[Span] = []
        for para in doc.paragraphs:
            for reason, patterns in self.groups:
                for rx in patterns:
                    for m in rx.finditer(para.norm):
                        if m.end() <= m.start():
                            continue
                        s, e = para.to_orig(m.start(), m.end())
                        out.append(Span(para.part, para.index, s, e, para.text[s:e], KEEP_KIND, "keep", 1.0,
                                        role=reason, meta={"strength": "strong"}))
        return out

    def is_authority(self, text: str) -> bool:
        return any(rx.match(text.strip()) for rx in self.authority_res)

    # -- judges / clerk ---------------------------------------------------------
    def extract_people(self, doc: Document) -> tuple[list[KeepPerson], list[Span]]:
        people: dict[str, KeepPerson] = {}
        spans: list[Span] = []
        max_tokens = int(self.cfg.detect.get("max_name_tokens", 4)) + 1
        label_role: str | None = None
        for para in doc.paragraphs:
            prev_label, label_role = label_role, None
            found_here = False
            for role, triggers in (("judge", self.judge_triggers), ("clerk", self.clerk_triggers)):
                for rx in triggers:
                    for m in rx.finditer(para.norm):
                        names = parse_name_list(para.norm, m.end(), self.titles_re, self.stopwords, max_tokens)
                        if not names:
                            continue
                        found_here = True
                        for ns, ne, toks in names:
                            self._add(people, spans, para, ns, ne, toks, role)
                        line_start = para.norm.rfind("\n", 0, m.start()) + 1
                        rest = para.norm[names[-1][1]:].strip(" \t.,;")
                        if not para.norm[line_start : m.start()].strip() and not rest:
                            label_role = role  # "Richter:  Dr. A" -> names may continue below
            for role, rx in self.postfix:
                for m in rx.finditer(para.norm):
                    seg = max(para.norm.rfind(c, 0, m.start()) for c in ";:\n(") + 1
                    lead = re.compile(r"[ \t]*(?:[a-zäöü]+[ \t]+)*").match(para.norm, seg)
                    pos = lead.end()
                    for rx_t in self.judge_triggers + self.clerk_triggers:
                        t = rx_t.match(para.norm, pos)
                        if t:
                            pos = t.end()
                            break
                    names = parse_name_list(para.norm, pos, self.titles_re, self.stopwords, max_tokens)
                    names = [n for n in names if n[1] <= m.start()]
                    if names and not para.norm[names[-1][1] : m.start()].strip():
                        found_here = True
                        for ns, ne, toks in names:
                            self._add(people, spans, para, ns, ne, toks, role)
            if not found_here and prev_label is not None and para.kind == "document":
                names = parse_name_list(para.norm, 0, self.titles_re, self.stopwords, max_tokens)
                if names and not para.norm[names[-1][1]:].strip(" \t.,;"):
                    for ns, ne, toks in names:
                        self._add(people, spans, para, ns, ne, toks, prev_label)
                    label_role = prev_label
        return list(people.values()), spans

    def _add(self, people, spans, para: Paragraph, ns: int, ne: int, toks: list[str], role: str) -> None:
        name = " ".join(toks)
        known = [p for p in people.values() if p.surname == toks[-1]]
        if len(toks) == 1 and known:
            name = known[0].name  # "Präsident Vogt" refers to "Dr. Hugo Vogt"
        elif name not in people:
            people[name] = KeepPerson(role, name, toks)
        s, e = para.to_orig(ns, ne)
        spans.append(Span(para.part, para.index, s, e, para.text[s:e], KEEP_KIND, "keep", 1.0, role=role,
                          meta={"strength": "strong", "person": name}))

    def people_spans(self, doc: Document, people: list[KeepPerson]) -> list[Span]:
        out: list[Span] = []
        for person in people:
            for strength, forms in (("strong", person.forms_strong), ("weak", person.forms_weak)):
                for form in forms:
                    rx = form_regex(form)
                    for para in doc.paragraphs:
                        for m in rx.finditer(para.norm):
                            s, e = para.to_orig(m.start(), m.end())
                            out.append(Span(para.part, para.index, s, e, para.text[s:e], KEEP_KIND, "keep", 1.0,
                                            role=person.role, meta={"strength": strength, "person": person.name}))
        return out

    def detect(self, doc: Document) -> tuple[list[KeepPerson], list[Span]]:
        people, trigger_spans = self.extract_people(doc)
        return people, self.pattern_spans(doc) + trigger_spans + self.people_spans(doc, people)
