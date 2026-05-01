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
from ucdb.process import import_akn_file  # noqa: E402
from ucdb.web import BrowserStore  # noqa: E402

WORK_ID = "tax-work"


def _seed(workdir: Path) -> Path:
    db_path = workdir / "db.sqlite3"
    db.init_db(db_path)
    for version_label, nodes in fixture.build_versions()[:2]:
        xml_path = workdir / f"{version_label}.xml"
        xml_path.write_text(fixture.to_akn_xml(version_label, nodes), encoding="utf-8")
        with db.connect(db_path) as conn:
            result = import_akn_file(
                conn,
                work_id=WORK_ID,
                version_label=version_label,
                xml_path=xml_path,
                validate_schema=False,
            )
        assert result.status == "imported"
    return db_path


def test_browser_store_lists_collected_metadata(workdir: Path) -> None:
    store = BrowserStore(_seed(workdir))

    summary = store.summary()
    works = store.works()
    expressions = store.expressions(WORK_ID)
    nodes = store.nodes(int(expressions[-1]["id"]))
    document = store.document(int(expressions[-1]["id"]))

    assert summary["works"] == 1
    assert summary["expressions"] == 2
    assert summary["nodes"] > 0
    assert works[0]["id"] == WORK_ID
    assert works[0]["expression_count"] == 2
    assert expressions[-1]["node_count"] > 0
    assert nodes
    assert document
    assert "depth" in document[0]
    assert any(row["text"] for row in document)


def test_browser_store_searches_and_opens_docs_diffs(workdir: Path) -> None:
    store = BrowserStore(_seed(workdir))

    results = store.search("twenty-five percent", work_id=WORK_ID)
    assert results

    node = store.node(int(results[0]["id"]))
    assert node is not None
    assert node["text"]
    assert node["xml_fragment"]
    assert "lines" in node
    assert "history" in node

    revisions = store.revisions(WORK_ID)
    assert revisions
    changes = store.changes(int(revisions[-1]["id"]))
    assert changes
    change = store.change(int(changes[0]["id"]))
    assert change is not None
    assert change["change_type"] in {"added", "removed", "modified"}


def test_browser_store_compares_documents_between_versions(workdir: Path) -> None:
    store = BrowserStore(_seed(workdir))
    expressions = store.expressions(WORK_ID)

    diff = store.document_diff(int(expressions[0]["id"]), int(expressions[1]["id"]))

    assert diff is not None
    assert diff["from_expression"]["version_label"] == "2020-01-01"
    assert diff["to_expression"]["version_label"] == "2020-07-01"
    assert diff["stats"]["added"] > 0
    assert diff["stats"]["modified"] > 0
    assert diff["stats"]["unchanged"] > 0
    assert any(row["change_type"] == "added" and row["to"] for row in diff["rows"])
    assert any(
        row["change_type"] == "modified"
        and row["from"]
        and row["to"]
        and row["text_diff"]
        for row in diff["rows"]
    )


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
