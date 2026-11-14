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
from ..docx_io.reader import OBJECT, Document, Paragraph
from .span import KEEP_KIND, Span
from .text import (
    CAPS_WORD, INITIAL, NAME_LEFT, NAME_RIGHT, NAME_WORD, PARTICLE, alternation, compile_list, form_regex,
    genitive_forms, titles_regex,
)

_TOKEN = re.compile(rf"{NAME_WORD}|{CAPS_WORD}|{INITIAL}")
_PARTICLE = re.compile(rf"{PARTICLE}(?=\s+[A-ZÄÖÜ])")
_DELIM = re.compile(r"[ \t]*(?:,|;|\bund\b|\bsowie\b|\n|\t)[ \t\n]*")


# A possessive or an organisation before the trigger means an office outside
# the court: "seinen Präsidenten", "der Präsident des Verwaltungsrates".
_FOREIGN_OFFICE = re.compile(
    r"(?:\b(?:sein|seine|seinen|seinem|seiner|ihr|ihre|ihren|ihrem|ihrer|dessen|deren|unser|unseren)|"
    r"vertreten\s+durch(?:\s+(?:den|die))?)\s+$")


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
            ("citation", compile_list(kl.get("publication_places", []))),
        ]
        rb = cfg.detect["rubrum"]
        self.role_words = re.compile("(?:" + "|".join(rb["party_labels"] + rb["representative_labels"]) + ")")
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
    def _plausible_citation(self, text: str) -> bool:
        """Heuristic author/case-name matches must not consist of role words
        ("Beschwerdeführer/Antragsteller Albert Kaiser")."""
        applicant = re.split(r"\s(?:v\.?|vs\.|gegen|c\.|against)\s", text)[0]
        words = re.findall(r"[A-ZÄÖÜ][\w'-]+", applicant)
        return not any(w in self.stopwords or self.role_words.fullmatch(w) for w in words)

    def pattern_spans(self, doc: Document) -> list[Span]:
        out: list[Span] = []
        for para in doc.paragraphs:
            for reason, patterns in self.groups:
                for rx in patterns:
                    for m in rx.finditer(para.norm):
                        if m.end() <= m.start():
                            continue
                        if reason in ("cited_author", "cited_case") and not self._plausible_citation(m.group()):
                            continue
                        s, e = para.to_orig(m.start(), m.end())
                        out.append(Span(para.part, para.index, s, e, para.text[s:e], KEEP_KIND, "keep", 1.0,
                                        role=reason, meta={"strength": "strong"}))
        return out

    def is_authority(self, text: str) -> bool:
        return any(rx.match(text.strip()) for rx in self.authority_res)

    # -- judges / clerk ---------------------------------------------------------
    def _postfix_names(self, norm: str, m: re.Match, max_tokens: int):
        """Names directly before "als <Rolle>": try every plausible start
        (segment start, after ',', 'sowie', 'und') and take the one whose
        name list ends right before "als"."""
        seg = max(norm.rfind(c, 0, m.start()) for c in ";:\n(") + 1
        starts = [seg] + [x.end() for x in re.finditer(r",|\bsowie\b|\bund\b", norm[seg : m.start()])]
        for st in starts:
            st = st if st == seg else seg + st
            lead = re.compile(r"[ \t]*(?:[a-zäöü]+[ \t]+)*").match(norm, st)
            pos = lead.end()
            for rx_t in self.judge_triggers + self.clerk_triggers:
                t = rx_t.match(norm, pos)
                if t:
                    pos = t.end()
                    break
            names = [n for n in parse_name_list(norm, pos, self.titles_re, self.stopwords, max_tokens)
                     if n[1] <= m.start()]
            if names and not norm[names[-1][1] : m.start()].replace(OBJECT, "").strip():
                return names
        return []

    def extract_people(self, doc: Document) -> tuple[list[KeepPerson], list[Span]]:
        people: dict[str, KeepPerson] = {}
        spans: list[Span] = []
        max_tokens = int(self.cfg.detect.get("max_name_tokens", 4)) + 1
        label_role: str | None = None
        prev_names: tuple[Paragraph, list] | None = None
        role_only = re.compile(r"^\s*(?:(?:Der|Die)\s+)?(?:" + "|".join(
            [r"(?:Vize)?[Pp]räsident(?:in)?", r"Vorsitzende[r]?", r"(?:Ersatz|Oberst|Ober)?[Rr]ichter(?:in)?",
             r"Schriftf(?:ü|ue)hrer(?:in)?", r"Gerichtsschreiber(?:in)?"]) + r")\s*$")
        for para in doc.paragraphs:
            prev_label, label_role = label_role, None
            found_here = False
            for role, triggers in (("judge", self.judge_triggers), ("clerk", self.clerk_triggers)):
                for rx in triggers:
                    for m in rx.finditer(para.norm):
                        if _FOREIGN_OFFICE.search(para.norm[max(0, m.start() - 40) : m.start()]):
                            continue  # "vertreten durch seinen Präsidenten X": not a court member
                        names = parse_name_list(para.norm, m.end(), self.titles_re, self.stopwords, max_tokens)
                        line_start = para.norm.rfind("\n", 0, m.start()) + 1
                        at_line_start = not para.norm[line_start : m.start()].strip()
                        if not names:
                            if at_line_start and not para.norm[m.end():].strip(" \t:.,"):
                                label_role = role  # name follows in the next paragraph
                            continue
                        found_here = True
                        for ns, ne, toks in names:
                            self._add(people, spans, para, ns, ne, toks, role)
                        rest = para.norm[names[-1][1]:].strip(" \t.,;")
                        if at_line_start and not rest:
                            label_role = role  # "Richter:  Dr. A" -> names may continue below
            for role, rx in self.postfix:
                for m in rx.finditer(para.norm):
                    names = self._postfix_names(para.norm, m, max_tokens)
                    if names:
                        found_here = True
                        for ns, ne, toks in names:
                            self._add(people, spans, para, ns, ne, toks, role)
            if role_only.match(para.norm) and prev_names is not None:
                prev_para, names = prev_names
                role = "clerk" if re.search(r"Schrift|schreiber", para.norm) else "judge"
                for ns, ne, toks in names:
                    self._add(people, spans, prev_para, ns, ne, toks, role)
            if para.norm.strip():
                names = parse_name_list(para.norm, 0, self.titles_re, self.stopwords, max_tokens)
                whole = names and not para.norm[names[-1][1]:].replace(OBJECT, "").strip(" \t.,;")
                prev_names = (para, names) if whole else None
            if not found_here and prev_label is not None and para.kind == "document":
                names = parse_name_list(para.norm, 0, self.titles_re, self.stopwords, max_tokens)
                if names and not para.norm[names[-1][1]:].strip(" \t.,;"):
                    for ns, ne, toks in names:
                        self._add(people, spans, para, ns, ne, toks, prev_label)
                    label_role = prev_label
                elif not para.norm.strip():
                    label_role = prev_label  # skip empty paragraphs between label and name
        return list(people.values()), spans

    def cited_names(self, doc: Document, pattern_spans: list[Span]) -> list[Span]:
        """Applicants of cited decisions ("Hanzevacki gegen Kroatien") are kept
        wherever they occur ("Herr Hanzevacki")."""
        forms: set[str] = set()
        for k in pattern_spans:
            if k.role != "cited_case":
                continue
            applicant = re.split(r"\s(?:v\.?|vs\.|gegen|c\.|against)\s", k.text)[0]
            toks = re.findall(r"[A-ZÄÖÜ][\w'-]+", applicant)
            toks = [t for t in toks if t not in self.stopwords and not self.role_words.fullmatch(t)]
            if toks:
                forms.add(toks[-1])
                forms.add(" ".join(toks))
        out: list[Span] = []
        for form in forms:
            rx = form_regex(form)
            for para in doc.paragraphs:
                for m in rx.finditer(para.norm):
                    s_, e_ = para.to_orig(m.start(), m.end())
                    out.append(Span(para.part, para.index, s_, e_, para.text[s_:e_], KEEP_KIND, "keep", 1.0,
                                    role="cited_case", meta={"strength": "strong"}))
        return out

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
        patterns = self.pattern_spans(doc)
        return people, patterns + self.cited_names(doc, patterns) + trigger_spans + self.people_spans(doc, people)
