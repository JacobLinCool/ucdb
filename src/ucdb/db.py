"""SQLite schema and data access for UCDB 0.2."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = "2"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS works (
    id TEXT PRIMARY KEY,
    jurisdiction TEXT NOT NULL,
    document_class TEXT NOT NULL,
    title TEXT,
    source_authority TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS expressions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id TEXT NOT NULL,
    version_label TEXT NOT NULL,
    language TEXT NOT NULL,
    expression_date TEXT,
    effective_date TEXT,
    promulgation_date TEXT,
    enforcement_date TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    source_path TEXT,
    source_url TEXT,
    source_hash TEXT NOT NULL,
    source_size INTEGER,
    source_mime TEXT,
    canonical_format TEXT NOT NULL DEFAULT 'akn+xml',
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
    UNIQUE(work_id, source_hash, language),
    FOREIGN KEY(work_id) REFERENCES works(id) ON DELETE CASCADE,
    FOREIGN KEY(parent_expression_id) REFERENCES expressions(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_expressions_work ON expressions(work_id);
CREATE INDEX IF NOT EXISTS idx_expressions_status ON expressions(status);
CREATE INDEX IF NOT EXISTS idx_expressions_parent ON expressions(parent_expression_id);

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expression_id INTEGER NOT NULL,
    parent_id INTEGER,
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
    UNIQUE(expression_id, node_eid),
    FOREIGN KEY(expression_id) REFERENCES expressions(id) ON DELETE CASCADE,
    FOREIGN KEY(parent_id) REFERENCES nodes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_nodes_expression ON nodes(expression_id);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_nodes_eid ON nodes(node_eid);

CREATE TABLE IF NOT EXISTS node_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL,
    block_eid TEXT,
    block_type TEXT NOT NULL,
    text TEXT,
    xml_fragment TEXT NOT NULL,
    ordering INTEGER NOT NULL,
    text_hash TEXT,
    normalized_text_hash TEXT,
    FOREIGN KEY(node_id) REFERENCES nodes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_node_blocks_node ON node_blocks(node_id);

CREATE TABLE IF NOT EXISTS processing_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id TEXT,
    expression_id INTEGER,
    step TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT,
    details TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(work_id) REFERENCES works(id) ON DELETE SET NULL,
    FOREIGN KEY(expression_id) REFERENCES expressions(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_log_work ON processing_log(work_id);
CREATE INDEX IF NOT EXISTS idx_log_expression ON processing_log(expression_id);

CREATE TABLE IF NOT EXISTS revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id TEXT NOT NULL,
    from_expression_id INTEGER,
    to_expression_id INTEGER NOT NULL,
    nodes_added INTEGER NOT NULL DEFAULT 0,
    nodes_removed INTEGER NOT NULL DEFAULT 0,
    nodes_modified INTEGER NOT NULL DEFAULT 0,
    nodes_unchanged INTEGER NOT NULL DEFAULT 0,
    summary TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(from_expression_id, to_expression_id),
    FOREIGN KEY(work_id) REFERENCES works(id) ON DELETE CASCADE,
    FOREIGN KEY(from_expression_id) REFERENCES expressions(id) ON DELETE CASCADE,
    FOREIGN KEY(to_expression_id) REFERENCES expressions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_revisions_work ON revisions(work_id);
CREATE INDEX IF NOT EXISTS idx_revisions_to ON revisions(to_expression_id);

CREATE TABLE IF NOT EXISTS node_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL,
    change_type TEXT NOT NULL,
    node_eid TEXT,
    node_type TEXT,
    num TEXT,
    heading TEXT,
    from_node_id INTEGER,
    to_node_id INTEGER,
    text_diff TEXT,
    details TEXT,
    FOREIGN KEY(revision_id) REFERENCES revisions(id) ON DELETE CASCADE,
    FOREIGN KEY(from_node_id) REFERENCES nodes(id) ON DELETE SET NULL,
    FOREIGN KEY(to_node_id) REFERENCES nodes(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_node_changes_rev ON node_changes(revision_id);
CREATE INDEX IF NOT EXISTS idx_node_changes_type ON node_changes(change_type);
CREATE INDEX IF NOT EXISTS idx_node_changes_eid ON node_changes(node_eid);

CREATE TABLE IF NOT EXISTS node_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    text TEXT NOT NULL,
    origin_expression_id INTEGER NOT NULL,
    origin_node_id INTEGER,
    FOREIGN KEY(node_id) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY(origin_expression_id) REFERENCES expressions(id) ON DELETE CASCADE,
    FOREIGN KEY(origin_node_id) REFERENCES nodes(id) ON DELETE SET NULL,
    UNIQUE(node_id, line_no)
);

CREATE INDEX IF NOT EXISTS idx_node_lines_node ON node_lines(node_id);
CREATE INDEX IF NOT EXISTS idx_node_lines_origin ON node_lines(origin_expression_id);

CREATE TABLE IF NOT EXISTS rag_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expression_id INTEGER NOT NULL,
    node_id INTEGER NOT NULL,
    chunk_eid TEXT NOT NULL,
    citation TEXT NOT NULL,
    text TEXT NOT NULL,
    metadata TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(expression_id, chunk_eid),
    FOREIGN KEY(expression_id) REFERENCES expressions(id) ON DELETE CASCADE,
    FOREIGN KEY(node_id) REFERENCES nodes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS exports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expression_id INTEGER,
    export_type TEXT NOT NULL,
    exporter_version TEXT NOT NULL,
    canonical_hash TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(expression_id) REFERENCES expressions(id) ON DELETE CASCADE
);

CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    heading,
    text,
    node_eid,
    content='nodes',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON nodes BEGIN
    INSERT INTO nodes_fts(rowid, heading, text, node_eid)
    VALUES (new.id, new.heading, new.text, new.node_eid);
END;

CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, heading, text, node_eid)
    VALUES ('delete', old.id, old.heading, old.text, old.node_eid);
END;

CREATE TRIGGER IF NOT EXISTS nodes_au AFTER UPDATE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, heading, text, node_eid)
    VALUES ('delete', old.id, old.heading, old.text, old.node_eid);
    INSERT INTO nodes_fts(rowid, heading, text, node_eid)
    VALUES (new.id, new.heading, new.text, new.node_eid);
END;
"""

