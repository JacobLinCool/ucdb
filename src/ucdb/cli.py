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
from .process import (
    ProcessResult,
    import_xml_file,
    process_document,
    process_repository,
)
from .scan import scan_repository

# Load .env from the current working directory (or any parent), without
# overriding values already present in the real environment. This must run
# before Click resolves option defaults that read os.environ.
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


@click.group(help="Universal Code Database — convert legal documents into SQLite.")
@click.option(
    "--env-file",
    type=click.Path(dir_okay=False, path_type=str),
    default=None,
    is_eager=True,
    expose_value=False,
    callback=_load_env_file,
    help="Load environment variables from this file (overrides existing values). "
    "By default, a .env in the current directory or any parent is loaded "
    "without overriding the real environment.",
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


@main.command("init", help="Initialize a new database file.")
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
    table.add_column("code-id")
    table.add_column("version")
    table.add_column("file")
    count = 0
    for doc in scan_repository(root):
        table.add_row(doc.code_id, doc.version_label, str(doc.path))
        count += 1
    console.print(table)
    console.print(f"[bold]{count}[/bold] document(s) found")


@main.command("process", help="Run the full AI pipeline over an input repository.")
@click.argument("root", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--model", default=None, help="Override the model (env: UCDB_MODEL).")
@click.option(
    "--reprocess",
    is_flag=True,
    help="Re-process even if a matching source hash is already imported.",
)
@click.option(
    "--no-schema",
    is_flag=True,
    help="Skip XSD validation (well-formedness is always checked).",
)
@click.pass_context
def process_cmd(
    ctx: click.Context,
    root: Path,
    model: str | None,
    reprocess: bool,
    no_schema: bool,
) -> None:
    cfg = _ai_config(model)
    if not cfg.api_key:
        raise click.UsageError(
            "OPENAI_API_KEY is not set. Configure your AI backend before "
            "running `process` (use OPENAI_BASE_URL for compatible endpoints)."
        )
    with db.connect(_db_path(ctx)) as conn:
        results = process_repository(
            conn,
            root,
            ai_config=cfg,
            skip_existing=not reprocess,
            validate_schema=not no_schema,
            progress=lambda phase, r: _print_progress(phase, r),
        )
    _summarize(results)


@main.command("process-one", help="Process a single document file.")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--code-id", required=True, help="Code identifier (slug).")
@click.option(
    "--version", "version_label", required=True, help="Version label or date."
)
@click.option("--model", default=None)
@click.option("--reprocess", is_flag=True)
@click.option("--no-schema", is_flag=True)
@click.pass_context
def process_one_cmd(
    ctx: click.Context,
    path: Path,
    code_id: str,
    version_label: str,
    model: str | None,
    reprocess: bool,
    no_schema: bool,
) -> None:
    from .scan import FoundDocument

    cfg = _ai_config(model)
    if not cfg.api_key:
        raise click.UsageError("OPENAI_API_KEY is not set.")
    document = FoundDocument(code_id=code_id, version_label=version_label, path=path)
    with db.connect(_db_path(ctx)) as conn:
        result = process_document(
            conn,
            document,
            ai_config=cfg,
            skip_existing=not reprocess,
            validate_schema=not no_schema,
        )
    _summarize([result])


@main.command("import", help="Import a pre-generated USLM XML file.")
@click.argument(
    "xml_path", type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option("--code-id", required=True)
@click.option("--version", "version_label", required=True)
@click.option(
    "--source",
    "source_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Original source document for hashing (defaults to the XML file).",
)
@click.option("--no-schema", is_flag=True)
@click.pass_context
def import_cmd(
    ctx: click.Context,
    xml_path: Path,
    code_id: str,
    version_label: str,
    source_path: Path | None,
    no_schema: bool,
) -> None:
    with db.connect(_db_path(ctx)) as conn:
        result = import_xml_file(
            conn,
            code_id=code_id,
            version_label=version_label,
            xml_path=xml_path,
            source_path=source_path,
            validate_schema=not no_schema,
        )
    _summarize([result])


@main.command("serve", help="Run a read-only web UI for browsing the database.")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True, type=int)
@click.option("--open", "open_browser", is_flag=True, help="Open the browser.")
@click.pass_context
def serve_cmd(
    ctx: click.Context,
    host: str,
    port: int,
    open_browser: bool,
) -> None:
    from .web import serve

    serve(_db_path(ctx), host=host, port=port, open_browser=open_browser)


@main.group("query", help="Inspect data stored in the database.")
def query_group() -> None:
    pass


@query_group.command("codes", help="List every legislative code.")
@click.pass_context
def query_codes(ctx: click.Context) -> None:
    with db.connect(_db_path(ctx)) as conn:
        rows = db.list_codes(conn)
    table = Table(title="Codes")
    table.add_column("id")
    table.add_column("title")
    table.add_column("created")
    table.add_column("updated")
    for row in rows:
        table.add_row(
            row["id"], row["title"] or "", row["created_at"], row["updated_at"]
        )
    console.print(table)


@query_group.command("versions", help="List versions of a code.")
@click.argument("code_id")
@click.pass_context
def query_versions(ctx: click.Context, code_id: str) -> None:
    with db.connect(_db_path(ctx)) as conn:
        rows = db.list_versions(conn, code_id)
    table = Table(title=f"Versions of {code_id}")
    table.add_column("id", justify="right")
    table.add_column("version")
    table.add_column("status")
    table.add_column("source hash")
    table.add_column("xml hash")
    table.add_column("ai")
    table.add_column("validation")
    table.add_column("parent", justify="right")
    table.add_column("processed")
    for row in rows:
        ai = row["ai_provider"] or ""
        if row["ai_model"]:
            ai = f"{ai}/{row['ai_model']}" if ai else row["ai_model"]
        table.add_row(
            str(row["id"]),
            row["version_label"],
            row["status"],
            (row["source_hash"] or "")[:16],
            (row["xml_hash"] or "")[:16],
            ai,
            row["validation_status"] or "",
            str(row["parent_version_id"])
            if row["parent_version_id"] is not None
            else "",
            row["processed_at"] or "",
        )
    console.print(table)


@query_group.command("sections", help="List sections of a version.")
@click.argument("version_id", type=int)
@click.option("--limit", default=200, show_default=True)
@click.pass_context
def query_sections(ctx: click.Context, version_id: int, limit: int) -> None:
    with db.connect(_db_path(ctx)) as conn:
        rows = db.list_sections(conn, version_id)[:limit]
    table = Table(title=f"Sections of version {version_id}")
    table.add_column("id", justify="right")
    table.add_column("level")
    table.add_column("num")
    table.add_column("heading")
    table.add_column("identifier")
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["level"],
            row["num"] or "",
            (row["heading"] or "")[:80],
            row["identifier"] or "",
        )
    console.print(table)


@query_group.command("section", help="Show a single section's full content.")
@click.argument("section_id", type=int)
@click.option(
    "--xml", "show_xml", is_flag=True, help="Print the XML fragment instead of text."
)
@click.pass_context
def query_section(ctx: click.Context, section_id: int, show_xml: bool) -> None:
    with db.connect(_db_path(ctx)) as conn:
        row = conn.execute(
            "SELECT * FROM sections WHERE id = ?", (section_id,)
        ).fetchone()
    if not row:
        raise click.ClickException(f"section {section_id} not found")
    console.print(
        f"[bold]{row['level']}[/bold] {row['num'] or ''} {row['heading'] or ''}"
    )
    if row["identifier"]:
        console.print(f"[dim]identifier:[/dim] {row['identifier']}")
    console.print()
    console.print(row["xml_fragment"] if show_xml else (row["content"] or ""))


@query_group.command(
    "search",
    help=(
        "Full-text search across sections (FTS5). "
        "Pass --raw to use native FTS5 query syntax (e.g. 'tax AND income*')."
    ),
)
@click.argument("text")
@click.option("--code-id", default=None)
@click.option("--limit", default=50, show_default=True)
@click.option(
    "--raw",
    is_flag=True,
    default=False,
    help="Forward TEXT as raw FTS5 query syntax instead of treating it as a phrase.",
)
@click.pass_context
def query_search(
    ctx: click.Context, text: str, code_id: str | None, limit: int, raw: bool
) -> None:
    with db.connect(_db_path(ctx)) as conn:
        try:
            rows = db.search_sections(conn, text, code_id=code_id, limit=limit, raw=raw)
        except sqlite3.OperationalError as exc:
            raise click.ClickException(f"FTS query error: {exc}") from exc
    table = Table(title=f"Search: {text!r}")
    table.add_column("section id", justify="right")
    table.add_column("code")
    table.add_column("version")
    table.add_column("level")
    table.add_column("heading")
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["code_id"],
            row["version_label"],
            row["level"],
            (row["heading"] or "")[:80],
        )
    console.print(table)


