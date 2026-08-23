"""The kiosk's progressive-web-app surface.

What this protects is the one thing the offline queue cannot: the door screen
*starting* while the server is unreachable. Everything below is a precondition
for that, and every one of them fails silently — a manifest served with the
wrong content type, a worker whose scope cannot reach "/", an icon referenced
but never committed. None of them break a single page while the server is up,
which is exactly why they need a test: the first time anyone would notice is
during the outage the worker exists to survive.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_service_worker_is_served_from_the_root(client):
    """Not from /static, which is the whole point.

    A service worker may only control URLs at or below the path it was served
    from. At /static/sw.js its scope would be /static/* — it could cache the
    stylesheet and never the page, which is the only thing worth caching.
    """
    response = client.get("/sw.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    # Serving the worker stale would let it go on serving everything else stale.
    assert "no-cache" in response.headers.get("cache-control", "")


def test_the_worker_caches_the_whole_door_screen(client):
    """Every file the idle screen paints with is in the worker's shell list.

    A shell missing one of these loads to a page with no stylesheet or no
    controller — worse than not loading at all, because it looks like it worked.
    """
    source = (STATIC / "sw.js").read_text()

    for path in ("/", "/static/kiosk.css", "/static/kiosk.js", "/static/crest.png"):
        assert f'"{path}"' in source, f"{path} is not in the service worker's shell"


def test_the_worker_leaves_scans_alone(client):
    """/api is never named in the worker, and only GETs are intercepted.

    A cached POST /api/scan, or a scan answered from a cache, would be a second
    and invisible answer to the question kiosk.js's queue already owns.
    """
    source = (STATIC / "sw.js").read_text()

    assert '"/api' not in source
    assert 'request.method !== "GET"' in source


def test_manifest_parses_and_points_at_the_kiosk(client):
    response = client.get("/manifest.webmanifest")

    assert response.status_code == 200
    # Python's mimetypes has never heard of .webmanifest, so served off the
    # /static mount this would arrive as octet-stream and never be parsed.
    assert response.headers["content-type"].startswith("application/manifest+json")

    manifest = json.loads(response.text)
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"


def test_every_icon_the_manifest_names_exists(client):
    """A missing icon is a console warning and an uninstallable app."""
    manifest = json.loads(client.get("/manifest.webmanifest").text)

    for icon in manifest["icons"]:
        assert icon["src"].startswith("/static/")
        assert client.get(icon["src"]).status_code == 200

    # Chrome will not offer to install without one of at least 192px, and a
    # launcher crops a non-maskable icon to whatever shape it likes.
    assert any(icon["sizes"] == "192x192" for icon in manifest["icons"])
    assert any(icon.get("purpose") == "maskable" for icon in manifest["icons"])


def test_the_kiosk_page_links_the_manifest(client):
    page = client.get("/")

    assert page.status_code == 200
    assert '<link rel="manifest" href="/manifest.webmanifest">' in page.text


def test_every_icon_the_page_itself_links_resolves(client):
    """The manifest is not the only place icons are named.

    Safari ignores the manifest and reads apple-touch-icon; every browser reads
    the favicon link, and without one they all probe /favicon.ico, which this
    app has never served. Each of these is a 404 nobody sees except as a missing
    picture, so they are worth asserting rather than eyeballing.
    """
    page = client.get("/").text

    linked = re.findall(r'href="(/static/icons/[^"]+)"', page)
    assert linked, "the kiosk page links no icons at all"
    for src in linked:
        assert client.get(src).status_code == 200, f"{src} is linked but not served"


def test_the_kiosk_page_keeps_the_anchor_the_worker_injects_into(client):
    """sw.js marks a cached page by splicing a flag in before </head>.

    That flag is how kiosk.js knows to distrust the meal banner seeded into the
    markup. Losing the anchor loses the flag, and the failure is a kiosk booting
    from cache and announcing a meal that finished hours ago — with a live
    countdown under it, so nothing about it looks wrong.
    """
    assert "</head>" in client.get("/").text
