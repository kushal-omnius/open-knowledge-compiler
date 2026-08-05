"""OKF v0.2 conformance checker tests (ADR-013) — pure filesystem checks, no DB."""

from pathlib import Path

from knowledge_compiler.wiki.okf_conformance import check_bundle

CONCEPT_OK = """---
type: component
title: "Billing"
slug: component/billing
---

# Billing
"""

CONCEPT_NO_FRONTMATTER = "# Billing\n\nno frontmatter here.\n"

CONCEPT_EMPTY_TYPE = """---
title: "Billing"
---

# Billing
"""

INDEX_OK = """---
okf_version: "0.2"
---

# repo

## Components (1)

- [Billing](component/billing.md)
"""

INDEX_NO_FRONTMATTER_OK = "# repo\n\n- [Billing](component/billing.md)\n"

INDEX_BAD_EXTRA_KEY = """---
okf_version: "0.2"
title: "repo"
---

# repo
"""

LOG_OK = "# Log\n\n## 2026-08-05\n\n**Creation** `component/billing` (component).\n"

LOG_BAD_FRONTMATTER = """---
type: recent_changes
---

# Log
"""


def _write(tmp_path: Path, rel: str, content: str) -> None:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_conformant_bundle_has_no_issues(tmp_path):
    _write(tmp_path, "component/billing.md", CONCEPT_OK)
    _write(tmp_path, "index.md", INDEX_OK)
    _write(tmp_path, "log.md", LOG_OK)
    report = check_bundle(tmp_path)
    assert report.conformant
    assert report.files_checked == 3


def test_index_without_frontmatter_is_conformant(tmp_path):
    """okf_version is optional — index.md may have zero frontmatter."""
    _write(tmp_path, "index.md", INDEX_NO_FRONTMATTER_OK)
    report = check_bundle(tmp_path)
    assert report.conformant


def test_concept_missing_frontmatter_flagged(tmp_path):
    _write(tmp_path, "component/billing.md", CONCEPT_NO_FRONTMATTER)
    report = check_bundle(tmp_path)
    assert not report.conformant
    assert report.issues[0].rule == "missing-frontmatter"


def test_concept_missing_type_flagged(tmp_path):
    _write(tmp_path, "component/billing.md", CONCEPT_EMPTY_TYPE)
    report = check_bundle(tmp_path)
    assert not report.conformant
    assert report.issues[0].rule == "missing-type"


def test_index_extra_frontmatter_key_flagged(tmp_path):
    _write(tmp_path, "index.md", INDEX_BAD_EXTRA_KEY)
    report = check_bundle(tmp_path)
    assert not report.conformant
    assert report.issues[0].rule == "index.md-frontmatter"


def test_log_with_frontmatter_flagged(tmp_path):
    _write(tmp_path, "log.md", LOG_BAD_FRONTMATTER)
    report = check_bundle(tmp_path)
    assert not report.conformant
    assert report.issues[0].rule == "log.md-frontmatter"
