"""End-to-end processing pipeline: document → AI → USLM XML → SQLite."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import db, hashing
from .ai import AIConfig, AIError, generate_uslm_xml
from .blame import compute_line_provenance
from .extract import UnsupportedFormatError, extract_text
from .ingest import ingest_xml
from .revisions import RevisionStats, compute_revision
from .scan import FoundDocument, scan_repository
from .xml_utils import XMLValidationError


@dataclass
class ProcessResult:
    document: FoundDocument
    status: str  # "imported" | "skipped" | "failed"
    version_id: int | None
    sections: int = 0
    message: str | None = None
    revision: RevisionStats | None = None


ProgressFn = Callable[[str, ProcessResult], None]


def _finalize_revision(
    conn: sqlite3.Connection, code_id: str, version_id: int, version_label: str
) -> RevisionStats:
    prev = db.previous_version(conn, code_id, version_label)
    parent_id = int(prev["id"]) if prev else None
    if parent_id is not None:
        db.set_version_status(conn, version_id, "imported", parent_version_id=parent_id)
    stats = compute_revision(
        conn,
        code_id=code_id,
        to_version_id=version_id,
        from_version_id=parent_id,
    )
    blame_lines = compute_line_provenance(
        conn,
        version_id=version_id,
        parent_version_id=parent_id,
    )
    db.log_event(
        conn,
        step="revision",
        status="success",
        code_id=code_id,
        version_id=version_id,
        details={
            "from_version_id": parent_id,
            "added": stats.added,
            "removed": stats.removed,
            "modified": stats.modified,
            "unchanged": stats.unchanged,
            "blame_lines": blame_lines,
        },
    )
    return stats


def process_document(
    conn: sqlite3.Connection,
    document: FoundDocument,
    *,
    ai_config: AIConfig | None = None,
    skip_existing: bool = True,
    validate_schema: bool = True,
) -> ProcessResult:
    """Run the full pipeline for a single document."""
    db.upsert_code(conn, document.code_id)

    source_hash = hashing.hash_file(document.path)
    existing = db.find_version_by_hash(conn, document.code_id, source_hash)
    if existing is not None and skip_existing and existing["status"] == "imported":
        return ProcessResult(
            document=document,
            status="skipped",
            version_id=int(existing["id"]),
            message="already imported (matching source hash)",
        )

    if existing is not None:
        version_id = int(existing["id"])
    else:
        version_id = db.create_version(
            conn,
            code_id=document.code_id,
            version_label=document.version_label,
            source_path=str(document.path),
            source_hash=source_hash,
            source_size=hashing.file_size(document.path),
            source_mime=hashing.guess_mime(document.path),
        )

    db.log_event(
        conn,
        step="pipeline.start",
        status="started",
        code_id=document.code_id,
        version_id=version_id,
        details={"path": str(document.path), "hash": source_hash},
    )

    cfg = ai_config or AIConfig.from_env()

    try:
        db.set_version_status(conn, version_id, "extracting")
        text = extract_text(document.path)
        db.log_event(
            conn,
            step="extract",
            status="success",
            code_id=document.code_id,
            version_id=version_id,
            details={"chars": len(text)},
        )

        db.set_version_status(
            conn,
            version_id,
            "generating",
            ai_provider=cfg.provider_name(),
            ai_model=cfg.model,
            ai_base_url=cfg.base_url,
        )
        xml_text = generate_uslm_xml(
            text,
            code_id=document.code_id,
            version_label=document.version_label,
            config=cfg,
        )
        xml_hash = hashing.hash_text(xml_text)
        db.log_event(
            conn,
            step="ai.generate",
            status="success",
            code_id=document.code_id,
            version_id=version_id,
            details={
                "xml_chars": len(xml_text),
                "xml_hash": xml_hash,
                "provider": cfg.provider_name(),
                "model": cfg.model,
            },
        )

        db.set_version_status(
            conn,
            version_id,
            "validating",
            xml_hash=xml_hash,
        )
        try:
            section_count = ingest_xml(
                conn, version_id, xml_text, validate_schema=validate_schema
            )
        except XMLValidationError as exc:
            db.set_version_status(
                conn,
                version_id,
                "failed",
                validation_status="failed",
                validation_message=str(exc),
            )
            raise
        db.set_version_status(
            conn,
            version_id,
            "imported",
            validation_status="passed",
            mark_processed=True,
        )
        db.log_event(
            conn,
            step="ingest",
            status="success",
            code_id=document.code_id,
            version_id=version_id,
            details={"sections": section_count},
        )
        revision_stats = _finalize_revision(
            conn,
            document.code_id,
            version_id,
            document.version_label,
        )
        return ProcessResult(
            document=document,
            status="imported",
            version_id=version_id,
            sections=section_count,
            revision=revision_stats,
        )
    except (
        UnsupportedFormatError,
        AIError,
        XMLValidationError,
        Exception,
    ) as exc:
        db.set_version_status(conn, version_id, "failed")
        db.log_event(
            conn,
            step="pipeline.error",
            status="failure",
            code_id=document.code_id,
            version_id=version_id,
            message=str(exc),
            details={"type": type(exc).__name__},
        )
        return ProcessResult(
            document=document,
            status="failed",
            version_id=version_id,
            message=f"{type(exc).__name__}: {exc}",
        )


def process_repository(
    conn: sqlite3.Connection,
    root: Path | str,
    *,
    ai_config: AIConfig | None = None,
    skip_existing: bool = True,
    validate_schema: bool = True,
    progress: ProgressFn | None = None,
) -> list[ProcessResult]:
    """Process every supported document under *root*."""
    results: list[ProcessResult] = []
    for document in scan_repository(root):
        if progress:
            progress(
                "start",
                ProcessResult(document=document, status="pending", version_id=None),
            )
        result = process_document(
            conn,
            document,
            ai_config=ai_config,
            skip_existing=skip_existing,
            validate_schema=validate_schema,
        )
        results.append(result)
        if progress:
            progress("done", result)
    return results


def import_xml_file(
    conn: sqlite3.Connection,
    *,
    code_id: str,
    version_label: str,
    xml_path: Path | str,
    source_path: Path | str | None = None,
    validate_schema: bool = True,
) -> ProcessResult:
    """Import a pre-generated USLM XML file into the database."""
    xml_path = Path(xml_path)
    xml_text = xml_path.read_text(encoding="utf-8")
    xml_hash = hashing.hash_text(xml_text)
    source = Path(source_path) if source_path else xml_path
    source_hash = hashing.hash_file(source)
    db.upsert_code(conn, code_id)
    existing = db.find_version_by_hash(conn, code_id, source_hash)
    if existing is not None:
        version_id = int(existing["id"])
    else:
        version_id = db.create_version(
            conn,
            code_id=code_id,
            version_label=version_label,
            source_path=str(source),
            source_hash=source_hash,
            source_size=hashing.file_size(source),
            source_mime=hashing.guess_mime(source),
        )

    document = FoundDocument(code_id=code_id, version_label=version_label, path=source)
    try:
        section_count = ingest_xml(
            conn, version_id, xml_text, validate_schema=validate_schema
        )
        db.set_version_status(
            conn,
            version_id,
            "imported",
            xml_hash=xml_hash,
            validation_status="passed",
            ai_provider="manual",
            mark_processed=True,
        )
        db.log_event(
            conn,
            step="ingest.xml",
            status="success",
            code_id=code_id,
            version_id=version_id,
            details={
                "sections": section_count,
                "xml_path": str(xml_path),
                "xml_hash": xml_hash,
            },
        )
        revision_stats = _finalize_revision(conn, code_id, version_id, version_label)
        return ProcessResult(
            document=document,
            status="imported",
            version_id=version_id,
            sections=section_count,
            revision=revision_stats,
        )
    except XMLValidationError as exc:
        db.set_version_status(
            conn,
            version_id,
            "failed",
            xml_hash=xml_hash,
            validation_status="failed",
            validation_message=str(exc),
        )
        db.log_event(
            conn,
            step="ingest.xml",
            status="failure",
            code_id=code_id,
            version_id=version_id,
            message=str(exc),
        )
        return ProcessResult(
            document=document,
            status="failed",
            version_id=version_id,
            message=str(exc),
        )
