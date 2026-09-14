from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class Database:
    """Engine + session factory wrapper."""

    def __init__(self, url: str, echo: bool = False) -> None:
        self.url = url
        connect_args: dict = {}
        if url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
            path = url.replace("sqlite:///", "", 1)
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(url, echo=echo, future=True, connect_args=connect_args)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def _pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.close()

        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def create_all(self) -> None:
        """Create tables directly (development convenience; use Alembic in production)."""
        from trading_agent.db import models  # noqa: F401  (register mappings)

        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