_DROP_TABLES = [
    "nodes_fts",
    "sections_fts",
    "exports",
    "rag_chunks",
    "node_lines",
    "section_lines",
    "node_changes",
    "section_changes",
    "revisions",
    "processing_log",
    "node_blocks",
    "nodes",
    "sections",
    "expressions",
    "document_versions",
    "works",
    "codes",
    "schema_meta",
]

_DROP_TRIGGERS = [
    "nodes_ai",
    "nodes_ad",
    "nodes_au",
    "sections_ai",
    "sections_ad",
    "sections_au",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | str) -> None:
    """Create a UCDB 0.2 database, replacing older UCDB schemas."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        if _current_schema_version(conn) not in {None, SCHEMA_VERSION}:
            _drop_known_schema(conn)
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            ("schema_version", SCHEMA_VERSION),
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
            ("created_at", utcnow()),
        )


def _current_schema_version(conn: sqlite3.Connection) -> str | None:
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def _drop_known_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    for trigger in _DROP_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in _DROP_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute("PRAGMA foreign_keys = ON")


def upsert_work(
    conn: sqlite3.Connection,
    work_id: str,
    *,
    jurisdiction: str = "tw",
    document_class: str = "law",
    title: str | None = None,
    source_authority: str | None = None,
) -> None:
    now = utcnow()
    conn.execute(
        """
        INSERT INTO works(
            id, jurisdiction, document_class, title, source_authority,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            jurisdiction = excluded.jurisdiction,
            document_class = excluded.document_class,
            title = COALESCE(excluded.title, works.title),
            source_authority = COALESCE(excluded.source_authority, works.source_authority),
            updated_at = excluded.updated_at
        """,
        (work_id, jurisdiction, document_class, title, source_authority, now, now),
    )


def find_expression_by_hash(
    conn: sqlite3.Connection, work_id: str, source_hash: str, language: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM expressions
        WHERE work_id = ? AND source_hash = ? AND language = ?
        """,
        (work_id, source_hash, language),
    ).fetchone()


def create_expression(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    version_label: str,
    language: str,
    source_hash: str,
    akn_profile: str,
    expression_date: str | None = None,
    effective_date: str | None = None,
    promulgation_date: str | None = None,
    enforcement_date: str | None = None,
    source_path: str | None = None,
    source_url: str | None = None,
    source_size: int | None = None,
    source_mime: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO expressions(
            work_id, version_label, language, expression_date, effective_date,
            promulgation_date, enforcement_date, source_path, source_url, source_hash,
            source_size, source_mime, akn_profile, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            work_id,
            version_label,
            language,
            expression_date,
            effective_date,
            promulgation_date,
            enforcement_date,
            source_path,
            source_url,
            source_hash,
            source_size,
            source_mime,
            akn_profile,
            utcnow(),
        ),
    )
    return int(cur.lastrowid)


