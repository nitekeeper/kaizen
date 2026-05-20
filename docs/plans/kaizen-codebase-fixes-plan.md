# Kaizen Codebase Fixes — Implementation Plan

**Status:** plan:open  
**Design doc:** `/home/nitekeeper/apps/kaizen/docs/design/kaizen-codebase-fixes-design.md`  
**Atelier project:** Kaizen (`/home/nitekeeper/apps/kaizen`)  
**Total tasks:** 22  
**Total issues covered:** 32 (H8 dropped per design)

---

## Dependency order

Wave 1 tasks have no inter-task dependencies. Within each wave, tasks are ordered so no task depends on an unbuilt component. Cross-wave: Wave 2 tasks may depend on Wave 1 fixes; Wave 3 on Wave 2; Wave 4 on Wave 3 where noted.

---

## Task 1 — Add pytest.ini (M9)

**Issues:** M9  
**Files to modify:** `/home/nitekeeper/apps/kaizen/pytest.ini` (create new)

**Failing test first**  
File: `tests/test_migrate.py`  
Test name: `test_all_tables_created`  
Without `pytest.ini`, running `pytest` from the repo root with no arguments discovers no tests (wrong directory). The test collection output shows `0 items / 0 errors`. With `pytest.ini`, the same bare `pytest` invocation collects and runs all tests in `tests/`.  
Assertion: `assert passed_count > 0` (verified by running `python3 -m pytest --co -q` and checking non-zero item count).

**Implementation step**  
Create `/home/nitekeeper/apps/kaizen/pytest.ini` with the following exact content:

```ini
[pytest]
testpaths = tests
addopts = -q --tb=short
```

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest --co -q
```
Verify output lists test items from the `tests/` directory. Then run the full suite: `python3 -m pytest`.

**Commit message**  
`config: add pytest.ini with testpaths=tests and -q --tb=short`

---

## Task 2 — Pin pytest in requirements.txt (L5)

**Issues:** L5  
**Files to modify:** `/home/nitekeeper/apps/kaizen/requirements.txt`

**Failing test first**  
File: `tests/test_detect_config.py`  
Test name: any test in that file  
Asserting that pytest ≥9.0.3 is installed: `import pytest; assert tuple(int(x) for x in pytest.__version__.split('.')[:2]) >= (9, 0)`.  
This fails if the installed pytest is 8.x or a vulnerable sub-9 version.

**Implementation step**  
Replace the single line `pytest>=8.0` in `/home/nitekeeper/apps/kaizen/requirements.txt` with:
```
pytest>=9.0.3,<10
```

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && pip install -r requirements.txt && python3 -m pytest
```

**Commit message**  
`deps: pin pytest>=9.0.3,<10 to close CVE and stabilise test runner version`

---

## Task 3 — Fix hardcoded `"python"` in test runner tests (C2)

**Issues:** C2  
**Files to modify:** `/home/nitekeeper/apps/kaizen/tests/test_test_runner.py`

**Failing test first**  
File: `tests/test_test_runner.py`  
Test name: `TestRunTestsInClone::test_passing_tests_returns_true_and_count`  
Current failure: `_DEFAULT_PYTEST = "python3 -m pytest -v --tb=short"` — on Linux where the `python` symlink is absent, the subprocess exits with `FileNotFoundError` or `returncode != 0`, so `passed is False` and `count == 0`, failing `assert passed is True`.

**Implementation step**  
In `/home/nitekeeper/apps/kaizen/tests/test_test_runner.py`:

1. Add `import sys` at line 3 (after the existing blank line after the docstring).
2. Replace the module-level constant on line 14:
   - Old: `_DEFAULT_PYTEST = "python3 -m pytest -v --tb=short"`
   - New: `_DEFAULT_PYTEST = f"{sys.executable} -m pytest -v --tb=short"`
3. In `test_custom_test_command_string_parses_correctly`, replace the local `cmd` assignment on line 43:
   - Old: `cmd = "python3 -m pytest -v --tb=short"`
   - New: `cmd = f"{sys.executable} -m pytest -v --tb=short"`

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest tests/test_test_runner.py -v
```
All 3 tests in `TestRunTestsInClone` must pass.

**Commit message**  
`fix(tests): replace hardcoded "python" with sys.executable in test_test_runner.py`

---

## Task 4 — Fix pass-count regex and shlex POSIX mode (M3, L6)

**Issues:** M3, L6  
**Files to modify:** `/home/nitekeeper/apps/kaizen/scripts/test_runner.py`

**Failing test first**  
File: `tests/test_test_runner.py`  
Test name: `TestRunTestsInClone::test_passing_tests_returns_true_and_count`  
After Task 3 fixes the interpreter, this test still fails because the regex `r"(\d+) passed"` matches intermediate lines (e.g., `"1 passed, 2 warnings in 0.12s"`) correctly, but also matches non-summary lines on some pytest versions where the word "passed" appears in test output. The real failure case: with multiple matching lines, the code takes the *last* match instead of the canonical `=== N passed ===` summary, yielding the wrong count.  
Assertion: `assert count == 1` (exactly one test passes in the fixture repo).

**Implementation step**  
In `/home/nitekeeper/apps/kaizen/scripts/test_runner.py`:

1. Change line 22 from:
   ```python
   argv = shlex.split(test_command)
   ```
   to:
   ```python
   argv = shlex.split(test_command, posix=(sys.platform != "win32"))
   ```

2. Replace lines 33–36 (the count-extraction loop):
   ```python
   count = 0
   for line in (result.stdout or "").splitlines():
       m = re.search(r"(\d+) passed", line)
       if m:
           count = int(m.group(1))
   ```
   with:
   ```python
   count = 0
   for line in (result.stdout or "").splitlines():
       m = re.search(r"={3,}\s+(\d+) passed", line)
       if m:
           count = int(m.group(1))
           break
   ```

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest tests/test_test_runner.py -v
```

**Commit message**  
`fix(test_runner): anchor pass-count regex to pytest summary line; fix shlex POSIX mode`

---

## Task 5 — Fix connection leak in migrate.py (C4)

**Issues:** C4  
**Files to modify:** `/home/nitekeeper/apps/kaizen/scripts/migrate.py`

**Failing test first**  
File: `tests/test_migrate.py`  
Test name: `test_migration_is_idempotent`  
On Windows and some Linux configurations, an unclosed connection on a SQLite file prevents a second `apply_migrations` call from acquiring a write lock, raising `sqlite3.OperationalError: database is locked`. The test calls `apply_migrations` twice; without `conn.close()` in a `finally` block the second call may fail.  
Assertion: `apply_migrations(db_path, MIGRATIONS_DIR)` called twice raises no exception and `count == 1`.

