"""Minimal read-only web browser for a UCDB SQLite database."""

from __future__ import annotations

import json
import sqlite3
import webbrowser
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, quote, unquote, urlparse

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
                       heading, ordering, text
                FROM nodes
                WHERE expression_id = ?
                ORDER BY ordering
                LIMIT ?
                """,
                (expression_id, _clamp(limit, 1, 20000)),
            )
            return [_row(row) for row in rows]

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
        work_id: str | None = None,
        expression_id: int | None = None,
        limit: int = 50,
    ) -> list[JsonDict]:
        query = query.strip()
        if not query:
            return []
        with self.connect() as conn:
            params: list[Any]
            if len(query) >= 3:
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
                    )
                """
                params = [like, like, like]
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
                elif parsed.path == "/api/node":
                    self._send_json_or_404(
                        store.node(_int_required(query, "id")), "node not found"
                    )
                elif parsed.path == "/api/search":
                    self._send_json(
                        {
                            "results": store.search(
                                _required(query, "q"),
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
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>UCDB Browser</title>
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
      grid-template-columns: minmax(260px, 320px) minmax(0, 1fr);
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
      grid-template-rows: auto auto minmax(0, 1fr);
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
      grid-template-columns: repeat(5, minmax(0, 1fr));
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
      grid-template-columns: minmax(0, 1fr) auto auto;
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
      grid-template-columns: minmax(320px, .9fr) minmax(0, 1.3fr);
      gap: 14px;
      min-height: 0;
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
      overflow-wrap: anywhere;
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
    tr[data-clickable="true"]:hover { background: #f6fbfa; }

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
      .split { grid-template-columns: 1fr; }
      main { padding: 12px; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <div class="topline">
        <div>
          <h1>UCDB Browser</h1>
          <div class="muted">Akoma Ntoso SQLite</div>
        </div>
        <button id="refreshBtn" title="Reload database metadata">Reload</button>
      </div>

      <div class="summary" id="summary"></div>

      <div class="stack">
        <div class="panel">
          <div class="panel-head">
            <h2>Works</h2>
            <span class="badge" id="workCount">0</span>
          </div>
          <div class="panel-body" id="works"></div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <h2>Expressions</h2>
            <span class="badge" id="exprCount">0</span>
          </div>
          <div class="panel-body" id="expressions"></div>
        </div>
      </div>
    </aside>

    <main>
      <div class="toolbar">
        <input id="searchInput" placeholder="Search heading, text, or eId">
        <button id="searchBtn">Search</button>
        <button id="clearBtn">Clear</button>
      </div>

      <div class="tabs">
        <button data-view="nodes" class="active">Nodes</button>
        <button data-view="search">Search</button>
        <button data-view="revisions">Revisions</button>
        <button data-view="xml">AKN XML</button>
      </div>

      <div class="split">
        <section class="panel">
          <div class="panel-head">
            <h2 id="listTitle">Nodes</h2>
            <span class="status" id="status"></span>
          </div>
          <div class="panel-body" id="list"></div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <h2>Detail</h2>
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

    function table(columns, rows, onClick) {
      if (!rows.length) return '<div class="muted">No rows.</div>';
      const head = columns.map(col => `<th>${escapeHtml(col.label)}</th>`).join('');
      const body = rows.map((row, index) => {
        const cells = columns.map(col => `<td>${escapeHtml(col.value(row))}</td>`).join('');
        return `<tr data-index="${index}" data-clickable="${onClick ? 'true' : 'false'}">${cells}</tr>`;
      }).join('');
      setTimeout(() => {
        if (!onClick) return;
        document.querySelectorAll('#list tr[data-index]').forEach(tr => {
          tr.addEventListener('click', () => onClick(rows[Number(tr.dataset.index)]));
        });
      }, 0);
      return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
    }

    function sideTable(rootId, rows, columns, onClick, selectedId) {
      const root = el(rootId);
      if (!rows.length) {
        root.innerHTML = '<div class="muted">No rows.</div>';
        return;
      }
      root.innerHTML = '<table><tbody>' + rows.map((row, index) => {
        const selected = selectedId !== null && selectedId === row.id;
        const cells = columns.map(col => `<td>${escapeHtml(col.value(row))}</td>`).join('');
        return `<tr data-index="${index}" data-clickable="true" class="${selected ? 'active' : ''}">${cells}</tr>`;
      }).join('') + '</tbody></table>';
      root.querySelectorAll('tr[data-index]').forEach(tr => {
        tr.addEventListener('click', () => onClick(rows[Number(tr.dataset.index)]));
      });
    }

    function renderSummary() {
      const summary = state.summary || {};
      const metrics = [
        ['Works', summary.works],
        ['Expressions', summary.expressions],
        ['Nodes', summary.nodes],
        ['Revisions', summary.revisions],
        ['Changes', summary.changes],
      ];
      el('summary').innerHTML = metrics.map(([label, value]) => (
        `<div class="metric"><strong>${escapeHtml(value ?? 0)}</strong><span class="muted">${label}</span></div>`
      )).join('');
    }

    function renderSidebar() {
      el('workCount').textContent = state.works.length;
      el('exprCount').textContent = state.expressions.length;
      sideTable('works', state.works, [
        { label: 'Work', value: row => row.id },
        { label: 'Count', value: row => row.expression_count },
      ], selectWork, state.selectedWork && state.selectedWork.id);
      sideTable('expressions', state.expressions, [
        { label: 'Version', value: row => row.version_label },
        { label: 'Lang', value: row => row.language },
        { label: 'Nodes', value: row => row.node_count },
      ], selectExpression, state.selectedExpression && state.selectedExpression.id);
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
      state.expressions = (await api('/api/expressions', { work_id: work.id })).expressions;
      state.selectedExpression = state.expressions[state.expressions.length - 1] || null;
      renderSidebar();
      await renderView();
    }

    async function selectExpression(expression) {
      state.selectedExpression = expression;
      renderSidebar();
      await renderView();
    }

    async function renderView() {
      try {
        if (state.view === 'nodes') return renderNodes();
        if (state.view === 'search') return renderSearch();
        if (state.view === 'revisions') return renderRevisions();
        if (state.view === 'xml') return renderXml();
      } catch (error) {
        setStatus(error.message, true);
      }
    }

    async function renderNodes() {
      el('listTitle').textContent = 'Nodes';
      el('detail').innerHTML = '<div class="muted">Select a node.</div>';
      el('detailBadge').textContent = 'No selection';
      if (!state.selectedExpression) {
        el('list').innerHTML = '<div class="muted">No expression selected.</div>';
        return;
      }
      const payload = await api('/api/nodes', { expression_id: state.selectedExpression.id, limit: 2000 });
      el('list').innerHTML = table([
        { label: 'Order', value: row => row.ordering },
        { label: 'Type', value: row => row.node_type },
        { label: 'Num', value: row => row.num || '' },
        { label: 'eId', value: row => row.node_eid },
        { label: 'Heading', value: row => row.heading || '' },
      ], payload.nodes, openNode);
    }

    async function openNode(row) {
      const node = await api('/api/node', { id: row.id });
      el('detailBadge').textContent = node.node_eid;
      const lines = (node.lines || []).map(line => (
        `<tr><td>${line.line_no}</td><td>${escapeHtml(line.origin_version_label)}</td><td>${escapeHtml(line.text)}</td></tr>`
      )).join('');
      const history = (node.history || []).map(item => (
        `<tr><td>${item.revision_id}</td><td>${escapeHtml(item.to_label)}</td><td>${escapeHtml(item.change_type)}</td></tr>`
      )).join('');
      el('detail').innerHTML = `
        <div class="detail-grid">
          <div class="muted">Work</div><div>${escapeHtml(node.work_id)}</div>
          <div class="muted">Version</div><div>${escapeHtml(node.version_label)}</div>
          <div class="muted">Type</div><div>${escapeHtml(node.node_type)}</div>
          <div class="muted">Number</div><div>${escapeHtml(node.num || '')}</div>
          <div class="muted">Heading</div><div>${escapeHtml(node.heading || '')}</div>
          <div class="muted">Hash</div><div class="mono">${escapeHtml(node.text_hash || '')}</div>
        </div>
        <h3>Text</h3>
        <pre>${escapeHtml(node.text || '')}</pre>
        <h3 style="margin-top:12px">Blame</h3>
        <table><thead><tr><th>Line</th><th>Origin</th><th>Text</th></tr></thead><tbody>${lines}</tbody></table>
        <h3 style="margin-top:12px">History</h3>
        <table><thead><tr><th>Revision</th><th>To</th><th>Type</th></tr></thead><tbody>${history}</tbody></table>
      `;
    }

    async function renderSearch() {
      el('listTitle').textContent = 'Search';
      const q = state.searchQuery.trim();
      if (!q) {
        el('list').innerHTML = '<div class="muted">Enter a query.</div>';
        return;
      }
      const payload = await api('/api/search', {
        q,
        work_id: state.selectedWork && state.selectedWork.id,
        expression_id: state.selectedExpression && state.selectedExpression.id,
      });
      el('list').innerHTML = table([
        { label: 'Version', value: row => row.version_label },
        { label: 'Type', value: row => row.node_type },
        { label: 'Num', value: row => row.num || '' },
        { label: 'eId', value: row => row.node_eid },
        { label: 'Preview', value: row => row.preview || '' },
      ], payload.results, openNode);
    }

    async function renderRevisions() {
      el('listTitle').textContent = 'Revisions';
      el('detail').innerHTML = '<div class="muted">Select a revision.</div>';
      if (!state.selectedWork) {
        el('list').innerHTML = '<div class="muted">No work selected.</div>';
        return;
      }
      const payload = await api('/api/revisions', { work_id: state.selectedWork.id });
      el('list').innerHTML = table([
        { label: 'ID', value: row => row.id },
        { label: 'From', value: row => row.from_label || '(initial)' },
        { label: 'To', value: row => row.to_label },
        { label: '+', value: row => row.nodes_added },
        { label: '-', value: row => row.nodes_removed },
        { label: '~', value: row => row.nodes_modified },
        { label: '=', value: row => row.nodes_unchanged },
      ], payload.revisions, openRevision);
    }

    async function openRevision(row) {
      const payload = await api('/api/changes', { revision_id: row.id, limit: 1000 });
      const rows = payload.changes.map(change => (
        `<tr data-change-id="${change.id}" data-clickable="true"><td>${change.id}</td><td>${escapeHtml(change.change_type)}</td><td>${escapeHtml(change.node_type || '')}</td><td>${escapeHtml(change.node_eid || '')}</td><td>${escapeHtml(change.heading || '')}</td></tr>`
      )).join('');
      el('detailBadge').textContent = `Revision ${row.id}`;
      el('detail').innerHTML = `<table><thead><tr><th>ID</th><th>Type</th><th>Node</th><th>eId</th><th>Heading</th></tr></thead><tbody>${rows}</tbody></table><pre id="changeDiff" style="margin-top:12px"></pre>`;
      document.querySelectorAll('[data-change-id]').forEach(tr => {
        tr.addEventListener('click', async () => {
          const change = await api('/api/change', { id: tr.dataset.changeId });
          el('changeDiff').textContent = change.text_diff || change.diff_preview || '';
        });
      });
    }

    async function renderXml() {
      el('listTitle').textContent = 'AKN XML';
      if (!state.selectedExpression) {
        el('list').innerHTML = '<div class="muted">No expression selected.</div>';
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
