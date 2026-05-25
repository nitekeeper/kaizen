"""Kaizen setup — verify external dependencies and apply DB migrations."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from scripts._tmux_config import (
    CONFIG_BLOCK,
    MARKER_VERSION,
    apply_config_block,
    detect_existing_marker,
    show_diff,
)
from scripts.migrate import apply_migrations
from scripts.seed_atelier_in_clone import find_atelier_root

REPO_ROOT = Path(__file__).parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"
DB_PATH = REPO_ROOT / ".ai" / "memex.db"


class DepCheck:
    """Result of a single dependency verification step."""

    def __init__(self, name: str, ok: bool, detail: str, fix: str):
        self.name = name
        self.ok = ok
        self.detail = detail
        self.fix = fix


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def check_git() -> DepCheck:
    name = "git"
    fix = "Install git: https://git-scm.com/downloads"
    if shutil.which("git") is None:
        return DepCheck(name, False, "not found on PATH", fix)
    result = _run(["git", "--version"])
    if result.returncode != 0:
        return DepCheck(name, False, f"`git --version` exited {result.returncode}", fix)
    return DepCheck(name, True, result.stdout.strip(), fix)


def check_gh() -> DepCheck:
    name = "gh"
    install_fix = "Install GitHub CLI: https://cli.github.com/"
    auth_fix = "Authenticate GitHub CLI: run `gh auth login`"
    if shutil.which("gh") is None:
        return DepCheck(name, False, "not found on PATH", install_fix)
    result = _run(["gh", "auth", "status"])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        first = detail[0] if detail else "not authenticated"
        return DepCheck(name, False, f"not authenticated ({first})", auth_fix)
    # gh prints auth status to stderr typically; surface a short summary
    output = (result.stdout or result.stderr or "").strip().splitlines()
    summary = next(
        (
            line.strip()
            for line in output
            if "account" in line.lower() or "logged in" in line.lower()
        ),
        "authenticated",
    )
    return DepCheck(name, True, summary, auth_fix)


def check_python_version() -> DepCheck:
    name = "python"
    fix = "Upgrade Python to 3.11 or newer"
    v = sys.version_info
    detail = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 11):
        return DepCheck(name, True, detail, fix)
    return DepCheck(name, False, f"{detail} (need >= 3.11)", fix)


def check_atelier() -> DepCheck:
    name = "atelier"
    fix = "Install Atelier via Agora: run `agora install atelier` in Claude Code"
    try:
        root = find_atelier_root()
    except Exception as exc:
        return DepCheck(name, False, str(exc), fix)
    return DepCheck(name, True, str(root), fix)


def verify_all() -> list[DepCheck]:
    """Run every check and return the results. Does not raise."""
    return [
        check_git(),
        check_gh(),
        check_python_version(),
        check_atelier(),
    ]


def _safe(text: str) -> str:
    """Strip characters the active stdout encoding cannot represent."""
    enc = sys.stdout.encoding or "utf-8"
    try:
        text.encode(enc)
        return text
    except UnicodeEncodeError:
        return text.encode(enc, errors="replace").decode(enc, errors="replace")


def print_results(checks: list[DepCheck]) -> None:
    for c in checks:
        status = "[OK]  " if c.ok else "[FAIL]"
        print(_safe(f"{status} {c.name}: {c.detail}"))
        if not c.ok:
            print(_safe(f"       fix: {c.fix}"))


def _locate_tmux_conf() -> Path:
    """Return the path setup should target for tmux.conf.

    Prefers ``~/.tmux.conf`` (the canonical location). Falls back to
    ``~/.config/tmux/tmux.conf`` if THAT file exists and the canonical
    one does not — this matches the XDG-style layout some users adopt.
    Returns the canonical path when neither exists (so the "create new"
    branch in the consent flow proposes ``~/.tmux.conf``, which is what
    tmux looks for first).
    """
    home = Path(os.path.expanduser("~"))
    canonical = home / ".tmux.conf"
    xdg = home / ".config" / "tmux" / "tmux.conf"
    if canonical.exists():
        return canonical
    if xdg.exists():
        return xdg
    return canonical


def _prompt_yes(question: str) -> bool:
    """Prompt the user for Y/n; default is yes. Standardised wrapper."""
    try:
        ans = input(question).strip().lower()
    except EOFError:
        # Non-interactive stdin → default to "no" so unattended runs never
        # write to a user's tmux.conf without consent.
        return False
    return ans in ("", "y", "yes")


def _check_tmux_config() -> None:
    """Interactive consent flow for installing the agent-teams tmux block.

    Idempotent: a second invocation when the file already carries the
    current marker version is a single info-line no-op. See
    scripts/_tmux_config.py for the block content and helpers.
    """
    tmux_conf = _locate_tmux_conf()
    # Branch 1: file missing → offer to create.
    if not tmux_conf.exists():
        print(f"\nagent-teams tmux config: tmux.conf not found at {tmux_conf}.")
        print("Proposed block to install:\n")
        print(CONFIG_BLOCK)
        if _prompt_yes(f"Create {tmux_conf} with the agent-teams block? (Y/n) "):
            apply_config_block(tmux_conf, MARKER_VERSION)
            print(f"Created {tmux_conf} with v{MARKER_VERSION} block.")
        else:
            print("Skipped; you can re-run setup later.")
        return

    # The remaining branches depend on whether a marker is already present.
    try:
        existing_version = detect_existing_marker(tmux_conf)
    except ValueError as exc:
        # Malformed marker — surface but don't crash setup.
        print(f"\nagent-teams tmux config: WARNING — {exc}")
        print("Skipping tmux setup; please remove the malformed marker by hand.")
        return

    # Branch 2: file exists, no marker → offer to append.
    if existing_version is None:
        print(f"\nagent-teams tmux config: not yet installed in {tmux_conf}.")
        print("The following block will be appended:\n")
        print(CONFIG_BLOCK)
        if _prompt_yes("Apply now? (Y/n) "):
            apply_config_block(tmux_conf, MARKER_VERSION)
            print(f"Appended v{MARKER_VERSION} block to {tmux_conf}.")
        else:
            print("Skipped.")
        return

    # Branch 3: file exists, marker at current version → silent info line.
    if existing_version == MARKER_VERSION:
        print(f"agent-teams tmux config: up-to-date (v{existing_version})")
        return

    # Branch 4: file exists, marker older than current → offer to update.
    while True:
        print(
            f"\nagent-teams tmux config v{existing_version} → v{MARKER_VERSION} "
            f"available in {tmux_conf}."
        )
        try:
            ans = input("Update? (Y/n) [d to show diff] ").strip().lower()
        except EOFError:
            print(f"Kept v{existing_version}; re-run setup to update later.")
            return
        if ans == "d":
            print()
            print(show_diff(tmux_conf, MARKER_VERSION))
            continue
        if ans in ("", "y", "yes"):
            apply_config_block(tmux_conf, MARKER_VERSION)
            print(f"Updated {tmux_conf} from v{existing_version} to v{MARKER_VERSION}.")
            return
        # Anything else = decline.
        print(f"Kept v{existing_version}; re-run setup to update later.")
        return


def run_setup() -> int:
    checks = verify_all()
    print_results(checks)
    failed = [c for c in checks if not c.ok]
    if failed:
        print(f"\nSetup blocked: {len(failed)} dependencies missing. Fix the above and re-run.")
        return 1
    apply_migrations(str(DB_PATH), MIGRATIONS_DIR)
    try:
        rel = DB_PATH.relative_to(REPO_ROOT)
    except ValueError:
        rel = DB_PATH
    print(f"Database migrations applied to {rel}")
    # T2 (audit cleanup): offer to install the shared agent-teams tmux
    # config block. Idempotent + consent-gated; safe to call on every run.
    try:
        _check_tmux_config()
    except Exception as exc:
        # Don't fail setup on a tmux-config issue — the user's DB is fine.
        print(f"agent-teams tmux config check failed: {exc}")
    return 0


if __name__ == "__main__":
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    sys.exit(run_setup())
