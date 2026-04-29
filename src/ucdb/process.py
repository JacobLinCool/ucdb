"""End-to-end processing pipeline: source document -> Akoma Ntoso -> SQLite."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import db, hashing
from .ai import AIConfig, AIError, generate_akn_xml
from .akn import AKNValidationError
from .blame import compute_line_provenance
from .extract import UnsupportedFormatError, extract_text
from .ingest import ingest_akn_xml
from .revisions import RevisionStats, compute_revision
from .scan import FoundDocument, scan_repository
from .tw_profile import AKN_PROFILE


@dataclass
class ProcessResult:
    document: FoundDocument
    status: str
    expression_id: int | None
    nodes: int = 0
    message: str | None = None
    revision: RevisionStats | None = None


ProgressFn = Callable[[str, ProcessResult], None]


def _finalize_revision(
    conn: sqlite3.Connection,
    work_id: str,
    expression_id: int,
    version_label: str,
    language: str,
) -> RevisionStats:
    prev = db.previous_expression(conn, work_id, version_label, language)
    parent_id = int(prev["id"]) if prev else None
    if parent_id is not None:
        db.set_expression_status(
            conn, expression_id, "imported", parent_expression_id=parent_id
        )
    stats = compute_revision(
        conn,
        work_id=work_id,
        to_expression_id=expression_id,
        from_expression_id=parent_id,
    )
    line_count = compute_line_provenance(
        conn,
        expression_id=expression_id,
        parent_expression_id=parent_id,
    )
    db.log_event(
        conn,
        step="revision",
        status="success",
        work_id=work_id,
        expression_id=expression_id,
        details={
            "from_expression_id": parent_id,
            "added": stats.added,
            "removed": stats.removed,
            "modified": stats.modified,
            "unchanged": stats.unchanged,
            "blame_lines": line_count,
        },
    )
    return stats


def process_document(
    conn: sqlite3.Connection,
    document: FoundDocument,
    *,
    ai_config: AIConfig | None = None,
    language: str = "zho",
    skip_existing: bool = True,
    validate_schema: bool = True,
) -> ProcessResult:
    db.upsert_work(conn, document.work_id)

    source_hash = hashing.hash_file(document.path)
    existing = db.find_expression_by_hash(conn, document.work_id, source_hash, language)
    if existing is not None and skip_existing and existing["status"] == "imported":
        return ProcessResult(
            document=document,
            status="skipped",
            expression_id=int(existing["id"]),
            message="already imported (matching source hash)",
        )

    expression_id = (
        int(existing["id"])
        if existing is not None
        else db.create_expression(
            conn,
            work_id=document.work_id,
            version_label=document.version_label,
            language=language,
            source_path=str(document.path),
            source_hash=source_hash,
            source_size=hashing.file_size(document.path),
            source_mime=hashing.guess_mime(document.path),
            akn_profile=AKN_PROFILE,
            expression_date=document.version_label,
        )
    )
    db.log_event(
        conn,
        step="pipeline.start",
        status="started",
        work_id=document.work_id,
        expression_id=expression_id,
        details={"path": str(document.path), "hash": source_hash},
    )

    cfg = ai_config or AIConfig.from_env()
    try:
        db.set_expression_status(conn, expression_id, "extracting")
        text = extract_text(document.path)
        db.log_event(
            conn,
            step="extract",
            status="success",
            work_id=document.work_id,
            expression_id=expression_id,
            details={"chars": len(text)},
        )

        db.set_expression_status(
            conn,
            expression_id,
            "generating",
            ai_provider=cfg.provider_name(),
            ai_model=cfg.model,
            ai_base_url=cfg.base_url,
        )
        prev = db.previous_expression(
            conn, document.work_id, document.version_label, language
        )
        parent_xml = prev["canonical_xml"] if prev is not None else None
        parent_label = prev["version_label"] if prev is not None else None
        ai_result = generate_akn_xml(
            text,
            work_id=document.work_id,
            version_label=document.version_label,
            language=language,
            config=cfg,
            parent_xml=parent_xml,
            parent_label=parent_label,
        )
        xml_text = ai_result.xml
        xml_hash = hashing.hash_text(xml_text)
        db.log_event(
            conn,
            step="ai.generate",
            status="success",
            work_id=document.work_id,
            expression_id=expression_id,
            details={
                "xml_chars": len(xml_text),
                "canonical_hash": xml_hash,
                "provider": cfg.provider_name(),
                "model": cfg.model,
                "parent_version_label": parent_label,
                "usage": ai_result.usage.as_dict(),
            },
        )
        db.set_expression_status(
            conn, expression_id, "validating", canonical_hash=xml_hash
        )
        node_count = ingest_akn_xml(
            conn, expression_id, xml_text, validate_schema=validate_schema
        )
        db.set_expression_status(
            conn,
            expression_id,
            "imported",
            canonical_hash=xml_hash,
            validation_status="passed",
            mark_processed=True,
        )
        revision = _finalize_revision(
            conn, document.work_id, expression_id, document.version_label, language
        )
        return ProcessResult(
            document=document,
            status="imported",
            expression_id=expression_id,
            nodes=node_count,
            revision=revision,
        )
    except (UnsupportedFormatError, AIError, AKNValidationError, Exception) as exc:
        db.set_expression_status(conn, expression_id, "failed")
        db.log_event(
            conn,
            step="pipeline.error",
            status="failure",
            work_id=document.work_id,
            expression_id=expression_id,
            message=str(exc),
            details={"type": type(exc).__name__},
        )
        return ProcessResult(
            document=document,
            status="failed",
            expression_id=expression_id,
            message=f"{type(exc).__name__}: {exc}",
        )


def process_repository(
    conn: sqlite3.Connection,
    root: Path | str,
    *,
    ai_config: AIConfig | None = None,
    language: str = "zho",
    skip_existing: bool = True,
    validate_schema: bool = True,
    progress: ProgressFn | None = None,
) -> list[ProcessResult]:
    results: list[ProcessResult] = []
    for document in scan_repository(root):
        if progress:
            progress("start", ProcessResult(document, "pending", None))
        result = process_document(
            conn,
            document,
            ai_config=ai_config,
            language=language,
            skip_existing=skip_existing,
            validate_schema=validate_schema,
        )
        results.append(result)
        if progress:
            progress("done", result)
    return results


def import_akn_file(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    version_label: str,
    xml_path: Path | str,
    source_path: Path | str | None = None,
    language: str = "zho",
    title: str | None = None,
    jurisdiction: str = "tw",
    document_class: str = "law",
    source_authority: str | None = None,
    validate_schema: bool = True,
) -> ProcessResult:
    xml_path = Path(xml_path)
    xml_text = xml_path.read_text(encoding="utf-8")
    xml_hash = hashing.hash_text(xml_text)
    source = Path(source_path) if source_path else xml_path
    source_hash = hashing.hash_file(source)

    db.upsert_work(
        conn,
        work_id,
        jurisdiction=jurisdiction,
        document_class=document_class,
        title=title,
        source_authority=source_authority,
    )
    existing = db.find_expression_by_hash(conn, work_id, source_hash, language)
    expression_id = (
        int(existing["id"])
        if existing is not None
        else db.create_expression(
            conn,
            work_id=work_id,
            version_label=version_label,
            language=language,
            expression_date=version_label,
            source_path=str(source),
            source_hash=source_hash,
            source_size=hashing.file_size(source),
            source_mime=hashing.guess_mime(source),
            akn_profile=AKN_PROFILE,
        )
    )

    document = FoundDocument(work_id=work_id, version_label=version_label, path=source)
    try:
        node_count = ingest_akn_xml(
            conn, expression_id, xml_text, validate_schema=validate_schema
        )
        db.set_expression_status(
            conn,
            expression_id,
            "imported",
            canonical_hash=xml_hash,
            validation_status="passed",
            ai_provider="manual",
            mark_processed=True,
        )
        db.log_event(
            conn,
            step="ingest.akn",
            status="success",
            work_id=work_id,
            expression_id=expression_id,
            details={"nodes": node_count, "xml_path": str(xml_path), "hash": xml_hash},
        )
        revision = _finalize_revision(
            conn, work_id, expression_id, version_label, language
        )
        return ProcessResult(
            document, "imported", expression_id, node_count, revision=revision
        )
    except AKNValidationError as exc:
        db.set_expression_status(
            conn,
            expression_id,
            "failed",
            canonical_hash=xml_hash,
            validation_status="failed",
            validation_message=str(exc),
        )
        db.log_event(
            conn,
            step="ingest.akn",
            status="failure",
            work_id=work_id,
            expression_id=expression_id,
            message=str(exc),
        )
        return ProcessResult(document, "failed", expression_id, message=str(exc))