@query_group.command("log", help="Show recent processing log entries.")
@click.option("--code-id", default=None)
@click.option("--version-id", type=int, default=None)
@click.option("--limit", default=50, show_default=True)
@click.pass_context
def query_log(
    ctx: click.Context,
    code_id: str | None,
    version_id: int | None,
    limit: int,
) -> None:
    with db.connect(_db_path(ctx)) as conn:
        rows = db.list_processing_log(
            conn, code_id=code_id, version_id=version_id, limit=limit
        )
    table = Table(title="Processing log")
    table.add_column("when")
    table.add_column("code")
    table.add_column("version", justify="right")
    table.add_column("step")
    table.add_column("status")
    table.add_column("message")
    for row in rows:
        table.add_row(
            row["created_at"],
            row["code_id"] or "",
            str(row["version_id"]) if row["version_id"] is not None else "",
            row["step"],
            row["status"],
            (row["message"] or "")[:60],
        )
    console.print(table)


@query_group.command("xml", help="Dump the stored USLM XML for a version.")
@click.argument("version_id", type=int)
@click.pass_context
def query_xml(ctx: click.Context, version_id: int) -> None:
    with db.connect(_db_path(ctx)) as conn:
        row = db.get_version(conn, version_id)
    if not row:
        raise click.ClickException(f"version {version_id} not found")
    if not row["xml_content"]:
        raise click.ClickException(f"no XML stored for version {version_id}")
    click.echo(row["xml_content"])


