"""kc — the Knowledge Compiler CLI (pipeline.md §1 run modes)."""

from __future__ import annotations

import logging
import sys

import click

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(asctime)s][%(filename)s][%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from knowledge_compiler import __version__


def _progress(stage: str, i: int, n: int, detail: str) -> None:
    """Streamed to stderr (so it never mixes with stdout's final summary/scripting
    output) with an explicit flush — LLM extraction and embeddings are the two
    long-running, network-bound stages that were previously silent until the
    whole compile finished."""
    click.echo(f"  [{stage}] {i}/{n} {detail}", err=True)
    sys.stderr.flush()


@click.group()
@click.version_option(__version__, prog_name="kc")
def main() -> None:
    """Knowledge Compiler: compile engineering artifacts into a knowledge base."""


@main.command("init")
@click.option("--slug", required=True, help="Repository slug (unique in the knowledge base).")
@click.option("--forge-ref", required=True, help="Forge reference, e.g. github.com/org/repo.")
@click.option("--default-branch", default="main", show_default=True)
@click.option("--dir", "target_dir", type=click.Path(file_okay=False, exists=True), default=".",
              show_default=True, help="Directory to write kc.toml into.")
def init_cmd(slug: str, forge_ref: str, default_branch: str, target_dir: str) -> None:
    """Register a repository: run migrations, insert the repo row, write kc.toml."""
    from pathlib import Path

    from knowledge_compiler.compiler.bootstrap import init_repository
    from knowledge_compiler.compiler.run import CompileError

    try:
        repo_id, config_path = init_repository(Path(target_dir), slug, forge_ref, default_branch)
    except CompileError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"registered repository '{slug}' (id={repo_id}); wrote {config_path}")


@main.command("compile")
@click.option("--full", is_flag=True, help="Bootstrap / escape-hatch full compilation.")
@click.option("--pr", type=int, default=None, help="Incremental compilation of one merged PR.")
@click.option("--emit-only", is_flag=True,
              help="Re-render the wiki bundle from already-compiled Knowledge IR only — "
                   "no Collect/Extract/Normalize, no new compile_runs row. For rolling out "
                   "an OKF-spec-version bump cheaply across previously compiled repos "
                   "(ADR-013). Requires at least one prior successful compile.")
@click.option("--dir", "repo_dir", type=click.Path(file_okay=False, exists=True), default=".",
              show_default=True, help="Repository directory (contains kc.toml).")
@click.option("--no-llm", is_flag=True,
              help="Skip LLM extraction: deterministic pass only, run marked degraded "
                   "(pipeline.md §6.1). LLM-derived entities are never removed by such runs.")
@click.option("--verbose", "-v", is_flag=True,
              help="Print each added/changed/removed/moved entity slug in the summary.")
def compile_cmd(full: bool, pr: int | None, emit_only: bool, repo_dir: str, no_llm: bool,
                verbose: bool) -> None:
    """Compile the repository (pipeline.md §3)."""
    if emit_only:
        if full or pr is not None:
            raise click.UsageError("--emit-only cannot be combined with --full or --pr")
    elif full == (pr is not None):
        raise click.UsageError("exactly one of --full or --pr is required")
    from pathlib import Path

    from knowledge_compiler.compiler.run import CompileError, compile_full, compile_pr, emit_only as run_emit_only

    try:
        if emit_only:
            _echo_summary(run_emit_only(Path(repo_dir)), verbose)
        elif full:
            _echo_summary(compile_full(Path(repo_dir), no_llm=no_llm, progress=_progress), verbose)
        else:
            summaries = compile_pr(Path(repo_dir), _gateway(Path(repo_dir)), expect_pr=pr,
                                   no_llm=no_llm, progress=_progress)
            if not summaries:
                click.echo(f"PR #{pr} already compiled — nothing to do (idempotent)")
            for s in summaries:
                _echo_summary(s, verbose)
            if len(summaries) > 1:
                _echo_token_totals(summaries)
    except CompileError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command("reconcile")
@click.option("--dir", "repo_dir", type=click.Path(file_okay=False, exists=True), default=".",
              show_default=True, help="Repository directory (contains kc.toml).")
@click.option("--verbose", "-v", is_flag=True,
              help="Print each added/changed/removed/moved entity slug in the summary.")
