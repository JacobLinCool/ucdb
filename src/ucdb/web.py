"""Minimal read-only web browser for a collected UCDB SQLite database."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import webbrowser
from collections import OrderedDict
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, quote, unquote, urlparse

JsonDict = dict[str, Any]

_CACHE_MAX_SIZE = 128
_CACHE_TTL_SECONDS = 30.0


class _ResponseCache:
    """Thread-safe LRU cache with TTL for web API responses."""

    def __init__(self, max_size: int, ttl: float) -> None:
        self.max_size = max_size
        self.ttl = ttl
        self._data: OrderedDict[str, tuple[float, int, bytes, str]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> tuple[int, bytes, str] | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires_at, status, body, content_type = entry
            if expires_at < time.monotonic():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return status, body, content_type

    def put(self, key: str, status: int, body: bytes, content_type: str) -> None:
        with self._lock:
            self._data[key] = (
                time.monotonic() + self.ttl,
                status,
                body,
                content_type,
            )
            self._data.move_to_end(key)
            while len(self._data) > self.max_size:
                self._data.popitem(last=False)


class BrowserStore:
    """Read-only queries used by the web UI."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        if not self.db_path.is_file():
            raise FileNotFoundError(f"database not found: {self.db_path}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        uri = "file:" + quote(str(self.db_path)) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        try:
            yield conn
        finally:
            conn.close()

    def summary(self) -> JsonDict:
        with self.connect() as conn:
            return {
                "db_path": str(self.db_path),
                "schema_version": _scalar(
                    conn,
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'",
                ),
                "codes": _count(conn, "codes"),
                "versions": _count(conn, "document_versions"),
                "sections": _count(conn, "sections"),
                "revisions": _count(conn, "revisions"),
                "changes": _count(conn, "section_changes"),
            }

    def codes(self) -> list[JsonDict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    c.*,
                    COUNT(DISTINCT v.id) AS version_count,
                    COUNT(s.id) AS section_count
                FROM codes c
                LEFT JOIN document_versions v ON v.code_id = c.id
                LEFT JOIN sections s ON s.version_id = v.id
                GROUP BY c.id
                ORDER BY c.id
                """
            )
            return [_row(row) for row in rows]

    def versions(self, code_id: str) -> list[JsonDict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    v.*,
                    parent.version_label AS parent_version_label,
                    COUNT(s.id) AS section_count
                FROM document_versions v
                LEFT JOIN document_versions parent ON parent.id = v.parent_version_id
                LEFT JOIN sections s ON s.version_id = v.id
                WHERE v.code_id = ?
                GROUP BY v.id
                ORDER BY v.version_label
                """,
                (code_id,),
            )
            return [_row(row) for row in rows]

    def sections(self, version_id: int, *, limit: int = 500) -> list[JsonDict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    id, version_id, parent_id, level, identifier, num, heading,
                    ordering,
                    substr(COALESCE(content, ''), 1, 280) AS preview
                FROM sections
                WHERE version_id = ?
                ORDER BY ordering
                LIMIT ?
                """,
                (version_id, _clamp(limit, 1, 2000)),
            )
            return [_row(row) for row in rows]

    def document(self, version_id: int, *, limit: int = 5000) -> list[JsonDict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    id, version_id, parent_id, level, identifier, num, heading,
                    ordering, content
                FROM sections
                WHERE version_id = ?
                ORDER BY ordering
                LIMIT ?
                """,
                (version_id, _clamp(limit, 1, 20000)),
            )
            return [_row(row) for row in rows]

    def section(self, section_id: int) -> JsonDict | None:
        with self.connect() as conn:
            section = conn.execute(
                """
                SELECT
                    s.*,
                    v.code_id,
                    v.version_label,
                    v.effective_date,
                    v.source_path,
                    v.source_hash,
                    v.xml_hash,
                    v.ai_provider,
                    v.ai_model,
                    v.validation_status,
                    v.validation_message
                FROM sections s
                JOIN document_versions v ON v.id = s.version_id
                WHERE s.id = ?
                """,
                (section_id,),
            ).fetchone()
            if section is None:
                return None

            lines = conn.execute(
                """
                SELECT
                    sl.line_no,
                    sl.text,
                    sl.origin_version_id,
                    v.version_label AS origin_version_label
                FROM section_lines sl
                JOIN document_versions v ON v.id = sl.origin_version_id
                WHERE sl.section_id = ?
                ORDER BY sl.line_no
                """,
                (section_id,),
            )
            history: list[JsonDict] = []
            if section["identifier"]:
                history_rows = conn.execute(
                    """
                    SELECT
                        sc.id AS change_id,
                        sc.change_type,
                        sc.level,
                        sc.num,
                        sc.heading,
                        r.id AS revision_id,
                        vf.version_label AS from_label,
                        vt.version_label AS to_label
                    FROM section_changes sc
                    JOIN revisions r ON r.id = sc.revision_id
                    LEFT JOIN document_versions vf ON vf.id = r.from_version_id
                    JOIN document_versions vt ON vt.id = r.to_version_id
                    WHERE r.code_id = ? AND sc.identifier = ?
                    ORDER BY vt.version_label, sc.id
                    """,
                    (section["code_id"], section["identifier"]),
                )
                history = [_row(row) for row in history_rows]

            data = _row(section)
            data["lines"] = [_row(row) for row in lines]
            data["history"] = history
            return data

    def search(
        self,
        query: str,
        *,
        code_id: str | None = None,
        version_id: int | None = None,
        limit: int = 50,
    ) -> list[JsonDict]:
        query = query.strip()
        if not query:
            return []

        with self.connect() as conn:
            params: list[Any]
            if len(query) >= 3:
                sql = """
                    SELECT
                        s.id,
                        s.version_id,
                        s.level,
                        s.identifier,
                        s.num,
                        s.heading,
                        substr(COALESCE(s.content, ''), 1, 360) AS preview,
                        v.code_id,
                        v.version_label,
                        bm25(sections_fts) AS rank
                    FROM sections_fts
                    JOIN sections s ON s.id = sections_fts.rowid
                    JOIN document_versions v ON v.id = s.version_id
                    WHERE sections_fts MATCH ?
                """
                params = [_fts_phrase(query)]
            else:
                like = "%" + _escape_like(query) + "%"
                sql = """
                    SELECT
                        s.id,
                        s.version_id,
                        s.level,
                        s.identifier,
                        s.num,
                        s.heading,
                        substr(COALESCE(s.content, ''), 1, 360) AS preview,
                        v.code_id,
                        v.version_label,
                        0.0 AS rank
                    FROM sections s
                    JOIN document_versions v ON v.id = s.version_id
                    WHERE (
                        COALESCE(s.heading, '') LIKE ? ESCAPE '\\'
                        OR COALESCE(s.content, '') LIKE ? ESCAPE '\\'
                        OR COALESCE(s.identifier, '') LIKE ? ESCAPE '\\'
                    )
                """
                params = [like, like, like]

            if code_id:
                sql += " AND v.code_id = ?"
                params.append(code_id)
            if version_id is not None:
                sql += " AND s.version_id = ?"
                params.append(version_id)
            sql += " ORDER BY rank, v.version_label DESC, s.ordering LIMIT ?"
            params.append(_clamp(limit, 1, 200))
            return [_row(row) for row in conn.execute(sql, params)]

    def revisions(self, code_id: str) -> list[JsonDict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    r.*,
                    vf.version_label AS from_label,
                    vt.version_label AS to_label
                FROM revisions r
                LEFT JOIN document_versions vf ON vf.id = r.from_version_id
                JOIN document_versions vt ON vt.id = r.to_version_id
                WHERE r.code_id = ?
                ORDER BY vt.version_label, r.id
                """,
                (code_id,),
            )
            return [_row(row) for row in rows]

    def changes(
        self,
        revision_id: int,
        *,
        change_type: str | None = None,
        limit: int = 300,
    ) -> list[JsonDict]:
        sql = """
            SELECT
                id, revision_id, change_type, identifier, level, num, heading,
                from_section_id, to_section_id,
                substr(COALESCE(text_diff, ''), 1, 420) AS diff_preview
            FROM section_changes
            WHERE revision_id = ?
        """
        params: list[Any] = [revision_id]
        if change_type:
            sql += " AND change_type = ?"
            params.append(change_type)
        sql += " ORDER BY id LIMIT ?"
        params.append(_clamp(limit, 1, 1000))
        with self.connect() as conn:
            return [_row(row) for row in conn.execute(sql, params)]

    def change(self, change_id: int) -> JsonDict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    sc.*,
                    r.code_id,
                    vf.version_label AS from_label,
                    vt.version_label AS to_label,
                    fs.content AS from_content,
                    ts.content AS to_content
                FROM section_changes sc
                JOIN revisions r ON r.id = sc.revision_id
                LEFT JOIN document_versions vf ON vf.id = r.from_version_id
                JOIN document_versions vt ON vt.id = r.to_version_id
                LEFT JOIN sections fs ON fs.id = sc.from_section_id
                LEFT JOIN sections ts ON ts.id = sc.to_section_id
                WHERE sc.id = ?
                """,
                (change_id,),
            ).fetchone()
            return _row(row) if row else None

    def version_xml(self, version_id: int) -> JsonDict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT
                    id, code_id, version_label, effective_date, source_path,
                    source_hash, source_size, source_mime, xml_hash,
                    validation_status, validation_message, xml_content
                FROM document_versions
                WHERE id = ?
                """,
                (version_id,),
            ).fetchone()
            return _row(row) if row else None


def serve(
    db_path: Path | str,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    open_browser: bool = False,
) -> None:
    """Run the web browser until interrupted."""

    store = BrowserStore(db_path)
    handler = make_handler(store)
    server = ThreadingHTTPServer((host, port), handler)
    actual_host, actual_port = server.server_address[:2]
    url = f"http://{actual_host}:{actual_port}"
    print(f"Serving {store.db_path} at {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
    finally:
        server.server_close()


def make_handler(store: BrowserStore) -> type[BaseHTTPRequestHandler]:
    cache = _ResponseCache(max_size=_CACHE_MAX_SIZE, ttl=_CACHE_TTL_SECONDS)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ucdb-web/0.1"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if path.startswith("/api/"):
                hit = cache.get(self.path)
                if hit is not None:
                    self._send_cached(*hit)
                    return
            query = parse_qs(parsed.query)
            try:
                if path == "/":
                    self._send_html(INDEX_HTML)
                    return
                if path == "/api/summary":
                    self._send_json(store.summary())
                    return
                if path == "/api/codes":
                    self._send_json({"codes": store.codes()})
                    return
                if path == "/api/versions":
                    self._send_json(
                        {"versions": store.versions(_required(query, "code_id"))}
                    )
                    return
                if path == "/api/sections":
                    self._send_json(
                        {
                            "sections": store.sections(
                                _int_required(query, "version_id"),
                                limit=_int(query, "limit", 500),
                            )
                        }
                    )
                    return
                if path == "/api/document":
                    self._send_json(
                        {
                            "sections": store.document(
                                _int_required(query, "version_id"),
                                limit=_int(query, "limit", 5000),
                            )
                        }
                    )
                    return
                if path == "/api/section":
                    section = store.section(_int_required(query, "id"))
                    self._send_json_or_404(section, "section not found")
                    return
                if path == "/api/search":
                    self._send_json(
                        {
                            "results": store.search(
                                _required(query, "q"),
                                code_id=_optional(query, "code_id"),
                                version_id=_int_optional(query, "version_id"),
                                limit=_int(query, "limit", 50),
                            )
                        }
                    )
                    return
                if path == "/api/revisions":
                    self._send_json(
                        {"revisions": store.revisions(_required(query, "code_id"))}
                    )
                    return
                if path == "/api/changes":
                    self._send_json(
                        {
                            "changes": store.changes(
                                _int_required(query, "revision_id"),
                                change_type=_optional(query, "type"),
                                limit=_int(query, "limit", 300),
                            )
                        }
                    )
                    return
                if path == "/api/change":
                    change = store.change(_int_required(query, "id"))
                    self._send_json_or_404(change, "change not found")
                    return
                if path == "/api/xml":
                    version = store.version_xml(_int_required(query, "version_id"))
                    self._send_json_or_404(version, "version not found")
                    return
                self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except sqlite3.Error as exc:
                self._send_json(
                    {"error": f"sqlite error: {exc}"},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} - {fmt % args}")

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(
            self, payload: JsonDict, *, status: HTTPStatus = HTTPStatus.OK
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
            cacheable = status == HTTPStatus.OK and self.path.startswith("/api/")
            if cacheable:
                cache.put(self.path, int(status), body, content_type)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Cache", "MISS" if cacheable else "BYPASS")
            self.end_headers()
            self.wfile.write(body)

        def _send_cached(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Cache", "HIT")
            self.end_headers()
            self.wfile.write(body)

        def _send_json_or_404(self, payload: JsonDict | None, message: str) -> None:
            if payload is None:
                self._send_json({"error": message}, status=HTTPStatus.NOT_FOUND)
            else:
                self._send_json(payload)

    return Handler


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _scalar(conn: sqlite3.Connection, sql: str) -> Any:
    row = conn.execute(sql).fetchone()
    return row[0] if row else None


def _row(row: sqlite3.Row) -> JsonDict:
    return {key: row[key] for key in row.keys()}


def _fts_phrase(query: str) -> str:
    return '"' + query.replace('"', '""') + '"'


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clamp(value: int, low: int, high: int) -> int:
    return min(max(value, low), high)


def _required(query: dict[str, list[str]], key: str) -> str:
    value = _optional(query, key)
    if value is None or value == "":
        raise ValueError(f"missing required query parameter: {key}")
    return value


def _optional(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    value = unquote(values[0]).strip()
    return value or None


def _int_required(query: dict[str, list[str]], key: str) -> int:
    value = _required(query, key)
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc


def _int_optional(query: dict[str, list[str]], key: str) -> int | None:
    value = _optional(query, key)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc


def _int(query: dict[str, list[str]], key: str, default: int) -> int:
    value = _optional(query, key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>UCDB Browser</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #fafaf8;
      --surface: #ffffff;
      --surface-alt: #f3f3ef;
      --border: #e3e3dd;
      --border-strong: #c8c8c0;
      --text: #1a1a1a;
      --text-muted: #6b7280;
      --text-dim: #9ca3af;
      --accent: #0d7f74;
      --accent-soft: #e6f3f1;
      --accent-strong: #075a52;
      --added: #15803d;
      --added-bg: #f0fdf4;
      --added-border: #bbf7d0;
      --removed: #b91c1c;
      --removed-bg: #fef2f2;
      --removed-border: #fecaca;
      --modified: #b45309;
      --modified-bg: #fff7ed;
      --modified-border: #fed7aa;
      --diff-bg: #f8f6f1;
      --diff-fg: #1f2937;
      --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.04);
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    button, input, select { font: inherit; color: inherit; }
    button {
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--surface);
      min-height: 32px;
      padding: 5px 10px;
      cursor: pointer;
      transition: border-color 0.1s, background 0.1s, color 0.1s;
    }
    button:hover { border-color: var(--accent); }
    input, select {
      border: 1px solid var(--border);
      border-radius: 6px;
      min-height: 34px;
      padding: 6px 9px;
      background: var(--surface);
      width: 100%;
    }
    input:focus, select:focus {
      outline: 2px solid var(--accent-soft);
      outline-offset: 0;
      border-color: var(--accent);
    }

    /* Layout */
    .app {
      display: grid;
      grid-template-columns: 280px minmax(0, 1fr);
      height: 100vh;
    }
    aside {
      display: grid;
      grid-template-rows: auto minmax(0, 1fr) auto;
      border-right: 1px solid var(--border);
      background: var(--surface);
      min-width: 0;
    }
    .aside-head { padding: 14px 16px 12px; border-bottom: 1px solid var(--border); }
    .aside-head h1 {
      margin: 0;
      font-size: 15px;
      font-weight: 700;
      letter-spacing: 0.01em;
    }
    .aside-head .tag {
      display: block;
      margin-top: 3px;
      color: var(--text-muted);
      font-size: 11px;
      overflow-wrap: anywhere;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }
    .aside-body { overflow: auto; padding: 8px 8px 16px; }
    .aside-foot {
      border-top: 1px solid var(--border);
      padding: 10px 14px;
      display: flex;
      flex-wrap: wrap;
      gap: 4px 14px;
      color: var(--text-muted);
      font-size: 11px;
      background: var(--surface-alt);
    }
    .aside-foot strong { color: var(--text); font-weight: 600; }

    /* Code/version tree */
    .tree { display: grid; gap: 1px; }
    .tree-code {
      width: 100%;
      display: flex;
      align-items: center;
      gap: 6px;
      border: 0;
      background: transparent;
      padding: 7px 8px;
      border-radius: 6px;
      text-align: left;
      min-height: 32px;
      font-weight: 600;
    }
    .tree-code:hover { background: var(--surface-alt); border-color: transparent; }
    .tree-code.active { background: var(--accent-soft); color: var(--accent-strong); }
    .tree-code .chev {
      width: 12px;
      color: var(--text-dim);
      font-size: 10px;
      flex-shrink: 0;
    }
    .tree-code.active .chev { color: var(--accent); }
    .tree-code .label { flex: 1; min-width: 0; overflow-wrap: anywhere; }
    .tree-code .meta { color: var(--text-muted); font-size: 11px; font-weight: 400; flex-shrink: 0; }
    .tree-code.active .meta { color: var(--accent); }
    .tree-versions {
      display: grid;
      gap: 1px;
      padding: 4px 0 8px 22px;
      border-left: 1px dashed var(--border);
      margin-left: 13px;
    }
    .tree-version {
      width: 100%;
      border: 0;
      background: transparent;
      padding: 5px 8px;
      border-radius: 5px;
      text-align: left;
      min-height: 28px;
      display: flex;
      gap: 8px;
      align-items: baseline;
      color: var(--text-muted);
    }
    .tree-version:hover { background: var(--surface-alt); color: var(--text); border-color: transparent; }
    .tree-version.active { background: var(--accent); color: #fff; }
    .tree-version .vlabel { flex: 1; min-width: 0; overflow-wrap: anywhere; font-weight: 500; }
    .tree-version .vmeta { font-size: 11px; opacity: 0.8; flex-shrink: 0; }

    /* Main */
    main {
      display: grid;
      grid-template-columns: minmax(0, 5fr) minmax(0, 7fr);
      min-width: 0;
    }
    .pane {
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      min-width: 0;
      border-right: 1px solid var(--border);
      background: var(--bg);
    }
    .pane.detail { border-right: 0; background: var(--surface); }
    .pane-head {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 12px 16px;
    }
    .pane.detail > .pane-head { background: var(--surface); }
    .pane-body { overflow: auto; padding: 14px 16px 28px; }

    /* List pane controls */
    .seg {
      display: inline-flex;
      border: 1px solid var(--border);
      border-radius: 7px;
      overflow: hidden;
      background: var(--surface);
    }
    .seg button {
      border: 0;
      border-radius: 0;
      background: transparent;
      padding: 6px 14px;
      min-height: 30px;
      color: var(--text-muted);
      font-weight: 500;
    }
    .seg button.active { background: var(--accent); color: #fff; }
    .seg button + button { border-left: 1px solid var(--border); }
    .seg button.active + button, .seg button + button.active { border-left-color: transparent; }

    .scope {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 10px;
      align-items: center;
      color: var(--text-muted);
      font-size: 12px;
      min-height: 22px;
    }
    .chip {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      border: 1px solid var(--border);
      background: var(--surface);
      border-radius: 999px;
      padding: 2px 9px;
      font-size: 11px;
      color: var(--text);
      max-width: 100%;
      overflow-wrap: anywhere;
    }
    .chip.dismissable { padding-right: 3px; }
    .chip button {
      border: 0;
      background: transparent;
      padding: 0 4px;
      min-height: 18px;
      font-size: 13px;
      line-height: 1;
      color: var(--text-muted);
      border-radius: 50%;
    }
    .chip button:hover { background: var(--border); color: var(--text); }
    .scope .hint { color: var(--text-dim); font-size: 11px; }

    .search {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 8px;
      margin-top: 10px;
    }

    .list { display: grid; gap: 6px; }
    .row {
      width: 100%;
      display: block;
      border: 1px solid var(--border);
      background: var(--surface);
      border-radius: 7px;
      padding: 9px 11px;
      text-align: left;
      box-shadow: var(--shadow-sm);
    }
    .row:hover { border-color: var(--accent); }
    .row.active { border-color: var(--accent); background: var(--accent-soft); }
    .row-title {
      font-weight: 600;
      font-size: 13.5px;
      overflow-wrap: anywhere;
      line-height: 1.35;
    }
    .row-sub {
      margin-top: 3px;
      color: var(--text-muted);
      font-size: 11.5px;
      overflow-wrap: anywhere;
    }
    .row-preview {
      margin-top: 5px;
      color: var(--text);
      font-size: 12px;
      line-height: 1.45;
      overflow-wrap: anywhere;
    }

    /* Detail pane */
    .breadcrumb {
      display: flex;
      flex-wrap: wrap;
      gap: 4px 6px;
      align-items: center;
      color: var(--text-muted);
      font-size: 11.5px;
      margin-bottom: 6px;
    }
    .breadcrumb .seg-text { overflow-wrap: anywhere; }
    .breadcrumb .sep { color: var(--text-dim); }
    .breadcrumb .current { color: var(--text); font-weight: 500; }

    .detail-title {
      margin: 0;
      font-size: 18px;
      font-weight: 650;
      line-height: 1.3;
      overflow-wrap: anywhere;
    }
    .detail-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      border: 1px solid var(--border);
      border-radius: 4px;
      padding: 2px 7px;
      font-size: 11px;
      color: var(--text-muted);
      background: var(--surface);
      font-weight: 500;
    }
    .badge.added { color: var(--added); border-color: var(--added-border); background: var(--added-bg); }
    .badge.removed { color: var(--removed); border-color: var(--removed-border); background: var(--removed-bg); }
    .badge.modified { color: var(--modified); border-color: var(--modified-border); background: var(--modified-bg); }

    .tabs {
      display: flex;
      flex-wrap: wrap;
      gap: 0;
      border-bottom: 1px solid var(--border);
      margin: 14px -16px 0;
      padding: 0 12px;
    }
    .tab {
      border: 0;
      border-radius: 0;
      border-bottom: 2px solid transparent;
      background: transparent;
      padding: 7px 12px;
      min-height: 34px;
      color: var(--text-muted);
      font-weight: 500;
      text-transform: lowercase;
      letter-spacing: 0.02em;
    }
    .tab:hover { color: var(--text); }
    .tab.active { color: var(--accent); border-bottom-color: var(--accent); }

    .doc-text {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      line-height: 1.65;
      font-size: 13.5px;
    }
    pre {
      margin: 0;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      background: var(--diff-bg);
      color: var(--diff-fg);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 12px 14px;
      max-height: 60vh;
      overflow: auto;
      font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    .kv {
      display: grid;
      grid-template-columns: minmax(120px, 180px) minmax(0, 1fr);
      gap: 6px 12px;
      font-size: 13px;
    }
    .kv dt { margin: 0; color: var(--text-muted); font-weight: 500; }
    .kv dd { margin: 0; overflow-wrap: anywhere; }
    .kv dd:empty::after { content: "—"; color: var(--text-dim); }

    .empty {
      color: var(--text-muted);
      padding: 24px 8px;
      text-align: center;
      font-size: 13px;
    }
    .empty.tight { padding: 10px 6px; font-size: 12px; }

    .back {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      border: 0;
      background: transparent;
      padding: 0 0 4px;
      min-height: auto;
      color: var(--accent);
      font-size: 12px;
      cursor: pointer;
    }
    .back:hover { text-decoration: underline; border-color: transparent; }

    .change-added { color: var(--added); font-weight: 600; }
    .change-removed { color: var(--removed); font-weight: 600; }
    .change-modified { color: var(--modified); font-weight: 600; }

    /* Document mode */
    .toc { display: grid; gap: 1px; }
    .toc-item {
      width: 100%;
      border: 0;
      background: transparent;
      text-align: left;
      padding: 5px 8px;
      border-radius: 5px;
      min-height: 28px;
      color: var(--text-muted);
      font-size: 12px;
      overflow-wrap: anywhere;
      line-height: 1.4;
      border-left: 2px solid transparent;
    }
    .toc-item:hover { background: var(--surface-alt); color: var(--text); border-color: transparent; }
    .toc-item.active { background: var(--accent-soft); color: var(--accent-strong); border-left-color: var(--accent); }
    .toc-item.depth-0 { font-weight: 600; color: var(--text); font-size: 13px; }
    .toc-item.depth-1 { padding-left: 20px; }
    .toc-item.depth-2 { padding-left: 32px; }
    .toc-item.depth-3 { padding-left: 44px; font-size: 11.5px; }
    .toc-item.depth-4 { padding-left: 56px; font-size: 11.5px; }

    .doc-view { display: grid; gap: 16px; }
    .doc-section { padding-left: 0; scroll-margin-top: 12px; }
    .doc-section.depth-1 { padding-left: 16px; }
    .doc-section.depth-2 { padding-left: 32px; }
    .doc-section.depth-3 { padding-left: 44px; }
    .doc-section.depth-4 { padding-left: 56px; }
    .doc-heading {
      font-weight: 650;
      font-size: 14.5px;
      margin-bottom: 4px;
      line-height: 1.3;
      overflow-wrap: anywhere;
    }
    .doc-section.depth-0 .doc-heading {
      font-size: 17px;
      padding-bottom: 5px;
      border-bottom: 1px solid var(--border);
    }
    .doc-section.depth-2 .doc-heading { font-size: 13.5px; }
    .doc-section.depth-3 .doc-heading,
    .doc-section.depth-4 .doc-heading { font-size: 13px; color: var(--text-muted); font-weight: 600; }
    .doc-body {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      line-height: 1.65;
      font-size: 13.5px;
    }

    @media (max-width: 1100px) {
      main { grid-template-columns: minmax(0, 1fr); grid-template-rows: minmax(0, 1fr) minmax(0, 1fr); }
      .pane { border-right: 0; }
      .pane.list { border-bottom: 1px solid var(--border); }
    }
    @media (max-width: 720px) {
      .app { grid-template-columns: 1fr; grid-template-rows: auto minmax(0, 1fr); height: auto; min-height: 100vh; }
      aside { border-right: 0; border-bottom: 1px solid var(--border); }
      .aside-body { max-height: 38vh; }
      .tabs { margin: 14px -12px 0; padding: 0 8px; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <div class="aside-head">
        <h1>UCDB Browser</h1>
        <span class="tag" id="dbPath"></span>
      </div>
      <nav class="aside-body">
        <div id="tree" class="tree"></div>
      </nav>
      <div class="aside-foot" id="summary"></div>
    </aside>

    <main>
      <section class="pane list">
        <div class="pane-head">
          <div class="seg">
            <button id="modeSections" class="active" type="button">Sections</button>
            <button id="modeDocument" type="button">Document</button>
            <button id="modeRevisions" type="button">Revisions</button>
          </div>
          <form id="searchForm" class="search">
            <input id="searchInput" type="search" placeholder="Search heading, identifier, or text">
            <button type="submit">Search</button>
          </form>
          <div id="scope" class="scope"></div>
        </div>
        <div class="pane-body">
          <div id="list" class="list"></div>
        </div>
      </section>

      <section class="pane detail">
        <div class="pane-head" id="detailHead">
          <nav class="breadcrumb"><span class="current">Nothing selected</span></nav>
          <h2 class="detail-title">Welcome</h2>
        </div>
        <div class="pane-body" id="detailBody">
          <div class="empty">Pick a code, choose a version, then open a section, revision, or change.</div>
        </div>
      </section>
    </main>
  </div>

  <script>
    const state = {
      codes: [],
      versions: [],
      selectedCode: null,
      selectedVersion: null,
      mode: "sections",
      detailTab: "content",
      query: "",
      searchAllVersions: false,
      currentSection: null,
      currentSectionData: null,
      currentRevision: null,
      currentChange: null
    };

    const $ = (id) => document.getElementById(id);
    const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => (
      {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]
    ));
    const short = (value, max = 160) => {
      const t = String(value ?? "");
      return t.length > max ? t.slice(0, max - 1) + "…" : t;
    };
    const api = async (path, params = {}) => {
      const url = new URL(path, window.location.origin);
      for (const [k, v] of Object.entries(params)) {
        if (v !== null && v !== undefined && v !== "") url.searchParams.set(k, v);
      }
      const res = await fetch(url);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      return data;
    };

    async function boot() {
      const summary = await api("/api/summary");
      $("dbPath").textContent = summary.db_path;
      $("summary").innerHTML = `
        <span><strong>${summary.codes}</strong> codes</span>
        <span><strong>${summary.versions}</strong> versions</span>
        <span><strong>${summary.sections}</strong> sections</span>
        <span><strong>${summary.changes}</strong> changes</span>
        <span class="hint">schema ${esc(summary.schema_version || "?")}</span>`;
      const data = await api("/api/codes");
      state.codes = data.codes;
      bindStatic();
      bindDelegated();
      if (state.codes.length) {
        await selectCode(state.codes[0].id);
      } else {
        renderTree();
        renderScope();
      }
    }

    function bindStatic() {
      $("modeSections").addEventListener("click", () => setMode("sections"));
      $("modeDocument").addEventListener("click", () => setMode("document"));
      $("modeRevisions").addEventListener("click", () => setMode("revisions"));
      $("searchForm").addEventListener("submit", async (event) => {
        event.preventDefault();
        state.query = $("searchInput").value.trim();
        await loadList();
      });
    }

    function bindDelegated() {
      document.addEventListener("click", (event) => {
        const t = event.target;

        const code = t.closest("[data-code]");
        if (code) { selectCode(code.dataset.code); return; }

        const ver = t.closest("[data-version]");
        if (ver) { selectVersion(Number(ver.dataset.version)); return; }

        const sec = t.closest("[data-section]");
        if (sec) { showSection(Number(sec.dataset.section)); return; }

        const rev = t.closest("[data-revision]");
        if (rev) { showRevision(Number(rev.dataset.revision)); return; }

        const ch = t.closest("[data-change]");
        if (ch) { showChange(Number(ch.dataset.change)); return; }

        const jump = t.closest("[data-jump]");
        if (jump) {
          const target = document.getElementById("sec-" + jump.dataset.jump);
          if (target) {
            target.scrollIntoView({ behavior: "smooth", block: "start" });
            document.querySelectorAll(".toc-item.active").forEach(el => el.classList.remove("active"));
            jump.classList.add("active");
          }
          return;
        }

        const back = t.closest("[data-back]");
        if (back) {
          if (back.dataset.back === "revision" && state.currentRevision) {
            showRevision(state.currentRevision);
          }
          return;
        }

        const tab = t.closest("[data-tab]");
        if (tab) {
          state.detailTab = tab.dataset.tab;
          if (state.currentSectionData) renderSectionDetail(state.currentSectionData);
          return;
        }

        const action = t.closest("[data-action]");
        if (action) {
          const a = action.dataset.action;
          if (a === "clear-query") { state.query = ""; $("searchInput").value = ""; }
          else if (a === "scope-all") { state.searchAllVersions = true; }
          else if (a === "scope-version") { state.searchAllVersions = false; }
          loadList();
          return;
        }
      });
    }

    function setMode(mode) {
      state.mode = mode;
      $("modeSections").classList.toggle("active", mode === "sections");
      $("modeDocument").classList.toggle("active", mode === "document");
      $("modeRevisions").classList.toggle("active", mode === "revisions");
      $("searchForm").style.display = mode === "sections" ? "" : "none";
      loadList();
    }

    function renderTree() {
      const tree = $("tree");
      if (!state.codes.length) {
        tree.innerHTML = `<div class="empty tight">No codes loaded.</div>`;
        return;
      }
      tree.innerHTML = state.codes.map(code => {
        const isSelected = code.id === state.selectedCode;
        const versions = isSelected ? renderVersionTree() : "";
        return `
          <div>
            <button type="button" class="tree-code ${isSelected ? "active" : ""}" data-code="${esc(code.id)}">
              <span class="chev">${isSelected ? "\u25be" : "\u25b8"}</span>
              <span class="label">${esc(code.id)}</span>
              <span class="meta">${code.version_count}v</span>
            </button>
            ${versions}
          </div>`;
      }).join("");
    }

    function renderVersionTree() {
      if (!state.versions.length) {
        return `<div class="tree-versions"><div class="empty tight">No versions.</div></div>`;
      }
      return `<div class="tree-versions">${state.versions.map(v => `
        <button type="button" class="tree-version ${state.selectedVersion && v.id === state.selectedVersion.id ? "active" : ""}" data-version="${v.id}">
          <span class="vlabel">${esc(v.version_label)}</span>
          <span class="vmeta">${v.section_count}</span>
        </button>`).join("")}</div>`;
    }

    async function selectCode(codeId) {
      state.selectedCode = codeId;
      state.versions = [];
      state.selectedVersion = null;
      state.query = "";
      state.searchAllVersions = false;
      $("searchInput").value = "";
      const data = await api("/api/versions", { code_id: codeId });
      state.versions = data.versions;
      state.selectedVersion = state.versions.at(-1) || null;
      renderTree();
      await loadList();
    }

    async function selectVersion(versionId) {
      state.selectedVersion = state.versions.find(v => v.id === versionId) || null;
      state.query = "";
      state.searchAllVersions = false;
      $("searchInput").value = "";
      if (state.mode !== "sections") setMode("sections");
      else { renderTree(); await loadList(); }
    }

    async function loadList() {
      renderScope();
      renderTree();
      if (state.mode === "sections") {
        if (state.query) {
          const params = { q: state.query, code_id: state.selectedCode };
          if (!state.searchAllVersions && state.selectedVersion) params.version_id = state.selectedVersion.id;
          const data = await api("/api/search", params);
          renderSectionList(data.results, true);
        } else if (state.selectedVersion) {
          const data = await api("/api/sections", { version_id: state.selectedVersion.id });
          renderSectionList(data.sections, false);
        } else {
          $("list").innerHTML = `<div class="empty">Select a version to view its sections.</div>`;
        }
      } else if (state.mode === "document") {
        if (!state.selectedVersion) {
          $("list").innerHTML = `<div class="empty">Select a version to view its document.</div>`;
          return;
        }
        const data = await api("/api/document", { version_id: state.selectedVersion.id });
        renderDocument(data.sections);
      } else {
        if (!state.selectedCode) {
          $("list").innerHTML = `<div class="empty">Select a code to view revisions.</div>`;
          return;
        }
        const data = await api("/api/revisions", { code_id: state.selectedCode });
        renderRevisionList(data.revisions);
      }
    }

    function renderScope() {
      const scope = $("scope");
      const chips = [];
      if (state.mode === "sections") {
        if (state.query) {
          chips.push(`<span class="chip dismissable">"${esc(short(state.query, 40))}" <button type="button" data-action="clear-query" aria-label="Clear search">×</button></span>`);
          if (state.selectedCode) chips.push(`<span class="chip">code: ${esc(state.selectedCode)}</span>`);
          if (state.searchAllVersions) {
            chips.push(`<span class="chip dismissable">all versions <button type="button" data-action="scope-version" aria-label="Limit to current version">×</button></span>`);
          } else if (state.selectedVersion) {
            chips.push(`<span class="chip dismissable">version: ${esc(state.selectedVersion.version_label)} <button type="button" data-action="scope-all" aria-label="Search all versions">×</button></span>`);
          }
        } else if (state.selectedVersion) {
          chips.push(`<span class="chip">${esc(state.selectedCode)} / ${esc(state.selectedVersion.version_label)}</span>`);
          chips.push(`<span class="hint">${state.selectedVersion.section_count} sections</span>`);
        } else if (state.selectedCode) {
          chips.push(`<span class="chip">${esc(state.selectedCode)}</span>`);
          chips.push(`<span class="hint">no version selected</span>`);
        } else {
          chips.push(`<span class="hint">Select a code from the sidebar</span>`);
        }
      } else if (state.mode === "document") {
        if (state.selectedVersion) {
          chips.push(`<span class="chip">${esc(state.selectedCode)} / ${esc(state.selectedVersion.version_label)}</span>`);
          chips.push(`<span class="hint">${state.selectedVersion.section_count} sections</span>`);
        } else if (state.selectedCode) {
          chips.push(`<span class="chip">${esc(state.selectedCode)}</span>`);
          chips.push(`<span class="hint">no version selected</span>`);
        } else {
          chips.push(`<span class="hint">Select a code from the sidebar</span>`);
        }
      } else if (state.selectedCode) {
        chips.push(`<span class="chip">code: ${esc(state.selectedCode)}</span>`);
      } else {
        chips.push(`<span class="hint">Select a code from the sidebar</span>`);
      }
      scope.innerHTML = chips.join("");
    }

    function renderSectionList(rows, isSearch) {
      if (!rows.length) {
        $("list").innerHTML = `<div class="empty">${isSearch ? "No matches." : "No sections."}</div>`;
        return;
      }
      $("list").innerHTML = rows.map(row => {
        const head = (row.num || row.identifier || row.level || "").trim();
        const heading = (row.heading || "").trim();
        const title = head && heading ? `${esc(head)} ${esc(heading)}` : esc(head || heading || "(untitled)");
        const versionPart = row.version_label ? ` / ${esc(row.version_label)}` : "";
        const codePart = row.code_id || state.selectedCode || "";
        return `
          <button type="button" class="row ${state.currentSection === row.id ? "active" : ""}" data-section="${row.id}">
            <div class="row-title">${title}</div>
            <div class="row-sub">${esc(codePart)}${versionPart} · ${esc(row.level || "")} · ${esc(row.identifier || "—")}</div>
            ${row.preview ? `<div class="row-preview">${esc(short(row.preview, 200))}</div>` : ""}
          </button>`;
      }).join("");
    }

    function renderRevisionList(rows) {
      if (!rows.length) {
        $("list").innerHTML = `<div class="empty">No revisions for this code.</div>`;
        return;
      }
      $("list").innerHTML = rows.map(row => `
        <button type="button" class="row ${state.currentRevision === row.id ? "active" : ""}" data-revision="${row.id}">
          <div class="row-title">${esc(row.from_label || "(initial)")} → ${esc(row.to_label)}</div>
          <div class="row-sub">
            <span class="change-added">+${row.sections_added || 0}</span> ·
            <span class="change-removed">−${row.sections_removed || 0}</span> ·
            <span class="change-modified">~${row.sections_modified || 0}</span> ·
            <span style="color:var(--text-dim)">=${row.sections_unchanged || 0}</span>
          </div>
        </button>`).join("");
    }

    /* Document */

    function computeDepths(sections) {
      const byId = new Map();
      sections.forEach(s => byId.set(s.id, s));
      const depths = new Map();
      function depthOf(id) {
        if (depths.has(id)) return depths.get(id);
        const node = byId.get(id);
        if (!node || node.parent_id == null || !byId.has(node.parent_id)) {
          depths.set(id, 0);
          return 0;
        }
        const d = depthOf(node.parent_id) + 1;
        depths.set(id, d);
        return d;
      }
      sections.forEach(s => depthOf(s.id));
      return depths;
    }

    function renderDocument(sections) {
      if (!sections.length) {
        $("list").innerHTML = `<div class="empty">No sections.</div>`;
        $("detailHead").innerHTML = `
          <nav class="breadcrumb">
            <span class="seg-text">${esc(state.selectedCode || "")}</span>
            <span class="sep">›</span>
            <span class="current seg-text">${esc(state.selectedVersion?.version_label || "")}</span>
          </nav>
          <h2 class="detail-title">Document</h2>`;
        $("detailBody").innerHTML = `<div class="empty">No sections to display.</div>`;
        return;
      }
      const depths = computeDepths(sections);
      const cap = (n) => Math.min(n, 4);

      $("list").innerHTML = `<div class="toc">${sections.map(s => {
        const depth = cap(depths.get(s.id) || 0);
        const head = (s.num || s.identifier || "").trim();
        const heading = (s.heading || "").trim();
        const label = head && heading ? `${esc(head)} ${esc(heading)}` : esc(head || heading || "(untitled)");
        return `<button type="button" class="toc-item depth-${depth}" data-jump="${s.id}">${label}</button>`;
      }).join("")}</div>`;

      state.currentSection = null;
      state.currentRevision = null;
      state.currentChange = null;

      $("detailHead").innerHTML = `
        <nav class="breadcrumb">
          <span class="seg-text">${esc(state.selectedCode)}</span>
          <span class="sep">›</span>
          <span class="current seg-text">${esc(state.selectedVersion.version_label)}</span>
        </nav>
        <h2 class="detail-title">Document</h2>
        <div class="detail-meta">
          <span class="badge">${sections.length} sections</span>
        </div>`;

      $("detailBody").innerHTML = `<article class="doc-view">${sections.map(s => {
        const depth = cap(depths.get(s.id) || 0);
        const head = (s.num || s.identifier || "").trim();
        const heading = (s.heading || "").trim();
        const label = head && heading ? `${esc(head)} ${esc(heading)}` : esc(head || heading || "");
        return `<section class="doc-section depth-${depth}" id="sec-${s.id}">
          ${label ? `<div class="doc-heading">${label}</div>` : ""}
          ${s.content ? `<div class="doc-body">${esc(s.content)}</div>` : ""}
        </section>`;
      }).join("")}</article>`;
    }

    /* Detail */

    async function showSection(id) {
      const row = await api("/api/section", { id });
      state.currentSection = id;
      state.currentSectionData = row;
      state.currentRevision = null;
      state.currentChange = null;
      state.detailTab = "content";
      document.querySelectorAll(".row[data-section]").forEach(el => {
        el.classList.toggle("active", Number(el.dataset.section) === id);
      });
      renderSectionDetail(row);
    }

    function renderSectionDetail(row) {
      const tabs = ["content", "xml", "metadata", "blame", "history"];
      const head = (row.num || row.identifier || row.level || "").trim();
      const heading = (row.heading || "").trim();
      const title = (head && heading ? `${esc(head)} ${esc(heading)}` : esc(head || heading || "(untitled)")) || "(untitled)";
      $("detailHead").innerHTML = `
        <nav class="breadcrumb">
          <span class="seg-text">${esc(row.code_id)}</span>
          <span class="sep">›</span>
          <span class="seg-text">${esc(row.version_label)}</span>
          <span class="sep">›</span>
          <span class="current seg-text">${esc(row.identifier || row.num || ("section " + row.id))}</span>
        </nav>
        <h2 class="detail-title">${title}</h2>
        <div class="detail-meta">
          <span class="badge">${esc(row.level || "section")}</span>
          ${row.identifier ? `<span class="badge">${esc(row.identifier)}</span>` : ""}
          ${row.validation_status ? `<span class="badge">${esc(row.validation_status)}</span>` : ""}
        </div>
        <div class="tabs">
          ${tabs.map(tab => `<button type="button" class="tab ${state.detailTab === tab ? "active" : ""}" data-tab="${tab}">${tab}</button>`).join("")}
        </div>`;
      const bodyByTab = {
        content: row.content ? `<div class="doc-text">${esc(row.content)}</div>` : `<div class="empty">No content stored.</div>`,
        xml: row.xml_fragment ? `<pre>${esc(row.xml_fragment)}</pre>` : `<div class="empty">No XML fragment stored.</div>`,
        metadata: renderMetadata(row),
        blame: renderBlame(row.lines || []),
        history: renderHistory(row.history || [])
      };
      $("detailBody").innerHTML = bodyByTab[state.detailTab];
    }

    function renderMetadata(row) {
      const keys = [
        "id", "version_id", "parent_id", "ordering", "code_id", "version_label",
        "effective_date", "source_path", "source_hash", "xml_hash", "ai_provider",
        "ai_model", "validation_status", "validation_message"
      ];
      return `<dl class="kv">${keys.map(k => `<dt>${esc(k)}</dt><dd>${esc(row[k] ?? "")}</dd>`).join("")}</dl>`;
    }

    function renderBlame(lines) {
      if (!lines.length) return `<div class="empty">No line provenance for this section.</div>`;
      return `<dl class="kv">${lines.map(line => `
        <dt>${line.line_no} · ${esc(line.origin_version_label)}</dt>
        <dd>${esc(line.text)}</dd>`).join("")}</dl>`;
    }

    function renderHistory(rows) {
      if (!rows.length) return `<div class="empty">No recorded changes for this identifier.</div>`;
      return `<div class="list">${rows.map(row => `
        <button type="button" class="row" data-change="${row.change_id}">
          <div class="row-title"><span class="change-${esc(row.change_type)}">${esc(row.change_type)}</span> · ${esc(row.from_label || "(initial)")} → ${esc(row.to_label)}</div>
          <div class="row-sub">revision ${row.revision_id} · change ${row.change_id}${row.heading ? " · " + esc(row.heading) : ""}</div>
        </button>`).join("")}</div>`;
    }

    /* Revision */

    async function showRevision(id) {
      state.currentRevision = id;
      state.currentChange = null;
      const [changesData, revisionsData] = await Promise.all([
        api("/api/changes", { revision_id: id }),
        api("/api/revisions", { code_id: state.selectedCode })
      ]);
      const meta = revisionsData.revisions.find(r => r.id === id);
      if (state.mode !== "revisions") setMode("revisions");
      document.querySelectorAll(".row[data-revision]").forEach(el => {
        el.classList.toggle("active", Number(el.dataset.revision) === id);
      });
      renderRevisionDetail(id, meta, changesData.changes);
    }

    function renderRevisionDetail(id, meta, changes) {
      const label = meta ? `${esc(meta.from_label || "(initial)")} → ${esc(meta.to_label)}` : `revision ${id}`;
      $("detailHead").innerHTML = `
        <nav class="breadcrumb">
          <span class="seg-text">${esc(state.selectedCode)}</span>
          <span class="sep">›</span>
          <span class="current seg-text">revision ${id}</span>
        </nav>
        <h2 class="detail-title">${label}</h2>
        <div class="detail-meta">
          <span class="badge added">+${meta?.sections_added || 0} added</span>
          <span class="badge removed">−${meta?.sections_removed || 0} removed</span>
          <span class="badge modified">~${meta?.sections_modified || 0} modified</span>
          <span class="badge">=${meta?.sections_unchanged || 0} unchanged</span>
        </div>`;
      if (!changes.length) {
        $("detailBody").innerHTML = `<div class="empty">No recorded changes.</div>`;
        return;
      }
      $("detailBody").innerHTML = `<div class="list">${changes.map(c => {
        const head = (c.num || c.identifier || "").trim();
        const heading = (c.heading || "").trim();
        const title = head && heading ? `${esc(head)} ${esc(heading)}` : esc(head || heading || "(untitled)");
        return `
          <button type="button" class="row" data-change="${c.id}">
            <div class="row-title"><span class="change-${esc(c.change_type)}">${esc(c.change_type)}</span> · ${title}</div>
            <div class="row-sub">${esc(c.level || "")} · ${esc(c.identifier || "—")}</div>
            ${c.diff_preview ? `<div class="row-preview">${esc(short(c.diff_preview, 240))}</div>` : ""}
          </button>`;
      }).join("")}</div>`;
    }

    /* Change */

    async function showChange(id) {
      const row = await api("/api/change", { id });
      state.currentChange = id;
      if (!state.currentRevision) state.currentRevision = row.revision_id;
      const head = (row.num || row.identifier || "").trim();
      const heading = (row.heading || "").trim();
      const title = head && heading ? `${esc(head)} ${esc(heading)}` : esc(head || heading || "(untitled change)");
      $("detailHead").innerHTML = `
        ${state.currentRevision ? `<button type="button" class="back" data-back="revision">← Back to revision</button>` : ""}
        <nav class="breadcrumb">
          <span class="seg-text">${esc(row.code_id)}</span>
          <span class="sep">›</span>
          <span class="seg-text">${esc(row.from_label || "(initial)")} → ${esc(row.to_label)}</span>
          <span class="sep">›</span>
          <span class="current seg-text">change ${row.id}</span>
        </nav>
        <h2 class="detail-title">${title}</h2>
        <div class="detail-meta">
          <span class="badge ${esc(row.change_type)}">${esc(row.change_type)}</span>
          ${row.identifier ? `<span class="badge">${esc(row.identifier)}</span>` : ""}
          ${row.level ? `<span class="badge">${esc(row.level)}</span>` : ""}
        </div>`;
      $("detailBody").innerHTML = row.text_diff
        ? `<pre>${esc(row.text_diff)}</pre>`
        : `<div class="empty">No text diff stored.</div>`;
    }

    boot().catch(err => {
      $("detailBody").innerHTML = `<div class="empty">${esc(err.message)}</div>`;
      console.error(err);
    });
  </script>
</body>
</html>
"""
