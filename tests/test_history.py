"""End-to-end test: build a 10-snapshot legal-code fixture and verify
search, version-pair diff, line-level blame, and section history.

The test runs without a network or AI backend — we feed pre-generated USLM XML
through :func:`ucdb.process.import_xml_file`, which exercises the same ingest,
revision, and line-provenance code paths as the AI pipeline.

Invocation
----------
* As a script:           ``uv run python tests/test_history.py``
* With pytest installed: ``uv run pytest tests/test_history.py``

The script form runs every ``test_*`` function in this file and prints a tally,
so the suite stays usable even before pytest is added as a dev dependency.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Allow running directly via `python tests/test_history.py` without an editable
# install — the package source lives under `src/`, which is not on sys.path
# unless we add it.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture  # noqa: E402

from ucdb import db  # noqa: E402
from ucdb.process import import_xml_file  # noqa: E402

CODE_ID = "tax-code"


def _seed(workdir: Path) -> Path:
    """Build a fresh DB at *workdir/db.sqlite3* and import all 10 snapshots."""
    db_path = workdir / "db.sqlite3"
    db.init_db(db_path)

    versions = fixture.build_versions()
    for version_label, sections in versions:
        xml_path = workdir / f"{version_label}.xml"
        xml_path.write_text(
            fixture.to_uslm_xml(version_label, sections), encoding="utf-8"
        )
        with db.connect(db_path) as conn:
            result = import_xml_file(
                conn,
                code_id=CODE_ID,
                version_label=version_label,
                xml_path=xml_path,
                validate_schema=False,
            )
        assert result.status == "imported", (
            f"{version_label}: expected imported, got {result.status} ({result.message})"
        )
    return db_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_all_versions_imported(workdir: Path) -> None:
    db_path = _seed(workdir)
    with db.connect(db_path) as conn:
        rows = db.list_versions(conn, CODE_ID)
    assert len(rows) == 10
    assert all(row["status"] == "imported" for row in rows)


def test_section_lines_populated(workdir: Path) -> None:
    db_path = _seed(workdir)
    with db.connect(db_path) as conn:
        # Every section ever inserted should have at least one blame row.
        sections_total = conn.execute(
            "SELECT COUNT(*) FROM sections WHERE level = 'section'"
        ).fetchone()[0]
        sections_with_lines = conn.execute(
            """
            SELECT COUNT(DISTINCT s.id)
            FROM sections s JOIN section_lines sl ON sl.section_id = s.id
            WHERE s.level = 'section'
            """
        ).fetchone()[0]
        assert sections_total == sections_with_lines, (
            f"some sections lack blame rows: {sections_total - sections_with_lines}"
        )


def test_blame_v1_lines_attributed_to_v1(workdir: Path) -> None:
    """Lines that survive untouched from v1 to v10 must blame to v1."""
    db_path = _seed(workdir)
    with db.connect(db_path) as conn:
        v10 = db.find_version_by_label(conn, CODE_ID, "2024-07-01")
        # /tax-code/s10 is edited only in v8 (truncation). Lines 1..7 survive
        # unchanged from v1; in v10 they must still blame to v1.
        s10 = db.find_section_by_identifier(conn, int(v10["id"]), "/tax-code/s10")
        lines = db.get_section_lines(conn, int(s10["id"]))
        assert lines, "expected blame lines for /tax-code/s10 at v10"
        # s10 was truncated to 7 lines in v8, then untouched.
        assert len(lines) == 7
        for line in lines:
            assert line["origin_version_label"] == "2020-01-01", (
                f"line {line['line_no']} of s10 should blame to v1, "
                f"got {line['origin_version_label']}: {line['text']}"
            )


def test_blame_modified_line_attributed_to_editing_version(workdir: Path) -> None:
    """The income-definition rewrite in v2 must show v2 as the line origin."""
    db_path = _seed(workdir)
    with db.connect(db_path) as conn:
        v10 = db.find_version_by_label(conn, CODE_ID, "2024-07-01")
        s1 = db.find_section_by_identifier(conn, int(v10["id"]), "/tax-code/s1")
        lines = db.get_section_lines(conn, int(s1["id"]))
        income_lines = [
            line for line in lines if line["text"].startswith("Income means")
        ]
        assert len(income_lines) == 1, (
            f"expected exactly one Income line, got {len(income_lines)}: "
            f"{[line['text'] for line in income_lines]}"
        )
        assert income_lines[0]["origin_version_label"] == "2020-07-01", (
            f"Income definition was rewritten in v2 (2020-07-01); "
            f"got {income_lines[0]['origin_version_label']}"
        )

        # The s1 lines added at v8 must blame to v8.
        digital_asset = [
            line for line in lines if line["text"].startswith("Digital asset")
        ]
        assert len(digital_asset) == 1
        assert digital_asset[0]["origin_version_label"] == "2023-07-01"


def test_blame_resets_when_section_is_reintroduced(workdir: Path) -> None:
    """Section /tax-code/s5 is removed in v4 then re-added with new text in v7.

    Every line of s5 at v10 must blame to v7 — the re-introduction version.
    Blame must NOT transitively jump back to v1 across the deletion gap.
    """
    db_path = _seed(workdir)
    with db.connect(db_path) as conn:
        v10 = db.find_version_by_label(conn, CODE_ID, "2024-07-01")
        s5 = db.find_section_by_identifier(conn, int(v10["id"]), "/tax-code/s5")
        assert s5 is not None, "s5 should exist again at v10"
        lines = db.get_section_lines(conn, int(s5["id"]))
        assert lines, "s5 must have blame rows"
        for line in lines:
            assert line["origin_version_label"] == "2023-01-01", (
                f"s5 line {line['line_no']} should blame to v7 (2023-01-01); "
                f"got {line['origin_version_label']}: {line['text']!r}"
            )


def test_diff_versions_arbitrary_pair(workdir: Path) -> None:
    """Diff between v1 and v10 must reflect the net cumulative change."""
    db_path = _seed(workdir)
    with db.connect(db_path) as conn:
        v1 = db.find_version_by_label(conn, CODE_ID, "2020-01-01")
        v10 = db.find_version_by_label(conn, CODE_ID, "2024-07-01")
        changes, stats = db.diff_versions(
            conn,
            code_id=CODE_ID,
            from_version_id=int(v1["id"]),
            to_version_id=int(v10["id"]),
        )

    by_ident = {c.identifier: c for c in changes if c.identifier}

    # s11 (Privacy) was added in v2 → still added relative to v1.
    assert by_ident["/tax-code/s11"].change_type == "added"

    # s5 (Credits) was rewritten on the round-trip; net effect vs v1 is "modified".
    assert by_ident["/tax-code/s5"].change_type == "modified"

    # Tax rate (s3) returned to 25% but the surtax stayed at 4% — so modified.
    assert by_ident["/tax-code/s3"].change_type == "modified"

    # s2 was edited multiple times, net effect is modified.
    assert by_ident["/tax-code/s2"].change_type == "modified"

    # s12 was added in v6 then repealed in v9 → must NOT appear in v1↔v10 diff.
    assert "/tax-code/s12" not in by_ident

    # Net counts: 1 addition (s11), 0 removals, several modifications.
    assert stats.added == 1, f"expected 1 added, got {stats.added}"
    assert stats.removed == 0, f"expected 0 removed, got {stats.removed}"
    assert stats.modified >= 4


def test_diff_versions_filtered_by_identifier(workdir: Path) -> None:
    db_path = _seed(workdir)
    with db.connect(db_path) as conn:
        v1 = db.find_version_by_label(conn, CODE_ID, "2020-01-01")
        v10 = db.find_version_by_label(conn, CODE_ID, "2024-07-01")
        changes, stats = db.diff_versions(
            conn,
            code_id=CODE_ID,
            from_version_id=int(v1["id"]),
            to_version_id=int(v10["id"]),
            identifier="/tax-code/s3",
        )
    assert len(changes) == 1
    change = changes[0]
    assert change.change_type == "modified"
    assert change.identifier == "/tax-code/s3"
    assert change.text_diff is not None
    assert "twenty-five" in change.text_diff
    assert "four percent" in change.text_diff  # surtax stayed bumped
    assert stats.modified == 1


def test_history_lists_every_touch(workdir: Path) -> None:
    """The Filing Requirements section was touched only in v5."""
    db_path = _seed(workdir)
    with db.connect(db_path) as conn:
        history_s2 = db.section_history(conn, CODE_ID, "/tax-code/s2")
        history_s5 = db.section_history(conn, CODE_ID, "/tax-code/s5")
        history_s12 = db.section_history(conn, CODE_ID, "/tax-code/s12")

    # s2: present in v1 (added vs nothing) + edited in v5 → 2 entries.
    s2_types = [h["change_type"] for h in history_s2]
    assert "added" in s2_types
    assert s2_types.count("modified") == 1, s2_types

    # s5: added v1, removed v4, re-added v7, untouched after → 3 entries.
    s5_types = [h["change_type"] for h in history_s5]
    assert s5_types.count("added") == 2
    assert s5_types.count("removed") == 1
    assert "modified" not in s5_types

    # s12: added v6, removed v9 → 2 entries, no modifies.
    s12_types = [h["change_type"] for h in history_s12]
    assert s12_types.count("added") == 1
    assert s12_types.count("removed") == 1


def test_search_still_works(workdir: Path) -> None:
    """Sanity-check that FTS5 search still finds known phrases after ingest."""
    db_path = _seed(workdir)
    with db.connect(db_path) as conn:
        rows = db.search_sections(conn, "twenty-five percent", code_id=CODE_ID)
    assert rows, "FTS5 should match the standard-rate phrase"
    # The phrase appears in v1, v2, v10 (s3 returns to 25%) — at least one hit.
    matched_versions = {row["version_label"] for row in rows}
    assert "2020-01-01" in matched_versions or "2024-07-01" in matched_versions


def test_reprocess_is_idempotent(workdir: Path) -> None:
    """Re-importing every snapshot must keep counts stable, not duplicate rows."""
    db_path = _seed(workdir)
    with db.connect(db_path) as conn:
        line_count_before = conn.execute(
            "SELECT COUNT(*) FROM section_lines"
        ).fetchone()[0]
        revision_count_before = conn.execute(
            "SELECT COUNT(*) FROM revisions WHERE code_id = ?", (CODE_ID,)
        ).fetchone()[0]

    versions = fixture.build_versions()
    for version_label, sections in versions:
        xml_path = workdir / f"reimport-{version_label}.xml"
        xml_path.write_text(
            fixture.to_uslm_xml(version_label, sections), encoding="utf-8"
        )
        with db.connect(db_path) as conn:
            import_xml_file(
                conn,
                code_id=CODE_ID,
                version_label=version_label,
                xml_path=xml_path,
                validate_schema=False,
            )

    with db.connect(db_path) as conn:
        line_count_after = conn.execute(
            "SELECT COUNT(*) FROM section_lines"
        ).fetchone()[0]
        revision_count_after = conn.execute(
            "SELECT COUNT(*) FROM revisions WHERE code_id = ?", (CODE_ID,)
        ).fetchone()[0]

    assert line_count_before == line_count_after, (
        f"section_lines count changed on reimport: "
        f"{line_count_before} → {line_count_after}"
    )
    assert revision_count_before == revision_count_after


# ---------------------------------------------------------------------------
# pytest fixture
# ---------------------------------------------------------------------------

try:
    import pytest

    @pytest.fixture()
    def workdir(tmp_path: Path) -> Path:  # type: ignore[misc]
        return tmp_path

except ImportError:  # pragma: no cover — pytest is optional at runtime
    pass


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


def _main() -> int:
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failed: list[tuple[str, BaseException]] = []
    for name, fn in tests:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                fn(Path(tmp))  # type: ignore[arg-type]
            except AssertionError as exc:
                failed.append((name, exc))
                print(f"FAIL  {name}: {exc}")
            except Exception as exc:  # pragma: no cover
                failed.append((name, exc))
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
            else:
                print(f"ok    {name}")
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(_main())
