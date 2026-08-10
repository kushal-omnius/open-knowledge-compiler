"""Versioned prompt templates (ADR-008): editing a template bumps its VERSION,
producing cache misses exactly where the change matters and nowhere else.

The response models double as the validation gate: output failing validation is
never persisted (ADR-008 invariant). Schema restrictions per structured outputs:
additionalProperties false everywhere, no length/numeric constraints.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

TEMPLATE_ID = "semantic-extraction"
TEMPLATE_VERSION = "1"

PROMPT = """\
You are the semantic layer of a knowledge compiler for software repositories. \
Extract engineering knowledge from ONE source file. Report only what the code \
actually evidences — no speculation, no generic observations.

File: {path}
Module: {module}
Symbols defined here (the ONLY valid values for symbol_paths):
{symbols}

<source>
{source}
</source>

Return JSON with three lists (empty lists are correct and common):
- business_rules: domain constraints/policies the code enforces (e.g. "discount \
is capped at 20%"). name (short noun phrase), statement (the rule, precisely, \
with concrete values), intent (why it plausibly exists), symbol_paths (which \
listed symbols enforce it), related_components (dotted module paths involved).
- features: user- or system-facing capabilities this file meaningfully implements \
(not utilities). name, narrative (2-3 sentences: what it is FOR), symbol_paths, \
related_components.
- risks: concrete engineering risks evidenced in this code (unbounded input, \
missing error handling at a boundary, security-sensitive logic). name, \
description, category, symbol_paths, related_components.

Rules: symbol_paths must come from the list above. related_components must be \
dotted module paths. A pure test file usually yields empty lists. Do not report \
implementation details as business rules — a rule must be a domain statement."""


class _Item(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    symbol_paths: list[str]
    related_components: list[str]


class BusinessRuleOut(_Item):
    statement: str
    intent: str


class FeatureOut(_Item):
    narrative: str


class RiskOut(_Item):
    description: str
    category: str


class ExtractionOut(BaseModel):
    """The validation gate (ADR-008): parse this or be rejected."""

    model_config = ConfigDict(extra="forbid")

    business_rules: list[BusinessRuleOut]
    features: list[FeatureOut]
    risks: list[RiskOut]


SCHEMA = ExtractionOut.model_json_schema()


def build_prompt(path: str, module: str, symbols: list[str], source: str) -> str:
    symbol_lines = "\n".join(f"- {s}" for s in symbols) or "- (none)"
    return PROMPT.format(path=path, module=module, symbols=symbol_lines, source=source)


# --- Jira → Feature linkage template (ADR-008) ------------------------------------

JIRA_FEATURE_TEMPLATE_ID = "jira-feature-match"
JIRA_FEATURE_TEMPLATE_VERSION = "1"

_JIRA_FEATURE_PROMPT = """\
You are the semantic enrichment layer of a knowledge compiler.
Link a Jira story to the compiled features it motivated.

Jira story: {key}
Summary: {summary}
Description: {description}

Compiled features (name | narrative):
{features}

Which of the above features does this Jira story motivate? A story motivates a \
feature when its summary or description requested or described that user-facing \
capability. Return only names exactly as listed above. Return an empty list if \
no feature clearly matches — be conservative."""


class JiraFeatureMatchOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    motivates: list[str]


JIRA_FEATURE_SCHEMA = JiraFeatureMatchOut.model_json_schema()


def build_jira_feature_prompt(key: str, summary: str, description: str,
                               features: dict[str, str]) -> str:
    lines = "\n".join(f"- {name} | {narrative[:120]}"
                      for name, narrative in sorted(features.items()))
    return _JIRA_FEATURE_PROMPT.format(
        key=key, summary=summary,
        description=(description[:800] if description else "(none)"),
        features=lines or "- (none)",
    )