**Implementation step**  
In `/home/nitekeeper/apps/kaizen/scripts/migrate.py`, rewrite `apply_migrations` to wrap all connection usage in `try/finally`:

Replace lines 9–33:
```python
def apply_migrations(db_path: str, migrations_dir: Path) -> None:
    conn = get_connection(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS migrations (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            filename   TEXT NOT NULL UNIQUE,
            applied_at TEXT NOT NULL
        )
    """)
    conn.commit()

    applied = {row[0] for row in conn.execute("SELECT filename FROM migrations")}

    for migration_file in sorted(migrations_dir.glob("*.sql")):
        if migration_file.name in applied:
            continue
        sql = migration_file.read_text()
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO migrations (filename, applied_at) VALUES (?, ?)",
            (migration_file.name, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()

    conn.close()
```

with:
```python
def apply_migrations(db_path: str, migrations_dir: Path) -> None:
    conn = get_connection(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS migrations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                filename   TEXT NOT NULL UNIQUE,
                applied_at TEXT NOT NULL
            )
        """)
        conn.commit()

        applied = {row[0] for row in conn.execute("SELECT filename FROM migrations")}

        for migration_file in sorted(migrations_dir.glob("*.sql")):
            if migration_file.name in applied:
                continue
            sql = migration_file.read_text()
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO migrations (filename, applied_at) VALUES (?, ?)",
                (migration_file.name, datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
    finally:
        conn.close()
```

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest tests/test_migrate.py -v
```

**Commit message**  
`fix(migrate): wrap connection in try/finally to guarantee conn.close() on error`

---

## Task 6 — Add FK indexes migration (L1)

**Issues:** L1  
**Files to create:** `/home/nitekeeper/apps/kaizen/migrations/002_add_fk_indexes.sql`

**Failing test first**  
File: `tests/test_migrate.py`  
Test name: `test_all_tables_created` — extend to verify indexes exist after migration 002.  
Add to `test_all_tables_created`: after calling `apply_migrations`, query `sqlite_master` for index names:
```python
indexes = {row[0] for row in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
).fetchall()}
for expected_idx in {"idx_runs_project_id", "idx_cycles_run_id", "idx_abandonments_cycle_id"}:
    assert expected_idx in indexes, f"Missing index {expected_idx!r}"
```
This assertion fails until the migration file exists.

**Implementation step**  
Create `/home/nitekeeper/apps/kaizen/migrations/002_add_fk_indexes.sql` with:
```sql
CREATE INDEX IF NOT EXISTS idx_runs_project_id ON runs(project_id);
CREATE INDEX IF NOT EXISTS idx_cycles_run_id ON cycles(run_id);
CREATE INDEX IF NOT EXISTS idx_abandonments_cycle_id ON abandonments(cycle_id);
```

Also update `test_migration_is_idempotent` in `tests/test_migrate.py` to assert `count == 2` (since there are now 2 migration files) and update `test_migration_recorded` to check both filenames are present.

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest tests/test_migrate.py -v
```

**Commit message**  
`feat(migrations): add 002_add_fk_indexes.sql for runs, cycles, abandonments FK columns`

---

## Task 7 — Add CHECK constraints to abandonments schema (M8)

**Issues:** M8  
**Files to modify:** `/home/nitekeeper/apps/kaizen/migrations/001_kaizen_schema.sql`, `/home/nitekeeper/apps/kaizen/tests/test_migrate.py`

**Failing test first**  
File: `tests/test_migrate.py`  
Test name: `test_abandonments_phase_and_reason_check_constraints` (new test to add)  
Add to `test_migrate.py`:
```python
def test_abandonments_phase_reached_check_rejects_invalid(tmp_path):
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    with closing(get_connection(db_path)) as conn:
        project_id = _insert_project(conn)
        conn.execute(
            "INSERT INTO runs (project_id, branch, cycles_requested, started_at, status) "
            "VALUES (?, 'kaizen/test', 1, datetime('now'), 'running')", (project_id,)
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO cycles (run_id, cycle_n, status, started_at) "
            "VALUES (?, 1, 'abandoned', datetime('now'))", (run_id,)
        )
        cycle_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO abandonments (cycle_id, phase_reached, reason, detail, created_at) "
                "VALUES (?, 'invalid-phase', 'no_consensus', 'detail', datetime('now'))",
                (cycle_id,)
            )
            conn.commit()


def test_abandonments_reason_check_rejects_invalid(tmp_path):
    db_path = str(tmp_path / "test.db")
    apply_migrations(db_path, MIGRATIONS_DIR)
    with closing(get_connection(db_path)) as conn:
        project_id = _insert_project(conn)
        conn.execute(
            "INSERT INTO runs (project_id, branch, cycles_requested, started_at, status) "
            "VALUES (?, 'kaizen/test', 1, datetime('now'), 'running')", (project_id,)
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO cycles (run_id, cycle_n, status, started_at) "
            "VALUES (?, 1, 'abandoned', datetime('now'))", (run_id,)
        )
        cycle_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO abandonments (cycle_id, phase_reached, reason, detail, created_at) "
                "VALUES (?, 'meeting', 'push_failed', 'detail', datetime('now'))",
                (cycle_id,)
            )
            conn.commit()
```
Both tests fail because `001_kaizen_schema.sql` currently has no CHECK on `abandonments`.

Note: the existing test `test_abandonments_reason_and_phase_are_free_text` must be **removed** (or renamed to `test_abandonments_valid_values_accepted`) since it asserts the opposite invariant.

**Implementation step**  
In `/home/nitekeeper/apps/kaizen/migrations/001_kaizen_schema.sql`, change the `abandonments` table DDL. Replace:
```sql
  phase_reached       TEXT NOT NULL,
  reason              TEXT NOT NULL,
```
with:
```sql
  phase_reached       TEXT NOT NULL CHECK (phase_reached IN ('agenda','meeting','implementation','test')),
  reason              TEXT NOT NULL CHECK (reason IN ('no_consensus','destructive_rejected','tests_unrecoverable','other')),
```

In `tests/test_migrate.py`:
1. Remove `test_abandonments_reason_and_phase_are_free_text` (the old free-text assertion).
2. Add the two new tests above.

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest tests/test_migrate.py -v
```

**Commit message**  
`fix(schema): add CHECK constraints to abandonments.phase_reached and reason; remove push_failed`

---

## Task 8 — Fix `r.role_name` SQL alias in expert-roster skill (C1)

**Issues:** C1  
**Files to modify:** `/home/nitekeeper/apps/kaizen/internal/expert-roster/SKILL.md`

**Failing test first**  
File: `tests/test_skill_frontmatter.py`  
Test name: add `test_expert_roster_sql_uses_correct_column_alias` (new test)  
```python
def test_expert_roster_sql_uses_correct_column_alias():
    skill_path = Path(__file__).resolve().parents[1] / "internal" / "expert-roster" / "SKILL.md"
    content = skill_path.read_text()
    # Must NOT contain the broken alias
    assert "r.role_name" not in content, \
        "SQL uses r.role_name which does not exist; should be r.name AS role_name"
    # Must contain the correct alias
    assert "r.name AS role_name" in content
