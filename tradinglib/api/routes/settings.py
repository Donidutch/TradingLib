"""Application settings endpoints (``docs/SPEC.md`` section 5).

``GET /api/settings`` returns the stored preferences (theme, density,
timezone, benchmark, default universe), the choices the Settings page renders
in its selects, and an honest per-provider credential status. ``PUT`` writes a
partial update and *applies* it:

* ``default_universe`` is pushed into the universe registry, so it becomes the
  active universe the datastrip, tape and screener actually use;
* API keys are exported into the process environment and
  :func:`tradinglib.config.reload_settings` is called, so
  :meth:`tradinglib.config.Settings.provider_status` flips for real - the readback
  in the UI is the resolved configuration, not an echo of what was posted.

API keys are **write-only**: a value can be posted and it can be cleared, but
no response in this module ever returns one. The readback is limited to
"configured / not configured", where the value came from (stored, environment
or the TOML file) and when it was last written.

Secrets are persisted in the application's SQLite database in clear text -
exactly as they already live in ``tradinglib.toml`` or in the environment. That is
stated here rather than implied: this file adds no encryption, and the database
file is only as private as the directory it sits in.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Annotated, Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from tradinglib.api.deps import get_universes, translate_errors
from tradinglib.config import Settings, get_settings, reload_settings
from tradinglib.data.universe import UniverseRegistry
from tradinglib.db import Base, get_engine, session_scope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

Universes = Annotated[UniverseRegistry, Depends(get_universes)]

#: Themes the injected Settings stylesheet implements.
THEMES: Final[tuple[str, ...]] = ("dark", "light")

#: Display densities the UI implements.
DENSITIES: Final[tuple[str, ...]] = ("compact", "comfortable")

#: Timezones offered in the select. Any valid IANA name is accepted by ``PUT``;
#: these are only the shortcuts the page lists (the REFERENCE offers the first
#: two, and the rest are the venues TradingLib's providers actually quote in).
TIMEZONE_CHOICES: Final[tuple[str, ...]] = (
    "America/New_York",
    "Europe/Copenhagen",
    "UTC",
    "Europe/London",
    "Asia/Singapore",
    "Asia/Tokyo",
)

#: Benchmark shortcuts. Every one is a real, priceable ETF ticker - a benchmark
#: that cannot be fetched would break every backtest that used it, so the list
#: holds only instruments the market provider can actually return bars for.
BENCHMARK_CHOICES: Final[tuple[tuple[str, str], ...]] = (
    ("SPY", "S&P 500 (SPY)"),
    ("QQQ", "Nasdaq-100 (QQQ)"),
    ("IWM", "Russell 2000 (IWM)"),
    ("ACWI", "MSCI ACWI (ACWI)"),
    ("VT", "FTSE Global All Cap (VT)"),
)

#: provider -> (label, environment variables written when a key is stored).
#: The first name is the canonical one; the others are the aliases
#: :func:`tradinglib.config.load_settings` also reads, kept in step so a stored key
#: wins no matter which alias the environment happens to carry.
KEY_PROVIDERS: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "anthropic": ("Anthropic API key", ("ANTHROPIC_API_KEY", "TRADINGLIB_ANTHROPIC_API_KEY")),
    "openai": ("OpenAI API key", ("OPENAI_API_KEY", "TRADINGLIB_OPENAI_API_KEY")),
    "alpaca_api_key": (
        "Alpaca key id",
        ("ALPACA_API_KEY", "APCA_API_KEY_ID", "TRADINGLIB_ALPACA_API_KEY"),
    ),
    "alpaca_secret_key": (
        "Alpaca secret key",
        ("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY", "TRADINGLIB_ALPACA_SECRET_KEY"),
    ),
    "fred": ("FRED API key", ("FRED_API_KEY", "TRADINGLIB_FRED_API_KEY")),
}

#: Which stored key(s) each reported provider depends on, and the note the UI
#: shows next to its status.
PROVIDER_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "anthropic": ("anthropic",),
    "openai": ("openai",),
    "alpaca": ("alpaca_api_key", "alpaca_secret_key"),
    "fred": ("fred",),
    "yfinance": (),
    "edgar": (),
    "trends": (),
}


class AppSettingRow(Base):
    """One stored preference. Values are text; the API owns their meaning."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class AppSecretRow(Base):
    """One stored credential. Never serialised into any response body."""

    __tablename__ = "app_secrets"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)


