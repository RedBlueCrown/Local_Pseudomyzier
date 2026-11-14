"""End-to-end flows used by the CLI (and later a web endpoint).

analyse()  detection + resolution on the ORIGINAL document (always)
review()   analyse + write review copy (.docx) and entities (.xlsx)
finalize() review copy -> clean export, with leak check
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .detect.keeplist import KeepList, KeepPerson
from .detect.merge import apply_keep, resolve_overlaps
from .detect.ner import NerDetector
from .detect.rubrum import RubrumParser
from .detect.rules import RuleDetector
from .detect.span import KEEP_KIND, Span
from .docx_io.finalize import (
    accept_all_parts, remove_comments, remove_summary, remove_tool_bookmarks, strip_metadata,
    strip_tool_highlights, tool_comment_states, tool_item_states, tool_ranges,
)
from .docx_io.package import DocxPackage
from .docx_io.reader import Document, read_document
from .docx_io.revisions import Edit, RevisionWriter
from .docx_io.validate import validate
from .evaluate.leakcheck import LeakReport, Term, check, scrub_hidden
from .resolve.assign import assign_replacements, attach_final, decide, plan_mentions, renumber
from .resolve.entities import Entity, EntityBuilder, Mention, merge_into, rescan
from .review.entities_file import EntityRow, read_entities, write_entities
from .review.summary import insert_summary

log = logging.getLogger(__name__)

_KIND_FOR_CATEGORY = {"party": "person", "representative": "representative", "place": "place", "plot": "plot",
                      "id": "id"}


@dataclass
class Analysis:
    pkg: DocxPackage
    doc: Document
    entities: list[Entity]
    mentions: list[Mention]
    keep_people: list[KeepPerson]
    stats: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    passages: list = field(default_factory=list)  # guideline 2.4 suggestions (comments only)


def _apply_overrides(builder: EntityBuilder, entities: list[Entity], rows: list[EntityRow]) -> list[Entity]:
    """Match xlsx rows to freshly detected entities (by shared forms, same
    category; id as tie-breaker) and apply the reviewer's edits."""
    by_row: dict[int, Entity] = {}
    used: set[int] = set()
    for i, row in enumerate(rows):
        forms = {f.casefold() for f in row.forms}
        cands = [e for e in entities if id(e) not in used and forms & {f.casefold() for f in e.forms}
                 and (row.category is None or e.category == row.category or row.id is None)]
        if not cands:
            continue
        exact = [e for e in cands if e.id == row.id]
        e = (exact or cands)[0]
        by_row[i] = e
        used.add(id(e))
    row_ids = {row.id: i for i, row in enumerate(rows) if row.id}
    for i, row in enumerate(rows):
        e = by_row.get(i)
        if e is None:
            if not row.forms:
                continue
            category = row.category or "party"
            kind = row.kind if row.id and row.kind else _KIND_FOR_CATEGORY[category]
            e = builder._new(kind, "representative" if category == "representative" else "affected")
            e.category = category
            e.confidence = 1.0
            e.sources.add("reviewer")
            if kind in ("person", "representative"):
                e.tokens = row.forms[0].split()
            by_row[i] = e
        for f in row.forms:
            e.add_form(f)
        for t in row.leak_terms:
            if t not in e.leak_terms:
                e.leak_terms.append(t)
        if row.replacement_changed:
            e.replacement, e.replacement_locked = row.replacement, True
        if row.decision_changed or (row.id is None and row.decision):
            e.decision, e.decision_locked = row.decision, True
    for i, row in enumerate(rows):
        if row.merge_into and row.merge_into in row_ids and i in by_row:
            src, dst = by_row[i], by_row.get(row_ids[row.merge_into])
            if dst is not None and dst is not src:
                merge_into(dst, src, f"zusammengeführt ({row.id} -> {row.merge_into})")
    return [e for e in builder.entities if e.forms]