@query_group.command("revisions", help="List version-to-version revisions of a code.")
@click.argument("code_id")
@click.pass_context
def query_revisions(ctx: click.Context, code_id: str) -> None:
    with db.connect(_db_path(ctx)) as conn:
        rows = db.list_revisions(conn, code_id)
    table = Table(title=f"Revisions of {code_id}")
    table.add_column("rev id", justify="right")
    table.add_column("from")
    table.add_column("to")
    table.add_column("+", justify="right")
    table.add_column("-", justify="right")
    table.add_column("~", justify="right")
    table.add_column("=", justify="right")
    table.add_column("created")
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["from_label"] or "(initial)",
            row["to_label"],
            str(row["sections_added"]),
            str(row["sections_removed"]),
            str(row["sections_modified"]),
            str(row["sections_unchanged"]),
            row["created_at"],
        )
    console.print(table)


@query_group.command(
    "revision", help="Show the section-level changes inside a revision."
)
@click.argument("revision_id", type=int)
@click.option(
    "--type",
    "change_type",
    type=click.Choice(["added", "removed", "modified"]),
    default=None,
    help="Filter by change type.",
)
@click.option("--limit", default=200, show_default=True)
@click.pass_context
def query_revision(
    ctx: click.Context,
    revision_id: int,
    change_type: str | None,
    limit: int,
) -> None:
    with db.connect(_db_path(ctx)) as conn:
        revision = db.get_revision(conn, revision_id)
        if not revision:
            raise click.ClickException(f"revision {revision_id} not found")
        rows = db.list_section_changes(conn, revision_id, change_type=change_type)[
            :limit
        ]
    console.print(
        f"[bold]Revision {revision_id}[/bold] "
        f"{revision['from_label'] or '(initial)'} → {revision['to_label']}  "
        f"[green]+{revision['sections_added']}[/green] "
        f"[red]-{revision['sections_removed']}[/red] "
        f"[yellow]~{revision['sections_modified']}[/yellow] "
        f"={revision['sections_unchanged']}"
    )
    table = Table()
    table.add_column("change id", justify="right")
    table.add_column("type")
    table.add_column("level")
    table.add_column("num")
    table.add_column("identifier")
    table.add_column("heading")
    for row in rows:
        color = {"added": "green", "removed": "red", "modified": "yellow"}[
            row["change_type"]
        ]
        table.add_row(
            str(row["id"]),
            f"[{color}]{row['change_type']}[/{color}]",
            row["level"] or "",
            row["num"] or "",
            row["identifier"] or "",
            (row["heading"] or "")[:60],
        )
    console.print(table)


