"""Smoke tests for the read-only UCDB web browser data layer."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture  # noqa: E402

from ucdb import db  # noqa: E402
from ucdb.process import import_xml_file  # noqa: E402
from ucdb.web import BrowserStore  # noqa: E402

CODE_ID = "tax-code"


def _seed(workdir: Path) -> Path:
    db_path = workdir / "db.sqlite3"
    db.init_db(db_path)
    for version_label, sections in fixture.build_versions()[:2]:
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
        assert result.status == "imported"
    return db_path


def test_browser_store_lists_collected_metadata(workdir: Path) -> None:
    store = BrowserStore(_seed(workdir))

    summary = store.summary()
    codes = store.codes()
    versions = store.versions(CODE_ID)
    sections = store.sections(int(versions[-1]["id"]))

    assert summary["codes"] == 1
    assert summary["versions"] == 2
    assert summary["sections"] > 0
    assert codes[0]["id"] == CODE_ID
    assert codes[0]["version_count"] == 2
    assert versions[-1]["section_count"] > 0
    assert sections


def test_browser_store_searches_and_opens_docs_diffs(workdir: Path) -> None:
    store = BrowserStore(_seed(workdir))

    results = store.search("twenty-five percent", code_id=CODE_ID)
    assert results

    section = store.section(int(results[0]["id"]))
    assert section is not None
    assert section["content"]
    assert section["xml_fragment"]
    assert "lines" in section
    assert "history" in section

    revisions = store.revisions(CODE_ID)
    assert revisions
    changes = store.changes(int(revisions[-1]["id"]))
    assert changes
    change = store.change(int(changes[0]["id"]))
    assert change is not None
    assert change["change_type"] in {"added", "removed", "modified"}


try:
    import pytest

    @pytest.fixture()
    def workdir(tmp_path: Path) -> Path:  # type: ignore[misc]
        return tmp_path

except ImportError:  # pragma: no cover
    pass


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