```
This test fails on the current file which contains `r.role_name` on line 49.

**Implementation step**  
In `/home/nitekeeper/apps/kaizen/internal/expert-roster/SKILL.md`, locate the SQL query block starting around line 43. Change line 49:
- Old: `'       r.role_name '`
- New: `'       r.name AS role_name '`

Also update the comment on line 65 that says `roles(id TEXT PRIMARY KEY, role_name TEXT, ...)`:
- Old: `roles(id TEXT PRIMARY KEY, role_name TEXT, ...)`
- New: `roles(id INTEGER PRIMARY KEY, name TEXT, ...)` (also corrects the `id` type to match the actual `INTEGER AUTOINCREMENT` schema)

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest tests/test_skill_frontmatter.py -v
```

**Commit message**  
`fix(expert-roster): correct SQL column alias r.role_name → r.name AS role_name`

---

## Task 9 — Minimal env dict for atelier subprocesses (H7)

**Issues:** H7  
**Files to modify:** `/home/nitekeeper/apps/kaizen/scripts/seed_atelier_in_clone.py`

**Failing test first**  
File: `tests/test_seed_atelier_in_clone.py`  
Test name: add `test_atelier_env_does_not_forward_secrets` (new test)  
```python
def test_atelier_env_does_not_forward_secrets(monkeypatch):
    from scripts.seed_atelier_in_clone import _atelier_env, find_atelier_root
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super-secret-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-super-secret")
    monkeypatch.setenv("HOME", "/home/testuser")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    atelier_root = find_atelier_root()
    env = _atelier_env(atelier_root)
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "HOME" in env
    assert "PATH" in env
    assert "PYTHONPATH" in env
```
This test fails before the fix because `os.environ.copy()` propagates all env vars.

**Implementation step**  
In `/home/nitekeeper/apps/kaizen/scripts/seed_atelier_in_clone.py`, replace the `_atelier_env` function (lines 55–58):

Old:
```python
def _atelier_env(atelier_root: Path) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(atelier_root)
    return env
```

New:
```python
def _atelier_env(atelier_root: Path) -> dict:
    """Return a minimal environment dict for atelier subprocesses.

    Forwards only PATH, HOME, PYTHONPATH, and locale vars.
    Never forwards session tokens, API keys, or other ambient credentials.
    """
    import os as _os
    env: dict[str, str] = {}
    for key in ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TEMP", "TMP"):
        val = _os.environ.get(key)
        if val is not None:
            env[key] = val
    env["PYTHONPATH"] = str(atelier_root)
    return env
```

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest tests/test_seed_atelier_in_clone.py -v
```

**Commit message**  
`fix(seed_atelier): replace os.environ.copy() with minimal env dict to prevent secret leakage`

---

## Task 10 — Copy roles/agents from atelier's DB into clone DB (C3)

**Issues:** C3  
**Files to modify:** `/home/nitekeeper/apps/kaizen/scripts/seed_atelier_in_clone.py`

**Failing test first**  
File: `tests/test_seed_atelier_in_clone.py`  
Test name: `TestSeedSchemaAndRoles::test_roles_populated_with_at_least_60_records`  
Current behaviour: after `seed_atelier_schema(clone)` + `seed_atelier_roles(clone)`, querying the clone's `.ai/memex.db` for `SELECT COUNT(*) FROM roles` returns 0 because `seed_roles.py` routes writes to `~/.memex/agents.db` via `mode_detector`, not to the clone DB passed as argument.  
Assertion: `assert count >= 60` fails.

**Implementation step**  
Add a new function `_copy_roles_agents_from_atelier(clone_dir: Path) -> None` after `_atelier_env` in `/home/nitekeeper/apps/kaizen/scripts/seed_atelier_in_clone.py`.

The implementation must:

1. Resolve the Memex registry path: `Path.home() / ".memex" / "registry.json"`. If the file does not exist, raise `RuntimeError("Memex registry not found at ~/.memex/registry.json")`.
2. Read the registry JSON, extract `registry["agents"]["path"]` as the source DB path string.
3. Open both DBs with `sqlite3.connect`: `src_conn = sqlite3.connect(agents_db_path)` and `dst_conn = sqlite3.connect(str(clone_dir / ".ai" / "memex.db"))`.
4. Wrap all operations in `try/finally` closing both connections.
5. Copy `roles` rows: `SELECT id, name, description, created_at, updated_at FROM roles` from source; `INSERT OR REPLACE INTO roles (id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)` into destination.
6. Copy `agents` rows: `SELECT id, name, role_id, profile, created_at, updated_at FROM agents` from source; `INSERT OR REPLACE INTO agents (id, name, role_id, profile, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)` into destination.
7. Call `dst_conn.commit()` once after all inserts.

Then modify `seed_atelier_roles` to call `_copy_roles_agents_from_atelier(clone_dir)` immediately after the existing subprocess call to `seed_roles.py` succeeds.

Full function body:
```python
def _copy_roles_agents_from_atelier(clone_dir: Path) -> None:
    """Copy roles and agents from atelier's Memex DB into the clone's local DB.

    seed_roles.py always routes writes to ~/.memex/agents.db (via mode_detector),
    ignoring the db_path argument. This function copies the result into the clone.
    """
    import json
    import sqlite3 as _sqlite3

    registry_path = Path.home() / ".memex" / "registry.json"
    if not registry_path.exists():
        raise RuntimeError(
            f"Memex registry not found at {registry_path}. "
            "Run Atelier setup before seeding a clone."
        )
    registry = json.loads(registry_path.read_text())
    agents_db_path = registry["agents"]["path"]

    dst_db = str(clone_dir / ".ai" / "memex.db")
    src_conn = _sqlite3.connect(agents_db_path)
    dst_conn = _sqlite3.connect(dst_db)
    try:
        roles = src_conn.execute(
            "SELECT id, name, description, created_at, updated_at FROM roles"
        ).fetchall()
        for row in roles:
            dst_conn.execute(
                "INSERT OR REPLACE INTO roles (id, name, description, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                row,
            )
        agents = src_conn.execute(
            "SELECT id, name, role_id, profile, created_at, updated_at FROM agents"
        ).fetchall()
        for row in agents:
            dst_conn.execute(
                "INSERT OR REPLACE INTO agents (id, name, role_id, profile, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                row,
            )
        dst_conn.commit()
    finally:
        src_conn.close()
        dst_conn.close()