@query_group.command(
    "diff", help="Show the unified text diff for a single section change."
)
@click.argument("change_id", type=int)
@click.pass_context
def query_diff(ctx: click.Context, change_id: int) -> None:
    with db.connect(_db_path(ctx)) as conn:
        row = db.get_section_change(conn, change_id)
    if not row:
        raise click.ClickException(f"section change {change_id} not found")
    if not row["text_diff"]:
        console.print(
            f"[dim]No text diff stored for change {change_id} "
            f"(type={row['change_type']}).[/dim]"
        )
        return
    click.echo(row["text_diff"])


def _resolve_version(conn: sqlite3.Connection, code_id: str, label: str) -> "object":
    row = db.find_version_by_label(conn, code_id, label)
    if row is None:
        raise click.ClickException(f"version {label!r} of {code_id!r} not found")
    return row


@query_group.command(
    "diff-versions",
    help="Diff any two versions of a code (need not be adjacent).",
)
@click.argument("code_id")
@click.option("--from", "from_label", required=True, help="Earlier version label.")
@click.option("--to", "to_label", required=True, help="Later version label.")
@click.option(
    "--identifier",
    default=None,
    help="Restrict the diff to a single USLM identifier.",
)
@click.option(
    "--unified",
    is_flag=True,
    help="Print full unified diffs (one per modified section) instead of a table.",
)
@click.pass_context
def query_diff_versions(
    ctx: click.Context,
    code_id: str,
    from_label: str,
    to_label: str,
    identifier: str | None,
    unified: bool,
) -> None:
    with db.connect(_db_path(ctx)) as conn:
        from_v = _resolve_version(conn, code_id, from_label)
        to_v = _resolve_version(conn, code_id, to_label)
        changes, stats = db.diff_versions(
            conn,
            code_id=code_id,
            from_version_id=int(from_v["id"]),
            to_version_id=int(to_v["id"]),
            identifier=identifier,
        )
    console.print(
        f"[bold]{code_id}[/bold] {from_label} → {to_label}  "
        f"[green]+{stats.added}[/green] "
        f"[red]-{stats.removed}[/red] "
        f"[yellow]~{stats.modified}[/yellow] "
        f"={stats.unchanged}"
    )
    if unified:
        for change in changes:
            color = {
                "added": "green",
                "removed": "red",
                "modified": "yellow",
            }[change.change_type]
            console.print(
                f"\n[{color}]{change.change_type}[/{color}] "
                f"{change.level} {change.num or ''} "
                f"{change.identifier or '(anonymous)'} — "
                f"{(change.heading or '')[:80]}"
            )
            if change.text_diff:
                click.echo(change.text_diff)
        return
    table = Table()
    table.add_column("type")
    table.add_column("level")
    table.add_column("num")
    table.add_column("identifier")
    table.add_column("heading")
    for change in changes:
        color = {"added": "green", "removed": "red", "modified": "yellow"}[
            change.change_type
        ]
        table.add_row(
            f"[{color}]{change.change_type}[/{color}]",
            change.level or "",
            change.num or "",
            change.identifier or "",
            (change.heading or "")[:60],
        )
    console.print(table)


