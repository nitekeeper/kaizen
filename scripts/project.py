"""Project CRUD module + CLI for Kaizen's project registry.

Each project row stores the auto-detected (and user-confirmed) config used
when running a multi-cycle improvement against the target repository.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from scripts.db import get_connection

DEFAULT_DB_PATH = ".ai/memex.db"

# Fields the user can mutate via update_project.
_UPDATABLE = {
    "git_url",
    "name",
    "base_branch",
    "test_command",
    "read_paths",
    "expert_roster",
    "language",
    "last_run_at",
    "notes",
}

# Fields that are stored as JSON-encoded TEXT but exposed as Python lists.
_JSON_LIST_FIELDS = ("read_paths", "expert_roster")

# Defense-in-depth: valid SQL identifier guard in case _UPDATABLE ever drifts.
_COLUMN_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_dict(row, cols) -> dict:
    out = dict(zip(cols, row, strict=False))
    for field in _JSON_LIST_FIELDS:
        raw = out.get(field)
        if isinstance(raw, str):
            try:
                out[field] = json.loads(raw)
            except json.JSONDecodeError:
                out[field] = []
    return out


# ── CRUD ───────────────────────────────────────────────────────────────────


def create_project(
    db_path: str,
    git_url: str,
    name: str,
    base_branch: str,
    test_command: str,
    read_paths: list[str],
    expert_roster: list[str],
    language: str | None,
    notes: str | None = None,
) -> dict:
    """Insert a new project row and return it (with lists deserialised)."""
    now = _now()
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO projects "
            "(git_url, name, base_branch, test_command, read_paths, expert_roster, "
            " language, registered_at, last_run_at, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                git_url,
                name,
                base_branch,
                test_command,
                json.dumps(read_paths),
                json.dumps(expert_roster),
                language,
                now,
                None,
                notes,
            ),
        )
        conn.commit()
        new_id = cur.lastrowid
    finally:
        conn.close()
    return get_project(db_path, new_id)


def get_project(db_path: str, project_id: int) -> dict | None:
    conn = get_connection(db_path)
    try:
        cur = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [c[0] for c in cur.description]
        return _row_to_dict(row, cols)
    finally:
        conn.close()


def get_project_by_url(db_path: str, git_url: str) -> dict | None:
    conn = get_connection(db_path)
    try:
        cur = conn.execute("SELECT * FROM projects WHERE git_url = ?", (git_url,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = [c[0] for c in cur.description]
        return _row_to_dict(row, cols)
    finally:
        conn.close()


def list_projects(db_path: str) -> list[dict]:
    conn = get_connection(db_path)
    try:
        cur = conn.execute("SELECT * FROM projects ORDER BY id")
        rows = cur.fetchall()
        cols = [c[0] for c in cur.description]
        return [_row_to_dict(r, cols) for r in rows]
    finally:
        conn.close()


def update_project(db_path: str, project_id: int, **fields) -> dict:
    """Update any subset of updatable fields. List fields are re-encoded to JSON."""
    updates = {k: v for k, v in fields.items() if k in _UPDATABLE}
    if not updates:
        return get_project(db_path, project_id)
    for field in _JSON_LIST_FIELDS:
        if field in updates and not isinstance(updates[field], str):
            updates[field] = json.dumps(updates[field])
    # Defense-in-depth: catch future _UPDATABLE drift before it reaches the f-string.
    for k in updates:
        if not _COLUMN_NAME_RE.fullmatch(k):
            raise ValueError(f"Invalid column name {k!r}: only [a-zA-Z_][a-zA-Z0-9_]* allowed")
    # nosec B608 — column names pass _UPDATABLE allowlist (above) AND the
    # _COLUMN_NAME_RE regex (two-layer defense); values flow through `?` binding.
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn = get_connection(db_path)
    try:
        conn.execute(
            f"UPDATE projects SET {set_clause} WHERE id = ?",  # nosec B608
            (*updates.values(), project_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_project(db_path, project_id)


def delete_project(db_path: str, project_id: int) -> bool:
    conn = get_connection(db_path)
    try:
        cur = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── Registration flow helpers ───────────────────────────────────────────────


def _name_from_url(git_url: str) -> str:
    """Derive a project name from a git URL: https://.../owner/repo.git -> repo."""
    stem = git_url.rstrip("/").split("/")[-1]
    if stem.endswith(".git"):
        stem = stem[:-4]
    return stem or "unnamed"


def _prompt(label: str, default):
    """Prompt with default shown. Empty input returns the default unchanged."""
    rendered = (
        json.dumps(default)
        if isinstance(default, (list, dict))
        else ("" if default is None else str(default))
    )
    try:
        raw = input(f"{label} [{rendered}]: ").strip()
    except EOFError:
        return default
    if raw == "":
        return default
    if isinstance(default, list):
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, list):
                raise ValueError
            return parsed
        except (json.JSONDecodeError, ValueError):
            print("  (expected JSON array; keeping default)")
            return default
    return raw


def _edit_detected(detected: dict) -> dict:
    """Walk each field and let the user override or accept the default."""
    edited = dict(detected)
    edited["test_command"] = _prompt("test_command", detected.get("test_command"))
    edited["read_paths"] = _prompt("read_paths", detected.get("read_paths") or [])
    edited["expert_roster"] = _prompt("expert_roster", detected.get("expert_roster") or [])
    edited["language"] = _prompt("language", detected.get("language"))
    return edited