```

Also add `import json` to the top of the file if not already present, and add `import sqlite3` at the top level.

In `seed_atelier_roles`, after the `if result.returncode != 0:` block (i.e., after the subprocess succeeds), add:
```python
    _copy_roles_agents_from_atelier(clone_dir)
```

Add `@pytest.mark.skipif` to the integration tests in `TestSeedSchemaAndRoles` per Task 17 (M10).

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest tests/test_seed_atelier_in_clone.py -v
```

**Commit message**  
`fix(seed): copy roles+agents from ~/.memex/agents.db into clone DB after seed_roles.py runs`

---

## Task 11 — Fix `clone_repo` branch parameter (H1)

**Issues:** H1  
**Files to modify:** `/home/nitekeeper/apps/kaizen/scripts/clone.py`

**Failing test first**  
File: `tests/test_clone.py`  
Test name: add `test_clone_uses_specified_branch` (new test in `TestCloneRepo` class):
```python
def test_clone_uses_specified_branch(self, tmp_path, bare_remote, source_repo):
    from scripts.clone import clone_repo
    from tests.conftest import _git
    # Create a non-main branch in source_repo and push it
    _git(["checkout", "-b", "develop"], source_repo)
    (source_repo / "develop_file.txt").write_text("develop branch content")
    _git(["add", "."], source_repo)
    _git(["commit", "-m", "add develop file"], source_repo)
    _git(["push", "origin", "develop"], source_repo)
    _git(["checkout", "main"], source_repo)

    dest = tmp_path / "clone_develop"
    clone_repo(str(bare_remote), dest, branch="develop")
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=dest, capture_output=True, text=True,
    )
    assert result.stdout.strip() == "develop"
    assert (dest / "develop_file.txt").exists()
```
This fails before the fix because `clone_repo` always passes `-b main` regardless of the `branch` argument.

**Implementation step**  
In `/home/nitekeeper/apps/kaizen/scripts/clone.py`, change the `clone_repo` signature and subprocess call:

Old:
```python
def clone_repo(remote_url: str, dest: Path) -> None:
    ...
    subprocess.run(
        ["git", "clone", "-b", "main", remote_url, str(dest)],
```

New:
```python
def clone_repo(remote_url: str, dest: Path, branch: str = "main") -> None:
    ...
    subprocess.run(
        ["git", "clone", "-b", branch, remote_url, str(dest)],
```

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest tests/test_clone.py -v
```

**Commit message**  
`fix(clone): add branch parameter to clone_repo; pass project base_branch instead of hardcoding main`

---

## Task 12 — Remove stale dir before re-clone; clean up on seed failure (H2, M1)

**Issues:** H2, M1  
**Files to modify:** `/home/nitekeeper/apps/kaizen/scripts/run.py`

**Failing test first**  
File: `tests/test_run.py`  
Test name: add `test_orchestrate_run_removes_stale_experiment_dir` (new test):
```python
def test_orchestrate_run_removes_stale_experiment_dir(db, project, tmp_path, monkeypatch):
    from pathlib import Path
    stubs = _install_orchestrator_stubs(monkeypatch, tmp_path)
    # Pre-create a stale experiment dir with a sentinel file
    experiment_dir = tmp_path / "experiment" / "owner-repo"
    experiment_dir.mkdir(parents=True)
    sentinel = experiment_dir / "stale_sentinel.txt"
    sentinel.write_text("stale")

    def fake_executor(clone_dir, proj, run_row, cycle_n):
        return {"status": "success", "commit_sha": "abc", "minutes_memex_slug": None}

    result = orchestrate_run(
        db_path=db,
        git_url=project["git_url"],
        cycles_requested=1,
        cycle_executor=fake_executor,
    )
    assert result["status"] == "complete"
    # Stale sentinel must be gone — the dir was wiped before re-clone
    assert not sentinel.exists()
```
And add `test_orchestrate_run_cleans_up_on_seed_failure`:
```python
def test_orchestrate_run_cleans_up_on_seed_failure(db, project, tmp_path, monkeypatch):
    _install_orchestrator_stubs(monkeypatch, tmp_path)
    import scripts.seed_atelier_in_clone as seed_mod
    monkeypatch.setattr(seed_mod, "seed_all", lambda d: (_ for _ in ()).throw(RuntimeError("seed boom")))

    experiment_dir = tmp_path / "experiment" / "owner-repo"
    with pytest.raises(RuntimeError, match="seed boom"):
        orchestrate_run(
            db_path=db,
            git_url=project["git_url"],
            cycles_requested=1,
            cycle_executor=lambda *a: {"status": "success", "commit_sha": "x", "minutes_memex_slug": None},
        )
    # Experiment dir must be cleaned up after seed failure
    assert not experiment_dir.exists()
```

**Implementation step**  
In `/home/nitekeeper/apps/kaizen/scripts/run.py`, add `import shutil` at the top (in the stdlib imports area, before the `from scripts.db` line).

Then in `orchestrate_run`, around line 203–211:

1. After `experiment_dir = experiment_dir_for(kaizen_root(), git_url)` and before `clone_repo(git_url, experiment_dir)`, add:
   ```python
   if experiment_dir.exists():
       from scripts.platform_utils import safe_rmtree
       safe_rmtree(experiment_dir)
   ```

2. Wrap the `seed_all` call in a try/except:
   ```python
   # 3. Seed atelier
   try:
       seed_all(experiment_dir)
   except Exception:
       shutil.rmtree(experiment_dir, ignore_errors=True)
       raise
   ```

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest tests/test_run.py -v
```

**Commit message**  
`fix(run): remove stale experiment dir before re-clone; clean up on seed_all failure`

---

## Task 13 — Wrap cycle loop in try/except to finalize failed runs (H3)

**Issues:** H3  
**Files to modify:** `/home/nitekeeper/apps/kaizen/scripts/run.py`

**Failing test first**  
File: `tests/test_run.py`  
Test name: add `test_orchestrate_run_cycle_exception_sets_status_failed`:
```python
def test_orchestrate_run_cycle_exception_sets_status_failed(db, project, tmp_path, monkeypatch):
    _install_orchestrator_stubs(monkeypatch, tmp_path)

    def exploding_executor(clone_dir, proj, run_row, cycle_n):
        raise RuntimeError("cycle exploded")

    with pytest.raises(RuntimeError, match="cycle exploded"):
        orchestrate_run(
            db_path=db,
            git_url=project["git_url"],
            cycles_requested=2,
            cycle_executor=exploding_executor,
        )

    # The run row must not remain in 'running' status
    runs = list_runs(db)
    assert len(runs) == 1
    assert runs[0]["status"] == "failed", f"Expected 'failed', got {runs[0]['status']!r}"
```
Before the fix, `runs[0]["status"]` is `"running"`.

