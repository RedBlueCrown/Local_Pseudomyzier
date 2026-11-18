# anon – on-prem anonymisation of court decisions

Takes a Liechtenstein court decision (`.docx`) and returns

1. a **review copy** in which every anonymisation is a native Word *tracked change*
   (author = category, highlighted in the category colour) and every uncertain
   item is a **comment** by `Prüfen`, plus an editable `*.entities.xlsx`;
2. after human review, a **clean export** with all changes applied, review
   artefacts and metadata removed and a **leak check** passed.

Everything runs locally. No cloud APIs, no telemetry. Microsoft Word is not
needed (Word or LibreOffice are only used by the reviewer).

## Install

Python 3.12, Windows or Linux.

```bash
python -m venv .venv
```

```bash
.venv/bin/pip install .
```

Optional NER (GLiNER2 PII model, CPU; pulls PyTorch):

```bash
.venv/bin/pip install ".[ner]"
```

```bash
.venv/bin/anon setup-models
```

`setup-models` is the only command that uses the network. It checks the
licence on each model card against `ner.allowed_licenses` and stores the
weights in `./models/`. All other commands set `HF_HUB_OFFLINE=1` /
`TRANSFORMERS_OFFLINE=1` and never download anything. Without the weights the
tool runs with rules and gazetteers only and says so in the review copy.

## App: review in the browser

```bash
anon app
```

starts the review app on `http://127.0.0.1:8765` and opens it in the default
browser (`--port`, `--workdir`, `--no-browser`, `--config`). Close it with
Ctrl+C. Workflow:

1. **Drop the decision** (.docx) onto the start page. Analysis runs locally
   (progress bar); the case appears in the case list.
