"""API smoke tests for ``/api/trade`` (paper broker, synthetic quotes).

The application's paper broker is pointed at a deterministic quote source via
``app.state.trade_quote_provider``, so these tests exercise the real routes,
the real simulator and the real SQLite accounting without touching a vendor.
No test places an order on a live venue: every request targets the paper
adapter, and the one Alpaca request asserts that an unconfigured adapter is
refused before any network call is made.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from tests.test_trade_paper import SyntheticQuoteProvider


@pytest.fixture()
def quotes(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> SyntheticQuoteProvider:
    """Install a synthetic quote source and a predictable paper configuration."""
    monkeypatch.setenv("TRADINGLIB_PAPER_ACCOUNT", "api-paper")
    monkeypatch.setenv("TRADINGLIB_PAPER_CASH", "50000")
    monkeypatch.setenv("TRADINGLIB_PAPER_SLIPPAGE_BPS", "0")
    monkeypatch.setenv("TRADINGLIB_IBKR_PORT", "1")
    provider = SyntheticQuoteProvider({"AAPL": 100.0, "MSFT": 200.0})
    app.state.trade_quote_provider = provider
    return provider


async def test_broker_statuses_are_honest(
    client: AsyncClient, quotes: SyntheticQuoteProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every adapter reports what it actually is - no session is opened."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    response = await client.get("/api/trade/brokers")
    assert response.status_code == 200
    body = response.json()
    assert body["default"] == "paper"
    assert body["live_trading_enabled"] is False

    by_name = {b["broker"]: b for b in body["brokers"]}
    assert set(by_name) == {"paper", "alpaca", "ibkr"}
    assert by_name["paper"]["paper"] is True
    assert by_name["ibkr"]["paper"] is True
    assert by_name["ibkr"]["gateway_reachable"] is False
    assert by_name["ibkr"]["status"] == "unavailable"
    for payload in by_name.values():
        assert payload["paper"] is True, "no adapter may default to live"


async def test_empty_portfolio_reports_the_opening_balance(
    client: AsyncClient, quotes: SyntheticQuoteProvider
) -> None:
    """A fresh paper account holds its opening cash and nothing else."""
    response = await client.get("/api/trade/portfolio")
    assert response.status_code == 200
    body = response.json()
    assert body["broker"] == "paper"
    assert body["cash"] == 50_000.0
    assert body["starting_cash"] == 50_000.0
    assert body["positions"] == []
    assert body["equity"] == 50_000.0
    assert body["broker_status"]["paper"] is True


async def test_market_order_fills_and_moves_the_portfolio(
    client: AsyncClient, quotes: SyntheticQuoteProvider
) -> None:
    """POST /api/trade/orders places a real simulated trade."""
    response = await client.post(
        "/api/trade/orders", json={"symbol": "AAPL", "side": "buy", "qty": 10}
    )
    assert response.status_code == 200
    order = response.json()["order"]
    assert order["status"] == "filled"
    assert order["avg_fill_price"] == pytest.approx(100.0)
    assert order["broker"] == "paper"

    portfolio = (await client.get("/api/trade/portfolio")).json()
    assert portfolio["cash"] == pytest.approx(49_000.0)
    assert portfolio["positions"][0]["symbol"] == "AAPL"
    assert portfolio["positions"][0]["qty"] == 10
    assert portfolio["equity"] == pytest.approx(50_000.0)

    orders = (await client.get("/api/trade/orders")).json()
    assert orders["count"] == 1
    assert orders["open"] == 0

    fills = (await client.get("/api/trade/fills")).json()
    assert fills["count"] == 1
    assert fills["fills"][0]["reference_price"] == pytest.approx(100.0)


async def test_limit_order_rests_and_fills_on_a_pushed_quote(
    client: AsyncClient, quotes: SyntheticQuoteProvider
) -> None:
    """A resting limit order fills when a quote is fed to the simulator."""
    placed = await client.post(
        "/api/trade/orders",
        json={"symbol": "MSFT", "side": "buy", "qty": 2, "type": "limit", "limit_price": 180.0},
    )
    assert placed.status_code == 200
    order = placed.json()["order"]
    assert order["status"] == "new"

    pushed = await client.post(
        "/api/trade/paper/quote", json={"symbol": "MSFT", "price": 179.0}
    )
    assert pushed.status_code == 200
    body = pushed.json()
    assert body["filled_count"] == 1
    assert body["filled"][0]["id"] == order["id"]
    assert body["filled"][0]["avg_fill_price"] == pytest.approx(179.0)


async def test_cancelling_a_resting_order(
    client: AsyncClient, quotes: SyntheticQuoteProvider
) -> None:
    """DELETE cancels a resting order; a second attempt is a 400."""
    order = (
        await client.post(
            "/api/trade/orders",
            json={"symbol": "AAPL", "side": "buy", "qty": 1, "type": "limit", "limit_price": 50.0},
        )
    ).json()["order"]

    cancelled = await client.delete(f"/api/trade/orders/{order['id']}")
    assert cancelled.status_code == 200
    assert cancelled.json()["order"]["status"] == "cancelled"

    again = await client.delete(f"/api/trade/orders/{order['id']}")
    assert again.status_code == 400

    missing = await client.delete("/api/trade/orders/ord-nope")
    assert missing.status_code == 404


async def test_invalid_orders_are_refused(
    client: AsyncClient, quotes: SyntheticQuoteProvider
) -> None:
    """The request contract is enforced before anything reaches the simulator."""
    assert (
        await client.post("/api/trade/orders", json={"symbol": "AAPL", "side": "buy", "qty": 0})
    ).status_code == 422
    assert (
        await client.post(
            "/api/trade/orders", json={"symbol": "AAPL", "side": "buy", "qty": 1, "type": "limit"}
        )
    ).status_code == 400
    assert (await client.get("/api/trade/portfolio?broker=nasdaq")).status_code == 422


async def test_unknown_symbol_is_a_404_not_a_guess(
    client: AsyncClient, quotes: SyntheticQuoteProvider
) -> None:
    """With no quote there is no honest fill price, so the order is refused."""
    response = await client.post(
        "/api/trade/orders", json={"symbol": "ZZZZ", "side": "buy", "qty": 1}
    )
    assert response.status_code == 404


async def test_unconfigured_alpaca_is_refused_before_any_network_call(
    client: AsyncClient, quotes: SyntheticQuoteProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Routing to Alpaca without credentials answers 503, never a live order."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)

    response = await client.post(
        "/api/trade/orders",
        json={"symbol": "AAPL", "side": "buy", "qty": 1, "broker": "alpaca"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "not_configured"


async def test_accounts_and_reset(client: AsyncClient, quotes: SyntheticQuoteProvider) -> None:
    """The account is listed once it exists, and resetting restores its cash."""
    await client.post("/api/trade/orders", json={"symbol": "AAPL", "side": "buy", "qty": 5})

    accounts = (await client.get("/api/trade/accounts")).json()
    assert [a["id"] for a in accounts["accounts"]] == ["api-paper"]
    assert accounts["accounts"][0]["broker"] == "paper"

    reset = await client.post("/api/trade/paper/reset", json={})
    assert reset.status_code == 200
    assert reset.json()["cash"] == 50_000.0
    assert reset.json()["positions"] == []
