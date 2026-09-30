"""Runtime configuration for TradingLib.

Settings are resolved from two sources, in increasing order of precedence:

1. an optional TOML file (``TradingLib/tradinglib.toml`` by default, override with the
   ``TRADINGLIB_CONFIG`` environment variable);
2. environment variables.

Nothing here invents credentials: a key that is absent stays ``None`` and the
owning subsystem must report itself as "not configured" rather than pretending
to work. :meth:`Settings.provider_status` exposes exactly that information for
``/api/agents/config`` and the Settings page.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

#: Repository root - ``<root>/tradinglib/config.py`` -> ``<root>``.
ROOT_DIR: Final[Path] = Path(__file__).resolve().parent.parent

#: Default location of the optional TOML configuration file.
DEFAULT_CONFIG_PATH: Final[Path] = ROOT_DIR / "tradinglib.toml"

#: Default SEC EDGAR User-Agent. EDGAR rejects requests without a contactable
#: identity, so this is a real (overridable) default rather than a placeholder.
DEFAULT_EDGAR_USER_AGENT: Final[str] = "TradingLib Research (tradinglib-research@example.com)"


def _env(*names: str) -> str | None:
    """Return the first non-empty environment variable among ``names``."""
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean environment variable (``1/true/yes/on`` are true)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    """Read a float environment variable, falling back to ``default``."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    """Read an int environment variable, falling back to ``default``."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _dig(data: dict[str, Any], *path: str) -> Any:
    """Walk nested TOML tables, returning ``None`` when a key is missing."""
    node: Any = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def mask_secret(value: str | None) -> str | None:
    """Mask a secret for display: keep the last four characters only."""
    if not value:
        return None
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


@dataclass(frozen=True, slots=True)
class Budgets:
    """LLM spend budgets, in US dollars. Enforced by ``tradinglib.agents.runner``.

    The per-run default is sized from a measured run rather than a round
    number: one stage of the six-stage Full Research Cycle costs roughly
    $0.20-$1.30 through the Claude Code CLI, whose large cached system context
    dominates the first uncached call. A $1 ceiling therefore stopped the
    pipeline inside its first stage. Lower it in Agent Center → Configuration
    if a tighter cap is wanted; the ceiling is enforced either way.
    """

    per_run_usd: float = 12.00
    daily_usd: float = 50.00
    monthly_usd: float = 250.00

    def as_dict(self) -> dict[str, float]:
        """Return the budgets as a JSON-serialisable mapping."""
        return {
            "per_run_usd": self.per_run_usd,
            "daily_usd": self.daily_usd,
            "monthly_usd": self.monthly_usd,
        }


#: Defaults used when neither the TOML file nor the environment sets a budget.
DEFAULT_BUDGETS: Final[Budgets] = Budgets()