**Implementation step**  
In `/home/nitekeeper/apps/kaizen/scripts/run.py`, inside `orchestrate_run`, wrap the cycle loop (lines 227–269, the `for cycle_n in range(...)` block) in a `try/except`:

```python
    try:
        for cycle_n in range(1, cycles_requested + 1):
            # ... existing cycle loop body unchanged ...
    except Exception:
        finalize_run(
            db_path=db_path,
            run_id=run_row["id"],
            cycles_succeeded=cycles_succeeded,
            cycles_abandoned=cycles_abandoned,
            pr_url=None,
            status="failed",
        )
        raise
```

The existing code from `cycles_succeeded = 0` through the end of the `for` loop stays intact inside the `try` block. The `except Exception` block calls `finalize_run` before re-raising.

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest tests/test_run.py -v
```

**Commit message**  
`fix(run): wrap cycle loop in try/except; call finalize_run(status="failed") before re-raising`

---

## Task 14 — Add `check_atelier` to setup.py (H4)

**Issues:** H4, M11  
**Files to modify:** `/home/nitekeeper/apps/kaizen/scripts/setup.py`, `/home/nitekeeper/apps/kaizen/tests/test_setup.py`

**Failing test first**  
File: `tests/test_setup.py`  
Test name: `TestVerifyAll::test_returns_three_checks` — after adding `check_atelier`, this becomes a 4-check list and the assertion `assert len(checks) == 3` fails.  
Also add `test_check_atelier_present`:
```python
def test_check_atelier_present(monkeypatch):
    # Simulate atelier cache present by making find_atelier_root succeed
    import scripts.seed_atelier_in_clone as seed_mod
    fake_root = Path("/fake/atelier")
    monkeypatch.setattr(seed_mod, "find_atelier_root", lambda: fake_root)
    from scripts.setup import check_atelier
    c = check_atelier()
    assert c.ok is True
    assert str(fake_root) in c.detail

def test_check_atelier_missing(monkeypatch):
    import scripts.seed_atelier_in_clone as seed_mod
    monkeypatch.setattr(
        seed_mod, "find_atelier_root",
        lambda: (_ for _ in ()).throw(RuntimeError("not found"))
    )
    from scripts.setup import check_atelier
    c = check_atelier()
    assert c.ok is False
    assert "atelier" in c.fix.lower()
```
`test_check_atelier_present` fails before the fix because `check_atelier` does not exist.

**Implementation step**  
In `/home/nitekeeper/apps/kaizen/scripts/setup.py`:

1. After the existing `check_python_version` function and before `verify_all`, add:
   ```python
   def check_atelier() -> DepCheck:
       name = "atelier"
       fix = "Install Atelier via Agora: run `agora install atelier` in Claude Code"
       from scripts.seed_atelier_in_clone import find_atelier_root
       try:
           root = find_atelier_root()
           return DepCheck(name, True, str(root), fix)
       except RuntimeError as exc:
           return DepCheck(name, False, str(exc), fix)
   ```

2. In `verify_all`, add `check_atelier()` to the returned list:
   ```python
   def verify_all() -> list[DepCheck]:
       return [
           check_git(),
           check_gh(),
           check_python_version(),
           check_atelier(),
       ]
   ```

In `tests/test_setup.py`:

1. Update `TestVerifyAll::test_returns_three_checks`:
   - Rename to `test_returns_four_checks`
   - Change `assert len(checks) == 3` to `assert len(checks) == 4`
   - Change `assert {c.name for c in checks} == {"git", "gh", "python"}` to `assert {c.name for c in checks} == {"git", "gh", "python", "atelier"}`

2. Update `_all_present` helper to also patch `check_atelier` via monkeypatching `find_atelier_root`:
   ```python
   import scripts.seed_atelier_in_clone as seed_mod
   monkeypatch.setattr(seed_mod, "find_atelier_root", lambda: Path("/fake/atelier/1.1.0"))
   ```

3. Add the two new test methods to `TestVerifyAll` or a new `TestCheckAtelier` class.

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest tests/test_setup.py -v
```

**Commit message**  
`feat(setup): add check_atelier() using find_atelier_root(); update verify_all to 4 checks`

---

## Task 15 — Replace `sys.exit` with `RuntimeError` in library functions (H6, M5)

**Issues:** H6, M5  
**Files to modify:** `/home/nitekeeper/apps/kaizen/scripts/destructive_check.py`, `/home/nitekeeper/apps/kaizen/scripts/worktree.py`

**Failing test first**  
File: `tests/test_destructive_check.py`  
Test name: add `test_get_diff_raises_on_git_failure` (new test):
```python
def test_get_diff_raises_on_git_failure(tmp_path):
    from scripts.destructive_check import get_diff
    # tmp_path is not a git repo; git diff will fail
    with pytest.raises(RuntimeError, match="git diff failed"):
        get_diff(tmp_path)
```
Before the fix, `get_diff` calls `sys.exit(1)` which raises `SystemExit`, not `RuntimeError`.

File: `tests/test_worktree.py` (add a new test file or extend existing — no existing test file for worktree; add `tests/test_worktree.py`):
```python
def test_merge_back_raises_on_detached_head(tmp_path):
    import subprocess
    from scripts.worktree import merge_back
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True, capture_output=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    # Simulate linked worktree detection by mocking detect_worktree
    # For a simpler path: mock get_current_branch to return empty string
    import scripts.worktree as wt_mod
    from unittest.mock import patch
    with patch.object(wt_mod, "detect_worktree", return_value=(True, ".git/worktrees/wt")):
        with patch.object(wt_mod, "get_current_branch", return_value=""):
            with pytest.raises(RuntimeError, match="detached HEAD"):
                merge_back(repo)
```
Before the fix, `merge_back` calls `sys.exit(1)` which raises `SystemExit`.

**Implementation step**  
In `/home/nitekeeper/apps/kaizen/scripts/destructive_check.py`, change `get_diff` (lines 22–25):
- Old: `sys.exit(1)` at the end of the error path
- New:
  ```python
  if result.returncode != 0:
      raise RuntimeError(
          f"git diff failed in {clone_dir}: {result.stderr.strip()}"
      )
  ```

In `/home/nitekeeper/apps/kaizen/scripts/worktree.py`, replace all four `sys.exit(1)` calls in `merge_back` (lines 79, 102, 114, 141) with `raise RuntimeError(...)`, using the existing error message text as the exception argument. Specifically:

- Line 79: `raise RuntimeError("Worktree is in detached HEAD state. Re-attach to a branch before saving:\n  git checkout -b <branch-name>")`
- Line 102: `raise RuntimeError(f"Main workspace is on '{main_current}', not '{base_branch}'.\n...")`  (keep full message)
- Line 114: `raise RuntimeError(f"Main workspace has uncommitted changes.\n...")`  (keep full message)
- Line 141: `raise RuntimeError(f"CONFLICT: Merge of '{wt_branch}' into '{base_branch}' produced conflicts.\n...")`  (keep full message)

