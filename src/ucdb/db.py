"""SQLite schema and core data access layer."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = "1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS codes (
    id TEXT PRIMARY KEY,
    title TEXT,
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_id TEXT NOT NULL,
    version_label TEXT NOT NULL,
    effective_date TEXT,
    source_path TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    source_size INTEGER,
    source_mime TEXT,
    xml_content TEXT,
    xml_hash TEXT,
    ai_provider TEXT,
    ai_model TEXT,
    ai_base_url TEXT,
    validation_status TEXT,
    validation_message TEXT,
    parent_version_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    processed_at TEXT,
    UNIQUE(code_id, version_label),
    UNIQUE(code_id, source_hash),
    FOREIGN KEY(code_id) REFERENCES codes(id) ON DELETE CASCADE,
    FOREIGN KEY(parent_version_id) REFERENCES document_versions(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_versions_code ON document_versions(code_id);
CREATE INDEX IF NOT EXISTS idx_versions_status ON document_versions(status);
CREATE INDEX IF NOT EXISTS idx_versions_parent ON document_versions(parent_version_id);

CREATE TABLE IF NOT EXISTS sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL,
    parent_id INTEGER,
    level TEXT NOT NULL,
    identifier TEXT,
    num TEXT,
    heading TEXT,
    content TEXT,
    xml_fragment TEXT,
    ordering INTEGER NOT NULL,
    FOREIGN KEY(version_id) REFERENCES document_versions(id) ON DELETE CASCADE,
    FOREIGN KEY(parent_id) REFERENCES sections(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sections_version ON sections(version_id);
CREATE INDEX IF NOT EXISTS idx_sections_parent ON sections(parent_id);
CREATE INDEX IF NOT EXISTS idx_sections_level ON sections(level);
CREATE INDEX IF NOT EXISTS idx_sections_identifier ON sections(identifier);

CREATE TABLE IF NOT EXISTS processing_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_id TEXT,
    version_id INTEGER,
    step TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT,
    details TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(version_id) REFERENCES document_versions(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_log_version ON processing_log(version_id);
CREATE INDEX IF NOT EXISTS idx_log_code ON processing_log(code_id);

-- A revision compares two versions of the same code. `from_version_id` is
-- NULL for the very first version (everything is "added").
CREATE TABLE IF NOT EXISTS revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code_id TEXT NOT NULL,
    from_version_id INTEGER,
    to_version_id INTEGER NOT NULL,
    sections_added INTEGER NOT NULL DEFAULT 0,
    sections_removed INTEGER NOT NULL DEFAULT 0,
    sections_modified INTEGER NOT NULL DEFAULT 0,
    sections_unchanged INTEGER NOT NULL DEFAULT 0,
    summary TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(from_version_id, to_version_id),
    FOREIGN KEY(code_id) REFERENCES codes(id) ON DELETE CASCADE,
    FOREIGN KEY(from_version_id) REFERENCES document_versions(id) ON DELETE CASCADE,
    FOREIGN KEY(to_version_id)   REFERENCES document_versions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_revisions_code ON revisions(code_id);
CREATE INDEX IF NOT EXISTS idx_revisions_to ON revisions(to_version_id);

CREATE TABLE IF NOT EXISTS section_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id INTEGER NOT NULL,
    change_type TEXT NOT NULL,         -- added | removed | modified
    identifier TEXT,
    level TEXT,
    num TEXT,
    heading TEXT,
    from_section_id INTEGER,
    to_section_id INTEGER,
    text_diff TEXT,
    FOREIGN KEY(revision_id)     REFERENCES revisions(id) ON DELETE CASCADE,
    FOREIGN KEY(from_section_id) REFERENCES sections(id)  ON DELETE SET NULL,
    FOREIGN KEY(to_section_id)   REFERENCES sections(id)  ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_section_changes_rev ON section_changes(revision_id);
CREATE INDEX IF NOT EXISTS idx_section_changes_type ON section_changes(change_type);
CREATE INDEX IF NOT EXISTS idx_section_changes_identifier ON section_changes(identifier);

-- Line-level provenance: every line of a section in a particular version is
-- tagged with the version that *first introduced that exact line* for the
-- section's identifier chain. This is the storage backing `ucdb query blame`.
-- Lines are inherited from the predecessor version when they survive an edit,
-- otherwise they are stamped with the current version.
CREATE TABLE IF NOT EXISTS section_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    section_id INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    text TEXT NOT NULL,
    origin_version_id INTEGER NOT NULL,
    origin_section_id INTEGER,
    FOREIGN KEY(section_id)        REFERENCES sections(id)          ON DELETE CASCADE,
    FOREIGN KEY(origin_version_id) REFERENCES document_versions(id) ON DELETE CASCADE,
    FOREIGN KEY(origin_section_id) REFERENCES sections(id)          ON DELETE SET NULL,
    UNIQUE(section_id, line_no)
);

CREATE INDEX IF NOT EXISTS idx_section_lines_section ON section_lines(section_id);
CREATE INDEX IF NOT EXISTS idx_section_lines_origin_version ON section_lines(origin_version_id);

-- Full-text index over section heading/content/identifier. Stored as an
-- external-content FTS5 table that mirrors `sections` via triggers, so the
-- canonical row data lives in `sections` and the index is purely derived.
CREATE VIRTUAL TABLE IF NOT EXISTS sections_fts USING fts5(
    heading,
    content,
    identifier,
    content='sections',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS sections_ai AFTER INSERT ON sections BEGIN
    INSERT INTO sections_fts(rowid, heading, content, identifier)
    VALUES (new.id, new.heading, new.content, new.identifier);
END;

CREATE TRIGGER IF NOT EXISTS sections_ad AFTER DELETE ON sections BEGIN
    INSERT INTO sections_fts(sections_fts, rowid, heading, content, identifier)
    VALUES ('delete', old.id, old.heading, old.content, old.identifier);
END;

CREATE TRIGGER IF NOT EXISTS sections_au AFTER UPDATE ON sections BEGIN
    INSERT INTO sections_fts(sections_fts, rowid, heading, content, identifier)
    VALUES ('delete', old.id, old.heading, old.content, old.identifier);
    INSERT INTO sections_fts(rowid, heading, content, identifier)
    VALUES (new.id, new.heading, new.content, new.identifier);
END;
"""

