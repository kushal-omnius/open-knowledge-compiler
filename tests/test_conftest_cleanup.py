"""Guards the test-repo cleanup fixture's slug pattern (tests/conftest.py).

Pure regex test, no DB needed — this is the safety net for a fixture that
deletes rows in the shared Postgres instance: a false-positive match against
a real repo slug would delete real dogfood data, not just test cruft.
"""

from conftest import _TEST_SLUG_RE

REAL_REPO_SLUGS = ["repo-a", "open-knowledge-compiler", "repo-b", "my-service"]

TEST_SLUGS = [
    "it-01cfc2d8", "it-a-08ccb3", "it-b-12db4a",
    "inc-072e14a5", "llm-02f7d1ad", "val-034b8cb6",
    "m2-013eef9a", "m2o-04d7a2cf", "vf-0029eda4", "dbg-217df2a0",
]


def test_cleanup_pattern_never_matches_real_repo_slugs():
    for slug in REAL_REPO_SLUGS:
        assert not _TEST_SLUG_RE.match(slug), f"cleanup pattern must not match real repo '{slug}'"


def test_cleanup_pattern_matches_known_test_slug_prefixes():
    for slug in TEST_SLUGS:
        assert _TEST_SLUG_RE.match(slug), f"cleanup pattern should match test slug '{slug}'"