In all four cases remove the `print(...)` call that precedes `sys.exit(1)` since the `RuntimeError` message carries the same text.

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest tests/test_destructive_check.py tests/test_worktree.py -v
```

**Commit message**  
`fix: replace sys.exit(1) with RuntimeError in destructive_check.get_diff and worktree.merge_back`

---

## Task 16 — Gate `def` removal check on `.py` extension (M4)

**Issues:** M4  
**Files to modify:** `/home/nitekeeper/apps/kaizen/scripts/destructive_check.py`

**Failing test first**  
File: `tests/test_destructive_check.py`  
Test name: add `test_removed_def_in_markdown_not_flagged` (new test in `TestDetectDestructive`):
```python
def test_removed_def_in_markdown_not_flagged(self, tmp_path):
    # A markdown file with a removed line containing "def" — should not trigger
    diff = (
        "diff --git a/docs/api.md b/docs/api.md\n"
        "index abc..def 100644\n"
        "--- a/docs/api.md\n"
        "+++ b/docs/api.md\n"
        "@@ -1,3 +1,2 @@\n"
        "-def get_phase: returns the current phase\n"
        " ## Overview\n"
    )
    issues = detect_destructive(diff, tmp_path)
    assert not any(i["type"] == "removed_public_function" for i in issues)
```
Before the fix, this fails because `_check_removed_public_functions` matches the `-def` line regardless of file type.

**Implementation step**  
In `/home/nitekeeper/apps/kaizen/scripts/destructive_check.py`, modify `_check_removed_public_functions` to track the current file and only check `.py` files. Replace the function body:

```python
def _check_removed_public_functions(diff_text: str) -> list[dict]:
    """Flag removed top-level public function definitions (not starting with _).
    
    Only checks .py files — ignores def removal in markdown, YAML, etc.
    """
    issues = []
    current_file = "unknown"
    for line in diff_text.splitlines():
        header = re.match(r"^diff --git a/(.+?) b/\1$", line)
        if header:
            current_file = header.group(1)
        if not current_file.endswith(".py"):
            continue
        m = re.match(r"^-(?:async\s+)?def ([a-zA-Z][a-zA-Z0-9_]*)\(", line)
        if m:
            issues.append({
                "type": "removed_public_function",
                "description": f"Public function '{m.group(1)}' was removed",
                "file": current_file,
            })
    return issues
```

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest tests/test_destructive_check.py -v
```

**Commit message**  
`fix(destructive_check): gate public-function removal check on .py file extension`

---

## Task 17 — Add skipif guard to seed_atelier integration tests (M10)

**Issues:** M10  
**Files to modify:** `/home/nitekeeper/apps/kaizen/tests/test_seed_atelier_in_clone.py`

**Failing test first**  
File: `tests/test_seed_atelier_in_clone.py`  
Test name: `TestFindAtelierRoot::test_returns_a_path_with_atelier_markers`  
In CI environments without the Atelier plugin cache, this test raises `RuntimeError` rather than skipping. The test must skip gracefully.  
Assertion: after adding `skipif`, running in a CI environment with no atelier cache produces `SKIPPED` (not `ERROR` or `FAILED`).

**Implementation step**  
In `/home/nitekeeper/apps/kaizen/tests/test_seed_atelier_in_clone.py`, add at module level after the imports:

```python
from scripts.seed_atelier_in_clone import _AGORA_ATELIER

_ATELIER_PRESENT = _AGORA_ATELIER.is_dir()
_SKIP_NO_ATELIER = pytest.mark.skipif(
    not _ATELIER_PRESENT,
    reason="Atelier plugin cache not present at ~/.claude/plugins/cache/agora/atelier/",
)
```

Apply `@_SKIP_NO_ATELIER` as a decorator to every test class and standalone test function that exercises the real atelier subprocess:
- `TestFindAtelierRoot` (entire class)
- `TestSeedSchemaAndRoles` (entire class)

The `TestEnsureWikiDir` class does not call atelier subprocesses and needs no skip guard.

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest tests/test_seed_atelier_in_clone.py -v
```

**Commit message**  
`fix(tests): add skipif guard for Atelier plugin cache in test_seed_atelier_in_clone.py`

---

## Task 18 — Fix column injection in `update_project` (M6)

**Issues:** M6  
**Files to modify:** `/home/nitekeeper/apps/kaizen/scripts/project.py`

**Failing test first**  
File: `tests/test_project.py`  
Test name: add `test_update_project_rejects_invalid_column_name` (new test):
```python
def test_update_project_rejects_invalid_column_name(db):
    created = _make(db)
    with pytest.raises((ValueError, KeyError)):
        # Attempting to update a column whose name contains SQL injection characters
        update_project(db, created["id"], **{"name; DROP TABLE projects--": "evil"})
```
Before the fix, the f-string builds `SET name; DROP TABLE projects-- = ?` and SQLite raises `OperationalError` rather than a clean `ValueError`. We want an explicit validation error before the query runs.

**Implementation step**  
In `/home/nitekeeper/apps/kaizen/scripts/project.py`, in `update_project`, add column name validation before building `set_clause`. Replace the block beginning with `set_clause = ...`:

Old:
```python
    set_clause = ", ".join(f"{k} = ?" for k in updates)
```

New:
```python
    import re as _re
    for k in updates:
        if not _re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*', k):
            raise ValueError(
                f"Invalid column name {k!r}: only [a-zA-Z_][a-zA-Z0-9_]* allowed"
            )
    set_clause = ", ".join(f"{k} = ?" for k in updates)
```

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest tests/test_project.py -v
```

**Commit message**  
`fix(project): validate column names with fullmatch before building SET clause in update_project`

---

## Task 19 — Read `base_branch` from git during project registration (L2)

**Issues:** L2  
**Files to modify:** `/home/nitekeeper/apps/kaizen/scripts/project.py`

**Failing test first**  
File: `tests/test_project.py`  
Test name: add `test_register_cli_detects_base_branch` — however, `_register_cli` is an interactive CLI function that is difficult to unit test directly. Instead, add a unit test for the helper function that reads the branch from git.

Add a new test `test_detect_base_branch_reads_from_git`:
```python
def test_detect_base_branch_reads_from_git(tmp_path):
    import subprocess
    from scripts.project import _detect_base_branch
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "trunk", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True, capture_output=True)
    (repo / "README.md").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    # Simulate remote HEAD pointing to trunk
    assert _detect_base_branch(repo) == "trunk"
```
This test fails before the fix because `_detect_base_branch` does not exist.

**Implementation step**  
In `/home/nitekeeper/apps/kaizen/scripts/project.py`, add a new helper function before `_register_cli`:

