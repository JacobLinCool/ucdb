# Universal Code Database

UCDB converts legal documents into a portable SQLite database with Akoma Ntoso /
LegalDocML as the canonical XML format.

The database stores canonical Akoma Ntoso XML per expression and derives query,
diff, blame, search, and downstream data surfaces from normalized node tables.

See [docs/architecture/020-akoma-ntoso-design.md](docs/architecture/020-akoma-ntoso-design.md)
for the design rationale.

## Pipeline

```text
source document or Akoma Ntoso XML
-> normalized legal model
-> Akoma Ntoso / LegalDocML XML
-> SQLite works + expressions + nodes
-> diff / blame / search / exports
```

The storage artifact is a single SQLite database. It includes:

- source hashes and processing provenance;
- canonical Akoma Ntoso XML and canonical hash;
- normalized structural nodes with `eId`, `num`, heading, text, XML fragment,
  text hashes, hierarchy, and document ordering;
- revision summaries and node-level changes;
- line-level provenance for `ucdb query blame`;
- FTS5 trigram search for CJK-friendly substring matching;
- placeholders for RAG chunks and reproducible exports.

## Install

```bash
pip install ucdb
# or
uv tool install ucdb
```

For local development:

```bash
uv sync
uv run ucdb --help
```

## Quick Start

```bash
ucdb init

# Import canonical Akoma Ntoso XML produced elsewhere.
ucdb import-akn ./law.xml --work-id civil-code --version 2026-04-29 --no-schema

ucdb query works
ucdb query expressions civil-code
ucdb query nodes 1
ucdb query search "契約" --work-id civil-code
ucdb query akn 1
```

AI-assisted processing is still available for PDF/DOCX/ODT/TXT/Markdown inputs:

```bash
export OPENAI_API_KEY=sk-...
ucdb process ./input
```

Input repositories are scanned as:

```text
./input/<work-id>/<version-label>/<document>.{pdf,docx,odt,txt,md}
```

## Configuration

| Environment variable | Purpose | Default |
| --- | --- | --- |
| `UCDB_DB` | Default SQLite path | `ucdb.sqlite3` |
| `OPENAI_API_KEY` | API key for AI-assisted normalization | required for `process` |
| `OPENAI_BASE_URL` | OpenAI-compatible endpoint | OpenAI default |
| `UCDB_MODEL` | Model used for structured extraction | `gpt-5.4-mini` |
| `UCDB_AKN_XSD` | Optional Akoma Ntoso XSD path for strict validation | off |
| `UCDB_JSON` | Emit JSON summaries for process/import commands | off |

## Main Commands

```text
ucdb init
ucdb scan <root>
ucdb process <root>
ucdb process-one <file> --work-id ... --version ...
ucdb import-akn <xml> --work-id ... --version ...
ucdb export json <expression-id>
ucdb export rag <expression-id>
ucdb export markdown <expression-id>
ucdb export html <expression-id>
ucdb serve

ucdb query works
ucdb query expressions <work-id>
ucdb query nodes <expression-id>
ucdb query node <node-id> [--xml]
ucdb query search <text> [--work-id ...]
ucdb query akn <expression-id>
ucdb query revisions <work-id>
ucdb query revision <revision-id>
ucdb query diff <change-id>
ucdb query diff-expressions <work-id> --from <v1> --to <v2> [--node-eid ...]
ucdb query blame <work-id> <node-eid> [--version ...]
ucdb query history <work-id> <node-eid>
ucdb query log
```

## Core Modules

```text
model.py       normalized LegalDocument / LegalNode dataclasses
akn.py         Akoma Ntoso parser, serializer, and validation helpers
tw_profile.py  Taiwan profile helpers and identifier normalization
db.py          SQLite schema and data access
ingest.py      Akoma Ntoso XML -> normalized tables
ai.py          OpenAI-compatible extraction -> normalized model -> AKN XML
process.py     pipeline orchestration
revisions.py   structural node diff engine
blame.py       line-level provenance
web.py         read-only browser data layer and HTTP server
exporters.py   JSON, RAG JSONL, Markdown, and HTML renderers
```

## Development

Run the current no-network regression tests:

```bash
uv run python tests/test_history.py
uv run python tests/test_web.py
```

The fixture imports ten Akoma Ntoso snapshots and verifies search, arbitrary
version-pair diff, repeal/re-enactment handling, line blame, history, and web
store queries.
