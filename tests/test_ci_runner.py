"""Tests for scripts/ci_runner.py — target-repo CI mirror."""

from __future__ import annotations

from scripts.ci_runner import _has_ruff_config, run_ci_checks


def test_no_ruff_config_skips_lint_with_warning(tmp_path):
    """When the target has no ruff config, lint is skipped and a warning is logged."""
    clone = tmp_path / "fake_clone"
    clone.mkdir()
    # Create a minimal pyproject without [tool.ruff]; tests must run though.
    (clone / "pyproject.toml").write_text('[project]\nname = "fake"\n')
    # Stub a test command that always passes.
    all_passed, results = run_ci_checks(clone, "true")
    assert all_passed is True
    assert "tests" in results
    assert "lint_warning" in results
    assert "ruff_check" not in results
    assert "ruff_format" not in results
    warning_text = results["lint_warning"][1]
    assert "No ruff config detected" in warning_text


def test_ruff_config_runs_check_and_format(tmp_path):
    """When the target has [tool.ruff] in pyproject.toml, both ruff checks run."""
    clone = tmp_path / "fake_clone"
    clone.mkdir()
    (clone / "pyproject.toml").write_text(
        '[project]\nname = "fake"\n\n[tool.ruff]\nline-length = 100\n'
    )
    # Add a syntactically valid, ruff-clean python file so both checks pass.
    (clone / "hello.py").write_text('print("hi")\n')
    _all_passed, results = run_ci_checks(clone, "true")
    assert "tests" in results
    assert "ruff_check" in results
    assert "ruff_format" in results
    assert "lint_warning" not in results
    # All should pass against a trivially-clean file.
    assert results["tests"][0] is True
    # Note: ruff_check and ruff_format may pass or fail depending on the specific
    # ruff config + file contents; we only assert they were attempted.


def test_has_ruff_config_detects_ruff_toml(tmp_path):
    """ruff.toml at the clone root is sufficient signal."""
    clone = tmp_path / "fake_clone"
    clone.mkdir()
    (clone / "ruff.toml").write_text("line-length = 100\n")
    assert _has_ruff_config(clone) is True


def test_ruff_missing_returns_failed_result_not_exception(tmp_path, monkeypatch):
    """Safety F1.3: ruff binary absent must produce a failed check result,
    not crash the cycle with FileNotFoundError."""
    import subprocess as real_subprocess

    from scripts import ci_runner

    clone = tmp_path / "fake_clone"
    clone.mkdir()
    (clone / "pyproject.toml").write_text(
        '[project]\nname = "fake"\n\n[tool.ruff]\nline-length = 100\n'
    )

    original_run = real_subprocess.run

    def fake_run(argv, *args, **kwargs):
        # Let the test command ("true") run normally; simulate ruff absent.
        if argv and argv[0] == "ruff":
            raise FileNotFoundError(2, "No such file or directory: 'ruff'")
        return original_run(argv, *args, **kwargs)

    monkeypatch.setattr(ci_runner.subprocess, "run", fake_run)

    all_passed, results = ci_runner.run_ci_checks(clone, "true")
    # Should not crash. Both ruff checks should be present with passed=False.
    assert "ruff_check" in results
    assert "ruff_format" in results
    assert results["ruff_check"][0] is False
    assert results["ruff_format"][0] is False
    assert "ruff binary not found" in results["ruff_check"][1]
    assert all_passed is False
