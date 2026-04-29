"""Derived export renderers for UCDB expressions."""

from __future__ import annotations

import json
import sqlite3
from html import escape

from . import db


def export_expression_json(conn: sqlite3.Connection, expression_id: int) -> str:
    expression = db.get_expression(conn, expression_id)
    if expression is None:
        raise ValueError(f"expression {expression_id} not found")
    nodes = db.list_nodes(conn, expression_id)
    payload = {
        "expression": _row(expression),
        "nodes": [_row(node) for node in nodes],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def export_rag_jsonl(conn: sqlite3.Connection, expression_id: int) -> str:
    expression = db.get_expression(conn, expression_id)
    if expression is None:
        raise ValueError(f"expression {expression_id} not found")
    nodes = [
        node
        for node in db.list_nodes(conn, expression_id)
        if (node["text"] or "").strip()
    ]
    lines: list[str] = []
    for node in nodes:
        payload = {
            "id": f"{expression['work_id']}:{expression['version_label']}:{node['node_eid']}",
            "text": node["text"],
            "citation": _citation(expression, node),
            "metadata": {
                "work_id": expression["work_id"],
                "expression_id": expression["id"],
                "version_label": expression["version_label"],
                "language": expression["language"],
                "node_id": node["id"],
                "node_eid": node["node_eid"],
                "node_type": node["node_type"],
                "num": node["num"],
                "heading": node["heading"],
                "source_path": expression["source_path"],
                "source_url": expression["source_url"],
                "source_hash": expression["source_hash"],
                "canonical_hash": expression["canonical_hash"],
                "text_hash": node["text_hash"],
                "normalized_text_hash": node["normalized_text_hash"],
            },
        }
        lines.append(json.dumps(payload, ensure_ascii=False))
    return "\n".join(lines) + ("\n" if lines else "")


def export_markdown(conn: sqlite3.Connection, expression_id: int) -> str:
    expression = db.get_expression(conn, expression_id)
    if expression is None:
        raise ValueError(f"expression {expression_id} not found")
    nodes = db.list_nodes(conn, expression_id)
    parts = [f"# {expression['work_id']} {expression['version_label']}", ""]
    for node in nodes:
        level = min(int(node["depth"]) + 2, 6)
        title = " ".join(
            part for part in [node["num"], node["heading"] or node["node_eid"]] if part
        )
        parts.append("#" * level + " " + title)
        if node["text"]:
            parts.append("")
            parts.append(node["text"])
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def export_html(conn: sqlite3.Connection, expression_id: int) -> str:
    expression = db.get_expression(conn, expression_id)
    if expression is None:
        raise ValueError(f"expression {expression_id} not found")
    nodes = db.list_nodes(conn, expression_id)
    parts = [
        "<!doctype html>",
        '<html lang="zh-Hant">',
        "<head>",
        '  <meta charset="utf-8">',
        f"  <title>{escape(expression['work_id'])} {escape(expression['version_label'])}</title>",
        "</head>",
        "<body>",
        f"  <h1>{escape(expression['work_id'])} {escape(expression['version_label'])}</h1>",
    ]
    for node in nodes:
        level = min(int(node["depth"]) + 2, 6)
        title = " ".join(
            part for part in [node["num"], node["heading"] or node["node_eid"]] if part
        )
        parts.append(f'  <section id="{escape(node["node_eid"])}">')
        parts.append(f"    <h{level}>{escape(title)}</h{level}>")
        if node["text"]:
            for paragraph in node["text"].splitlines():
                if paragraph.strip():
                    parts.append(f"    <p>{escape(paragraph)}</p>")
        parts.append("  </section>")
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts)


def _citation(expression: sqlite3.Row, node: sqlite3.Row) -> str:
    parts = [
        expression["work_id"],
        expression["version_label"],
        node["num"] or node["node_eid"],
        node["heading"],
    ]
    return " ".join(str(part) for part in parts if part)


def _row(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}