@query_group.command(
    "blame",
    help=(
        "Show line-by-line provenance for a section identifier. "
        "Each line is annotated with the version that first introduced it."
    ),
)
@click.argument("code_id")
@click.argument("identifier")
@click.option(
    "--version",
    "version_label",
    default=None,
    help="Version label to blame at (defaults to the latest imported version).",
)
@click.pass_context
def query_blame(
    ctx: click.Context,
    code_id: str,
    identifier: str,
    version_label: str | None,
) -> None:
    with db.connect(_db_path(ctx)) as conn:
        if version_label:
            version_row = _resolve_version(conn, code_id, version_label)
        else:
            version_row = db.latest_version(conn, code_id)
            if version_row is None:
                raise click.ClickException(f"no imported version of {code_id!r} found")
        section = db.find_section_by_identifier(
            conn, int(version_row["id"]), identifier
        )
        if section is None:
            raise click.ClickException(
                f"identifier {identifier!r} not found in "
                f"{code_id}@{version_row['version_label']}"
            )
        lines = db.get_section_lines(conn, int(section["id"]))
    console.print(
        f"[bold]{identifier}[/bold] @ {code_id}/{version_row['version_label']}  "
        f"({section['level']} {section['num'] or ''}: "
        f"{(section['heading'] or '')[:60]})"
    )
    table = Table()
    table.add_column("line", justify="right")
    table.add_column("origin")
    table.add_column("text")
    for row in lines:
        table.add_row(
            str(row["line_no"]),
            row["origin_version_label"],
            row["text"],
        )
    console.print(table)


@query_group.command(
    "history",
    help="List every revision that touched a given USLM identifier.",
)
@click.argument("code_id")
@click.argument("identifier")
@click.pass_context
def query_history(ctx: click.Context, code_id: str, identifier: str) -> None:
    with db.connect(_db_path(ctx)) as conn:
        rows = db.section_history(conn, code_id, identifier)
    table = Table(title=f"History of {identifier} in {code_id}")
    table.add_column("rev id", justify="right")
    table.add_column("from")
    table.add_column("to")
    table.add_column("type")
    table.add_column("change id", justify="right")
    table.add_column("heading")
    for row in rows:
        color = {"added": "green", "removed": "red", "modified": "yellow"}[
            row["change_type"]
        ]
        table.add_row(
            str(row["revision_id"]),
            row["from_label"] or "(initial)",
            row["to_label"],
            f"[{color}]{row['change_type']}[/{color}]",
            str(row["change_id"]),
            (row["heading"] or "")[:60],
        )
    console.print(table)
    if not rows:
        console.print(
            f"[dim]No recorded changes for {identifier!r} in {code_id!r}.[/dim]"
        )


def _print_progress(phase: str, result: ProcessResult) -> None:
    doc = result.document
    if phase == "start":
        console.print(
            f"[cyan]→[/cyan] {doc.code_id}/{doc.version_label}/{doc.path.name}"
        )
        return
    color = {
        "imported": "green",
        "skipped": "yellow",
        "failed": "red",
    }.get(result.status, "white")
    if result.status == "imported":
        extra = f" sections={result.sections}"
        if result.revision is not None:
            r = result.revision
            extra += f" rev=+{r.added}/-{r.removed}/~{r.modified}/={r.unchanged}"
    else:
        extra = f" {result.message or ''}"
    console.print(
        f"  [{color}]{result.status}[/{color}] {doc.code_id}/{doc.version_label}{extra}"
    )


def _summarize(results: list[ProcessResult]) -> None:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no documents"
    console.print(f"\n[bold]Summary:[/bold] {summary}")
    failed = [r for r in results if r.status == "failed"]
    if failed:
        console.print("[red]Failures:[/red]")
        for r in failed:
            console.print(f"  - {r.document.relative_key}: {r.message}")
    if os.environ.get("UCDB_JSON"):
        click.echo(
            json.dumps(
                [
                    {
                        "code_id": r.document.code_id,
                        "version": r.document.version_label,
                        "path": str(r.document.path),
                        "status": r.status,
                        "version_id": r.version_id,
                        "sections": r.sections,
                        "message": r.message,
                    }
                    for r in results
                ],
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
