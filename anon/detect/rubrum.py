"""Rubrum parser: parties and representatives with their addresses.

Handles
  * label blocks:  "Beschwerdeführer:<tab>Albert Kaiser, Dorfstrasse 5, 9490 Vaduz"
    with the name on the same line, the next line (w:br) or the next table
    cell / paragraph, numbered parties ("1. ...", "2. ..."), address lines
    below the name and "vertreten durch:" lines/sub-blocks;
  * inline phrases: "in der Rechtssache der klagenden Partei Albert Kaiser,
    Dorfstrasse 5, 9490 Vaduz, vertreten durch Berger & Partner, ..., gegen ...".

Output spans
  person/org   the party name only (titles and legal form excluded) -> letter
  address_tail everything after the name in the block (address, birth date,
               ...) -> deleted ("Beschwerdeführer: A")
  rep_block    the whole representative block -> "***" (entity-linked)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import Config
from ..docx_io.reader import Document, Paragraph
from .keeplist import KeepList
from .rules import RuleDetector
from .span import Span
from .text import compile_list, titles_regex

_NUMBERING = re.compile(r"^[ \t]*(?:\d{1,2}[.)]|[a-z]\)|[IVX]+\.)[ \t]+")
_TRAIL = " \t,;.:"


@dataclass
class _Line:
    para: Paragraph
    start: int  # normalised offsets
    end: int

    @property
    def text(self) -> str:
        return self.para.norm[self.start : self.end]


class RubrumParser:
    def __init__(self, cfg: Config, keep: KeepList, rules: RuleDetector):
        self.cfg = cfg
        self.keep = keep
        self.rules = rules
        rb = cfg.detect["rubrum"]
        labels = "|".join(rb["party_labels"])
        self.party_label = re.compile(
            rf"^[ \t]*(?:\d+\.[ \t]*)?(?:{labels})(?:[ \t]*(?:\d+|[IVX]+))?[ \t]*(?::|\t|$)[ \t]*"
        )
        loose = "|".join(r for r in rb["representative_labels"] if r.startswith("vertr"))
        strict = "|".join(r for r in rb["representative_labels"] if not r.startswith("vertr")) or "(?!x)x"
        self.rep_label = re.compile(
            rf"^[ \t]*(?:(?:{loose or '(?!x)x'})[ \t]*:?|(?:{strict})[ \t]*(?::|\t|$))[ \t]*"
        )
        self.rep_inline = re.compile(r",?[ \t]*\b(?:vertreten\s+durch|vertr\.\s*d\.)[ \t]*:?[ \t]*")
        self.terminators = compile_list(rb["block_terminators"])
        self.end_markers = compile_list(rb["end_markers"])
        self.hints = compile_list(rb["address_line_hints"])
        self.inline = compile_list(rb.get("inline_party_patterns", []))
        self.max_paragraphs = int(rb.get("max_paragraphs", 150))
        self.max_block_lines = int(rb.get("max_block_lines", 8))
        self.titles = re.compile(titles_regex(cfg.detect["titles"]) + r"[ \t]*")
        self.honorific = re.compile(rf"(?:{'|'.join(cfg.detect['honorifics'])})[ \t]+")
        self.article = re.compile(r"(?:der|die|des|dem|den)[ \t]+")
        self.legal_end = re.compile(rf"[ \t]+(?:{'|'.join(sorted(cfg.detect['legal_forms'], key=len, reverse=True))})$")
        self.rep_noise = re.compile(
            rf"(?:^|[ \t,]+)(?:{'|'.join(rb.get('representative_noise', []))})(?=$|[ \t,])"
        )

    # ------------------------------------------------------------------
    def _lines(self, doc: Document) -> list[_Line]:
        lines: list[_Line] = []
        main = [p for p in doc.paragraphs if p.kind == "document"]
        for para in main[: self.max_paragraphs]:
            if any(rx.match(para.norm.strip()) for rx in self.end_markers):
                break
            pos = 0
            for chunk in para.norm.split("\n"):
                lines.append(_Line(para, pos, pos + len(chunk)))
                pos += len(chunk) + 1
        return lines

    def _address_like(self, text: str) -> bool:
        if any(rx.search(text) for rx in self.hints):
            return True
        return bool(self.rules.place_re.search(text) or self.rules.address_re.search(text))

    def _span(self, para: Paragraph, ns: int, ne: int, kind: str, conf: float, role: str | None = None,
              **meta) -> Span:
        s, e = para.to_orig(ns, ne)
        return Span(para.part, para.index, s, e, para.text[s:e], kind, "rubrum", conf, role, meta)

    @staticmethod
    def _trim(text: str, ns: int, ne: int) -> tuple[int, int]:
        while ns < ne and text[ns] in " \t":
            ns += 1
        while ne > ns and text[ne - 1] in _TRAIL:
            ne -= 1
        return ns, ne

    # ------------------------------------------------------------------
    def parse(self, doc: Document) -> list[Span]:
        spans: list[Span] = []
        lines = self._lines(doc)
        state: str | None = None  # party_name | party_addr | rep_name | rep_addr
        block_lines = 0
        for line in lines:
            text = line.text
            stripped = text.strip()
            m = self.party_label.match(text)
            if m:
                state, block_lines = "party_name", 0
                state = self._content(line, line.start + m.end(), "party_name", spans) or state
                continue
            if state is None:
                continue
            m = self.rep_label.match(text)
            if m and stripped:
                state = self._content(line, line.start + m.end(), "rep_name", spans) or "rep_name"
                continue
            if not stripped:
                if state in ("party_name", "rep_name"):
                    continue
                state = None
                continue
            block_lines += 1
            expecting_name = state in ("party_name", "rep_name")
            terminated = any(rx.match(stripped) for rx in self.terminators)
            if expecting_name and terminated and self.article.match(stripped):
                terminated = False  # "die Papageien Anstalt" on the line after the label
            if block_lines > self.max_block_lines or terminated:
                state = None
                continue
            if stripped in ("und", "sowie"):
                state = "party_name"
                continue
            num = _NUMBERING.match(text)
            if num and state in ("party_name", "party_addr"):
                state = self._content(line, line.start + num.end(), "party_name", spans) or "party_name"
            elif state in ("party_name", "rep_name"):
                state = self._content(line, line.start, state, spans) or state
            elif self._address_like(text):
                ns, ne = self._trim(line.para.norm, line.start, line.end)
                if line.start > 0:
                    ns = line.start - 1  # include the line break before an address line
                if ne > ns:
                    spans.append(self._span(line.para, ns, ne, "address_tail", 0.9,
                                            rep=state == "rep_addr", note="Adresszeile im Rubrum"))
            else:
                state = None
        spans.extend(self._inline(doc))
        return spans

    def _content(self, line: _Line, ns: int, state: str, spans: list[Span]) -> str | None:
        """Parse party or representative content from ns to the end of line."""
        para = line.para
        norm = para.norm
        ns, ne = self._trim(norm, ns, line.end)
        if ne <= ns:
            return None  # name follows on the next line / cell
        if state == "rep_name":
            spans.extend(self._rep_block(para, ns, ne))
            return "rep_addr"
        rep = self.rep_inline.search(norm, ns, ne)
        party_end = rep.start() if rep else ne
        spans.extend(self._party(para, ns, party_end))
        if rep:
            rs, re_ = self._trim(norm, rep.end(), ne)
            if re_ > rs:
                spans.extend(self._rep_block(para, rs, re_))
            return "rep_addr"
        return "party_addr"

    def _strip_prefixes(self, norm: str, ns: int, ne: int) -> int:
        changed = True
        while changed and ns < ne:
            changed = False
            for rx in (_NUMBERING, self.honorific, self.article, self.titles):
                m = rx.match(norm, ns, ne)
                if m and m.end() > ns:
                    ns, changed = m.end(), True
        return ns

    def _party(self, para: Paragraph, ns: int, ne: int, conf: float = 0.95) -> list[Span]:
        norm = para.norm
        ns = self._strip_prefixes(norm, ns, ne)
        ns, ne = self._trim(norm, ns, ne)
        if ne <= ns:
            return []
        cut = re.search(r",|\(|;|[ \t]geb(?:\.|oren)", norm[ns:ne])
        name_end = ns + cut.start() if cut else ne
        name_s, name_e = self._trim(norm, ns, name_end)
        name = norm[name_s:name_e]
        if not name or len(name) < 2 or name.strip("*") == "" or re.fullmatch(r"[A-Z]{1,2}", name):
            return []
        if self.keep.is_authority(name):
            return []
        stop = self.cfg.gazetteers.stopwords
        if all(t in stop or not t[:1].isupper() for t in name.split()):
            return []  # "der Beschwerdeführerin" is a role, not a name
        out: list[Span] = []
        lf = self.legal_end.search(name)
        maiden = None
        if cut and cut.group().strip().startswith("geb"):
            mm = re.match(r"[ \t]geb(?:\.|oren)[ \t]*([A-ZÄÖÜ][\w'-]+)", norm[name_end:ne])
            maiden = mm.group(1) if mm else None
        if lf:
            org_e = name_s + lf.start()
            out.append(self._span(para, name_s, org_e, "org", conf, role="party",
                                  legal_form=lf.group().strip(), from_rubrum=True))
        else:
            meta = {"tokens": name.split(), "from_rubrum": True}
            if maiden:
                meta["maiden"] = maiden
            out.append(self._span(para, name_s, name_e, "person", conf, role="party", **meta))
        tail_s, tail_e = self._trim(norm, name_end, ne)
        if tail_e > tail_s and re.search(r"\w", norm[tail_s:tail_e]):
            out.append(self._span(para, name_end, tail_e, "address_tail", conf, note="Adresse/Angaben im Rubrum"))
        return out

    def _rep_block(self, para: Paragraph, ns: int, ne: int, conf: float = 0.95) -> list[Span]:
        norm = para.norm
        content = norm[ns:ne]
        first = content.split(",")[0]
        body_s = self._strip_prefixes(norm, ns, ns + len(first))
        name = norm[body_s : ns + len(first)].strip(_TRAIL)
        core = self.rep_noise.sub("", name).strip(_TRAIL)
        if core:
            core_s = self._strip_prefixes(core, 0, len(core))
            core = core[core_s:].strip(_TRAIL)
        if not name or self.keep.is_authority(name):
            return []
        forms = [f for f in dict.fromkeys([name, core]) if f and len(f) > 1]
        tokens = core.split() if core and not re.search(r"&|\+|\bund\b|Partner", core) else None
        return [self._span(para, ns, ne, "rep_block", conf, role="representative", name=name, forms=forms,
                           person_tokens=tokens)]

    # ------------------------------------------------------------------
    def _inline(self, doc: Document) -> list[Span]:
        out: list[Span] = []
        main = [p for p in doc.paragraphs if p.kind == "document"][: self.max_paragraphs]
        for para in main:
            if any(rx.match(para.norm.strip()) for rx in self.end_markers):
                break
            for rx in self.inline:
                for m in rx.finditer(para.norm):
                    out.extend(self._inline_at(para, m.end()))
        return out

    def _inline_at(self, para: Paragraph, ns: int) -> list[Span]:
        norm = para.norm
        stop = re.search(r";|\.\s+[A-ZÄÖÜ]|\n|\s(?:gegen|wider|wegen)\s", norm[ns:])
        end = ns + stop.start() if stop else len(norm)
        segs = []
        pos = ns
        for part in norm[ns:end].split(","):
            segs.append((pos, pos + len(part)))
            pos += len(part) + 1
        if not segs:
            return []
        out = self._party(para, segs[0][0], segs[0][1], conf=0.9)
        if not out:
            return []
        tail_end = segs[0][1]
        i = 1
        while i < len(segs) and self._address_like(norm[segs[i][0] : segs[i][1]]) \
                and not self.rep_inline.match(norm, segs[i][0] - 1):
            tail_end = segs[i][1]
            i += 1
        ts, te = self._trim(norm, segs[0][1], tail_end)
        if te > ts:
            out.append(self._span(para, segs[0][1], te, "address_tail", 0.9, note="Adresse im Rubrum"))
        if i < len(segs):
            rep = self.rep_inline.match(norm, segs[i][0] - 1)
            if rep:
                block_end = segs[i][1]
                j = i + 1
                while j < len(segs) and self._address_like(norm[segs[j][0] : segs[j][1]]):
                    block_end = segs[j][1]
                    j += 1
                rs, re_ = self._trim(norm, rep.end(), block_end)
                if re_ > rs:
                    out.extend(self._rep_block(para, rs, re_, conf=0.9))
        return out
