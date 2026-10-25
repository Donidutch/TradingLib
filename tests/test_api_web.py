"""Serving the React console from ``web/dist`` at ``/app`` (SPEC D1b)."""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tradinglib.config import Settings
from tradinglib.events import EventBus


@pytest_asyncio.fixture()
async def web_client(settings: Settings, bus: EventBus, tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """A client for an app whose ``web_dir`` is a small fake build."""
    from tradinglib.api.app import create_app

    web_dir = tmp_path / "dist"
    (web_dir / "assets").mkdir(parents=True)
    (web_dir / "index.html").write_text("<div id=root>REACT</div>")
    (web_dir / "assets" / "index.js").write_text("console.log(1)")
    app = create_app(dataclasses.replace(settings, web_dir=web_dir), bus=bus)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c


async def test_build_is_served_at_app(web_client: AsyncClient) -> None:
    """index.html and hashed assets come from web/dist, revalidated each time."""
    bare = await web_client.get("/app")
    assert bare.status_code == 307
    assert bare.headers["location"] == "/app/"
    index = await web_client.get("/app/")
    assert index.status_code == 200
    assert "REACT" in index.text
    assert index.headers["cache-control"] == "no-cache, must-revalidate"
    asset = await web_client.get("/app/assets/index.js")
    assert asset.status_code == 200


async def test_deep_links_fall_back_to_index(web_client: AsyncClient) -> None:
    """A route path loads the app; a missing asset is still a 404."""
    deep = await web_client.get("/app/stock/AAPL")
    assert deep.status_code == 200
    assert "REACT" in deep.text
    missing = await web_client.get("/app/assets/gone.js")
    assert missing.status_code == 404


async def test_old_console_and_api_are_untouched(web_client: AsyncClient) -> None:
    """/ still serves ui/ and /api still wins over both static mounts."""
    assert "TRADINGLIB" in (await web_client.get("/")).text
    assert (await web_client.get("/api/health")).json()["ok"] is True


@pytest_asyncio.fixture()
async def unbuilt_client(settings: Settings, bus: EventBus, tmp_path: Path) -> AsyncIterator[AsyncClient]:
    """A client for an app whose ``web_dir`` does not exist."""
    from tradinglib.api.app import create_app

    app = create_app(dataclasses.replace(settings, web_dir=tmp_path / "absent"), bus=bus)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c


async def test_missing_build_is_reported_not_hidden(unbuilt_client: AsyncClient) -> None:
    """Without a build, /app answers 503 with the path instead of the old console."""
    for path in ("/app", "/app/", "/app/stock/AAPL"):
        response = await unbuilt_client.get(path)
        assert response.status_code == 503, path
        assert response.json()["error"] == "web_not_built"
    assert "TRADINGLIB" in (await unbuilt_client.get("/")).text


def test_web_dir_setting(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """TRADINGLIB_WEB_DIR overrides the default, relative to the repo root."""
    from tradinglib.config import ROOT_DIR, reload_settings

    assert settings.web_dir == ROOT_DIR / "web" / "dist"
    monkeypatch.setenv("TRADINGLIB_WEB_DIR", "elsewhere/dist")
    assert reload_settings().web_dir == (ROOT_DIR / "elsewhere" / "dist").resolve()
