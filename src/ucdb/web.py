"""Minimal read-only web browser for a UCDB SQLite database."""

from __future__ import annotations

import json
import sqlite3
import webbrowser
from contextlib import contextmanager
from difflib import SequenceMatcher
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, quote, unquote, urlparse

from .revisions import text_diff

JsonDict = dict[str, Any]


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
                    conn, "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ),
                "works": _count(conn, "works"),
                "expressions": _count(conn, "expressions"),
                "nodes": _count(conn, "nodes"),
                "revisions": _count(conn, "revisions"),
                "changes": _count(conn, "node_changes"),
            }

    def works(self) -> list[JsonDict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    w.*,
                    COUNT(DISTINCT e.id) AS expression_count,
                    COUNT(n.id) AS node_count
                FROM works w
                LEFT JOIN expressions e ON e.work_id = w.id
                LEFT JOIN nodes n ON n.expression_id = e.id
                GROUP BY w.id
                ORDER BY w.id
                """
            )
            return [_row(row) for row in rows]

    def expressions(self, work_id: str) -> list[JsonDict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    e.*,
                    parent.version_label AS parent_version_label,
                    COUNT(n.id) AS node_count
                FROM expressions e
                LEFT JOIN expressions parent ON parent.id = e.parent_expression_id
                LEFT JOIN nodes n ON n.expression_id = e.id
                WHERE e.work_id = ?
                GROUP BY e.id
                ORDER BY e.version_label, e.language
                """,
                (work_id,),
            )
            return [_row(row) for row in rows]

    def nodes(self, expression_id: int, *, limit: int = 500) -> list[JsonDict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, expression_id, parent_id, node_type, node_eid, num,
                       heading, ordering, substr(COALESCE(text, ''), 1, 280) AS preview
                FROM nodes
                WHERE expression_id = ?
                ORDER BY ordering
                LIMIT ?
                """,
                (expression_id, _clamp(limit, 1, 2000)),
            )
            return [_row(row) for row in rows]

    def document(self, expression_id: int, *, limit: int = 5000) -> list[JsonDict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, expression_id, parent_id, node_type, node_eid, num,
                       heading, ordering, depth, text
                FROM nodes
                WHERE expression_id = ?
                ORDER BY ordering
                LIMIT ?
                """,
                (expression_id, _clamp(limit, 1, 20000)),
            )
            return [_row(row) for row in rows]

    def document_diff(
        self,
        from_expression_id: int,
        to_expression_id: int,
        *,
        limit: int = 10000,
    ) -> JsonDict | None:
        limit = _clamp(limit, 1, 20000)
        with self.connect() as conn:
            from_expression = _expression_summary(conn, from_expression_id)
            to_expression = _expression_summary(conn, to_expression_id)
            if from_expression is None or to_expression is None:
                return None
            if from_expression["work_id"] != to_expression["work_id"]:
                raise ValueError(
                    "from_expression_id and to_expression_id must share a work"
                )
            from_nodes = _document_nodes(conn, from_expression_id, limit)
            to_nodes = _document_nodes(conn, to_expression_id, limit)
            rows, stats = _document_diff_rows(from_nodes, to_nodes)
            return {
                "from_expression": from_expression,
                "to_expression": to_expression,
                "stats": stats,
                "rows": rows,
            }

    def node(self, node_id: int) -> JsonDict | None:
        with self.connect() as conn:
            node = conn.execute(
                """
                SELECT n.*, e.work_id, e.version_label, e.language, e.effective_date,
                       e.source_path, e.source_hash, e.canonical_hash,
                       e.ai_provider, e.ai_model, e.validation_status,
                       e.validation_message
                FROM nodes n
                JOIN expressions e ON e.id = n.expression_id
                WHERE n.id = ?
                """,
                (node_id,),
            ).fetchone()
            if node is None:
                return None
            lines = conn.execute(
                """
                SELECT nl.line_no, nl.text, nl.origin_expression_id,
                       e.version_label AS origin_version_label
                FROM node_lines nl
                JOIN expressions e ON e.id = nl.origin_expression_id
                WHERE nl.node_id = ?
                ORDER BY nl.line_no
                """,
                (node_id,),
            )
            history: list[JsonDict] = []
            if node["node_eid"]:
                history_rows = conn.execute(
                    """
                    SELECT nc.id AS change_id, nc.change_type, nc.node_type,
                           nc.num, nc.heading, r.id AS revision_id,
                           fe.version_label AS from_label,
                           te.version_label AS to_label
                    FROM node_changes nc
                    JOIN revisions r ON r.id = nc.revision_id
                    LEFT JOIN expressions fe ON fe.id = r.from_expression_id
                    JOIN expressions te ON te.id = r.to_expression_id
                    WHERE r.work_id = ? AND nc.node_eid = ?
                    ORDER BY te.version_label, nc.id
                    """,
                    (node["work_id"], node["node_eid"]),
                )
                history = [_row(row) for row in history_rows]
            data = _row(node)
            data["lines"] = [_row(row) for row in lines]
            data["history"] = history
            return data

    def search(
        self,
        query: str,
        *,
        scope: str = "all",
        work_id: str | None = None,
        expression_id: int | None = None,
        limit: int = 50,
    ) -> list[JsonDict]:
        query = query.strip()
        if not query:
            return []
        scope = scope if scope in {"all", "text", "heading", "num", "eid"} else "all"
        with self.connect() as conn:
            params: list[Any]
            if scope != "all":
                like = "%" + _escape_like(query) + "%"
                field = {
                    "text": "COALESCE(n.text, '')",
                    "heading": "COALESCE(n.heading, '')",
                    "num": "COALESCE(n.num, '')",
                    "eid": "COALESCE(n.node_eid, '')",
                }[scope]
                sql = f"""
                    SELECT n.id, n.expression_id, n.node_type, n.node_eid, n.num,
                           n.heading, substr(COALESCE(n.text, ''), 1, 360) AS preview,
                           e.work_id, e.version_label, 0.0 AS rank
                    FROM nodes n
                    JOIN expressions e ON e.id = n.expression_id
                    WHERE {field} LIKE ? ESCAPE '\\'
                """
                params = [like]
            elif len(query) >= 3:
                sql = """
                    SELECT n.id, n.expression_id, n.node_type, n.node_eid, n.num,
                           n.heading, substr(COALESCE(n.text, ''), 1, 360) AS preview,
                           e.work_id, e.version_label, bm25(nodes_fts) AS rank
                    FROM nodes_fts
                    JOIN nodes n ON n.id = nodes_fts.rowid
                    JOIN expressions e ON e.id = n.expression_id
                    WHERE nodes_fts MATCH ?
                """
                params = [_fts_phrase(query)]
            else:
                like = "%" + _escape_like(query) + "%"
                sql = """
                    SELECT n.id, n.expression_id, n.node_type, n.node_eid, n.num,
                           n.heading, substr(COALESCE(n.text, ''), 1, 360) AS preview,
                           e.work_id, e.version_label, 0.0 AS rank
                    FROM nodes n
                    JOIN expressions e ON e.id = n.expression_id
                    WHERE (
                        COALESCE(n.heading, '') LIKE ? ESCAPE '\\'
                        OR COALESCE(n.text, '') LIKE ? ESCAPE '\\'
                        OR COALESCE(n.node_eid, '') LIKE ? ESCAPE '\\'
                        OR COALESCE(n.num, '') LIKE ? ESCAPE '\\'
                    )
                """
                params = [like, like, like, like]
            if work_id:
                sql += " AND e.work_id = ?"
                params.append(work_id)
            if expression_id is not None:
                sql += " AND n.expression_id = ?"
                params.append(expression_id)
            sql += " ORDER BY rank, e.version_label DESC, n.ordering LIMIT ?"
            params.append(_clamp(limit, 1, 200))
            return [_row(row) for row in conn.execute(sql, params)]

    def revisions(self, work_id: str) -> list[JsonDict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT r.*, fe.version_label AS from_label, te.version_label AS to_label
                FROM revisions r
                LEFT JOIN expressions fe ON fe.id = r.from_expression_id
                JOIN expressions te ON te.id = r.to_expression_id
                WHERE r.work_id = ?
                ORDER BY te.version_label, r.id
                """,
                (work_id,),
            )
            return [_row(row) for row in rows]

    def changes(
        self, revision_id: int, *, change_type: str | None = None, limit: int = 300
    ) -> list[JsonDict]:
        sql = """
            SELECT id, revision_id, change_type, node_eid, node_type, num, heading,
                   from_node_id, to_node_id,
                   substr(COALESCE(text_diff, ''), 1, 420) AS diff_preview
            FROM node_changes
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
                SELECT nc.*, r.work_id, fe.version_label AS from_label,
                       te.version_label AS to_label,
                       fn.text AS from_text, tn.text AS to_text
                FROM node_changes nc
                JOIN revisions r ON r.id = nc.revision_id
                LEFT JOIN expressions fe ON fe.id = r.from_expression_id
                JOIN expressions te ON te.id = r.to_expression_id
                LEFT JOIN nodes fn ON fn.id = nc.from_node_id
                LEFT JOIN nodes tn ON tn.id = nc.to_node_id
                WHERE nc.id = ?
                """,
                (change_id,),
            ).fetchone()
            return _row(row) if row else None

    def expression_xml(self, expression_id: int) -> JsonDict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, work_id, version_label, language, effective_date,
                       source_path, source_hash, source_size, source_mime,
                       canonical_hash, validation_status, validation_message,
                       canonical_xml
                FROM expressions
                WHERE id = ?
                """,
                (expression_id,),
            ).fetchone()
            return _row(row) if row else None


