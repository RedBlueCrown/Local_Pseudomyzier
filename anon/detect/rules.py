"""Regex + gazetteer recognisers (no model needed).

Everything configurable lives in rules.yaml / gazetteers. Rubrum parsing is
in rubrum.py. All matching runs on the normalised paragraph text; spans are
mapped back to original offsets.
"""

from __future__ import annotations

import re
from datetime import date

from ..config import Config
from ..docx_io.reader import Document, Paragraph
from .span import Span
from .text import (
    CAPS_WORD, INITIAL, NAME_LEFT, NAME_RIGHT, NAME_WORD, PARTICLE, UPPER, alternation, case_variants, titles_regex,
)

_COUNTRIES = r"(?:Liechtenstein|Schweiz|Österreich|Deutschland|Italien|Frankreich)"
_CANTONS = r"(?:SG|GR|ZH|AG|BE|LU|TG|AR|AI|SZ|GL|ZG|SO|BL|BS|SH|TI|VD|VS|NE|GE|JU|FR|NW|OW|UR)"
PLACE_NAME = (
    rf"(?:(?:St\.|Sankt|Bad|Klein|Gross|Ober|Unter)\s)?(?!{_COUNTRIES}\b)[{UPPER}][\w'-]*"
    rf"(?:\s(?:am|an\sder|im|ob|bei)\s[{UPPER}][\w'-]+)?(?:\s{_CANTONS}(?![\w]))?"
)
_PLACE_CONTEXT = re.compile(
    r"(?:\b(?:in|von|nach|aus|bei|ab|bis|Gemeinde|Gemeindegebiet|Ortsteil|Weiler|Dorf|Ortschaft|wohnhaft|"
    r"Wohnsitz|Sitz|ansässig|Grundbuch)\s+|\b\d{4}\s+)$"
)
_FIELD_CTX = (
    r"(?:\b(?:Gebiet|Flur|Flurname|Flurbezeichnung|Ortsteil|Ortsbezeichnung|Weiler|Quartier|Lokalität|Örtlichkeit|"
    r"Lage|Gebietsbezeichnung|Riedgebiet|Alp|Maiensäss|Rüfe)\s+)"
)
_FIELD_PREP = r"(?:\b(?:im|in|am|auf\sdem|auf\sder|beim|ob|unter|bei)\s+)"
_TOKEN = re.compile(rf"{NAME_WORD}|{CAPS_WORD}|{INITIAL}")
_PARTICLE_RE = re.compile(rf"{PARTICLE}(?=\s+[{UPPER}])")
_PARTICLE_ONLY = re.compile(PARTICLE)
_ARTICLE_BEFORE = r"(?:\b(?:der|die|des|dem|den|ein|eine|einer|einem|eines|seine[mnrs]?|ihre[mnrs]?|deren|dessen)\s+)?"


def iban_valid(raw: str) -> bool:
    s = re.sub(r"\s", "", raw).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{8,30}", s):
        return False
    moved = s[4:] + s[:4]
    digits = "".join(str(int(c, 36)) for c in moved)
    return int(digits) % 97 == 1


