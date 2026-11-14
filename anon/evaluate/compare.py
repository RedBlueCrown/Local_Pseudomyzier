"""Evaluation against hand-made gold anonymisations.

For every gold pair (input "... pseudonymisiert.docx", gold "... anonymisiert.docx"):
  1. run review + automatic finalize (entities kept, i.e. --keep-entities),
  2. token-level diff input <-> gold and input <-> tool output,
  3. classify each gold change as matched / missed / passage edit (2.4), each
     tool change not in gold as extra (often a gold miss: check manually),
  4. report recall per category (excluding 2.4 passage edits), exact-match
     rate, extras, and letter-sequence agreement.
Inputs without gold get a review copy and a detection summary.

The report is written to the output directory (it contains pseudonymised
test-data tokens, so keep it out of git); the log only gets counts.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..docx_io.finalize import accept_all_parts
from ..docx_io.package import DocxPackage
from ..docx_io.reader import read_document

log = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"\[…\]|\[\.\.\.\]|\*{3}|\w+(?:[-'.]\w+)*|[^\w\s]")
LETTER_RE = re.compile(r"^[A-Z]{1,3}s?$")
PARA = "¶"


@dataclass
class Tok:
    text: str
    part: str
    para: int
    start: int
    end: int


def tokens(pkg: DocxPackage) -> list[Tok]:
    accept_all_parts(pkg)
    out: list[Tok] = []
    for p in read_document(pkg).paragraphs:
        for m in TOKEN_RE.finditer(p.norm):
            s, e = p.to_orig(m.start(), m.end())
            out.append(Tok(m.group(), p.part, p.index, s, e))
        out.append(Tok(PARA, p.part, p.index, len(p.text), len(p.text)))
    return out


@dataclass
class Change:
    i1: int
    i2: int
    before: list[str]
    after: list[str]
    kind: str = "entity"  # entity | passage | other
    before_ctx: str = ""  # input token just before the change
    category: str = "?"
    status: str = ""  # matched | missed | passage | extra
    exact: bool = False


def diff(a: list[Tok], b: list[Tok]) -> list[Change]:
    sm = difflib.SequenceMatcher(a=[t.text for t in a], b=[t.text for t in b], autojunk=False)
    out = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        before = [t.text for t in a[i1:i2]]
        after = [t.text for t in b[j1:j2]]
        if not [x for x in before + after if x != PARA]:
            continue  # only paragraph structure differs
        out.append(Change(i1, i2, before, after, before_ctx=a[i1 - 1].text if i1 > 0 else ""))
    return out


def _words(tokens: list[str]) -> list[str]:
    return [t for t in tokens if t != PARA and re.match(r"\w|\*", t)]


def _non_anonymisation(c: Change) -> str | None:
    """Gold changes that are not anonymisation: added text, editorial fixes
    (incl. corruption in the pseudonymised input), names the gold kept."""
    before, after = _words(c.before), _words(c.after)
    if not before and after:
        return "added"
    if not before and not after:
        return "editorial"
    if all(t.isdigit() for t in before + after) and c.before_ctx == "\ufffc":
        return "editorial"  # field result (page number) next to field characters
    anon_like = any(LETTER_RE.match(t) or t == "***" or t.isdigit() for t in after)
    if not anon_like and all(t[:1].islower() for t in before + after):
        return "editorial"  # "zur Esche" stays a passage below; "ist", "vNovaussichtlichem"
    if after and not anon_like and all(t[:1].isupper() for t in after) and all(t[:1].isupper() for t in before):
        return "gold_kept_name"  # gold made from the real original left this name unchanged
    return None


def _is_passage(c: Change, max_entity_tokens: int) -> bool:
    after = " ".join(c.after)
    if "[…]" in after or "[...]" in after:
        return True
    words = [t for t in c.before if t != PARA and re.match(r"\w", t)]
    looks_entity = all(LETTER_RE.match(t) or t in ("***", PARA) or t.isdigit() or not re.match(r"\w", t)
                       for t in c.after)
    return len(words) > max_entity_tokens and not looks_entity


def _guess_category(c: Change) -> str:
    after = [t for t in c.after if t != PARA]
    if not after:
        return "place"  # deletions are Rubrum addresses in practice
    if all(t.isdigit() or not re.match(r"\w", t) for t in after) and any(t.isdigit() for t in after):
        return "plot"
    if any(LETTER_RE.match(t) for t in after):
        return "party"
    if "***" in after:
        return "place/id"
    return "?"


def _overlap(a: Change, b: Change) -> bool:
    if a.i1 == a.i2 or b.i1 == b.i2:  # insertion: compare positions
        return a.i1 <= b.i2 and b.i1 <= a.i2
    return a.i1 < b.i2 and b.i1 < a.i2


@dataclass
class DocResult:
    name: str
    gold: list[Change] = field(default_factory=list)
    tool: list[Change] = field(default_factory=list)
    letters_agree: int = 0
    letters_total: int = 0
    letter_mismatches: list[tuple[str, str, str]] = field(default_factory=list)
    leak_ok: bool = True
    stats: dict = field(default_factory=dict)


def _token_categories(toks: list[Tok], analysis) -> tuple[list[str], list[bool]]:
    """Category of the tool mention covering each input token (or '') and
    whether that mention is only a comment ("Prüfen")."""
    spans: dict[tuple[str, int], list[tuple[int, int, str, bool]]] = {}
    cat = {e.id: e.category for e in analysis.entities}
    for m in analysis.mentions:
        s = m.span
        spans.setdefault(s.key, []).append((s.start, s.end, cat.get(s.meta.get("entity"), "?"), not m.change))
    cats, flagged = [], []
    for t in toks:
        hit, flag = "", False
        for s, e, c, comment in spans.get((t.part, t.para), ()):
            if s < t.end and t.start < e:
                hit, flag = c, comment
                break
        cats.append(hit)
        flagged.append(flag)
    return cats, flagged


def compare_pair(input_bytes: bytes, gold_bytes: bytes, tool_bytes: bytes, analysis, name: str,
                 max_entity_tokens: int = 8) -> DocResult:
    a = tokens(DocxPackage.from_bytes(input_bytes))
    g = tokens(DocxPackage.from_bytes(gold_bytes))
    t = tokens(DocxPackage.from_bytes(tool_bytes))
    cats, flagged = _token_categories(a, analysis)
    res = DocResult(name)
    res.gold = diff(a, g)
    res.tool = diff(a, t)
    for c in res.tool:
        found = [cats[i] for i in range(c.i1, c.i2) if cats[i]]
        c.category = found[0] if found else _guess_category(c)
    for c in res.gold:
        if _is_passage(c, max_entity_tokens):
            c.kind, c.status, c.category = "passage", "passage", "passage (2.4)"
            continue
        other = _non_anonymisation(c)
        if other:
            c.kind, c.status, c.category = "other", other, other
            continue
        hits = [tc for tc in res.tool if _overlap(c, tc)]
        found = [cats[i] for i in range(c.i1, c.i2) if cats[i]]
        c.category = found[0] if found else _guess_category(c)
        if hits:
            c.status = "matched"
            c.exact = any(tc.i1 == c.i1 and tc.i2 == c.i2 and tc.after == c.after for tc in hits)
        elif any(flagged[i] for i in range(c.i1, c.i2)):
            c.status = "flagged"  # found, but only as a "Prüfen" comment
        else:
            c.status = "missed"
    for tc in res.tool:
        if not any(_overlap(tc, gc) for gc in res.gold):
            tc.status = "extra"
        else:
            tc.status = "matched"
    # letter-sequence agreement: original surface -> letter in gold vs tool
    def letters(changes: list[Change]) -> dict[str, str]:
        out: dict[str, str] = {}
        for c in changes:
            after = [x for x in c.after if x != PARA]
            if len(after) >= 1 and LETTER_RE.match(after[0]):
                words = []
                for x in c.before:  # leading name tokens only ("Albert Kaiser, Dorfstrasse 5" -> "Albert Kaiser")
                    if not re.match(r"[A-ZÄÖÜ]", x):
                        break
                    words.append(x)
                if words:
                    out.setdefault(" ".join(words), after[0].rstrip("s") if len(after[0]) > 1 else after[0])
        return out

    gl, tl = letters(res.gold), letters(res.tool)
    for surface in sorted(set(gl) & set(tl)):
        res.letters_total += 1
        if gl[surface] == tl[surface]:
            res.letters_agree += 1
        else:
            res.letter_mismatches.append((surface, gl[surface], tl[surface]))
    return res


# ---------------------------------------------------------------------------
def _find_pairs(root: Path) -> tuple[list[tuple[Path, Path]], list[Path]]:
    docs = sorted(p for p in root.rglob("*.docx") if not p.name.startswith("~$"))
    gold_by_stem = {p.stem.casefold(): p for p in docs if "anonymisiert" in p.stem.casefold()
                    and "pseudonymisiert" not in p.stem.casefold()}
    pairs, unseen = [], []
    for p in docs:
        low = p.stem.casefold()
        if "richtlinie" in low or "richlinie" in low or low in gold_by_stem:
            continue
        if "pseudonymisiert" in low:
            gold = gold_by_stem.get(low.replace("pseudonymisiert", "anonymisiert"))
            if gold is None:
                prefix = re.split(r"\s+pseudonymisiert", p.stem, flags=re.I)[0].casefold()
                gold = next((g for s, g in gold_by_stem.items() if s.startswith(prefix)), None)
            if gold is not None:
                pairs.append((p, gold))
                continue
        unseen.append(p)
    return pairs, unseen


def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:.1f}%" if b else "–"


def run_evaluation(pairs_dir: Path, out_dir: Path, cfg: Config, *, use_ner: bool = True, llm=None) -> Path:
    from ..pipeline import finalize, review  # noqa: PLC0415
    from ..review.entities_file import read_entities  # noqa: PLC0415

    out_dir.mkdir(parents=True, exist_ok=True)
    pairs, unseen = _find_pairs(pairs_dir)
    log.info("evaluate: %d gold pairs, %d unseen inputs", len(pairs), len(unseen))
    results: list[DocResult] = []
    lines = ["# Evaluation", "", "", ""]
    for inp, gold in pairs:
        target = out_dir / inp.stem
        analysis, docx, xlsx = review(inp, cfg, target, use_ner=use_ner, llm=llm)
        # simulated reviewer: accepts every change, dismisses every "Prüfen" comment
        from ..docx_io.finalize import remove_comments as _rc  # noqa: PLC0415

        reviewed = DocxPackage.load(docx)
        _rc(reviewed, None)
        fin = finalize(reviewed.to_bytes(), read_entities(xlsx), cfg)
        tool_bytes = fin.data
        if tool_bytes is None:  # leak check failed: compare the accepted review copy anyway
            pkg = DocxPackage.load(docx)
            accept_all_parts(pkg)
            from ..docx_io.finalize import remove_comments, remove_summary  # noqa: PLC0415

            remove_summary(pkg)
            remove_comments(pkg, None)
            tool_bytes = pkg.to_bytes()
        else:
            (target / f"{inp.stem}.clean.docx").write_bytes(tool_bytes)
        res = compare_pair(inp.read_bytes(), gold.read_bytes(), tool_bytes,
                           analysis, inp.stem)
        res.leak_ok = fin.data is not None
        res.stats = analysis.stats
        results.append(res)
        log.info("evaluate %s: gold=%d tool=%d", "pair", len(res.gold), len(res.tool))
    if results:
        lines += _report_tables(results)
    for res in results:
        lines += _report_doc(res)
    if unseen:
        lines += ["", "## Dokumente ohne Gold (Prüfkopien zur manuellen Kontrolle)", ""]
        lines += ["| Dokument | Entitäten | Änderungen | Prüfhinweise | nach Kategorie |", "|---|---|---|---|---|"]
        for inp in unseen:
            analysis, docx, _ = review(inp, cfg, out_dir / inp.stem, use_ner=use_ner, llm=llm)
            s = analysis.stats
            cats = ", ".join(f"{k}={v}" for k, v in sorted(s["by_category"].items()))
            lines.append(f"| {inp.stem} | {s['entities']} | {s['changes']} | {s['comments']} | {cats} |")
    ner_state = {r.stats.get("ner") for r in results} or {"on" if use_ner else "off"}
    llm_state = {r.stats.get("llm") for r in results} or {"on" if llm else "off"}
    lines[2] = (f"Gold-Paare: {len(pairs)}, ohne Gold: {len(unseen)}; NER: {'/'.join(sorted(ner_state))}; "
                f"LLM: {'/'.join(sorted(llm_state))}")
    report = out_dir / "report.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "report.json").write_text(json.dumps(_json(results), ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def _report_tables(results: list[DocResult]) -> list[str]:
    cats: dict[str, dict[str, int]] = {}
    def blank():
        return {"gold": 0, "matched": 0, "flagged": 0, "exact": 0, "missed": 0, "extra": 0}

    other: dict[str, int] = {}
    for r in results:
        for c in r.gold:
            if c.kind in ("passage", "other"):
                other[c.status] = other.get(c.status, 0) + 1
                continue
            d = cats.setdefault(c.category, blank())
            d["gold"] += 1
            d[c.status] += 1
            d["exact"] += int(c.exact)
        for c in r.tool:
            if c.status == "extra":
                cats.setdefault(c.category, blank())["extra"] += 1
    lines = ["## Recall nach Kategorie (ohne 2.4-Passagen und Nicht-Anonymisierungen)", "",
             "| Kategorie | Gold-Änderungen | geändert | Recall | davon exakt | nur Prüfhinweis | verpasst | "
             "Extra (Tool) |", "|---|---|---|---|---|---|---|---|"]
    tot = blank()
    for cat, d in sorted(cats.items()):
        for k in tot:
            tot[k] += d[k]
        lines.append(f"| {cat} | {d['gold']} | {d['matched']} | {_pct(d['matched'], d['gold'])} | "
                     f"{_pct(d['exact'], d['matched'])} | {d['flagged']} | {d['missed']} | {d['extra']} |")
    lines.append(f"| **gesamt** | {tot['gold']} | {tot['matched']} | {_pct(tot['matched'], tot['gold'])} | "
                 f"{_pct(tot['exact'], tot['matched'])} | {tot['flagged']} | {tot['missed']} | {tot['extra']} |")
    lines += ["", f"Recall inkl. Prüfhinweise: {_pct(tot['matched'] + tot['flagged'], tot['gold'])}",
              "Nicht als Anonymisierung gezählt: " + ", ".join(f"{k}={v}" for k, v in sorted(other.items()))]
    passages = sum(1 for r in results for c in r.gold if c.kind == "passage")
    la = sum(r.letters_agree for r in results)
    lt = sum(r.letters_total for r in results)
    lines += ["", f"2.4-Passagen im Gold (nur als Vorschlag erwartet): {passages}",
              f"Buchstabenfolge übereinstimmend: {la}/{lt} ({_pct(la, lt)})", ""]
    return lines


def _ctx(c: Change) -> str:
    b = " ".join(x for x in c.before if x != PARA)[:120]
    a = " ".join(x for x in c.after if x != PARA)[:120]
    return f"`{b}` → `{a}`"


def _report_doc(r: DocResult) -> list[str]:
    lines = ["", f"## {r.name}", "",
             f"Leak-Check: {'bestanden' if r.leak_ok else 'FEHLGESCHLAGEN'}; "
             f"Entitäten {r.stats.get('entities')}, Änderungen {r.stats.get('changes')}, "
             f"Prüfhinweise {r.stats.get('comments')}", ""]
    missed = [c for c in r.gold if c.status == "missed"]
    flagged = [c for c in r.gold if c.status == "flagged"]
    passages = [c for c in r.gold if c.kind == "passage"]
    others = [c for c in r.gold if c.kind == "other"]
    inexact = [c for c in r.gold if c.status == "matched" and not c.exact]
    extra = [c for c in r.tool if c.status == "extra"]
    for title, items in (("Verpasst (im Gold geändert, Tool nicht)", missed),
                         ("Nur als Prüfhinweis markiert (Gold hat geändert)", flagged),
                         ("Keine Anonymisierung (Ergänzungen, Korrekturen, im Gold belassene Namen)", others),
                         ("Extra (Tool geändert, Gold nicht – oft Gold-Versäumnis, manuell prüfen)", extra),
                         ("Gefunden, aber anders ersetzt (u. a. menschliche Flüchtigkeitsfehler im Gold)", inexact),
                         ("2.4-Passagen im Gold (Tool soll nur vorschlagen)", passages)):
        lines.append(f"### {title}: {len(items)}")
        for c in items[:200]:
            lines.append(f"- [{c.category}] {_ctx(c)}")
        lines.append("")
    if r.letter_mismatches:
        lines.append("### Abweichende Buchstaben (Original → Gold / Tool)")
        for s, g, t in r.letter_mismatches:
            lines.append(f"- `{s}` → {g} / {t}")
    return lines


def _json(results: list[DocResult]) -> list[dict]:
    out = []
    for r in results:
        out.append({
            "name": r.name, "leak_ok": r.leak_ok,
            "gold": [{"category": c.category, "status": c.status, "exact": c.exact} for c in r.gold],
            "tool_extra": sum(1 for c in r.tool if c.status == "extra"),
            "letters": [r.letters_agree, r.letters_total],
        })
    return out
