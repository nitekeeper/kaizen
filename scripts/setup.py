"""Kaizen setup — verify external dependencies and apply DB migrations."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from scripts.migrate import apply_migrations

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
        return DepCheck(
            name, False, f"`git --version` exited {result.returncode}", fix
        )
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
        (line.strip() for line in output if "account" in line.lower() or "logged in" in line.lower()),
        "authenticated",
    )
    return DepCheck(name, True, summary, auth_fix)


def check_memex() -> DepCheck:
    name = "memex"
    fix = "Install memex (e.g. `pip install memex` or install via agora)"
    if shutil.which("memex") is None:
        return DepCheck(name, False, "not found on PATH", fix)
    result = _run(["memex", "--version"])
    if result.returncode != 0:
        return DepCheck(
            name, False, f"`memex --version` exited {result.returncode}", fix
        )
    return DepCheck(name, True, result.stdout.strip() or "memex available", fix)


def _find_atelier_root() -> Path | None:
    """Locate atelier on disk. Returns None if not found.

    Prefers scripts.seed_atelier_in_clone.find_atelier_root if importable;
    otherwise inlines the same search heuristic.
    """
    try:
        from scripts.seed_atelier_in_clone import find_atelier_root as _find
        try:
            return _find()
        except RuntimeError:
            return None
    except Exception:
        # Inline fallback
        markers = ("scripts/migrate.py", "scripts/seed_roles.py")

        def looks_like(candidate: Path) -> bool:
            return all((candidate / m).exists() for m in markers)

        home_skill = Path.home() / "Documents" / "Skills" / "atelier"
        if looks_like(home_skill):
            return home_skill
        cwd = Path.cwd().resolve()
        for ancestor in [cwd] + list(cwd.parents):
            sibling = ancestor / "atelier"
            if looks_like(sibling):
                return sibling
            if looks_like(ancestor):
                return ancestor
        return None


def check_atelier_on_disk() -> DepCheck:
    name = "atelier"
    fix = (
        "Clone atelier to a discoverable location "
        "(e.g. ~/Documents/Skills/atelier) or install via agora."
    )
    root = _find_atelier_root()
    if root is None:
        return DepCheck(name, False, "not found on disk", fix)
    return DepCheck(name, True, f"{root} (markers found)", fix)


def check_python_version() -> DepCheck:
    name = "python"
    fix = "Upgrade Python to 3.11 or newer"
    v = sys.version_info
    detail = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 11):
        return DepCheck(name, True, detail, fix)
    return DepCheck(name, False, f"{detail} (need >= 3.11)", fix)


def verify_all() -> list[DepCheck]:
    """Run every check and return the results. Does not raise."""
    return [
        check_git(),
        check_gh(),
        check_memex(),
        check_atelier_on_disk(),
        check_python_version(),
    ]


def _safe(text: str) -> str:
    """Strip characters the active stdout encoding cannot represent."""
    enc = (sys.stdout.encoding or "utf-8")
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


def run_setup() -> int:
    checks = verify_all()
    print_results(checks)
    failed = [c for c in checks if not c.ok]
    if failed:
        print(
            f"\nSetup blocked: {len(failed)} dependencies missing. "
            "Fix the above and re-run."
        )
        return 1
    apply_migrations(str(DB_PATH), MIGRATIONS_DIR)
    try:
        rel = DB_PATH.relative_to(REPO_ROOT)
    except ValueError:
        rel = DB_PATH
    print(f"Database migrations applied to {rel}")
    return 0


if __name__ == "__main__":
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    sys.exit(run_setup())