```python
def _detect_base_branch(repo_dir: Path) -> str:
    """Detect the default branch of the remote origin.

    Tries `git symbolic-ref refs/remotes/origin/HEAD` first.
    Falls back to the current local branch name.
    Falls back to 'main' if neither resolves.
    """
    import subprocess as _sp
    result = _sp.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode == 0:
        # output: refs/remotes/origin/main → main
        ref = result.stdout.strip()
        return ref.split("/")[-1] if ref else "main"
    # Fallback: current branch
    result2 = _sp.run(
        ["git", "branch", "--show-current"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    branch = result2.stdout.strip()
    return branch if branch else "main"
```

In `_register_cli`, change the `create_project` call to use `_detect_base_branch(dest)` instead of the hardcoded `"main"`:
```python
        project = create_project(
            db_path=db_path,
            git_url=git_url,
            name=_name_from_url(git_url),
            base_branch=_detect_base_branch(dest),
            ...
        )
```

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest tests/test_project.py -v
```

**Commit message**  
`fix(project): detect base_branch from git symbolic-ref during registration instead of hardcoding main`

---

## Task 20 — Fix `_fmt_ts` UTC normalization (L3)

**Issues:** L3  
**Files to modify:** `/home/nitekeeper/apps/kaizen/scripts/pr.py`

**Failing test first**  
File: `tests/test_pr.py`  
Test name: add `test_fmt_ts_normalizes_naive_datetime_to_utc` (new test):
```python
def test_fmt_ts_normalizes_naive_datetime_to_utc():
    from scripts.pr import _fmt_ts
    # A naive ISO timestamp (no tz offset) must still render as UTC
    result = _fmt_ts("2026-05-16T14:23:00")
    assert result.endswith("UTC"), f"Expected UTC suffix, got: {result!r}"
    assert "2026-05-16 14:23" in result

def test_fmt_ts_with_offset_normalizes_to_utc():
    from scripts.pr import _fmt_ts
    # +05:30 offset should be converted to UTC (14:23 - wait: 08:53 UTC)
    result = _fmt_ts("2026-05-16T14:23:00+05:30")
    assert result.endswith("UTC")
    assert "2026-05-16 08:53" in result
```
Before the fix, `_fmt_ts` calls `dt.strftime(...)` directly. If `dt` is naive (no tzinfo), the `%Z` placeholder renders as empty string not "UTC", causing the format string `"%Y-%m-%d %H:%M UTC"` to produce a literal "UTC" suffix regardless — so the suffix test passes, but an offset-aware timestamp is not converted to UTC before formatting.

**Implementation step**  
In `/home/nitekeeper/apps/kaizen/scripts/pr.py`, change the `_fmt_ts` import line at the top to include `timezone`:
- Old: `from datetime import datetime`
- New: `from datetime import datetime, timezone`

Replace the `_fmt_ts` function:
```python
def _fmt_ts(ts: str | None) -> str:
    """Format an ISO timestamp string as 'YYYY-MM-DD HH:MM UTC'.

    Normalises to UTC. Tolerates None and non-ISO inputs (returns '—' / raw value).
    """
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    if dt.tzinfo is None:
        # Treat naive timestamps as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")
```

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest tests/test_pr.py -v
```

**Commit message**  
`fix(pr): normalise timestamps to UTC in _fmt_ts before formatting`

---

## Task 21 — Return markdown from `process_abandonment`; fix db.py chmod; close connection in test (M2, L9, L8)

**Issues:** M2, L9, L8  
**Files to modify:** `/home/nitekeeper/apps/kaizen/scripts/abandonment.py`, `/home/nitekeeper/apps/kaizen/scripts/db.py`, `/home/nitekeeper/apps/kaizen/tests/test_project.py`

**Failing test first**  
File: `tests/test_abandonment.py`  
Test name: add `test_process_abandonment_returns_markdown_in_row` (new test):
```python
def test_process_abandonment_returns_rendered_markdown(db, run_and_cycle):
    row = process_abandonment(
        db_path=db,
        project=run_and_cycle["project"],
        run_id=run_and_cycle["run"]["id"],
        cycle_id=run_and_cycle["cycle"]["id"],
        cycle_n=1,
        subject="x",
        participants=["pm"],
        phase_reached="meeting",
        reason="no_consensus",
        detail="d",
        artifacts=[],
    )
    # The function must return the abandonment row dict.
    # The rendered markdown is now stored as a side-effect in .ai/wiki/<slug>.md
    # (not returned inline), but the row must contain the slug for cross-referencing.
    assert row["report_memex_slug"].startswith("kaizen:abandonment:")
```

File: `tests/test_db.py`  
Test name: add `test_db_file_permissions_are_0600` (new test):
```python
def test_db_file_permissions_are_0600(tmp_path):
    import stat
    db_path = str(tmp_path / "test.db")
    get_connection(db_path).close()
    mode = oct(stat.S_IMODE(Path(db_path).stat().st_mode))
    assert mode == oct(0o600), f"Expected 0600, got {mode}"
```

File: `tests/test_project.py`  
Test name: `test_lists_stored_as_json_strings_in_db` — currently uses `with get_connection(db) as conn:` which is a context manager that commits/rolls back but does not close. The fix is to use `closing()`.  
The test does not fail as-is on Linux, but the `closing()` wrapping is the specified fix.

**Implementation step**  

**M2 — `abandonment.py`:** The current `process_abandonment` already returns the `record_abandonment` result row (the markdown is computed but not returned from the function). The real M2 bug is that the markdown is computed into `markdown` variable but never captured to wiki or returned. The fix: write the markdown to the `.ai/wiki/<slug>.md` file in the kaizen project (not the clone), so the agent can call `memex:run capture` on it afterwards. However, since writing to the project wiki is not in scope for `process_abandonment` (that would require passing the kaizen project dir), the simplest fix is to return the markdown string as a second return value or to make `process_abandonment` write the markdown to a temp path.

Per the design, the correct fix is: `process_abandonment` should return the `(row, markdown)` tuple so the orchestrator can capture it. Update the function signature and return type:
```python
def process_abandonment(...) -> tuple[dict, str]:
    markdown = format_report(...)
    slug = _slug_for(run_id, cycle_n)
    row = record_abandonment(...)
    return row, markdown
```

Update all callers. In `scripts/run.py`, the call `ab_row = process_abandonment(...)` becomes `ab_row, _ab_markdown = process_abandonment(...)`.

**L9 — `db.py`:** In `/home/nitekeeper/apps/kaizen/scripts/db.py`, modify `get_connection` to create the DB file with mode `0600` if it does not already exist:
```python
import os
import sqlite3


def get_connection(db_path: str) -> sqlite3.Connection:
    path = db_path if db_path == ":memory:" else str(db_path)
    if path != ":memory:" and not os.path.exists(path):
        # Create with restricted permissions before SQLite opens it
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
```

