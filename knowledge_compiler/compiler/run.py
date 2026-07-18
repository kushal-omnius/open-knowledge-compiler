"""The compile runner (pipeline.md §2–3): kc compile --full, end to end.

Execution model: per-repo advisory lock for the whole run; staging writes
(run row, artifacts, facts) commit incrementally; the atomic commit (Persist)
is one transaction (pipeline.md §2 commit disciplines).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from knowledge_compiler import FACT_VOCABULARY_VERSION, KNOWLEDGE_MODEL_VERSION
from knowledge_compiler.collectors.git import GitCollector
from knowledge_compiler.compiler.diff import CompileScope, compute_diff
from knowledge_compiler.compiler.normalize import Thresholds, normalize
from knowledge_compiler.extractors.python_analyzer import PythonAnalyzer
from knowledge_compiler.ir import Fact
from knowledge_compiler.storage.db import make_engine, repo_lock_key
from knowledge_compiler.storage.persist import load_current_state, persist_compile
from knowledge_compiler.storage.schema import ArtifactRow, CompileRun, FactRow, Repository


class CompileError(Exception):
    pass


@dataclass
class CompileSummary:
    repo_slug: str
    compile_run_id: int
    commit_sha: str
    entities: int
    relationships: int
    added: int
    changed: int
    removed: int
    moved: int
    dirty: int
    warnings: list[str]
    wiki_dir: str | None = None
    wiki_pages_written: int = 0


def read_config(repo_dir: Path) -> dict:
    config_path = repo_dir / "kc.toml"
    if not config_path.is_file():
        raise CompileError(f"no kc.toml in {repo_dir} — run `kc init` first")
    return tomllib.loads(config_path.read_text(encoding="utf-8"))


def read_repo_config(repo_dir: Path) -> dict:
    return read_config(repo_dir)["repository"]


def compile_full(repo_dir: Path) -> CompileSummary:
    config = read_config(repo_dir)
    repo_slug = config["repository"]["slug"]
    wiki_dir = repo_dir / config.get("wiki", {}).get("output_dir", "kc-wiki")
    engine = make_engine()

    with Session(engine) as session:
        # whole-run per-repo serialization (session-level lock; freed on close/unlock)
        session.execute(text("SELECT pg_advisory_lock(:k)"), {"k": repo_lock_key(repo_slug)})
        try:
            return _compile_full_locked(session, repo_dir, repo_slug, wiki_dir)
        finally:
            session.rollback()  # drop any failed in-flight transaction before unlocking
            session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": repo_lock_key(repo_slug)})
            session.commit()


def _compile_full_locked(session: Session, repo_dir: Path, repo_slug: str,
                         wiki_dir: Path) -> CompileSummary:
    repo = session.execute(select(Repository).where(Repository.slug == repo_slug)).scalar_one_or_none()
    if repo is None:
        raise CompileError(f"repository '{repo_slug}' is not registered — run `kc init`")

    collector = GitCollector(repo_dir)
    commit_sha = collector.head_commit()

    # staging commit 1: the run row
    run = CompileRun(repo_id=repo.id, scope="full", commit_sha=commit_sha, status="running",
                     fact_vocabulary_version=FACT_VOCABULARY_VERSION,
                     knowledge_model_version=KNOWLEDGE_MODEL_VERSION)
    session.add(run)
    session.commit()

    try:
        # Collect -> staging commit 2
        artifacts = collector.collect_full()
        session.add_all(ArtifactRow(repo_id=repo.id, compile_run_id=run.id,
                                    artifact_type=a.artifact_type, source_ref=a.source_ref,
                                    content_hash=a.content_hash, content=a.content)
                        for a in artifacts)
        session.commit()

        # Extract -> staging commit 3
        facts: list[Fact] = PythonAnalyzer().analyze(artifacts)
        session.add_all(FactRow(repo_id=repo.id, compile_run_id=run.id, fact_type=f.fact_type,
                                payload=f.payload, content_hash=f.content_hash,
                                extraction=f.extraction.model_dump(),
                                artifact_ids=list(f.artifact_refs),
                                anchors=[a.model_dump() for a in f.anchors])
                        for f in facts)
        session.commit()

        # Normalize + Diff (pure)
        current = load_current_state(session, repo.id, repo_slug)
        candidate = normalize(facts, current, Thresholds(), repo_slug)
        scope = CompileScope(full=True, ran_families=frozenset({"deterministic"}))
        delta, dirty = compute_diff(candidate, current, scope)

        # Persist: THE atomic commit (ADR-003)
        persist_compile(session, repo.id, run, candidate, delta)
        run.finished_at = func.now()
        session.commit()
    except Exception as exc:
        session.rollback()
        run.status = "failed"
        run.error = str(exc)
        run.finished_at = func.now()
        session.commit()
        raise

    ops = [c.op for c in delta.entity_changes]
    summary = CompileSummary(
        repo_slug=repo_slug, compile_run_id=run.id, commit_sha=commit_sha,
        entities=len(candidate.entities), relationships=len(candidate.relationships),
        added=ops.count("added"), changed=ops.count("changed"),
        removed=ops.count("removed"), moved=ops.count("moved"),
        dirty=len(dirty), warnings=list(candidate.warnings),
    )

    # Emit (pipeline.md §3.6): never rolls back Persist; failures are warnings,
    # re-runnable from the delta.
    try:
        summary.wiki_pages_written = _emit_wiki(session, repo_slug, run, wiki_dir, dirty)
        summary.wiki_dir = str(wiki_dir)
    except Exception as exc:  # noqa: BLE001 — deliberate: emission must not fail the compile
        summary.warnings.append(f"wiki emission failed (compile state is intact): {exc}")
    return summary


def _emit_wiki(session: Session, repo_slug: str, run: CompileRun, wiki_dir: Path,
               dirty: set[str]) -> int:
    from knowledge_compiler.storage.schema import DeltaChangeRow
    from knowledge_compiler.wiki.emitter import RunDelta, WikiContext, WikiEmitter

    state = load_current_state(session, run.repo_id, repo_slug)

    recent_runs = session.execute(
        select(CompileRun).where(CompileRun.repo_id == run.repo_id,
                                 CompileRun.status == "succeeded")
        .order_by(CompileRun.id.desc()).limit(10)).scalars().all()
    recent = []
    for r in recent_runs:
        changes = session.execute(
            select(DeltaChangeRow.op, DeltaChangeRow.slug, DeltaChangeRow.entity_type)
            .where(DeltaChangeRow.compile_run_id == r.id)
            .order_by(DeltaChangeRow.slug)).all()
        recent.append(RunDelta(compile_run_id=r.id, commit_sha=r.commit_sha,
                               changes=tuple((op, slug, et) for op, slug, et in changes)))

    emitter = WikiEmitter(wiki_dir)
    ctx = WikiContext(repo_slug=repo_slug, compile_run_id=run.id, commit_sha=run.commit_sha)
    written = emitter.emit(state.entities, state.relationships, dirty, recent, ctx)
    return len(written)
