"""Normalize (docs/normalize.md): facts -> fully identified Knowledge IR.

Pure function of (facts, current, config) — no clock, no randomness, no unordered
iteration (normalize.md §9 determinism checklist). Never concludes removals (Diff's
job, pipeline.md §5). The LLM never assigns identity (ADR-004).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from knowledge_compiler.ir import (
    LLM_CANDIDATE_FACT_TYPES,
    Anchor,
    Entity,
    Fact,
    Relationship,
    content_hash,
)

# --- Contract types -------------------------------------------------------------


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


# --- Helpers ---------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


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
              repo_slug: str) -> CandidateState:
    n = _Normalizer(facts, current, config, repo_slug)
    return n.run()


class _Normalizer:
    def __init__(self, facts: list[Fact], current: CurrentState, config: Thresholds,
                 repo_slug: str) -> None:
        self.facts = facts
        self.current = current
        self.config = config
        self.repo_slug = repo_slug
        self.entities: dict[str, Entity] = {}
        self.relationships: set[tuple[str, str, str]] = set()
        self.evidence: dict[str, MatchEvidence] = {}
        self.provenance: dict[str, list[dict]] = {}
        self.conflicts: list[dict] = []
        self.warnings: list[str] = []

    def run(self) -> CandidateState:
        rename_map = self._p1_rename_map()
        self._p2_deterministic_entities()
        self._p3_p4_candidates(rename_map)
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

    def _pull_requests(self) -> None:
        for f in self._facts_of("pr_observed"):
            number = f.payload["number"]
            payload = {k: v for k, v in sorted(f.payload.items())}
            self._put(self._entity("pull_request", f"pull-request/{number}",
                                   f"PR #{number}: {f.payload.get('title', '')}", payload),
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
            symbols_by_module.setdefault(module, []).append(
                {"symbol_path": f.payload["symbol_path"], "kind": f.payload["kind"]})

        deps_by_module: dict[str, list[str]] = {}
        for f in self._facts_of("dependency_observed"):
            deps_by_module.setdefault(f.payload["from_path"], []).append(f.payload["to_path"])

        # internal-ness resolves against observed ∪ current-state components — a PR
        # slice importing an out-of-scope module must not classify it as external
        internal = set(by_path) | {e.payload["path"] for e in self.current.entities
                                   if e.entity_type == "component"}
        for path in sorted(by_path):
            group = by_path[path]
            deps = sorted(set(deps_by_module.get(path, [])))
            payload = {
                "path": path,
                "kind": group[0].payload["kind"],
                "language": group[0].payload["language"],
                "files": sorted({f.payload["file"] for f in group}),
                "symbols": sorted(symbols_by_module.get(path, []), key=lambda s: s["symbol_path"]),
                "internal_dependencies": [d for d in deps if self._resolve_internal(d, internal)],
                # external dependency coordinates (ecosystem-agnostic; resolution is post-V1)
                "external_dependencies": [d for d in deps if not self._resolve_internal(d, internal)],
            }
            self._put(self._entity("component", f"component/{slugify(path)}", path, payload),
                      rule="natural_key", signals={}, facts=group)

    @staticmethod
    def _resolve_internal(dep: str, internal: set[str]) -> str | None:
        """Longest internal component prefix of a dotted import path, else None."""
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
        candidates = [c for c in internal if c == dep or c.endswith("." + dep)]
        if len(candidates) == 1:
            return candidates[0]
        return None

    def _apis(self) -> None:
        by_key: dict[tuple[str, str], list[Fact]] = {}
        for f in self._facts_of("api_endpoint_observed"):
            key = (f.payload["method"], normalize_route(f.payload["route"]))
            by_key.setdefault(key, []).append(f)

        for method, route in sorted(by_key):
            group = by_key[(method, route)]
            sources = sorted({f.payload["source"] for f in group})
            payload = {
                "method": method, "route": route,
                "raw_routes": sorted({f.payload["route"] for f in group}),
                "handlers": sorted({f.payload["handler"] for f in group if f.payload.get("handler")}),
                "sources": sources,  # discrepancy attribute (ir.md §4.3): visible, never auto-resolved
                "files": sorted({f.payload["file"] for f in group}),
            }
            name = f"{method} {route}"
            slug = f"api/{slugify(name)}"
            self._put(self._entity("api", slug, name, payload), "natural_key", {}, group)

    def _test_coverage(self) -> None:
        targets_by_module: dict[str, list[str]] = {}
        for f in self._facts_of("test_target_observed"):
            targets_by_module.setdefault(f.payload["test_module"], []).append(f.payload["target_path"])

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
                resolved = self._resolve_internal(dep, set(components))
                if resolved and resolved != path:
                    self.relationships.add((observed[path], "depends_on", components[resolved]))

        for e in sorted(self.entities.values(), key=lambda e: e.slug):
            if e.entity_type == "api":
                for file in e.payload["files"]:
                    module = _file_to_module(file)
                    resolved = self._resolve_internal(module, set(components))
                    if resolved:
                        self.relationships.add((e.slug, "defined_in", components[resolved]))
            elif e.entity_type == "test_coverage":
                for target in e.payload["targets"]:
                    resolved = self._resolve_internal(target, set(components))
                    if resolved:
                        self.relationships.add((e.slug, "covers", components[resolved]))
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

    # -- P6: wiki pages (derived in Normalize — ADR-009 boundary) ------------------------

    def _p6_wiki_pages(self) -> None:
        owners = [e for e in self.entities.values()
                  if e.entity_type in ("component", "api", "feature", "business_rule", "risk")]
        for owner in sorted(owners, key=lambda e: e.slug):
            slug = f"wiki-page/{slugify(owner.slug)}"
            payload = {"owner_slug": owner.slug, "page_type": "entity"}
            self._put(self._entity("wiki_page", slug, owner.name, payload),
                      "natural_key", {}, [])
            self.relationships.add((slug, "documents", owner.slug))


def _file_to_module(file: str) -> str:
    path = file[:-3] if file.endswith(".py") else file
    parts = [p for p in path.split("/") if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)
