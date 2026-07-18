"""Database access (ADR-001: single PostgreSQL store).

Configuration (CLAUDE.md: externals via env, never hardcoded):
  KC_DATABASE_URL — SQLAlchemy URL. Default matches docker-compose.yml:
                    postgresql+psycopg://kc:kc@localhost:5432/knowledge
"""

from __future__ import annotations

import os
import zlib
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

DEFAULT_DATABASE_URL = "postgresql+psycopg://kc:kc@localhost:5432/knowledge"


def database_url() -> str:
    return os.environ.get("KC_DATABASE_URL", DEFAULT_DATABASE_URL)


def make_engine(url: str | None = None) -> Engine:
    return create_engine(url or database_url())


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