def serve(
    db_path: Path | str,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    open_browser: bool = False,
) -> None:
    store = BrowserStore(db_path)
    server = ThreadingHTTPServer((host, port), make_handler(store))
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
    class Handler(BaseHTTPRequestHandler):
        server_version = "ucdb-web/0.2"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            try:
                if parsed.path == "/":
                    self._send_html(INDEX_HTML)
                elif parsed.path == "/api/summary":
                    self._send_json(store.summary())
                elif parsed.path == "/api/works":
                    self._send_json({"works": store.works()})
                elif parsed.path == "/api/expressions":
                    self._send_json(
                        {"expressions": store.expressions(_required(query, "work_id"))}
                    )
                elif parsed.path == "/api/nodes":
                    self._send_json(
                        {
                            "nodes": store.nodes(
                                _int_required(query, "expression_id"),
                                limit=_int(query, "limit", 500),
                            )
                        }
                    )
                elif parsed.path == "/api/document":
                    self._send_json(
                        {
                            "document": store.document(
                                _int_required(query, "expression_id"),
                                limit=_int(query, "limit", 5000),
                            )
                        }
                    )
                elif parsed.path == "/api/document-diff":
                    self._send_json_or_404(
                        store.document_diff(
                            _int_required(query, "from_expression_id"),
                            _int_required(query, "to_expression_id"),
                            limit=_int(query, "limit", 10000),
                        ),
                        "expression not found",
                    )
                elif parsed.path == "/api/node":
                    self._send_json_or_404(
                        store.node(_int_required(query, "id")), "node not found"
                    )
                elif parsed.path == "/api/search":
                    self._send_json(
                        {
                            "results": store.search(
                                _required(query, "q"),
                                scope=_optional(query, "scope") or "all",
                                work_id=_optional(query, "work_id"),
                                expression_id=_int_optional(query, "expression_id"),
                                limit=_int(query, "limit", 50),
                            )
                        }
                    )
                elif parsed.path == "/api/revisions":
                    self._send_json(
                        {"revisions": store.revisions(_required(query, "work_id"))}
                    )
                elif parsed.path == "/api/changes":
                    self._send_json(
                        {
                            "changes": store.changes(
                                _int_required(query, "revision_id"),
                                change_type=_optional(query, "type"),
                                limit=_int(query, "limit", 300),
                            )
                        }
                    )
                elif parsed.path == "/api/change":
                    self._send_json_or_404(
                        store.change(_int_required(query, "id")), "change not found"
                    )
                elif parsed.path == "/api/xml":
                    self._send_json_or_404(
                        store.expression_xml(_int_required(query, "expression_id")),
                        "expression not found",
                    )
                else:
                    self._send_json({"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except sqlite3.Error as exc:
                self._send_json(
                    {"error": f"sqlite error: {exc}"},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )

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
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
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


def _expression_summary(
    conn: sqlite3.Connection, expression_id: int
) -> JsonDict | None:
    row = conn.execute(
        """
        SELECT id, work_id, version_label, language, effective_date, source_path
        FROM expressions
        WHERE id = ?
        """,
        (expression_id,),
    ).fetchone()
    return _row(row) if row else None


def _document_nodes(
    conn: sqlite3.Connection, expression_id: int, limit: int
) -> list[JsonDict]:
    rows = conn.execute(
        """
        SELECT id, expression_id, parent_id, node_type, node_eid, num,
               heading, ordering, depth, text, normalized_text_hash
        FROM nodes
        WHERE expression_id = ?
        ORDER BY ordering
        LIMIT ?
        """,
        (expression_id, limit),
    )
    return [_row(row) for row in rows]


def _document_diff_rows(
    from_nodes: list[JsonDict], to_nodes: list[JsonDict]
) -> tuple[list[JsonDict], JsonDict]:
    rows: list[JsonDict] = []
    stats = {"added": 0, "removed": 0, "modified": 0, "unchanged": 0}
    from_eids = [str(row["node_eid"]) for row in from_nodes]
    to_eids = [str(row["node_eid"]) for row in to_nodes]
    matcher = SequenceMatcher(a=from_eids, b=to_eids, autojunk=False)

    def append_row(
        change_type: str, before: JsonDict | None, after: JsonDict | None
    ) -> None:
        stats[change_type] += 1
        snapshot = after or before or {}
        rows.append(
            {
                "change_type": change_type,
                "node_eid": snapshot.get("node_eid"),
                "node_type": snapshot.get("node_type"),
                "num": snapshot.get("num"),
                "heading": snapshot.get("heading"),
                "depth": snapshot.get("depth", 0),
                "from": _diff_node_payload(before),
                "to": _diff_node_payload(after),
                "text_diff": text_diff(
                    before.get("text") if before else None,
                    after.get("text") if after else None,
                )
                if change_type == "modified"
                else "",
            }
        )

    for tag, from_start, from_end, to_start, to_end in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(from_end - from_start):
                before = from_nodes[from_start + offset]
                after = to_nodes[to_start + offset]
                change_type = (
                    "modified" if _node_changed(before, after) else "unchanged"
                )
                append_row(change_type, before, after)
            continue
        if tag in {"delete", "replace"}:
            for before in from_nodes[from_start:from_end]:
                append_row("removed", before, None)
        if tag in {"insert", "replace"}:
            for after in to_nodes[to_start:to_end]:
                append_row("added", None, after)
    return rows, stats


def _diff_node_payload(row: JsonDict | None) -> JsonDict | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "node_type": row["node_type"],
        "node_eid": row["node_eid"],
        "num": row["num"],
        "heading": row["heading"],
        "depth": row["depth"],
        "text": row["text"],
    }


def _node_changed(before: JsonDict, after: JsonDict) -> bool:
    return any(
        (before.get(key) or "") != (after.get(key) or "")
        for key in ("node_type", "num", "heading", "normalized_text_hash")
    )


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


INDEX_HTML = """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" href="data:,">
  <title>UCDB 法規瀏覽器</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --line: #d8dde6;
      --muted: #667085;
      --text: #101828;
      --accent: #0f766e;
      --accent-soft: #e6f4f1;
      --danger: #b42318;
      --warn: #a15c07;
      --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      --sans: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
        "Segoe UI", sans-serif;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: var(--sans);
      font-size: 14px;
      line-height: 1.45;
    }

    a { color: var(--accent); }

    button, input, select {
      font: inherit;
    }

    button {
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--text);
      min-height: 34px;
      border-radius: 6px;
      padding: 6px 10px;
      cursor: pointer;
    }

    button:hover { border-color: var(--accent); }
    button.active {
      background: var(--accent-soft);
      border-color: var(--accent);
      color: #095b54;
    }

    input, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      min-height: 34px;
      padding: 6px 9px;
    }

    .app {
      display: grid;
      grid-template-columns: minmax(340px, 390px) minmax(0, 1fr);
      min-height: 100vh;
    }

    aside {
      border-right: 1px solid var(--line);
      background: #fbfcfd;
      padding: 16px;
      overflow: auto;
    }

    main {
      min-width: 0;
      padding: 18px;
      display: grid;
      grid-template-rows: auto auto auto minmax(0, 1fr);
      gap: 14px;
    }

    h1, h2, h3 {
      margin: 0;
      line-height: 1.2;
      letter-spacing: 0;
    }

    h1 { font-size: 20px; }
    h2 { font-size: 15px; }
    h3 { font-size: 14px; }

    .muted { color: var(--muted); }
    .mono { font-family: var(--mono); }

    .topline {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 16px;
    }

    .summary {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 16px;
    }

    .metric {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      padding: 9px;
      min-width: 0;
    }

    .metric strong {
      display: block;
      font-size: 18px;
    }

    .stack { display: grid; gap: 10px; }

    .toolbar {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(130px, 170px) auto auto;
      gap: 8px;
      align-items: center;
    }

    .tabs {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }

    .panel {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      min-width: 0;
      overflow: hidden;
    }

    .panel-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfd;
    }

    .panel-body {
      padding: 12px;
      overflow: auto;
    }

    .split {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 14px;
      min-height: 0;
    }

    .split.detail-open {
      grid-template-columns: minmax(0, 1fr) minmax(320px, 380px);
    }

    .detail-panel { display: none; }
    .split.detail-open .detail-panel { display: block; }

    .detail-panel .panel-body {
      max-height: calc(100vh - 190px);
    }

    table {
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
    }

    th, td {
      text-align: left;
      border-bottom: 1px solid #edf0f4;
      padding: 8px;
      vertical-align: top;
      overflow-wrap: break-word;
    }

    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
      background: #fbfcfd;
      position: sticky;
      top: 0;
      z-index: 1;
    }

    tr[data-clickable="true"] { cursor: pointer; }
    tr[data-clickable="true"]:hover,
    tr[data-clickable="true"]:focus { background: #f6fbfa; outline: 0; }
    tr.active { background: var(--accent-soft); }

    .node-table th:nth-child(1) { width: 72px; }
    .node-table th:nth-child(2) { width: 110px; }
    .node-table th:nth-child(3) { width: 110px; }

    pre {
      margin: 0;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-family: var(--mono);
      font-size: 12px;
      background: #f6f7f9;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
    }

    .context-bar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      min-height: 34px;
      color: var(--muted);
      font-size: 13px;
    }

    .context-current {
      color: var(--text);
      font-weight: 700;
    }

    .empty-state {
      border: 1px dashed var(--line);
      border-radius: 8px;
      padding: 14px;
      color: var(--muted);
      background: #fbfcfd;
      margin-bottom: 12px;
    }

    .empty-state strong {
      display: block;
      color: var(--text);
      margin-bottom: 4px;
    }

    .side-list {
      display: grid;
      gap: 6px;
    }

    .side-item {
      width: 100%;
      min-height: 0;
      text-align: left;
      border: 1px solid transparent;
      background: transparent;
      padding: 8px;
      display: grid;
      gap: 4px;
    }

    .side-item:hover {
      background: #f6fbfa;
      border-color: var(--line);
    }

    .side-item.active {
      background: var(--accent-soft);
      border-color: var(--accent);
    }

    .side-title {
      font-weight: 700;
      overflow-wrap: anywhere;
    }

    .side-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
    }

    .document-view {
      display: grid;
      grid-template-columns: minmax(170px, 230px) minmax(0, 1fr);
      gap: 18px;
      align-items: start;
    }

    .document-toc {
      position: sticky;
      top: 0;
      display: grid;
      gap: 4px;
      max-height: calc(100vh - 190px);
      overflow: auto;
      border-right: 1px solid var(--line);
      padding-right: 10px;
    }

    .toc-link {
      border: 0;
      min-height: 28px;
      border-radius: 4px;
      padding: 4px 6px;
      text-align: left;
      color: var(--muted);
      background: transparent;
      overflow-wrap: anywhere;
    }

    .toc-link:hover { color: var(--accent); background: #f6fbfa; }

    .document-content {
      display: grid;
      gap: 12px;
    }

    .document-node {
      border-bottom: 1px solid #edf0f4;
      padding: 0 0 12px;
      cursor: pointer;
    }

    .document-node:last-child { border-bottom: 0; }
    .document-node:hover .document-heading,
    .document-node.active .document-heading { color: var(--accent); }

    .document-node.kind-chapter {
      margin-top: 8px;
      padding-top: 10px;
      border-top: 2px solid var(--line);
    }

    .document-node.kind-article {
      padding-top: 6px;
    }

    .document-heading {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: baseline;
      margin-bottom: 4px;
      color: var(--text);
    }

    .kind-chapter .document-heading {
      font-size: 18px;
      margin-bottom: 8px;
    }

    .kind-article .document-heading {
      font-size: 16px;
      margin-bottom: 7px;
    }

    .document-type {
      color: var(--muted);
      font-size: 12px;
      letter-spacing: 0;
    }

    .document-num {
      font-weight: 700;
    }

    .document-text {
      margin: 0;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-size: 15px;
      line-height: 1.75;
    }

    .compare-controls {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto auto;
      gap: 8px;
      align-items: end;
      margin-bottom: 12px;
    }

    .field {
      display: grid;
      gap: 4px;
    }

    .field label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
    }

    .segmented {
      display: inline-flex;
      gap: 4px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 3px;
      background: #fff;
    }

    .segmented button {
      border: 0;
      min-height: 28px;
      border-radius: 4px;
      padding: 4px 8px;
    }

    .diff-summary {
      display: grid;
      grid-template-columns: repeat(4, minmax(120px, 1fr));
      gap: 6px;
      margin-bottom: 12px;
    }

    .summary-pill {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      background: #fff;
    }

    .summary-pill strong {
      display: block;
      font-size: 18px;
    }

    .diff-row {
      border: 1px solid var(--line);
      border-left-width: 4px;
      border-radius: 6px;
      margin-bottom: 10px;
      overflow: hidden;
      background: #fff;
    }

    .diff-row.added { border-color: #9bd3ae; }
    .diff-row.removed { border-color: #f0aaa6; }
    .diff-row.modified { border-color: #e9c46a; }

    .diff-heading {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      align-items: baseline;
      padding: 8px 10px;
      border-bottom: 1px solid #edf0f4;
      background: #fbfcfd;
      cursor: pointer;
    }

    .diff-change {
      border-radius: 999px;
      padding: 1px 7px;
      font-size: 12px;
      font-weight: 700;
      background: #eef2f6;
      color: var(--muted);
    }

    .diff-change.added { background: #e8f7ed; color: #177245; }
    .diff-change.removed { background: #fff0ef; color: #b42318; }
    .diff-change.modified { background: #fff7df; color: #855a04; }

    .diff-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin: 0 0 12px;
    }

    .diff-body {
      padding: 10px;
    }

    .diff-side-by-side {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 10px;
    }

    .diff-cell {
      min-width: 0;
    }

    .diff-cell-title {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 6px;
    }

    .diff-text {
      margin: 0;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }

    .diff-text.added,
    .diff-line.added { background: #edf9f1; }

    .diff-text.removed,
    .diff-line.removed { background: #fff1f0; }

    .diff-line {
      display: block;
      min-height: 18px;
      padding: 0 4px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-family: var(--mono);
      font-size: 12px;
    }

    .diff-line.added::before { content: "+ "; font-weight: 700; }
    .diff-line.removed::before { content: "- "; font-weight: 700; }

    .inline-diff {
      border-radius: 3px;
      padding: 0 2px;
      font-weight: 700;
    }

    .inline-diff.added {
      background: #bcebd0;
    }

    .inline-diff.removed {
      background: #ffd0cc;
    }

    .detail-grid {
      display: grid;
      grid-template-columns: 120px minmax(0, 1fr);
      gap: 6px 10px;
      margin-bottom: 12px;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      color: var(--muted);
      background: #fff;
      max-width: 100%;
      overflow-wrap: anywhere;
    }

    .copy-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 12px;
    }

    .status {
      min-height: 20px;
      color: var(--muted);
      font-size: 13px;
    }

    .error { color: var(--danger); }

    @media (max-width: 980px) {
      .app { grid-template-columns: 1fr; }
      aside { border-right: 0; border-bottom: 1px solid var(--line); }
      .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .split,
      .split.detail-open,
      .document-view { grid-template-columns: 1fr; }
      .compare-controls { grid-template-columns: 1fr; }
      .diff-side-by-side { grid-template-columns: 1fr; }
      .diff-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .toolbar { grid-template-columns: 1fr; }
      .document-toc {
        position: static;
        max-height: 180px;
        border-right: 0;
        border-bottom: 1px solid var(--line);
        padding: 0 0 10px;
      }
      main { padding: 12px; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <div class="topline">
        <div>
          <h1>UCDB 法規瀏覽器</h1>
          <div class="muted">SQLite 法規版本資料庫</div>
        </div>
        <button id="refreshBtn" title="重新讀取資料庫">重新整理</button>
      </div>

      <div class="summary" id="summary"></div>

      <div class="stack">
        <div class="panel">
          <div class="panel-head">
            <h2>法規集</h2>
            <span class="badge" id="workCount">0</span>
          </div>
          <div class="panel-body" id="works"></div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <h2>版本</h2>
            <span class="badge" id="exprCount">0</span>
          </div>
          <div class="panel-body" id="expressions"></div>
        </div>
      </div>
    </aside>

    <main>
      <div class="context-bar" id="contextBar"></div>
      <div class="toolbar">
        <input id="searchInput" placeholder="搜尋目前版本的全文、標題、條號或節點 ID">
        <select id="searchScope" title="搜尋範圍">
          <option value="all">全文 / 標題 / 條號 / 節點 ID</option>
          <option value="text">全文</option>
          <option value="heading">標題</option>
          <option value="num">條號</option>
          <option value="eid">節點 ID</option>
        </select>
        <button id="searchBtn">搜尋</button>
        <button id="clearBtn">清除</button>
      </div>

      <div class="tabs">
        <button data-view="nodes" class="active">節點</button>
        <button data-view="document">文件</button>
        <button data-view="diff">差異</button>
        <button data-view="search">搜尋</button>
        <button data-view="revisions">修訂紀錄</button>
        <button data-view="xml">原始 XML</button>
      </div>

      <div class="split">
        <section class="panel">
          <div class="panel-head">
            <h2 id="listTitle">Nodes</h2>
            <span class="status" id="status"></span>
          </div>
          <div class="panel-body" id="list"></div>
        </section>

        <section class="panel detail-panel" id="detailPanel">
          <div class="panel-head">
            <h2>詳細資料</h2>
            <span class="badge" id="detailBadge">No selection</span>
          </div>
          <div class="panel-body" id="detail"></div>
        </section>
      </div>
    </main>
  </div>

  <script>
    const state = {
      summary: null,
      works: [],
      expressions: [],
      selectedWork: null,
      selectedExpression: null,
      view: 'nodes',
      searchQuery: '',
      searchScope: 'all',
      diffFromExpressionId: null,
      diffToExpressionId: null,
      diffMode: 'single',
      diffOnlyChanged: true,
      selectedNodeId: null,
    };

    const el = id => document.getElementById(id);
    const qs = params => new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== null && v !== undefined && v !== '')
    ).toString();

    async function api(path, params = {}) {
      const url = Object.keys(params).length ? `${path}?${qs(params)}` : path;
      const response = await fetch(url);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || response.statusText);
      return data;
    }

    function setStatus(text, error = false) {
      el('status').textContent = text || '';
      el('status').className = error ? 'status error' : 'status';
    }

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;',
      })[char]);
    }

    function nodeTypeLabel(type) {
      return {
        attachment: '沿革 / 附件',
        chapter: '章節',
        article: '條文',
        paragraph: '項',
        point: '款',
        subpoint: '目',
        item: '項目',
      }[String(type || '').toLowerCase()] || String(type || '');
    }

    function changeLabel(changeType) {
      return {
        added: '新增',
        removed: '刪除',
        modified: '修改',
        unchanged: '未變更',
      }[changeType] || changeType;
    }

    function changeIcon(changeType) {
      return {
        added: '+',
        removed: '-',
        modified: '±',
        unchanged: '=',
      }[changeType] || '';
    }

    function languageLabel(language) {
      return { zho: '繁中', eng: '英文' }[String(language || '').toLowerCase()] || language || '';
    }

    function setDetailEmpty() {
      el('detailBadge').textContent = '未選取';
      el('detail').innerHTML = '';
      document.querySelector('.split').classList.remove('detail-open');
      state.selectedNodeId = null;
    }

    function openDetail() {
      document.querySelector('.split').classList.add('detail-open');
    }

    function renderContext() {
      const work = state.selectedWork ? state.selectedWork.id : '未選取法規集';
      const expr = state.selectedExpression
        ? `${state.selectedExpression.version_label} ${languageLabel(state.selectedExpression.language)}`
        : '未選取版本';
      const view = {
        nodes: '節點',
        document: '文件',
        diff: '差異',
        search: '搜尋',
        revisions: '修訂紀錄',
        xml: '原始 XML',
      }[state.view] || state.view;
      el('contextBar').innerHTML = `
        <span>${escapeHtml(work)}</span>
        <span>/</span>
        <span>${escapeHtml(expr)}</span>
        <span>/</span>
        <span class="context-current">${escapeHtml(view)}</span>
      `;
    }

    function table(columns, rows, onClick, options = {}) {
      if (!rows.length) return '<div class="muted">沒有資料。</div>';
      const head = columns.map(col => `<th>${escapeHtml(col.label)}</th>`).join('');
      const body = rows.map((row, index) => {
        const cells = columns.map(col => `<td>${escapeHtml(col.value(row))}</td>`).join('');
        const active = options.selectedId && String(row.id) === String(options.selectedId);
        return `<tr tabindex="${onClick ? '0' : '-1'}" data-index="${index}" data-clickable="${onClick ? 'true' : 'false'}" class="${active ? 'active' : ''}">${cells}</tr>`;
      }).join('');
      setTimeout(() => {
        if (!onClick) return;
        document.querySelectorAll('#list tr[data-index]').forEach(tr => {
          const activate = () => {
            document.querySelectorAll('#list tr.active').forEach(row => row.classList.remove('active'));
            tr.classList.add('active');
            onClick(rows[Number(tr.dataset.index)]);
          };
          tr.addEventListener('click', activate);
          tr.addEventListener('keydown', event => {
            if (event.key === 'Enter') activate();
          });
        });
      }, 0);
      return `<table class="${escapeHtml(options.className || '')}"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
    }

    function sideList(rootId, rows, renderRow, onClick, selectedId) {
      const root = el(rootId);
      if (!rows.length) {
        root.innerHTML = '<div class="muted">沒有資料。</div>';
        return;
      }
      root.innerHTML = '<div class="side-list">' + rows.map((row, index) => {
        const selected = selectedId !== null && selectedId === row.id;
        return `<button class="side-item ${selected ? 'active' : ''}" data-index="${index}">${renderRow(row)}</button>`;
      }).join('') + '</div>';
      root.querySelectorAll('[data-index]').forEach(item => {
        item.addEventListener('click', () => onClick(rows[Number(item.dataset.index)]));
      });
    }

    function renderSummary() {
      const summary = state.summary || {};
      const metrics = [
        ['法規集', summary.works],
        ['版本', summary.expressions],
        ['條文節點', summary.nodes],
        ['修訂紀錄', summary.revisions],
        ['變更', summary.changes],
      ];
      el('summary').innerHTML = metrics.map(([label, value]) => (
        `<div class="metric"><strong>${escapeHtml(value ?? 0)}</strong><span class="muted">${label}</span></div>`
      )).join('');
    }

    function renderSidebar() {
      el('workCount').textContent = state.works.length;
      el('exprCount').textContent = state.expressions.length;
      sideList('works', state.works, row => `
        <span class="side-title">${escapeHtml(row.id)}</span>
        <span class="side-meta">
          <span>${escapeHtml(row.expression_count)} 個版本</span>
          <span>${escapeHtml(row.node_count)} 個節點</span>
        </span>
      `, selectWork, state.selectedWork && state.selectedWork.id);
      sideList('expressions', state.expressions, row => `
        <span class="side-title">${escapeHtml(row.version_label)}</span>
        <span class="side-meta">
          <span>${escapeHtml(languageLabel(row.language))}</span>
          <span>${escapeHtml(row.node_count)} 個節點</span>
        </span>
      `, selectExpression, state.selectedExpression && state.selectedExpression.id);
      renderContext();
    }

    function expressionById(id) {
      return state.expressions.find(expr => String(expr.id) === String(id)) || null;
    }

    function previousExpressionId(expressionId) {
      const index = state.expressions.findIndex(expr => String(expr.id) === String(expressionId));
      if (index > 0) return state.expressions[index - 1].id;
      if (state.expressions.length > 1) return state.expressions[0].id;
      return expressionId;
    }

    function ensureDiffDefaults() {
      if (!state.expressions.length) {
        state.diffFromExpressionId = null;
        state.diffToExpressionId = null;
        return;
      }
      if (!expressionById(state.diffToExpressionId)) {
        state.diffToExpressionId = state.selectedExpression
          ? state.selectedExpression.id
          : state.expressions[state.expressions.length - 1].id;
      }
      if (!expressionById(state.diffFromExpressionId)) {
        state.diffFromExpressionId = previousExpressionId(state.diffToExpressionId);
      }
      if (String(state.diffFromExpressionId) === String(state.diffToExpressionId) && state.expressions.length > 1) {
        state.diffFromExpressionId = previousExpressionId(state.diffToExpressionId);
      }
    }

    function expressionOptions(selectedId) {
      return state.expressions.map(expr => (
        `<option value="${escapeHtml(expr.id)}" ${String(expr.id) === String(selectedId) ? 'selected' : ''}>${escapeHtml(expr.version_label)} ${escapeHtml(expr.language)}</option>`
      )).join('');
    }

    async function loadInitial() {
      setStatus('Loading...');
      state.summary = await api('/api/summary');
      const worksPayload = await api('/api/works');
      state.works = worksPayload.works;
      state.selectedWork = state.works[0] || null;
      if (state.selectedWork) {
        const exprPayload = await api('/api/expressions', { work_id: state.selectedWork.id });
        state.expressions = exprPayload.expressions;
        state.selectedExpression = state.expressions[state.expressions.length - 1] || null;
      }
      renderSummary();
      renderSidebar();
      await renderView();
      setStatus('');
    }

    async function selectWork(work) {
      state.selectedWork = work;
      state.selectedExpression = null;
      state.selectedNodeId = null;
      state.diffFromExpressionId = null;
      state.diffToExpressionId = null;
      state.expressions = (await api('/api/expressions', { work_id: work.id })).expressions;
      state.selectedExpression = state.expressions[state.expressions.length - 1] || null;
      renderSidebar();
      await renderView();
    }

    async function selectExpression(expression) {
      state.selectedExpression = expression;
      state.selectedNodeId = null;
      if (state.view === 'diff') {
        state.diffToExpressionId = expression.id;
        state.diffFromExpressionId = previousExpressionId(expression.id);
      }
      renderSidebar();
      await renderView();
    }

    async function renderView() {
      try {
        renderContext();
        if (state.view === 'nodes') return renderNodes();
        if (state.view === 'document') return renderDocument();
        if (state.view === 'diff') return renderDiff();
        if (state.view === 'search') return renderSearch();
        if (state.view === 'revisions') return renderRevisions();
        if (state.view === 'xml') return renderXml();
      } catch (error) {
        setStatus(error.message, true);
      }
    }

    async function renderNodes() {
      el('listTitle').textContent = '節點';
      setDetailEmpty();
      if (!state.selectedExpression) {
        el('list').innerHTML = '<div class="muted">尚未選取版本。</div>';
        return;
      }
      const payload = await api('/api/nodes', { expression_id: state.selectedExpression.id, limit: 2000 });
      el('list').innerHTML = `
        <div class="empty-state">
          <strong>點選任一列查看詳細資料。</strong>
          詳細資料會顯示條文文字、節點 ID、版本紀錄與逐行來源；主表格保留較適合瀏覽的欄位。
        </div>
        ${table([
          { label: '順序', value: row => row.ordering },
          { label: '類型', value: row => nodeTypeLabel(row.node_type) },
          { label: '條號', value: row => row.num || '' },
          { label: '標題 / 摘要', value: row => row.heading || row.preview || '' },
        ], payload.nodes, openNode, { className: 'node-table', selectedId: state.selectedNodeId })}
      `;
    }

    async function renderDocument() {
      el('listTitle').textContent = '文件閱讀';
      setDetailEmpty();
      if (!state.selectedExpression) {
        el('list').innerHTML = '<div class="muted">尚未選取版本。</div>';
        return;
      }
      const payload = await api('/api/document', { expression_id: state.selectedExpression.id, limit: 10000 });
      if (!payload.document.length) {
        el('list').innerHTML = '<div class="muted">此版本沒有文件節點。</div>';
        return;
      }
      const tocRows = payload.document.filter(row => ['chapter', 'article'].includes(String(row.node_type).toLowerCase()));
      el('list').innerHTML = `<div class="document-view">
        <nav class="document-toc" aria-label="條文目錄">
          ${tocRows.map(row => {
            const title = [row.num, row.heading].filter(Boolean).join(' ') || row.node_eid;
            return `<button class="toc-link" data-toc-node-id="${escapeHtml(row.id)}">${escapeHtml(title)}</button>`;
          }).join('')}
        </nav>
        <div class="document-content">${payload.document.map(row => {
        const depth = Math.max(0, Number(row.depth || 0));
        const title = [row.num, row.heading].filter(Boolean).join(' ');
        const kind = `kind-${String(row.node_type || '').toLowerCase()}`;
        const showLabel = !['chapter', 'article', 'paragraph', 'point'].includes(String(row.node_type || '').toLowerCase());
        return `
          <article class="document-node ${escapeHtml(kind)}" data-node-id="${row.id}" style="padding-left:${Math.min(depth, 6) * 14}px">
            <div class="document-heading">
              ${showLabel ? `<span class="document-type">${escapeHtml(nodeTypeLabel(row.node_type))}</span>` : ''}
              ${row.num ? `<span class="document-num">${escapeHtml(row.num)}</span>` : ''}
              ${row.heading ? `<strong>${escapeHtml(row.heading)}</strong>` : ''}
              ${!title ? `<span class="muted">${escapeHtml(row.node_eid)}</span>` : ''}
            </div>
            ${row.text ? `<p class="document-text">${escapeHtml(row.text)}</p>` : ''}
          </article>
        `;
      }).join('')}</div></div>`;
      document.querySelectorAll('[data-node-id]').forEach(article => {
        article.addEventListener('click', () => {
          document.querySelectorAll('.document-node.active').forEach(node => node.classList.remove('active'));
          article.classList.add('active');
          openNode({ id: article.dataset.nodeId });
        });
      });
      document.querySelectorAll('[data-toc-node-id]').forEach(link => {
        link.addEventListener('click', () => {
          const target = document.querySelector(`[data-node-id="${CSS.escape(link.dataset.tocNodeId)}"]`);
          if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
      });
    }

    async function renderDiff() {
      el('listTitle').textContent = '版本差異';
      setDetailEmpty();
      if (state.expressions.length < 2) {
        el('list').innerHTML = '<div class="muted">至少需要兩個版本才能比對。</div>';
        return;
      }
      ensureDiffDefaults();
      const payload = await api('/api/document-diff', {
        from_expression_id: state.diffFromExpressionId,
        to_expression_id: state.diffToExpressionId,
        limit: 10000,
      });
      const fromLabel = payload.from_expression.version_label;
      const toLabel = payload.to_expression.version_label;
      const changedRows = payload.rows.filter(row => row.change_type !== 'unchanged');
      const visibleRows = state.diffOnlyChanged ? changedRows : payload.rows;
      const changedTotal = payload.stats.added + payload.stats.removed + payload.stats.modified;
      el('list').innerHTML = `
        ${renderDiffControls()}
        <div class="diff-summary">
          <div class="summary-pill"><strong>${escapeHtml(payload.stats.added)}</strong><span class="muted">新增節點</span></div>
          <div class="summary-pill"><strong>${escapeHtml(payload.stats.removed)}</strong><span class="muted">刪除節點</span></div>
          <div class="summary-pill"><strong>${escapeHtml(payload.stats.modified)}</strong><span class="muted">修改節點</span></div>
          <div class="summary-pill"><strong>${escapeHtml(payload.stats.unchanged)}</strong><span class="muted">未變更節點</span></div>
        </div>
        <div class="diff-actions">
          <span class="badge">${escapeHtml(fromLabel)} → ${escapeHtml(toLabel)}</span>
          <span class="badge">共 ${escapeHtml(changedTotal)} 處變更</span>
          <button id="prevChangeBtn">上一個變更</button>
          <button id="nextChangeBtn">下一個變更</button>
        </div>
        <div>${visibleRows.map((row, index) => (
          state.diffMode === 'side-by-side'
            ? renderSideBySideDiffRow(row, fromLabel, toLabel, index)
            : renderSingleDiffRow(row, index)
        )).join('')}</div>
      `;
      el('diffFrom').addEventListener('change', async event => {
        state.diffFromExpressionId = Number(event.target.value);
        await renderDiff();
      });
      el('diffTo').addEventListener('change', async event => {
        state.diffToExpressionId = Number(event.target.value);
        await renderDiff();
      });
      document.querySelectorAll('[data-diff-mode]').forEach(button => {
        button.addEventListener('click', async () => {
          state.diffMode = button.dataset.diffMode;
          await renderDiff();
        });
      });
      el('diffOnlyChanged').addEventListener('change', async event => {
        state.diffOnlyChanged = event.target.checked;
        state.currentChangeIndex = 0;
        await renderDiff();
      });
      el('prevChangeBtn').addEventListener('click', () => jumpChange(-1));
      el('nextChangeBtn').addEventListener('click', () => jumpChange(1));
      document.querySelectorAll('[data-open-node-id]').forEach(row => {
        row.addEventListener('click', event => {
          const target = event.target;
          if (target && (['SELECT', 'BUTTON'].includes(target.tagName) || target.closest('summary'))) return;
          openNode({ id: row.dataset.openNodeId });
        });
      });
    }

    function renderDiffControls() {
      return `
        <div class="compare-controls">
          <div class="field">
            <label for="diffFrom">來源版本</label>
            <select id="diffFrom">${expressionOptions(state.diffFromExpressionId)}</select>
          </div>
          <div class="field">
            <label for="diffTo">目標版本</label>
            <select id="diffTo">${expressionOptions(state.diffToExpressionId)}</select>
          </div>
          <div class="field">
            <label>檢視</label>
            <div class="segmented">
              <button data-diff-mode="single" class="${state.diffMode === 'single' ? 'active' : ''}">單欄</button>
              <button data-diff-mode="side-by-side" class="${state.diffMode === 'side-by-side' ? 'active' : ''}">左右比對</button>
            </div>
          </div>
          <div class="field">
            <label>篩選</label>
            <label class="badge"><input id="diffOnlyChanged" type="checkbox" ${state.diffOnlyChanged ? 'checked' : ''} style="width:auto; min-height:0; margin-right:6px">只看變更</label>
          </div>
        </div>
      `;
    }

    function renderSingleDiffRow(row, index) {
      const nodeId = (row.to && row.to.id) || (row.from && row.from.id) || '';
      const open = row.change_type !== 'unchanged' ? 'open' : '';
      return `
        <details class="diff-row ${escapeHtml(row.change_type)}" data-change-index="${index}" data-open-node-id="${escapeHtml(nodeId)}" ${open}>
          <summary class="diff-heading">${renderDiffHeadingContent(row)}</summary>
          <div class="diff-body">
            ${row.change_type === 'modified' ? renderDiffLines(row.text_diff) : renderSingleNodeText(row)}
          </div>
        </details>
      `;
    }

    function renderSideBySideDiffRow(row, fromLabel, toLabel, index) {
      const nodeId = (row.to && row.to.id) || (row.from && row.from.id) || '';
      const open = row.change_type !== 'unchanged' ? 'open' : '';
      return `
        <details class="diff-row ${escapeHtml(row.change_type)}" data-change-index="${index}" data-open-node-id="${escapeHtml(nodeId)}" ${open}>
          <summary class="diff-heading">${renderDiffHeadingContent(row)}</summary>
          <div class="diff-body diff-side-by-side">
            <div class="diff-cell">
              <div class="diff-cell-title">${escapeHtml(fromLabel)}</div>
              ${renderSideText(row.from, row.change_type === 'removed' || row.change_type === 'modified' ? 'removed' : '')}
            </div>
            <div class="diff-cell">
              <div class="diff-cell-title">${escapeHtml(toLabel)}</div>
              ${renderSideText(row.to, row.change_type === 'added' || row.change_type === 'modified' ? 'added' : '')}
            </div>
          </div>
        </details>
      `;
    }

    function renderDiffHeadingContent(row) {
      const title = [row.num, row.heading].filter(Boolean).join(' ') || row.node_eid || '';
      return `
          <span class="diff-change ${escapeHtml(row.change_type)}">${escapeHtml(changeIcon(row.change_type))} ${escapeHtml(changeLabel(row.change_type))}</span>
          <span class="document-type">${escapeHtml(nodeTypeLabel(row.node_type))}</span>
          <strong>${escapeHtml(title)}</strong>
          <span class="muted">${escapeHtml(row.node_eid || '')}</span>
      `;
    }

    function renderSingleNodeText(row) {
      if (row.change_type === 'added') return renderSideText(row.to, 'added');
      if (row.change_type === 'removed') return renderSideText(row.from, 'removed');
      return renderSideText(row.to || row.from, '');
    }

    function renderSideText(node, changeClass) {
      if (!node) return '<div class="muted">此側沒有對應節點。</div>';
      return `<p class="diff-text ${escapeHtml(changeClass)}">${escapeHtml(node.text || '')}</p>`;
    }

    function renderDiffLines(diff) {
      const lines = String(diff || '').split('\\n').filter(line => (
        !line.startsWith('---') && !line.startsWith('+++') && !line.startsWith('@@')
      ));
      if (!lines.length) return '<div class="muted">只有中繼資料變更。</div>';
      const rendered = [];
      for (let index = 0; index < lines.length;) {
        if (!lines[index].startsWith('-')) {
          rendered.push(renderPlainDiffLine(lines[index]));
          index += 1;
          continue;
        }

        const removed = [];
        while (index < lines.length && lines[index].startsWith('-')) {
          removed.push(lines[index].slice(1));
          index += 1;
        }

        const added = [];
        while (index < lines.length && lines[index].startsWith('+')) {
          added.push(lines[index].slice(1));
          index += 1;
        }

        if (!added.length) {
          rendered.push(...removed.map(line => renderDiffLineHtml('removed', escapeHtml(line || ' '))));
          continue;
        }

        const count = Math.max(removed.length, added.length);
        for (let offset = 0; offset < count; offset += 1) {
          const before = removed[offset];
          const after = added[offset];
          if (before !== undefined && after !== undefined) {
            const pair = inlineDiffHtml(before, after);
            rendered.push(renderDiffLineHtml('removed', pair.before || ' '));
            rendered.push(renderDiffLineHtml('added', pair.after || ' '));
          } else if (before !== undefined) {
            rendered.push(renderDiffLineHtml('removed', escapeHtml(before || ' ')));
          } else {
            rendered.push(renderDiffLineHtml('added', escapeHtml(after || ' ')));
          }
        }
      }
      return rendered.join('');
    }

    function renderPlainDiffLine(line) {
      const kind = line.startsWith('+') ? 'added' : (line.startsWith('-') ? 'removed' : '');
      const content = kind ? line.slice(1) : line.replace(/^ /, '');
      return renderDiffLineHtml(kind, escapeHtml(content || ' '));
    }

    function renderDiffLineHtml(kind, html) {
      return `<span class="diff-line ${escapeHtml(kind)}">${html}</span>`;
    }

    function inlineDiffHtml(before, after) {
      if (before === after) {
        const html = escapeHtml(before);
        return { before: html, after: html };
      }

      const beforeChars = Array.from(before);
      const afterChars = Array.from(after);
      let prefix = 0;
      while (
        prefix < beforeChars.length &&
        prefix < afterChars.length &&
        beforeChars[prefix] === afterChars[prefix]
      ) {
        prefix += 1;
      }

      let suffix = 0;
      while (
        suffix < beforeChars.length - prefix &&
        suffix < afterChars.length - prefix &&
        beforeChars[beforeChars.length - 1 - suffix] === afterChars[afterChars.length - 1 - suffix]
      ) {
        suffix += 1;
      }

      const beforeMiddle = beforeChars.slice(prefix, beforeChars.length - suffix);
      const afterMiddle = afterChars.slice(prefix, afterChars.length - suffix);
      const prefixHtml = escapeHtml(beforeChars.slice(0, prefix).join(''));
      const suffixHtml = escapeHtml(beforeChars.slice(beforeChars.length - suffix).join(''));

      if (beforeMiddle.length * afterMiddle.length > 160000) {
        return {
          before: prefixHtml + wrapInlineDiff('removed', beforeMiddle.join('')) + suffixHtml,
          after: prefixHtml + wrapInlineDiff('added', afterMiddle.join('')) + suffixHtml,
        };
      }

      const ops = diffCharOps(beforeMiddle, afterMiddle);
      let beforeHtml = prefixHtml;
      let afterHtml = prefixHtml;
      for (const op of ops) {
        if (op.kind === 'equal') {
          const html = escapeHtml(op.text);
          beforeHtml += html;
          afterHtml += html;
        } else if (op.kind === 'removed') {
          beforeHtml += wrapInlineDiff('removed', op.text);
        } else {
          afterHtml += wrapInlineDiff('added', op.text);
        }
      }
      return { before: beforeHtml + suffixHtml, after: afterHtml + suffixHtml };
    }

    function diffCharOps(beforeChars, afterChars) {
      const rows = beforeChars.length + 1;
      const cols = afterChars.length + 1;
      const scores = new Uint32Array(rows * cols);
      for (let row = beforeChars.length - 1; row >= 0; row -= 1) {
        for (let col = afterChars.length - 1; col >= 0; col -= 1) {
          const offset = row * cols + col;
          scores[offset] = beforeChars[row] === afterChars[col]
            ? scores[(row + 1) * cols + col + 1] + 1
            : Math.max(scores[(row + 1) * cols + col], scores[row * cols + col + 1]);
        }
      }

      const ops = [];
      let row = 0;
      let col = 0;
      while (row < beforeChars.length || col < afterChars.length) {
        if (row < beforeChars.length && col < afterChars.length && beforeChars[row] === afterChars[col]) {
          pushDiffOp(ops, 'equal', beforeChars[row]);
          row += 1;
          col += 1;
        } else if (
          col < afterChars.length &&
          (row === beforeChars.length || scores[row * cols + col + 1] >= scores[(row + 1) * cols + col])
        ) {
          pushDiffOp(ops, 'added', afterChars[col]);
          col += 1;
        } else {
          pushDiffOp(ops, 'removed', beforeChars[row]);
          row += 1;
        }
      }
      return ops;
    }

    function pushDiffOp(ops, kind, char) {
      const last = ops[ops.length - 1];
      if (last && last.kind === kind) {
        last.text += char;
      } else {
        ops.push({ kind, text: char });
      }
    }

    function wrapInlineDiff(kind, value) {
      if (!value) return '';
      return `<mark class="inline-diff ${escapeHtml(kind)}">${escapeHtml(value)}</mark>`;
    }

    function jumpChange(direction) {
      const rows = Array.from(document.querySelectorAll('.diff-row:not(.unchanged)'));
      if (!rows.length) return;
      state.currentChangeIndex = Math.min(
        Math.max((state.currentChangeIndex || 0) + direction, 0),
        rows.length - 1
      );
      rows[state.currentChangeIndex].open = true;
      rows[state.currentChangeIndex].scrollIntoView({ behavior: 'smooth', block: 'center' });
    }

    async function openNode(row) {
      const node = await api('/api/node', { id: row.id });
      state.selectedNodeId = node.id;
      openDetail();
      el('detailBadge').textContent = node.node_eid;
      const lines = (node.lines || []).map(line => (
        `<tr><td>${line.line_no}</td><td>${escapeHtml(line.origin_version_label)}</td><td>${escapeHtml(line.text)}</td></tr>`
      )).join('');
      const history = (node.history || []).map(item => (
        `<tr><td>${item.revision_id}</td><td>${escapeHtml(item.to_label)}</td><td>${escapeHtml(changeLabel(item.change_type))}</td></tr>`
      )).join('');
      el('detail').innerHTML = `
        <div class="copy-row">
          <button data-copy-node-text>複製條文</button>
          <button data-copy-node-id>複製節點 ID</button>
          <button data-close-detail>收合詳細資料</button>
        </div>
        <div class="detail-grid">
          <div class="muted">法規集</div><div>${escapeHtml(node.work_id)}</div>
          <div class="muted">版本</div><div>${escapeHtml(node.version_label)} ${escapeHtml(languageLabel(node.language))}</div>
          <div class="muted">類型</div><div>${escapeHtml(nodeTypeLabel(node.node_type))}</div>
          <div class="muted">條號</div><div>${escapeHtml(node.num || '')}</div>
          <div class="muted">標題</div><div>${escapeHtml(node.heading || '')}</div>
          <div class="muted">節點 ID</div><div class="mono">${escapeHtml(node.node_eid || '')}</div>
          <div class="muted">文字雜湊</div><div class="mono">${escapeHtml(node.text_hash || '')}</div>
        </div>
        <h3>條文內容</h3>
        <pre>${escapeHtml(node.text || '')}</pre>
        <h3 style="margin-top:12px">逐行來源</h3>
        <table><thead><tr><th>行</th><th>來源版本</th><th>文字</th></tr></thead><tbody>${lines}</tbody></table>
        <h3 style="margin-top:12px">版本紀錄</h3>
        <table><thead><tr><th>修訂</th><th>目標版本</th><th>類型</th></tr></thead><tbody>${history}</tbody></table>
      `;
      document.querySelector('[data-copy-node-text]').addEventListener('click', () => navigator.clipboard.writeText(node.text || ''));
      document.querySelector('[data-copy-node-id]').addEventListener('click', () => navigator.clipboard.writeText(node.node_eid || ''));
      document.querySelector('[data-close-detail]').addEventListener('click', setDetailEmpty);
    }

    async function renderSearch() {
      el('listTitle').textContent = '搜尋';
      setDetailEmpty();
      const q = state.searchQuery.trim();
      if (!q) {
        el('list').innerHTML = '<div class="empty-state"><strong>輸入關鍵字後搜尋目前版本。</strong>可搜尋全文、標題、條號或節點 ID；結果會顯示數量並可點選查看詳細資料。</div>';
        return;
      }
      const payload = await api('/api/search', {
        q,
        scope: state.searchScope,
        work_id: state.selectedWork && state.selectedWork.id,
        expression_id: state.selectedExpression && state.selectedExpression.id,
      });
      el('list').innerHTML = `
        <div class="empty-state"><strong>${escapeHtml(payload.results.length)} 筆結果</strong>搜尋範圍：${escapeHtml(el('searchScope').selectedOptions[0].textContent)}</div>
        ${table([
          { label: '版本', value: row => row.version_label },
          { label: '類型', value: row => nodeTypeLabel(row.node_type) },
          { label: '條號', value: row => row.num || '' },
          { label: '內容摘要', value: row => row.preview || '' },
        ], payload.results, openNode, { className: 'node-table' })}
      `;
    }

    async function renderRevisions() {
      el('listTitle').textContent = '修訂紀錄';
      setDetailEmpty();
      if (!state.selectedWork) {
        el('list').innerHTML = '<div class="muted">尚未選取法規集。</div>';
        return;
      }
      const payload = await api('/api/revisions', { work_id: state.selectedWork.id });
      el('list').innerHTML = table([
        { label: 'ID', value: row => row.id },
        { label: '來源版本', value: row => row.from_label || '(初始)' },
        { label: '目標版本', value: row => row.to_label },
        { label: '新增', value: row => row.nodes_added },
        { label: '刪除', value: row => row.nodes_removed },
        { label: '修改', value: row => row.nodes_modified },
        { label: '未變更', value: row => row.nodes_unchanged },
      ], payload.revisions, openRevision);
    }

    async function openRevision(row) {
      const payload = await api('/api/changes', { revision_id: row.id, limit: 1000 });
      const rows = payload.changes.map(change => (
        `<tr data-change-id="${change.id}" data-clickable="true"><td>${change.id}</td><td>${escapeHtml(changeLabel(change.change_type))}</td><td>${escapeHtml(nodeTypeLabel(change.node_type))}</td><td>${escapeHtml(change.node_eid || '')}</td><td>${escapeHtml(change.heading || '')}</td></tr>`
      )).join('');
      el('detailBadge').textContent = `Revision ${row.id}`;
      openDetail();
      el('detail').innerHTML = `<div class="copy-row"><button data-close-detail>收合詳細資料</button></div><table><thead><tr><th>ID</th><th>類型</th><th>節點</th><th>節點 ID</th><th>標題</th></tr></thead><tbody>${rows}</tbody></table><pre id="changeDiff" style="margin-top:12px"></pre>`;
      document.querySelector('[data-close-detail]').addEventListener('click', setDetailEmpty);
      document.querySelectorAll('[data-change-id]').forEach(tr => {
        tr.addEventListener('click', async () => {
          const change = await api('/api/change', { id: tr.dataset.changeId });
          el('changeDiff').textContent = change.text_diff || change.diff_preview || '';
        });
      });
    }

    async function renderXml() {
      el('listTitle').textContent = '原始 XML';
      setDetailEmpty();
      if (!state.selectedExpression) {
        el('list').innerHTML = '<div class="muted">尚未選取版本。</div>';
        return;
      }
      const payload = await api('/api/xml', { expression_id: state.selectedExpression.id });
      el('list').innerHTML = `<pre>${escapeHtml(payload.canonical_xml || '')}</pre>`;
    }

    document.querySelectorAll('[data-view]').forEach(button => {
      button.addEventListener('click', async () => {
        document.querySelectorAll('[data-view]').forEach(tab => tab.classList.remove('active'));
        button.classList.add('active');
        state.view = button.dataset.view;
        await renderView();
      });
    });

    el('searchBtn').addEventListener('click', async () => {
      state.searchQuery = el('searchInput').value;
      state.searchScope = el('searchScope').value;
      state.view = 'search';
      document.querySelectorAll('[data-view]').forEach(tab => tab.classList.toggle('active', tab.dataset.view === 'search'));
      await renderView();
    });

    el('searchInput').addEventListener('keydown', event => {
      if (event.key === 'Enter') el('searchBtn').click();
    });

    el('clearBtn').addEventListener('click', async () => {
      el('searchInput').value = '';
      state.searchQuery = '';
      state.view = 'nodes';
      document.querySelectorAll('[data-view]').forEach(tab => tab.classList.toggle('active', tab.dataset.view === 'nodes'));
      await renderView();
    });

    el('refreshBtn').addEventListener('click', () => loadInitial().catch(error => setStatus(error.message, true)));
    loadInitial().catch(error => setStatus(error.message, true));
  </script>
</body>
</html>
"""
