"""Tests for scripts/ci_runner.py — target-repo CI mirror.

These tests stub ``subprocess.run`` so the real Bandit / pip-audit / ruff /
pytest binaries are never invoked. The goal is to verify the dispatch logic
(detection + opt-in + exit-code handling + skip-reason wording), not the
behavior of the third-party tools themselves.
"""

from __future__ import annotations

import subprocess as real_subprocess
import types

from scripts import ci_runner
from scripts.ci_runner import (
    _bandit_config_path,
    _has_ruff_config,
    _pip_audit_referenced_in_workflows,
    run_ci_checks,
)


def _mk_completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Build a CompletedProcess stand-in without re-running anything."""
    return real_subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ── existing ruff/lint behavior (preserved across the shape migration) ────


def test_no_ruff_config_skips_lint_with_warning(tmp_path, monkeypatch):
    """When the target has no ruff config, lint is skipped with status=skip."""
    clone = tmp_path / "fake_clone"
    clone.mkdir()
    (clone / "pyproject.toml").write_text('[project]\nname = "fake"\n')
    monkeypatch.setattr(ci_runner.subprocess, "run", lambda *a, **kw: _mk_completed(0, "ok", ""))
    all_passed, results = run_ci_checks(clone, "true")
    assert all_passed is True
    assert "tests" in results
    assert "lint_warning" in results
    assert results["lint_warning"]["status"] == "skip"
    assert results["lint_warning"]["reason"] == "no_ruff_config"
    assert "ruff_check" not in results
    assert "ruff_format" not in results
    assert "No ruff config detected" in results["lint_warning"]["output"]


def test_ruff_config_runs_check_and_format(tmp_path, monkeypatch):
    """When [tool.ruff] is present, both ruff_check and ruff_format run."""
    clone = tmp_path / "fake_clone"
    clone.mkdir()
    (clone / "pyproject.toml").write_text(
        '[project]\nname = "fake"\n\n[tool.ruff]\nline-length = 100\n'
    )
    monkeypatch.setattr(ci_runner.subprocess, "run", lambda *a, **kw: _mk_completed(0, "", ""))
    _all_passed, results = run_ci_checks(clone, "true")
    assert "tests" in results
    assert "ruff_check" in results
    assert "ruff_format" in results
    assert "lint_warning" not in results
    assert results["ruff_check"]["status"] == "pass"
    assert results["ruff_format"]["status"] == "pass"


def test_has_ruff_config_detects_ruff_toml(tmp_path):
    """ruff.toml at the clone root is sufficient signal."""
    clone = tmp_path / "fake_clone"
    clone.mkdir()
    (clone / "ruff.toml").write_text("line-length = 100\n")
    assert _has_ruff_config(clone) is True


def test_ruff_binary_missing_returns_skip(tmp_path, monkeypatch):
    """F1 (audit cleanup): ruff binary absent is a HOST tooling gap — return
    SKIP so the cycle does not abandon for a missing binary. all_passed stays
    True (SKIP never counts as failure)."""
    clone = tmp_path / "fake_clone"
    clone.mkdir()
    (clone / "pyproject.toml").write_text(
        '[project]\nname = "fake"\n\n[tool.ruff]\nline-length = 100\n'
    )

    def fake_run(argv, *args, **kwargs):
        if argv and argv[0] == "ruff":
            raise FileNotFoundError(2, "No such file or directory: 'ruff'")
        return _mk_completed(0, "ok", "")

    monkeypatch.setattr(ci_runner.subprocess, "run", fake_run)
    monkeypatch.setenv("KAIZEN_SKIP_PIP_AUDIT", "1")

    all_passed, results = run_ci_checks(clone, "true")
    assert "ruff_check" in results
    assert "ruff_format" in results
    assert results["ruff_check"]["status"] == "skip"
    assert results["ruff_format"]["status"] == "skip"
    assert results["ruff_check"]["reason"] == "ruff_binary_missing"
    assert "ruff binary not found" in results["ruff_check"]["output"]
    # SKIP never counts as a failure (F1 main behavioral claim).
    assert all_passed is True


# ── Bandit detection ──────────────────────────────────────────────────────


def test_bandit_config_detected_via_bandit_yaml(tmp_path):
    clone = tmp_path / "c"
    clone.mkdir()
    (clone / "bandit.yaml").write_text("skips: []\n")
    cfg = _bandit_config_path(clone)
    assert cfg is not None
    assert cfg.name == "bandit.yaml"


def test_bandit_config_detected_via_bandit_yml(tmp_path):
    clone = tmp_path / "c"
    clone.mkdir()
    (clone / "bandit.yml").write_text("skips: []\n")
    cfg = _bandit_config_path(clone)
    assert cfg is not None
    assert cfg.name == "bandit.yml"


def test_bandit_config_detected_via_dot_bandit(tmp_path):
    clone = tmp_path / "c"
    clone.mkdir()
    (clone / ".bandit").write_text("[bandit]\nskips: B101\n")
    cfg = _bandit_config_path(clone)
    assert cfg is not None
    assert cfg.name == ".bandit"


def test_bandit_config_detected_via_pyproject_section(tmp_path):
    clone = tmp_path / "c"
    clone.mkdir()
    (clone / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[tool.bandit]\nskips = ["B101"]\n'
    )
    cfg = _bandit_config_path(clone)
    assert cfg is not None
    assert cfg.name == "pyproject.toml"


def test_bandit_no_config_returns_none(tmp_path):
    clone = tmp_path / "c"
    clone.mkdir()
    (clone / "pyproject.toml").write_text('[project]\nname = "x"\n')
    assert _bandit_config_path(clone) is None


# ── Bandit dispatch + exit-code handling ──────────────────────────────────


def _setup_bandit_only_clone(tmp_path):
    """Build a clone where only Bandit is opted in (no ruff, no pip-audit)."""
    clone = tmp_path / "c"
    clone.mkdir()
    (clone / "bandit.yaml").write_text("skips: []\n")
    return clone


def _patch_subprocess_for_bandit(monkeypatch, bandit_rc: int):
    """Patch subprocess so Bandit returns ``bandit_rc``; everything else 0."""

    def fake_run(argv, *args, **kwargs):
        if argv and argv[0] == "bandit":
            return _mk_completed(bandit_rc, f"bandit stdout rc={bandit_rc}", "")
        return _mk_completed(0, "", "")

    monkeypatch.setattr(ci_runner.subprocess, "run", fake_run)


def test_bandit_exit_0_is_pass(tmp_path, monkeypatch):
    clone = _setup_bandit_only_clone(tmp_path)
    _patch_subprocess_for_bandit(monkeypatch, 0)
    monkeypatch.setenv("KAIZEN_SKIP_PIP_AUDIT", "1")
    all_passed, results = run_ci_checks(clone, "true")
    assert results["bandit"]["status"] == "pass"
    assert "reason" not in results["bandit"]
    assert all_passed is True


def test_bandit_exit_1_is_fail_findings(tmp_path, monkeypatch):
    """Exit code 1 = real Bandit findings — fail with reason 'bandit_findings'."""
    clone = _setup_bandit_only_clone(tmp_path)
    _patch_subprocess_for_bandit(monkeypatch, 1)
    monkeypatch.setenv("KAIZEN_SKIP_PIP_AUDIT", "1")
    all_passed, results = run_ci_checks(clone, "true")
    assert results["bandit"]["status"] == "fail"
    assert results["bandit"]["reason"] == "bandit_findings"
    assert all_passed is False


def test_bandit_exit_2_is_fail_config_error(tmp_path, monkeypatch):
    """Exit code 2 = Bandit config file invalid — fail with distinct reason."""
    clone = _setup_bandit_only_clone(tmp_path)
    _patch_subprocess_for_bandit(monkeypatch, 2)
    monkeypatch.setenv("KAIZEN_SKIP_PIP_AUDIT", "1")
    all_passed, results = run_ci_checks(clone, "true")
    assert results["bandit"]["status"] == "fail"
    assert results["bandit"]["reason"] == "bandit_config_error"
    # Confirm config-error reason is NOT conflated with findings reason.
    assert results["bandit"]["reason"] != "bandit_findings"
    assert all_passed is False


def test_bandit_unexpected_exit_code_named(tmp_path, monkeypatch):
    """Exit codes other than 0/1/2 fail with a reason naming the code."""
    clone = _setup_bandit_only_clone(tmp_path)
    _patch_subprocess_for_bandit(monkeypatch, 137)
    monkeypatch.setenv("KAIZEN_SKIP_PIP_AUDIT", "1")
    _all_passed, results = run_ci_checks(clone, "true")
    assert results["bandit"]["status"] == "fail"
    assert results["bandit"]["reason"] == "bandit_unexpected_exit_137"


def test_no_bandit_config_skipped_with_reason(tmp_path, monkeypatch):
    clone = tmp_path / "c"
    clone.mkdir()
    (clone / "pyproject.toml").write_text('[project]\nname = "x"\n')
    monkeypatch.setattr(ci_runner.subprocess, "run", lambda *a, **kw: _mk_completed(0, "", ""))
    monkeypatch.setenv("KAIZEN_SKIP_PIP_AUDIT", "1")
    all_passed, results = run_ci_checks(clone, "true")
    assert results["bandit"]["status"] == "skip"
    assert results["bandit"]["reason"] == "no_bandit_config"
    assert "No Bandit config detected" in results["bandit"]["output"]
    # Skip never counts as a failure.
    assert all_passed is True


def test_bandit_binary_missing_returns_skip(tmp_path, monkeypatch):
    """F2 (audit cleanup): bandit binary absent is a HOST tooling gap — return
    SKIP so the cycle does not abandon for a missing binary. all_passed stays
    True."""
    clone = _setup_bandit_only_clone(tmp_path)

    def fake_run(argv, *args, **kwargs):
        if argv and argv[0] == "bandit":
            raise FileNotFoundError(2, "No such file or directory: 'bandit'")
        return _mk_completed(0, "", "")

    monkeypatch.setattr(ci_runner.subprocess, "run", fake_run)
    monkeypatch.setenv("KAIZEN_SKIP_PIP_AUDIT", "1")
    all_passed, results = run_ci_checks(clone, "true")
    assert results["bandit"]["status"] == "skip"
    assert results["bandit"]["reason"] == "bandit_binary_missing"
    assert all_passed is True


def test_bandit_opt_out_via_KAIZEN_SKIP_CHECKS_env(tmp_path, monkeypatch):
    """F2/F11 (audit cleanup): KAIZEN_SKIP_CHECKS=bandit short-circuits the
    branch entirely — bandit is never invoked even when the target opts in
    via a config file."""
    clone = _setup_bandit_only_clone(tmp_path)

    invoked: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
        invoked.append(list(argv))
        return _mk_completed(0, "", "")

    monkeypatch.setattr(ci_runner.subprocess, "run", fake_run)
    monkeypatch.setenv("KAIZEN_SKIP_CHECKS", "bandit")
    monkeypatch.setenv("KAIZEN_SKIP_PIP_AUDIT", "1")
    all_passed, results = run_ci_checks(clone, "true")
    assert results["bandit"]["status"] == "skip"
    assert results["bandit"]["reason"] == "opted out via KAIZEN_SKIP_CHECKS"
    # Crucially: bandit must NOT have been invoked.
    assert not any(call and call[0] == "bandit" for call in invoked)
    assert all_passed is True


# ── pip-audit detection + dispatch ────────────────────────────────────────


def test_pip_audit_referenced_in_workflow_yaml(tmp_path):
    clone = tmp_path / "c"
    (clone / ".github" / "workflows").mkdir(parents=True)
    (clone / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  audit:\n    steps:\n      - run: pip-audit\n"
    )
    assert _pip_audit_referenced_in_workflows(clone) is True


def test_pip_audit_not_referenced_returns_false(tmp_path):
    clone = tmp_path / "c"
    (clone / ".github" / "workflows").mkdir(parents=True)
    (clone / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  test:\n    steps:\n      - run: pytest\n"
    )
    assert _pip_audit_referenced_in_workflows(clone) is False


def test_pip_audit_no_workflow_dir_returns_false(tmp_path):
    clone = tmp_path / "c"
    clone.mkdir()
    assert _pip_audit_referenced_in_workflows(clone) is False


def test_pip_audit_dispatched_when_workflow_mentions_it(tmp_path, monkeypatch):
    """Workflow opts in via the literal 'pip-audit' string → check runs against requirements.txt."""
    clone = tmp_path / "c"
    (clone / ".github" / "workflows").mkdir(parents=True)
    (clone / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  a:\n    steps:\n      - run: pip-audit --strict\n"
    )
    (clone / "requirements.txt").write_text("requests==2.31.0\n")

    invoked: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
        invoked.append(list(argv))
        if argv and argv[0] == "pip-audit":
            return _mk_completed(0, "no vulns", "")
        return _mk_completed(0, "", "")

    monkeypatch.setattr(ci_runner.subprocess, "run", fake_run)
    monkeypatch.delenv("KAIZEN_SKIP_PIP_AUDIT", raising=False)
    all_passed, results = run_ci_checks(clone, "true")
    assert ["pip-audit", "-r", "requirements.txt"] in invoked
    assert results["pip_audit"]["status"] == "pass"
    assert all_passed is True


def test_pip_audit_scans_all_requirements_files(tmp_path, monkeypatch):
    """Every recognized requirements*.txt is passed via -r in one invocation."""
    clone = tmp_path / "c"
    (clone / ".github" / "workflows").mkdir(parents=True)
    (clone / ".github" / "workflows" / "ci.yml").write_text("run: pip-audit\n")
    (clone / "requirements.txt").write_text("a==1.0\n")
    (clone / "requirements-dev.txt").write_text("b==1.0\n")
    (clone / "requirements-test.txt").write_text("c==1.0\n")

    invoked: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
        invoked.append(list(argv))
        return _mk_completed(0, "", "")

    monkeypatch.setattr(ci_runner.subprocess, "run", fake_run)
    monkeypatch.delenv("KAIZEN_SKIP_PIP_AUDIT", raising=False)
    run_ci_checks(clone, "true")
    pip_audit_calls = [c for c in invoked if c and c[0] == "pip-audit"]
    assert len(pip_audit_calls) == 1
    assert pip_audit_calls[0] == [
        "pip-audit",
        "-r",
        "requirements.txt",
        "-r",
        "requirements-dev.txt",
        "-r",
        "requirements-test.txt",
    ]


def test_pip_audit_skips_when_no_target_requirements(tmp_path, monkeypatch):
    """Workflow opts in but no requirements*.txt exists → skip (do NOT scan host env)."""
    clone = tmp_path / "c"
    (clone / ".github" / "workflows").mkdir(parents=True)
    (clone / ".github" / "workflows" / "ci.yml").write_text("run: pip-audit\n")
    # No requirements*.txt in the clone.

    invoked: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
        invoked.append(list(argv))
        return _mk_completed(0, "", "")

    monkeypatch.setattr(ci_runner.subprocess, "run", fake_run)
    monkeypatch.delenv("KAIZEN_SKIP_PIP_AUDIT", raising=False)
    all_passed, results = run_ci_checks(clone, "true")
    assert results["pip_audit"]["status"] == "skip"
    assert results["pip_audit"]["reason"] == "no_target_requirements"
    # Critically: pip-audit MUST NOT have been invoked (no host-env scan).
    assert not any(call and call[0] == "pip-audit" for call in invoked)
    assert all_passed is True


def test_pip_audit_fail_when_exit_nonzero(tmp_path, monkeypatch):
    clone = tmp_path / "c"
    (clone / ".github" / "workflows").mkdir(parents=True)
    (clone / ".github" / "workflows" / "ci.yml").write_text("run: pip-audit\n")
    (clone / "requirements.txt").write_text("requests==2.31.0\n")

    def fake_run(argv, *args, **kwargs):
        if argv and argv[0] == "pip-audit":
            return _mk_completed(1, "CVE-XXXX-YYYY", "")
        return _mk_completed(0, "", "")

    monkeypatch.setattr(ci_runner.subprocess, "run", fake_run)
    monkeypatch.delenv("KAIZEN_SKIP_PIP_AUDIT", raising=False)
    all_passed, results = run_ci_checks(clone, "true")
    assert results["pip_audit"]["status"] == "fail"
    assert results["pip_audit"]["reason"] == "pip_audit_exit_1"
    assert "CVE" in results["pip_audit"]["output"]
    assert all_passed is False


def test_pip_audit_skipped_when_no_workflow_reference(tmp_path, monkeypatch):
    clone = tmp_path / "c"
    clone.mkdir()
    monkeypatch.setattr(ci_runner.subprocess, "run", lambda *a, **kw: _mk_completed(0, "", ""))
    monkeypatch.delenv("KAIZEN_SKIP_PIP_AUDIT", raising=False)
    all_passed, results = run_ci_checks(clone, "true")
    assert results["pip_audit"]["status"] == "skip"
    assert results["pip_audit"]["reason"] == "no_pip_audit_in_workflows"
    assert all_passed is True


def test_pip_audit_opt_out_via_env_var(tmp_path, monkeypatch):
    """KAIZEN_SKIP_PIP_AUDIT=1 must skip even when workflows opt in."""
    clone = tmp_path / "c"
    (clone / ".github" / "workflows").mkdir(parents=True)
    (clone / ".github" / "workflows" / "ci.yml").write_text("run: pip-audit\n")

    invoked: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
        invoked.append(list(argv))
        return _mk_completed(0, "", "")

    monkeypatch.setattr(ci_runner.subprocess, "run", fake_run)
    monkeypatch.setenv("KAIZEN_SKIP_PIP_AUDIT", "1")
    all_passed, results = run_ci_checks(clone, "true")
    assert results["pip_audit"]["status"] == "skip"
    assert results["pip_audit"]["reason"] == "opted out via KAIZEN_SKIP_PIP_AUDIT"
    # Crucially, pip-audit must NOT have been invoked.
    assert not any(call and call[0] == "pip-audit" for call in invoked)
    assert all_passed is True


def test_pip_audit_binary_missing_returns_skip(tmp_path, monkeypatch):
    """F2-parity (audit cleanup): pip-audit binary absent is a HOST tooling
    gap — return SKIP, not FAIL, so the cycle does not abandon."""
    clone = tmp_path / "c"
    (clone / ".github" / "workflows").mkdir(parents=True)
    (clone / ".github" / "workflows" / "ci.yml").write_text("run: pip-audit\n")
    (clone / "requirements.txt").write_text("requests==2.31.0\n")

    def fake_run(argv, *args, **kwargs):
        if argv and argv[0] == "pip-audit":
            raise FileNotFoundError(2, "No such file or directory: 'pip-audit'")
        return _mk_completed(0, "", "")

    monkeypatch.setattr(ci_runner.subprocess, "run", fake_run)
    monkeypatch.delenv("KAIZEN_SKIP_PIP_AUDIT", raising=False)
    all_passed, results = run_ci_checks(clone, "true")
    assert results["pip_audit"]["status"] == "skip"
    assert results["pip_audit"]["reason"] == "pip_audit_binary_missing"
    assert all_passed is True


def test_pip_audit_infra_failure_returns_skip(tmp_path, monkeypatch):
    """F3: pip-audit can fail for HOST reasons (no python3-venv, no network).
    Inspect output for known infra signatures and return SKIP — not FAIL —
    so the cycle does not abandon for a host issue."""
    clone = tmp_path / "c"
    (clone / ".github" / "workflows").mkdir(parents=True)
    (clone / ".github" / "workflows" / "ci.yml").write_text("run: pip-audit\n")
    (clone / "requirements.txt").write_text("requests==2.31.0\n")

    def fake_run(argv, *args, **kwargs):
        if argv and argv[0] == "pip-audit":
            return _mk_completed(
                1,
                "",
                "ERROR: ensurepip is not available in this environment.\n",
            )
        return _mk_completed(0, "", "")

    monkeypatch.setattr(ci_runner.subprocess, "run", fake_run)
    monkeypatch.delenv("KAIZEN_SKIP_PIP_AUDIT", raising=False)
    all_passed, results = run_ci_checks(clone, "true")
    assert results["pip_audit"]["status"] == "skip"
    assert results["pip_audit"]["reason"] == "pip_audit_infra_unavailable"
    assert all_passed is True


def test_pytest_binary_missing_returns_skip(tmp_path, monkeypatch):
    """F5: a missing test-runner binary is a HOST tooling gap — return SKIP
    so the cycle does not abandon for a missing binary."""
    clone = tmp_path / "c"
    clone.mkdir()
    (clone / "pyproject.toml").write_text('[project]\nname = "x"\n')

    def fake_run(argv, *args, **kwargs):
        if argv and argv[0] == "pytest":
            raise FileNotFoundError(2, "No such file or directory: 'pytest'")
        return _mk_completed(0, "", "")

    monkeypatch.setattr(ci_runner.subprocess, "run", fake_run)
    monkeypatch.setenv("KAIZEN_SKIP_PIP_AUDIT", "1")
    all_passed, results = run_ci_checks(clone, "pytest")
    assert results["tests"]["status"] == "skip"
    assert results["tests"]["reason"] == "test_runner_missing"
    assert "test runner" in results["tests"]["output"]
    assert all_passed is True


def test_kaizen_skip_checks_csv_env_parses_multiple(tmp_path, monkeypatch):
    """F11: KAIZEN_SKIP_CHECKS is a comma-separated list. Setting it to
    ``"ruff,bandit"`` short-circuits BOTH branches without invoking either
    binary."""
    clone = tmp_path / "c"
    clone.mkdir()
    (clone / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[tool.ruff]\nline-length = 100\n\n[tool.bandit]\nskips = []\n'
    )
    invoked: list[list[str]] = []

    def fake_run(argv, *args, **kwargs):
        invoked.append(list(argv))
        return _mk_completed(0, "", "")

    monkeypatch.setattr(ci_runner.subprocess, "run", fake_run)
    monkeypatch.setenv("KAIZEN_SKIP_CHECKS", "ruff,bandit")
    monkeypatch.setenv("KAIZEN_SKIP_PIP_AUDIT", "1")
    all_passed, results = run_ci_checks(clone, "true")
    assert results["ruff_check"]["status"] == "skip"
    assert results["ruff_format"]["status"] == "skip"
    assert results["bandit"]["status"] == "skip"
    assert results["ruff_check"]["reason"] == "opted out via KAIZEN_SKIP_CHECKS"
    assert results["bandit"]["reason"] == "opted out via KAIZEN_SKIP_CHECKS"
    # Neither binary should have been invoked.
    assert not any(call and call[0] == "ruff" for call in invoked)
    assert not any(call and call[0] == "bandit" for call in invoked)
    assert all_passed is True


def test_kaizen_skip_checks_legacy_pip_audit_alias(tmp_path, monkeypatch):
    """F11 back-compat: KAIZEN_SKIP_PIP_AUDIT=1 still resolves to pip-audit
    being skipped with the legacy reason text."""
    clone = tmp_path / "c"
    (clone / ".github" / "workflows").mkdir(parents=True)
    (clone / ".github" / "workflows" / "ci.yml").write_text("run: pip-audit\n")

    monkeypatch.setattr(ci_runner.subprocess, "run", lambda *a, **kw: _mk_completed(0, "", ""))
    monkeypatch.delenv("KAIZEN_SKIP_CHECKS", raising=False)
    monkeypatch.setenv("KAIZEN_SKIP_PIP_AUDIT", "1")
    _all_passed, results = run_ci_checks(clone, "true")
    assert results["pip_audit"]["status"] == "skip"
    assert results["pip_audit"]["reason"] == "opted out via KAIZEN_SKIP_PIP_AUDIT"


def test_pip_audit_workflow_match_ignores_comments(tmp_path):
    """F14: a comment that mentions ``pip-audit`` must NOT opt the target in.
    Only `run:` or `uses:` lines count."""
    clone = tmp_path / "c"
    (clone / ".github" / "workflows").mkdir(parents=True)
    (clone / ".github" / "workflows" / "ci.yml").write_text(
        "# we don't use pip-audit yet\njobs:\n  test:\n    steps:\n      - run: pytest\n"
    )
    assert _pip_audit_referenced_in_workflows(clone) is False


def test_pip_audit_workflow_match_accepts_uses_line(tmp_path):
    """F14: ``uses: pypa/gh-action-pip-audit@...`` is a valid opt-in signal."""
    clone = tmp_path / "c"
    (clone / ".github" / "workflows").mkdir(parents=True)
    (clone / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  audit:\n    steps:\n      - uses: pypa/gh-action-pip-audit@v1\n"
    )
    assert _pip_audit_referenced_in_workflows(clone) is True


# ── result-shape contract (uniform across all checks) ─────────────────────


def test_all_results_use_uniform_dict_shape(tmp_path, monkeypatch):
    """Every check's result is a dict with 'status' and 'output' keys."""
    clone = tmp_path / "c"
    clone.mkdir()
    (clone / "pyproject.toml").write_text('[project]\nname = "x"\n')
    monkeypatch.setattr(ci_runner.subprocess, "run", lambda *a, **kw: _mk_completed(0, "", ""))
    monkeypatch.setenv("KAIZEN_SKIP_PIP_AUDIT", "1")
    _all_passed, results = run_ci_checks(clone, "true")
    for name, r in results.items():
        assert isinstance(r, dict), f"{name} result is not a dict: {type(r)}"
        assert "status" in r, f"{name} missing status"
        assert r["status"] in ("pass", "fail", "skip"), f"{name} bad status {r['status']}"
        assert "output" in r, f"{name} missing output"


# Sentinel: keep linter happy about an otherwise-unused import.
assert isinstance(types.ModuleType, type)
