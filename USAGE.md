# Using `ucdb`

UCDB 0.2 stores Akoma Ntoso / LegalDocML XML as the canonical representation and
indexes normalized legal nodes in SQLite.

## Initialize

```bash
ucdb --db ./law.sqlite3 init
```

The database contains `works`, `expressions`, `nodes`, `node_blocks`,
`revisions`, `node_changes`, `node_lines`, `rag_chunks`, `exports`, and a
trigram FTS5 index over nodes.

## Import Akoma Ntoso XML

```bash
ucdb --db ./law.sqlite3 import-akn ./law.xml \
  --work-id civil-code \
  --version 2026-04-29 \
  --language zho \
  --no-schema
```

Use `--source <file>` when the XML was generated from another source document
and the source hash should track that original input.

## Process Source Documents

Input repository layout:

```text
./input/<work-id>/<version-label>/<document>.{pdf,docx,odt,txt,md}
```

```bash
export OPENAI_API_KEY=sk-...
ucdb --db ./law.sqlite3 scan ./input
ucdb --db ./law.sqlite3 process ./input --language zho
```

The AI step emits a normalized legal tree. Python serializes that tree into
Akoma Ntoso XML deterministically before ingesting it.

## Query

```bash
ucdb --db ./law.sqlite3 query works
ucdb --db ./law.sqlite3 query expressions civil-code
ucdb --db ./law.sqlite3 query nodes 1
ucdb --db ./law.sqlite3 query node 12
ucdb --db ./law.sqlite3 query node 12 --xml
ucdb --db ./law.sqlite3 query akn 1
```

Search:

```bash
ucdb --db ./law.sqlite3 query search "契約" --work-id civil-code
ucdb --db ./law.sqlite3 query search "tax AND income" --raw
```

Diff:

```bash
ucdb --db ./law.sqlite3 query revisions civil-code
ucdb --db ./law.sqlite3 query revision 3
ucdb --db ./law.sqlite3 query diff 18
ucdb --db ./law.sqlite3 query diff-expressions civil-code \
  --from 2025-01-01 \
  --to 2026-04-29 \
  --node-eid art_12
```

Blame and history:

```bash
ucdb --db ./law.sqlite3 query blame civil-code art_12 --version 2026-04-29
ucdb --db ./law.sqlite3 query history civil-code art_12
```

Web browser:

```bash
ucdb --db ./law.sqlite3 serve --open
```

## Export

Exports are derived from canonical storage and do not mutate the database.

```bash
ucdb --db ./law.sqlite3 export json 1
ucdb --db ./law.sqlite3 export rag 1
ucdb --db ./law.sqlite3 export markdown 1
ucdb --db ./law.sqlite3 export html 1
```

`export rag` emits JSONL. Each line includes stable citation metadata, source
hashes, canonical hash, node `eId`, text hash, and normalized text hash.

## Schema Vocabulary

- `work`: stable legal work, for example a law or regulation.
- `expression`: a language/date/version expression of a work.
- `node`: a structural legal provision such as part, chapter, section, article,
  paragraph, point, attachment, appendix, or schedule.
- `node_eid`: stable Akoma Ntoso expression-local identifier.
- `canonical_xml`: stored Akoma Ntoso XML for an expression.
- `node_lines`: line-level first-introduction provenance.

## Verification

```bash
uv run python tests/test_history.py
uv run python tests/test_web.py
```
