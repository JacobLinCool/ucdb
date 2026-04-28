# Universal Code Database

A pipeline for converting legacy legal documents — PDF, DOCX, ODT, plain text, and Markdown — into a queryable SQLite database based on the United States Legislative Markup (USLM) XML schema.

## Goal

This project builds a one-way document ingestion system:

```text
raw document → AI extraction → legislative XML → SQLite database
```

The database stores USLM XML as the canonical structured representation, while revision history, file hashes, processing metadata, AI provenance, and version-to-version diffs are tracked in SQLite metadata tables.

## What is USLM?

[**United States Legislative Markup**](https://github.com/usgpo/uslm) is the XML schema maintained by the U.S. House Office of the Law Revision Counsel and the Government Publishing Office (GPO) for encoding U.S. legislative documents — most notably the *United States Code* itself. It is the canonical, machine-readable representation that publishers, courts, and downstream tooling all consume.

We adopt it here because:

* **It is hierarchical and self-describing.** A statute naturally decomposes into nested levels — title → subtitle → chapter → subchapter → part → subpart → section → subsection → paragraph → subparagraph → clause — and USLM has elements for every one of them.
* **Numbering and headings are first-class.** Each container exposes `<num>` and `<heading>` children; prose lives inside `<content><p>…</p></content>`. This separation is exactly the structure a database wants.
* **Identifiers are stable.** USLM elements carry an `identifier` attribute (e.g. `/us/usc/t26/s1`) that survives revisions and is ideal as a cross-version join key.
* **It is namespace-scoped.** The official namespace is `http://schemas.gpo.gov/xml/uslm`, with metadata expressed via Dublin Core (`http://purl.org/dc/elements/1.1/`). This avoids ambiguity when local names collide (e.g. `<dc:title>` versus a USLM `<title>` container).

A minimal valid document looks like this:

```xml
<uslm xmlns="http://schemas.gpo.gov/xml/uslm"
      xmlns:dc="http://purl.org/dc/elements/1.1/">
  <meta>
    <dc:title>Tax Code 2024 Edition</dc:title>
    <dc:type>code</dc:type>
    <dc:identifier>/tax-code/2024-01-01</dc:identifier>
  </meta>
  <main>
    <title identifier="/tax-code">
      <num>I</num>
      <heading>Tax Code</heading>
      <section identifier="/tax-code/s1">
        <num>1</num>
        <heading>Definitions</heading>
        <content>
          <p>Income means the gross receipts of any taxpayer.</p>
        </content>
      </section>
    </title>
  </main>
</uslm>
```

The official schemas (`USLM-2.x.xsd`, `uslm-table-module.xsd`, etc.) are published under [usgpo/uslm](https://github.com/usgpo/uslm). This project does not bundle them; you can plug a copy in via `UCDB_USLM_XSD` (see *Configuration* below) to enable strict validation.

## Input Repository Structure

Users maintain an input document repository like:

```text
./somewhere/<code-id>/<version-or-date>/document.{pdf,docx,odt,txt,md}
```

Example:

```text
./input/tax-code/2024-01-01/document.pdf
./input/tax-code/2024-06-01/document.docx
```

`<code-id>` becomes the primary key in the `codes` table; `<version-or-date>` is recorded as the version label. The actual filename is preserved on the row.

## Features

* Create, add to, and query a legislative code database
* Batch-process document repositories
* Record source file hashes (SHA-256) for reproducibility
* Track versions, dates, changes, and processing metadata
* Convert legacy documents into United States Legislative Markup XML
* Store structured results in SQLite
* Support AI-assisted processing via GPT API and compatible endpoints such as Gemini, Ollama, or other OpenAI-compatible APIs

## Installation

From PyPI:

```bash
pip install ucdb
# or
uv tool install ucdb     # gives you a global `ucdb` command
```

For local development the project uses [uv](https://docs.astral.sh/uv/) and a `src/` layout:

```bash
uv sync          # create a venv and install dependencies
uv run ucdb --help
```

The wheel installs a `ucdb` console script. You can also invoke the package directly with `python -m ucdb`.

## Quick Start

```bash
# 1. point at an OpenAI-compatible backend
export OPENAI_API_KEY=sk-...
# optional: use a different provider
# export OPENAI_BASE_URL=http://localhost:11434/v1
# export UCDB_MODEL=gpt-5.4-mini

# 2. create the database
ucdb init                       # writes ./ucdb.sqlite3

# 3. scan the input repo (no side effects)
ucdb scan ./input

# 4. run the full pipeline
ucdb process ./input

# 5. inspect what was loaded
ucdb query codes
ucdb query versions tax-code
ucdb query sections 1
ucdb query search "income"
```

If you have already produced USLM XML out-of-band, skip the AI step:

```bash
ucdb import ./pre-generated.xml \
    --code-id tax-code --version 2024-01-01 \
    --source ./input/tax-code/2024-01-01/document.pdf
```

## Configuration

| Environment variable | Purpose                                                           | Default          |
| -------------------- | ----------------------------------------------------------------- | ---------------- |
| `UCDB_DB`            | Default SQLite path (overridden by `--db`)                        | `ucdb.sqlite3`   |
| `OPENAI_API_KEY`     | API key for the AI backend                                        | *(required)*     |
| `OPENAI_BASE_URL`    | Endpoint for OpenAI-compatible providers (Gemini, Ollama, vLLM …) | OpenAI default   |
| `UCDB_MODEL`         | Model id used for XML generation                                  | `gpt-5.4-mini`    |
| `UCDB_USLM_XSD`      | Path to a USLM XSD; enables strict schema validation              | *(off)*          |
| `UCDB_JSON`          | If set, `process`/`import` also dump a JSON summary               | *(off)*          |

All of these can also be supplied through a `.env` file. On startup `ucdb` looks
for a `.env` in the current working directory (and any parent), loading it
without overriding values already present in the real environment — so an
explicit `export OPENAI_API_KEY=…` always wins. Pass `--env-file <path>` to
load an explicit file that *does* override existing values.

```bash
# .env
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=http://localhost:11434/v1
UCDB_MODEL=gpt-5.4-mini
```

## Components

### Core library

Layout under `src/ucdb/`:

```text
db.py          SQLite schema + data-access helpers
extract.py     PDF / DOCX / ODT / plain-text / Markdown extraction
hashing.py     SHA-256 file hashing & MIME guessing
ai.py          OpenAI-compatible client → USLM XML
xml_utils.py   Namespace-aware USLM parsing + XSD validation
ingest.py      XML → flat sections rows
revisions.py   Version-to-version diff engine
scan.py        Walk <root>/<code>/<version>/<file>
process.py     End-to-end pipeline orchestration
cli.py         Click-based CLI
blame.py       Line-level provenance computation (git-blame analog)
```

A 10-snapshot synthetic test suite lives under `tests/`. It builds a ~100-line legislative code, mutates it through ten plausible edit scenarios (additions, removals, repeal-and-reenact, multi-section rewrites), drives the result through the full ingest pipeline, and asserts that diff/blame/history return the expected attributions. Run it with `uv run python tests/test_history.py` (or `uv run pytest tests/` once pytest is installed).

The library is intentionally thin and synchronous; every operation runs inside a single `db.connect()` transaction so partial work rolls back on failure.

### CLI

```text
ucdb init                              create a new database
ucdb scan <root>                       list documents in an input repo
ucdb process <root>                    run the full AI pipeline
ucdb process-one <file> --code-id … --version …
ucdb import <xml>     --code-id … --version … [--source <doc>]
ucdb query codes
ucdb query versions <code-id>
ucdb query sections <version-id>
ucdb query section  <section-id> [--xml]
ucdb query search   <text> [--code-id <id>] [--raw]
ucdb query log       [--code-id <id>] [--version-id <id>]
ucdb query xml       <version-id>
ucdb query revisions <code-id>
ucdb query revision  <revision-id> [--type added|removed|modified]
ucdb query diff      <change-id>
ucdb query diff-versions <code-id> --from <v1> --to <v2> [--identifier <id>] [--unified]
ucdb query blame     <code-id> <identifier> [--version <v>]
ucdb query history   <code-id> <identifier>
```

## Database schema

```text
codes(id PK, title, description, created_at, updated_at)

document_versions(
  id PK, code_id FK→codes,
  version_label, effective_date,
  source_path, source_hash, source_size, source_mime,    -- source provenance
  xml_content, xml_hash,                                  -- generated USLM + its hash
  ai_provider, ai_model, ai_base_url,                     -- AI provenance
  validation_status, validation_message,                  -- well-formed/schema check
  parent_version_id FK→document_versions,                 -- previous version of the same code
  status,                                                 -- pending|extracting|generating|validating|imported|failed
  created_at, processed_at,
  UNIQUE(code_id, version_label),
  UNIQUE(code_id, source_hash)
)

sections(
  id PK, version_id FK, parent_id FK→sections,
  level,                                    -- title|chapter|section|subsection|…
  identifier, num, heading, content,
  xml_fragment, ordering
)

processing_log(
  id PK, code_id, version_id, step, status,
  message, details(JSON), created_at
)

-- Revision tracking — populated automatically after each successful import.
revisions(
  id PK, code_id FK,
  from_version_id FK→document_versions,     -- NULL for the very first version
  to_version_id   FK→document_versions,
  sections_added, sections_removed,
  sections_modified, sections_unchanged,
  summary(JSON), created_at,
  UNIQUE(from_version_id, to_version_id)
)

section_changes(
  id PK, revision_id FK,
  change_type,                              -- added | removed | modified
  identifier, level, num, heading,          -- snapshot for quick listing
  from_section_id FK→sections,              -- NULL for added
  to_section_id   FK→sections,              -- NULL for removed
  text_diff                                 -- unified diff of section content (modified only)
)

-- Line-level provenance — backs `ucdb query blame`. Populated after each
-- successful import alongside the revision computation.
section_lines(
  id PK, section_id FK→sections,
  line_no,                                  -- 1-based, content-line index (blanks skipped)
  text,                                     -- the line itself
  origin_version_id FK→document_versions,   -- version that first introduced this exact line
  origin_section_id FK→sections,            -- the section row in origin_version_id
  UNIQUE(section_id, line_no)
)
```

`sections.parent_id` reconstructs the legislative hierarchy as a self-referencing tree; `ordering` preserves document order; `xml_fragment` keeps the original USLM subtree so nothing is lost in the flattening step.

### Full-text search

`sections` is shadowed by a SQLite **FTS5** virtual table, `sections_fts`, that indexes `heading`, `content`, and `identifier` with the `unicode61` tokenizer (diacritics folded). The index is kept in sync with `sections` via `AFTER INSERT/UPDATE/DELETE` triggers, so writes through the normal data-access layer require no extra plumbing — older databases are migrated and back-filled automatically the next time `ucdb` opens them.

`ucdb query search` and `db.search_sections()` use this index and rank results by BM25:

```bash
ucdb query search "income tax"           # phrase match (default — input is quoted for you)
ucdb query search 'income*' --raw        # raw FTS5 syntax: prefix, AND/OR/NOT, NEAR, column filters
ucdb query search 'heading:definitions' --raw
```

## Revision tracking

The pipeline keeps two levels of detail about how documents change over time:

* **USLM XML** — stored verbatim per version in `document_versions.xml_content`. This is the canonical structured representation; it carries the legislative hierarchy, section text, and any USLM amendment-related markup the AI emits.
* **SQLite metadata tables** — `revisions` summarises each version-to-version transition (counts of added / removed / modified / unchanged sections), and `section_changes` records one row per affected section, joined back to the actual `sections` rows on either side. Modified sections additionally carry a unified `text_diff` so you can see exactly what wording changed without re-running the AI.

Sections are aligned across versions by their USLM `identifier` attribute — the only key the schema guarantees to be stable across revisions. Sections without an identifier are reported as anonymous additions/removals.

Revisions are computed automatically after each successful `process` or `import` run, comparing the new version against the most recent previously-imported version of the same code (lexicographic by `version_label`). To inspect them:

```bash
ucdb query revisions tax-code           # one row per (from → to) transition
ucdb query revision 2                   # section-level changes for revision 2
ucdb query revision 2 --type modified   # filter to a single change type
ucdb query diff 4                       # unified text diff for one section change
```

Each `document_versions` row also records its **AI provenance** (`ai_provider`, `ai_model`, `ai_base_url`) and the SHA-256 of the generated XML (`xml_hash`), so a given output can be traced back to a specific model/endpoint and verified later for tampering.

### Diff between any two versions

`ucdb query diff` was already there for inspecting one persisted `section_changes` row. `ucdb query diff-versions` adds the `git diff <a> <b>` analog: pick any two versions of the same code (they need not be adjacent) and the comparison is recomputed on the fly using the same engine that drives the persisted revisions.

```bash
ucdb query diff-versions tax-code --from 2020-01-01 --to 2024-07-01
ucdb query diff-versions tax-code --from 2020-01-01 --to 2024-07-01 \
    --identifier /tax-code/s3 --unified
```

### Line-level blame

Every line of every section carries a **first-introduction stamp** in `section_lines`. When a new version is imported, each section's lines are aligned against the same identifier in the predecessor version using `difflib.SequenceMatcher`; lines that survive unchanged inherit the predecessor's `origin_version_id`, while edited or new lines are stamped with the current version. The result is a `git blame` analog: every line of every section, at every version, knows when it was authored.

```bash
ucdb query blame tax-code /tax-code/s5                    # blame at the latest version
ucdb query blame tax-code /tax-code/s5 --version 2023-01-01
ucdb query history tax-code /tax-code/s5                  # every revision that touched this identifier
```

If a section is removed and later re-introduced under the same identifier, the re-introduction acts as a fresh history root: blame attributes its lines to the re-introduction version, not transitively to the original. This matches the legal reality that a repealed-then-reenacted provision is a new enactment.

The pipeline also computes blame for the very first version of a code: every line is stamped with the first version, since there is nothing earlier to inherit from.

## Processing Workflow

```text
1. Hash the source file (sha256)        — dedupe + reproducibility
2. Upsert the code, create a version row
3. Extract plain text (pypdf / python-docx / odfpy; `.txt`/`.md` pass through verbatim)
4. Send text to the AI backend; receive USLM XML
5. Parse + well-formedness check (lxml)
6. Structural check: <uslm> root with a <main>; optional XSD validation
7. Walk USLM-namespace elements, insert sections (parent ↔ child)
8. Mark version `imported`; record AI provenance + xml hash
9. Compute a revision against the previous version (if any) and persist section-level diffs
10. Record every step in processing_log
```

Re-running `ucdb process` is a no-op for any source whose hash already exists with status `imported`. Use `--reprocess` to force.

## Design Principles

* **One-way ingestion.** Input documents are the source of truth; the database is regenerated from them.
* **Namespace-correct USLM.** Hierarchical containers are matched only when they live in the USLM namespace (or have no namespace), so foreign elements like `<dc:title>` cannot be mistaken for USLM `<title>` containers.
* **Reproducible processing.** Every version row carries the SHA-256 of its source, plus a step-by-step `processing_log` audit trail.
* **Traceable changes across document versions.** Multiple versions of the same code coexist as separate `document_versions` rows; their sections share `identifier` values where present, so cross-version queries are straightforward.
* **Modular AI backend.** Anything that speaks the OpenAI Chat Completions wire format works — swap providers via `OPENAI_BASE_URL` / `UCDB_MODEL`.
* **SQLite-first.** Single-file database, WAL journaling, foreign keys enabled — portable, embeddable, easy to back up.
