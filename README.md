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

## Workflow

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
round trip, LLM allow-list and fail-safe.

## Status and open points

* **The real test documents were not available** in the build environment,
  so the gold pairs could not be diffed and no recall numbers exist yet. The
  rules were built from the brief and checked on synthetic decisions with
  fake names. First step on site: put the files into `testdata/`, run
  `anon evaluate`, and tune `rules.yaml`/gazetteers from the report.
* **NER was not run with real weights** here (Hugging Face was unreachable
  from the build environment). The integration is tested with a stub
  backend. The GLiNER2 PII model id `fastino/gliner2-privacy-filter-PII-multi`
  is taken from the `gliner2` package documentation; `setup-models` verifies
  it and its licence before downloading. Its label names may need adjusting
  in `ner.labels`.

Assumptions made beyond the brief (all switchable):

* The court seat in the date line (`Vaduz, 10. Juni 2026`) is kept
  (`keeplist.yaml: court_seat_lines`).
* A municipal authority that is a party (`Gemeinde Vaduz`) is kept as a
  whole (`policy.keep_municipal_authorities`).
* Representatives are replaced as a whole block in the Rubrum (`***`); in the
  body titles stay (`Rechtsanwalt Dr. ***`).
* Address lines in their own paragraph/table cell are deleted, leaving an
  empty paragraph (paragraph marks are not deleted).
* Tracked changes already present in the input are accepted before
  processing; all comments are removed on finalize (`finalize.remove_all_comments`).
* Unresolved `Prüfen` comments block finalize until they are deleted,
  resolved, or the entity is set to `keep` in the Excel file.
* Plot numbers found outside a plot context, ambiguous place names
  (`Planken`, `Eschen`, `Steg`, `Maurer` …) and bare surnames shared by two
  entities or by a judge are comments, not changes.
* Comments are not possible in headers/footers (Word limitation); uncertain
  items there are listed in the summary table instead.