def analyse(data: bytes, cfg: Config, *, use_ner: bool = True, ner: NerDetector | None = None,
            llm=None, rows: list[EntityRow] | None = None) -> Analysis:
    pkg = DocxPackage.from_bytes(data)
    notes: list[str] = []
    existing = accept_all_parts(pkg)
    if existing:
        log.warning("input contained %d tracked changes; they were accepted first", existing)
        notes.append(f"Das Original enthielt {existing} Überarbeitungen; sie wurden vorab angenommen.")
    doc = read_document(pkg)
    keep = KeepList(cfg)
    people, keep_spans = keep.detect(doc)
    rules = RuleDetector(cfg)
    rubrum = RubrumParser(cfg, keep, rules).parse(doc)
    keep_spans += [s for s in rubrum if s.kind == KEEP_KIND]
    candidates: list[Span] = [s for s in rubrum if s.kind != KEEP_KIND] + rules.detect(doc)
    stats = {"paragraphs": len(doc.paragraphs), "judges_clerks": len(people)}
    if use_ner:
        ner = ner or NerDetector.from_config(cfg)
        ner_spans = ner.detect(doc)
        stats["ner"] = "on" if ner.available else "unavailable"
        candidates += ner_spans
    else:
        stats["ner"] = "off"
    _soften_cited_context(doc, candidates, keep_spans)
    candidates = apply_keep(candidates, keep_spans)
    selected, swallowed = resolve_overlaps(candidates)
    builder = EntityBuilder(cfg, doc)
    entities = builder.build(selected)
    for e in entities:
        if e.kind == "address":
            for m in e.mentions:
                for s in swallowed.get(id(m.span), []):
                    t = " ".join(s.text.split()).strip(" ,;")
                    if t and t not in e.leak_terms and t not in e.forms:
                        e.leak_terms.append(t)
        if e.kind == "address":
            e.forms = [f.strip(" ,;.") for f in e.forms if f.strip(" ,;.")]
    passages = []
    if llm is not None:
        from .detect.llm import enrich_entities  # noqa: PLC0415

        notes.extend(enrich_entities(llm, doc, entities, cfg))
        passages = list(getattr(llm, "passages", []))
        entities = [e for e in entities if e.forms]
        stats["llm"] = "on"
    else:
        stats["llm"] = "off"
    if rows:
        entities = _apply_overrides(builder, entities, rows)
    for e in entities:
        if not e.decision_locked and e.kind in ("person", "org") and e.role == "expert_or_doctor" \
                and cfg.policy.get("third_parties") == "keep":
            e.decision = "keep"
    resc = rescan(doc, entities, cfg, rules.plot_head)
    spans = apply_keep(selected + resc, keep_spans)
    final, _ = resolve_overlaps(spans)
    entities = attach_final(entities, final, lambda s: doc.get(s.part, s.para).order_key(s.start))
    renumber(entities)
    decide(entities, cfg)
    assign_replacements(entities, cfg)
    mentions = plan_mentions(entities, cfg, None)
    stats.update({
        "entities": len(entities),
        "changes": sum(1 for m in mentions if m.change),
        "comments": sum(1 for m in mentions if not m.change),
        "by_category": _count_by_category(mentions, entities),
    })
    stats["passages"] = len(passages)
    return Analysis(pkg, doc, entities, mentions, people, stats, notes, passages)


_CITED_COURT = re.compile(r"\b(?:EGMR|EuGH|EFTA-(?:Gerichtshof|GH)|Gerichtshofs?\s+für\s+Menschenrechte|Bsw\.)")


def _soften_cited_context(doc: Document, candidates: list[Span], keep_spans: list[Span]) -> None:
    """Places in a paragraph about a cited ECHR/CJEU/EFTA decision belong to that
    decision (guideline 2.6): ask instead of changing."""
    cited = {k.key for k in keep_spans if k.role == "cited_case" and not k.meta.get("dynamic")}
    cited |= {p.pid for p in doc.paragraphs if _CITED_COURT.search(p.norm)}
    for c in candidates:
        if c.key in cited and c.kind in ("place", "place_adj", "address", "fieldname") and c.source != "rubrum":
            c.confidence = min(c.confidence, 0.5)
            c.meta["note"] = "Ort im Zusammenhang mit einer zitierten Entscheidung"


