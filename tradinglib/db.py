"""SQLite application-state storage (SQLAlchemy 2.0).

Per ``docs/SPEC.md`` decision D4 storage is hybrid: relational application state
(universes, screens, hypotheses, runs, findings, audit records, settings) lives
in SQLite through this module, while time series live in Parquet + DuckDB via
``tradinglib.data.store``.

Models are declared by the modules that own them; they only need to inherit
from :class:`Base` and be imported before :func:`init_db` runs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, Table, create_engine, event, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from tradinglib.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base shared by every TradingLib ORM model."""


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _configure_sqlite(dbapi_connection: Any, _record: Any) -> None:
    """Apply the pragmas TradingLib relies on to every new SQLite connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def get_engine(database_url: str | None = None, *, echo: bool = False) -> Engine:
    """Return the process-wide SQLAlchemy engine, creating it on first use.

    Args:
        database_url: override URL. Defaults to the configured SQLite file at
            ``<data_dir>/app.db``.
        echo: emit SQL to the logger (debugging aid).
    """
    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        url = database_url or settings.database_url
        if url.startswith("sqlite") and ":memory:" not in url:
            Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            url,
            echo=echo,
            future=True,
            connect_args={"check_same_thread": False} if url.startswith("sqlite") else {},
        )
        if url.startswith("sqlite"):
            event.listen(_engine, "connect", _configure_sqlite)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
        logger.debug("database engine created for %s", url)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the session factory bound to the process-wide engine."""
    if _session_factory is None:
        get_engine()
    assert _session_factory is not None  # noqa: S101 - established by get_engine
    return _session_factory


def get_session() -> Session:
    """Create a new :class:`~sqlalchemy.orm.Session`; the caller closes it."""
    return get_session_factory()()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commits on success, rolls back on exception."""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(database_url: str | None = None) -> Engine:
    """Create the data directory, the engine and every declared table.

    Modules owning models must be imported before this call so their tables are
    registered on :attr:`Base.metadata`.
    """
    get_settings().ensure_dirs()
    engine = get_engine(database_url)
    Base.metadata.create_all(engine)
    logger.info("database initialised (%s tables)", len(Base.metadata.tables))
    return engine


def table_exists(name: str) -> bool:
    """Return whether a physical table called ``name`` exists in the database."""
    try:
        return inspect(get_engine()).has_table(name)
    except Exception:  # noqa: BLE001 - a missing/locked file is simply "no table"
        logger.debug("table_exists(%s) could not inspect the database", name, exc_info=True)
        return False


def ensure_columns(table: Table) -> list[str]:
    """Add any declared column that is missing from an existing physical table.

    SQLAlchemy's ``create_all`` never alters a table that already exists, so a
    model that gains a column would silently keep reading the old schema. This
    helper compares the declared columns against the live ones and issues the
    ``ALTER TABLE ... ADD COLUMN`` statements needed to close the gap. Only
    nullable columns (or columns with a server/py default) can be added this way,
    which is the only shape used in this codebase.

    Args:
        table: the SQLAlchemy table declared by the model.

    Returns:
        The names of the columns that were added (empty when already in sync).
    """
    engine = get_engine()
    inspector = inspect(engine)
    if not inspector.has_table(table.name):
        return []
    existing = {column["name"] for column in inspector.get_columns(table.name)}
    added: list[str] = []
    dialect = engine.dialect
    with engine.begin() as connection:
        for column in table.columns:
            if column.name in existing:
                continue
            ddl_type = column.type.compile(dialect=dialect)
            connection.exec_driver_sql(
                f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}'
            )
            added.append(column.name)
    if added:
        logger.info("migrated %s: added column(s) %s", table.name, ", ".join(added))
    return added


def dispose_engine() -> None:
    """Dispose of the engine and forget it. Intended for tests and shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