def _detect_base_branch(git_url: str) -> str:
    """Detect the default branch of a git remote via `ls-remote --symref`.

    Falls back to 'main' if detection fails (network error, malformed output, etc.).
    Used at project registration time to seed `projects.base_branch` from the
    actual repo, rather than assuming 'main'.
    """
    try:
        result = subprocess.run(  # nosec B603 B607
            ["git", "ls-remote", "--symref", git_url, "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return "main"
    if result.returncode != 0:
        return "main"
    for line in result.stdout.splitlines():
        if line.startswith("ref: refs/heads/"):
            ref = line[len("ref: refs/heads/") :]
            return ref.split()[0] if ref.split() else "main"
    return "main"


def _register_cli(git_url: str, db_path: str) -> int:
    """Run the interactive register flow. Returns process exit code."""
    from scripts.clone import cleanup_experiment, clone_repo  # local to avoid import-time cost
    from scripts.detect_config import detect_all

    existing = get_project_by_url(db_path, git_url)
    if existing is not None:
        print(json.dumps(existing, indent=2))
        return 0

    base_branch = _detect_base_branch(git_url)
    with tempfile.TemporaryDirectory(prefix="kaizen-register-") as tmp:
        dest = Path(tmp) / "clone"
        print(f"Cloning {git_url} (branch={base_branch}) ... ", end="", flush=True)
        try:
            clone_repo(git_url, dest, base_branch)
        except Exception as exc:
            print("failed.")
            print(f"clone error: {exc}", file=sys.stderr)
            return 2
        print("done.")

        detected = detect_all(dest)

        print("Detected:")
        for key in ("language", "test_command", "read_paths", "expert_roster"):
            val = detected.get(key)
            rendered = json.dumps(val) if isinstance(val, list) else str(val)
            print(f"  {key:14s} : {rendered}")

        if detected.get("language") == "unknown":
            print(
                "Notice: language could not be auto-detected. "
                "Please supply test_command and read_paths."
            )
            test_cmd = _prompt("test_command (required)", None)
            read_paths = _prompt("read_paths (JSON array, required)", [])
            if not test_cmd or not read_paths:
                print("Aborted: test_command and read_paths required for unknown language.")
                return 1
            detected["test_command"] = test_cmd
            detected["read_paths"] = read_paths

        try:
            choice = input("Confirm (y), edit (e), or abort (n)? ").strip().lower()
        except EOFError:
            choice = "n"

        if choice in ("e", "edit"):
            detected = _edit_detected(detected)
        elif choice in ("n", "no", "abort"):
            print("Aborted; no project saved.")
            return 1
        elif choice not in ("y", "yes", ""):
            print("Aborted; unrecognised choice.")
            return 1

        if not detected.get("test_command"):
            print("Aborted: test_command is required.")
            return 1

        # Tempdir is removed automatically on context exit; cleanup_experiment is
        # also safe to call but unnecessary here.
        _ = cleanup_experiment  # silence unused-import if reordered later

        project = create_project(
            db_path=db_path,
            git_url=git_url,
            name=_name_from_url(git_url),
            base_branch=base_branch,
            test_command=detected["test_command"],
            read_paths=detected.get("read_paths") or [],
            expert_roster=detected.get("expert_roster") or [],
            language=detected.get("language"),
            notes=None,
        )

    print(json.dumps(project, indent=2))
    return 0


# ── CLI ─────────────────────────────────────────────────────────────────────


def _parse_edit_args(args: list[str]) -> dict:
    """Parse --field=value pairs. JSON-parses values for list fields."""
    out: dict = {}
    for arg in args:
        m = re.match(r"^--([a-zA-Z_]+)=(.*)$", arg, flags=re.DOTALL)
        if not m:
            raise SystemExit(f"bad arg: {arg!r} (expected --field=value)")
        key, raw = m.group(1), m.group(2)
        if key in _JSON_LIST_FIELDS:
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, list):
                    raise ValueError
            except (json.JSONDecodeError, ValueError) as err:
                raise SystemExit(f"--{key} expects a JSON array, got: {raw!r}") from err
            out[key] = parsed
        else:
            out[key] = raw
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print(
            "Usage: project.py {register|get|get-by-url|list|edit|delete} ...",
            file=sys.stderr,
        )
        return 1

    db_path = DEFAULT_DB_PATH
    cmd, rest = argv[0], argv[1:]

    if cmd == "register":
        if len(rest) != 1:
            print("Usage: project.py register <git-url>", file=sys.stderr)
            return 1
        return _register_cli(rest[0], db_path)

    if cmd == "get":
        if len(rest) != 1:
            print("Usage: project.py get <id>", file=sys.stderr)
            return 1
        row = get_project(db_path, int(rest[0]))
        if row is None:
            print("Not found", file=sys.stderr)
            return 1
        print(json.dumps(row, indent=2))
        return 0

    if cmd == "get-by-url":
        if len(rest) != 1:
            print("Usage: project.py get-by-url <git-url>", file=sys.stderr)
            return 1
        row = get_project_by_url(db_path, rest[0])
        if row is None:
            print("Not found", file=sys.stderr)
            return 1
        print(json.dumps(row, indent=2))
        return 0

    if cmd == "list":
        print(json.dumps(list_projects(db_path), indent=2))
        return 0

    if cmd == "edit":
        if not rest:
            print("Usage: project.py edit <id> --field=value [--field=value]...", file=sys.stderr)
            return 1
        project_id = int(rest[0])
        fields = _parse_edit_args(rest[1:])
        if not fields:
            print("No fields to update.", file=sys.stderr)
            return 1
        print(json.dumps(update_project(db_path, project_id, **fields), indent=2))
        return 0

    if cmd == "delete":
        if len(rest) != 1:
            print("Usage: project.py delete <id>", file=sys.stderr)
            return 1
        ok = delete_project(db_path, int(rest[0]))
        print("Deleted" if ok else "Not found")
        return 0 if ok else 1

    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
