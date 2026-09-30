"""Shared pytest fixtures.

Every test runs against an isolated temporary data directory and an isolated
event bus so the developer's real ``data/app.db`` is never touched.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tradinglib import config as config_module
from tradinglib import db as db_module
from tradinglib.config import Settings, reload_settings
from tradinglib.events import EventBus, reset_event_bus

#: Repository root, used to locate the real ``ui/`` directory.
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Settings pointing at a throwaway data directory.

    ``TRADINGLIB_INGEST_AUTOSTART`` is forced off so that booting the app in a test
    never issues live vendor requests; tests that want the scheduler drive it
    explicitly (``create_app(..., autostart_ingest=True)`` with an injected
    connector factory, or ``scheduler.tick()``).
    """
    monkeypatch.setenv("TRADINGLIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRADINGLIB_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("TRADINGLIB_INGEST_AUTOSTART", "0")
    db_module.dispose_engine()
    resolved = reload_settings()
    resolved.ensure_dirs()
    yield resolved
    db_module.dispose_engine()
    config_module.reload_settings()


@pytest.fixture()
def bus() -> Iterator[EventBus]:
    """A fresh event bus for each test."""
    b = reset_event_bus()
    yield b
    b.clear()


@pytest.fixture()
def app(settings: Settings, bus: EventBus) -> FastAPI:
    """An application instance bound to the temporary settings and bus."""
    from tradinglib.api.app import create_app

    return create_app(settings, bus=bus)


@pytest_asyncio.fixture()
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An httpx client driving the app in-process, with its lifespan run.

    Starlette's own ``lifespan_context`` is used so startup/shutdown really
    execute - the smoke test therefore exercises the same boot path uvicorn does.
    """
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c