def reconcile_cmd(repo_dir: str, verbose: bool) -> None:
    """Catch up on merged PRs missed since the last compile (pipeline.md §4)."""
    from pathlib import Path

    from knowledge_compiler.compiler.run import CompileError, reconcile

    try:
        summaries = reconcile(Path(repo_dir), _gateway(Path(repo_dir)), progress=_progress)
    except CompileError as exc:
        raise click.ClickException(str(exc)) from exc
    if not summaries:
        click.echo("up to date — no merged PRs after the watermark")
    for s in summaries:
        _echo_summary(s, verbose)
    if len(summaries) > 1:
        _echo_token_totals(summaries)


def _gateway(repo_dir):
    from knowledge_compiler.collectors.forge import ForgeError, GitHubGateway
    from knowledge_compiler.compiler.run import CompileError, read_repo_config

    forge_ref = read_repo_config(repo_dir)["forge_ref"]  # e.g. github.com/org/repo
    parts = forge_ref.split("/")
    if len(parts) < 3 or "github" not in parts[0]:
        raise CompileError(f"cannot derive owner/repo from forge_ref '{forge_ref}' "
                           "(expected github.com/<owner>/<repo>)")
    try:
        return GitHubGateway(owner=parts[1], repo=parts[2])
    except ForgeError as exc:
        raise CompileError(str(exc)) from exc


def _echo_summary(s, verbose: bool = False) -> None:
    scope = f"PR #{s.pr_number}" if s.pr_number else "full"
    click.echo(f"compiled {s.repo_slug} [{scope}] @ {s.commit_sha[:12]} (run {s.compile_run_id})")
    click.echo(f"  entities: {s.entities}  relationships: {s.relationships}")
    click.echo(f"  delta: +{s.added} ~{s.changed} -{s.removed} moved:{s.moved}  dirty: {s.dirty}")
    if verbose and s.entity_changes:
        symbols = {"added": "+", "changed": "~", "removed": "-", "moved": ">"}
        for op in ("added", "changed", "removed", "moved"):
            for c in (c for c in s.entity_changes if c.op == op):
                click.echo(f"    {symbols[op]} {c.slug}  [{c.entity_type}]")
    if s.wiki_dir:
        click.echo(f"  wiki: {s.wiki_pages_written} files -> {s.wiki_dir}")
    if s.published_sha:
        click.echo(f"  published: {s.published_sha[:12]} (pushed: {s.pushed})")
    if s.llm_calls:
        click.echo(f"  llm: {s.llm_calls} calls  tokens: in={s.llm_input_tokens} out={s.llm_output_tokens}")
    if s.embedding_calls:
        click.echo(f"  embeddings: {s.embedding_calls} calls  tokens: in={s.embedding_input_tokens}")
    for w in s.warnings:
        click.echo(f"  warning: {w}")


def _echo_token_totals(summaries: list) -> None:
    """End-of-run grand total across every item a reconcile walked — the number
    that matters when a run spans many PRs/commits, not just the last one."""
    llm_calls = sum(s.llm_calls for s in summaries)
    llm_in = sum(s.llm_input_tokens for s in summaries)
    llm_out = sum(s.llm_output_tokens for s in summaries)
    emb_calls = sum(s.embedding_calls for s in summaries)
    emb_in = sum(s.embedding_input_tokens for s in summaries)
    if not (llm_calls or emb_calls):
        return
    click.echo(f"total across {len(summaries)} run(s):")
    if llm_calls:
        click.echo(f"  llm: {llm_calls} calls  tokens: in={llm_in} out={llm_out}")
    if emb_calls:
        click.echo(f"  embeddings: {emb_calls} calls  tokens: in={emb_in}")


@main.command("verify")
@click.option("--dir", "repo_dir", type=click.Path(file_okay=False, exists=True), default=".",
              show_default=True, help="Repository directory (contains kc.toml).")
@click.pass_context
def verify_cmd(ctx: click.Context, repo_dir: str) -> None:
    """Shadow full compile + equivalence check against current state (pipeline.md §7)."""
    from pathlib import Path

    from knowledge_compiler.compiler.run import CompileError, verify

    try:
        report = verify(Path(repo_dir))
    except CompileError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"identity evidence: {report.evidence_histogram}")
    if report.equivalent:
        click.echo("VERIFIED: incremental state is equivalent to a full compile")
        return
    click.echo("DIVERGED: a full compile would produce the following delta:")
    for op, slug in report.entity_divergences:
        click.echo(f"  {op}: {slug}")
    if report.relationship_divergences:
        click.echo(f"  relationship divergences: {report.relationship_divergences}")
    click.echo("remedy: kc compile --full (slug-preserving)")
    ctx.exit(1)


@main.command("inspect")
@click.option("--dir", "repo_dir", type=click.Path(file_okay=False, exists=True), default=".",
              show_default=True, help="Repository directory (contains kc.toml).")
