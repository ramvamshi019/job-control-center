"""
database.py
-----------
SQLite + SQLModel setup.

Beginner notes:
- One SQLite file holds everything (backend/data/jobs.db).
- `init_db()` creates tables if they don't exist. Safe to call repeatedly.
- `get_session()` is a FastAPI dependency that yields a DB session.
- For scripts, use `with session_scope() as session:` instead.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

# SQLite needs this flag to be used across threads (FastAPI + scripts).
_is_sqlite = settings.database_url.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}

engine = create_engine(
    settings.database_url,
    echo=False,            # set True to see raw SQL while debugging
    connect_args=_connect_args,
)


if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        """WAL lets the dashboard/API read while the live crawler writes.
        busy_timeout makes writers wait instead of erroring 'database is locked'."""
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=10000")  # 10s
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


def _ensure_sqlite_dir() -> None:
    """Make sure the folder for the SQLite file exists (e.g. backend/data/)."""
    url = settings.database_url
    if url.startswith("sqlite:///"):
        path = url.replace("sqlite:///", "", 1)
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)


def init_db() -> None:
    """Create all tables. Import models first so SQLModel knows about them."""
    _ensure_sqlite_dir()
    # IMPORTANT: importing models registers them with SQLModel.metadata.
    from app.models import company, job, application  # noqa: F401

    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency. Use:  session: Session = Depends(get_session)"""
    with Session(engine, expire_on_commit=False) as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """For scripts:  with session_scope() as session: ...

    expire_on_commit=False is CRITICAL for the parallel live watcher: worker
    threads read already-loaded Company attributes (name/career_url/ats_type)
    while the main thread commits each company. With the default (expire on
    commit) those reads would trigger a lazy reload on a just-committed session
    from another thread, which SQLite/SQLAlchemy rejects ("session is in
    'committed'/'prepared' state") — silently failing most crawls per cycle.
    Keeping attributes populated after commit makes the reads pure-Python and
    thread-safe (no SQL emitted from workers)."""
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