def _count_by_category(mentions: list[Mention], entities: list[Entity]) -> dict[str, int]:
    cat = {e.id: e.category for e in entities}
    out: dict[str, int] = {}
    for m in mentions:
        c = cat.get(m.span.meta.get("entity"), "?")
        out[c] = out.get(c, 0) + 1
    return out


def build_edits(analysis: Analysis, cfg: Config) -> list[Edit]:
    cat = {e.id: e.category for e in analysis.entities}
    edits = []
    for m in analysis.mentions:
        s = m.span
        category = cat[s.meta["entity"]]
        if s.kind == "address_tail" or (s.kind == "address" and m.replacement == ""):
            category = "place"
        kind = analysis.doc.get(s.part, s.para).kind
        if m.change:
            edits.append(Edit(s.part, s.para, s.start, s.end, m.replacement, cfg.author(category),
                              cfg.highlight(category), m.item_id))
        elif kind in ("header", "footer"):
            # Word allows no comments in headers/footers: propose as a change by "Prüfen"
            edits.append(Edit(s.part, s.para, s.start, s.end, m.replacement, cfg.author("review"), None,
                              m.item_id))
        else:
            edits.append(Edit(s.part, s.para, s.start, s.end, None, cfg.author("review"), None, m.item_id,
                              comment=m.comment))
    next_id = max((m.item_id for m in analysis.mentions), default=0)
    for p in analysis.passages:
        anchor = _free_anchor(p, edits)
        if anchor is None:
            continue
        next_id += 1
        quote = analysis.doc.get(p.part, p.para).text[p.start : p.end]
        edits.append(Edit(p.part, p.para, anchor[0], anchor[1], None, cfg.author("review"), None, next_id,
                          comment=f"Richtlinie 2.4 (Vorschlag, nicht angewendet): „{quote}“ – {p.reason} "
                                  f"Vorschlag: {p.proposal}"))
    return edits


def _free_anchor(p, edits: list[Edit]) -> tuple[int, int] | None:
    """Longest stretch of the passage not covered by another edit (a comment
    range must not overlap tracked changes written by this tool)."""
    taken = sorted((max(e.start, p.start), min(e.end, p.end)) for e in edits
                   if e.part == p.part and e.para == p.para and e.start < p.end and e.end > p.start)
    best, pos = None, p.start
    for s, e in taken + [(p.end, p.end)]:
        if s > pos and (best is None or s - pos > best[1] - best[0]):
            best = (pos, s)
        pos = max(pos, e)
    return best


def write_review(analysis: Analysis, cfg: Config, out_docx: Path, out_xlsx: Path | None) -> dict:
    writer = RevisionWriter(analysis.doc)
    writer.apply(build_edits(analysis, cfg))
    notes = list(analysis.notes)
    if writer.skipped:
        hf = [e for e in writer.skipped if e.replacement is None]
        if hf:
            notes.append(f"{len(hf)} unsichere Stelle(n) in Kopf-/Fusszeilen konnten nicht kommentiert werden "
                         f"(Word erlaubt dort keine Kommentare): Nr. "
                         + ", ".join(str(e.item_id) for e in hf) + ". Bitte dort manuell prüfen.")
        other = len(writer.skipped) - len(hf)
        if other:
            notes.append(f"{other} Änderung(en) konnten nicht geschrieben werden (Struktur); bitte prüfen.")
    if analysis.stats.get("ner") == "unavailable":
        notes.append("NER-Modell nicht verfügbar: nur Regeln/Verzeichnisse wurden verwendet.")
    insert_summary(analysis.pkg, analysis.entities, cfg, notes, writer.ids)
    problems = validate(DocxPackage.from_bytes(analysis.pkg.to_bytes()))
    if problems:
        log.error("review copy failed structural validation: %d problem(s)", len(problems))
        raise RuntimeError("review copy failed structural validation: " + "; ".join(problems[:5]))
    analysis.pkg.save(out_docx)
    if out_xlsx is not None:
        write_entities(out_xlsx, analysis.entities)
    return {"skipped": len(writer.skipped)}


