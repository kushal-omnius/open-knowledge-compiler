"""The compile runner (pipeline.md §2–4): full, PR-incremental, and reconcile.

Execution model: per-repo advisory lock for the whole run; staging writes
(run row, artifacts, facts) commit incrementally; the atomic commit (Persist)
is one transaction. Every incremental compile reconciles first (ADR-002).
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from knowledge_compiler import (
    FACT_VOCABULARY_VERSION, KNOWLEDGE_MODEL_VERSION, OKF_SPEC_VERSION,
)
from knowledge_compiler.collectors.forge import CommitInfo, ForgeGateway, MergedPR
from knowledge_compiler.collectors.git import GitCollector
from knowledge_compiler.collectors.jira import build_jira_gateway
from knowledge_compiler.collectors.mutation import read_mutation_scores
from knowledge_compiler.compiler.diff import CompileScope, compute_diff
from knowledge_compiler.compiler.normalize import Thresholds, normalize
from knowledge_compiler.extractors.javascript_analyzer import JavaScriptAnalyzer
from knowledge_compiler.extractors.python_analyzer import PythonAnalyzer
from knowledge_compiler.extractors.typescript_analyzer import TypeScriptAnalyzer


def _extract(artifacts: list[Artifact]) -> list[Fact]:
    """The deterministic Extract stage: every active analyzer over the staged
    artifacts (each filters to its own extensions). Analyzer routing by extension
    is configuration-grade, not architecture (ADR-006)."""
    facts: list[Fact] = []
    for analyzer in (PythonAnalyzer(), TypeScriptAnalyzer(), JavaScriptAnalyzer()):
        facts.extend(analyzer.analyze(artifacts))
    return facts
from knowledge_compiler.ir import Artifact, Extraction, Fact, content_hash
from knowledge_compiler.storage.db import (
    DatabaseUnavailableError, check_connection, make_engine, repo_lock_key,
)
from knowledge_compiler.storage.persist import load_current_state, persist_compile
from knowledge_compiler.storage.schema import ArtifactRow, CompileRun, FactRow, Repository

_FORGE_EXTRACTION = Extraction(method="deterministic", extractor="forge-collector",
                               extractor_version="0.1")
_JIRA_EXTRACTION = Extraction(method="deterministic", extractor="jira-collector",
                              extractor_version="0.1")
_MUTATION_EXTRACTION = Extraction(method="deterministic", extractor="mutation-collector",
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
    entity_changes: list = None  # list[EntityChange] — kept as plain list to avoid circular import at dataclass level
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

# kc:external-key: compile-full
def compile_full(repo_dir: Path, no_llm: bool = False, llm_provider=None,
                 embedder=None, progress=None) -> CompileSummary:
    """progress: optional callable(stage: str, i: int, n: int, detail: str), called
    during the long-running LLM extraction and embeddings stages so a caller can
    report progress instead of waiting silently for the final summary."""
    with _locked_session(repo_dir) as (session, repo, ctx):
        ctx.update(no_llm=no_llm, llm_provider=llm_provider, embedder=embedder,
                   progress=progress)
        return _compile_one(session, repo, ctx, pr=None)


# kc:external-key: incremental-reconcile
def reconcile(repo_dir: Path, gateway: ForgeGateway, expect_pr: int | None = None,
              no_llm: bool = False, llm_provider=None, embedder=None,
              jira_gateway=None, progress=None) -> list[CompileSummary]:
    """Process merged PRs (and direct commits) after the watermark, in timestamp
    order, exactly once (pipeline.md §4).

    Two-pass strategy (BRAINSTORM-commit-reconcile.md Option C):
    - Pass 1: merged PRs via ForgeGateway.list_merged_prs (unchanged)
    - Pass 2: commits on default branch via ForgeGateway.list_commits; any
      commit whose SHA is already covered by a PR is skipped (dedup by SHA)
    Both streams are merged by timestamp and processed in order.

    `expect_pr` asserts the triggering PR was covered (already-processed => no-op).
    progress: see compile_full."""
    with _locked_session(repo_dir) as (session, repo, ctx):
        ctx.update(no_llm=no_llm, llm_provider=llm_provider, embedder=embedder,
                   jira_gateway=jira_gateway, progress=progress)

        # Unified watermark: max timestamp across PR-based and commit-based runs.
        watermark = session.execute(
            select(func.max(func.coalesce(CompileRun.commit_timestamp,
                                          CompileRun.merged_at))).where(
                CompileRun.repo_id == repo.id, CompileRun.status == "succeeded",
                func.coalesce(CompileRun.commit_timestamp,
                              CompileRun.merged_at).is_not(None))
        ).scalar_one_or_none()

        # Pass 1: PR-based (existing path)
        prs = gateway.list_merged_prs(ctx["default_branch"], watermark)
        pr_shas = {pr.merge_commit_sha for pr in prs}

        # Pass 2: direct commits not already covered by any PR in this reconcile window
        raw_commits = gateway.list_commits(ctx["default_branch"], watermark)
        direct_commits = [c for c in raw_commits if c.sha not in pr_shas]

        # Merge both streams by timestamp (ties: PRs before commits for determinism)
        items: list[tuple] = (
            [(pr.merged_at, 0, "pr", pr) for pr in prs] +
            [(c.timestamp, 1, "commit", c) for c in direct_commits]
        )
        items.sort(key=lambda x: (x[0], x[1]))

        summaries: list[CompileSummary] = []
        covered = expect_pr is None or _already_succeeded(session, repo.id, expect_pr)
        for _, _, kind, item in items:
            if kind == "pr":
                pr = item
                if _already_succeeded(session, repo.id, pr.number):
                    continue  # idempotence (ADR-002)
                summaries.append(_compile_one(session, repo, ctx, pr=pr))
                covered = covered or pr.number == expect_pr
            else:
                commit = item
                if _commit_already_succeeded(session, repo.id, commit.sha):
                    continue  # idempotence for direct commits
                summaries.append(_compile_one(session, repo, ctx, pr=None, commit=commit))

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


# kc:external-key: emit-only
def emit_only(repo_dir: Path) -> CompileSummary:
    """Emit-stage-only rerun against already-compiled Knowledge IR — no
    Collect/Extract/Normalize, no new `compile_runs` row (ADR-013's cheap
    OKF-spec-version rollout path: a spec bump is a `wiki/emitter.py` code
    change plus a re-render, never a data migration or a full recompile).

    Reuses the most recent succeeded run's identity (commit, compile_run_id)
    for the emitted frontmatter — this run is not new history, so nothing is
    persisted to `compile_runs`; only the wiki bundle on disk changes.
    Requires at least one prior successful compile.
    """
    with _locked_session(repo_dir) as (session, repo, ctx):
        last_run = session.execute(
            select(CompileRun).where(CompileRun.repo_id == repo.id,
                                     CompileRun.status == "succeeded")
            .order_by(CompileRun.id.desc()).limit(1)).scalar_one_or_none()
        if last_run is None:
            raise CompileError(
                f"no successful compile found for '{ctx['repo_slug']}' — "
                f"run `kc compile --full` first")

        state = load_current_state(session, repo.id, ctx["repo_slug"])
        wiki_pages_written = _emit_wiki(session, ctx["repo_slug"], last_run,
                                        ctx["wiki_dir"], dirty=None)
        summary = CompileSummary(
            repo_slug=ctx["repo_slug"], compile_run_id=last_run.id,
            commit_sha=last_run.commit_sha, entities=len(state.entities),
            relationships=len(state.relationships), added=0, changed=0, removed=0,
            moved=0, dirty=0, warnings=[], wiki_pages_written=wiki_pages_written,
            wiki_dir=str(ctx["wiki_dir"]))
        _publish_wiki(ctx, summary, last_run.id, last_run.commit_sha)
        return summary


# kc:external-key: verify
def verify(repo_dir: Path) -> VerifyReport:
    """Shadow compile: Collect + Extract + Normalize + Diff — zero writes.
    Empty delta <=> incremental history is equivalent to a fresh full compile.
    The remedy for divergence is a real `kc compile --full` (slug-preserving)."""
    with _locked_session(repo_dir) as (session, repo, ctx):
        collector = GitCollector(ctx["repo_dir"])
        artifacts = collector.collect_full()
        facts = _extract(artifacts) + _mutation_facts(ctx) + _journey_facts(ctx)
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


def _commit_already_succeeded(session: Session, repo_id: int, commit_sha: str) -> bool:
    """Idempotence check for direct-commit (scope='commit') runs."""
    return session.execute(
        select(CompileRun.id).where(CompileRun.repo_id == repo_id,
                                    CompileRun.commit_sha == commit_sha,
                                    CompileRun.scope == "commit",
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
    engine = make_engine()
    try:
        check_connection(engine)
    except DatabaseUnavailableError as exc:
        raise CompileError(str(exc)) from exc
    with Session(engine) as session:
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
                 pr: MergedPR | None, commit: CommitInfo | None = None) -> CompileSummary:
    """Compile one change unit. Modes:
    - pr=None, commit=None  → full compile
    - pr is not None        → PR-incremental (existing path)
    - commit is not None    → direct-commit incremental (no PR metadata)
    """
    collector = GitCollector(ctx["repo_dir"])

    if pr is None and commit is None:
        commit_sha = collector.head_commit()
        in_scope: frozenset[str] = frozenset()
        full = True
        scope_label = "full"
    elif pr is not None:
        commit_sha = pr.merge_commit_sha
        full = False
        in_scope = frozenset({f.path for f in pr.files}
                             | {f.old_path for f in pr.files if f.old_path})
        scope_label = "pr"
    else:
        commit_sha = commit.sha
        full = False
        in_scope = frozenset(commit.files)
        scope_label = "commit"

    run = CompileRun(repo_id=repo.id, scope=scope_label,
                     pr_number=pr.number if pr else None,
                     merged_at=pr.merged_at if pr else None,
                     commit_timestamp=commit.timestamp if commit else None,
                     commit_sha=commit_sha, status="running",
                     fact_vocabulary_version=FACT_VOCABULARY_VERSION,
                     knowledge_model_version=KNOWLEDGE_MODEL_VERSION,
                     okf_spec_version=OKF_SPEC_VERSION)
    session.add(run)
    session.commit()  # staging commit 1

    try:
        # Collect -> staging commit 2
        if pr is None and commit is None:
            artifacts = collector.collect_full()
            extra_facts: list[Fact] = []
            # File-based Jira in full compile: load ALL cached issues as jira_observed
            # facts so the Jira→Feature enrichment pass has something to match against.
            # REST-based Jira is PR-scoped only (no PR = no issue keys to fetch from).
            jira_cfg = ctx["config"].get("jira", {})
            if jira_cfg.get("enabled") and jira_cfg.get("source") == "file":
                all_keys = _file_jira_all_keys(ctx)
                if all_keys:
                    extra_facts += _jira_facts(ctx, all_keys, pr_number=None)
        elif pr is not None:
            live_paths = [f.path for f in pr.files if f.change != "removed"]
            artifacts = collector.collect_at_commit(commit_sha, live_paths)
            extra_facts = _pr_facts(pr)
            issue_keys = next((f.payload["linked_issue_keys"] for f in extra_facts
                              if f.fact_type == "pr_observed"), [])
            extra_facts += _jira_facts(ctx, issue_keys, pr.number)
        else:
            live_paths = list(commit.files)
            artifacts = collector.collect_at_commit(commit_sha, live_paths)
            extra_facts = []
            issue_keys = sorted(set(_ISSUE_KEY.findall(commit.message)))
            if issue_keys:
                extra_facts += _jira_facts(ctx, issue_keys, pr_number=None)
        extra_facts += _mutation_facts(ctx)
        extra_facts += _journey_facts(ctx)
        session.add_all(ArtifactRow(repo_id=repo.id, compile_run_id=run.id,
                                    artifact_type=a.artifact_type, source_ref=a.source_ref,
                                    content_hash=a.content_hash, content=a.content)
                        for a in artifacts)
        session.commit()

        # Extract -> staging commit 3 (deterministic first and always — ADR-006)
        facts = _extract(artifacts) + extra_facts
        llm_ran, llm_warnings = _extract_semantic(session, ctx, artifacts, facts)

        # Jira→Feature enrichment (LLM-derived motivates edges). Runs after
        # _extract_semantic so feature candidates are in `facts`. Load current
        # state here once; pass it to normalize below to avoid a second DB round-trip.
        current = load_current_state(session, repo.id, ctx["repo_slug"])
        enrichment_facts = _jira_feature_enrichment_facts(ctx, session, facts, current)
        facts.extend(enrichment_facts)
        llm_ran = llm_ran or bool(enrichment_facts)

        # degraded = the semantic layer is configured on but didn't run at all
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
        # current already loaded above (before enrichment)
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
        entity_changes=list(delta.entity_changes),
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
    # Embeddings (ADR-005): same post-persist, never-roll-back contract.
    emb_cfg = ctx["config"].get("embeddings", {})
    if emb_cfg.get("enabled", False):
        try:
            from knowledge_compiler.llm.embeddings import build_embedder
            from knowledge_compiler.llm.provider import LLMProviderError
            from knowledge_compiler.retrieval.embed import emit_embeddings

            try:
                embedder = ctx.get("embedder") or build_embedder(emb_cfg)
                progress = ctx.get("progress")
                on_progress = (lambda i, n, ref: progress("embed", i, n, ref)) if progress else None
                _, emb_warnings = emit_embeddings(session, repo.id, embedder, dirty,
                                                  on_progress=on_progress)
                summary.warnings.extend(emb_warnings)
            except LLMProviderError as exc:
                summary.warnings.append(f"embeddings unavailable — FTS-only retrieval: {exc}")
        except Exception as exc:  # noqa: BLE001 — same contract as emission
            summary.warnings.append(f"embedding emission failed (compile state intact): {exc}")

    _publish_wiki(ctx, summary, run.id, commit_sha)
    return summary


def _publish_wiki(ctx: dict, summary: CompileSummary, run_id: int, commit_sha: str) -> None:
    try:
        pub_cfg = _publisher_config(ctx["config"])
        if pub_cfg.enabled:
            from knowledge_compiler.wiki.publisher import GitBranchPublisher

            result = GitBranchPublisher(ctx["repo_dir"], pub_cfg).publish(
                ctx["wiki_dir"], f"kc: compile run {run_id} @ {commit_sha[:12]}")
            summary.published_sha = result.commit_sha if result.committed else None
            summary.pushed = result.pushed
    except Exception as exc:  # noqa: BLE001 — same contract as emission
        summary.warnings.append(f"wiki publish failed (compile state is intact): {exc}")


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

    from knowledge_compiler.extractors.annotation_parser import parse_external_keys
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
        # test files are excluded by default: their fixture code reads as domain
        # rules to the model (dogfood finding). Evidence-based, language-agnostic
        # detection: any file that produced a test_case_observed fact.
        skip = frozenset() if llm_cfg.get("include_tests", False) else frozenset(
            f.payload["file"] for f in facts if f.fact_type == "test_case_observed")
        # ADR-022: pre-extract kc:external-key: annotations from artifact source.
        # Deterministic; does not affect the LLM cache key.
        annotations: dict[str, dict[str, str]] = {
            a.source_ref: parse_external_keys(a.content)
            for a in artifacts
            if a.content is not None and a.source_ref not in skip
        }
        progress = ctx.get("progress")
        on_progress = (lambda i, n, ref: progress("llm", i, n, ref)) if progress else None
        extractor = LLMSemanticExtractor(
            provider=provider, cache=LLMCache(session.get_bind()),
            max_calls=llm_cfg.get("max_calls_per_run", 200),
            known_symbols=symbols, modules=modules, skip_files=skip,
            on_progress=on_progress, known_annotations=annotations)
        facts.extend(extractor.extract(artifacts))
        return True, list(extractor.warnings)
    except LLMBudgetExceeded as exc:
        raise CompileError(str(exc)) from exc  # failed-resumable (pipeline.md §6.2)
    except LLMProviderError as exc:
        return False, [f"LLM provider unavailable — compiled degraded (--no-llm semantics): {exc}"]


_ISSUE_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


def _pr_facts(pr: MergedPR) -> list[Fact]:
    linked_issue_keys = sorted(set(_ISSUE_KEY.findall(f"{pr.title}\n{pr.body}")))
    payload = {"number": pr.number, "title": pr.title, "body": pr.body,
               "merged_at": pr.merged_at.isoformat(), "merge_commit_sha": pr.merge_commit_sha,
               "files": sorted(f.path for f in pr.files),
               "linked_issue_keys": linked_issue_keys}
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


def _mutation_facts(ctx: dict) -> list[Fact]:
    """Item 2 of the QA-agent-grounding backlog: opt-in, deterministic — reads
    whatever a mutation-testing CI job already produced (collectors/mutation.py),
    never runs mutation testing itself. Applies uniformly to full and PR
    compiles alike: mutation scores describe the repo's current test suite,
    not a PR's file diff, so they aren't scope-limited the way PR facts are."""
    section = ctx["config"].get("mutation", {})
    if not section.get("enabled", False):
        return []
    scores = read_mutation_scores(ctx["repo_dir"], section.get("scores_file", "mutation-scores.json"))
    facts = []
    for module, stats in sorted(scores.items()):
        payload = {"module": module, **stats}
        facts.append(Fact(fact_type="mutation_score_observed", payload=payload,
                          artifact_refs=(f"mutation-scores:{module}",),
                          extraction=_MUTATION_EXTRACTION, content_hash=content_hash(payload)))
    return facts


_JOURNEY_EXTRACTION = Extraction(method="deterministic", extractor="journey-config",
                                 extractor_version="0.1")


def _journey_facts(ctx: dict) -> list[Fact]:
    """User journeys (ADR-017, items 3+4): V1 scope is deterministic-only,
    declared directly in kc.toml `[[journeys]]` — each names an ordered list
    of already-compiled entity slugs (component/api/business_rule) the
    journey traverses. Unresolvable step slugs are dropped (not failed) by
    Normalize, same DP8 "visible, never silently merge" discipline as the
    external-dependency precedent — a config typo shouldn't fail the whole
    compile, but it should be visible (a compile warning), not silent.

    Inline [[journeys]] and journeys_file are both supported and merged.
    journeys_file is resolved relative to the repo directory; a missing or
    unreadable file fails loudly (same posture as a bad Jira source)."""
    journeys = list(ctx["config"].get("journeys", []))
    journeys_file = ctx["config"].get("journeys_file")
    if journeys_file:
        paths = [journeys_file] if isinstance(journeys_file, str) else journeys_file
        for rel in paths:
            file_path = Path(ctx["repo_dir"]) / rel
            try:
                extra = tomllib.loads(file_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raise CompileError(f"journeys_file not found: {file_path}")
            except Exception as exc:
                raise CompileError(f"journeys_file unreadable ({file_path}): {exc}")
            journeys += extra.get("journeys", [])
    facts = []
    for j in journeys:
        payload = {"name": j["name"], "steps": list(j.get("steps", []))}
        facts.append(Fact(fact_type="user_journey_observed", payload=payload,
                          artifact_refs=(f"kc.toml:journeys:{j['name']}",),
                          extraction=_JOURNEY_EXTRACTION, content_hash=content_hash(payload)))
    return facts


def _jira_facts(ctx: dict, issue_keys: list[str], pr_number: int | None) -> list[Fact]:
    if not issue_keys:
        return []
    jira_cfg = ctx["config"].get("jira", {})
    if not jira_cfg.get("enabled", False):
        return []
    gateway = ctx.get("jira_gateway") or build_jira_gateway(jira_cfg, ctx["repo_dir"])
    if gateway is None:
        return []
    facts = []
    for issue in gateway.get_issues(issue_keys):
        payload = {"key": issue.key, "summary": issue.summary, "status": issue.status,
                   "description": issue.description, "issue_type": issue.issue_type,
                   "linked_pr": pr_number}
        facts.append(Fact(fact_type="jira_observed", payload=payload,
                          artifact_refs=(f"jira:{issue.key}",), extraction=_JIRA_EXTRACTION,
                          content_hash=content_hash(payload)))
    return facts


def _file_jira_all_keys(ctx: dict) -> list[str]:
    """Return all issue keys from the file-based Jira cache.

    Used in full-compile mode so the Jira→Feature enrichment pass has
    jira_observed facts to work against even when there's no PR to extract
    keys from. Returns [] if source != 'file' or cache unreadable.
    """
    import json
    jira_cfg = ctx["config"].get("jira", {})
    cache_file = jira_cfg.get("cache_file", "jira-cache.json")
    try:
        data = json.loads((ctx["repo_dir"] / cache_file).read_text(encoding="utf-8"))
        return sorted(data.keys())
    except Exception:
        return []


def _jira_feature_enrichment_facts(ctx: dict, session: Session,
                                    facts: list[Fact], current) -> list[Fact]:
    """LLM-derived motivates edges: Jira Story → Feature (ir.md §3.3).

    Reads jira_observed and feature_candidate facts (+ current-state feature
    entities) and calls the LLM per-issue to match which compiled features a
    Jira story motivated. Returns jira_feature_link_observed facts consumed by
    Normalize._jira_stories() to populate payload["linked_feature_names"], which
    _p5_relationships() resolves to motivates → Feature edges.

    Skipped entirely when: LLM disabled, Jira disabled, no Jira facts, or no
    feature facts. Provider/validation errors skip the affected issue (never
    fail the compile — Jira enrichment is best-effort additive, not core)."""
    if not _llm_enabled(ctx) or not ctx["config"].get("jira", {}).get("enabled", False):
        return []

    jira_facts = [f for f in facts if f.fact_type == "jira_observed"]
    if not jira_facts:
        return []

    feature_candidates = {f.payload["name"]: f.payload.get("narrative", "")
                          for f in facts if f.fact_type == "feature_candidate"}
    current_features = {e.name: e.payload.get("narrative", "")
                        for e in current.entities if e.entity_type == "feature"}
    all_features: dict[str, str] = {**current_features, **feature_candidates}
    if not all_features:
        return []

    from knowledge_compiler.llm.cache import LLMCache, cache_key
    from knowledge_compiler.llm.provider import LLMProviderError, build_provider
    from knowledge_compiler.llm.templates import (
        JIRA_FEATURE_SCHEMA, JIRA_FEATURE_TEMPLATE_ID, JIRA_FEATURE_TEMPLATE_VERSION,
        JiraFeatureMatchOut, build_jira_feature_prompt,
    )

    llm_cfg = ctx["config"].get("llm", {})
    try:
        provider = ctx.get("llm_provider") or build_provider(llm_cfg)
        cache = LLMCache(session.get_bind())
    except LLMProviderError:
        return []

    feature_set_hash = content_hash({"names": sorted(all_features)})
    _ENRICH_EXTRACTION = Extraction(method="llm", extractor="jira-feature-match",
                                    extractor_version="0.1",
                                    model_id=provider.model_id,
                                    template_version=JIRA_FEATURE_TEMPLATE_VERSION)
    results: list[Fact] = []
    for jf in sorted(jira_facts, key=lambda f: f.payload["key"]):
        key = jf.payload["key"]
        summary = jf.payload.get("summary", "")
        description = jf.payload.get("description", "")
        ck = cache_key(JIRA_FEATURE_TEMPLATE_ID, JIRA_FEATURE_TEMPLATE_VERSION,
                       provider.model_id,
                       content_hash({"k": key, "s": summary,
                                     "d": description, "fh": feature_set_hash}))
        cached = cache.get(ck)
        if cached is not None:
            try:
                out = JiraFeatureMatchOut.model_validate(cached)
            except Exception:
                continue
        else:
            prompt = build_jira_feature_prompt(key, summary, description, all_features)
            try:
                raw = provider.complete(prompt, JIRA_FEATURE_SCHEMA)
                out = JiraFeatureMatchOut.model_validate(raw)
                cache.put(ck, JIRA_FEATURE_TEMPLATE_ID, JIRA_FEATURE_TEMPLATE_VERSION,
                          provider.model_id, out.model_dump())
            except Exception:
                continue
        matched = sorted({n for n in out.motivates if n in all_features})
        if matched:
            payload = {"jira_key": key, "feature_names": matched}
            results.append(Fact(fact_type="jira_feature_link_observed", payload=payload,
                                artifact_refs=(f"jira:{key}",),
                                extraction=_ENRICH_EXTRACTION,
                                content_hash=content_hash(payload)))
    return results


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
               dirty: set[str] | None) -> int:
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
            select(DeltaChangeRow.op, DeltaChangeRow.slug, DeltaChangeRow.entity_type,
                  DeltaChangeRow.change_summary)
            .where(DeltaChangeRow.compile_run_id == r.id)
            .order_by(DeltaChangeRow.slug)).all()
        recent.append(RunDelta(compile_run_id=r.id, commit_sha=r.commit_sha,
                               finished_at=r.finished_at,
                               changes=tuple((op, slug, et, cs) for op, slug, et, cs in changes)))

    emitter = WikiEmitter(wiki_dir)
    ctx = WikiContext(repo_slug=repo_slug, compile_run_id=run.id, commit_sha=run.commit_sha,
                      finished_at=run.finished_at)
    written = emitter.emit(state.entities, state.relationships, dirty, recent, ctx)
    return len(written)
