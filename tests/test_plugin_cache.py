"""Tests for scripts/plugin_cache.py — shared numeric-semver plugin-cache resolution."""

from __future__ import annotations

import pytest

from scripts.plugin_cache import newest_version_dir, parse_version

# ── parse_version ──────────────────────────────────────────────────────────


class TestParseVersion:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("2.9.0", (2, 9, 0)),
            ("2.10.1", (2, 10, 1)),
            ("2.9.0-rc1", (2, 9, 0)),
            ("3.0.0+build", (3, 0, 0)),
            (" 2.9.0 ", (2, 9, 0)),
        ],
    )
    def test_parseable(self, name, expected):
        assert parse_version(name) == expected

    @pytest.mark.parametrize("name", ["latest", "main", "", "v2.9.0", "2.9"])
    def test_unparseable_returns_none(self, name):
        assert parse_version(name) is None


# ── newest_version_dir ─────────────────────────────────────────────────────


def _mk(cache, *names):
    for n in names:
        (cache / n).mkdir(parents=True, exist_ok=True)


class TestNewestVersionDir:
    def test_picks_numeric_newest_not_lexicographic(self, tmp_path):
        _mk(tmp_path, "2.9.0", "2.10.0")
        best = newest_version_dir(tmp_path, lambda p: True)
        assert best is not None
        assert best.name == "2.10.0"

    def test_skips_unparseable_names(self, tmp_path):
        _mk(tmp_path, "garbage", "latest", "2.9.0")
        best = newest_version_dir(tmp_path, lambda p: True)
        assert best is not None
        assert best.name == "2.9.0"

    def test_skips_dirs_failing_is_valid(self, tmp_path):
        _mk(tmp_path, "2.9.0", "2.10.0")
        best = newest_version_dir(tmp_path, lambda p: p.name == "2.9.0")
        assert best is not None
        assert best.name == "2.9.0"

    def test_min_version_filter(self, tmp_path):
        _mk(tmp_path, "2.8.0", "2.9.0", "2.10.0")
        best = newest_version_dir(tmp_path, lambda p: True, min_version=(2, 9, 0))
        assert best is not None
        assert best.name == "2.10.0"
        none = newest_version_dir(tmp_path, lambda p: True, min_version=(3, 0, 0))
        assert none is None

    def test_skips_plain_files(self, tmp_path):
        _mk(tmp_path, "2.9.0")
        (tmp_path / "3.0.0").write_text("a file, not a dir")
        best = newest_version_dir(tmp_path, lambda p: True)
        assert best is not None
        assert best.name == "2.9.0"

    def test_none_when_no_candidate(self, tmp_path):
        _mk(tmp_path, "garbage")
        assert newest_version_dir(tmp_path, lambda p: True) is None

    def test_none_when_cache_dir_missing(self, tmp_path):
        assert newest_version_dir(tmp_path / "nope", lambda p: True) is None
