"""Deterministic assignment: decisions, letters, plot numbers, stars.

Letters go to entities in order of first mention (Rubrum first, then body,
footnotes at their anchor). Plot numbers have their own counter. Nothing
here is decided by a model.
"""

from __future__ import annotations

import re
from itertools import count, product
from typing import Iterator

from ..config import Config
from ..detect.rules import generalised_birthdate
from ..detect.span import Span
from .entities import Entity, Mention

CATEGORY_LABEL = {"party": "Partei/Person", "representative": "Vertreter", "place": "Ort", "plot": "Grundstück",
                  "id": "Datum/ID"}


def letter_sequence(skip: set[str]) -> Iterator[str]:
    """A..Z, then AA, AB, ... ZZ, then AAA ... (skipping reserved letters)."""
    alphabet = [chr(c) for c in range(ord("A"), ord("Z") + 1)]
    for length in count(1):
        for combo in product(alphabet, repeat=length):
            s = "".join(combo)
            if s not in skip:
                yield s


def attach_final(entities: list[Entity], spans: list[Span], order_of) -> list[Entity]:
    by_id = {e.id: e for e in entities}
    for e in entities:
        e.mentions = []
    for s in spans:
        e = by_id.get(s.meta.get("entity"))
        if e is None:
            continue
        e.mentions.append(Mention(s, genitive=bool(s.meta.get("genitive")), ambiguous=list(s.meta.get("ambiguous") or []),
                                  order=order_of(s)))
    alive = [e for e in entities if e.mentions]
    for e in alive:
        e.mentions.sort(key=lambda m: m.order)
        e.first_order = e.mentions[0].order
    alive.sort(key=lambda e: e.first_order)
    return alive


def renumber(entities: list[Entity]) -> None:
    for i, e in enumerate(entities, 1):
        e.id = f"E{i}"
        for m in e.mentions:
            m.span.meta["entity"] = e.id


def decide(entities: list[Entity], cfg: Config) -> None:
    thr = cfg.review_threshold
    third = cfg.policy.get("third_parties", "letters")
    for e in entities:
        if e.decision_locked:
            continue
        if e.kind in ("person", "org") and e.role == "expert_or_doctor" and third == "keep":
            e.decision = "keep"
        elif e.confidence < thr or e.force_review:
            e.decision = "review"
        else:
            e.decision = "auto"


def _needs_letter(e: Entity, cfg: Config) -> bool:
    if e.kind in ("person", "org"):
        return e.category == "party" or cfg.policy.get("representatives", "stars") == "letters"
    if e.kind == "representative":
        return cfg.policy.get("representatives", "stars") == "letters"
    return False


def assign_replacements(entities: list[Entity], cfg: Config) -> None:
    stars = cfg.stars
    reserved = {e.replacement for e in entities if e.replacement_locked}
    letters = letter_sequence(reserved | set(cfg.policy.get("letters_skip", []) or []))
    plot_used = {e.replacement for e in entities if e.replacement_locked and e.kind == "plot"}
    plot_counter = count(int(cfg.policy.get("plot_start", 1)))
    for e in sorted(entities, key=lambda e: e.first_order):
        if e.replacement_locked or e.decision == "keep":
            continue
        if _needs_letter(e, cfg):
            e.replacement = next(letters)
        elif e.kind == "plot":
            n = str(next(plot_counter))
            while n in plot_used:
                n = str(next(plot_counter))
            e.replacement = n
        else:
            e.replacement = stars


def _mention_replacement(e: Entity, m: Mention, cfg: Config) -> tuple[str, Span]:
    span = m.span
    stars = cfg.stars
    if span.kind == "address_tail" and span.source == "rubrum":
        return "", span
    if span.kind == "rep_block":
        return e.replacement or stars, span
    if e.kind in ("person", "org", "representative"):
        base = e.replacement
        is_letter = bool(re.fullmatch(r"[A-Z]{1,3}", base))
        if m.genitive and is_letter:
            base += "s"
        if e.kind == "org" and cfg.policy.get("company_keep_activity") and e.activity and \
                span.text.rstrip().endswith(e.activity) and is_letter:
            base = f"{base} {e.activity}"
        return base, span
    if e.kind == "birthdate" and cfg.policy.get("birth_dates") == "generalise" and not span.meta.get("year_only") \
            and not e.replacement_locked:
        gen = generalised_birthdate(span.text, cfg.policy["birth_date_generalise_template"])
        start = span.meta.get("generalise_from", span.start)
        if gen:
            ext = Span(span.part, span.para, min(start, span.start), span.end, span.text, span.kind, span.source,
                       span.confidence, span.role, dict(span.meta))
            return gen, ext
    return e.replacement, span


def _comment_text(e: Entity, m: Mention, replacement: str, reason: str) -> str:
    label = CATEGORY_LABEL.get(e.category, e.category)
    shown = m.span.text.replace("\n", " ").strip()
    repl = replacement if replacement else "(löschen)"
    return (f"{label} „{shown}“ → Vorschlag „{repl}“ ({e.id}; {reason}). "
            "Wenn zutreffend: im Text ersetzen. Wenn nicht: Kommentar löschen oder als erledigt markieren.")


def plan_mentions(entities: list[Entity], cfg: Config, doc_text_of) -> list[Mention]:
    thr = cfg.review_threshold
    out: list[Mention] = []
    for e in entities:
        if e.decision == "keep":
            continue
        for m in e.mentions:
            repl, span = _mention_replacement(e, m, cfg)
            m.replacement = repl
            m.span = span
            low = m.span.confidence < thr
            flagged = bool(m.ambiguous or m.span.meta.get("conflict"))
            if e.decision == "auto" and not flagged and (not low or (e.decision_locked and e.decision == "auto")):
                m.change = True
                m.comment = None
            else:
                m.change = False
                reasons = []
                if e.decision == "review":
                    reasons.append(f"unsichere Erkennung, Konfidenz {e.confidence:.2f}")
                    reasons.extend(n for n in e.notes if n.startswith("LLM"))
                if m.span.meta.get("conflict"):
                    reasons.append(m.span.meta["conflict"])
                if m.span.meta.get("note"):
                    reasons.append(m.span.meta["note"])
                if low and not reasons:
                    reasons.append(f"Konfidenz {m.span.confidence:.2f}")
                m.comment = _comment_text(e, m, repl, "; ".join(dict.fromkeys(reasons)) or "bitte prüfen")
            out.append(m)
    out.sort(key=lambda m: m.order)
    for i, m in enumerate(out, 1):
        m.item_id = i
    return out