2. **Prüfansicht** shows the decision with every proposal inline: original
   struck through, replacement in a coloured label (colour = category).
   Dashed outline = *question* (uncertain, never changed unless you say so).
   * Click a mark → *Anonymisieren* / *Original lassen*, for all occurrences
     of that person at once, change the replacement, or *Nie anonymisieren*.
   * **N** jumps to the next open question, **A** / **X** decide, **J**/**K**
     move between marks, **E** toggles the result view.
   * Missed something? Select the text → *Anonymisieren als* Person / Firma /
     Vertreter / Ort / Grundstück / Datum/ID (every occurrence is found), or
     *Textstelle ersetzen …* for free rewording / `[…]` (guideline 2.4).
   * Side panel: **Fragen** (with the words around each question),
     **Entitäten** (all persons, places … with letters; rename, merge, keep),
     **Hinweise** (guideline 2.4 suggestions from the local LLM), **Info**.
3. **Ergebnis** shows the text exactly as it will be published.
4. **Exportieren** builds the review copy from your decisions, finalizes it and
   runs the leak check (same code as `anon finalize`). Only if it passes can
   you download *… anonymisiert.docx*. If it fails, the dialog lists each
   remaining original value with a *Zur Stelle* button.
5. **Fall abschliessen** deletes the original and the mapping (one-way).

Every decision is saved immediately. Cases live in `~/.anon/cases/` and are
deleted automatically after `retention_days` (settings, default 14).
A review copy edited in Word can still be finished from the start page
(*In Word geprüft? Fertigstellen …*).

**With a local LLM** (e.g. Qwen3 via Ollama, on the same machine or an
internal server):

```bash
ollama pull qwen3:8b
```

Then in the app: ⚙ *Einstellungen* → *Sprachmodell verwenden*, server
`http://localhost:11434/v1` (vLLM: `http://localhost:8000/v1`), *Modelle
laden*, pick the model, save. The status pill top right turns green; new
uploads use it (switch on the start page). Larger models (`qwen3:14b`,
`qwen3:32b`) give better 2.4 suggestions if the GPU has the memory. Qwen3's
thinking mode is switched off automatically (faster, same JSON).
The LLM classifies roles (party vs. doctor/expert), groups name variants and
suggests 2.4 passages; it never writes text. Hosts it may contact come from
`llm.allowed_hosts` in the config file only; the settings dialog cannot
widen them.

Security: the server listens on 127.0.0.1 only, checks the Host header (DNS
rebinding), requires a per-start token on every API call (other web pages in
the same browser cannot use the API), sends a strict Content-Security-Policy
and loads no external resources (works fully offline).

## Workflow in Word (command line)

```bash
anon review "Urteil.docx" -o out/ --no-llm
```

produces `out/Urteil.review.docx` and `out/Urteil.entities.xlsx`.

In Word (or LibreOffice):

| You see | Meaning | What to do |
|---|---|---|
| Change by **Partei** (yellow) | name → letter (A, B, …, AA …) | accept / reject |
| **Vertreter** (cyan) | representative → `***` | accept / reject |
| **Ort** (green) | place, street, postcode, field name, place adjective → `***`; Rubrum address deleted | accept / reject |
| **Grundstück** (magenta) | plot number → 1, 2, 3 … | accept / reject |
| **Datum/ID** (grey) | birth date, phone, e-mail, IBAN, plate, register no., AHV … → `***` | accept / reject |
| Comment by **Prüfen** | uncertain item, never changed automatically | replace in the text yourself, or delete / resolve the comment if nothing must change |
| Comment *Richtlinie 2.4* | passage that may identify someone (LLM suggestion) | rewrite manually if needed |

*Show Markup → Specific People* filters by category. The table at the top
(“Prüfübersicht”) lists every entity; it is removed automatically.

Bigger corrections go into the Excel file (it is the source of truth), then
re-render the review copy — always from the **original** input:

```bash
anon review "Urteil.docx" --entities out/Urteil.entities.xlsx -o out/
```

In `*.entities.xlsx` you can change `Ersetzung`, set `Entscheidung` to
`keep` / `review` / `auto`, merge two rows (`Zusammenführen mit` = ID of the
main row) or add a missed entity (new row: `Formen`, `Kategorie`). Untouched
letters are re-sequenced by first mention; edited ones stay fixed.

Finally:

```bash
anon finalize out/Urteil.review.docx --entities out/Urteil.entities.xlsx -o "Urteil anonymisiert.docx"
```

Finalize accepts all remaining changes (rejected ones stay rejected), removes
the summary, all comments and the tool’s bookmarks and highlights, strips
metadata (core/app/custom properties, people part, `w:rsid*`, document
variables, attached template, thumbnail, customXml, glossary, field codes and
hyperlink targets containing original values) and then runs the **leak
check**: every original surface form and number is searched (whole-token,
case-insensitive) in every part of the file. A hit is accepted only if the
reviewer kept the entity, rejected that change, deleted/resolved that
`Prüfen` comment, or it is inside a keep-list item (judge, citation …).
Anything else → exit code 1, nothing written. On success the entities file is
deleted (one-way); `--keep-entities` exists for evaluation only. The review
copy itself still contains the originals in its tracked changes.

Logs contain counts and categories only. `--show-values` prints leaked values
to the console (never to the log).

## Evaluation

```bash
anon evaluate --pairs testdata/ -o eval_out/ --no-llm
```

Pairs `… pseudonymisiert.docx` with `… anonymisiert.docx`, runs review +
finalize, diffs input↔gold and input↔tool on tokens and reports per category:
recall (2.4 passage edits excluded), exact-match rate, misses, extras (often
gold slips), and letter-sequence agreement. Other documents get a review copy
and a detection summary. `eval_out/` and `testdata/` are git-ignored.

## Configuration (maintained by court staff)

* `anon/config/rules.yaml` – categories/colours, **policy switches**
  (`third_parties`, `representatives`, `birth_dates`, `company_keep_activity`,
  `keep_municipal_authorities`, `review_threshold` …), Rubrum labels, titles,
  legal forms, street/plot/birth-date/ID patterns, NER and LLM settings.
* `anon/config/keeplist.yaml` – judge/clerk triggers, case numbers, citations,
  cited case names and authors, authorities, countries, court-seat line.
* `anon/config/gazetteers/*.txt` – places and adjectives (`?` = ambiguous),
  field names, products, first names, stop words, generic street nouns,
  surnames that are also nouns.

`--config my.yaml` is deep-merged over `rules.yaml`; it may contain a
`keeplist:` section and `gazetteer_dirs:` with additional `*.txt` files.

### Optional local LLM

```yaml
llm:
  enabled: true
  base_url: http://localhost:11434/v1   # Ollama; vLLM: http://localhost:8000/v1
  model: gpt-oss:20b
  allowed_hosts: [localhost, 127.0.0.1, "::1", llm.gericht.local]
```

Only allow-listed hosts are contacted (IP addresses must be internal),
proxy variables are ignored, redirects refused. The LLM classifies roles,
groups name variants and suggests 2.4 passages (comments only, quotes must be
verbatim). It never writes document text. If the endpoint is down the run
continues without it.

## Tests

```bash
.venv/bin/python -m pytest -q
```

Covers every item of the brief’s §7, including Reject-All = original and
Accept-All = expected text in all story parts, structural validation, an
independent LibreOffice (UNO) check of accept/reject and PDF conversion
(skipped when LibreOffice + `python3-uno` are missing), keep-list, genitive,
plots, legal forms, headers/footers/footnotes, metadata, leak check, xlsx
round trip, LLM allow-list and fail-safe, the app API (token/host checks,
decisions, entity actions, manual edits, export) and a browser run of the
whole review flow (`tests/test_ui.py`, needs Node.js with Playwright; set
`ANON_UI_SHOTS=dir` for screenshots).

## Status and evaluation

Evaluated on the three gold pairs (VGH 2026.21, RS 2025.177, SV 2025.13),
rules and gazetteers only (no NER weights were available in the build
environment):

| | |
|---|---|
| Recall of anonymisation changes (excl. 2.4 passages) | 169 / 169 (100 %) |
| …with the same replacement text as the gold | 85 % (rest: human slips such as `1Auflandungsarbeiten`, `CC AG` vs `C AG`, deleted vs. `***`) |
| Changes the gold does not have | 1 (a `Vaduz` the gold missed) |
| Letter sequence agreement | 12 / 15 (the gold itself departs from first-mention order) |
| Leak check after review | passes on all six inputs |

Not counted as anonymisation: 2 guideline-2.4 passage deletions (`[…]`, the
tool only suggests these), 8 editorial changes (grammar, corrupted words in
the pseudonymised input such as `vNovaussichtlichem`), 4 names the gold kept
(the two treating doctors in SV), 1 added descriptor block. The three StGH
decisions have no gold; their review copies were checked by hand
(judges, clerk, cited scholars and authorities stay; parties, companies,
representatives, witnesses, places, plots and IDs are replaced).

The rules were tuned on these documents. Expect new patterns in other
decisions: run `anon evaluate` on every new gold pair and extend
`rules.yaml` / gazetteers accordingly.

### Rules learned from the guideline and the gold files (beyond the brief)

* Birth-date generalisation is *year + 1* (`10.08.2000 → vor dem Jahr 2001`).
* `Gemeinde Vaduz` as a place designation is anonymised (`Gemeinde ***`);
  only an authority that is a party in the Rubrum is kept, with its address
  (`Liechtensteinische Invalidenversicherung, Amtsweg 1, 9490 Vaduz`).
* Benches name roles before *or after* the names (`Dr. Marc Bauer als
  Richter; Daniela Kollerer als Schriftführerin`, `Oberstrichterinnen …
  als weitere Mitglieder`); the copy is signed after `Für die Richtigkeit
  der Ausfertigung`. `seinen Präsidenten X` is not a judge.
* Rubrum variants: `Revisionswerber/` + address-only continuation line,
  `betreffend:` as party label, `Interessierte Parteien: 1. … 2. …`,
  `Belangte Behörde:` (kept), blocks spread over empty paragraphs,
  profession/nationality/detention lines deleted with the address.
* Cited scholars appear as `siehe Hans Lehrer, Titel, in: Kleiner/Lehrmann
  [Hrsg.], …, LPS Bd. 52, Schaan 2012` and author lists `A/B/ C, Titel`;
  publication places (`Bern 2022`) stay. Applicants and places of cited
  ECHR decisions stay (places there become questions, not changes).
* Field names also unquoted after `Feldweg`; water bodies (`Esche`) and
  clinic/prison/court towns (`Krankenhaus Beispielstadt`, `Gemeindegericht in
  Zagreb`) are places; `Plan Nr. …` is an identifier; plots also in
  compounds (`Strassengrundstück Nr. 1234`).

### Open points

* **NER was not run with real weights** (Hugging Face is unreachable from
  the build environment). The integration is tested with a stub backend.
  The GLiNER2 PII model id `fastino/gliner2-privacy-filter-PII-multi` is
  taken from the `gliner2` package documentation; `setup-models` verifies
  it and its licence before downloading. Label names may need adjusting in
  `ner.labels`.
* The LLM layer is tested against a fake local endpoint only.
* Review copies were validated structurally and in LibreOffice; opening them
  in Microsoft Word is still to be confirmed on site.

### Assumptions (all switchable)

* The court seat in the date line (`Vaduz, 10. Juni 2026`) is kept
  (`keeplist.yaml: court_seat_lines`); so are court/authority seats
  (`Fürstliches Landgericht, Abteilung 8, Vaduz`).
* Representatives are replaced as a whole block in the Rubrum (`***`); in the
  body titles stay (`Rechtsanwalt Dr. ***`).
* Address lines in their own paragraph/table cell are emptied; paragraph
  marks are not deleted.
* Tracked changes already present in the input are accepted before
  processing; all comments are removed on finalize (`finalize.remove_all_comments`).
* Open `Prüfen` comments block finalize until they are deleted, resolved, or
  the entity is set to `keep` in the Excel file. A change that was only
  partly rejected (deletion rejected, insertion kept) or whose inserted text
  was edited also blocks.
* Ambiguous place names (`Planken`, `Eschen`, `Steg`, `Maurer` …), bare
  surnames shared by two entities, a judge or a citation, and plot numbers
  outside a plot context are questions, not changes.
* In headers/footers Word allows no comments; uncertain items there are
  proposed as tracked changes by `Prüfen`.