**L8 — `test_project.py`:** In `test_lists_stored_as_json_strings_in_db` (line 71), replace:
```python
    with get_connection(db) as conn:
        row = conn.execute(...)
```
with:
```python
    from contextlib import closing
    with closing(get_connection(db)) as conn:
        row = conn.execute(...)
```

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest tests/test_abandonment.py tests/test_db.py tests/test_project.py tests/test_run.py -v
```

**Commit message**  
`fix(abandonment,db,test): return markdown from process_abandonment; 0600 DB perms; close conn in test`

---

## Task 22 — Clean `.ai/` paths before `git add -A`; update stale docstring; fix abandonment skill prose (L4, L7, H5, M7)

**Issues:** L4, L7, H5, M7  
**Files to modify:** `/home/nitekeeper/apps/kaizen/scripts/cycle_git.py`, `/home/nitekeeper/apps/kaizen/scripts/git_utils.py`, `/home/nitekeeper/apps/kaizen/internal/abandonment-report/SKILL.md`

**Failing test first**  
File: `tests/test_cycle_git.py`  
Test name: add `test_commit_cycle_excludes_ai_dir` (new test in `TestCommitCycle`):
```python
def test_commit_cycle_excludes_ai_dir(self, tmp_path, bare_remote, source_repo):
    dest = tmp_path / "clone"
    clone_repo(str(bare_remote), dest)
    create_branch(dest, "test-exclusion")
    # Create a change in a real file and also noise in .ai/
    (dest / "CHANGES.txt").write_text("a real change")
    (dest / ".ai").mkdir(exist_ok=True)
    (dest / ".ai" / "session_debug.log").write_text("debug noise")
    commit_cycle(
        clone_dir=dest, cycle_n=1,
        decisions=["real change"],
        participants=["Dr. Test"],
        n_tests=1, subject="test exclusion",
        minutes_rel_path="docs/kaizen/minutes.md",
    )
    result = subprocess.run(
        ["git", "show", "--name-only", "--pretty=format:"],
        cwd=dest, capture_output=True, text=True,
    )
    staged_files = result.stdout.strip().splitlines()
    assert "CHANGES.txt" in staged_files
    assert not any(f.startswith(".ai/") for f in staged_files), \
        f".ai/ files must not be staged: {staged_files}"
```
Before the fix, `.ai/session_debug.log` appears in staged files.

File: `tests/test_skill_frontmatter.py`  
Test name: add `test_abandonment_skill_no_capture_to_memex_reference`:
```python
def test_abandonment_skill_no_capture_to_memex_reference():
    skill_path = Path(__file__).resolve().parents[1] / "internal" / "abandonment-report" / "SKILL.md"
    content = skill_path.read_text()
    assert "capture_to_memex" not in content, \
        "abandonment-report SKILL.md must not reference the non-existent capture_to_memex function"
    assert "push_failed" not in content, \
        "push_failed is a run-level event and must not appear in abandonment SKILL.md reasons"
```

**Implementation step**  

**L4 — `cycle_git.py`:** In `commit_cycle`, before the `_git(["add", "-A"], clone_dir)` call, add cleanup of transient paths:
```python
def commit_cycle(...) -> None:
    """Stage all changes and produce the standard kaizen cycle commit."""
    import shutil
    # Remove transient/debug paths before staging to keep commits clean
    for transient in [".ai", "__pycache__", ".pytest_cache"]:
        transient_path = clone_dir / transient
        if transient_path.exists():
            shutil.rmtree(transient_path, ignore_errors=True)
    _git(["add", "-A"], clone_dir)
    ...
```

**L7 — `git_utils.py`:** Replace the stale docstring on line 1:
- Old: `"""Shared git subprocess helper used by self_improve and worktree modules."""`
- New: `"""Shared git subprocess helper used by cycle_git, clone, and worktree modules."""`

**H5 and M7 — `abandonment-report/SKILL.md`:**
1. Remove `capture_to_memex` from line 9 of the description: change "format report, capture it to Kaizen's own memex" to "format report, write markdown to `.ai/wiki/<slug>.md`, then call `memex:run capture`".
2. Remove `push_failed` from the list of reasons on line 19: remove `, `push_failed`` from the `reason` field description.
3. In the Step-by-step path (lines 85–86), replace:
   ```
   from scripts.abandonment import capture_to_memex
   slug = capture_to_memex('kaizen:abandonment:<run_id>-cycle-<cycle_n>', open('<tmp file with markdown>').read())
   ```
   with:
   ```
   # Write markdown to .ai/wiki/<slug>.md first, then capture via skill:
   # slug = "kaizen:abandonment:<run_id>-cycle-<n>"
   # Path(".ai/wiki").mkdir(parents=True, exist_ok=True)
   # Path(f".ai/wiki/{slug}.md").write_text(markdown)
   # Then invoke: memex:run capture <slug> .ai/wiki/<slug>.md
   ```

**H9 — `skills/improve/SKILL.md`:** Add a pre-flight step verifying `memex:run` is available. In Step 1, after the `python3 scripts/setup.py` check, add:

> **Verify `memex:run` is available:** Check that Memex is listed in `~/.claude/settings.json` under `enabledPlugins["memex@agora"]`. If it is absent, surface the error: "memex@agora plugin not enabled. Enable it via Agora before running kaizen:improve." and abort.

**Run tests**  
```
cd /home/nitekeeper/apps/kaizen && python3 -m pytest tests/test_cycle_git.py tests/test_skill_frontmatter.py -v
```

**Commit message**  
`fix: clean .ai/ before git add; update docstrings; remove capture_to_memex and push_failed from abandonment skill`

---

## Issue-to-task index

| Issue | Task |
|-------|------|
| C1    | Task 8  |
| C2    | Task 3  |
| C3    | Task 10 |
| C4    | Task 5  |
| H1    | Task 11 |
| H2    | Task 12 |
| H3    | Task 13 |
| H4    | Task 14 |
| H5    | Task 22 |
| H6    | Task 15 |
| H7    | Task 9  |
| H9    | Task 22 |
| M1    | Task 12 |
| M2    | Task 21 |
| M3    | Task 4  |
| M4    | Task 16 |
| M5    | Task 15 |
| M6    | Task 18 |
| M7    | Task 22 |
| M8    | Task 7  |
| M9    | Task 1  |
| M10   | Task 17 |
| M11   | Task 14 |
| L1    | Task 6  |
| L2    | Task 19 |
| L3    | Task 20 |
| L4    | Task 22 |
| L5    | Task 2  |
| L6    | Task 4  |
| L7    | Task 22 |
| L8    | Task 21 |
| L9    | Task 21 |
