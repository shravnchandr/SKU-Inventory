"""A problem with an item list found during a rescan must never abort the
whole rescan: the stock statements in the same folder still get imported.
Before this fix, an item list that failed to import (e.g. a duplicated item
code) or couldn't be copied (a Windows file lock) raised straight out of
sync_folder."""

from unittest.mock import patch

from src.gowri_proj import db
from src.gowri_proj.sync import sync_folder
from tests.test_sync_folder import _write_stock_statement
from tests.test_webapp_catalog_upload import _write_item_list


def _items(*rows):
    return [("brand", "BRAND A")] + [
        ("item", c, n, "10", 1.0, 1.0, 12.0, "3004", None) for c, n in rows
    ]


def test_item_list_that_fails_to_import_does_not_abort_the_rescan(tmp_path):
    _write_stock_statement(tmp_path / "aug.xlsx")
    # Same code twice: item_catalog's primary key rejects it.
    _write_item_list(
        tmp_path / "list.xlsx", _items(("X1", "A TAB"), ("X1", "B TAB")), as_of="17/08/2026"
    )
    with db.connect(str(tmp_path / "t.db")) as conn:
        r = sync_folder(conn, str(tmp_path))
        assert [x[0] for x in r.imported] == ["aug.xlsx"]
        assert [e[0] for e in r.errors] == ["list.xlsx"]
        assert db.get_item_catalog_meta(conn) is None  # nothing half-applied
        assert list(db.list_watched_files_problems(conn)["filename"]) == ["list.xlsx"]


def test_locked_file_while_keeping_a_copy_does_not_abort_the_rescan(tmp_path):
    _write_stock_statement(tmp_path / "aug.xlsx")
    _write_item_list(tmp_path / "list.xlsx", _items(("X1", "A TAB")), as_of="17/08/2026")
    with (
        db.connect(str(tmp_path / "t.db")) as conn,
        patch(
            "src.gowri_proj.sync.shutil.copyfile",
            side_effect=PermissionError(5, "Access is denied"),
        ),
    ):
        r = sync_folder(conn, str(tmp_path))
        assert [x[0] for x in r.imported] == ["aug.xlsx"]
        # The list itself is imported — only the copies couldn't be made.
        assert r.item_lists_imported == [("list.xlsx", "2026-08-17")]
        assert db.get_item_catalog_meta(conn)["as_of"] == "2026-08-17"
