"""A folder rescan recognises the POS item list (by its "Item List as on"
banner, under any filename) instead of rejecting it as a broken stock
statement. uploads/item_catalog.xlsx — where the Reports page saves the
item list — used to show up under Import health's "Files rejected" after
every rescan, counting as an issue."""

import os

from src.gowri_proj import db
from src.gowri_proj.sync import sync_folder
from tests.test_sync_folder import _write_stock_statement
from tests.test_webapp_catalog_upload import _write_item_list

ITEMS = [("brand", "BRAND A"), ("item", "X1", "X TAB", "10", 1.0, 1.0, 12.0, "3004", None)]


def test_item_list_is_recognised_not_rejected(tmp_path):
    _write_stock_statement(tmp_path / "aug.xlsx")
    _write_item_list(tmp_path / "item_catalog.xlsx", ITEMS)
    with db.connect(str(tmp_path / "t.db")) as conn:
        r = sync_folder(conn, str(tmp_path))
        problems = db.list_watched_files_problems(conn)
    assert [x[0] for x in r.imported] == ["aug.xlsx"]
    assert r.item_lists == ["item_catalog.xlsx"]
    assert r.errors == []
    assert problems.empty


def test_item_list_recognised_under_any_name(tmp_path):
    _write_item_list(tmp_path / "item list Aug.xlsx", ITEMS)
    with db.connect(str(tmp_path / "t.db")) as conn:
        r = sync_folder(conn, str(tmp_path))
    assert r.item_lists == ["item list Aug.xlsx"] and r.errors == []


def test_existing_false_rejection_clears_on_next_rescan(tmp_path):
    # The state real databases are in: the item list recorded as an error
    # by an earlier rescan, and the file unchanged since.
    path = tmp_path / "item_catalog.xlsx"
    _write_item_list(path, ITEMS)
    st = os.stat(path)
    with db.connect(str(tmp_path / "t.db")) as conn:
        db.upsert_watched_file(conn, "item_catalog.xlsx", st.st_size, int(st.st_mtime), None,
                               "error", "This file only has 9 column(s)...")  # fmt: skip
        assert not db.list_watched_files_problems(conn).empty
        r = sync_folder(conn, str(tmp_path))
        assert r.item_lists == ["item_catalog.xlsx"]
        assert db.list_watched_files_problems(conn).empty
        # And it stays quiet on later rescans.
        again = sync_folder(conn, str(tmp_path))
        assert again.unchanged == ["item_catalog.xlsx"] and again.item_lists == []


def test_a_genuinely_broken_statement_is_still_rejected(tmp_path):
    (tmp_path / "broken.xlsx").write_bytes(b"not a spreadsheet")
    with db.connect(str(tmp_path / "t.db")) as conn:
        r = sync_folder(conn, str(tmp_path))
        assert [e[0] for e in r.errors] == ["broken.xlsx"]
        assert r.item_lists == []
        assert list(db.list_watched_files_problems(conn)["filename"]) == ["broken.xlsx"]
