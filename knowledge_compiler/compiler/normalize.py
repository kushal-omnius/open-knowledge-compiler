"""Normalize (docs/normalize.md): facts -> fully identified Knowledge IR.

Pure function of (facts, current, config) — no clock, no randomness, no unordered
iteration (normalize.md §9 determinism checklist). Never concludes removals (Diff's
job, pipeline.md §5). The LLM never assigns identity (ADR-004).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from knowledge_compiler.ir import (
    LLM_CANDIDATE_FACT_TYPES,
    Anchor,
    Entity,
    Fact,
    Relationship,
    content_hash,
)

if TYPE_CHECKING:
    from knowledge_compiler.compiler.diff import CompileScope

# --- Contract types -------------------------------------------------------------


# kc:external-key: identity-cascade-thresholds
@dataclass(frozen=True)
class Thresholds:
    """Identity-matching config (ADR-004: dogfood-tuned; conservative defaults —
    over-split beats over-merge, vision DP 8)."""

    t_anchor: float = 0.5
    t_name: float = 0.8


@dataclass
class CurrentState:
    """The persisted state Normalize matches against (ADR-003 current state)."""

    entities: list[Entity] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    # rename lineage from the delta log, old slug -> new slug (normalize.md §4.1)
    renames: dict[str, str] = field(default_factory=dict)


@dataclass
class MatchEvidence:
    slug: str
    rule: str  # external_key | anchor_overlap | name_similarity | minted | natural_key
    signals: dict[str, float]


@dataclass
class CandidateState:
    entities: list[Entity]
    relationships: list[Relationship]
    evidence: dict[str, MatchEvidence]     # slug -> how identity was assigned
    provenance: dict[str, list[dict]]      # slug -> contributing fact envelope snapshots
    conflicts: list[dict]
    warnings: list[str]
    # this compile's rename pairs, old slug -> new slug (normalize.md §4.1)
    renames: dict[str, str] = field(default_factory=dict)


# --- Helpers ---------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


# kc:external-key: entity-slug-generation
def slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "unnamed"


def normalize_route(route: str) -> str:
    """Positional params (ir.md §3.2 API key note): /users/{id} and /users/<user_id> -> /users/{}"""
    return re.sub(r"[{<][^}>]*[}>]", "{}", route)


def _tokens(name: str) -> frozenset[str]:
    return frozenset(t for t in _SLUG_RE.split(name.lower()) if t)


def name_similarity(a: str, b: str) -> float:
    """Token-set Jaccard (normalize.md §5.3): word-order robust, deterministic."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def anchor_overlap(candidate: tuple[Anchor, ...], target: tuple[Anchor, ...]) -> float:
    """Candidate-relative overlap (normalize.md §5.3): |A ∩ B| / |A|.
    Symbol granularity where both sides have symbol_path, else file granularity."""
    if not candidate:
        return 0.0

    def keys(anchors: tuple[Anchor, ...], symbol_level: bool) -> set:
        return ({(a.file_path, a.symbol_path) for a in anchors if a.symbol_path}
                if symbol_level else {a.file_path for a in anchors})

    symbol_level = any(a.symbol_path for a in candidate) and any(a.symbol_path for a in target)
    ck, tk = keys(candidate, symbol_level), keys(target, symbol_level)
    if not ck:
        return 0.0
    return len(ck & tk) / len(ck)


def _map_anchor(anchor: Anchor, rename_map: dict[str, str]) -> Anchor:
    """Apply git rename mapping to a stored anchor (normalize.md §3).

    A file rename changes the module prefix of every symbol inside it, so the
    symbol_path prefix is rewritten alongside file_path — otherwise symbol-level
    overlap scores 0 across renames and identity churns (the exact failure
    ADR-004's rename handling exists to prevent)."""
    new_path = rename_map.get(anchor.file_path)
    if new_path is None:
        return anchor
    update: dict = {"file_path": new_path}
    if anchor.symbol_path:
        old_mod, new_mod = _file_to_module(anchor.file_path), _file_to_module(new_path)
        if old_mod and anchor.symbol_path.startswith(old_mod + "."):
            update["symbol_path"] = new_mod + anchor.symbol_path[len(old_mod):]
        elif anchor.symbol_path == old_mod:
            update["symbol_path"] = new_mod
    return anchor.model_copy(update=update)


def _fact_snapshot(fact: Fact) -> dict:
    """Provenance snapshot: survives fact pruning (data-model.md §4)."""
    return {
        "fact_type": fact.fact_type,
        "artifact_refs": list(fact.artifact_refs),
        "extraction": fact.extraction.model_dump(),
        "anchors": [a.model_dump() for a in fact.anchors],
    }


# --- The algorithm ----------------------------------------------------------------


def normalize(facts: list[Fact], current: CurrentState, config: Thresholds,
              repo_slug: str, scope: CompileScope | None = None) -> CandidateState:
    """`scope` is the compile's CompileScope; without it (unit callers) every
    current entity is treated as surviving the compile."""
    n = _Normalizer(facts, current, config, repo_slug, scope)
    return n.run()


class _Normalizer:
    def __init__(self, facts: list[Fact], current: CurrentState, config: Thresholds,
                 repo_slug: str, scope: CompileScope | None = None) -> None:
        self.facts = facts
        self.current = current
        self.scope = scope
        self.config = config
        self.repo_slug = repo_slug
        self.entities: dict[str, Entity] = {}
        self.relationships: set[tuple[str, str, str]] = set()
        self.evidence: dict[str, MatchEvidence] = {}
        self.provenance: dict[str, list[dict]] = {}
        self.conflicts: list[dict] = []
        self.warnings: list[str] = []
        self.renames: dict[str, str] = {}

    def run(self) -> CandidateState:
        rename_map = self._p1_rename_map()
        self._p2_deterministic_entities()
        self._mutation_scores()
        self._state_models()
        self._escaped_defects()
        self._p3_p4_candidates(rename_map)
        self._rename_lineage(rename_map)
        self._user_journeys()
        self._p5_relationships()
        self._p6_wiki_pages()
        # P7 anchor currency happens inside matching (matched entities take candidate anchors)
        # P8 conflict surfacing happens inside aggregation (API sources) and merging
        return CandidateState(
            entities=[self.entities[s] for s in sorted(self.entities)],
            relationships=sorted(
                (Relationship(relation_type=r, from_slug=f, to_slug=t)
                 for f, r, t in self.relationships),
                key=lambda r: (r.relation_type, r.from_slug, r.to_slug),
            ),
            evidence=self.evidence,
            provenance=self.provenance,
            conflicts=self.conflicts,
            warnings=sorted(self.warnings),
            renames=dict(sorted(self.renames.items())),
        )

    # -- shared -------------------------------------------------------------------

    def _facts_of(self, fact_type: str) -> list[Fact]:
        return sorted((f for f in self.facts if f.fact_type == fact_type),
                      key=lambda f: f.content_hash)

    def _put(self, entity: Entity, rule: str, signals: dict[str, float],
             facts: list[Fact]) -> None:
        self.entities[entity.slug] = entity
        self.evidence[entity.slug] = MatchEvidence(entity.slug, rule, signals)
        self.provenance.setdefault(entity.slug, []).extend(_fact_snapshot(f) for f in facts)

    def _entity(self, entity_type: str, slug: str, name: str, payload: dict,
                anchors: tuple[Anchor, ...] = ()) -> Entity:
        return Entity(slug=slug, entity_type=entity_type, repo_id=self.repo_slug,
                      name=name, payload=payload, content_hash=content_hash(payload),
                      anchors=anchors)

    # -- P1 -------------------------------------------------------------------------

    def _p1_rename_map(self) -> dict[str, str]:
        renames: dict[str, str] = {}
        for f in self._facts_of("source_change_observed"):
            if f.payload.get("change") == "renamed":
                renames[f.payload["old_path"]] = f.payload["new_path"]
        return renames

    # -- P2: deterministic entities (natural keys) -----------------------------------

    def _p2_deterministic_entities(self) -> None:
        self._project()
        self._components()
        self._apis()
        self._test_coverage()
        self._pull_requests()
        self._jira_stories()

    def _user_journeys(self) -> None:
        """ADR-017 (items 3+4): V1 scope is deterministic-only — kc.toml
        `[[journeys]]` names an ordered step list of already-compiled entity
        slugs (component/api/business_rule/feature/risk — anything already
        compiled, this run or a prior one). Natural key = slugified name,
        same pattern as pull_request/jira_story. Runs after P3/P4 (not
        inside P2) precisely so LLM-derived step slugs (business_rule/
        feature/risk) are already minted and resolvable, not just
        deterministic ones. A step slug that doesn't resolve to any
        compiled entity at all is dropped with a warning (DP8: visible,
        never silently merged) rather than failing the whole journey or
        the compile.

        Resolve against self.entities (this run) *and* self.current.entities
        (everything already persisted) — bug found dogfooding repoA: an
        incremental `reconcile` only extracts facts for files that changed
        in that pass, so self.entities alone is that run's slice, never the
        full accumulated state. Checking self.entities only meant a journey
        step whose defining file simply wasn't touched this pass — not
        actually removed, still real and queryable — was misreported as
        unresolved on every incremental compile that didn't happen to touch
        every step's file simultaneously. Other Normalize methods already
        consult self.current.entities for this same cross-run-continuity
        reason (e.g. the identity-cascade matching pool, _project()); this
        method just hadn't.

        Fail-closed status (dogfood-review finding): a dropped step used to be
        visible only in the transient compile-run warning list — the journey
        entity itself looked identical to a fully-resolved one, so a shortened
        chain could silently pass as a complete one downstream (journey_coverage,
        test_plan, the wiki page). `status` + `unresolved_steps` make that
        distinction durable on the entity, not just at compile time.

        Only current entities that survive *this* compile count (dogfood finding
        on frida, 1.5.0): a compile that itself removes a step's target (e.g. an
        API slug renamed by router-prefix composition) used to resolve the step
        against the pre-compile snapshot, persist the journey as `complete`, and
        make the next `kc verify` diverge. Survival is Diff's removal-evidence
        rule (split_unobserved), not a parallel notion."""
        current_slugs = {e.slug for e in self.current.entities}
        if self.scope is not None:
            from knowledge_compiler.compiler.diff import split_unobserved
            removed, _ = split_unobserved(self.entities, self.current, self.scope)
            current_slugs -= removed
        for f in self._facts_of("user_journey_observed"):
            name = f.payload["name"]
            declared_steps = f.payload.get("steps", [])
            resolved_steps = []
            unresolved_steps = []
            renamed_steps = {}
            for declared in declared_steps:
                step_slug = self._follow_renames(declared, current_slugs)
                if step_slug != declared:
                    renamed_steps[declared] = step_slug
                    self.warnings.append(
                        f"user_journey '{name}': step slug '{declared}' was renamed to "
                        f"'{step_slug}' — update kc.toml")
                if step_slug in self.entities or step_slug in current_slugs:
                    resolved_steps.append(step_slug)
                else:
                    unresolved_steps.append(step_slug)
                    self.warnings.append(
                        f"user_journey '{name}': step slug '{step_slug}' does not "
                        f"resolve to any compiled entity — dropped")
            if not unresolved_steps:
                status = "complete"
            elif resolved_steps:
                status = "partial"
            else:
                status = "invalid"
            payload = {"name": name, "steps": resolved_steps, "status": status,
                      "unresolved_steps": unresolved_steps}
            if renamed_steps:
                # only when a step followed rename lineage, so other journeys keep their hash
                payload["renamed_steps"] = dict(sorted(renamed_steps.items()))
            self._put(self._entity("user_journey", f"user-journey/{slugify(name)}",
                                   name, payload),
                      rule="natural_key", signals={}, facts=[f])

    _MAX_RENAME_HOPS = 32

    def _follow_renames(self, slug: str, current_slugs: set[str]) -> str:
        """The slug a journey step goes by now. A pair from this compile always
        applies (its old slug is still in current state, about to be removed);
        persisted lineage applies only once the old slug resolves nowhere, so a
        path re-created after a rename keeps its own slug."""
        lineage = {**self.current.renames, **self.renames}
        seen = {slug}
        for _ in range(self._MAX_RENAME_HOPS):
            live = slug in self.entities or slug in current_slugs
            if slug not in self.renames and (live or slug not in lineage):
                break
            nxt = lineage[slug]
            if nxt in seen:
                break
            seen.add(nxt)
            slug = nxt
        return slug

    def _pull_requests(self) -> None:
        for f in self._facts_of("pr_observed"):
            number = f.payload["number"]
            payload = {k: v for k, v in sorted(f.payload.items())}
            self._put(self._entity("pull_request", f"pull-request/{number}",
                                   f"PR #{number}: {f.payload.get('title', '')}", payload),
                      rule="natural_key", signals={}, facts=[f])

    def _jira_stories(self) -> None:
        # Natural key = issue key (architecture.md §6: "PR/Jira = their external
        # ids"). Multiple PRs can link the same issue across compiles; merge on
        # slug like any other natural-key entity.
        linked_features_by_key: dict[str, list[str]] = {}
        for f in self._facts_of("jira_feature_link_observed"):
            linked_features_by_key.setdefault(f.payload["jira_key"], []).extend(
                f.payload.get("feature_names", []))
        for f in self._facts_of("jira_observed"):
            key = f.payload["key"]
            payload = {k: v for k, v in sorted(f.payload.items())}
            names = sorted(set(linked_features_by_key.get(key, [])))
            if names:
                payload["linked_feature_names"] = names
            self._put(self._entity("jira_story", f"jira-story/{slugify(key)}",
                                   f"{key}: {f.payload.get('summary', '')}", payload),
                      rule="natural_key", signals={}, facts=[f])

    def _project(self) -> None:
        payload = {"repo_slug": self.repo_slug}
        self._put(self._entity("project", f"project/{slugify(self.repo_slug)}",
                               self.repo_slug, payload),
                  rule="natural_key", signals={}, facts=[])

    def _components(self) -> None:
        by_path: dict[str, list[Fact]] = {}
        for f in self._facts_of("component_observed"):
            by_path.setdefault(f.payload["path"], []).append(f)

        symbols_by_module: dict[str, list[dict]] = {}
        for f in self._facts_of("symbol_observed"):
            module = f.payload["symbol_path"].rsplit(".", 1)[0]
            # methods: symbol_path is module.Class.method — walk up to the module
            while module not in by_path and "." in module:
                module = module.rsplit(".", 1)[0]
            # body_hash (when the analyzer emits it) makes a logic-only edit change
            # the component payload, and therefore its content_hash and delta
            symbol = {"symbol_path": f.payload["symbol_path"], "kind": f.payload["kind"]}
            if f.payload.get("body_hash") is not None:
                symbol["body_hash"] = f.payload["body_hash"]
            symbols_by_module.setdefault(module, []).append(symbol)

        # router declarations and mounts, kept on the component that declares
        # them: PR compiles read this from current state to find which files
        # must be re-collected so route prefixes compose the same as a full compile
        module_of_file = {f.payload["file"]: f.payload["path"] for f in self._facts_of("component_observed")}
        routing_by_module: dict[str, dict[str, list]] = {}
        for f in self._facts_of("router_observed"):
            module = module_of_file.get(f.payload["file"])
            if module is not None:
                routing_by_module.setdefault(module, {"routers": [], "mounts": []})["routers"].append(
                    {"router": f.payload["router"], "prefix": f.payload["prefix"]})
        for f in self._facts_of("router_mount_observed"):
            module = module_of_file.get(f.payload["file"])
            if module is not None:
                routing_by_module.setdefault(module, {"routers": [], "mounts": []})["mounts"].append(
                    {"parent": f.payload["parent"], "child": f.payload["child"],
                     "prefix": f.payload["prefix"]})

        deps_by_module: dict[str, list[str]] = {}
        for f in self._facts_of("dependency_observed"):
            deps_by_module.setdefault(f.payload["from_path"], []).append(f.payload["to_path"])

        # internal-ness resolves against observed ∪ current-state components — a PR
        # slice importing an out-of-scope module must not classify it as external
        internal = set(by_path) | {e.payload["path"] for e in self.current.entities
                                   if e.entity_type == "component"}
        # language family per component and per source file, so import resolution
        # never crosses languages (candidate wins over current state)
        self._family_of: dict[str, str] = {}
        self._family_of_file: dict[str, str] = {}
        for e in self.current.entities:
            if e.entity_type == "component" and e.payload.get("language"):
                family = _language_family(e.payload["language"])
                self._family_of[e.payload["path"]] = family
                for file in e.payload.get("files", []):
                    self._family_of_file[file] = family
        for path, group in by_path.items():
            family = _language_family(group[0].payload["language"])
            self._family_of[path] = family
            for f in group:
                self._family_of_file[f.payload["file"]] = family
        for path in sorted(by_path):
            group = by_path[path]
            deps = sorted(set(deps_by_module.get(path, [])))
            family = self._family_of[path]
            payload = {
                "path": path,
                "kind": group[0].payload["kind"],
                "language": group[0].payload["language"],
                "files": sorted({f.payload["file"] for f in group}),
                "symbols": sorted(symbols_by_module.get(path, []), key=lambda s: s["symbol_path"]),
                "internal_dependencies": [d for d in deps
                                          if self._resolve_internal(d, internal, family)],
                # external dependency coordinates (ecosystem-agnostic; resolution is post-V1)
                "external_dependencies": [d for d in deps
                                          if not self._resolve_internal(d, internal, family)],
            }
            if path in routing_by_module:
                # only present when the module declares routers or mounts, so
                # components without routing keep their payload (and hash) as is
                routing = routing_by_module[path]
                payload["routing"] = {
                    "routers": sorted(routing["routers"], key=lambda r: (r["router"], r["prefix"])),
                    "mounts": sorted(routing["mounts"],
                                     key=lambda m: (m["parent"], m["child"], m["prefix"])),
                }
            self._put(self._entity("component", f"component/{slugify(path)}", path, payload),
                      rule="natural_key", signals={}, facts=group)

    def _mutation_scores(self) -> None:
        """Item 2 of the QA-agent-grounding backlog: attach mutation-kill data
        (collectors/mutation.py, opt-in) to the Component entity it names, by
        exact dotted-path match against the already-built `path` payload field
        — the same natural key `_components` used, no new identity mechanism.
        A scores-file module naming a path not observed this compile (not yet
        instrumented, or a stale entry) is silently skipped, same class as the
        external-dependency and unresolved-coverage-target precedents already
        in this file."""
        for f in self._facts_of("mutation_score_observed"):
            slug = f"component/{slugify(f.payload['module'])}"
            component = self.entities.get(slug)
            if component is None or component.entity_type != "component":
                continue
            killed, survived, timeout = f.payload["killed"], f.payload["survived"], f.payload["timeout"]
            total = killed + survived + timeout
            payload = {
                **component.payload,
                "mutation_kill_rate": round(killed / total, 4) if total else None,
                "mutation_sample": {"killed": killed, "survived": survived,
                                    "timeout": timeout, "total": total},
            }
            self._put(self._entity("component", slug, component.name, payload,
                                   anchors=component.anchors),
                      rule="natural_key", signals={}, facts=[f])

    def _state_models(self) -> None:
        """ADR-023: aggregate state_transition_observed facts (python_analyzer,
        structural transition heuristic) into one state_model entity per
        (owning module, field) — deterministic identity, Component granularity
        (not the mutated object's class: real code mutates dynamically-typed
        objects — e.g. a `model_cls`-parameterized helper — that static
        analysis can't resolve to a class). A transition whose module doesn't
        resolve to a compiled Component this compile is silently skipped, same
        class as the mutation-score precedent above."""
        component_paths = {e.payload["path"] for e in self.entities.values()
                           if e.entity_type == "component"}
        by_key: dict[tuple[str, str], list[Fact]] = {}
        for f in self._facts_of("state_transition_observed"):
            module = f.anchors[0].symbol_path if f.anchors else None
            if module is None or module not in component_paths:
                continue
            by_key.setdefault((module, f.payload["field"]), []).append(f)

        for module_path, field_name in sorted(by_key):
            group = by_key[(module_path, field_name)]
            states: set[str] = set()
            transitions: list[dict] = []
            seen_edges: set[tuple[str | None, str]] = set()
            for f in group:
                from_state, to_state = f.payload["from_state"], f.payload["to_state"]
                states.add(to_state)
                if from_state is not None:
                    states.add(from_state)
                edge = (from_state, to_state)
                if edge not in seen_edges:
                    seen_edges.add(edge)
                    transitions.append({"from": from_state, "to": to_state,
                                        "confidence": f.payload["confidence"]})
            transitions.sort(key=lambda t: (t["from"] or "", t["to"]))
            froms = {t["from"] for t in transitions if t["from"] is not None}
            terminal_states = sorted(states - froms)
            name = f"{module_path} {field_name}"
            slug = f"state-model/{slugify(name)}"
            payload = {
                "owner_component": module_path, "field": field_name,
                "states": sorted(states), "transitions": transitions,
                "terminal_states": terminal_states,
            }
            self._put(self._entity("state_model", slug, name, payload),
                      "natural_key", {}, group)

    _MIN_ESCAPED_DEFECT_SAMPLE = 3  # ADR-020 Failure Modes: withhold below this, sparse-sample noise

    def _escaped_defects(self) -> None:
        """ADR-020: forward-looking, PR-triggered correlation between bug-fix
        PRs and whether the component(s) they touched already had compiled
        Test Coverage (a `covers` relationship) as of `self.current` — the
        state immediately preceding this compile, by construction (ADR-003).
        Deliberately NOT the `kc-covers:` declared-coverage header: that is
        never durably compiled (validation.py's score_test is a stateless
        CLI computation with no persistence path), so the only durable
        coverage signal this IR actually has is the compiled `covers` edge —
        an implementation-time resolution of a choice ADR-020 left open.
        Cumulative counts carry forward via self.current's prior Component
        payload, same re-merge pattern as _mutation_scores; a fix touching a
        file whose component isn't resolved this compile is silently
        skipped, same class as that precedent too."""
        component_paths = {e.payload["path"]: e.slug for e in self.entities.values()
                           if e.entity_type == "component"}
        covered_before_cache: dict[str, bool] = {}

        def was_covered_before(slug: str) -> bool:
            if slug not in covered_before_cache:
                covered_before_cache[slug] = any(
                    r.relation_type == "covers" and r.to_slug == slug
                    for r in self.current.relationships)
            return covered_before_cache[slug]

        facts_by_component: dict[str, list[Fact]] = {}
        covered_hits: dict[str, int] = {}
        for f in self._facts_of("escaped_defect_observed"):
            touched: set[str] = set()
            for file_path in f.payload["changed_files"]:
                module = _file_to_module(file_path)
                resolved = self._resolve_internal(module, set(component_paths))
                if resolved:
                    touched.add(component_paths[resolved])
            for slug in touched:
                facts_by_component.setdefault(slug, []).append(f)
                if was_covered_before(slug):
                    covered_hits[slug] = covered_hits.get(slug, 0) + 1

        for slug, contributing in facts_by_component.items():
            component = self.entities.get(slug)
            if component is None:
                continue
            prior = next((e for e in self.current.entities if e.slug == slug), None)
            prior_fix_count = prior.payload.get("escaped_defect_fix_count", 0) if prior else 0
            prior_covered_count = (prior.payload.get("escaped_defect_covered_fix_count", 0)
                                   if prior else 0)
            fix_count = prior_fix_count + len(contributing)
            covered_count = prior_covered_count + covered_hits.get(slug, 0)
            trust_score = (round(1 - covered_count / fix_count, 4)
                          if fix_count >= self._MIN_ESCAPED_DEFECT_SAMPLE else None)
            payload = {**component.payload,
                      "escaped_defect_fix_count": fix_count,
                      "escaped_defect_covered_fix_count": covered_count,
                      "escaped_defect_trust_score": trust_score}
            self._put(self._entity("component", slug, component.name, payload,
                                   anchors=component.anchors),
                      rule="natural_key", signals={}, facts=contributing)

    def _resolve_internal(self, dep: str, internal: set[str],
                          family: str | None = None) -> str | None:
        """Longest internal component prefix of a dotted import path, else None.
        `family` is the importer's language family; when given, the suffix
        fallback only considers components of that family."""
        parts = dep.split(".")
        for i in range(len(parts), 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in internal:
                return prefix

        # Import-root mismatch fallback (dogfood finding): the project's real Python
        # import root can be a subdirectory (e.g. `backend/` on PYTHONPATH) rather
        # than the repo root used to name components, so a bare import like `db`
        # shares no prefix with its component `backend.db`. Resolve via dotted-suffix
        # match instead; only when exactly one internal component qualifies, so
        # ambiguous names stay unresolved (ADR-004's over-split-over-merge bias).
        # Same language family only (dogfood finding on frida): Python's stdlib
        # `import types` must not resolve to a lone TypeScript `widgets/types.ts`.
        candidates = [c for c in internal if c == dep or c.endswith("." + dep)]
        if family is not None:
            family_of = getattr(self, "_family_of", {})
            candidates = [c for c in candidates if family_of.get(c) == family]
        if len(candidates) == 1:
            return candidates[0]
        return None

    # -- router prefix composition (FastAPI include_router, Express app.use) -------

    _MAX_ROUTE_PREFIXES = 16  # a router mounted under many parents is rare; cap the fan-out

    def _router_key(self, ref: str, internal: set[str]) -> str:
        """Dotted router ref (`app.api.items.router`) -> `<component>:<attr>`.
        The component is the longest prefix that names an internal component
        (dotted-suffix fallback for an import root below the repo root, same
        rule as _resolve_internal). A ref with no internal component, such as a
        third-party app object, keys on itself."""
        parts = ref.split(".")
        for i in range(len(parts) - 1, 0, -1):
            if ".".join(parts[:i]) in internal:
                return f"{'.'.join(parts[:i])}:{'.'.join(parts[i:])}"
        for i in range(len(parts) - 1, 0, -1):
            head = ".".join(parts[:i])
            candidates = [c for c in internal if c.endswith("." + head)]
            if len(candidates) == 1:
                return f"{candidates[0]}:{'.'.join(parts[i:])}"
        return ref

    @staticmethod
    def _join_route(prefix: str, route: str) -> str:
        if not prefix:
            return route
        if not route:
            return prefix  # `@router.post("")` under prefix /items -> /items
        return prefix.rstrip("/") + (route if route.startswith("/") else "/" + route)

    def _route_prefixes(self, internal: set[str]) -> dict[str, list[str]]:
        """Router key -> every full prefix it is served under. full(R) is the
        full prefix of each parent R is mounted on, plus the mount's prefix,
        plus R's own prefix. A router mounted nowhere is served at its own prefix.
        Cycles (a.use(b); b.use(a)) are cut at the repeated router."""
        own: dict[str, str] = {}
        for f in self._facts_of("router_observed"):
            own[self._router_key(f.payload["router"], internal)] = f.payload["prefix"]
        parents: dict[str, set[tuple[str, str]]] = {}
        for f in self._facts_of("router_mount_observed"):
            child = self._router_key(f.payload["child"], internal)
            parents.setdefault(child, set()).add(
                (self._router_key(f.payload["parent"], internal), f.payload["prefix"]))

        # No memo: a result computed while some routers are on the visiting path
        # (cycle-cut) differs from the top-level one, and reusing it would make
        # the output depend on iteration order. Routing graphs are small.
        def full(key: str, visiting: frozenset[str]) -> list[str]:
            mine = own.get(key, "")
            result: set[str] = set()
            for parent, mount_prefix in sorted(parents.get(key, ())):
                if parent in visiting:
                    continue
                for p in full(parent, visiting | {key}):
                    result.add(self._join_route(self._join_route(p, mount_prefix), mine))
            return sorted(result)[: self._MAX_ROUTE_PREFIXES] if result else [mine]

        return {key: full(key, frozenset()) for key in set(own) | set(parents)}

    def _apis(self) -> None:
        # same observed ∪ current-state set _components resolves imports against
        internal = {f.payload["path"] for f in self._facts_of("component_observed")} | \
            {e.payload["path"] for e in self.current.entities if e.entity_type == "component"}
        prefixes = self._route_prefixes(internal)
        by_key: dict[tuple[str, str], list[tuple[Fact, str]]] = {}
        for f in self._facts_of("api_endpoint_observed"):
            routes = [f.payload["route"]]
            if f.payload.get("router"):
                key = self._router_key(f.payload["router"], internal)
                routes = [self._join_route(p, f.payload["route"]) for p in prefixes.get(key, [""])]
            for raw in routes:
                by_key.setdefault((f.payload["method"], normalize_route(raw)), []).append((f, raw))

        slugs = self._api_slugs(sorted(by_key))
        for method, route in sorted(by_key):
            pairs = by_key[(method, route)]
            group = list({id(f): f for f, _ in pairs}.values())
            sources = sorted({f.payload["source"] for f in group})
            payload = {
                "method": method, "route": route,
                "raw_routes": sorted({raw for _, raw in pairs}),
                "handlers": sorted({f.payload["handler"] for f in group if f.payload.get("handler")}),
                "sources": sources,  # discrepancy attribute (ir.md §4.3): visible, never auto-resolved
                "files": sorted({f.payload["file"] for f in group}),
            }
            name = f"{method} {route}"
            self._put(self._entity("api", slugs[(method, route)], name, payload),
                      "natural_key", {}, group)

    def _api_slugs(self, keys: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
        """Slug per (method, normalized route), with ADR-004's collision rule:
        a deterministic `-2`, `-3`, ... suffix at mint time. slugify drops `{}`
        and trailing slashes, so `GET /users`, `GET /users/{}` and `GET /users/`
        all slugify to `api/get-users`. Before this, all but one were silently
        overwritten.

        An API that already exists in current state keeps its slug (identity is
        the natural key, so the slug is looked up by it), and those slugs are
        reserved first. A newly seen colliding key then takes the next free
        suffix, in sorted key order. No existing slug ever changes; only routes
        that previously had no entity get a suffixed one. As ADR-004 accepts,
        a rebuild into an empty database may hand out suffixes differently
        ("equivalent modulo slug renaming")."""
        existing = {(e.payload["method"], e.payload["route"]): e.slug
                    for e in self.current.entities if e.entity_type == "api"}
        taken = set(existing.values())
        slugs: dict[tuple[str, str], str] = {}
        for key in keys:
            if key in existing:
                slugs[key] = existing[key]
        for key in keys:
            if key in slugs:
                continue
            base = f"api/{slugify(f'{key[0]} {key[1]}')}"
            slug, n = base, 1
            while slug in taken:
                n += 1
                slug = f"{base}-{n}"
            slugs[key] = slug
            taken.add(slug)
        return slugs

    def _test_coverage(self) -> None:
        # `targets` stays the module's import list (unchanged shape). Per-test
        # `call`/`http` evidence is kept aside for _p5, which resolves it to
        # components and records the strongest mechanism per component.
        targets_by_module: dict[str, list[str]] = {}
        self._test_evidence: dict[str, list[dict]] = {}
        for f in self._facts_of("test_target_observed"):
            mechanism = f.payload.get("mechanism", "import")
            if mechanism == "import":
                targets_by_module.setdefault(f.payload["test_module"], []).append(f.payload["target_path"])
            elif f.payload.get("node_id"):
                self._test_evidence.setdefault(f.payload["node_id"], []).append(f.payload)

        for f in self._facts_of("test_case_observed"):
            node_id = f.payload["node_id"]
            # the analyzer owns its module convention (ADR-006 boundary): the fact
            # carries it; Normalize never derives it with language assumptions
            test_module = f.payload["module"]
            payload = {
                "node_id": node_id,
                "framework": f.payload["framework"],
                "file": f.payload["file"],
                "targets": sorted(set(targets_by_module.get(test_module, []))),
            }
            self._put(self._entity("test_coverage", f"test-coverage/{slugify(node_id)}",
                                   node_id, payload, anchors=f.anchors),
                      "natural_key", {}, [f])

    # -- rename lineage (normalize.md §4.1) -------------------------------------------

    _LINEAGE_TYPES = ("component", "test_coverage", "state_model")  # path-keyed natural keys

    def _rename_lineage(self, rename_map: dict[str, str]) -> None:
        """Pair each path-keyed deterministic entity whose file git renamed with
        the entity now observed at the renamed key. Its natural key (ADR-004)
        changed, so it is still a different entity with a different slug; the
        pair only records lineage, which Diff writes into the delta so stale
        citations can be followed. Pairs only on a unique match in both
        directions (DP 8: never guess). Components go first because a state
        model's key is its owning component's path."""
        if not rename_map:
            return
        current_slugs = {e.slug for e in self.current.entities}
        gone = sorted((e for e in self.current.entities if e.slug not in self.entities),
                      key=lambda e: e.slug)
        fresh = [self.entities[s] for s in sorted(self.entities) if s not in current_slugs]
        moved_paths: dict[str, str] = {}
        for entity_type in self._LINEAGE_TYPES:
            by_key: dict[object, list[Entity]] = {}
            for e in fresh:
                if e.entity_type == entity_type:
                    by_key.setdefault(self._lineage_key(e), []).append(e)
            pairs: dict[str, list[str]] = {}
            for old in gone:
                if old.entity_type != entity_type:
                    continue
                key = self._renamed_lineage_key(old, rename_map, moved_paths)
                hits = by_key.get(key, []) if key is not None else []
                if len(hits) == 1:
                    pairs.setdefault(hits[0].slug, []).append(old.slug)
            for new_slug, olds in sorted(pairs.items()):
                if len(olds) != 1:
                    continue
                self.renames[olds[0]] = new_slug
                if entity_type == "component":
                    old = next(e for e in gone if e.slug == olds[0])
                    moved_paths[old.payload["path"]] = self.entities[new_slug].payload["path"]

    @staticmethod
    def _lineage_key(e: Entity) -> object:
        if e.entity_type == "component":
            return tuple(sorted(e.payload.get("files", [])))
        if e.entity_type == "test_coverage":
            return e.payload.get("node_id")
        return (e.payload.get("owner_component"), e.payload.get("field"))

    @staticmethod
    def _renamed_lineage_key(e: Entity, rename_map: dict[str, str],
                             moved_paths: dict[str, str]) -> object | None:
        """The key `e` would have after the renames, or None if none applies."""
        if e.entity_type == "component":
            files = e.payload.get("files", [])
            if not any(f in rename_map for f in files):
                return None
            return tuple(sorted(rename_map.get(f, f) for f in files))
        if e.entity_type == "test_coverage":
            file, node_id = e.payload.get("file"), e.payload.get("node_id", "")
            if file not in rename_map or not node_id.startswith(file):
                return None
            return rename_map[file] + node_id[len(file):]
        owner = e.payload.get("owner_component")
        if owner not in moved_paths:
            return None
        return (moved_paths[owner], e.payload.get("field"))

    # -- P3 + P4: the cascade (normalize.md §5–6) ------------------------------------

    _CANDIDATE_TYPE = {
        "feature_candidate": "feature",
        "business_rule_candidate": "business_rule",
        "risk_candidate": "risk",
    }

    def _p3_p4_candidates(self, rename_map: dict[str, str]) -> None:
        candidates = sorted(
            (f for f in self.facts if f.fact_type in LLM_CANDIDATE_FACT_TYPES),
            key=lambda f: f.content_hash,  # deterministic processing order (ir.md §4.2)
        )
        for cand in candidates:
            if not cand.anchors:
                # ADR-004 hard requirement: no anchors, no identity participation
                self.warnings.append(
                    f"rejected {cand.fact_type} '{cand.payload.get('name', '?')}': missing anchors")
                continue
            self._match_or_mint(cand, rename_map)

    def _match_or_mint(self, cand: Fact, rename_map: dict[str, str]) -> None:
        entity_type = self._CANDIDATE_TYPE[cand.fact_type]
        name = cand.payload["name"]
        # pool: current-state entities of this type ∪ already-processed (minted/matched) this compile
        pool = [e for e in self.current.entities if e.entity_type == entity_type
                and e.slug not in self.entities]
        minted = [e for e in self.entities.values() if e.entity_type == entity_type]
        targets = sorted(pool + minted, key=lambda e: e.slug)

        # Rule 1 — external key
        ext = cand.payload.get("external_key")
        if ext:
            hits = [e for e in targets if e.payload.get("external_key") == ext]
            if hits:
                self._absorb(hits[0], cand, entity_type, "external_key", {})
                return

        # Rule 2 — anchor overlap (primary evidence)
        scored = []
        for e in targets:
            mapped = tuple(_map_anchor(a, rename_map) for a in e.anchors)
            score = anchor_overlap(cand.anchors, mapped)
            if score >= self.config.t_anchor:
                scored.append((score, e.slug, e))
        if scored:
            score, _, e = max(scored, key=lambda s: (s[0], s[1]))  # ties: lexicographic slug
            self._absorb(e, cand, entity_type, "anchor_overlap", {"anchor_overlap": round(score, 4)})
            return

        # Rule 3 — name similarity (last resort, component-scoped)
        cand_components = set(cand.payload.get("related_components", []))
        scored = []
        for e in targets:
            if not cand_components & set(e.payload.get("related_components", [])):
                continue
            sim = name_similarity(name, e.name)
            if sim >= self.config.t_name:
                scored.append((sim, e.slug, e))
        if scored:
            sim, _, e = max(scored, key=lambda s: (s[0], s[1]))
            self._absorb(e, cand, entity_type, "name_similarity", {"name_similarity": round(sim, 4)})
            return

        # Mint (over-split beats over-merge)
        slug = self._mint_slug(entity_type, name)
        payload = {k: v for k, v in sorted(cand.payload.items())}
        self._put(self._entity(entity_type, slug, name, payload, anchors=cand.anchors),
                  "minted", {}, [cand])

    def _mint_slug(self, entity_type: str, name: str) -> str:
        base = f"{entity_type.replace('_', '-')}/{slugify(name)}"
        taken = set(self.entities) | {e.slug for e in self.current.entities}
        if base not in taken:
            return base
        i = 2
        while f"{base}-{i}" in taken:
            i += 1
        return f"{base}-{i}"

    def _absorb(self, target: Entity, cand: Fact, entity_type: str, rule: str,
                signals: dict[str, float]) -> None:
        """Match: merge candidate into target (P4) + anchor currency rewrite (P7)."""
        existing = self.entities.get(target.slug)
        if existing is None:
            # first touch this compile: candidate content wins, identity preserved
            payload = {k: v for k, v in sorted(cand.payload.items())}
            merged = self._entity(entity_type, target.slug, cand.payload["name"], payload,
                                  anchors=cand.anchors)
        else:
            # co-matched candidates in one compile (normalize.md §6): deterministic merge
            richer, other = self._merge_order(existing, cand)
            payload = dict(richer_payload := richer)
            anchors = tuple(sorted(set(existing.anchors) | set(cand.anchors),
                                   key=lambda a: (a.file_path, a.symbol_path or "", a.span or (0, 0))))
            if existing.payload.get("statement") and cand.payload.get("statement") \
                    and existing.payload["statement"] != cand.payload["statement"]:
                self.conflicts.append({
                    "slug": target.slug, "kind": "co_match_content_disagreement",
                    "values": sorted([existing.payload["statement"], cand.payload["statement"]]),
                })
            merged = self._entity(entity_type, target.slug, payload.get("name", existing.name),
                                  payload, anchors=anchors)
        self._put(merged, rule, signals, [cand])

    @staticmethod
    def _merge_order(existing: Entity, cand: Fact) -> tuple[dict, dict]:
        """Content from the side with more anchors; ties -> smaller content hash (deterministic)."""
        cand_payload = {k: v for k, v in sorted(cand.payload.items())}
        e_rank = (len(existing.anchors), existing.content_hash)
        c_rank = (len(cand.anchors), content_hash(cand_payload))
        return (cand_payload, existing.payload) if (c_rank > e_rank) else (existing.payload, cand_payload)

    # -- P5: relationships -------------------------------------------------------------

    # -- coverage quality (QA-signal review finding 2) --------------------------------

    COVERAGE_CONFIDENCE = {"http": 0.8, "call": 0.7, "import": 0.3}

    @staticmethod
    def _route_segments(route: str) -> list[str]:
        route = route.split("?", 1)[0].split("#", 1)[0]
        return [seg for seg in route.strip("/").split("/") if seg]

    def _http_components(self, method: str, path: str, components: dict[str, str]) -> set[str]:
        """Components defining the API a test request hits. Literal segments
        must match, and a `{}`/`:param` segment matches any one segment. When
        several routes match (`/items/recent` and `/items/{}`), only the most
        specific (fewest params) count."""
        if not hasattr(self, "_api_index"):
            apis = {e.slug: e for e in self.current.entities if e.entity_type == "api"}
            apis.update({e.slug: e for e in self.entities.values() if e.entity_type == "api"})
            self._api_index = [
                (e.payload["method"], self._route_segments(e.payload["route"]),
                 frozenset(e.payload.get("files", [])))
                for e in apis.values()]
        want = self._route_segments(path)
        best: tuple[int, set[str]] = (10**6, set())
        for api_method, segs, files in self._api_index:
            if api_method != method or len(segs) != len(want):
                continue
            params = 0
            for pat, seg in zip(segs, want):
                if pat == "{}" or pat.startswith(":"):
                    params += 1
                elif pat != seg:
                    break
            else:
                comps = {components[r] for f in files
                         if (r := self._resolve_internal(_file_to_module(f), set(components)))}
                if params < best[0]:
                    best = (params, set(comps))
                elif params == best[0]:
                    best[1].update(comps)
        return best[1]

    def _covers(self, e: Entity, components: dict[str, str]) -> None:
        """`covers` edges for one test, plus the strongest evidence per covered
        component in the payload's `coverage` list: http (0.8) > call (0.7) >
        import (0.3). Edges keep their old meaning (any evidence); the
        confidence is what coverage_for / impact_plan judge a gap by."""
        best: dict[str, tuple[float, str]] = {}

        def offer(slug: str, mechanism: str) -> None:
            conf = self.COVERAGE_CONFIDENCE[mechanism]
            if slug not in best or conf > best[slug][0]:
                best[slug] = (conf, mechanism)

        family = getattr(self, "_family_of_file", {}).get(e.payload["file"])
        for target in e.payload["targets"]:
            resolved = self._resolve_internal(target, set(components), family)
            if resolved:
                offer(components[resolved], "import")
            # unresolved targets are external imports (pytest, stdlib, deps)
        for ev in getattr(self, "_test_evidence", {}).get(e.payload["node_id"], []):
            if ev["mechanism"] == "call":
                resolved = self._resolve_internal(ev["target_path"], set(components))
                if resolved:
                    offer(components[resolved], "call")
            elif ev["mechanism"] == "http":
                for slug in self._http_components(ev["method"], ev["target_path"], components):
                    offer(slug, "http")

        for slug in best:
            self.relationships.add((e.slug, "covers", slug))
        coverage = [{"component": slug, "mechanism": mech, "confidence": conf}
                    for slug, (conf, mech) in sorted(best.items())]
        if coverage != e.payload.get("coverage"):
            self.entities[e.slug] = self._entity(
                "test_coverage", e.slug, e.name, {**e.payload, "coverage": coverage},
                anchors=e.anchors)

    def _p5_relationships(self) -> None:
        observed = {e.payload["path"]: e.slug for e in self.entities.values()
                    if e.entity_type == "component"}
        # resolution map includes current-state components (candidate wins) so a PR
        # slice can link into out-of-scope components; edges originate from observed only
        components = {e.payload["path"]: e.slug for e in self.current.entities
                      if e.entity_type == "component"}
        components.update(observed)
        project_slug = next(s for s, e in sorted(self.entities.items())
                            if e.entity_type == "project")

        # Containment derives from paths alone, so it is generated over ALL known
        # components (observed ∪ current) — otherwise a PR slice would make the
        # always-observed Project (or a touched package) silently drop `contains`
        # edges to out-of-scope children (verify-chain finding).
        for path in sorted(components):
            parent = path.rsplit(".", 1)[0] if "." in path else None
            if parent and parent in components:
                self.relationships.add((components[parent], "contains", components[path]))
            else:
                self.relationships.add((project_slug, "contains", components[path]))

        # Dependencies come from observed imports only; out-of-scope components'
        # existing edges are protected by the survivor rule in Diff.
        for path in sorted(observed):
            entity = self.entities[observed[path]]
            for dep in entity.payload["internal_dependencies"]:
                resolved = self._resolve_internal(dep, set(components),
                                                  getattr(self, "_family_of", {}).get(path))
                if resolved and resolved != path:
                    self.relationships.add((observed[path], "depends_on", components[resolved]))

        # feature_by_name: lookup for jira_story → feature motivates edges (LLM-derived)
        feature_by_name = {e.name: e.slug for e in self.entities.values()
                           if e.entity_type == "feature"}

        for e in sorted(self.entities.values(), key=lambda e: e.slug):
            if e.entity_type == "api":
                for file in e.payload["files"]:
                    module = _file_to_module(file)
                    resolved = self._resolve_internal(module, set(components))
                    if resolved:
                        self.relationships.add((e.slug, "defined_in", components[resolved]))
            elif e.entity_type == "test_coverage":
                self._covers(e, components)
                    # unresolved targets are external imports (pytest, stdlib, deps) —
                    # already recorded as external_dependencies on the test's component;
                    # warning here would be noise, not DP-8 visibility (dogfood finding)
            elif e.entity_type in ("feature", "business_rule", "risk"):
                rel = {"feature": "implemented_by", "business_rule": "governs", "risk": "affects"}[e.entity_type]
                for comp_path in sorted(e.payload.get("related_components", [])):
                    resolved = self._resolve_internal(comp_path, set(components))
                    if resolved:
                        self.relationships.add((e.slug, rel, components[resolved]))
                    # unresolved paths are external libraries the LLM listed as
                    # related (sqlalchemy, click, ...) — legitimately related but
                    # not linkable entities; silently dropped (dogfood finding,
                    # same class as external coverage targets above)
            elif e.entity_type == "user_journey":
                # ADR-017: traverses edges are unordered (V1 relationships carry no
                # payload, ir.md §3.3) — the journey's own payload["steps"] list is
                # the source of truth for order; these edges only make the journey
                # navigable from the wiki/relations side. Every resolved step gets
                # its edge, including one only in current state: a journey is
                # re-observed on every compile, so its outgoing edges are
                # authoritative, and an edge only to this compile's slice would
                # drop the steps a PR didn't touch (normalize.md §7). Diff never
                # adds an edge to an entity it removes.
                for step_slug in e.payload["steps"]:
                    self.relationships.add((e.slug, "traverses", step_slug))
            elif e.entity_type == "state_model":
                owner_path = e.payload["owner_component"]
                if owner_path in components:
                    self.relationships.add((e.slug, "models", components[owner_path]))
            elif e.entity_type == "jira_story":
                # ir.md §3.3: motivates | Jira Story -> Feature | Pull Request.
                pr_slug = f"pull-request/{e.payload.get('linked_pr')}"
                if e.payload.get("linked_pr") is not None and pr_slug in self.entities:
                    self.relationships.add((e.slug, "motivates", pr_slug))
                # Feature linkage: LLM-derived via jira_feature_link_observed facts;
                # names stored in payload["linked_feature_names"] by _jira_stories().
                for feature_name in e.payload.get("linked_feature_names", []):
                    feature_slug = feature_by_name.get(feature_name)
                    if feature_slug:
                        self.relationships.add((e.slug, "motivates", feature_slug))

    # -- P6: wiki pages (derived in Normalize — ADR-009 boundary) ------------------------

    def _p6_wiki_pages(self) -> None:
        owners = [e for e in self.entities.values()
                  if e.entity_type in ("component", "api", "feature", "business_rule",
                                       "risk", "user_journey", "state_model")]
        for owner in sorted(owners, key=lambda e: e.slug):
            slug = f"wiki-page/{slugify(owner.slug)}"
            payload = {"owner_slug": owner.slug, "page_type": "entity"}
            self._put(self._entity("wiki_page", slug, owner.name, payload),
                      "natural_key", {}, [])
            self.relationships.add((slug, "documents", owner.slug))


# languages whose modules can import each other; anything else is its own family
_LANGUAGE_FAMILIES = {"typescript": "js", "javascript": "js"}


def _language_family(language: str) -> str:
    return _LANGUAGE_FAMILIES.get(language, language)


def _file_to_module(file: str) -> str:
    path = file[:-3] if file.endswith(".py") else file
    parts = [p for p in path.split("/") if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)
