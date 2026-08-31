"""Database access (ADR-001: single PostgreSQL store).

Configuration (CLAUDE.md: externals via env, never hardcoded):
  KC_DATABASE_URL — SQLAlchemy URL. Default matches docker-compose.yml:
                    postgresql+psycopg://kc:kc@localhost:5432/kc_wiki
  KC_DB_CONNECT_TIMEOUT_SECONDS — bound on how long a connection attempt may
                    hang before failing (default 120s / 2 minutes) — the
                    common real-world case is an instant refused connection
                    (Docker/Postgres down), but a black-holed network path
                    can otherwise hang far longer than any caller should wait.
"""

from __future__ import annotations

import os
import zlib
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

DEFAULT_DATABASE_URL = "postgresql+psycopg://kc:kc@localhost:5432/kc_wiki"
DEFAULT_CONNECT_TIMEOUT_SECONDS = 120


class DatabaseUnavailableError(Exception):
    """Postgres could not be reached — translated from a raw connection
    failure/timeout into an actionable message. Callers translate this into
    their own idiom (CompileError, click.ClickException, ...)."""


def database_url() -> str:
    return os.environ.get("KC_DATABASE_URL", DEFAULT_DATABASE_URL)


def connect_timeout_seconds() -> int:
    return int(os.environ.get("KC_DB_CONNECT_TIMEOUT_SECONDS", DEFAULT_CONNECT_TIMEOUT_SECONDS))


def connect_args() -> dict:
    """psycopg connect() parameter — bounds the TCP/auth handshake, not
    query execution. Shared with alembic/env.py so migrations get the same
    bound as every other entry point."""
    return {"connect_timeout": connect_timeout_seconds()}


def make_engine(url: str | None = None) -> Engine:
    return create_engine(url or database_url(), connect_args=connect_args())


def check_connection(engine: Engine) -> None:
    """Fail fast and clearly if Postgres is unreachable, instead of letting a
    raw driver traceback (or, absent connect_args, an indefinite hang) surface
    from whatever query happens to run first."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except (OperationalError, OSError) as exc:
        target = engine.url.render_as_string(hide_password=True)
        raise DatabaseUnavailableError(
            f"could not connect to Postgres at {target} "
            f"(waited up to {connect_timeout_seconds()}s) — is Docker running? "
            f"Try: docker compose up -d"
        ) from exc


def repo_lock_key(repo_slug: str) -> int:
    """Stable 32-bit advisory-lock key for a repository (ADR-002 serialization).

    Derived from the slug (not the DB id) so the lock can be taken before any row exists.
    """
    return zlib.crc32(repo_slug.encode("utf-8"))


@contextmanager
def advisory_lock(session: Session, repo_slug: str) -> Iterator[None]:
    """Per-repo compile serialization (pipeline.md §2). Blocks until acquired;
    released with the session's transaction/connection."""
    key = repo_lock_key(repo_slug)
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    yield
