"""The browser-tab icon. Before it existed, every page load logged a
/favicon.ico 404 (browsers request that path on their own)."""

import pytest

from src.gowri_proj.webapp import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"), uploads_dir=str(tmp_path / "uploads"))
    with app.test_client() as c:
        yield c


@pytest.mark.parametrize("path", ["/favicon.ico", "/favicon.svg"])
def test_favicon_is_served(client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.mimetype == "image/svg+xml"
    assert resp.data.startswith(b"<svg")


def test_pages_link_the_favicon(client):
    # /reports renders with no data imported (the dashboard redirects there).
    html = client.get("/reports").data.decode()
    assert '<link rel="icon" type="image/svg+xml" href="/favicon.svg"' in html
