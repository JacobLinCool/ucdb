# Using `ucdb`

A walkthrough of every command, with realistic input and output. The examples below were captured against the synthetic ten-snapshot tax-code fixture under `tests/fixture.py`, so you can reproduce them locally:

```bash
uv run python tests/test_history.py    # builds and verifies the fixture
```

If you want to follow along interactively, the test driver also leaves a populated database behind when you import the fixture into your own SQLite file (see *Importing pre-generated XML* below).

> Output blocks in this document use a fixed terminal width. The CLI uses [Rich](https://rich.readthedocs.io/) tables, so live runs pick up your terminal's width and color.

---

## 1. Setting up

### Install

```bash
pip install ucdb           # or: uv tool install ucdb
```

### Top-level help

```bash
ucdb --help
```

```text
Usage: ucdb [OPTIONS] COMMAND [ARGS]...

  Universal Code Database — convert legal documents into SQLite.

Options:
  --db TEXT  Path to the SQLite database (env: UCDB_DB).  [default:
             (ucdb.sqlite3)]
  --version  Show the version and exit.
  --help     Show this message and exit.

Commands:
  import       Import a pre-generated USLM XML file.
  init         Initialize a new database file.
  process      Run the full AI pipeline over an input repository.
  process-one  Process a single document file.
  query        Inspect data stored in the database.
  scan         List documents found in an input repository.
```

### Initialize a database

```bash
ucdb --db ./tax-code.sqlite3 init
```

```text
Initialized database at tax-code.sqlite3
```

The file is created with foreign keys, WAL journaling, the FTS5 search index, and the line-blame table all in place. Re-running `init` against an existing file is a safe no-op (migrations run, schema is applied, nothing is destroyed). If you really need a clean slate, pass `--force`.

`UCDB_DB` is honored as a default, so you can drop `--db` from later commands by exporting `UCDB_DB=./tax-code.sqlite3`.

---

## 2. Ingesting documents

### Layout

`ucdb` expects an input tree shaped like this:

```text
./input/<code-id>/<version-label>/<document>.{pdf,docx,odt,txt,md}
```

Where `<code-id>` is the slug used as a primary key (e.g. `tax-code`), and `<version-label>` is whatever string sorts lexicographically (`2024-01-01`, `2024-Q1`, `1.3.0`, …). `<document>` is the source the AI extracts from.

```bash
ucdb scan ./input
```

```text
                      Documents under input
┏━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ code-id  ┃ version    ┃ file                                  ┃
┡━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ tax-code │ 2020-01-01 │ input/tax-code/2020-01-01/document.md │
│ tax-code │ 2020-07-01 │ input/tax-code/2020-07-01/document.md │
│ tax-code │ 2021-01-01 │ input/tax-code/2021-01-01/document.md │
│ tax-code │ 2021-07-01 │ input/tax-code/2021-07-01/document.md │
│ tax-code │ 2022-01-01 │ input/tax-code/2022-01-01/document.md │
│ tax-code │ 2022-07-01 │ input/tax-code/2022-07-01/document.md │
│ tax-code │ 2023-01-01 │ input/tax-code/2023-01-01/document.md │
│ tax-code │ 2023-07-01 │ input/tax-code/2023-07-01/document.md │
│ tax-code │ 2024-01-01 │ input/tax-code/2024-01-01/document.md │
│ tax-code │ 2024-07-01 │ input/tax-code/2024-07-01/document.md │
└──────────┴────────────┴───────────────────────────────────────┘
10 document(s) found
```

`scan` is read-only — it just reports what `process` would touch.

### Run the AI pipeline

```bash
export OPENAI_API_KEY=sk-...
ucdb process ./input
```

Each document is hashed, sent to the configured AI backend, validated as USLM, ingested as a `document_versions` row plus per-section `sections` rows, and joined to the previous version with a freshly computed revision **and** line-level blame.

To target an OpenAI-compatible provider (Gemini, Ollama, vLLM, …) set `OPENAI_BASE_URL`. To pin a specific model set `UCDB_MODEL` or pass `--model`.

### Importing pre-generated XML

If you already have valid USLM XML — for example because you produced it out of band, or because you are running the test fixture — skip the AI step:

```bash
ucdb import ./input/tax-code/2024-07-01/document.xml \
    --code-id tax-code --version 2024-07-01 \
    --source ./input/tax-code/2024-07-01/document.md
```

The `--source` flag points at the original document so the row's `source_hash` reflects the human-authored input, not the AI output. If you omit it, the XML file itself is hashed.

---

## 3. Inspecting what was loaded

### List codes and versions

```bash
ucdb query codes
```

```text
                                   Codes
┏━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ id       ┃ title ┃ created                   ┃ updated                   ┃
┡━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ tax-code │       │ 2026-04-28T20:50:47+00:00 │ 2026-04-28T20:50:47+00:00 │
└──────────┴───────┴───────────────────────────┴───────────────────────────┘
```

```bash
ucdb query versions tax-code
```

```text
                                                  Versions of tax-code
┏━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┓
┃ id ┃ version    ┃ status   ┃ source hash      ┃ xml hash         ┃ ai     ┃ validation ┃ parent ┃ processed          ┃
┡━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━┩
│  1 │ 2020-01-01 │ imported │ sha256:c2a60dc31 │ sha256:c2a60dc31 │ manual │ passed     │        │ 2026-04-28T20:50:… │
│  2 │ 2020-07-01 │ imported │ sha256:53490782a │ sha256:53490782a │ manual │ passed     │      1 │ 2026-04-28T20:50:… │
│  3 │ 2021-01-01 │ imported │ sha256:5091bdaf5 │ sha256:5091bdaf5 │ manual │ passed     │      2 │ 2026-04-28T20:50:… │
│  4 │ 2021-07-01 │ imported │ sha256:5ea7dbf77 │ sha256:5ea7dbf77 │ manual │ passed     │      3 │ 2026-04-28T20:50:… │
│  5 │ 2022-01-01 │ imported │ sha256:b7237278c │ sha256:b7237278c │ manual │ passed     │      4 │ 2026-04-28T20:50:… │
│  6 │ 2022-07-01 │ imported │ sha256:6dda9e7fa │ sha256:6dda9e7fa │ manual │ passed     │      5 │ 2026-04-28T20:50:… │
│  7 │ 2023-01-01 │ imported │ sha256:147adf86c │ sha256:147adf86c │ manual │ passed     │      6 │ 2026-04-28T20:50:… │
│  8 │ 2023-07-01 │ imported │ sha256:ebdbdf174 │ sha256:ebdbdf174 │ manual │ passed     │      7 │ 2026-04-28T20:50:… │
│  9 │ 2024-01-01 │ imported │ sha256:a8ebf4166 │ sha256:a8ebf4166 │ manual │ passed     │      8 │ 2026-04-28T20:50:… │
│ 10 │ 2024-07-01 │ imported │ sha256:05b751fec │ sha256:05b751fec │ manual │ passed     │      9 │ 2026-04-28T20:50:… │
└────┴────────────┴──────────┴──────────────────┴──────────────────┴────────┴────────────┴────────┴────────────────────┘
```

`parent` is the predecessor version row used for revision and blame computation.

### Drill into a single version

```bash
ucdb query sections 1 --limit 5
```

```text
                   Sections of version 1
┏━━━━┳━━━━━━━━━┳━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ id ┃ level   ┃ num ┃ heading             ┃ identifier   ┃
┡━━━━╇━━━━━━━━━╇━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│  1 │ title   │ I   │ Tax Code            │ /tax-code    │
│  2 │ section │ 1   │ Definitions         │ /tax-code/s1 │
│  3 │ section │ 2   │ Filing Requirements │ /tax-code/s2 │
│  4 │ section │ 3   │ Tax Rates           │ /tax-code/s3 │
│  5 │ section │ 4   │ Deductions          │ /tax-code/s4 │
└────┴─────────┴─────┴─────────────────────┴──────────────┘
```

`ucdb query section <section-id>` prints the body, and `--xml` prints the original USLM fragment.

```bash
ucdb query section 4
```

```text
section 3 Tax Rates
identifier: /tax-code/s3

The standard rate of tax is twenty-five percent of taxable income.
A reduced rate of fifteen percent applies to long-term capital gains.
An additional surtax of three percent applies to income above one million.
Different rates apply to non-residents as set out in subsection (d).
Indexed thresholds shall be adjusted annually for inflation.
The Authority may issue tables to assist taxpayers in computing tax.
Tax shall be rounded to the nearest whole unit of currency.
No fractional tax shall be assessed below one unit of currency.
Negative taxable income produces zero tax for the year.
Carryforward of losses is governed by section 4.
```

### Processing log

```bash
ucdb query log --limit 5
```

```text
                                  Processing log
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━┓
┃ when                      ┃ code     ┃ version ┃ step       ┃ status  ┃ message ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━┩
│ 2026-04-28T20:50:47+00:00 │ tax-code │      10 │ revision   │ success │         │
│ 2026-04-28T20:50:47+00:00 │ tax-code │      10 │ ingest.xml │ success │         │
│ 2026-04-28T20:50:47+00:00 │ tax-code │       9 │ revision   │ success │         │
│ 2026-04-28T20:50:47+00:00 │ tax-code │       9 │ ingest.xml │ success │         │
│ 2026-04-28T20:50:47+00:00 │ tax-code │       8 │ revision   │ success │         │
└───────────────────────────┴──────────┴─────────┴────────────┴─────────┴─────────┘
```

Add `--code-id` or `--version-id` to filter.

---

## 4. Full-text search

`ucdb query search` is backed by a SQLite **FTS5** index (`sections_fts`) and ranks results by BM25. By default the input is treated as a single phrase, so any punctuation or whitespace is matched literally:

```bash
ucdb query search "twenty-five percent" --code-id tax-code
```

```text
                    Search: 'twenty-five percent'
┏━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┓
┃ section id ┃ code     ┃ version    ┃ level   ┃ heading             ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━┩
│          4 │ tax-code │ 2020-01-01 │ section │ Tax Rates           │
│         15 │ tax-code │ 2020-07-01 │ section │ Tax Rates           │
│        111 │ tax-code │ 2024-07-01 │ section │ Tax Rates           │
│          3 │ tax-code │ 2020-01-01 │ section │ Filing Requirements │
│         14 │ tax-code │ 2020-07-01 │ section │ Filing Requirements │
…
└────────────┴──────────┴────────────┴─────────┴─────────────────────┘
```

Notice that `Tax Rates` shows up at v1, v2, and v10 — the rate was changed away from "twenty-five percent" in v3, then restored in v10. The search index reflects every snapshot, not just the latest.

### Raw FTS5 query syntax

`--raw` forwards your input as a real FTS5 query, unlocking prefix matches, boolean operators, `NEAR`, and column filters:

```bash
ucdb query search 'audit*' --raw --code-id tax-code --limit 5
```

```text
                     Search: 'audit*'
┏━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━┓
┃ section id ┃ code     ┃ version    ┃ level   ┃ heading ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━┩
│          8 │ tax-code │ 2020-01-01 │ section │ Audits  │
│         19 │ tax-code │ 2020-07-01 │ section │ Audits  │
│         31 │ tax-code │ 2021-01-01 │ section │ Audits  │
│         42 │ tax-code │ 2021-07-01 │ section │ Audits  │
│         53 │ tax-code │ 2022-01-01 │ section │ Audits  │
└────────────┴──────────┴────────────┴─────────┴─────────┘
```

Other useful raw forms:

```bash
ucdb query search 'tax AND rate'                  --raw   # boolean
ucdb query search 'income NEAR/3 deduction'       --raw   # proximity
ucdb query search 'heading:definitions'           --raw   # column filter
```

---

## 5. Revision tracking

Every successful import auto-computes a revision against the previous version of the same code. Revisions are listed by `(from, to)` pair with section-level counts:

```bash
ucdb query revisions tax-code
```

```text
                              Revisions of tax-code
┏━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━┳━━━┳━━━┳━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ rev id ┃ from       ┃ to         ┃  + ┃ - ┃ ~ ┃  = ┃ created                   ┃
┡━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━╇━━━╇━━━╇━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│      1 │ (initial)  │ 2020-01-01 │ 11 │ 0 │ 0 │  0 │ 2026-04-28T20:50:47+00:00 │
│      2 │ 2020-01-01 │ 2020-07-01 │  1 │ 0 │ 1 │ 10 │ 2026-04-28T20:50:47+00:00 │
│      3 │ 2020-07-01 │ 2021-01-01 │  0 │ 0 │ 1 │ 11 │ 2026-04-28T20:50:47+00:00 │
│      4 │ 2021-01-01 │ 2021-07-01 │  0 │ 1 │ 0 │ 11 │ 2026-04-28T20:50:47+00:00 │
│      5 │ 2021-07-01 │ 2022-01-01 │  0 │ 0 │ 2 │  9 │ 2026-04-28T20:50:47+00:00 │
│      6 │ 2022-01-01 │ 2022-07-01 │  1 │ 0 │ 1 │ 10 │ 2026-04-28T20:50:47+00:00 │
│      7 │ 2022-07-01 │ 2023-01-01 │  1 │ 0 │ 1 │ 11 │ 2026-04-28T20:50:47+00:00 │
│      8 │ 2023-01-01 │ 2023-07-01 │  0 │ 0 │ 2 │ 11 │ 2026-04-28T20:50:47+00:00 │
│      9 │ 2023-07-01 │ 2024-01-01 │  0 │ 1 │ 1 │ 11 │ 2026-04-28T20:50:47+00:00 │
│     10 │ 2024-01-01 │ 2024-07-01 │  0 │ 0 │ 3 │  9 │ 2026-04-28T20:50:47+00:00 │
└────────┴────────────┴────────────┴────┴───┴───┴────┴───────────────────────────┘
```

(Columns: additions / removals / modifications / unchanged sections.)

Drill into a single revision to see its section-level changes:

```bash
ucdb query revision 7
```

```text
Revision 7 2022-07-01 → 2023-01-01  +1 -0 ~1 =11
┏━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┳━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┓
┃ change id ┃ type     ┃ level   ┃ num ┃ identifier    ┃ heading            ┃
┡━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━╇━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━┩
│        20 │ added    │ section │ 5   │ /tax-code/s5  │ Credits            │
│        21 │ modified │ section │ 11  │ /tax-code/s11 │ Privacy of Returns │
└───────────┴──────────┴─────────┴─────┴───────────────┴────────────────────┘
```

`ucdb query revision <id> --type modified` filters to a single change type.

### One change's unified diff

`ucdb query diff <change-id>` prints the text diff stored on a `section_changes` row:

```bash
ucdb query diff 21
```

```text
--- before
+++ after
@@ -1,5 +1,5 @@
 Tax returns and return information are confidential.
 Disclosure is permitted only as authorized by law.
-Unauthorized disclosure is punishable as a criminal offense.
+Unauthorized disclosure is punishable by fine and imprisonment.
 Aggregated statistical data may be published.
 The Authority shall maintain reasonable safeguards.
```

---

## 6. Diff between any two versions (`git diff` analog)

`ucdb query diff-versions` compares any two versions on the fly — they don't need to be adjacent. Use the table form for a section-level summary:

```bash
ucdb query diff-versions tax-code --from 2020-01-01 --to 2024-07-01
```

```text
tax-code 2020-01-01 → 2024-07-01  +1 -0 ~10 =1
┏━━━━━━━━━━┳━━━━━━━━━┳━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┓
┃ type     ┃ level   ┃ num ┃ identifier    ┃ heading             ┃
┡━━━━━━━━━━╇━━━━━━━━━╇━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━┩
│ modified │ section │ 1   │ /tax-code/s1  │ Definitions         │
│ modified │ section │ 2   │ /tax-code/s2  │ Filing Requirements │
│ modified │ section │ 3   │ /tax-code/s3  │ Tax Rates           │
│ modified │ section │ 4   │ /tax-code/s4  │ Deductions          │
│ modified │ section │ 5   │ /tax-code/s5  │ Credits             │
│ modified │ section │ 6   │ /tax-code/s6  │ Penalties           │
│ modified │ section │ 7   │ /tax-code/s7  │ Audits              │
│ modified │ section │ 8   │ /tax-code/s8  │ Appeals             │
│ modified │ section │ 9   │ /tax-code/s9  │ Refunds             │
│ modified │ section │ 10  │ /tax-code/s10 │ Records Retention   │
│ added    │ section │ 11  │ /tax-code/s11 │ Privacy of Returns  │
└──────────┴─────────┴─────┴───────────────┴─────────────────────┘
```

Note how `/tax-code/s12` (added in v6, repealed in v9) is **not** in this list: the diff is between v1 and v10, so the round-trip cancels out. Conversely `/tax-code/s5` is `modified` rather than `added`/`removed` — it was repealed and re-enacted with new wording, and the net effect against v1 is a content change.

`--unified` prints the full unified diff for every modified section, optionally narrowed to a single identifier:

```bash
ucdb query diff-versions tax-code \
    --from 2020-01-01 --to 2024-07-01 \
    --identifier /tax-code/s3 --unified
```

```text
tax-code 2020-01-01 → 2024-07-01  +0 -0 ~1 =0

modified section 3 /tax-code/s3 — Tax Rates
--- before
+++ after
@@ -1,5 +1,5 @@
 The standard rate of tax is twenty-five percent of taxable income.
 A reduced rate of fifteen percent applies to long-term capital gains.
-An additional surtax of three percent applies to income above one million.
+An additional surtax of four percent applies to income above one million.
 Different rates apply to non-residents as set out in subsection (d).
 Indexed thresholds shall be adjusted annually for inflation.
```

---

## 7. Line-level blame (`git blame` analog)

Every imported version automatically populates `section_lines`: for each line of each section, the version that **first introduced that exact line** is recorded. Rewritten lines reset to the editing version; surviving lines flow forward unchanged. Re-introducing a repealed identifier acts as a fresh history root, so its lines are attributed to the re-enactment, not to the original.

```bash
ucdb query blame tax-code /tax-code/s1
```

```text
/tax-code/s1 @ tax-code/2024-07-01  (section 1: Definitions)
┏━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ line ┃ origin     ┃ text                                                                            ┃
┡━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│    1 │ 2020-01-01 │ In this Code, the following terms have the meanings given below.                │
│    2 │ 2020-07-01 │ Income means all gains, receipts, accretions, and other accessions to wealth.   │
│    3 │ 2020-01-01 │ Taxpayer means any person subject to the provisions of this Code.               │
│    4 │ 2020-01-01 │ Year means the calendar year unless context indicates otherwise.                │
│    5 │ 2020-01-01 │ Resident means a person domiciled in the jurisdiction.                          │
│    6 │ 2020-01-01 │ Non-resident means a person not domiciled in the jurisdiction.                  │
│    7 │ 2020-01-01 │ Spouse means a person legally married to the taxpayer.                          │
│    8 │ 2020-01-01 │ Dependent means a person whose support is supplied by the taxpayer.             │
│    9 │ 2020-01-01 │ Charity means an organization recognized as exempt under section 5.             │
│   10 │ 2020-01-01 │ Authority means the Department of Revenue and its agents.                       │
│   11 │ 2023-07-01 │ Digital asset means a cryptographically secured representation of value.        │
│   12 │ 2023-07-01 │ Pass-through entity means a partnership, S corporation, or similar arrangement. │
└──────┴────────────┴─────────────────────────────────────────────────────────────────────────────────┘
```

Reading the table top-to-bottom:

* Lines 1, 3–10 still credit the **original 2020-01-01 enactment** — they have not changed in five years and ten revisions.
* Line 2 was the wording fix in 2020-07-01 ("…and other accessions to wealth").
* Lines 11–12 are the 2023-07-01 amendment that added the digital-asset and pass-through definitions.

Blame at a specific historical version with `--version`:

```bash
ucdb query blame tax-code /tax-code/s1 --version 2021-01-01
```

`ucdb query history` answers the complementary question — *every revision that ever touched this identifier*:

```bash
ucdb query history tax-code /tax-code/s5
```

```text
                History of /tax-code/s5 in tax-code
┏━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┓
┃ rev id ┃ from       ┃ to         ┃ type    ┃ change id ┃ heading ┃
┡━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━┩
│      1 │ (initial)  │ 2020-01-01 │ added   │         6 │ Credits │
│      4 │ 2021-01-01 │ 2021-07-01 │ removed │        15 │ Credits │
│      7 │ 2022-07-01 │ 2023-01-01 │ added   │        20 │ Credits │
└────────┴────────────┴────────────┴─────────┴───────────┴─────────┘
```

The `change id` column points back into `section_changes`, so you can chain into `ucdb query diff <change-id>` for the unified diff of a particular event.

---

## 8. Dumping XML

```bash
ucdb query xml 7   # the canonical USLM stored for version row id 7
```

The output is the verbatim XML the AI produced (or the file you imported). Pipe it to `xmllint --format -` if you want it pretty-printed.

---

## 9. End-to-end example: "What did the standard tax rate look like over time?"

```bash
# Find the section that talks about the standard rate.
ucdb query search '"standard rate"' --code-id tax-code --limit 3

# Pin its identifier and read its history.
ucdb query history tax-code /tax-code/s3

# See the cumulative change vs the very first version.
ucdb query diff-versions tax-code \
    --from 2020-01-01 --to 2024-07-01 \
    --identifier /tax-code/s3 --unified

# Blame each line as it stands in the latest version.
ucdb query blame tax-code /tax-code/s3
```

That sequence — search → history → diff → blame — covers the four questions a regulator usually asks: *where is it*, *when did it change*, *what changed*, and *who authored the line in front of me*.

---

## 10. Reference cards

### Database & ingestion

| Command | Effect |
| --- | --- |
| `ucdb init` | Create / migrate the SQLite file. |
| `ucdb scan <root>` | Read-only listing of the input tree. |
| `ucdb process <root>` | Run the AI pipeline over every supported document. |
| `ucdb process-one <file> --code-id … --version …` | Pipeline for a single document. |
| `ucdb import <xml> --code-id … --version … [--source <doc>]` | Import pre-generated USLM XML, skipping the AI step. |
| `ucdb serve [--host 127.0.0.1] [--port 8000] [--open]` | Start the read-only web browser for search, docs, diffs, and metadata. |

### Querying

| Command | Effect |
| --- | --- |
| `ucdb query codes` | List all known codes. |
| `ucdb query versions <code-id>` | Versions of a code, with hashes and parent links. |
| `ucdb query sections <version-id>` | Sections inside one version. |
| `ucdb query section <section-id> [--xml]` | One section's body, plain text or XML. |
| `ucdb query xml <version-id>` | Dump the raw stored USLM XML. |
| `ucdb query log [--code-id …] [--version-id …]` | Processing log. |
| `ucdb query search <text> [--code-id …] [--raw]` | FTS5 search; `--raw` enables native FTS5 syntax. |
| `ucdb query revisions <code-id>` | Auto-computed adjacent-version revisions. |
| `ucdb query revision <revision-id> [--type added\|removed\|modified]` | Section-level changes inside a revision. |
| `ucdb query diff <change-id>` | Unified diff stored on a single `section_changes` row. |
| `ucdb query diff-versions <code-id> --from <v1> --to <v2> [--identifier <id>] [--unified]` | Diff arbitrary version pair. |
| `ucdb query blame <code-id> <identifier> [--version <v>]` | Per-line origin attribution for a section. |
| `ucdb query history <code-id> <identifier>` | Every revision that touched this identifier. |

### Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `UCDB_DB` | Default SQLite path. | `ucdb.sqlite3` |
| `OPENAI_API_KEY` | API key for the AI backend. | *(required for `process`)* |
| `OPENAI_BASE_URL` | OpenAI-compatible endpoint (Gemini, Ollama, vLLM …). | OpenAI default |
| `UCDB_MODEL` | Model id used for XML generation. | `gpt-4o-mini` |
| `UCDB_USLM_XSD` | Path to a USLM XSD; turns on strict schema validation. | *(off)* |
| `UCDB_JSON` | If set, `process`/`import` also dump a JSON summary. | *(off)* |