class SettingsUpdate(BaseModel):
    """Body of ``PUT /api/settings``. Omitted fields are left untouched."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    theme: str | None = None
    density: str | None = None
    timezone: str | None = None
    benchmark: str | None = None
    default_universe: str | None = Field(default=None, validation_alias="defaultUniverse")
    #: provider -> new value. An empty string clears the stored credential.
    keys: dict[str, str] = Field(default_factory=dict)


#: Tables this module owns; created on demand like every other domain module's.
_OWNED_TABLES = (AppSettingRow.__table__, AppSecretRow.__table__)


def ensure_schema() -> None:
    """Create the settings tables if they do not exist yet."""
    Base.metadata.create_all(get_engine(), tables=list(_OWNED_TABLES))


def _now() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------
def read_preferences() -> dict[str, tuple[str, datetime]]:
    """Return every stored preference as ``key -> (value, updated_at)``."""
    ensure_schema()
    with session_scope() as session:
        rows = session.scalars(select(AppSettingRow)).all()
        return {row.key: (row.value, row.updated_at) for row in rows}


def write_preference(key: str, value: str) -> None:
    """Insert or update one preference."""
    ensure_schema()
    with session_scope() as session:
        row = session.get(AppSettingRow, key)
        if row is None:
            session.add(AppSettingRow(key=key, value=value, updated_at=_now()))
        else:
            row.value = value
            row.updated_at = _now()


def read_secrets() -> dict[str, tuple[str, datetime]]:
    """Return every stored credential as ``provider -> (value, updated_at)``.

    Only :func:`apply_persisted_settings` and the ``PUT`` handler call this;
    the values never leave the process through an HTTP response.
    """
    ensure_schema()
    with session_scope() as session:
        rows = session.scalars(select(AppSecretRow)).all()
        return {row.provider: (row.value, row.updated_at) for row in rows}


def write_secret(provider: str, value: str) -> None:
    """Store a credential, or delete the row when ``value`` is empty."""
    ensure_schema()
    with session_scope() as session:
        row = session.get(AppSecretRow, provider)
        if not value:
            if row is not None:
                session.delete(row)
            return
        if row is None:
            session.add(AppSecretRow(provider=provider, value=value, updated_at=_now()))
        else:
            row.value = value
            row.updated_at = _now()


# ---------------------------------------------------------------------------
# applying
# ---------------------------------------------------------------------------
#: Environment variables this module has exported, and the value it wrote.
#: Only these are ever removed again, so clearing a credential in the UI can
#: never delete one the operator exported in their own shell.
_EXPORTED: dict[str, str] = {}


def apply_persisted_settings() -> Settings:
    """Export stored credentials into the environment and reload the settings.

    Called once by the application lifespan and again after every ``PUT``, so
    a key typed into the Settings page has the same effect as one exported in
    the shell before the server started. A stored key deliberately takes
    precedence over the environment: it is the most recent explicit choice the
    operator made.

    Returns:
        The freshly resolved process-wide settings.
    """
    try:
        secrets = read_secrets()
    except Exception:  # noqa: BLE001 - a storage fault must not stop the server
        logger.exception("settings: stored credentials could not be read")
        secrets = {}
    wanted: dict[str, str] = {}
    for provider, (value, _stamp) in secrets.items():
        spec = KEY_PROVIDERS.get(provider)
        if spec is None:
            logger.warning("settings: ignoring stored credential for unknown provider %r", provider)
            continue
        for env_name in spec[1]:
            wanted[env_name] = value

    # Withdraw the exports whose stored credential has been cleared, but only
    # when the environment still holds exactly what we put there.
    for env_name, written in list(_EXPORTED.items()):
        if env_name in wanted:
            continue
        if os.environ.get(env_name) == written:
            del os.environ[env_name]
        del _EXPORTED[env_name]

    for env_name, value in wanted.items():
        os.environ[env_name] = value
        _EXPORTED[env_name] = value
    return reload_settings()


def _validate_timezone(name: str) -> str:
    """Return ``name`` when it is a real IANA timezone.

    Raises:
        HTTPException: 400 when the zone is unknown to the system database.
    """
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "unknown_timezone", "timezone": name, "detail": str(exc)},
        ) from exc
    return name


def _validate_choice(field: str, value: str, choices: tuple[str, ...]) -> str:
    """Return ``value`` when it is one of ``choices``, else answer 400."""
    if value not in choices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_value",
                "field": field,
                "value": value,
                "allowed": list(choices),
            },
        )
    return value


def _validate_benchmark(value: str) -> str:
    """Normalise a benchmark ticker (uppercase, 1-10 symbol characters)."""
    symbol = value.strip().upper()
    if not symbol or len(symbol) > 10 or not all(c.isalnum() or c in ".-" for c in symbol):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_value",
                "field": "benchmark",
                "value": value,
                "detail": "a benchmark is a ticker such as SPY",
            },
        )
    return symbol


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------
def _key_status(settings: Settings, secrets: dict[str, tuple[str, datetime]]) -> list[dict[str, Any]]:
    """Describe each credential slot without ever revealing its value."""
    resolved = {
        "anthropic": settings.anthropic_api_key,
        "openai": settings.openai_api_key,
        "alpaca_api_key": settings.alpaca_api_key,
        "alpaca_secret_key": settings.alpaca_secret_key,
        "fred": settings.fred_api_key,
    }
    out: list[dict[str, Any]] = []
    for provider, (label, env_names) in KEY_PROVIDERS.items():
        stored = secrets.get(provider)
        value = resolved.get(provider)
        if stored is not None:
            source = "stored"
        elif value:
            source = "environment" if os.environ.get(env_names[0]) else "config-file"
        else:
            source = None
        out.append(
            {
                "provider": provider,
                "label": label,
                "env": env_names[0],
                "configured": bool(value),
                "source": source,
                "updated_at": stored[1].isoformat() if stored is not None else None,
            }
        )
    return out


def _provider_status(settings: Settings) -> list[dict[str, Any]]:
    """Return the provider readback, with the raw masked keys removed."""
    out: list[dict[str, Any]] = []
    for name, info in settings.provider_status().items():
        entry = {k: v for k, v in info.items() if k != "key"}
        entry["provider"] = name
        entry["keys"] = list(PROVIDER_KEYS.get(name, ()))
        out.append(entry)
    return out


def build_payload(registry: UniverseRegistry | None) -> dict[str, Any]:
    """Assemble the ``/api/settings`` body from storage and the live settings."""
    settings = get_settings()
    prefs = read_preferences()
    secrets = read_secrets()

    active_universe: str | None = None
    universes: list[dict[str, Any]] = []
    if registry is not None:
        try:
            active_universe = registry.active_universe_id()
            universes = [
                {"id": u.id, "name": u.name, "builtin": u.builtin}
                for u in registry.list_universes(include_members=False)
            ]
        except Exception:  # noqa: BLE001 - reported, never fabricated
            logger.exception("settings: the universe registry could not be read")

    def pref(key: str, fallback: str) -> str:
        entry = prefs.get(key)
        return entry[0] if entry is not None else fallback

    stored_universe = prefs.get("default_universe")
    default_universe = stored_universe[0] if stored_universe else (
        active_universe or settings.default_universe
    )

    return {
        "theme": pref("theme", "dark"),
        "density": pref("density", "compact"),
        "timezone": pref("timezone", settings.display_timezone),
        "benchmark": pref("benchmark", settings.default_benchmark),
        "default_universe": default_universe,
        "active_universe": active_universe,
        "sources": {
            key: ("stored" if key in prefs else "default")
            for key in ("theme", "density", "timezone", "benchmark", "default_universe")
        },
        "updated_at": max((stamp for _v, stamp in prefs.values()), default=None),
        "options": {
            "themes": list(THEMES),
            "densities": list(DENSITIES),
            "timezones": list(TIMEZONE_CHOICES),
            "benchmarks": [{"symbol": s, "label": label} for s, label in BENCHMARK_CHOICES],
            "universes": universes,
        },
        "keys": _key_status(settings, secrets),
        "providers": _provider_status(settings),
        "budgets": settings.budgets.as_dict(),
        "config_path": str(settings.config_path) if settings.config_path else None,
        "secret_storage": (
            "Credentials are stored in clear text in the application database "
            f"({settings.db_path}) and exported into this process's environment. "
            "They are never returned by the API."
        ),
    }


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@router.get("", summary="Application settings")
async def get_app_settings(request: Request) -> dict[str, Any]:
    """Return the stored preferences, the select options and key status."""
    registry = getattr(request.app.state, "universe_registry", None)
    with translate_errors("settings"):
        return build_payload(registry)


@router.put("", summary="Update application settings")
async def update_app_settings(
    request: Request,
    universes: Universes,
    body: Annotated[SettingsUpdate, Body()],
) -> dict[str, Any]:
    """Apply a partial settings update and return the new state.

    Raises:
        HTTPException: 400 for an unknown value (theme, density, timezone,
            benchmark) or a universe id that does not exist.
    """
    applied: list[str] = []

    if body.theme is not None:
        write_preference("theme", _validate_choice("theme", body.theme, THEMES))
        applied.append("theme")
    if body.density is not None:
        write_preference("density", _validate_choice("density", body.density, DENSITIES))
        applied.append("density")
    if body.timezone is not None:
        write_preference("timezone", _validate_timezone(body.timezone))
        applied.append("timezone")
    if body.benchmark is not None:
        write_preference("benchmark", _validate_benchmark(body.benchmark))
        applied.append("benchmark")

    if body.default_universe is not None:
        universe_id = body.default_universe.strip()
        with translate_errors("universe"):
            # Raises KeyError -> 404 when the id is unknown, so the preference
            # can never point at a universe that does not exist.
            universes.set_active(universe_id)
        write_preference("default_universe", universe_id)
        applied.append("default_universe")

    key_changes: list[str] = []
    for provider, value in body.keys.items():
        if provider not in KEY_PROVIDERS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "unknown_provider",
                    "provider": provider,
                    "allowed": sorted(KEY_PROVIDERS),
                },
            )
        write_secret(provider, str(value or "").strip())
        key_changes.append(provider)

    if key_changes:
        settings = apply_persisted_settings()
        # Subsystems that captured the settings object at boot must see the
        # new one; anything that resolved a client at startup (the Alpaca
        # stream) needs a restart, which the payload says out loud.
        request.app.state.settings = settings
        applied.append("keys")

    payload = build_payload(universes)
    payload["applied"] = applied
    payload["keys_changed"] = key_changes
    if key_changes:
        payload["restart_required"] = [
            "alpaca_stream" if p.startswith("alpaca") else p for p in key_changes
        ]
        payload["restart_detail"] = (
            "New credentials are in effect for every request made from now on. "
            "The Alpaca market stream is opened at startup, so it only picks up "
            "new Alpaca credentials after a server restart."
        )
    return payload