def review(input_path: Path, cfg: Config, outdir: Path, *, use_ner: bool = True, entities_path: Path | None = None,
           llm=None, ner: NerDetector | None = None) -> tuple[Analysis, Path, Path]:
    rows = read_entities(entities_path) if entities_path else None
    analysis = analyse(input_path.read_bytes(), cfg, use_ner=use_ner, ner=ner, llm=llm, rows=rows)
    outdir.mkdir(parents=True, exist_ok=True)
    stem = input_path.stem
    out_docx = outdir / f"{stem}.review.docx"
    out_xlsx = outdir / f"{stem}.entities.xlsx"
    write_review(analysis, cfg, out_docx, out_xlsx)
    return analysis, out_docx, out_xlsx


# ---------------------------------------------------------------------------
# Finalize
# ---------------------------------------------------------------------------
@dataclass
class FinalizeResult:
    data: bytes | None
    report: LeakReport
    stats: dict


def finalize(review_bytes: bytes, rows: list[EntityRow], cfg: Config) -> FinalizeResult:
    pkg = DocxPackage.from_bytes(review_bytes)
    tool_authors = cfg.tool_authors
    states = tool_comment_states(pkg)
    items = tool_item_states(pkg, tool_authors)
    stats = {"pending_changes_accepted": len(items)}
    stats["revisions_accepted"] = accept_all_parts(pkg)
    stats["summary_removed"] = remove_summary(pkg)
    remove_all = cfg.rules.get("finalize", {}).get("remove_all_comments", True)
    stats["comments_removed"] = remove_comments(pkg, None if remove_all else tool_authors)
    stats["highlights_removed"] = strip_tool_highlights(pkg, set(cfg.tool_highlights.values()))
    fin = cfg.rules.get("finalize", {})
    stats.update(strip_metadata(pkg, strip_custom_xml=fin.get("strip_custom_xml", True),
                                remove_glossary=fin.get("remove_glossary", True),
                                remove_thumbnail=fin.get("remove_thumbnail", True)))
    terms: list[Term] = []
    scrub_terms: list[str] = []
    for row in rows:
        kept = row.decision == "keep"
        for t in row.terms():
            terms.append(Term(t, row.id or "neu", row.category or "?", kept))
            if not kept:
                scrub_terms.append(t)
    stats["hidden_values_scrubbed"] = scrub_hidden(pkg, scrub_terms, cfg.stars)
    pkg = DocxPackage.from_bytes(pkg.to_bytes())
    doc = read_document(pkg)
    ranges = tool_ranges(doc)
    _, keep_spans = KeepList(cfg).detect(doc)
    report = check(pkg, doc, terms, ranges, items, states, keep_spans, _valid_replacements(rows, cfg))
    if not report.ok:
        return FinalizeResult(None, report, stats)
    remove_tool_bookmarks(pkg)
    out = pkg.to_bytes()
    problems = validate(DocxPackage.from_bytes(out))
    if problems:
        report.problems.extend(f"Struktur: {p}" for p in problems[:10])
        return FinalizeResult(None, report, stats)
    return FinalizeResult(out, report, stats)


def _valid_replacements(rows: list[EntityRow], cfg: Config) -> set[str]:
    """Texts the tool may have inserted (to detect edited insertions)."""
    out = {cfg.stars}
    for row in rows:
        r = row.replacement or row.tool_replacement
        if not r:
            continue
        out |= {r, r + "s"}
        for word in cfg.detect.get("company_activity_words", []):
            out.add(f"{r} {word}")
    for year in range(1890, 2101):
        out.add(cfg.policy.get("birth_date_generalise_template", "vor dem Jahr {year_bound}")
                .format(year_bound=year, year=year - 1))
    out |= {str(n) for n in range(0, 10000)}
    return out