def set_expression_status(
    conn: sqlite3.Connection,
    expression_id: int,
    status: str,
    *,
    canonical_xml: str | None = None,
    canonical_hash: str | None = None,
    canonical_format: str | None = None,
    ai_provider: str | None = None,
    ai_model: str | None = None,
    ai_base_url: str | None = None,
    validation_status: str | None = None,
    validation_message: str | None = None,
    parent_expression_id: int | None = None,
    mark_processed: bool = False,
) -> None:
    fields = ["status = ?"]
    params: list[Any] = [status]
    optional = {
        "canonical_xml": canonical_xml,
        "canonical_hash": canonical_hash,
        "canonical_format": canonical_format,
        "ai_provider": ai_provider,
        "ai_model": ai_model,
        "ai_base_url": ai_base_url,
        "validation_status": validation_status,
        "validation_message": validation_message,
        "parent_expression_id": parent_expression_id,
    }
    for key, value in optional.items():
        if value is not None:
            fields.append(f"{key} = ?")
            params.append(value)
    if mark_processed:
        fields.append("processed_at = ?")
        params.append(utcnow())
    params.append(expression_id)
    conn.execute(
        f"UPDATE expressions SET {', '.join(fields)} WHERE id = ?",
        params,
    )


def previous_expression(
    conn: sqlite3.Connection, work_id: str, version_label: str, language: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM expressions
        WHERE work_id = ? AND version_label < ? AND language = ? AND status = 'imported'
        ORDER BY version_label DESC
        LIMIT 1
        """,
        (work_id, version_label, language),
    ).fetchone()


def clear_expression_nodes(conn: sqlite3.Connection, expression_id: int) -> None:
    conn.execute("DELETE FROM nodes WHERE expression_id = ?", (expression_id,))


def insert_node(
    conn: sqlite3.Connection,
    *,
    expression_id: int,
    parent_id: int | None,
    node_eid: str,
    node_type: str,
    profile_type: str | None,
    num: str | None,
    heading: str | None,
    text: str | None,
    xml_fragment: str,
    text_hash: str | None,
    normalized_text_hash: str | None,
    ordering: int,
    depth: int,
    source_locator: str | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO nodes(
            expression_id, parent_id, node_eid, node_type, profile_type,
            num, heading, text, xml_fragment, text_hash, normalized_text_hash,
            ordering, depth, source_locator
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            expression_id,
            parent_id,
            node_eid,
            node_type,
            profile_type,
            num,
            heading,
            text,
            xml_fragment,
            text_hash,
            normalized_text_hash,
            ordering,
            depth,
            source_locator,
        ),
    )
    return int(cur.lastrowid)


def insert_node_block(
    conn: sqlite3.Connection,
    *,
    node_id: int,
    block_eid: str | None,
    block_type: str,
    text: str | None,
    xml_fragment: str,
    ordering: int,
    text_hash: str | None,
    normalized_text_hash: str | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO node_blocks(
            node_id, block_eid, block_type, text, xml_fragment,
            ordering, text_hash, normalized_text_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            node_id,
            block_eid,
            block_type,
            text,
            xml_fragment,
            ordering,
            text_hash,
            normalized_text_hash,
        ),
    )
    return int(cur.lastrowid)


def log_event(
    conn: sqlite3.Connection,
    *,
    step: str,
    status: str,
    message: str | None = None,
    work_id: str | None = None,
    expression_id: int | None = None,
    details: dict | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO processing_log(
            work_id, expression_id, step, status, message, details, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            work_id,
            expression_id,
            step,
            status,
            message,
            json.dumps(details, ensure_ascii=False) if details else None,
            utcnow(),
        ),
    )


def list_works(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM works ORDER BY id"))


def list_expressions(conn: sqlite3.Connection, work_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM expressions WHERE work_id = ? ORDER BY version_label, language",
            (work_id,),
        )
    )


def get_expression(conn: sqlite3.Connection, expression_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM expressions WHERE id = ?", (expression_id,)
    ).fetchone()


def find_expression_by_label(
    conn: sqlite3.Connection, work_id: str, version_label: str, language: str = "zho"
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM expressions
        WHERE work_id = ? AND version_label = ? AND language = ?
        """,
        (work_id, version_label, language),
    ).fetchone()


def latest_expression(
    conn: sqlite3.Connection, work_id: str, language: str = "zho"
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM expressions
        WHERE work_id = ? AND language = ? AND status = 'imported'
        ORDER BY version_label DESC
        LIMIT 1
        """,
        (work_id, language),
    ).fetchone()


def list_nodes(conn: sqlite3.Connection, expression_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM nodes WHERE expression_id = ? ORDER BY ordering",
            (expression_id,),
        )
    )


def find_node_by_eid(
    conn: sqlite3.Connection, expression_id: int, node_eid: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM nodes WHERE expression_id = ? AND node_eid = ?",
        (expression_id, node_eid),
    ).fetchone()


def get_node_lines(conn: sqlite3.Connection, node_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT
                nl.line_no,
                nl.text,
                nl.origin_expression_id,
                nl.origin_node_id,
                e.version_label AS origin_version_label,
                e.work_id AS origin_work_id,
                e.effective_date AS origin_effective_date
            FROM node_lines nl
            JOIN expressions e ON e.id = nl.origin_expression_id
            WHERE nl.node_id = ?
            ORDER BY nl.line_no
            """,
            (node_id,),
        )
    )