def inspect_cmd(repo_dir: str) -> None:
    """Entity/relationship counts and last delta — the first debugging surface."""
    from pathlib import Path

    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from knowledge_compiler.compiler.run import CompileError, read_repo_config
    from knowledge_compiler.storage.db import DatabaseUnavailableError, check_connection, make_engine
    from knowledge_compiler.storage.schema import (
        CompileRun, DeltaChangeRow, EntityRow, RelationshipRow, Repository,
    )

    try:
        slug = read_repo_config(Path(repo_dir))["slug"]
    except CompileError as exc:
        raise click.ClickException(str(exc)) from exc

    engine = make_engine()
    try:
        check_connection(engine)
    except DatabaseUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc

    with Session(engine) as session:
        repo = session.execute(select(Repository).where(Repository.slug == slug)).scalar_one_or_none()
        if repo is None:
            raise click.ClickException(f"repository '{slug}' is not registered — run `kc init`")

        click.echo(f"repository: {slug}")
        entity_counts = session.execute(
            select(EntityRow.entity_type, func.count()).where(EntityRow.repo_id == repo.id)
            .group_by(EntityRow.entity_type).order_by(EntityRow.entity_type)).all()
        total = sum(c for _, c in entity_counts)
        click.echo(f"entities: {total}")
        for entity_type, count in entity_counts:
            click.echo(f"  {entity_type}: {count}")
        rels = session.execute(select(func.count()).select_from(RelationshipRow)
                               .where(RelationshipRow.repo_id == repo.id)).scalar_one()
        click.echo(f"relationships: {rels}")

        last_run = session.execute(
            select(CompileRun).where(CompileRun.repo_id == repo.id,
                                     CompileRun.status == "succeeded")
            .order_by(CompileRun.id.desc()).limit(1)).scalar_one_or_none()
        if last_run is None:
            click.echo("last compile: none")
            return
        ops = session.execute(
            select(DeltaChangeRow.op, func.count())
            .where(DeltaChangeRow.compile_run_id == last_run.id)
            .group_by(DeltaChangeRow.op).order_by(DeltaChangeRow.op)).all()
        delta_str = "  ".join(f"{op}:{count}" for op, count in ops) or "empty"
        ts = last_run.finished_at.strftime("%Y-%m-%d %H:%M:%S") if last_run.finished_at else "unknown"
        click.echo(f"last compile: run {last_run.id} @ {last_run.commit_sha[:12]} [{ts}] — delta {delta_str}")
        if last_run.files_seen is not None:
            pct = (last_run.files_parsed / last_run.files_seen * 100) if last_run.files_seen else 100.0
            click.echo(f"knowledge completeness: {last_run.files_parsed}/{last_run.files_seen} "
                      f"files parsed ({pct:.1f}%)")
            if last_run.files_failed:
                click.echo(f"  failed: {', '.join(last_run.failed_files or [])}")


@main.command("validate-test")
@click.argument("test_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--for-entity", "for_entity", required=True,
              help="Entity slug originally passed to `test_plan` for this test's recommendations.")
@click.option("--dir", "repo_dir", type=click.Path(file_okay=False, exists=True), default=".",
              show_default=True, help="Repository directory (contains kc.toml).")