def year_bound(year: int) -> int:
    return (year // 10 + 1) * 10 + 1


class RuleDetector:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        d = cfg.detect
        g = cfg.gazetteers
        self.stop = g.stopwords
        self.first_names = g.first_names
        self.max_tokens = int(d.get("max_name_tokens", 4))
        titles = titles_regex(d["titles"])
        self.titles_ws = re.compile(rf"{titles}\s*")
        self.title_any = re.compile(rf"(?<![\w.]){titles}(?=\s)")
        self.honorific = re.compile(rf"\b(?:{'|'.join(d['honorifics'])})\s+")
        self.person_ctx = [(re.compile(rf"\b{_ARTICLE_BEFORE}(?:{c['pattern']})\s+"), c["role"])
                           for c in d.get("person_context", [])]
        self.legal_re = re.compile(rf"(?<![\w-])(?:{'|'.join(sorted(d['legal_forms'], key=len, reverse=True))})(?![\w-])")
        self.activity = set(d.get("company_activity_words", []))
        first = alternation(sorted(self.first_names))
        self.first_name_re = re.compile(
            rf"{NAME_LEFT}{first}(?:\s+(?:{first}|{INITIAL}))*\s+(?:{PARTICLE}\s+)?(?:{NAME_WORD}|{CAPS_WORD}){NAME_RIGHT}"
        )
        self.maiden_re = re.compile(rf"\bgeb(?:\.|orene|oren)\s+({NAME_WORD}|{CAPS_WORD})(?![\w.])")
        # places
        self.place_entries = {}
        forms = []
        for e in g.places:
            variants = [e.form]
            if e.kind == "place_adj" and e.form.endswith("er"):
                variants += [e.form + "in", e.form + "innen"]
            for v in variants:
                for cv in case_variants(v):
                    self.place_entries[cv] = e
                    forms.append(cv)
        self.place_re = re.compile(NAME_LEFT + alternation(forms) + NAME_RIGHT)
        self.field_entries = {e.form: e for e in g.fieldnames}
        self.field_re = re.compile(NAME_LEFT + alternation(list(self.field_entries)) + NAME_RIGHT) if self.field_entries else None
        self.field_quoted = re.compile(rf'(?P<ctx>{_FIELD_CTX}|{_FIELD_PREP})"(?P<name>[{UPPER}][^"\n]{{1,40}}?)"')
        self.product_re = (re.compile(NAME_LEFT + alternation([e.form for e in g.products]) + NAME_RIGHT)
                           if g.products else None)
        # streets / addresses
        st = d["streets"]
        suffix = "(?i:" + "|".join(st["suffixes"]) + ")"
        unnumbered = "(?i:" + "|".join(st["unnumbered_suffixes"]) + ")"
        mods = "(?:(?:" + "|".join(st["modifiers"]) + r")\s+)?"
        prefix_words = alternation(st["prefix_words"])
        house = r"\d{1,4}(?:\s?[a-z])?(?![\w])"
        street_name = rf"{mods}[{UPPER}][\w'.-]*?{suffix}"
        pc = d["postcodes"]
        fl = pc["fl"]
        foreign = "(?:" + "|".join(pc["foreign_prefixes"]) + ")"
        postplace = rf"(?:(?:FL-?\s?)?{fl}|{foreign}-\s?\d{{4,5}}|\d{{4,5}})\s+{PLACE_NAME}"
        self.address_re = re.compile(
            rf"{NAME_LEFT}(?:(?:{street_name}|{prefix_words}\s+[{UPPER}][\w'-]+)\s+{house}\s*,\s*{postplace}"
            rf"|{street_name}\s+{house})"
        )
        self.postplace_fl = re.compile(rf"(?<![\w-])(?:FL-?\s?)?{fl}\s+{PLACE_NAME}")
        self.postplace_foreign = re.compile(rf"(?<![\w-]){foreign}-\s?\d{{4,5}}\s+{PLACE_NAME}")
        self.postplace_ctx = re.compile(
            rf"(?:^|(?<=[\n\t,;(]))\s*(\d{{4,5}}\s+{PLACE_NAME})(?=\s*(?:$|[\n,;)]|\.\s|\.$))", re.M
        )
        self.street_unnumbered = re.compile(rf"{NAME_LEFT}[{UPPER}][\w'-]*?{unnumbered}{NAME_RIGHT}")
        self.street_generic = g.street_generic
        # plots
        pl = d["plots"]
        self.plot_head = re.compile(rf"\b{pl['prefix']}(?![\w])")
        self.plot_nr = pl["number_word"]
        self.plot_num = re.compile(rf"(?:(?P<nr>{pl['number_word']})\s*)?(?P<num>{pl['number']})(?![\w])")
        self.plot_sep = re.compile(rf"\s*{pl['separators']}\s*")
        self.plot_nr_in_sentence = re.compile(rf"{pl['number_word']}\s*({pl['number']})(?![\w])")
        # birth dates
        bd = d["birthdates"]
        self.birth_res = [re.compile(rf"(?P<trig>{t})(?P<date>{bd['date']})") for t in bd["triggers"]]
        self.birth_year_res = [re.compile(rf"(?P<trig>{t})(?P<date>(?:19|20)\d{{2}})(?![\w])") for t in bd["year_triggers"]]
        # identifiers
        self.ids = []
        for item in d.get("ids", []):
            ctx = re.compile(item["context"]) if item.get("context") else None
            self.ids.append((item["name"], re.compile(item["pattern"]), int(item.get("group", 0)), ctx,
                             int(item.get("window", 80)), float(item.get("confidence", 0.9))))
        self.iban_re = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b")

    # ------------------------------------------------------------------
    def detect(self, doc: Document) -> list[Span]:
        out: list[Span] = []
        for para in doc.paragraphs:
            if not para.norm.strip():
                continue
            for fn in (self._ids, self._birthdates, self._plots, self._addresses, self._places, self._fieldnames,
                       self._products, self._persons, self._companies):
                out.extend(fn(para))
        return out

    def _span(self, para: Paragraph, ns: int, ne: int, kind: str, conf: float, source: str = "rules",
              role: str | None = None, **meta) -> Span:
        s, e = para.to_orig(ns, ne)
        return Span(para.part, para.index, s, e, para.text[s:e], kind, source, conf, role, meta)

    # identifiers --------------------------------------------------------
    def _ids(self, para: Paragraph) -> list[Span]:
        out = []
        n = para.norm
        for name, rx, group, ctx, window, conf in self.ids:
            for m in rx.finditer(n):
                if ctx is not None and not ctx.search(n[max(0, m.start() - window) : m.start()]):
                    continue
                s, e = m.span(group)
                if e > s:
                    out.append(self._span(para, s, e, "id", conf, id_type=name))
        for m in self.iban_re.finditer(n):
            ok = iban_valid(m.group())
            out.append(self._span(para, m.start(), m.end(), "id", 0.98 if ok else 0.85, id_type="iban",
                                  checksum=ok))
        return out

    def _birthdates(self, para: Paragraph) -> list[Span]:
        out = []
        for rx in self.birth_res:
            for m in rx.finditer(para.norm):
                trig = m.group("trig")
                am = trig.rfind("am ")
                gen_start = m.start("trig") + am if am >= 0 else m.start("date")
                out.append(self._span(para, m.start("date"), m.end("date"), "birthdate", 0.95,
                                      generalise_from=para.to_orig(gen_start, gen_start)[0]))
        for rx in self.birth_year_res:
            for m in rx.finditer(para.norm):
                out.append(self._span(para, m.start("date"), m.end("date"), "birthdate", 0.9, year_only=True))
        return out

    # plots ----------------------------------------------------------------
    def _plots(self, para: Paragraph) -> list[Span]:
        out: list[Span] = []
        n = para.norm
        seen: set[int] = set()
        for head in self.plot_head.finditer(n):
            pos = head.end()
            ws = re.compile(r"\s*").match(n, pos)
            pos = ws.end()
            first = True
            while True:
                m = self.plot_num.match(n, pos)
                if m is None:
                    break
                num = m.group("num")
                if not first and re.match(r"\s+[A-ZÄÖÜ]", n[m.end("num") :]) and len(num) >= 4:
                    break  # ", 9490 Vaduz" is a postcode, not another plot
                looks_year = bool(re.fullmatch(r"(?:19|20)\d{2}", num))
                conf = 0.95 if (m.group("nr") or not looks_year) else 0.4
                out.append(self._span(para, m.start("num"), m.end("num"), "plot", conf, number=num))
                seen.add(m.start("num"))
                first = False
                sep = self.plot_sep.match(n, m.end())
                if sep is None:
                    break
                pos = sep.end()
            # further "Nr. 123" in the same sentence after the prefix
            end = re.search(r"\.\s+[A-ZÄÖÜ]|$", n[head.end() :])
            limit = head.end() + (end.start() if end else len(n))
            for m in self.plot_nr_in_sentence.finditer(n, head.end(), min(limit, head.end() + 250)):
                if m.start(1) not in seen:
                    seen.add(m.start(1))
                    out.append(self._span(para, m.start(1), m.end(1), "plot", 0.55, number=m.group(1),
                                          note="Nummer im selben Satz wie eine Grundstücksangabe"))
        return out

    # places ---------------------------------------------------------------
    def _addresses(self, para: Paragraph) -> list[Span]:
        out = []
        n = para.norm
        for m in self.address_re.finditer(n):
            out.append(self._span(para, m.start(), m.end(), "address", 0.95))
        for rx, conf in ((self.postplace_fl, 0.95), (self.postplace_foreign, 0.9)):
            for m in rx.finditer(n):
                out.append(self._span(para, m.start(), m.end(), "address", conf))
        for m in self.postplace_ctx.finditer(n):
            out.append(self._span(para, m.start(1), m.end(1), "address", 0.8))
        for m in self.street_unnumbered.finditer(n):
            if m.group() not in self.street_generic:
                out.append(self._span(para, m.start(), m.end(), "street", 0.5,
                                      note="Strassenname ohne Hausnummer"))
        return out

    def _places(self, para: Paragraph) -> list[Span]:
        out = []
        n = para.norm
        for m in self.place_re.finditer(n):
            entry = self.place_entries.get(m.group())
            if entry is None:
                continue
            kind = entry.kind
            if not entry.ambiguous:
                conf = 0.95 if kind == "place" else 0.9
            elif kind == "place" and _PLACE_CONTEXT.search(n[max(0, m.start() - 30) : m.start()]):
                conf = 0.9
            else:
                conf = 0.4
            meta = {"base": entry.base, "gazetteer_ambiguous": entry.ambiguous}
            if entry.ambiguous and conf < 0.6:
                meta["note"] = "mehrdeutiger Ortsname (auch Wort/Nachname)"
            out.append(self._span(para, m.start(), m.end(), kind, conf, source="gazetteer", **meta))
        return out

    def _fieldnames(self, para: Paragraph) -> list[Span]:
        out = []
        n = para.norm
        for m in self.field_quoted.finditer(n):
            name = m.group("name")
            if len(name.split()) > 3 or name.split()[0] in self.stop:
                continue
            noun_ctx = bool(re.match(_FIELD_CTX, m.group("ctx")))
            out.append(self._span(para, m.start("name"), m.end("name"), "fieldname", 0.85 if noun_ctx else 0.55,
                                  note="Flurname?"))
        if self.field_re is not None:
            for m in self.field_re.finditer(n):
                entry = self.field_entries[m.group()]
                before = n[max(0, m.start() - 30) : m.start()]
                ctx = re.search(rf"(?:{_FIELD_CTX}|{_FIELD_PREP})\"?$", before)
                conf = 0.85 if (ctx or not entry.ambiguous) else 0.4
                out.append(self._span(para, m.start(), m.end(), "fieldname", conf, source="gazetteer"))
        return out

    def _products(self, para: Paragraph) -> list[Span]:
        if self.product_re is None:
            return []
        return [self._span(para, m.start(), m.end(), "product", 0.9, source="gazetteer")
                for m in self.product_re.finditer(para.norm)]

    # persons ------------------------------------------------------------------
    def take_name(self, n: str, j: int, greedy: bool = False) -> tuple[int, int, list[str]] | None:
        """Read a person name at j (titles skipped, not included). Known first
        names, initials and particles extend the name; the first other
        capitalised word is the surname. With greedy=True (after a title or
        honorific) an unknown first word may still be followed by a surname."""
        while True:
            t = self.titles_ws.match(n, j)
            if not t or t.end() == j:
                break
            j = t.end()
        toks: list[tuple[str, int, int]] = []
        pos = j
        while len(toks) < self.max_tokens:
            m = _TOKEN.match(n, pos) or _PARTICLE_RE.match(n, pos)
            if m is None or m.group() in self.stop:
                break
            tok = m.group()
            toks.append((tok, m.start(), m.end()))
            leading = tok.endswith(".") or bool(_PARTICLE_ONLY.fullmatch(tok)) or tok in self.first_names
            if not leading and not (greedy and len(toks) == 1):
                break
            ws = re.compile(r" +").match(n, m.end())
            if ws is None:
                break
            nxt = _TOKEN.match(n, ws.end()) or _PARTICLE_RE.match(n, ws.end())
            if nxt is None or nxt.group() in self.stop:
                break
            if not leading and (nxt.group().endswith(".") or nxt.group() in self.first_names):
                break
            pos = ws.end()
        while toks and (toks[-1][0].endswith(".") or _PARTICLE_ONLY.fullmatch(toks[-1][0])):
            toks.pop()
        if not toks:
            return None
        return toks[0][1], toks[-1][2], [t[0] for t in toks]

    def _persons(self, para: Paragraph) -> list[Span]:
        out = []
        n = para.norm
        for m in self.honorific.finditer(n):
            got = self.take_name(n, m.end(), greedy=True)
            if got:
                out.append(self._span(para, got[0], got[1], "person", 0.85, role="affected", tokens=got[2],
                                      trigger="honorific"))
        for m in self.title_any.finditer(n):
            title = m.group()
            got = self.take_name(n, m.start(), greedy=True)
            if not got:
                continue
            role = "affected"
            if re.match(r"Dr\.\s?med", title):
                role = "expert_or_doctor"
            elif re.match(r"(?:RA|Rechtsanw)", title):
                role = "representative"
            out.append(self._span(para, got[0], got[1], "person", 0.8, role=role, tokens=got[2], trigger="title"))
        for rx, role in self.person_ctx:
            for m in rx.finditer(n):
                got = self.take_name(n, m.end())
                if got:
                    out.append(self._span(para, got[0], got[1], "person", 0.8, role=role, tokens=got[2],
                                          trigger="context"))
        for m in self.first_name_re.finditer(n):
            toks = m.group().split()
            if toks[-1] in self.stop or (toks[-1] in self.first_names and len(toks) == 2):
                continue
            out.append(self._span(para, m.start(), m.end(), "person", 0.7, role="affected", tokens=toks,
                                  trigger="first_name"))
        for m in self.maiden_re.finditer(n):
            if m.group(1) in self.stop:
                continue
            out.append(self._span(para, m.start(1), m.end(1), "person", 0.75, role="affected",
                                  tokens=[m.group(1)], trigger="maiden", maiden=True))
        return out

    def _companies(self, para: Paragraph) -> list[Span]:
        out = []
        n = para.norm
        word = re.compile(r"\S+")
        for m in self.legal_re.finditer(n):
            left = n[max(0, m.start() - 120) : m.start()]
            offset = max(0, m.start() - 120)
            toks = list(word.finditer(left))
            if not toks or not left.endswith(" "):
                continue
            chosen: list[re.Match] = []
            for t in reversed(toks):
                w_ = t.group()
                if chosen and w_ in ("&", "+", "und"):
                    chosen.append(t)
                    continue
                if re.search(r"[,;:)(\"]$", w_) or re.match(r"[(\"]", w_):
                    break
                capital = bool(re.fullmatch(rf"[{UPPER}][\w'.&+-]*|\d+[A-Za-z][\w'.&+-]*", w_))
                if not capital or (w_ in self.stop and w_ not in self.activity):
                    break
                chosen.append(t)
                if len(chosen) >= 5:
                    break
            while chosen and chosen[-1].group() in ("&", "+", "und"):
                chosen.pop()
            if not chosen:
                continue
            chosen.reverse()
            core = [t.group() for t in chosen
                    if t.group() not in self.activity and t.group() not in ("&", "+", "und") and len(t.group()) > 1]
            if not core:
                continue
            ns, ne = offset + chosen[0].start(), offset + chosen[-1].end()
            activity = []
            for t in reversed(chosen):
                if t.group() in self.activity:
                    activity.insert(0, t.group())
                else:
                    break
            out.append(self._span(para, ns, ne, "org", 0.85, role="affected", legal_form=m.group(),
                                  activity=" ".join(activity)))
        return out


def generalised_birthdate(text: str, template: str) -> str | None:
    m = re.search(r"(19|20)\d{2}", text)
    if not m:
        m2 = re.search(r"\b(\d{2})\s*$", text)
        if not m2:
            return None
        yy = int(m2.group(1))
        year = 1900 + yy if yy > date.today().year % 100 else 2000 + yy
    else:
        year = int(m.group())
    return template.format(year_bound=year_bound(year), year=year)