# Per-version migrations applied lazily when an older DB is opened. Each entry
# is a list of statements that move the schema from version `key` to `key+1`.
MIGRATIONS: dict[str, list[str]] = {
    "1": [
        "ALTER TABLE document_versions ADD COLUMN xml_hash TEXT",
        "ALTER TABLE document_versions ADD COLUMN ai_provider TEXT",
        "ALTER TABLE document_versions ADD COLUMN ai_model TEXT",
        "ALTER TABLE document_versions ADD COLUMN ai_base_url TEXT",
        "ALTER TABLE document_versions ADD COLUMN validation_status TEXT",
        "ALTER TABLE document_versions ADD COLUMN validation_message TEXT",
        "ALTER TABLE document_versions ADD COLUMN parent_version_id INTEGER REFERENCES document_versions(id)",
        "CREATE INDEX IF NOT EXISTS idx_versions_parent ON document_versions(parent_version_id)",
        """CREATE TABLE IF NOT EXISTS revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code_id TEXT NOT NULL,
            from_version_id INTEGER,
            to_version_id INTEGER NOT NULL,
            sections_added INTEGER NOT NULL DEFAULT 0,
            sections_removed INTEGER NOT NULL DEFAULT 0,
            sections_modified INTEGER NOT NULL DEFAULT 0,
            sections_unchanged INTEGER NOT NULL DEFAULT 0,
            summary TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(from_version_id, to_version_id),
            FOREIGN KEY(code_id) REFERENCES codes(id) ON DELETE CASCADE,
            FOREIGN KEY(from_version_id) REFERENCES document_versions(id) ON DELETE CASCADE,
            FOREIGN KEY(to_version_id)   REFERENCES document_versions(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_revisions_code ON revisions(code_id)",
        "CREATE INDEX IF NOT EXISTS idx_revisions_to ON revisions(to_version_id)",
        """CREATE TABLE IF NOT EXISTS section_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            revision_id INTEGER NOT NULL,
            change_type TEXT NOT NULL,
            identifier TEXT,
            level TEXT,
            num TEXT,
            heading TEXT,
            from_section_id INTEGER,
            to_section_id INTEGER,
            text_diff TEXT,
            FOREIGN KEY(revision_id)     REFERENCES revisions(id) ON DELETE CASCADE,
            FOREIGN KEY(from_section_id) REFERENCES sections(id)  ON DELETE SET NULL,
            FOREIGN KEY(to_section_id)   REFERENCES sections(id)  ON DELETE SET NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_section_changes_rev ON section_changes(revision_id)",
        "CREATE INDEX IF NOT EXISTS idx_section_changes_type ON section_changes(change_type)",
        "CREATE INDEX IF NOT EXISTS idx_section_changes_identifier ON section_changes(identifier)",
    ],
    "2": [
        """CREATE VIRTUAL TABLE IF NOT EXISTS sections_fts USING fts5(
            heading,
            content,
            identifier,
            content='sections',
            content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        )""",
        """CREATE TRIGGER IF NOT EXISTS sections_ai AFTER INSERT ON sections BEGIN
            INSERT INTO sections_fts(rowid, heading, content, identifier)
            VALUES (new.id, new.heading, new.content, new.identifier);
        END""",
        """CREATE TRIGGER IF NOT EXISTS sections_ad AFTER DELETE ON sections BEGIN
            INSERT INTO sections_fts(sections_fts, rowid, heading, content, identifier)
            VALUES ('delete', old.id, old.heading, old.content, old.identifier);
        END""",
        """CREATE TRIGGER IF NOT EXISTS sections_au AFTER UPDATE ON sections BEGIN
            INSERT INTO sections_fts(sections_fts, rowid, heading, content, identifier)
            VALUES ('delete', old.id, old.heading, old.content, old.identifier);
            INSERT INTO sections_fts(rowid, heading, content, identifier)
            VALUES (new.id, new.heading, new.content, new.identifier);
        END""",
        # Backfill the FTS index from any sections already present in the DB.
        "INSERT INTO sections_fts(sections_fts) VALUES('rebuild')",
    ],
    "3": [
        """CREATE TABLE IF NOT EXISTS section_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            section_id INTEGER NOT NULL,
            line_no INTEGER NOT NULL,
            text TEXT NOT NULL,
            origin_version_id INTEGER NOT NULL,
            origin_section_id INTEGER,
            FOREIGN KEY(section_id)        REFERENCES sections(id)          ON DELETE CASCADE,
            FOREIGN KEY(origin_version_id) REFERENCES document_versions(id) ON DELETE CASCADE,
            FOREIGN KEY(origin_section_id) REFERENCES sections(id)          ON DELETE SET NULL,
            UNIQUE(section_id, line_no)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_section_lines_section ON section_lines(section_id)",
        "CREATE INDEX IF NOT EXISTS idx_section_lines_origin_version ON section_lines(origin_version_id)",
    ],
}


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
    """Create the database file (or migrate an existing one) and apply the schema."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        # Run migrations first so the SCHEMA_SQL re-application (with its
        # IF NOT EXISTS clauses) doesn't reference columns that an older DB
        # hasn't grown yet.
        _migrate(conn)
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
    # On a brand-new DB the meta table does not exist yet — treat that as
    # "no recorded version" so callers can apply the initial schema.
    try:
        cur = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'")
    except sqlite3.OperationalError:
        return None
    row = cur.fetchone()
    return row[0] if row else None


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply pending migrations to bring an existing DB up to ``SCHEMA_VERSION``."""
    current = _current_schema_version(conn) or "0"
    while current in MIGRATIONS and current != SCHEMA_VERSION:
        for stmt in MIGRATIONS[current]:
            conn.execute(stmt)
        current = str(int(current) + 1)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
            ("schema_version", current),
        )


def upsert_code(
    conn: sqlite3.Connection,
    code_id: str,
    title: str | None = None,
    description: str | None = None,
) -> None:
    now = utcnow()
    conn.execute(
        """
        INSERT INTO codes(id, title, description, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = COALESCE(excluded.title, codes.title),
            description = COALESCE(excluded.description, codes.description),
            updated_at = excluded.updated_at
        """,
        (code_id, title, description, now, now),
    )


def find_version_by_hash(
    conn: sqlite3.Connection, code_id: str, source_hash: str
) -> sqlite3.Row | None:
    cur = conn.execute(
        "SELECT * FROM document_versions WHERE code_id = ? AND source_hash = ?",
        (code_id, source_hash),
    )
    return cur.fetchone()


def create_version(
    conn: sqlite3.Connection,
    *,
    code_id: str,
    version_label: str,
    source_path: str,
    source_hash: str,
    source_size: int | None,
    source_mime: str | None,
    effective_date: str | None = None,
) -> int:
    now = utcnow()
    cur = conn.execute(
        """
        INSERT INTO document_versions(
            code_id, version_label, effective_date,
            source_path, source_hash, source_size, source_mime,
            status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            code_id,
            version_label,
            effective_date,
            source_path,
            source_hash,
            source_size,
            source_mime,
            now,
        ),
    )
    return int(cur.lastrowid)


def set_version_status(
    conn: sqlite3.Connection,
    version_id: int,
    status: str,
    *,
    xml_content: str | None = None,
    xml_hash: str | None = None,
    ai_provider: str | None = None,
    ai_model: str | None = None,
    ai_base_url: str | None = None,
    validation_status: str | None = None,
    validation_message: str | None = None,
    parent_version_id: int | None = None,
    mark_processed: bool = False,
) -> None:
    fields = ["status = ?"]
    params: list[Any] = [status]
    if xml_content is not None:
        fields.append("xml_content = ?")
        params.append(xml_content)
    if xml_hash is not None:
        fields.append("xml_hash = ?")
        params.append(xml_hash)
    if ai_provider is not None:
        fields.append("ai_provider = ?")
        params.append(ai_provider)
    if ai_model is not None:
        fields.append("ai_model = ?")
        params.append(ai_model)
    if ai_base_url is not None:
        fields.append("ai_base_url = ?")
        params.append(ai_base_url)
    if validation_status is not None:
        fields.append("validation_status = ?")
        params.append(validation_status)
    if validation_message is not None:
        fields.append("validation_message = ?")
        params.append(validation_message)
    if parent_version_id is not None:
        fields.append("parent_version_id = ?")
        params.append(parent_version_id)
    if mark_processed:
        fields.append("processed_at = ?")
        params.append(utcnow())
    params.append(version_id)
    conn.execute(
        f"UPDATE document_versions SET {', '.join(fields)} WHERE id = ?",
        params,
    )


def previous_version(
    conn: sqlite3.Connection, code_id: str, version_label: str
) -> sqlite3.Row | None:
    """Return the most recent imported version of *code_id* whose label sorts before *version_label*."""
    cur = conn.execute(
        """
        SELECT * FROM document_versions
        WHERE code_id = ? AND version_label < ? AND status = 'imported'
        ORDER BY version_label DESC
        LIMIT 1
        """,
        (code_id, version_label),
    )
    return cur.fetchone()


def list_revisions(conn: sqlite3.Connection, code_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT r.*,
                   vf.version_label AS from_label,
                   vt.version_label AS to_label
            FROM revisions r
            LEFT JOIN document_versions vf ON vf.id = r.from_version_id
            JOIN document_versions vt ON vt.id = r.to_version_id
            WHERE r.code_id = ?
            ORDER BY vt.version_label
            """,
            (code_id,),
        )
    )


def get_revision(conn: sqlite3.Connection, revision_id: int) -> sqlite3.Row | None:
    cur = conn.execute(
        """
        SELECT r.*,
               vf.version_label AS from_label,
               vt.version_label AS to_label,
               vt.code_id AS code_id_resolved
        FROM revisions r
        LEFT JOIN document_versions vf ON vf.id = r.from_version_id
        JOIN document_versions vt ON vt.id = r.to_version_id
        WHERE r.id = ?
        """,
        (revision_id,),
    )
    return cur.fetchone()


def list_section_changes(
    conn: sqlite3.Connection,
    revision_id: int,
    *,
    change_type: str | None = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM section_changes WHERE revision_id = ?"
    params: list[Any] = [revision_id]
    if change_type:
        sql += " AND change_type = ?"
        params.append(change_type)
    sql += " ORDER BY id"
    return list(conn.execute(sql, params))


def get_section_change(conn: sqlite3.Connection, change_id: int) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM section_changes WHERE id = ?", (change_id,))
    return cur.fetchone()


def clear_version_sections(conn: sqlite3.Connection, version_id: int) -> None:
    conn.execute("DELETE FROM sections WHERE version_id = ?", (version_id,))


def insert_section(
    conn: sqlite3.Connection,
    *,
    version_id: int,
    parent_id: int | None,
    level: str,
    identifier: str | None,
    num: str | None,
    heading: str | None,
    content: str | None,
    xml_fragment: str | None,
    ordering: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO sections(
            version_id, parent_id, level, identifier, num,
            heading, content, xml_fragment, ordering
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            parent_id,
            level,
            identifier,
            num,
            heading,
            content,
            xml_fragment,
            ordering,
        ),
    )
    return int(cur.lastrowid)


def log_event(
    conn: sqlite3.Connection,
    *,
    step: str,
    status: str,
    message: str | None = None,
    code_id: str | None = None,
    version_id: int | None = None,
    details: dict | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO processing_log(
            code_id, version_id, step, status, message, details, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            code_id,
            version_id,
            step,
            status,
            message,
            json.dumps(details) if details else None,
            utcnow(),
        ),
    )


def list_codes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM codes ORDER BY id"))


def list_versions(conn: sqlite3.Connection, code_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM document_versions WHERE code_id = ? ORDER BY version_label",
            (code_id,),
        )
    )


def list_sections(conn: sqlite3.Connection, version_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM sections WHERE version_id = ? ORDER BY ordering",
            (version_id,),
        )
    )


def get_version(conn: sqlite3.Connection, version_id: int) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM document_versions WHERE id = ?", (version_id,))
    return cur.fetchone()


def find_version_by_label(
    conn: sqlite3.Connection, code_id: str, version_label: str
) -> sqlite3.Row | None:
    cur = conn.execute(
        "SELECT * FROM document_versions WHERE code_id = ? AND version_label = ?",
        (code_id, version_label),
    )
    return cur.fetchone()


def latest_version(conn: sqlite3.Connection, code_id: str) -> sqlite3.Row | None:
    """Return the most recent imported version of *code_id* (lexicographic)."""
    cur = conn.execute(
        """
        SELECT * FROM document_versions
        WHERE code_id = ? AND status = 'imported'
        ORDER BY version_label DESC
        LIMIT 1
        """,
        (code_id,),
    )
    return cur.fetchone()


def find_section_by_identifier(
    conn: sqlite3.Connection, version_id: int, identifier: str
) -> sqlite3.Row | None:
    cur = conn.execute(
        "SELECT * FROM sections WHERE version_id = ? AND identifier = ?",
        (version_id, identifier),
    )
    return cur.fetchone()


def get_section_lines(conn: sqlite3.Connection, section_id: int) -> list[sqlite3.Row]:
    """Return blame rows for *section_id*, joined with origin version labels."""
    return list(
        conn.execute(
            """
            SELECT
                sl.line_no,
                sl.text,
                sl.origin_version_id,
                sl.origin_section_id,
                v.version_label AS origin_version_label,
                v.code_id       AS origin_code_id,
                v.effective_date AS origin_effective_date
            FROM section_lines sl
            JOIN document_versions v ON v.id = sl.origin_version_id
            WHERE sl.section_id = ?
            ORDER BY sl.line_no
            """,
            (section_id,),
        )
    )


def section_history(
    conn: sqlite3.Connection, code_id: str, identifier: str
) -> list[sqlite3.Row]:
    """Every revision in *code_id* that touched a section with *identifier*."""
    return list(
        conn.execute(
            """
            SELECT
                sc.id            AS change_id,
                sc.change_type,
                sc.level,
                sc.num,
                sc.heading,
                sc.text_diff,
                sc.from_section_id,
                sc.to_section_id,
                r.id             AS revision_id,
                r.from_version_id,
                r.to_version_id,
                vf.version_label AS from_label,
                vt.version_label AS to_label,
                vt.effective_date AS to_effective_date
            FROM section_changes sc
            JOIN revisions r ON r.id = sc.revision_id
            LEFT JOIN document_versions vf ON vf.id = r.from_version_id
            JOIN document_versions vt ON vt.id = r.to_version_id
            WHERE r.code_id = ? AND sc.identifier = ?
            ORDER BY vt.version_label, sc.id
            """,
            (code_id, identifier),
        )
    )


def diff_versions(
    conn: sqlite3.Connection,
    *,
    code_id: str,
    from_version_id: int,
    to_version_id: int,
    identifier: str | None = None,
):
    """Compute a diff between any two versions of *code_id*, on the fly.

    Unlike persisted ``revisions``, this does not write to the database — it
    runs the same comparison core (``compare_section_sets``) and returns a
    fresh list of :class:`revisions.SectionChange` objects.
    """
    # Local import to avoid a circular module-load cycle: revisions imports db.
    from .revisions import CompareStats, compare_section_sets

    from_sections = list_sections(conn, from_version_id)
    to_sections = list_sections(conn, to_version_id)
    changes, stats = compare_section_sets(from_sections, to_sections)
    if identifier:
        changes = [c for c in changes if c.identifier == identifier]
        stats = CompareStats(
            added=sum(1 for c in changes if c.change_type == "added"),
            removed=sum(1 for c in changes if c.change_type == "removed"),
            modified=sum(1 for c in changes if c.change_type == "modified"),
            unchanged=0,
        )
    return changes, stats


def _fts_phrase(query: str) -> str:
    """Wrap *query* as a single FTS5 phrase, escaping embedded double quotes."""
    return '"' + query.replace('"', '""') + '"'


def search_sections(
    conn: sqlite3.Connection,
    query: str,
    *,
    code_id: str | None = None,
    limit: int = 50,
    raw: bool = False,
) -> list[sqlite3.Row]:
    """Full-text search over sections using the FTS5 ``sections_fts`` index.

    By default the input is treated as a single phrase, so any punctuation or
    whitespace is matched literally. Pass ``raw=True`` to forward FTS5 query
    syntax (boolean ops, prefix ``*``, ``NEAR``, column filters) verbatim.
    """
    match_expr = query if raw else _fts_phrase(query)
    sql = """
        SELECT s.*, v.code_id, v.version_label, bm25(sections_fts) AS rank
        FROM sections_fts
        JOIN sections s ON s.id = sections_fts.rowid
        JOIN document_versions v ON v.id = s.version_id
        WHERE sections_fts MATCH ?
    """
    params: list[Any] = [match_expr]
    if code_id:
        sql += " AND v.code_id = ?"
        params.append(code_id)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    return list(conn.execute(sql, params))


def list_processing_log(
    conn: sqlite3.Connection,
    *,
    code_id: str | None = None,
    version_id: int | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM processing_log WHERE 1=1"
    params: list[Any] = []
    if code_id:
        sql += " AND code_id = ?"
        params.append(code_id)
    if version_id is not None:
        sql += " AND version_id = ?"
        params.append(version_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return list(conn.execute(sql, params))
