"""Item lists: a dated copy of each one imported is kept under
uploads/item_lists/<FY>/item_list_<date>.<ext> — only the last six months
of them — and a rescan imports a newer item list dropped into uploads/
(never an older one)."""

import pytest

from src.gowri_proj import db
from src.gowri_proj.sync import sync_folder
from src.gowri_proj.webapp import create_app
from tests.test_webapp_catalog_upload import _write_item_list


def _items(*codes_names):
    return [("brand", "BRAND A")] + [
        ("item", code, name, "10", 1.0, 1.0, 12.0, "3004", None) for code, name in codes_names
    ]


@pytest.fixture
def env(tmp_path):
    uploads = tmp_path / "uploads"
    app = create_app(db_path=str(tmp_path / "t.db"), uploads_dir=str(uploads))
    with app.test_client() as c:
        yield c, app, uploads


def _upload(env, path):
    c, app, _ = env
    with open(path, "rb") as f:
        return c.post("/api/upload-item-list", data={"file": (f, path.name)},
                      content_type="multipart/form-data",
                      headers={"X-CSRF-Token": app.config["CSRF_TOKEN"]})  # fmt: skip


def _archived(uploads):
    return sorted(
        p.relative_to(uploads).as_posix() for p in (uploads / "item_lists").rglob("*.xls*")
    )


def test_upload_keeps_a_dated_copy_and_rescan_leaves_it_alone(env, tmp_path):
    _, app, uploads = env
    _write_item_list(tmp_path / "il.xlsx", _items(("X1", "X TAB")), as_of="09/08/2026")
    assert _upload(env, tmp_path / "il.xlsx").status_code == 200
    assert (uploads / "item_catalog.xlsx").exists()
    assert _archived(uploads) == ["item_lists/2627/item_list_2026-08-09.xlsx"]
    with db.connect(app.config["DB_PATH"]) as conn:
        r = sync_folder(conn, str(uploads))
        assert r.item_lists_imported == [] and r.errors == []
        assert db.list_watched_files_problems(conn).empty


def test_only_the_last_six_months_are_kept(env, tmp_path):
    _, _, uploads = env
    # Not ours: must never be deleted, however old.
    (uploads / "item_lists" / "2526").mkdir(parents=True)
    (uploads / "item_lists" / "2526" / "my notes.xlsx").write_bytes(b"x")
    (uploads / "old item list.xlsx").write_bytes(b"x")
    for i, d in enumerate(["10/01/2026", "01/03/2026", "09/08/2026"]):
        _write_item_list(tmp_path / f"il{i}.xlsx", _items(("X1", "X TAB")), as_of=d)
        assert _upload(env, tmp_path / f"il{i}.xlsx").status_code == 200
    # 9 Aug minus ~6 months = 7 Feb: January's copy goes, March's stays.
    assert _archived(uploads) == [
        "item_lists/2526/item_list_2026-03-01.xlsx",
        "item_lists/2526/my notes.xlsx",
        "item_lists/2627/item_list_2026-08-09.xlsx",
    ]
    assert (uploads / "old item list.xlsx").exists()


def test_six_months_counts_back_from_the_newest_list_not_today(env, tmp_path):
    _, _, uploads = env
    # Both well over six months before today, but only two months apart.
    for i, d in enumerate(["01/01/2025", "01/03/2025"]):
        _write_item_list(tmp_path / f"il{i}.xlsx", _items(("X1", "X TAB")), as_of=d)
        _upload(env, tmp_path / f"il{i}.xlsx")
    assert _archived(uploads) == [
        "item_lists/2425/item_list_2025-01-01.xlsx",
        "item_lists/2425/item_list_2025-03-01.xlsx",
    ]


def test_rescan_imports_a_newer_list_dropped_into_uploads(env, tmp_path):
    _, app, uploads = env
    _write_item_list(tmp_path / "il.xlsx", _items(("X1", "X TAB")), as_of="09/08/2026")
    _upload(env, tmp_path / "il.xlsx")
    _write_item_list(
        uploads / "item list 17 Aug.xlsx", _items(("X1", "X TAB (NON)")), as_of="17/08/2026"
    )
    with db.connect(app.config["DB_PATH"]) as conn:
        r = sync_folder(conn, str(uploads))
        assert r.item_lists_imported == [("item list 17 Aug.xlsx", "2026-08-17")]
        assert db.get_item_catalog_meta(conn)["as_of"] == "2026-08-17"
        assert (
            db.get_item_catalog_df(conn).set_index("code").loc["X1", "product_name"]
            == "X TAB (NON)"
        )
        # The rename it revealed is logged for rename matching.
        assert db.get_name_change_map(conn) == {"X TAB": "X TAB (NON)"}
        assert db.list_watched_files_problems(conn).empty
    assert "item_lists/2627/item_list_2026-08-17.xlsx" in _archived(uploads)


@pytest.mark.parametrize(
    "as_of, why",
    [("01/07/2026", "older than the current one"), ("", "no date in its banner")],
)
def test_rescan_never_imports_an_older_or_undated_list(env, tmp_path, as_of, why):
    _, app, uploads = env
    _write_item_list(tmp_path / "il.xlsx", _items(("X1", "X TAB")), as_of="09/08/2026")
    _upload(env, tmp_path / "il.xlsx")
    _write_item_list(uploads / "some list.xlsx", _items(("X1", "OTHER NAME")), as_of=as_of)
    with db.connect(app.config["DB_PATH"]) as conn:
        r = sync_folder(conn, str(uploads))
        assert r.item_lists_imported == []
        assert db.get_item_catalog_meta(conn)["as_of"] == "2026-08-09"
        assert db.get_item_catalog_df(conn).set_index("code").loc["X1", "product_name"] == "X TAB"
        assert why in db.get_watched_file(conn, "some list.xlsx")["detail"]
