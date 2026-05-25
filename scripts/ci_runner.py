"""Run target-repo CI checks locally in a clone (tests + lint + security).

Separate from scripts/test_runner.py — that module's contract is "run the test
command and count passed tests"; this module's contract is "run every CI check
the target repo defines and return per-check results."

Supports:

- pytest (via test_command) — always run
- ruff check + ruff format --check — opt-in via [tool.ruff] in pyproject.toml or ruff.toml
- Bandit (SAST) — opt-in via [tool.bandit] in pyproject.toml or .bandit / bandit.yaml / bandit.yml
- pip-audit (SCA) — opt-in via the literal string "pip-audit" in any .github/workflows/*.yml

Targets that use flake8/mypy/black are NOT auto-detected — a warning is logged
when no known lint config is found, and the cycle's Phase 5b agent must verify
whether the target's actual CI has other checks not mirrored here.

Result shape
------------

`run_ci_checks` returns ``(all_passed, results)`` where ``results`` is a dict
mapping check-name → ``{"status", "output", "reason"}``:

- ``status``: one of ``"pass"``, ``"fail"``, ``"skip"``.
- ``output``: captured stdout+stderr (empty string if nothing to report).
- ``reason``: present when ``status == "skip"`` (or when an unusual fail mode
  needs to be named — e.g. Bandit exit code 2 meaning the scanner itself
  crashed, distinct from exit code 1 meaning real findings).

``all_passed`` is ``True`` only if every check's status is ``"pass"`` or
``"skip"``. A ``skip`` is never a failure.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

# F11 — Per-check skip env var. Comma-separated check names; matched against
# the per-check identifiers ("ruff", "bandit", "pip_audit", "tests"). The
# legacy KAIZEN_SKIP_PIP_AUDIT=1 alias is honored at parse time for backwards
# compatibility — it folds into the same set as if "pip_audit" had been listed.
_KAIZEN_SKIP_CHECKS_ENV = "KAIZEN_SKIP_CHECKS"
_KAIZEN_SKIP_PIP_AUDIT_ENV = "KAIZEN_SKIP_PIP_AUDIT"
_SKIP_OPT_OUT_REASON = "opted out via KAIZEN_SKIP_CHECKS"

# F3 — pip-audit infra-failure detection. pip-audit shells out to build a
# temp venv; on hosts that lack `python3-venv` (or whose pip cannot reach
# its index) the audit fails for reasons unrelated to the target's deps.
# Treat these as SKIP — they're host issues, not vulnerability findings.
_PIP_AUDIT_INFRA_SIGNATURES = (
    "ensurepip is not available",
    "Could not find a version",
    "Connection refused",
    "PermissionError",
    "No matching distribution",
    "Failed to establish a new connection",
)


def _parse_skip_checks_env() -> set[str]:
    """Parse KAIZEN_SKIP_CHECKS + the legacy KAIZEN_SKIP_PIP_AUDIT alias.

    Returns a set of check-name strings (e.g. ``{"ruff", "pip_audit"}``).
    Tolerant of empty/whitespace items so ``KAIZEN_SKIP_CHECKS=" ruff, "``
    parses cleanly. The legacy ``KAIZEN_SKIP_PIP_AUDIT in {"1","true"}``
    alias adds ``"pip_audit"`` to the set so existing call sites keep working.
    """
    raw = os.environ.get(_KAIZEN_SKIP_CHECKS_ENV, "")
    items = {part.strip() for part in raw.split(",")}
    items.discard("")
    legacy = os.environ.get(_KAIZEN_SKIP_PIP_AUDIT_ENV, "").strip().lower()
    if legacy in ("1", "true"):
        items.add("pip_audit")
    return items


# Result-shape constants — single source of truth so the literal strings cannot
# drift between this module, callers, and the Phase 5b SKILL.md routing rules.
# nosec annotations: Bandit B105 flags "pass" as a hardcoded-password literal;
# here it's a check-status label ({"status": "pass" | "fail" | "skip"}), not a
# credential. Documented at module level and consumed only by the result-dict
# builder.
PASS = "pass"  # nosec B105
FAIL = "fail"
SKIP = "skip"


CheckResult = dict[str, Any]


def _result(status: str, output: str = "", reason: str | None = None) -> CheckResult:
    """Build a uniform check-result dict.

    Always includes ``status`` and ``output``. ``reason`` is included only when
    non-None — keeps the dict lean for plain pass/fail cases and explicit for
    skip / unusual-fail cases.
    """
    r: CheckResult = {"status": status, "output": output}
    if reason is not None:
        r["reason"] = reason
    return r


def _has_ruff_config(clone_dir: Path) -> bool:
    """Return True if the target repo opts in to ruff."""
    if (clone_dir / "ruff.toml").exists():
        return True
    pyproject = clone_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return "[tool.ruff]" in content or "[tool.ruff." in content
    return False


def _bandit_config_path(clone_dir: Path) -> Path | None:
    """Return the explicit Bandit config file if the target opts in, else None.

    Detection order:
      1. ``pyproject.toml`` with ``[tool.bandit]`` section — returns the
         pyproject path itself (Bandit reads it via ``-c pyproject.toml``).
      2. ``.bandit`` — returns its path.
      3. ``bandit.yaml`` / ``bandit.yml`` — returns its path.

    Returning ``None`` means no Bandit opt-in was found and the check should
    be skipped.
    """
    pyproject = clone_dir / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
        if "[tool.bandit]" in content or "[tool.bandit." in content:
            return pyproject

    for name in (".bandit", "bandit.yaml", "bandit.yml"):
        candidate = clone_dir / name
        if candidate.exists():
            return candidate
    return None


# F14: match `pip-audit` only when it appears inside a `run:` or `uses:`
# value. The leading `^\s*(?:-\s*)?` makes the prefix tolerant of the
# typical YAML-list ``- run: …`` form while still rejecting comments
# (``# pip-audit``) and free-form prose anchored at column 0 without a
# matching key.
_PIP_AUDIT_WORKFLOW_RE = re.compile(
    r"^\s*(?:-\s*)?(?:run|uses)\s*:\s*[^#\n]*pip-audit",
    re.MULTILINE,
)


def _pip_audit_referenced_in_workflows(clone_dir: Path) -> bool:
    """Return True if any .github/workflows/*.yml mentions ``pip-audit``.

    F14 (audit cleanup): tightened from a naive substring match to a
    line-anchored regex that only matches ``pip-audit`` appearing inside a
    ``run:`` or ``uses:`` key — so a comment like ``# we don't use pip-audit``
    no longer opts the target in by accident. Matches both bare ``run: pip-audit``
    and ``uses: pypa/gh-action-pip-audit@...``. We still do not YAML-parse;
    the regex is intentionally lenient on whitespace.
    """
    workflows = clone_dir / ".github" / "workflows"
    if not workflows.is_dir():
        return False
    for ext in ("*.yml", "*.yaml"):
        for wf in workflows.glob(ext):
            try:
                text = wf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _PIP_AUDIT_WORKFLOW_RE.search(text):
                return True
    return False


def _run_bandit(clone_dir: Path, config: Path) -> CheckResult:
    """Invoke Bandit and translate its exit code into our result shape.

    Bandit exit-code contract (per upstream docs):
      0 → no findings → pass
      1 → findings reported → fail (real lint hits)
      2 → Bandit config-file invalid (YAML parse error / unknown directive) →
          fail with a ``reason`` so the cycle can tell "config broken" apart
          from "code has security findings." Note: rc=2 specifically signals
          a config-file problem, not a generic scanner crash.

    Other exit codes → fail with the exit code in the reason so triage isn't
    silent.
    """
    # If config is pyproject.toml, pass it via -c; otherwise Bandit auto-picks
    # up .bandit / bandit.yaml. We pass -c explicitly in both cases so the
    # command is reproducible.
    argv = ["bandit", "-r", ".", "-c", str(config)]
    try:
        proc = subprocess.run(
            argv,
            cwd=clone_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError:
        # F2 (audit cleanup): a missing bandit binary is a HOST tooling gap,
        # not a finding in the target's code — return SKIP so the cycle does
        # not abandon. Install bandit (`pip install bandit`) to mirror the
        # target's CI when running kaizen, or remove the target's bandit
        # opt-in if it isn't actually used. KAIZEN_SKIP_CHECKS=bandit short-
        # circuits this branch entirely (handled at the dispatch site).
        return _result(
            SKIP,
            output=(
                "bandit binary not found on PATH — Bandit SAST check skipped. "
                "Install bandit (`pip install bandit`) to enable the Bandit CI "
                "mirror, or set KAIZEN_SKIP_CHECKS=bandit to silence this."
            ),
            reason="bandit_binary_missing",
        )

    output = (proc.stdout or "") + (proc.stderr or "")
    rc = proc.returncode
    if rc == 0:
        return _result(PASS, output=output)
    if rc == 1:
        return _result(FAIL, output=output, reason="bandit_findings")
    if rc == 2:
        return _result(FAIL, output=output, reason="bandit_config_error")
    return _result(FAIL, output=output, reason=f"bandit_unexpected_exit_{rc}")


_PIP_AUDIT_REQUIREMENTS_FILES = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
)


def _pip_audit_requirements_files(clone_dir: Path) -> list[Path]:
    """Return the requirements files we'll feed to ``pip-audit -r``.

    Bare ``pip-audit`` scans the active Python interpreter's site-packages
    — that's the host env, not the target repo's pinned deps, so it
    inevitably finds host-OS CVEs and fails the gate for reasons unrelated
    to the cycle's changes. We mirror what target CI does by scanning the
    target's requirements files instead.
    """
    return [
        clone_dir / name for name in _PIP_AUDIT_REQUIREMENTS_FILES if (clone_dir / name).is_file()
    ]


def _run_pip_audit(clone_dir: Path) -> CheckResult:
    """Invoke pip-audit on the target's requirements files.

    pip-audit's exit-code contract is binary (0 = clean, !=0 = findings or
    error), so we don't try to distinguish findings vs. crash the way we do
    for Bandit. A missing binary is reported as fail with a named reason so
    triage isn't silent.

    Scope: every ``requirements*.txt`` discovered in the clone is passed
    via ``-r`` so we audit only the target's pinned deps. If no
    requirements file is present we return ``skip`` rather than fall back
    to scanning the host interpreter — the latter is the bug this function
    used to have.
    """
    req_files = _pip_audit_requirements_files(clone_dir)
    if not req_files:
        return _result(
            SKIP,
            output="pip-audit skipped: no requirements*.txt in target repo (host-env scan suppressed).",
            reason="no_target_requirements",
        )

    argv = ["pip-audit"]
    for f in req_files:
        argv.extend(["-r", str(f.relative_to(clone_dir))])

    try:
        proc = subprocess.run(
            argv,
            cwd=clone_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError:
        # F2 parity: a missing pip-audit binary is a HOST tooling gap, not
        # a finding — return SKIP so the cycle does not abandon. Install
        # pip-audit (`pip install pip-audit`) to mirror the target's CI, or
        # set KAIZEN_SKIP_CHECKS=pip_audit (or the legacy
        # KAIZEN_SKIP_PIP_AUDIT=1) to silence.
        return _result(
            SKIP,
            output=(
                "pip-audit binary not found on PATH — pip-audit SCA check "
                "skipped. Install pip-audit (`pip install pip-audit`) or set "
                "KAIZEN_SKIP_CHECKS=pip_audit to silence."
            ),
            reason="pip_audit_binary_missing",
        )

    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return _result(PASS, output=output)
    # F3: distinguish "infra-related failure" (no python3-venv, no network,
    # ...) from "real vulnerability findings." If the combined stdout/stderr
    # mentions any known infra signature, treat as SKIP (so the cycle is not
    # abandoned for a host issue). Only treat as FAIL if pip-audit actually
    # ran far enough to evaluate the requirements set.
    for signature in _PIP_AUDIT_INFRA_SIGNATURES:
        if signature in output:
            return _result(
                SKIP,
                output=output,
                reason="pip_audit_infra_unavailable",
            )
    return _result(FAIL, output=output, reason=f"pip_audit_exit_{proc.returncode}")


def run_ci_checks(
    clone_dir: Path,
    test_command: str,
) -> tuple[bool, dict[str, CheckResult]]:
    """Run the test suite plus every CI mirror check the target opts in to.

    Returns:
        all_passed: True iff every check's status is "pass" or "skip".
        results:    mapping of check_name -> CheckResult dict
                    (``{"status", "output", "reason"?}``).

                    Always present:
                        "tests"
                    Always present (one of the two — never both):
                        "ruff_check" and "ruff_format" (if ruff is opted in),
                        otherwise "lint_warning" (a skip with status="skip").
                    Always present:
                        "bandit"    — pass / fail / skip depending on opt-in
                        "pip_audit" — pass / fail / skip depending on opt-in
                                      and KAIZEN_SKIP_PIP_AUDIT env var.

    See internal/cycle/SKILL.md Phase 5b for the routing rules that consume
    the per-check dict.
    """
    results: dict[str, CheckResult] = {}

    # F11 — Per-check skip env. Parsed once at the top so each branch consults
    # the same set; legacy KAIZEN_SKIP_PIP_AUDIT=1 folds in here.
    skip_checks = _parse_skip_checks_env()

    # ── tests ──────────────────────────────────────────────────────────
    if "tests" in skip_checks:
        results["tests"] = _result(
            SKIP,
            output="tests skipped via KAIZEN_SKIP_CHECKS.",
            reason=_SKIP_OPT_OUT_REASON,
        )
    else:
        argv = shlex.split(test_command, posix=(sys.platform != "win32"))
        try:
            proc = subprocess.run(
                argv,
                cwd=clone_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            results["tests"] = _result(
                PASS if proc.returncode == 0 else FAIL,
                output=(proc.stdout or "") + (proc.stderr or ""),
            )
        except FileNotFoundError:
            # F5: the test-runner binary isn't on PATH. This is a host
            # tooling gap, not a real test failure — return SKIP so the
            # cycle does not abandon for a missing binary. Install the
            # runner (e.g. `pip install pytest`) to mirror the target's CI.
            head = argv[0] if argv else "(empty)"
            results["tests"] = _result(
                SKIP,
                output=(
                    f"test runner {head!r} not found on PATH — tests check "
                    "skipped. Install the runner (e.g. pytest) to enable "
                    "the test CI mirror."
                ),
                reason="test_runner_missing",
            )

    # ── ruff ───────────────────────────────────────────────────────────
    if "ruff" in skip_checks:
        # F11: emit BOTH ruff_check and ruff_format as skip so the per-check
        # key set is stable across opt-in / opt-out (baseline diffs key by
        # check name; drift in the key set would break the cycle-introduced
        # comparison in team_executor).
        for name in ("ruff_check", "ruff_format"):
            results[name] = _result(
                SKIP,
                output=f"{name} skipped via KAIZEN_SKIP_CHECKS.",
                reason=_SKIP_OPT_OUT_REASON,
            )
    elif _has_ruff_config(clone_dir):
        for name, argv_ruff in [
            ("ruff_check", ["ruff", "check", "."]),
            ("ruff_format", ["ruff", "format", "--check", "."]),
        ]:
            try:
                r = subprocess.run(
                    argv_ruff,
                    cwd=clone_dir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                )
                results[name] = _result(
                    PASS if r.returncode == 0 else FAIL,
                    output=(r.stdout or "") + (r.stderr or ""),
                )
            except FileNotFoundError:
                # F1 (audit cleanup): a missing ruff binary is a HOST tooling
                # gap, not a finding — return SKIP so the cycle does not
                # abandon. Install ruff (`pip install ruff`) to mirror the
                # target's CI, or set KAIZEN_SKIP_CHECKS=ruff to silence.
                results[name] = _result(
                    SKIP,
                    output=(
                        f"ruff binary not found on PATH — {name!r} skipped. "
                        "Install ruff (`pip install ruff`) or set "
                        "KAIZEN_SKIP_CHECKS=ruff to silence."
                    ),
                    reason="ruff_binary_missing",
                )
    else:
        results["lint_warning"] = _result(
            SKIP,
            output=(
                "No ruff config detected in the target repo "
                "(ruff.toml absent and pyproject.toml has no [tool.ruff] "
                "section). Lint checks were skipped. If the target's actual "
                "CI runs flake8, mypy, black, or another linter, this cycle "
                "may report green prematurely — Phase 5b agents must verify "
                "against the target's .github/workflows/ before relying on "
                "local CI mirror."
            ),
            reason="no_ruff_config",
        )

    # ── bandit ─────────────────────────────────────────────────────────
    if "bandit" in skip_checks:
        results["bandit"] = _result(
            SKIP,
            output="bandit skipped via KAIZEN_SKIP_CHECKS.",
            reason=_SKIP_OPT_OUT_REASON,
        )
    else:
        bandit_cfg = _bandit_config_path(clone_dir)
        if bandit_cfg is None:
            results["bandit"] = _result(
                SKIP,
                output=(
                    "No Bandit config detected in the target repo "
                    "(no [tool.bandit] in pyproject.toml, no .bandit, no "
                    "bandit.yaml / bandit.yml). Bandit SAST check skipped."
                ),
                reason="no_bandit_config",
            )
        else:
            results["bandit"] = _run_bandit(clone_dir, bandit_cfg)

    # ── pip-audit ──────────────────────────────────────────────────────
    if "pip_audit" in skip_checks:
        # F11: present the legacy reason text when the alias was used so
        # back-compat callers reading the reason string keep working.
        if os.environ.get(_KAIZEN_SKIP_PIP_AUDIT_ENV, "").strip().lower() in ("1", "true"):
            results["pip_audit"] = _result(
                SKIP,
                output="pip-audit skipped via KAIZEN_SKIP_PIP_AUDIT=1.",
                reason="opted out via KAIZEN_SKIP_PIP_AUDIT",
            )
        else:
            results["pip_audit"] = _result(
                SKIP,
                output="pip-audit skipped via KAIZEN_SKIP_CHECKS.",
                reason=_SKIP_OPT_OUT_REASON,
            )
    elif not _pip_audit_referenced_in_workflows(clone_dir):
        results["pip_audit"] = _result(
            SKIP,
            output=(
                "pip-audit not referenced in any .github/workflows/*.yml "
                "in the target repo — pip-audit SCA check skipped. Set "
                "KAIZEN_SKIP_PIP_AUDIT=1 to skip even when the target's CI "
                "uses it (e.g. for offline runs)."
            ),
            reason="no_pip_audit_in_workflows",
        )
    else:
        results["pip_audit"] = _run_pip_audit(clone_dir)

    # Skip never counts as failure; only an explicit FAIL fails the run.
    all_passed = all(r["status"] != FAIL for r in results.values())
    return all_passed, results