@dataclass(frozen=True, slots=True)
class Settings:
    """Fully resolved TradingLib settings.

    Instances are immutable; call :func:`reload_settings` after changing the
    environment or the TOML file.
    """

    # --- paths -----------------------------------------------------------
    root_dir: Path = ROOT_DIR
    data_dir: Path = ROOT_DIR / "data"
    ui_dir: Path = ROOT_DIR / "ui"
    config_path: Path | None = None

    # --- credentials (None == not configured) ----------------------------
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    alpaca_api_key: str | None = None
    alpaca_secret_key: str | None = None
    alpaca_paper: bool = True
    alpaca_data_feed: str = "iex"
    fred_api_key: str | None = None
    edgar_user_agent: str = DEFAULT_EDGAR_USER_AGENT

    # --- budgets ---------------------------------------------------------
    budgets: Budgets = field(default_factory=Budgets)

    # --- server / defaults ----------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8200
    log_level: str = "info"
    default_universe: str = "us-quality-50"
    default_benchmark: str = "SPY"
    display_timezone: str = "America/New_York"

    # --- derived paths ---------------------------------------------------
    @property
    def db_path(self) -> Path:
        """Filesystem path of the SQLite application-state database."""
        return self.data_dir / "app.db"

    @property
    def database_url(self) -> str:
        """SQLAlchemy URL for the application-state database."""
        return f"sqlite+pysqlite:///{self.db_path}"

    @property
    def ts_dir(self) -> Path:
        """Root directory of the Parquet + DuckDB time-series store.

        This is the directory ``tradinglib.data.store.TimeSeriesStore`` actually
        writes (``<data_dir>/ts``); the name matches its ``TS_DIRNAME``.
        """
        return self.data_dir / "ts"

    @property
    def parquet_dir(self) -> Path:
        """Deprecated alias of :attr:`ts_dir`.

        Kept so existing callers keep working, but it resolves to the same
        directory the store uses - there is only ever one Parquet root.
        """
        return self.ts_dir

    @property
    def cache_dir(self) -> Path:
        """Directory used by ``tradinglib.data.cache`` for provider responses."""
        return self.data_dir / "cache"

    @property
    def artifacts_dir(self) -> Path:
        """Directory holding agent-run artifacts."""
        return self.data_dir / "artifacts"

    def ensure_dirs(self) -> None:
        """Create every directory TradingLib writes to (idempotent)."""
        for path in (
            self.data_dir,
            self.ts_dir,
            self.cache_dir,
            self.artifacts_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    # --- capability reporting -------------------------------------------
    def provider_status(self) -> dict[str, dict[str, Any]]:
        """Report, honestly, which credentialed integrations are configured.

        Keyless providers (yfinance, SEC EDGAR, FRED CSV, Google Trends) are
        reported as configured because they genuinely need no credentials.
        """
        return {
            "anthropic": {
                "configured": self.anthropic_api_key is not None,
                "key": mask_secret(self.anthropic_api_key),
                "note": "claude-agent-sdk may also use local Claude Code CLI auth; "
                "verified at runtime by tradinglib.agents.providers.",
            },
            "openai": {
                "configured": self.openai_api_key is not None,
                "key": mask_secret(self.openai_api_key),
            },
            "alpaca": {
                "configured": bool(self.alpaca_api_key and self.alpaca_secret_key),
                "key": mask_secret(self.alpaca_api_key),
                "paper": self.alpaca_paper,
                "feed": self.alpaca_data_feed,
            },
            "fred": {
                "configured": True,
                "key": mask_secret(self.fred_api_key),
                "note": "fredgraph CSV endpoints need no key; fredapi is used when a key is set.",
            },
            "yfinance": {"configured": True},
            "edgar": {"configured": True, "user_agent": self.edgar_user_agent},
            "trends": {"configured": True},
        }


def _load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML config file, returning ``{}`` when it does not exist."""
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_settings(config_path: str | os.PathLike[str] | None = None) -> Settings:
    """Resolve :class:`Settings` from the TOML file and the environment.

    Args:
        config_path: explicit TOML path. Defaults to ``$TRADINGLIB_CONFIG`` and
            then to ``<root>/tradinglib.toml``.

    Returns:
        A fully resolved, immutable settings object.
    """
    path = Path(config_path) if config_path else Path(_env("TRADINGLIB_CONFIG") or DEFAULT_CONFIG_PATH)
    doc = _load_toml(path)

    data_dir_raw = _env("TRADINGLIB_DATA_DIR") or _dig(doc, "data", "dir") or str(ROOT_DIR / "data")
    data_dir = Path(data_dir_raw)
    if not data_dir.is_absolute():
        data_dir = (ROOT_DIR / data_dir).resolve()

    ui_dir_raw = _env("TRADINGLIB_UI_DIR") or _dig(doc, "server", "ui_dir") or str(ROOT_DIR / "ui")
    ui_dir = Path(ui_dir_raw)
    if not ui_dir.is_absolute():
        ui_dir = (ROOT_DIR / ui_dir).resolve()

    budgets = Budgets(
        per_run_usd=_env_float(
            "TRADINGLIB_BUDGET_PER_RUN_USD",
            float(_dig(doc, "budgets", "per_run_usd") or DEFAULT_BUDGETS.per_run_usd),
        ),
        daily_usd=_env_float(
            "TRADINGLIB_BUDGET_DAILY_USD",
            float(_dig(doc, "budgets", "daily_usd") or DEFAULT_BUDGETS.daily_usd),
        ),
        monthly_usd=_env_float(
            "TRADINGLIB_BUDGET_MONTHLY_USD",
            float(_dig(doc, "budgets", "monthly_usd") or DEFAULT_BUDGETS.monthly_usd),
        ),
    )

    return Settings(
        root_dir=ROOT_DIR,
        data_dir=data_dir,
        ui_dir=ui_dir,
        config_path=path if path.is_file() else None,
        anthropic_api_key=_env("ANTHROPIC_API_KEY", "TRADINGLIB_ANTHROPIC_API_KEY")
        or _dig(doc, "keys", "anthropic"),
        openai_api_key=_env("OPENAI_API_KEY", "TRADINGLIB_OPENAI_API_KEY")
        or _dig(doc, "keys", "openai"),
        alpaca_api_key=_env("ALPACA_API_KEY", "APCA_API_KEY_ID", "TRADINGLIB_ALPACA_API_KEY")
        or _dig(doc, "keys", "alpaca", "key_id"),
        alpaca_secret_key=_env(
            "ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY", "TRADINGLIB_ALPACA_SECRET_KEY"
        )
        or _dig(doc, "keys", "alpaca", "secret_key"),
        alpaca_paper=_env_bool(
            "TRADINGLIB_ALPACA_PAPER",
            bool(_dig(doc, "keys", "alpaca", "paper") if _dig(doc, "keys", "alpaca", "paper") is not None else True),
        ),
        alpaca_data_feed=_env("TRADINGLIB_ALPACA_FEED")
        or str(_dig(doc, "keys", "alpaca", "feed") or "iex"),
        fred_api_key=_env("FRED_API_KEY", "TRADINGLIB_FRED_API_KEY") or _dig(doc, "keys", "fred"),
        edgar_user_agent=_env("TRADINGLIB_EDGAR_USER_AGENT")
        or str(_dig(doc, "edgar", "user_agent") or DEFAULT_EDGAR_USER_AGENT),
        budgets=budgets,
        host=_env("TRADINGLIB_HOST") or str(_dig(doc, "server", "host") or "127.0.0.1"),
        port=_env_int("TRADINGLIB_PORT", int(_dig(doc, "server", "port") or 8200)),
        log_level=_env("TRADINGLIB_LOG_LEVEL") or str(_dig(doc, "server", "log_level") or "info"),
        default_universe=_env("TRADINGLIB_DEFAULT_UNIVERSE")
        or str(_dig(doc, "defaults", "universe") or "us-quality-50"),
        default_benchmark=_env("TRADINGLIB_DEFAULT_BENCHMARK")
        or str(_dig(doc, "defaults", "benchmark") or "SPY"),
        display_timezone=_env("TRADINGLIB_TIMEZONE")
        or str(_dig(doc, "defaults", "timezone") or "America/New_York"),
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings, loading them on first use."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reload_settings(config_path: str | os.PathLike[str] | None = None) -> Settings:
    """Re-resolve and replace the process-wide settings. Returns the new value."""
    global _settings
    _settings = load_settings(config_path)
    return _settings
