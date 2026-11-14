"""entities.xlsx: the reviewer's editable source of truth.

Reviewers may
  * change "Ersetzung" (replacement)          -> locked for re-renders
  * set "Entscheidung" to keep / auto / review -> locked for re-renders
  * enter another ID in "Zusammenführen mit"   -> merge two entities
  * add a row without ID (forms + category)    -> missed entity
  * add forms to "Formen" (separated by " | ")

Hidden columns keep the tool's own suggestion so that only real edits are
treated as overrides (unchanged letters are re-sequenced on re-render).
The file contains original values: it is deleted by `anon finalize`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from ..resolve.entities import Entity

HEADERS = ["ID", "Kategorie", "Rolle", "Formen", "Ersetzung", "Entscheidung", "Zusammenführen mit", "Vorkommen",
           "Quelle", "Konfidenz", "Hinweise", "_tool_ersetzung", "_tool_entscheidung", "_leak_begriffe", "_art"]
CATEGORIES = ["party", "representative", "place", "plot", "id"]
DECISIONS = ["auto", "review", "keep"]
SEP = " | "

INSTRUCTIONS = [
    "Diese Datei enthält Originalwerte und wird beim Finalisieren gelöscht.",
    "Ersetzung ändern: Spalte 'Ersetzung' überschreiben (z. B. anderer Buchstabe).",
    "Nicht anonymisieren: 'Entscheidung' = keep.  Nur Kommentar: review.  Ändern: auto.",
    "Zwei Einträge zusammenführen: beim doppelten Eintrag die ID des Haupteintrags in 'Zusammenführen mit'.",
    "Fehlenden Eintrag ergänzen: neue Zeile ohne ID, 'Formen' (z. B. 'Hansjörg Beck | Beck') und 'Kategorie'.",
    "Kategorien: party (Buchstaben), representative (***), place (***), plot (Nummern), id (***).",
    "Danach: anon review ORIGINAL.docx --entities DIESE_DATEI.xlsx -o OUTDIR  (rendert die Prüfkopie neu).",
]


@dataclass
class EntityRow:
    id: str | None
    category: str | None
    role: str | None
    forms: list[str]
    replacement: str | None
    decision: str | None
    merge_into: str | None
    tool_replacement: str | None = None
    tool_decision: str | None = None
    leak_terms: list[str] = field(default_factory=list)
    kind: str | None = None

    @property
    def replacement_changed(self) -> bool:
        return self.replacement is not None and self.replacement != (self.tool_replacement or "")

    @property
    def decision_changed(self) -> bool:
        return bool(self.decision) and self.decision != (self.tool_decision or "")

    def terms(self) -> list[str]:
        return [t for t in dict.fromkeys(self.forms + self.leak_terms) if t and len(t.strip()) >= 2]


def write_entities(path: Path, entities: list[Entity]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Entitäten"
    ws.append(HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="DDDDDD")
    for e in entities:
        changes = sum(1 for m in e.mentions if m.change)
        ws.append([
            e.id, e.category, e.role or "", SEP.join(e.forms), e.replacement, e.decision, "",
            f"{len(e.mentions)} ({changes} Änderungen)", ", ".join(sorted(e.sources)), round(e.confidence, 2),
            "; ".join(e.notes), e.replacement, e.decision, SEP.join(e.leak_terms), e.kind,
        ])
    widths = [7, 14, 16, 50, 12, 12, 18, 18, 18, 10, 30]
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(ord("A") + i)].width = w
    for col in ("L", "M", "N", "O"):
        ws.column_dimensions[col].hidden = True
    ws.freeze_panes = "A2"
    dv_dec = DataValidation(type="list", formula1='"' + ",".join(DECISIONS) + '"', allow_blank=True)
    dv_cat = DataValidation(type="list", formula1='"' + ",".join(CATEGORIES) + '"', allow_blank=True)
    ws.add_data_validation(dv_dec)
    ws.add_data_validation(dv_cat)
    dv_dec.add("F2:F2000")
    dv_cat.add("B2:B2000")
    info = wb.create_sheet("Anleitung")
    for line in INSTRUCTIONS:
        info.append([line])
    info.column_dimensions["A"].width = 120
    wb.save(path)


def _cell(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s != "" else None


def read_entities(path: Path) -> list[EntityRow]:
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb["Entitäten"] if "Entitäten" in wb.sheetnames else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = [str(h).strip() if h is not None else "" for h in rows[0]]
    idx = {h: i for i, h in enumerate(header)}

    def get(row, name):
        i = idx.get(name)
        return _cell(row[i]) if i is not None and i < len(row) else None

    out: list[EntityRow] = []
    for row in rows[1:]:
        forms_raw = get(row, "Formen")
        if not forms_raw and not get(row, "ID"):
            continue
        forms = [f.strip() for f in (forms_raw or "").split("|") if f.strip()]
        leak = [f.strip() for f in (get(row, "_leak_begriffe") or "").split("|") if f.strip()]
        decision = get(row, "Entscheidung")
        if decision:
            decision = decision.lower()
            if decision not in DECISIONS:
                raise ValueError(f"unknown decision {decision!r} (allowed: {', '.join(DECISIONS)})")
        category = get(row, "Kategorie")
        if category and category not in CATEGORIES:
            raise ValueError(f"unknown category {category!r} (allowed: {', '.join(CATEGORIES)})")
        out.append(EntityRow(
            id=get(row, "ID"), category=category, role=get(row, "Rolle"), forms=forms,
            replacement=get(row, "Ersetzung"),
            decision=decision, merge_into=get(row, "Zusammenführen mit"),
            tool_replacement=get(row, "_tool_ersetzung"), tool_decision=get(row, "_tool_entscheidung"),
            leak_terms=leak, kind=get(row, "_art"),
        ))
    wb.close()
    return out
