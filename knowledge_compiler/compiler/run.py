"""The compile runner (pipeline.md §2–4): full, PR-incremental, and reconcile.

Execution model: per-repo advisory lock for the whole run; staging writes
(run row, artifacts, facts) commit incrementally; the atomic commit (Persist)
is one transaction. Every incremental compile reconciles first (ADR-002).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from knowledge_compiler import FACT_VOCABULARY_VERSION, KNOWLEDGE_MODEL_VERSION
from knowledge_compiler.collectors.forge import ForgeGateway, MergedPR
from knowledge_compiler.collectors.git import GitCollector
from knowledge_compiler.compiler.diff import CompileScope, compute_diff
from knowledge_compiler.compiler.normalize import Thresholds, normalize
from knowledge_compiler.extractors.python_analyzer import PythonAnalyzer
from knowledge_compiler.extractors.typescript_analyzer import TypeScriptAnalyzer


def _extract(artifacts: list[Artifact]) -> list[Fact]:
    """The deterministic Extract stage: every active analyzer over the staged
    artifacts (each filters to its own extensions). Analyzer routing by extension
    is configuration-grade, not architecture (ADR-006)."""
    facts: list[Fact] = []
    for analyzer in (PythonAnalyzer(), TypeScriptAnalyzer()):
        facts.extend(analyzer.analyze(artifacts))
    return facts
from knowledge_compiler.ir import Artifact, Extraction, Fact, content_hash
from knowledge_compiler.storage.db import make_engine, repo_lock_key
from knowledge_compiler.storage.persist import load_current_state, persist_compile
from knowledge_compiler.storage.schema import ArtifactRow, CompileRun, FactRow, Repository

_FORGE_EXTRACTION = Extraction(method="deterministic", extractor="forge-collector",
                               extractor_version="0.1")


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
    pr_number: int | None = None
    wiki_dir: str | None = None
    wiki_pages_written: int = 0
    published_sha: str | None = None
    pushed: bool = False


def read_config(repo_dir: Path) -> dict:
    config_path = repo_dir / "kc.toml"
    if not config_path.is_file():
        raise CompileError(f"no kc.toml in {repo_dir} — run `kc init` first")
    return tomllib.loads(config_path.read_text(encoding="utf-8"))


def read_repo_config(repo_dir: Path) -> dict:
    return read_config(repo_dir)["repository"]


# --- entry points ---------------------------------------------------------------


def compile_full(repo_dir: Path, no_llm: bool = False, llm_provider=None) -> CompileSummary:
    with _locked_session(repo_dir) as (session, repo, ctx):
        ctx.update(no_llm=no_llm, llm_provider=llm_provider)
        return _compile_one(session, repo, ctx, pr=None)


def reconcile(repo_dir: Path, gateway: ForgeGateway, expect_pr: int | None = None,
              no_llm: bool = False, llm_provider=None) -> list[CompileSummary]:
    """Process merged PRs after the watermark, in merge order, exactly once
    (pipeline.md §4). Every trigger heals prior gaps; `expect_pr` asserts the
    triggering PR was covered (already-processed => clean no-op)."""
    with _locked_session(repo_dir) as (session, repo, ctx):
        ctx.update(no_llm=no_llm, llm_provider=llm_provider)
        watermark = session.execute(
            select(func.max(CompileRun.merged_at)).where(
                CompileRun.repo_id == repo.id, CompileRun.status == "succeeded",
                CompileRun.merged_at.is_not(None))).scalar_one_or_none()

        prs = gateway.list_merged_prs(ctx["default_branch"], watermark)
        summaries: list[CompileSummary] = []
        covered = expect_pr is None or _already_succeeded(session, repo.id, expect_pr)
        for pr in prs:
            if _already_succeeded(session, repo.id, pr.number):
                continue  # idempotence (ADR-002)
            summaries.append(_compile_one(session, repo, ctx, pr=pr))
            covered = covered or pr.number == expect_pr
        if not covered:
            raise CompileError(
                f"PR #{expect_pr} was not found among merged PRs after the watermark — "
                f"is it merged into '{ctx['default_branch']}'?")
        return summaries


compile_pr = reconcile  # `kc compile --pr N` IS a reconcile with an expectation (ADR-002)


@dataclass
class VerifyReport:
    """pipeline.md §7: shadow full compile vs current state, matched via the
    ADR-004 cascade (Normalize), never slug equality."""

    equivalent: bool
    entity_divergences: list[tuple[str, str]]        # (op, slug) a full compile would produce
    relationship_divergences: int
    evidence_histogram: dict[str, int]               # cascade-rule usage (threshold tuning data)


def verify(repo_dir: Path) -> VerifyReport:
    """Shadow compile: Collect + Extract + Normalize + Diff — zero writes.
    Empty delta <=> incremental history is equivalent to a fresh full compile.
    The remedy for divergence is a real `kc compile --full` (slug-preserving)."""
    with _locked_session(repo_dir) as (session, repo, ctx):
        collector = GitCollector(ctx["repo_dir"])
        artifacts = collector.collect_full()
        facts = _extract(artifacts)
        current = load_current_state(session, repo.id, ctx["repo_slug"])
        candidate = normalize(facts, current, Thresholds(), ctx["repo_slug"])
        scope = CompileScope(full=True, ran_families=frozenset({"deterministic"}))
        delta, _ = compute_diff(candidate, current, scope)

        histogram: dict[str, int] = {}
        for ev in candidate.evidence.values():
            histogram[ev.rule] = histogram.get(ev.rule, 0) + 1

        return VerifyReport(
            equivalent=not delta.entity_changes and not delta.relationship_changes,
            entity_divergences=[(c.op, c.slug) for c in delta.entity_changes],
            relationship_divergences=len(delta.relationship_changes),
            evidence_histogram=dict(sorted(histogram.items())),
        )


def _already_succeeded(session: Session, repo_id: int, pr_number: int) -> bool:
    return session.execute(
        select(CompileRun.id).where(CompileRun.repo_id == repo_id,
                                    CompileRun.pr_number == pr_number,
                                    CompileRun.status == "succeeded").limit(1)
    ).scalar_one_or_none() is not None


# --- shared machinery --------------------------------------------------------------


from contextlib import contextmanager  # noqa: E402


@contextmanager
def _locked_session(repo_dir: Path):
    config = read_config(repo_dir)
    repo_slug = config["repository"]["slug"]
    ctx = {
        "repo_dir": repo_dir,
        "repo_slug": repo_slug,
        "default_branch": config["repository"].get("default_branch", "main"),
        "wiki_dir": repo_dir / config.get("wiki", {}).get("output_dir", "kc-wiki"),
        "config": config,
    }
    with Session(make_engine()) as session:
        session.execute(text("SELECT pg_advisory_lock(:k)"), {"k": repo_lock_key(repo_slug)})
        try:
            repo = session.execute(
                select(Repository).where(Repository.slug == repo_slug)).scalar_one_or_none()
            if repo is None:
                raise CompileError(f"repository '{repo_slug}' is not registered — run `kc init`")
            yield session, repo, ctx
        finally:
            session.rollback()
            session.execute(text("SELECT pg_advisory_unlock(:k)"),
                            {"k": repo_lock_key(repo_slug)})
            session.commit()


def _compile_one(session: Session, repo: Repository, ctx: dict,
                 pr: MergedPR | None) -> CompileSummary:
    collector = GitCollector(ctx["repo_dir"])

    if pr is None:
        commit_sha = collector.head_commit()
        in_scope: frozenset[str] = frozenset()
        full = True
    else:
        commit_sha = pr.merge_commit_sha
        full = False
        in_scope = frozenset({f.path for f in pr.files}
                             | {f.old_path for f in pr.files if f.old_path})

    run = CompileRun(repo_id=repo.id, scope="full" if pr is None else "pr",
                     pr_number=pr.number if pr else None,
                     merged_at=pr.merged_at if pr else None,
                     commit_sha=commit_sha, status="running",
                     fact_vocabulary_version=FACT_VOCABULARY_VERSION,
                     knowledge_model_version=KNOWLEDGE_MODEL_VERSION)
    session.add(run)
    session.commit()  # staging commit 1

    try:
        # Collect -> staging commit 2
        if pr is None:
            artifacts = collector.collect_full()
            extra_facts: list[Fact] = []
        else:
            live_paths = [f.path for f in pr.files if f.change != "removed"]
            artifacts = collector.collect_at_commit(commit_sha, live_paths)
            extra_facts = _pr_facts(pr)
        session.add_all(ArtifactRow(repo_id=repo.id, compile_run_id=run.id,
                                    artifact_type=a.artifact_type, source_ref=a.source_ref,
                                    content_hash=a.content_hash, content=a.content)
                        for a in artifacts)
        session.commit()

        # Extract -> staging commit 3 (deterministic first and always — ADR-006)
        facts = _extract(artifacts) + extra_facts
        llm_ran, llm_warnings = _extract_semantic(session, ctx, artifacts, facts)
        # degraded = the semantic layer is configured on but didn't run this compile
        # (--no-llm or provider failure) — pipeline.md §6.1
        run.degraded = _llm_configured(ctx) and not llm_ran
        session.add_all(FactRow(repo_id=repo.id, compile_run_id=run.id, fact_type=f.fact_type,
                                payload=f.payload, content_hash=f.content_hash,
                                extraction=f.extraction.model_dump(),
                                artifact_ids=list(f.artifact_refs),
                                anchors=[a.model_dump() for a in f.anchors])
                        for f in facts)
        session.commit()

        # Normalize + Diff (pure) -> Persist (THE atomic commit, ADR-003)
        families = frozenset({"deterministic"} | ({"llm"} if llm_ran else set()))
        scope = CompileScope(full=full, ran_families=families, in_scope_files=in_scope)
        current = load_current_state(session, repo.id, ctx["repo_slug"])
        candidate = normalize(facts, current, Thresholds(), ctx["repo_slug"])
        delta, dirty = compute_diff(candidate, current, scope)
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
        repo_slug=ctx["repo_slug"], compile_run_id=run.id, commit_sha=commit_sha,
        entities=len(candidate.entities), relationships=len(candidate.relationships),
        added=ops.count("added"), changed=ops.count("changed"),
        removed=ops.count("removed"), moved=ops.count("moved"),
        dirty=len(dirty), warnings=list(candidate.warnings) + llm_warnings,
        pr_number=pr.number if pr else None,
    )

    # Emit + Publish (pipeline.md §3.6): never roll back Persist; failures are warnings.
    try:
        summary.wiki_pages_written = _emit_wiki(session, ctx["repo_slug"], run,
                                                ctx["wiki_dir"], dirty)
        summary.wiki_dir = str(ctx["wiki_dir"])
    except Exception as exc:  # noqa: BLE001 — deliberate: emission must not fail the compile
        summary.warnings.append(f"wiki emission failed (compile state is intact): {exc}")
        return summary
    try:
        pub_cfg = _publisher_config(ctx["config"])
        if pub_cfg.enabled:
            from knowledge_compiler.wiki.publisher import GitBranchPublisher

            result = GitBranchPublisher(ctx["repo_dir"], pub_cfg).publish(
                ctx["wiki_dir"], f"kc: compile run {run.id} @ {commit_sha[:12]}")
            summary.published_sha = result.commit_sha if result.committed else None
            summary.pushed = result.pushed
    except Exception as exc:  # noqa: BLE001 — same contract as emission
        summary.warnings.append(f"wiki publish failed (compile state is intact): {exc}")
    return summary


def _llm_configured(ctx: dict) -> bool:
    return bool(ctx["config"].get("llm", {}).get("enabled", False))


def _llm_enabled(ctx: dict) -> bool:
    return _llm_configured(ctx) and not ctx.get("no_llm")


def _extract_semantic(session: Session, ctx: dict, artifacts: list[Artifact],
                      facts: list[Fact]) -> tuple[bool, list[str]]:
    """Run LLM extraction if configured (pipeline.md §6.1 semantics).

    Returns (llm_ran, warnings). Provider failure degrades the compile (warning,
    deterministic results kept); budget exhaustion fails the run resumably."""
    if not _llm_enabled(ctx):
        return False, []

    from knowledge_compiler.extractors.llm_extractor import (
        LLMBudgetExceeded, LLMSemanticExtractor,
    )
    from knowledge_compiler.llm.cache import LLMCache
    from knowledge_compiler.llm.provider import LLMProviderError, build_provider

    llm_cfg = ctx["config"].get("llm", {})
    try:
        provider = ctx.get("llm_provider") or build_provider(llm_cfg)
        modules = {f.payload["file"]: f.payload["path"]
                   for f in facts if f.fact_type == "component_observed"}
        symbols: dict[str, list[str]] = {}
        for f in facts:
            if f.fact_type == "symbol_observed":
                symbols.setdefault(f.payload["file"], []).append(f.payload["symbol_path"])
        extractor = LLMSemanticExtractor(
            provider=provider, cache=LLMCache(session.get_bind()),
            max_calls=llm_cfg.get("max_calls_per_run", 200),
            known_symbols=symbols, modules=modules)
        facts.extend(extractor.extract(artifacts))
        return True, list(extractor.warnings)
    except LLMBudgetExceeded as exc:
        raise CompileError(str(exc)) from exc  # failed-resumable (pipeline.md §6.2)
    except LLMProviderError as exc:
        return False, [f"LLM provider unavailable — compiled degraded (--no-llm semantics): {exc}"]


def _pr_facts(pr: MergedPR) -> list[Fact]:
    payload = {"number": pr.number, "title": pr.title, "body": pr.body,
               "merged_at": pr.merged_at.isoformat(), "merge_commit_sha": pr.merge_commit_sha,
               "files": sorted(f.path for f in pr.files)}
    facts = [Fact(fact_type="pr_observed", payload=payload,
                  artifact_refs=(f"pr:{pr.number}",), extraction=_FORGE_EXTRACTION,
                  content_hash=content_hash(payload))]
    for f in pr.files:
        if f.change == "renamed" and f.old_path:
            p = {"change": "renamed", "old_path": f.old_path, "new_path": f.path}
            facts.append(Fact(fact_type="source_change_observed", payload=p,
                              artifact_refs=(f"pr:{pr.number}",),
                              extraction=_FORGE_EXTRACTION, content_hash=content_hash(p)))
    return facts


def _publisher_config(config: dict):
    from knowledge_compiler.wiki.publisher import PublisherConfig

    section = config.get("publisher", {})
    return PublisherConfig(
        enabled=section.get("enabled", False),
        branch=section.get("branch", "knowledge/wiki"),
        remote=section.get("remote", "origin"),
        push=section.get("push", True),
    )


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
