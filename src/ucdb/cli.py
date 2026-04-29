"""Click-based command-line interface."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import click
from dotenv import find_dotenv, load_dotenv
from rich.console import Console
from rich.table import Table

from . import db
from .ai import AIConfig
from .exporters import (
    export_expression_json,
    export_html,
    export_markdown,
    export_rag_jsonl,
)
from .process import (
    ProcessResult,
    import_akn_file,
    process_document,
    process_repository,
)
from .scan import FoundDocument, scan_repository

load_dotenv(find_dotenv(usecwd=True))

DEFAULT_DB = "ucdb.sqlite3"
console = Console()


def _db_path(ctx: click.Context) -> Path:
    return Path(ctx.obj["db"])


def _ai_config(model: str | None) -> AIConfig:
    cfg = AIConfig.from_env()
    if model:
        cfg.model = model
    return cfg


def _load_env_file(
    ctx: click.Context, param: click.Parameter, value: str | None
) -> str | None:
    if value:
        path = Path(value)
        if not path.is_file():
            raise click.BadParameter(f"{value!r} is not a readable file")
        load_dotenv(dotenv_path=path, override=True)
    return value


@click.group(help="Universal Code Database — Akoma Ntoso legal data in SQLite.")
@click.option(
    "--env-file",
    type=click.Path(dir_okay=False, path_type=str),
    default=None,
    is_eager=True,
    expose_value=False,
    callback=_load_env_file,
    help="Load environment variables from this file.",
)
@click.option(
    "--db",
    "db_path",
    default=lambda: os.environ.get("UCDB_DB", DEFAULT_DB),
    show_default=DEFAULT_DB,
    help="Path to the SQLite database (env: UCDB_DB).",
)
@click.version_option(package_name="ucdb", prog_name="ucdb")
@click.pass_context
def main(ctx: click.Context, db_path: str) -> None:
    ctx.ensure_object(dict)
    ctx.obj["db"] = db_path


@main.command("init", help="Initialize a UCDB 0.2 database.")
@click.option("--force", is_flag=True, help="Recreate even if the file exists.")
@click.pass_context
def init_cmd(ctx: click.Context, force: bool) -> None:
    path = _db_path(ctx)
    if path.exists() and force:
        path.unlink()
    db.init_db(path)
    console.print(f"[green]Initialized[/green] database at [bold]{path}[/bold]")


@main.command("scan", help="List documents found in an input repository.")
@click.argument("root", type=click.Path(exists=True, file_okay=False, path_type=Path))
def scan_cmd(root: Path) -> None:
    table = Table(title=f"Documents under {root}")
    table.add_column("work-id")
    table.add_column("version")
    table.add_column("file")
    count = 0
    for doc in scan_repository(root):
        table.add_row(doc.work_id, doc.version_label, str(doc.path))
        count += 1
    console.print(table)
    console.print(f"[bold]{count}[/bold] document(s) found")


@main.command("process", help="Run the full AI pipeline over an input repository.")
@click.argument("root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--model", default=None, help="Override the model (env: UCDB_MODEL).")
@click.option("--language", default="zho", show_default=True)
@click.option("--reprocess", is_flag=True)
@click.option("--no-schema", is_flag=True, help="Skip XSD validation.")
@click.pass_context
def process_cmd(
    ctx: click.Context,
    root: Path,
    model: str | None,
    language: str,
    reprocess: bool,
    no_schema: bool,
) -> None:
    cfg = _ai_config(model)
    if not cfg.api_key:
        raise click.UsageError("OPENAI_API_KEY is not set.")
    with db.connect(_db_path(ctx)) as conn:
        results = process_repository(
            conn,
            root,
            ai_config=cfg,
            language=language,
            skip_existing=not reprocess,
            validate_schema=not no_schema,
            progress=_print_progress,
        )
    _summarize(results)


@main.command("process-one", help="Process a single document file.")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--work-id", required=True)
@click.option("--version", "version_label", required=True)
@click.option("--language", default="zho", show_default=True)
@click.option("--model", default=None)
@click.option("--reprocess", is_flag=True)
@click.option("--no-schema", is_flag=True)
@click.pass_context
def process_one_cmd(
    ctx: click.Context,
    path: Path,
    work_id: str,
    version_label: str,
    language: str,
    model: str | None,
    reprocess: bool,
    no_schema: bool,
) -> None:
    cfg = _ai_config(model)
    if not cfg.api_key:
        raise click.UsageError("OPENAI_API_KEY is not set.")
    doc = FoundDocument(work_id=work_id, version_label=version_label, path=path)
    with db.connect(_db_path(ctx)) as conn:
        result = process_document(
            conn,
            doc,
            ai_config=cfg,
            language=language,
            skip_existing=not reprocess,
            validate_schema=not no_schema,
        )
    _summarize([result])


@main.command("import-akn", help="Import a pre-generated Akoma Ntoso XML file.")
@click.argument(
    "xml_path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option("--work-id", required=True)
@click.option("--version", "version_label", required=True)
@click.option("--language", default="zho", show_default=True)
@click.option("--title", default=None)
@click.option("--document-class", default="law", show_default=True)
@click.option("--jurisdiction", default="tw", show_default=True)
@click.option(
    "--source",
    "source_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
)
@click.option("--no-schema", is_flag=True)
@click.pass_context
def import_akn_cmd(
    ctx: click.Context,
    xml_path: Path,
    work_id: str,
    version_label: str,
    language: str,
    title: str | None,
    document_class: str,
    jurisdiction: str,
    source_path: Path | None,
    no_schema: bool,
) -> None:
    with db.connect(_db_path(ctx)) as conn:
        result = import_akn_file(
            conn,
            work_id=work_id,
            version_label=version_label,
            xml_path=xml_path,
            source_path=source_path,
            language=language,
            title=title,
            document_class=document_class,
            jurisdiction=jurisdiction,
            validate_schema=not no_schema,
        )
    _summarize([result])


@main.group("query", help="Inspect data stored in the database.")
def query_group() -> None:
    pass


@main.group("export", help="Export derived artifacts from canonical storage.")
def export_group() -> None:
    pass


@export_group.command("json", help="Export one expression as normalized JSON.")
@click.argument("expression_id", type=int)
@click.pass_context
def export_json_cmd(ctx: click.Context, expression_id: int) -> None:
    with db.connect(_db_path(ctx)) as conn:
        click.echo(export_expression_json(conn, expression_id))


@export_group.command("rag", help="Export one expression as RAG JSONL chunks.")
@click.argument("expression_id", type=int)
@click.pass_context
def export_rag_cmd(ctx: click.Context, expression_id: int) -> None:
    with db.connect(_db_path(ctx)) as conn:
        click.echo(export_rag_jsonl(conn, expression_id), nl=False)


@export_group.command("markdown", help="Export one expression as Markdown.")
@click.argument("expression_id", type=int)
@click.pass_context
def export_markdown_cmd(ctx: click.Context, expression_id: int) -> None:
    with db.connect(_db_path(ctx)) as conn:
        click.echo(export_markdown(conn, expression_id), nl=False)


@export_group.command("html", help="Export one expression as HTML.")
@click.argument("expression_id", type=int)
@click.pass_context
def export_html_cmd(ctx: click.Context, expression_id: int) -> None:
    with db.connect(_db_path(ctx)) as conn:
        click.echo(export_html(conn, expression_id), nl=False)


@query_group.command("works", help="List legal works.")
@click.pass_context
def query_works(ctx: click.Context) -> None:
    with db.connect(_db_path(ctx)) as conn:
        rows = db.list_works(conn)
    table = Table(title="Works")
    table.add_column("id")
    table.add_column("class")
    table.add_column("title")
    table.add_column("updated")
    for row in rows:
        table.add_row(
            row["id"], row["document_class"], row["title"] or "", row["updated_at"]
        )
    console.print(table)


@query_group.command("expressions", help="List expressions of a work.")
@click.argument("work_id")
@click.pass_context
def query_expressions(ctx: click.Context, work_id: str) -> None:
    with db.connect(_db_path(ctx)) as conn:
        rows = db.list_expressions(conn, work_id)
    table = Table(title=f"Expressions of {work_id}")
    table.add_column("id", justify="right")
    table.add_column("version")
    table.add_column("lang")
    table.add_column("status")
    table.add_column("hash")
    table.add_column("parent", justify="right")
    table.add_column("processed")
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["version_label"],
            row["language"],
            row["status"],
            (row["canonical_hash"] or "")[:16],
            str(row["parent_expression_id"])
            if row["parent_expression_id"] is not None
            else "",
            row["processed_at"] or "",
        )
    console.print(table)


@query_group.command("nodes", help="List nodes of an expression.")
@click.argument("expression_id", type=int)
@click.option("--limit", default=200, show_default=True)
@click.pass_context
def query_nodes(ctx: click.Context, expression_id: int, limit: int) -> None:
    with db.connect(_db_path(ctx)) as conn:
        rows = db.list_nodes(conn, expression_id)[:limit]
    table = Table(title=f"Nodes of expression {expression_id}")
    table.add_column("id", justify="right")
    table.add_column("type")
    table.add_column("num")
    table.add_column("heading")
    table.add_column("eId")
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["node_type"],
            row["num"] or "",
            (row["heading"] or "")[:80],
            row["node_eid"],
        )
    console.print(table)


@query_group.command("node", help="Show one node.")
@click.argument("node_id", type=int)
@click.option("--xml", "show_xml", is_flag=True)
@click.pass_context
def query_node(ctx: click.Context, node_id: int, show_xml: bool) -> None:
    with db.connect(_db_path(ctx)) as conn:
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if row is None:
        raise click.ClickException(f"node {node_id} not found")
    console.print(
        f"[bold]{row['node_type']}[/bold] {row['num'] or ''} {row['heading'] or ''}"
    )
    console.print(f"[dim]eId:[/dim] {row['node_eid']}\n")
    console.print(row["xml_fragment"] if show_xml else (row["text"] or ""))


@query_group.command("search", help="Full-text search across nodes.")
@click.argument("text")
@click.option("--work-id", default=None)
@click.option("--limit", default=50, show_default=True)
@click.option("--raw", is_flag=True)
@click.pass_context
def query_search(
    ctx: click.Context, text: str, work_id: str | None, limit: int, raw: bool
) -> None:
    with db.connect(_db_path(ctx)) as conn:
        try:
            rows = db.search_nodes(conn, text, work_id=work_id, limit=limit, raw=raw)
        except sqlite3.OperationalError as exc:
            raise click.ClickException(f"FTS query error: {exc}") from exc
    table = Table(title=f"Search: {text!r}")
    table.add_column("node id", justify="right")
    table.add_column("work")
    table.add_column("version")
    table.add_column("type")
    table.add_column("heading")
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["work_id"],
            row["version_label"],
            row["node_type"],
            (row["heading"] or "")[:80],
        )
    console.print(table)


@query_group.command("akn", help="Dump the stored Akoma Ntoso XML for an expression.")
@click.argument("expression_id", type=int)
@click.pass_context
def query_akn(ctx: click.Context, expression_id: int) -> None:
    with db.connect(_db_path(ctx)) as conn:
        row = db.get_expression(conn, expression_id)
    if row is None:
        raise click.ClickException(f"expression {expression_id} not found")
    if not row["canonical_xml"]:
        raise click.ClickException(
            f"no canonical XML stored for expression {expression_id}"
        )
    click.echo(row["canonical_xml"])


@query_group.command("revisions", help="List revisions of a work.")
@click.argument("work_id")
@click.pass_context
def query_revisions(ctx: click.Context, work_id: str) -> None:
    with db.connect(_db_path(ctx)) as conn:
        rows = db.list_revisions(conn, work_id)
    table = Table(title=f"Revisions of {work_id}")
    table.add_column("rev id", justify="right")
    table.add_column("from")
    table.add_column("to")
    table.add_column("+", justify="right")
    table.add_column("-", justify="right")
    table.add_column("~", justify="right")
    table.add_column("=", justify="right")
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["from_label"] or "(initial)",
            row["to_label"],
            str(row["nodes_added"]),
            str(row["nodes_removed"]),
            str(row["nodes_modified"]),
            str(row["nodes_unchanged"]),
        )
    console.print(table)


@query_group.command("revision", help="Show node changes inside a revision.")
@click.argument("revision_id", type=int)
@click.option(
    "--type",
    "change_type",
    type=click.Choice(["added", "removed", "modified"]),
    default=None,
)
@click.pass_context
def query_revision(
    ctx: click.Context, revision_id: int, change_type: str | None
) -> None:
    with db.connect(_db_path(ctx)) as conn:
        revision = db.get_revision(conn, revision_id)
        if revision is None:
            raise click.ClickException(f"revision {revision_id} not found")
        rows = db.list_node_changes(conn, revision_id, change_type=change_type)
    table = Table(title=f"Revision {revision_id}")
    table.add_column("change id", justify="right")
    table.add_column("type")
    table.add_column("node")
    table.add_column("num")
    table.add_column("eId")
    table.add_column("heading")
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["change_type"],
            row["node_type"] or "",
            row["num"] or "",
            row["node_eid"] or "",
            (row["heading"] or "")[:60],
        )
    console.print(table)


@query_group.command("diff", help="Show the text diff for a single node change.")
@click.argument("change_id", type=int)
@click.pass_context
def query_diff(ctx: click.Context, change_id: int) -> None:
    with db.connect(_db_path(ctx)) as conn:
        row = db.get_node_change(conn, change_id)
    if row is None:
        raise click.ClickException(f"node change {change_id} not found")
    click.echo(row["text_diff"] or "")


@query_group.command("diff-expressions", help="Diff any two expressions.")
@click.argument("work_id")
@click.option("--from", "from_label", required=True)
@click.option("--to", "to_label", required=True)
@click.option("--language", default="zho", show_default=True)
@click.option("--node-eid", default=None)
@click.option("--json-output", is_flag=True)
@click.pass_context
def query_diff_expressions(
    ctx: click.Context,
    work_id: str,
    from_label: str,
    to_label: str,
    language: str,
    node_eid: str | None,
    json_output: bool,
) -> None:
    with db.connect(_db_path(ctx)) as conn:
        from_expr = _resolve_expression(conn, work_id, from_label, language)
        to_expr = _resolve_expression(conn, work_id, to_label, language)
        changes, stats = db.diff_expressions(
            conn,
            work_id=work_id,
            from_expression_id=int(from_expr["id"]),
            to_expression_id=int(to_expr["id"]),
            node_eid=node_eid,
        )
    if json_output:
        click.echo(
            json.dumps([c.__dict__ for c in changes], ensure_ascii=False, indent=2)
        )
        return
    console.print(
        f"[bold]{work_id}[/bold] {from_label} → {to_label} +{stats.added} -{stats.removed} ~{stats.modified} ={stats.unchanged}"
    )
    table = Table()
    table.add_column("type")
    table.add_column("node")
    table.add_column("num")
    table.add_column("eId")
    table.add_column("heading")
    for change in changes:
        table.add_row(
            change.change_type,
            change.node_type,
            change.num or "",
            change.node_eid or "",
            (change.heading or "")[:60],
        )
    console.print(table)


@query_group.command("blame", help="Show line provenance for a node eId.")
@click.argument("work_id")
@click.argument("node_eid")
@click.option("--version", "version_label", default=None)
@click.option("--language", default="zho", show_default=True)
@click.pass_context
def query_blame(
    ctx: click.Context,
    work_id: str,
    node_eid: str,
    version_label: str | None,
    language: str,
) -> None:
    with db.connect(_db_path(ctx)) as conn:
        expr = (
            _resolve_expression(conn, work_id, version_label, language)
            if version_label
            else db.latest_expression(conn, work_id, language)
        )
        if expr is None:
            raise click.ClickException(f"no imported expression of {work_id!r} found")
        node = db.find_node_by_eid(conn, int(expr["id"]), node_eid)
        if node is None:
            raise click.ClickException(f"node {node_eid!r} not found")
        lines = db.get_node_lines(conn, int(node["id"]))
    table = Table(title=f"{node_eid} @ {work_id}/{expr['version_label']}")
    table.add_column("line", justify="right")
    table.add_column("origin")
    table.add_column("text")
    for row in lines:
        table.add_row(str(row["line_no"]), row["origin_version_label"], row["text"])
    console.print(table)


@query_group.command("history", help="List every revision that touched a node eId.")
@click.argument("work_id")
@click.argument("node_eid")
@click.pass_context
def query_history(ctx: click.Context, work_id: str, node_eid: str) -> None:
    with db.connect(_db_path(ctx)) as conn:
        rows = db.node_history(conn, work_id, node_eid)
    table = Table(title=f"History of {node_eid} in {work_id}")
    table.add_column("rev id", justify="right")
    table.add_column("from")
    table.add_column("to")
    table.add_column("type")
    table.add_column("change id", justify="right")
    for row in rows:
        table.add_row(
            str(row["revision_id"]),
            row["from_label"] or "(initial)",
            row["to_label"],
            row["change_type"],
            str(row["change_id"]),
        )
    console.print(table)


@query_group.command("log", help="Show recent processing log entries.")
@click.option("--work-id", default=None)
@click.option("--expression-id", type=int, default=None)
@click.option("--limit", default=50, show_default=True)
@click.pass_context
def query_log(
    ctx: click.Context, work_id: str | None, expression_id: int | None, limit: int
) -> None:
    with db.connect(_db_path(ctx)) as conn:
        rows = db.list_processing_log(
            conn, work_id=work_id, expression_id=expression_id, limit=limit
        )
    table = Table(title="Processing log")
    table.add_column("when")
    table.add_column("work")
    table.add_column("expr", justify="right")
    table.add_column("step")
    table.add_column("status")
    table.add_column("message")
    for row in rows:
        table.add_row(
            row["created_at"],
            row["work_id"] or "",
            str(row["expression_id"] or ""),
            row["step"],
            row["status"],
            (row["message"] or "")[:60],
        )
    console.print(table)


@main.command("serve", help="Run a read-only web UI for browsing the database.")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True, type=int)
@click.option("--open", "open_browser", is_flag=True)
@click.pass_context
def serve_cmd(ctx: click.Context, host: str, port: int, open_browser: bool) -> None:
    from .web import serve

    serve(_db_path(ctx), host=host, port=port, open_browser=open_browser)


def _resolve_expression(
    conn: sqlite3.Connection, work_id: str, label: str | None, language: str
) -> sqlite3.Row:
    if label is None:
        row = db.latest_expression(conn, work_id, language)
    else:
        row = db.find_expression_by_label(conn, work_id, label, language)
    if row is None:
        raise click.ClickException(f"expression {label!r} of {work_id!r} not found")
    return row


def _print_progress(phase: str, result: ProcessResult) -> None:
    doc = result.document
    if phase == "start":
        console.print(
            f"[cyan]→[/cyan] {doc.work_id}/{doc.version_label}/{doc.path.name}"
        )
        return
    color = {"imported": "green", "skipped": "yellow", "failed": "red"}.get(
        result.status, "white"
    )
    if result.status == "imported":
        extra = f" nodes={result.nodes}"
        if result.revision is not None:
            r = result.revision
            extra += f" rev=+{r.added}/-{r.removed}/~{r.modified}/={r.unchanged}"
    else:
        extra = f" {result.message or ''}"
    console.print(
        f"  [{color}]{result.status}[/{color}] {doc.work_id}/{doc.version_label}{extra}"
    )


def _summarize(results: list[ProcessResult]) -> None:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    console.print(
        "\n[bold]Summary:[/bold] "
        + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no documents")
    )
    failed = [result for result in results if result.status == "failed"]
    if failed:
        console.print("[red]Failures:[/red]")
        for result in failed:
            console.print(f"  - {result.document.relative_key}: {result.message}")
    if os.environ.get("UCDB_JSON"):
        click.echo(
            json.dumps(
                [
                    {
                        "work_id": r.document.work_id,
                        "version": r.document.version_label,
                        "path": str(r.document.path),
                        "status": r.status,
                        "expression_id": r.expression_id,
                        "nodes": r.nodes,
                        "message": r.message,
                    }
                    for r in results
                ],
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
