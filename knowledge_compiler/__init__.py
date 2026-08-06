"""Knowledge Compiler: compiles engineering artifacts into a structured knowledge base.

Architecture: docs/architecture.md (v1.0, frozen). IR contract: docs/ir.md.
"""

__version__ = "1.0.0"

# IR layer versions, recorded per compile run (ir.md §5).
FACT_VOCABULARY_VERSION = "0.1"
KNOWLEDGE_MODEL_VERSION = "0.1"

# OKF (Open Knowledge Format) spec version the wiki emitter targets, recorded per
# compile run alongside the IR versions above (ADR-013). Spec: github.com/
# GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md
OKF_SPEC_VERSION = "0.2"
