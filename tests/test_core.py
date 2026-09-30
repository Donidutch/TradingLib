"""Tests for the cross-cutting core: config, event bus and database."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text

from tradinglib.config import Settings, load_settings, mask_secret
from tradinglib.db import init_db, session_scope, table_exists
from tradinglib.events import EventBus, channel_matches


def test_settings_defaults_are_keyless(settings: Settings) -> None:
    """Without credentials the keyed integrations report themselves unconfigured."""
    status = settings.provider_status()
    assert status["yfinance"]["configured"] is True
    assert status["edgar"]["configured"] is True
    assert status["alpaca"]["configured"] is False
    assert status["openai"]["configured"] is False


def test_env_overrides_and_masking(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Environment variables win, and secrets are masked for display."""
    monkeypatch.setenv("TRADINGLIB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-abcd1234")
    monkeypatch.setenv("TRADINGLIB_BUDGET_DAILY_USD", "42.5")
    resolved = load_settings(tmp_path / "absent.toml")
    assert resolved.openai_api_key == "sk-test-abcd1234"
    assert resolved.budgets.daily_usd == 42.5
    assert mask_secret(resolved.openai_api_key) == "*" * 12 + "1234"


def test_toml_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Values come from the TOML file when the environment is silent."""
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.delenv("TRADINGLIB_BUDGET_MONTHLY_USD", raising=False)
    config = tmp_path / "tradinglib.toml"
    config.write_text('[keys]\nfred = "fred-key"\n\n[budgets]\nmonthly_usd = 7.5\n')
    resolved = load_settings(config)
    assert resolved.fred_api_key == "fred-key"
    assert resolved.budgets.monthly_usd == 7.5


def test_database_initialises(settings: Settings) -> None:
    """``init_db`` creates the file and a session round-trips."""
    init_db()
    assert settings.db_path.exists()
    assert table_exists("definitely_not_a_table") is False
    with session_scope() as session:
        assert session.execute(text("select 1")).scalar_one() == 1
        assert session.execute(text("pragma foreign_keys")).scalar_one() == 1


@pytest.mark.parametrize(
    ("pattern", "channel", "expected"),
    [
        ("runs", "runs", True),
        ("runs", "runs.42", True),
        ("runs.*", "runs.42", True),
        ("*", "anything.here", True),
        ("runs", "runsx", False),
        ("tape", "quality", False),
    ],
)
def test_channel_matching(pattern: str, channel: str, expected: bool) -> None:
    """Subscription patterns cover exact, dotted-prefix and glob channels."""
    assert channel_matches(pattern, channel) is expected


async def test_event_bus_delivers_to_matching_subscribers(bus: EventBus) -> None:
    """Sync and async callbacks both receive matching events; others do not."""
    seen: list[str] = []

    async def async_cb(event: object) -> None:
        seen.append(f"async:{event.channel}")  # type: ignore[attr-defined]

    bus.subscribe("runs", lambda e: seen.append(f"sync:{e.channel}"))
    bus.subscribe("runs.*", async_cb)
    bus.subscribe("quality", lambda e: seen.append("wrong"))

    await bus.publish("runs.7", {"stage": 1})
    assert sorted(seen) == ["async:runs.7", "sync:runs.7"]


async def test_failing_subscriber_does_not_break_publish(bus: EventBus) -> None:
    """One broken subscriber must not stop the others."""
    delivered: list[str] = []

    def boom(_event: object) -> None:
        raise RuntimeError("subscriber exploded")

    bus.subscribe("status", boom)
    bus.subscribe("status", lambda e: delivered.append(e.channel))
    await bus.publish("status", {"ok": True})
    assert delivered == ["status"]


async def test_event_queue_drops_oldest_when_full(bus: EventBus) -> None:
    """A slow consumer degrades by losing the oldest events, not by blocking."""
    queue, sub = bus.queue("tape", maxsize=2)
    for i in range(4):
        await bus.publish("tape", {"i": i})
    sub.cancel()
    payloads = []
    while not queue.empty():
        payloads.append(queue.get_nowait().payload["i"])
    assert payloads == [2, 3]


async def test_unsubscribe_stops_delivery(bus: EventBus) -> None:
    """A cancelled subscription receives nothing further."""
    seen: list[int] = []
    sub = bus.subscribe("notifications", lambda e: seen.append(e.payload))
    await bus.publish("notifications", 1)
    sub.cancel()
    await bus.publish("notifications", 2)
    assert seen == [1]
    assert bus.subscription_count == 0


def test_publish_nowait_requires_a_loop(bus: EventBus) -> None:
    """``publish_nowait`` is explicit about needing a running loop."""
    with pytest.raises(RuntimeError):
        bus.publish_nowait("status", {"x": 1})

    async def inside() -> None:
        bus.publish_nowait("status", {"x": 1})
        await asyncio.sleep(0)

    asyncio.run(inside())