@click.pass_context
def validate_test_cmd(ctx: click.Context, test_file: str, for_entity: str, repo_dir: str) -> None:
    """Score a generated test's kc-covers: header against compiled knowledge.

    Never compiles — a downstream check over already-compiled state, same as
    `kc serve` (BRAINSTORM-test-generation-mechanism.md: the compiler's job
    ends at knowledge + test_plan; validation is a consumer, not a stage).
    """
    from pathlib import Path

    from sqlalchemy.orm import Session

    from knowledge_compiler.compiler.run import CompileError, read_config
    from knowledge_compiler.mcp.queries import resolve_repo
    from knowledge_compiler.storage.db import DatabaseUnavailableError, check_connection, make_engine
    from knowledge_compiler.validation import score_test

    try:
        config = read_config(Path(repo_dir))
    except CompileError as exc:
        raise click.ClickException(str(exc)) from exc
    slug = config["repository"]["slug"]
    dep_map = config.get("dependencies", {})

    engine = make_engine()
    try:
        check_connection(engine)
    except DatabaseUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc

    with Session(engine) as session:
        try:
            repo = resolve_repo(session, slug)
        except LookupError as exc:
            raise click.ClickException(str(exc)) from exc
        report = score_test(session, repo.id, Path(test_file), for_entity, dep_map=dep_map)

    if report is None:
        raise click.ClickException(
            f"entity '{for_entity}' not found — pass the same slug `test_plan` was run against")

    click.echo(f"kc-covers: {report.test_file}")
    click.echo(f"  for entity: {report.for_entity}")
    click.echo()
    if not report.header_found:
        click.echo("header:            MISSING -- automatic 0% (no kc-covers: block found)")
        click.echo()
        click.echo("SCORE: 0.0%")
        ctx.exit(1)

    click.echo("header:            FOUND")
    click.echo(f"claimed slugs:     {', '.join(report.claimed_slugs) or '(none)'}")
    click.echo()
    click.echo("existence:")
    for check in report.existence:
        click.echo(f"  {check.slug:<28} {'OK' if check.exists else 'MISSING'}")
    click.echo()
    click.echo(f"precision/recall vs test_plan({report.for_entity}):")
    click.echo(f"  citable recommended:  {', '.join(report.citable_recommended) or '(none)'}")
    click.echo(f"  precision: {report.precision * 100:.1f}%")
    if report.missing_from_claims:
        click.echo(f"  missed (recall gap):  {', '.join(report.missing_from_claims)}")
    click.echo(f"  recall:    {report.recall * 100:.1f}%")
    if report.symbols_kind_citable:
        api_n = len(report.api_kind_citable)
        total_n = len(report.citable_recommended)
        ceiling_pct = api_n / total_n * 100 if total_n else 0.0
        click.echo(f"  ceiling (black-box / api-kind only): {api_n}/{total_n} = {ceiling_pct:.1f}%"
                   f"  [{len(report.symbols_kind_citable)} symbols-kind target(s) require unit tests]")
    if report.extraneous_claims:
        click.echo(f"  extraneous claims (exist, not recommended): {', '.join(report.extraneous_claims)}")
    click.echo()
    click.echo("mutation data:     not checked here -- see mutation-test.yaml for the execution-based signal")
    click.echo()
    click.echo(f"SCORE: {report.score_pct}%")

    if report.nonexistent_claims:
        ctx.exit(1)


@main.command("validate-okf")
@click.option("--dir", "repo_dir", type=click.Path(file_okay=False, exists=True), default=".",
              show_default=True, help="Repository directory (contains kc.toml).")
def validate_okf_cmd(repo_dir: str) -> None:
    """Check the emitted wiki bundle against OKF conformance rules (ADR-013).

    Never compiles — reads whatever the last `kc compile` wrote to the wiki
    output directory, same posture as `kc validate-test`.
    """
    from pathlib import Path

    from knowledge_compiler.compiler.run import CompileError, read_config
    from knowledge_compiler.wiki.okf_conformance import check_bundle

    try:
        config = read_config(Path(repo_dir))
    except CompileError as exc:
        raise click.ClickException(str(exc)) from exc

    wiki_dir = Path(repo_dir) / config.get("wiki", {}).get("output_dir", "kc-wiki")
    if not wiki_dir.is_dir():
        raise click.ClickException(
            f"no wiki bundle at {wiki_dir} — run `kc compile --full` first")

    report = check_bundle(wiki_dir)
    click.echo(f"okf spec version: {report.okf_spec_version}")
    click.echo(f"files checked:    {report.files_checked}")
    click.echo()
    if report.conformant:
        click.echo(f"CONFORMANT — {wiki_dir} satisfies OKF v{report.okf_spec_version}")
        return

    click.echo(f"{len(report.issues)} conformance issue(s):")
    for issue in report.issues:
        click.echo(f"  [{issue.rule}] {issue.file}: {issue.detail}")
    click.echo()
    click.echo("NOT CONFORMANT")
    raise click.exceptions.Exit(1)


@main.command("serve")
@click.option("--dir", "repo_dir", type=click.Path(file_okay=False, exists=True), default=".",
              show_default=True, help="Repository directory (contains kc.toml).")
def serve_cmd(repo_dir: str) -> None:
    """Read-only MCP server over the compiled knowledge base (stdio). Never
    compiles (ADR-002) — state updates come from CI-triggered `kc compile`."""
    from pathlib import Path

    from knowledge_compiler.storage.db import DatabaseUnavailableError

    try:
        from knowledge_compiler.mcp.server import serve
    except ImportError as exc:
        raise click.ClickException(
            "mcp SDK not installed — pip install 'open-knowledge-compiler[serve]'") from exc
    try:
        serve(Path(repo_dir))
    except DatabaseUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc
