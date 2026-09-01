"""LLM semantic extractor (pipeline.md §3.2): candidates with mandatory anchors.

Owns the validation gate (reject, log, retry once — never persist malformed),
the cache consultation, and budget accounting (ADR-008). Anchors are built only
from symbol paths the deterministic pass actually observed — the LLM cannot
invent an anchor (ADR-004 evidence discipline).
"""

from __future__ import annotations

from typing import Callable

from pydantic import ValidationError

from knowledge_compiler.ir import Anchor, Artifact, Extraction, Fact, content_hash
from knowledge_compiler.llm.cache import LLMCache, cache_key
from knowledge_compiler.llm.templates import (
    SCHEMA, TEMPLATE_ID, TEMPLATE_VERSION, ExtractionOut, build_prompt,
)


class LLMBudgetExceeded(Exception):
    """Per-run call cap hit: the run fails resumably — cache keeps prepaid work
    (pipeline.md §6.2)."""


_FACT_TYPE = {"business_rules": "business_rule_candidate",
              "features": "feature_candidate",
              "risks": "risk_candidate"}


class LLMSemanticExtractor:
    def __init__(self, provider, cache: LLMCache, max_calls: int,
                 known_symbols: dict[str, list[str]], modules: dict[str, str],
                 skip_files: frozenset[str] = frozenset(),
                 on_progress: Callable[[int, int, str], None] | None = None,
                 known_annotations: dict[str, dict[str, str]] | None = None) -> None:
        """known_symbols: file -> symbol paths observed by the deterministic pass.
        modules: file -> module path. Both come from analyzer facts — the LLM
        layer is grounded in the deterministic skeleton, never the reverse.
        skip_files: excluded from semantic extraction (default: test files —
        their fixtures read as domain rules; dogfood finding).
        on_progress: called (index, total, source_ref) after each eligible file
        completes (cache hit or real call) — this stage is the dominant latency
        in a compile and was previously silent until the whole run finished.
        known_annotations: file -> {local_name: external_key} from kc:external-key:
        source annotations (ADR-022). Injected deterministically post-LLM so the
        cache key is unaffected and the LLM never assigns identity."""
        self.provider = provider
        self.cache = cache
        self.max_calls = max_calls
        self.known_symbols = known_symbols
        self.modules = modules
        self.skip_files = skip_files
        self.on_progress = on_progress
        self.known_annotations: dict[str, dict[str, str]] = known_annotations or {}
        self.calls_made = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.warnings: list[str] = []

    def extract(self, artifacts: list[Artifact]) -> list[Fact]:
        eligible = [a for a in sorted(artifacts, key=lambda a: a.source_ref)
                   if a.source_ref in self.modules and a.content is not None
                   and a.source_ref not in self.skip_files]
        facts: list[Fact] = []
        for i, artifact in enumerate(eligible, start=1):
            output, reason, usage = self._complete_cached(artifact)
            if output is not None:
                facts.extend(self._to_facts(artifact, output))
            if self.on_progress and reason == "changed":
                self.on_progress(i, len(eligible),
                                 f"{artifact.source_ref} --changed "
                                 f"(tokens in={usage['input_tokens']} out={usage['output_tokens']})")
        return facts

    def token_summary(self) -> dict:
        """Real spend for this run — cache hits cost nothing and aren't counted."""
        return {"calls": self.calls_made, "input_tokens": self.total_input_tokens,
                "output_tokens": self.total_output_tokens}

    # -- provider + cache ---------------------------------------------------------

    def _complete_cached(self, artifact: Artifact) -> tuple[ExtractionOut | None, str, dict]:
        """Returns (result, reason, usage) where reason is 'cached' or 'changed' and
        usage is {"input_tokens", "output_tokens"} actually spent on this artifact
        (zero for a cache hit; summed across validation retries on a miss)."""
        key = cache_key(TEMPLATE_ID, TEMPLATE_VERSION, self.provider.model_id,
                        artifact.content_hash)
        cached = self.cache.get(key)
        if cached is not None:
            zero = {"input_tokens": 0, "output_tokens": 0}
            return ExtractionOut.model_validate(cached), "cached", zero  # cache holds validated output only

        prompt = build_prompt(artifact.source_ref, self.modules[artifact.source_ref],
                              self.known_symbols.get(artifact.source_ref, []),
                              artifact.content)
        usage = {"input_tokens": 0, "output_tokens": 0}
        for attempt in (1, 2):  # validation gate: retry once, then skip loudly
            if self.calls_made >= self.max_calls:
                raise LLMBudgetExceeded(
                    f"LLM budget ({self.max_calls} calls) exhausted at {artifact.source_ref}; "
                    "re-run to resume — completed work is cached")
            self.calls_made += 1
            raw = self.provider.complete(prompt, SCHEMA)
            call_usage = getattr(self.provider, "last_usage", None) or usage
            usage = {"input_tokens": usage["input_tokens"] + call_usage.get("input_tokens", 0),
                     "output_tokens": usage["output_tokens"] + call_usage.get("output_tokens", 0)}
            self.total_input_tokens += call_usage.get("input_tokens", 0)
            self.total_output_tokens += call_usage.get("output_tokens", 0)
            try:
                validated = ExtractionOut.model_validate(raw)
            except ValidationError as exc:
                if attempt == 2:
                    self.warnings.append(
                        f"LLM output failed validation twice for {artifact.source_ref}: {exc}")
                    return None, "changed", usage
                continue
            self.cache.put(key, TEMPLATE_ID, TEMPLATE_VERSION, self.provider.model_id,
                           validated.model_dump())
            return validated, "changed", usage
        return None, "changed", usage

    # -- facts ---------------------------------------------------------------------

    def _to_facts(self, artifact: Artifact, output: ExtractionOut) -> list[Fact]:
        extraction = Extraction(method="llm", extractor="llm-semantic",
                                extractor_version="0.1", model_id=self.provider.model_id,
                                template_version=TEMPLATE_VERSION)
        valid_symbols = set(self.known_symbols.get(artifact.source_ref, []))
        annotations = self.known_annotations.get(artifact.source_ref, {})
        facts: list[Fact] = []
        for field, fact_type in _FACT_TYPE.items():
            for item in getattr(output, field):
                anchors = [Anchor(file_path=artifact.source_ref, symbol_path=s)
                           for s in item.symbol_paths if s in valid_symbols]
                if not anchors:
                    # file-level anchor floor: the file itself is real evidence
                    anchors = [Anchor(file_path=artifact.source_ref)]
                payload = {k: v for k, v in sorted(item.model_dump().items())
                           if k != "symbol_paths"}
                # ADR-022: inject external_key from kc:external-key: annotations.
                # Deterministic post-LLM injection — cache key is unaffected.
                # First annotated local name (sorted) among the item's symbol_paths wins.
                if annotations:
                    for sym in sorted(item.symbol_paths):
                        local_name = sym.rsplit(".", 1)[-1]
                        if local_name in annotations:
                            payload["external_key"] = annotations[local_name]
                            break
                facts.append(Fact(fact_type=fact_type, payload=payload,
                                  artifact_refs=(artifact.source_ref,),
                                  extraction=extraction,
                                  content_hash=content_hash(payload),
                                  anchors=tuple(anchors)))
        return facts