def list_revisions(conn: sqlite3.Connection, work_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT r.*,
                   fe.version_label AS from_label,
                   te.version_label AS to_label
            FROM revisions r
            LEFT JOIN expressions fe ON fe.id = r.from_expression_id
            JOIN expressions te ON te.id = r.to_expression_id
            WHERE r.work_id = ?
            ORDER BY te.version_label, r.id
            """,
            (work_id,),
        )
    )


def get_revision(conn: sqlite3.Connection, revision_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT r.*,
               fe.version_label AS from_label,
               te.version_label AS to_label,
               te.work_id AS work_id_resolved
        FROM revisions r
        LEFT JOIN expressions fe ON fe.id = r.from_expression_id
        JOIN expressions te ON te.id = r.to_expression_id
        WHERE r.id = ?
        """,
        (revision_id,),
    ).fetchone()


def list_node_changes(
    conn: sqlite3.Connection,
    revision_id: int,
    *,
    change_type: str | None = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM node_changes WHERE revision_id = ?"
    params: list[Any] = [revision_id]
    if change_type:
        sql += " AND change_type = ?"
        params.append(change_type)
    sql += " ORDER BY id"
    return list(conn.execute(sql, params))


def get_node_change(conn: sqlite3.Connection, change_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM node_changes WHERE id = ?", (change_id,)
    ).fetchone()


def node_history(
    conn: sqlite3.Connection, work_id: str, node_eid: str
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT
                nc.id AS change_id,
                nc.change_type,
                nc.node_type,
                nc.num,
                nc.heading,
                nc.text_diff,
                nc.from_node_id,
                nc.to_node_id,
                r.id AS revision_id,
                r.from_expression_id,
                r.to_expression_id,
                fe.version_label AS from_label,
                te.version_label AS to_label,
                te.effective_date AS to_effective_date
            FROM node_changes nc
            JOIN revisions r ON r.id = nc.revision_id
            LEFT JOIN expressions fe ON fe.id = r.from_expression_id
            JOIN expressions te ON te.id = r.to_expression_id
            WHERE r.work_id = ? AND nc.node_eid = ?
            ORDER BY te.version_label, nc.id
            """,
            (work_id, node_eid),
        )
    )


def diff_expressions(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    from_expression_id: int,
    to_expression_id: int,
    node_eid: str | None = None,
):
    from .revisions import CompareStats, compare_node_sets

    from_nodes = list_nodes(conn, from_expression_id)
    to_nodes = list_nodes(conn, to_expression_id)
    changes, stats = compare_node_sets(from_nodes, to_nodes)
    if node_eid:
        changes = [c for c in changes if c.node_eid == node_eid]
        stats = CompareStats(
            added=sum(1 for c in changes if c.change_type == "added"),
            removed=sum(1 for c in changes if c.change_type == "removed"),
            modified=sum(1 for c in changes if c.change_type == "modified"),
            unchanged=0,
        )
    return changes, stats


def search_nodes(
    conn: sqlite3.Connection,
    query: str,
    *,
    work_id: str | None = None,
    limit: int = 50,
    raw: bool = False,
) -> list[sqlite3.Row]:
    match_expr = query if raw else _fts_phrase(query)
    sql = """
        SELECT n.*, e.work_id, e.version_label, e.language, bm25(nodes_fts) AS rank
        FROM nodes_fts
        JOIN nodes n ON n.id = nodes_fts.rowid
        JOIN expressions e ON e.id = n.expression_id
        WHERE nodes_fts MATCH ?
    """
    params: list[Any] = [match_expr]
    if work_id:
        sql += " AND e.work_id = ?"
        params.append(work_id)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    return list(conn.execute(sql, params))


def list_processing_log(
    conn: sqlite3.Connection,
    *,
    work_id: str | None = None,
    expression_id: int | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM processing_log WHERE 1=1"
    params: list[Any] = []
    if work_id:
        sql += " AND work_id = ?"
        params.append(work_id)
    if expression_id is not None:
        sql += " AND expression_id = ?"
        params.append(expression_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return list(conn.execute(sql, params))


def _fts_phrase(query: str) -> str:
    return '"' + query.replace('"', '""') + '"'
