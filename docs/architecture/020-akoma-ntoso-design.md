# UCDB 0.2.0 Akoma Ntoso Architecture

Status: implemented  
Date: 2026-04-29

## Overview

UCDB stores legal documents as Akoma Ntoso / LegalDocML XML plus normalized
SQLite tables optimized for search, diff, citation, provenance, and downstream
NLP/RAG use.

The storage artifact is a portable SQLite database. Canonical XML remains the
source of structured legal evidence, while normalized tables provide efficient
query and derived views.

```text
source document or official open data
-> extractor / parser / AI normalizer
-> normalized legal document model
-> Akoma Ntoso Taiwan profile XML
-> SQLite canonical storage + normalized node tables
-> derived outputs:
   - structural/text/legal diff
   - line provenance
   - FTS search
   - RAG JSONL
   - JSON
   - Markdown
   - HTML
```

## Standards

Akoma Ntoso Version 1.0 was approved as an OASIS Standard on 2018-08-29.
LegalDocML defines a shared XML vocabulary and metadata model for legal
documents, including parliamentary, legislative, and judicial documents.

The Akoma Ntoso namespace used by UCDB is:

```text
http://docs.oasis-open.org/legaldocml/ns/akn/3.0
```

Primary sources checked on 2026-04-29:

- OASIS LegalDocumentML TC: https://www.oasis-open.org/committees/legaldocml/
- OASIS Akoma Ntoso 1.0 standard page: https://www.oasis-open.org/standard/akn-v1-0/
- Akoma Ntoso vocabulary: https://docs.oasis-open.org/legaldocml/akn-core/v1.0/os/part1-vocabulary/akn-core-v1.0-os-part1-vocabulary.html
- Akoma Ntoso schemas: https://docs.oasis-open.org/legaldocml/akn-core/v1.0/os/part2-specs/schemas/
- Akoma Ntoso examples: https://docs.oasis-open.org/legaldocml/akn-core/v1.0/os/part2-specs/examples/

## Design Principles

- Keep one canonical legal representation: Akoma Ntoso XML generated from a
  normalized legal document model.
- Keep SQLite as the portable database artifact.
- Store source hashes, canonical XML hashes, node text hashes, and normalized
  text hashes for reproducibility.
- Treat `eId` as the stable provision-level key for diff, history, blame, and
  citation.
- Derive search indexes, RAG chunks, JSON, Markdown, HTML, and web views from
  canonical storage.
- Keep AI output constrained to a normalized tree; XML serialization is
  deterministic Python code.
- Prefer explicit validation over fallback parsing.

## Taiwan Profile

UCDB defines `tw-legaldocml@0.2` as a conservative Taiwan profile of Akoma
Ntoso. The profile maps local legal structures into standard Akoma Ntoso
elements where possible, and uses profile metadata only where local specificity
is required.

Supported document classes:

- constitution;
- law;
- regulation / order;
- administrative rule;
- judicial or constitutional interpretation;
- treaty / agreement;
- attachment, appendix, table, schedule;
- bilingual expressions where Chinese and English do not align one-to-one.

Required metadata:

- jurisdiction;
- document class;
- language;
- work URI;
- expression URI;
- manifestation URI;
- source authority when known;
- source URL or local source path when known;
- source hash;
- expression / effective / promulgation / enforcement dates when known;
- ingestion timestamp.

Hierarchy mapping:

```text
編 -> part or hcontainer name="bian"
章 -> chapter
節 -> section or hcontainer name="jie" depending on position
條 -> article
項 -> paragraph
款 -> point
目 -> indent or point with Taiwan profile metadata
附件 -> attachment
附表 -> schedule
附錄 -> appendix
```

Numbering rules:

- preserve original visible numbering in `num`;
- compute machine IDs separately as ASCII-safe `eId` values;
- support article forms such as `第十二條之一`;
- never use raw Chinese labels in machine identifiers;
- keep repealed/deleted provisions as explicit nodes when the source publishes
  them as part of the legal text.

Bilingual policy:

- Chinese and English are separate expressions;
- provision alignment is stored only when source evidence supports the link;
- text is not assumed to be paragraph-by-paragraph translation.

## Identity Model

UCDB uses three identity layers:

- `work_id`: stable local work key used by the database and CLI.
- `expression_id`: SQLite row id for one language/version expression.
- `node_eid`: Akoma Ntoso expression-local `eId` for structural provisions.

Akoma Ntoso URI examples:

```text
/akn/tw/act/law/civil-code
/akn/tw/act/law/civil-code/zho@2026-04-29
```

Node `eId` examples:

```text
art_12
art_12_1
art_12__para_2
chp_1__sec_2
```

## Database Schema

Core tables:

```text
works(
  id TEXT PRIMARY KEY,
  jurisdiction TEXT NOT NULL,
  document_class TEXT NOT NULL,
  title TEXT,
  source_authority TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
)

expressions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  work_id TEXT NOT NULL REFERENCES works(id) ON DELETE CASCADE,
  version_label TEXT NOT NULL,
  language TEXT NOT NULL,
  expression_date TEXT,
  effective_date TEXT,
  promulgation_date TEXT,
  enforcement_date TEXT,
  status TEXT NOT NULL,
  source_path TEXT,
  source_url TEXT,
  source_hash TEXT NOT NULL,
  source_size INTEGER,
  source_mime TEXT,
  canonical_format TEXT NOT NULL,
  canonical_xml TEXT,
  canonical_hash TEXT,
  akn_profile TEXT NOT NULL,
  validation_status TEXT,
  validation_message TEXT,
  parent_expression_id INTEGER,
  ai_provider TEXT,
  ai_model TEXT,
  ai_base_url TEXT,
  created_at TEXT NOT NULL,
  processed_at TEXT,
  UNIQUE(work_id, version_label, language),
  UNIQUE(work_id, source_hash, language)
)

nodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  expression_id INTEGER NOT NULL REFERENCES expressions(id) ON DELETE CASCADE,
  parent_id INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
  node_eid TEXT NOT NULL,
  node_type TEXT NOT NULL,
  profile_type TEXT,
  num TEXT,
  heading TEXT,
  text TEXT,
  xml_fragment TEXT NOT NULL,
  text_hash TEXT,
  normalized_text_hash TEXT,
  ordering INTEGER NOT NULL,
  depth INTEGER NOT NULL,
  source_locator TEXT,
  UNIQUE(expression_id, node_eid)
)

node_blocks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  block_eid TEXT,
  block_type TEXT NOT NULL,
  text TEXT,
  xml_fragment TEXT NOT NULL,
  ordering INTEGER NOT NULL,
  text_hash TEXT,
  normalized_text_hash TEXT
)

processing_log(...)
revisions(...)
node_changes(...)
node_lines(...)
rag_chunks(...)
exports(...)
nodes_fts(...)
```

## Diff Model

The revision engine compares two expressions of the same work and persists a
summary row plus detailed node changes.

Persisted change types:

- `added`;
- `removed`;
- `modified`.

The `details` JSON on modified changes can include structural events:

- `node_moved`;
- `type_changed`;
- `num_changed`;
- `heading_changed`;
- `text_changed`.

Matching order:

1. exact `node_eid`;
2. source-backed predecessor link, when available;
3. same parent and normalized legal number;
4. high-confidence normalized text match;
5. otherwise added/removed.

The CLI and web viewer render readable tables, but machine-readable JSON export
is the preferred downstream integration surface.

## Line Provenance

`node_lines` records first-introduction provenance for every nonblank line of a
node. During import, lines are aligned against the same `node_eid` in the parent
expression using `difflib.SequenceMatcher`.

Surviving lines inherit their original expression. New or rewritten lines are
attributed to the current expression.

## Search

`nodes_fts` is a SQLite FTS5 external-content table over node heading, text, and
`node_eid`.

The index uses the `trigram` tokenizer so Chinese, Japanese, Korean, and mixed
text can be matched by substring without relying on whitespace tokenization.

## RAG and NLP Surfaces

RAG chunks are derived from `nodes`, not used as canonical storage.

Each JSONL chunk includes:

- stable chunk id;
- exact text;
- display citation;
- work id;
- expression id;
- version label;
- language;
- node id;
- node `eId`;
- node type;
- source path / URL / hash;
- canonical XML hash;
- exact and normalized text hashes.

Embedding storage is optional and can be added alongside provider/model metadata
without changing canonical storage.

## Importers and Exporters

Importers:

- Akoma Ntoso XML import;
- raw PDF/DOCX/ODT/TXT/MD extraction plus AI-assisted normalization;
- future official open-data parsers.

Exporters:

- normalized JSON;
- RAG JSONL;
- Markdown;
- HTML;
- raw Akoma Ntoso XML via query command.

Exporters must not mutate canonical storage. They are reproducible derived
artifacts keyed by expression id and canonical hash.

## Module Layout

```text
src/ucdb/model.py       normalized LegalDocument / LegalNode dataclasses
src/ucdb/akn.py         Akoma Ntoso serializer/parser/validator
src/ucdb/tw_profile.py  Taiwan profile helpers and identifier normalization
src/ucdb/db.py          SQLite schema and data access
src/ucdb/ingest.py      Akoma Ntoso XML -> normalized tables
src/ucdb/ai.py          OpenAI-compatible extraction -> normalized model -> AKN XML
src/ucdb/process.py     pipeline orchestration
src/ucdb/revisions.py   structural diff engine
src/ucdb/blame.py       node line provenance
src/ucdb/exporters.py   JSON, RAG JSONL, Markdown, HTML renderers
src/ucdb/web.py         read-only browser and JSON API
src/ucdb/cli.py         CLI
```

## CLI Surface

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
ucdb query works
ucdb query expressions <work-id>
ucdb query nodes <expression-id>
ucdb query node <node-id> [--xml]
ucdb query search <text>
ucdb query akn <expression-id>
ucdb query revisions <work-id>
ucdb query revision <revision-id>
ucdb query diff <change-id>
ucdb query diff-expressions <work-id> --from ... --to ...
ucdb query blame <work-id> <node-eid> [--version ...]
ucdb query history <work-id> <node-eid>
ucdb serve
```

## Testing Requirements

Regression fixtures should cover:

- Taiwan law hierarchy: 編/章/節/條/項/款/目;
- article forms such as `第 X 條之一`;
- repeal and re-enactment;
- amendment history metadata;
- attachment/table structures;
- Chinese-only expressions;
- Chinese and English expressions with imperfect alignment;
- arbitrary version-pair diff;
- line blame;
- RAG JSONL export with stable citations;
- web viewer API queries.
