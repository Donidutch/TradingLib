"""API smoke tests: the app boots, serves /api/status and the UI, and speaks /ws."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from httpx import AsyncClient
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


async def test_status_endpoint_responds(client: AsyncClient) -> None:
    """``GET /api/status`` returns 200 with the datastrip contract."""
    response = await client.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    for key in (
        "data_live",
        "gateway_ms",
        "sources_connected",
        "last_ingest",
        "delayed_series",
        "trials_logged",
        "spend",
        "engine_health",
    ):
        assert key in body, f"missing datastrip key: {key}"
    assert set(body["spend"]) >= {"used", "budget"}


async def test_status_is_honest_before_subsystems_start(client: AsyncClient) -> None:
    """Nothing is invented: unknown means null, and absent subsystems say so."""
    body = (await client.get("/api/status")).json()
    assert body["gateway_ms"] is None
    assert body["last_ingest"] is None
    assert body["data_live"] is False
    assert body["sources_connected"] == 0
    assert body["trials_logged"] == 0
    assert body["spend"]["used"] == 0.0
    assert body["subsystems"]["ingest_scheduler"] == "not_started"
    assert body["subsystems"]["database"] == "up"


async def test_health_endpoint(client: AsyncClient) -> None:
    """``GET /api/health`` is a dependency-free liveness probe."""
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


async def test_index_is_served(client: AsyncClient) -> None:
    """The UI directory is mounted at ``/`` and serves index.html."""
    response = await client.get("/")
    assert response.status_code == 200
    assert "TRADINGLIB" in response.text


async def test_css_blocks_are_served_in_order(client: AsyncClient) -> None:
    """Every REFERENCE style block is reachable as a static file, in order.

    Nine files: REFERENCE's eight ``<style>`` blocks plus the CSS it injects at
    runtime through ``TL.style(...)``, which the Agent Center's own markup
    (``asm-*``, ``v5x-*``, ``tl-import``, ``v5-count``) depends on.
    """
    names = sorted(p.name for p in (ROOT / "ui" / "css").glob("*.css"))
    assert len(names) == 9
    for name in names:
        response = await client.get(f"/css/{name}")
        assert response.status_code == 200, name
        assert response.text.strip()


def test_websocket_subscribe_protocol(app: FastAPI) -> None:
    """``/ws`` accepts a connection, honours subscribe and relays bus events."""
    with TestClient(app) as test_client:
        with test_client.websocket_connect("/ws") as socket:
            welcome = socket.receive_json()
            assert welcome["type"] == "welcome"
            assert "status" in welcome["channels"]

            socket.send_text(json.dumps({"action": "subscribe", "channel": "runs"}))
            ack = socket.receive_json()
            assert ack == {"type": "subscribed", "channel": "runs", "params": {}}

            socket.send_text(json.dumps({"action": "ping"}))
            assert socket.receive_json()["type"] == "pong"

            socket.send_text(json.dumps({"action": "subscribe", "channel": "nope"}))
            err = socket.receive_json()
            assert err["type"] == "error"
            assert err["error"] == "unknown_channel"


def test_status_row_helpers_read_real_tables(settings: object) -> None:
    """The counters read genuine rows, and reject anything that is not an identifier."""
    import pytest
    from sqlalchemy import text

    from tradinglib.api.routes.status import _count_rows, _sum_column
    from tradinglib.db import init_db, session_scope

    init_db()
    with session_scope() as session:
        # ``experiment_runs`` and ``agent_runs`` are both real tables now
        # (tradinglib.research.experiments, tradinglib.agents.runner), so the counters
        # are exercised against scratch tables of their own rather than
        # stand-ins that would collide with the declared schema.
        session.execute(text("create table smoke_rows (id integer primary key)"))
        session.execute(text("insert into smoke_rows (id) values (1),(2),(3)"))
        session.execute(text("create table smoke_costs (id integer primary key, cost_usd real)"))
        session.execute(text("insert into smoke_costs (id, cost_usd) values (1, 0.25),(2, 1.5)"))

    assert _count_rows("smoke_rows") == 3
    assert _count_rows("table_that_does_not_exist") == 0
    assert _sum_column("smoke_costs", "cost_usd") == 1.75
    assert _sum_column("table_that_does_not_exist", "cost_usd") == 0.0

    with pytest.raises(ValueError):
        _count_rows("smoke_costs; drop table smoke_costs")
