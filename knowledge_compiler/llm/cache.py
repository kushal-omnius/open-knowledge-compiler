"""Content-addressed LLM cache (ADR-008): key = hash(template+version+model+input).

Staleness is impossible by construction — the key IS the input. Writes commit
EAGERLY on their own transaction (pipeline.md §6.2): a budget-halted or failed
run keeps its prepaid answers, making re-runs cheap. Safe because entries are
immutable and content-addressed; an orphaned entry is just a prepaid answer.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from knowledge_compiler.storage.schema import LLMCacheRow


def cache_key(template_id: str, template_version: str, model_id: str, input_hash: str) -> str:
    return hashlib.sha256(
        f"{template_id}\0{template_version}\0{model_id}\0{input_hash}".encode()).hexdigest()


class LLMCache:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def get(self, key: str) -> dict[str, Any] | None:
        with Session(self.engine) as session:
            row = session.execute(
                select(LLMCacheRow).where(LLMCacheRow.cache_key == key)).scalar_one_or_none()
            return None if row is None else row.output

    def put(self, key: str, template_id: str, template_version: str, model_id: str,
            output: dict[str, Any]) -> None:
        with Session(self.engine) as session, session.begin():
            if session.get(LLMCacheRow, key) is None:  # immutable: first write wins
                session.add(LLMCacheRow(cache_key=key, template_id=template_id,
                                        template_version=template_version,
                                        model_id=model_id, output=output))
