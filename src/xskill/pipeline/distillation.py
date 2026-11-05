"""Explicit-database control and audit records for one-target native runs.

Configuration only reads skill content. Runtime creation is explicit and leaves
an ownership record before repository initialization, so a crash fails closed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from xskill._sqlite_connect import connect_with_lock
from xskill.pipeline.registry import get_connection
from xskill.tasks.locking import root_host_lock, root_run_lock


def _pid_alive_strict(pid: int) -> bool | None:
    """Return unknown rather than treating a failed probe as process exit."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False if ctypes.get_last_error() == 87 else None
    try:
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        return exit_code.value == 259
    finally:
        kernel.CloseHandle(handle)


def _legacy_writer_inventory(state_root: Path, skill_root: Path) -> list[int]:
    """Bounded read-only legacy process check; never expose command lines."""
    import subprocess
    from xskill.utils.proc import windowless_subprocess_kwargs
    if os.name == "nt":
        script = ("Get-CimInstance Win32_Process -ErrorAction Stop | "
                  "Where-Object {$_.Name -match '^(python|pythonw|xskill)(\\.exe)?$'} | "
                  "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, timeout=8, check=True,
                                **windowless_subprocess_kwargs())
        values = json.loads(result.stdout or "[]")
        values = [values] if isinstance(values, dict) else values
        processes = [(int(row["ProcessId"]), row.get("CommandLine")) for row in values]
    else:
        result = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True,
                                text=True, timeout=5, check=True)
        processes = [(int(parts[0]), parts[1]) for line in result.stdout.splitlines()
                     if len(parts := line.strip().split(None, 1)) == 2]
    found = []
    default_state = root_key_for(state_root) == root_key_for(Path.home() / ".xskill")
    for pid, command in processes:
        if pid == os.getpid():
            continue
        if command is None:
            raise RuntimeError("cannot inspect a potential legacy Python writer")
        lowered = command.lower().replace("\\", "/")
        is_writer = ("xskill._workers" in lowered and any(kind in lowered for kind in ("agent-worker", "kernel-host", "ecosystem-ingest")))
        is_host = bool(re.search(r"(?:xskill(?:\.exe)?|xskill/__main__\.py)\s+serve(?:\s|$)", lowered))
        scoped = any(str(path).lower().replace("\\", "/") in lowered for path in (state_root, skill_root))
        if (is_writer or is_host) and (default_state or scoped):
            found.append(pid)
    return found


def root_key_for(path: Path) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def source_key_for(path: Path) -> str:
    path = Path(path).expanduser().resolve()
    try:
        info = path.stat()
    except FileNotFoundError:
        return root_key_for(path)
    return f"{root_key_for(path)}#{info.st_dev}:{info.st_ino}"


def _source_key(value: str) -> str:
    value = str(value)
    # Keep opaque caller keys valid, while path-shaped keys use the same
    # canonical directory identity as the scanner and tool callbacks.
    if value and Path(value).is_dir():
        return source_key_for(Path(value))
    return value


def atom_fingerprint(atom) -> str:
    """Hash semantic evidence, excluding mutable scoring and process flags."""
    values = atom if isinstance(atom, dict) else vars(atom)
    fields = ("atom_id", "title", "content", "summary", "description", "task", "result",
              "traj_id", "start", "end", "start_idx", "end_idx", "turn_start", "turn_end",
              "start_turn", "end_turn", "messages", "source_span", "ranges", "intent",
              "offset_start", "offset_end", "raw_segment", "context_prefix", "tags")
    stable = {key: values[key] for key in fields if key in values}
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True,
                                     default=str, separators=(",", ":")).encode()).hexdigest()


def _atom_id(atom) -> str:
    return str(atom.get("atom_id", "") if isinstance(atom, dict) else atom.atom_id)


def _identity(path: Path) -> str:
    stat = path.stat()
    return json.dumps({"dev": stat.st_dev, "ino": stat.st_ino}, sort_keys=True)


def _validate_name(name: str, *, create: bool) -> None:
    if (not isinstance(name, str) or not name or name in {".", "..", "references"}
            or name.startswith(".") or name.rstrip(" .") != name
            or any(c in name for c in '/\\:')):
        raise ValueError("invalid target skill name")
    if create and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise ValueError("new skill name must be a lowercase hyphenated slug")
    if name.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise ValueError("reserved target skill name")


def _validate_path_prefix(path: Path) -> None:
    for entry in (path, *path.parents):
        if entry.is_symlink() or (hasattr(entry, "is_junction") and entry.is_junction()):
            raise ValueError(f"linked target path is not supported: {entry}")
        if entry.is_file() and entry.stat().st_nlink > 1:
            raise ValueError(f"hard linked target file is not supported: {entry}")


def _validate_paths(path: Path) -> None:
    _validate_path_prefix(path)
    for entry in path.rglob("*"):
        if entry.is_symlink() or (hasattr(entry, "is_junction") and entry.is_junction()):
            raise ValueError(f"linked target path is not supported: {entry}")
        if entry.is_file() and entry.stat().st_nlink > 1:
            raise ValueError(f"hard linked target file is not supported: {entry}")


def _read_target(path: Path, name: str, *, clean: bool) -> tuple[str, str, str]:
    from dulwich import porcelain
    from dulwich.repo import Repo
    from xskill.skill.frontmatter import parse_strict
    if not path.is_dir():
        raise ValueError("update target does not exist")
    _validate_paths(path)
    if not (path / ".git").is_dir():
        raise ValueError("target requires an independent Git repository")
    if ((path / ".git" / "objects" / "info" / "alternates").exists()
            or (path / ".git" / "objects" / "info" / "http-alternates").exists()
            or (path / ".git" / "commondir").exists()):
        raise ValueError("external Git object references are not supported")
    try:
        frontmatter, _ = parse_strict((path / "SKILL.md").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("target requires a valid readable SKILL.md") from exc
    if frontmatter.get("name") != name or not str(frontmatter.get("description", "")).strip():
        raise ValueError("target frontmatter identity or description is invalid")
    try:
        with Repo(str(path)) as repo:
            if b"refs/heads/main" in repo.refs:
                sha = repo.refs[b"refs/heads/main"].decode()
            elif b"refs/heads/baby" in repo.refs:
                sha = repo.refs[b"refs/heads/baby"].decode()
            else:
                raise ValueError("target requires a readable main or baby baseline")
            if clean:
                status = porcelain.status(repo)
                ignored = {".candidates.yml", ".distillation-run.json", ".ux_scores.jsonl"}
                changed = [p for group in status.staged.values() for p in group]
                changed.extend(status.unstaged)
                changed.extend(status.untracked)
                if any((p.decode() if isinstance(p, bytes) else p) not in ignored for p in changed):
                    raise ValueError("target contains uncommitted content changes")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("target Git repository cannot be read") from exc
    return str(frontmatter["description"]), sha, _identity(path)


class DistillationStore:
    def __init__(self, db_path: Path, skill_dir: Path):
        if db_path is None or skill_dir is None:
            raise ValueError("explicit database and skill root are required")
        self.db_path = Path(db_path).expanduser().resolve()
        self.skill_dir = Path(skill_dir).expanduser().resolve()
        self.root_key = root_key_for(self.skill_dir)

    @contextmanager
    def _connect(self):
        conn = get_connection(self.db_path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _control(self, conn):
        return conn.execute("SELECT * FROM distillation_control WHERE root_key=?", (self.root_key,)).fetchone()

    def run(self) -> dict | None:
        with self._connect() as conn:
            control = self._control(conn)
            if control is None or control["active_run_id"] is None:
                return None
            row = conn.execute("SELECT * FROM distillation_runs WHERE run_id=? AND root_key=?",
                               (control["active_run_id"], self.root_key)).fetchone()
            if row is None or row["mode"] not in {"create_one", "update_only"}:
                raise RuntimeError("invalid active distillation control; refusing automatic mode")
            return dict(row)

    def policy(self):
        from xskill.skill.write_policy import DistillationPolicy
        with self._connect() as conn:
            control = self._control(conn)
            generation = control["generation"] if control else 0
            run = None
            if control and control["active_run_id"]:
                run = conn.execute("SELECT * FROM distillation_runs WHERE run_id=? AND root_key=?",
                                   (control["active_run_id"], self.root_key)).fetchone()
                if run is None or run["mode"] not in {"create_one", "update_only"}:
                    raise RuntimeError("invalid active distillation control; refusing automatic mode")
            return DistillationPolicy(mode=run["mode"] if run else "auto",
                                      run_id=run["run_id"] if run else None,
                                      root_key=self.root_key,
                                      target_skill=run["target_skill"] if run else None,
                                      generation=generation)

    def configure(self, mode=None, skill=None, description=None, resume=None):
        if resume and any(value is not None for value in (mode, skill, description)):
            raise ValueError("--resume is mutually exclusive with mode, skill and description")
        if not resume and mode not in {"auto", "create_one", "update_only"}:
            raise ValueError("mode is required: auto, create_one or update_only")
        with root_host_lock(self.db_path.parent, self.skill_dir), root_run_lock(self.db_path.parent, self.skill_dir):
            # The web host may have no current child writer while its supervisor
            # restarts one. Reject configuration during that gap as well.
            self._reject_live_host()
            current = self.policy()
            if resume:
                with self._connect() as conn:
                    row = conn.execute("SELECT * FROM distillation_runs WHERE run_id=? AND root_key=?",
                                       (resume, self.root_key)).fetchone()
                if row is None:
                    raise ValueError("run does not exist in this skill library")
                run = dict(row)
                self._validate_run_target(run, resume=True)
            elif mode == "auto":
                if skill is not None or description is not None:
                    raise ValueError("auto does not accept skill or description")
                run = None
            else:
                _validate_name(skill or "", create=mode == "create_one")
                target = self.skill_dir / skill
                identity = sha = ""
                if mode == "create_one":
                    if not description or not description.strip():
                        raise ValueError("create_one requires a nonempty description")
                    _validate_paths(target)
                    if target.exists():
                        raise ValueError("create target already exists")
                else:
                    if description is not None:
                        raise ValueError("update_only does not accept description")
                    description, sha, identity = _read_target(target, skill, clean=True)
                run = dict(run_id=uuid.uuid4().hex, root_key=self.root_key, mode=mode,
                           target_skill=skill, description_snapshot=description,
                           target_identity=identity, base_sha=sha, owns_target=0)
            self._ensure_locator()
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                control = self._control(conn)
                if (control["generation"] if control else 0) != current.generation:
                    raise RuntimeError("distillation configuration changed concurrently")
                if control and control["active_run_id"] and control["active_run_id"] != (run or {}).get("run_id"):
                    conn.execute("UPDATE distillation_runs SET state='closed',updated_at=datetime('now') WHERE run_id=?",
                                 (control["active_run_id"],))
                if run and not resume:
                    names = tuple(run)
                    conn.execute(f"INSERT INTO distillation_runs ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                                 tuple(run.values()))
                if run and resume:
                    conn.execute("UPDATE distillation_runs SET state='interrupted',error='',updated_at=datetime('now') WHERE run_id=?", (run["run_id"],))
                conn.execute("INSERT INTO distillation_control(root_key,active_run_id,generation) VALUES(?,?,?) "
                             "ON CONFLICT(root_key) DO UPDATE SET active_run_id=excluded.active_run_id,generation=excluded.generation",
                             (self.root_key, run["run_id"] if run else None, current.generation + 1))
            return self.run()

    def _ensure_locator(self):
        """Publish only the explicit DB locator before activating its control.

        This allows independent SDK/CLI processes to discover the same authority
        without guessing a home path. The database remains the source of state.
        """
        marker = self.skill_dir / ".distillation-control.json"
        _validate_path_prefix(marker)
        expected = {"root_key": self.root_key, "db_path": str(self.db_path)}
        if marker.exists():
            try:
                current = json.loads(marker.read_text(encoding="utf-8"))
            except (ValueError, OSError) as exc:
                raise ValueError("cannot verify skill library database locator") from exc
            if current != expected:
                raise ValueError("skill library is already bound to a different explicit registry database")
            return
        self.skill_dir.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_name(f".{marker.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(expected), encoding="utf-8")
            temporary.replace(marker)
        finally:
            temporary.unlink(missing_ok=True)

    def _reject_live_host(self):
        state_root = self.db_path.parent
        unknown_metadata = False
        for name in ("serve_runtime.json", "agent_worker_status.json", "watcher_status.json", "kernel_host_status.json", "ecosystem_ingest_status.json"):
            status = state_root / name
            if not status.exists():
                continue
            try:
                data = json.loads(status.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("invalid writer lifecycle metadata")
                if data.get("active") is False:
                    continue
                stats = data.get("stats") or {}
                if not isinstance(stats, dict):
                    raise ValueError("invalid writer lifecycle metadata")
                pid = int(data.get("pid") or stats.get("pid") or 0)
                if not pid:
                    unknown_metadata = True
                    continue
                if pid < 0:
                    raise ValueError("invalid writer process identity")
                alive = _pid_alive_strict(pid)
                if alive is None:
                    raise RuntimeError("cannot confirm whether an existing writer has stopped")
                if alive:
                    raise RuntimeError("web host or writer is still running; stop it before configuring distillation")
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("cannot confirm whether the web host or writer has stopped") from exc
        # Isolated explicit homes have no relation to global user processes.
        # The default real runtime and legacy metadata require an inventory.
        if unknown_metadata or root_key_for(state_root) == root_key_for(Path.home() / ".xskill"):
            try:
                if _legacy_writer_inventory(state_root, self.skill_dir):
                    raise RuntimeError("legacy xskill host or writer is still running")
            except RuntimeError:
                raise
            except Exception as exc:
                raise RuntimeError("cannot confirm legacy writer processes have stopped") from exc

    def _validate_run_target(self, run, *, resume=False):
        path = self.skill_dir / run["target_skill"]
        _validate_name(run["target_skill"], create=run["mode"] == "create_one")
        _validate_paths(path)
        if run["mode"] == "create_one" and not run["owns_target"]:
            if path.exists():
                raise ValueError("create target exists without provable run ownership")
            return
        if not path.is_dir() or _identity(path) != run["target_identity"]:
            raise ValueError("target directory identity changed or target is missing")
        if run["mode"] == "create_one":
            try:
                marker = json.loads((path / ".distillation-run.json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError("cannot prove create target ownership") from exc
            if marker != {"run_id": run["run_id"], "root_key": self.root_key}:
                raise ValueError("create target ownership changed")
        # A previous run ID alone does not prove the origin of uncommitted
        # update content. Only clean committed checkpoints can resume until a
        # more specific recovery marker establishes ownership of dirty files.
        _read_target(path, run["target_skill"], clean=run["mode"] == "update_only" or not resume)

    def ensure_target(self):
        from xskill.skill.git import init_skill_repo_on_baby
        from xskill.skill.write_policy import library_write_guard, require_skill_write
        run = self.run()
        if run is None:
            return None
        self._validate_run_target(run, resume=run["state"] != "prepared")
        path = self.skill_dir / run["target_skill"]
        if run["mode"] == "create_one" and not run["owns_target"]:
            with library_write_guard(self.skill_dir, self.policy(), db_path=self.db_path):
                require_skill_write(path, operation="create")
                path.mkdir(parents=True, exist_ok=False)
                (path / ".distillation-run.json").write_text(json.dumps({"run_id": run["run_id"], "root_key": self.root_key}), encoding="utf-8")
                with self._connect() as conn:
                    conn.execute("UPDATE distillation_runs SET owns_target=1,target_identity=? WHERE run_id=?", (_identity(path), run["run_id"]))
                init_skill_repo_on_baby(str(path), run["target_skill"], run["description_snapshot"])
        self.reconcile_checkpoints()
        return path

    def reconcile_checkpoints(self, *, limit=200):
        """Recover durable batch receipts from reachable target commit objects.

        Commit trailers are framework generated. A trailer lacking exact audit
        identity is ignored, and ambiguous atom identities stop recovery rather
        than acknowledging a different source's material.
        """
        from dulwich.repo import Repo
        run = self.run()
        if not run:
            return 0
        target = self.skill_dir / run["target_skill"]
        if not (target / ".git").is_dir():
            return 0
        bound = min(int(limit), 200)
        if bound < 1:
            raise ValueError("checkpoint recovery limit must be positive")
        receipts = []
        examined = set()
        parents = set()
        with Repo(str(target)) as repo:
            refs = [sha for name, sha in repo.get_refs().items() if name == b"HEAD" or name.startswith(b"refs/heads/")]
            for entry in repo.get_walker(include=list(set(refs)), max_entries=bound):
                commit = entry.commit
                examined.add(commit.id)
                parents.update(commit.parents)
                lines = commit.message.decode("utf-8", "replace").splitlines()
                if f"XSkill-Run: {run['run_id']}" not in lines:
                    continue
                for line in lines:
                    if not line.startswith("XSkill-Atoms: "):
                        continue
                    try:
                        atom_ids = json.loads(line[len("XSkill-Atoms: "):])
                    except ValueError as exc:
                        raise RuntimeError("invalid checkpoint receipt in target repository") from exc
                    if not isinstance(atom_ids, list) or not all(isinstance(value, str) and value for value in atom_ids):
                        raise RuntimeError("invalid checkpoint atom identities")
                    receipts.append((commit.id.decode(), atom_ids))
        truncated = bool((parents | set(refs)) - examined)
        changed = 0
        # Files and commit traversal are complete before the short SQL txn.
        with self._connect() as conn:
            for sha, atom_ids in receipts:
                for atom_id in atom_ids:
                    rows = conn.execute("SELECT source_key,content_sha256,state FROM distillation_atoms WHERE run_id=? AND atom_id=? AND state IN ('pending','accepted','failed')",
                                        (run["run_id"], atom_id)).fetchall()
                    if len(rows) > 1:
                        raise RuntimeError("ambiguous source/content identity in checkpoint recovery")
                    if rows:
                        changed += conn.execute("UPDATE distillation_atoms SET state='committed',candidate_applied=1,checkpoint_sha=?,updated_at=datetime('now') WHERE run_id=? AND atom_id=? AND source_key=? AND content_sha256=?",
                                                (sha, run["run_id"], atom_id, rows[0]["source_key"], rows[0]["content_sha256"])).rowcount
            if truncated:
                unresolved = conn.execute("SELECT 1 FROM distillation_atoms WHERE run_id=? AND state IN ('pending','failed') LIMIT 1", (run["run_id"],)).fetchone()
                if unresolved:
                    raise RuntimeError("bounded checkpoint recovery was truncated with unproven pending material; refusing automatic reapplication")
        return changed

    def set_state(self, state, error=""):
        if state not in {"prepared", "running", "stopped", "interrupted", "failed", "closed"}:
            raise ValueError("invalid run state")
        run = self.run()
        if run:
            with self._connect() as conn:
                conn.execute("UPDATE distillation_runs SET state=?,error=?,updated_at=datetime('now') WHERE run_id=?", (state, error, run["run_id"]))

    def set_artifact(self, state, branch="", sha="", error=""):
        run = self.run()
        if run:
            with self._connect() as conn:
                conn.execute("UPDATE distillation_runs SET artifact_state=?,artifact_branch=?,artifact_sha=?,error=?,updated_at=datetime('now') WHERE run_id=?",
                             (state, branch, sha, error, run["run_id"]))

    def atom_record(self, atom, source_key=""):
        run = self.run()
        if not run:
            return None
        source_key = _source_key(source_key)
        fingerprint = atom_fingerprint(atom)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM distillation_atoms WHERE run_id=? AND source_key=? AND atom_id=? AND content_sha256=?",
                               (run["run_id"], source_key, _atom_id(atom), fingerprint)).fetchone()
            return dict(row) if row else None

    def processed(self, atom, source_key=""):
        record = self.atom_record(atom, source_key)
        return bool(record and record["state"] in {"accepted", "skipped", "committed"})

    def record_atom(self, atom, state, source_key="", reason="", weightscore=0, checkpoint_sha=None):
        run = self.run()
        if not run:
            raise RuntimeError("atom audit requires an active restricted run")
        state = "failed" if state == "error" else state
        source_key = _source_key(source_key)
        fingerprint = atom_fingerprint(atom)
        with self._connect() as conn:
            if state in {"pending", "accepted"}:
                conn.execute("UPDATE distillation_atoms SET state='superseded',updated_at=datetime('now') WHERE run_id=? AND source_key=? AND atom_id=? AND content_sha256<>? AND state IN ('pending','accepted','failed')",
                             (run["run_id"], source_key, _atom_id(atom), fingerprint))
            conn.execute("INSERT INTO distillation_atoms(run_id,source_key,atom_id,content_sha256,state,reason,weightscore,candidate_applied,checkpoint_sha,retry_count) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,source_key,atom_id,content_sha256) DO UPDATE SET "
                         "state=excluded.state,reason=excluded.reason,weightscore=excluded.weightscore,candidate_applied=excluded.candidate_applied,"
                         "checkpoint_sha=COALESCE(excluded.checkpoint_sha,distillation_atoms.checkpoint_sha),retry_count=distillation_atoms.retry_count+CASE WHEN excluded.state='failed' THEN 1 ELSE 0 END,updated_at=datetime('now')",
                         (run["run_id"], source_key, _atom_id(atom), fingerprint, state, reason, weightscore,
                          int(state in {"accepted", "committed"}), checkpoint_sha, int(state == "failed")))

    def checkpoint_atoms(self, atom_ids, sha, source_key=None):
        run = self.run()
        if run:
            if source_key is not None:
                source_key = _source_key(source_key)
            atom_ids = list(atom_ids)
            with self._connect() as conn:
                if source_key is None:
                    for atom_id in atom_ids:
                        count = conn.execute("SELECT count(*) FROM distillation_atoms WHERE run_id=? AND atom_id=? AND state IN ('pending','accepted')", (run["run_id"], atom_id)).fetchone()[0]
                        if count > 1:
                            raise RuntimeError("ambiguous source/content identity in checkpoint acknowledgement")
                    conn.executemany("UPDATE distillation_atoms SET checkpoint_sha=?,state='committed',candidate_applied=1,updated_at=datetime('now') WHERE run_id=? AND atom_id=? AND state IN ('pending','accepted')",
                                     [(sha, run["run_id"], atom_id) for atom_id in atom_ids])
                else:
                    for atom_id in atom_ids:
                        count = conn.execute("SELECT count(*) FROM distillation_atoms WHERE run_id=? AND source_key=? AND atom_id=? AND state IN ('pending','accepted')", (run["run_id"], source_key, atom_id)).fetchone()[0]
                        if count > 1:
                            raise RuntimeError("ambiguous content identity in checkpoint acknowledgement")
                    conn.executemany("UPDATE distillation_atoms SET checkpoint_sha=?,state='committed',candidate_applied=1,updated_at=datetime('now') WHERE run_id=? AND source_key=? AND atom_id=? AND state IN ('pending','accepted')",
                                     [(sha, run["run_id"], source_key, atom_id) for atom_id in atom_ids])

    def status(self):
        result = {"mode": "auto", "generation": 0, "root_key": self.root_key,
                  "run": None, "accepted": 0, "skipped": 0, "failed": 0}
        if not self.db_path.exists():
            return result
        # Status never creates a DB, runs migrations, scans content, or hashes
        # history. A pre-feature registry without control tables means auto.
        with closing(connect_with_lock(sqlite3.connect, self.db_path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='distillation_control'").fetchone() is None:
                return result
            control = self._control(conn)
            if control:
                result["generation"] = control["generation"]
            if control and control["active_run_id"]:
                row = conn.execute("SELECT * FROM distillation_runs WHERE run_id=? AND root_key=?", (control["active_run_id"], self.root_key)).fetchone()
                if row is None or row["mode"] not in {"create_one", "update_only"}:
                    raise RuntimeError("invalid active distillation control")
                run = dict(row)
                result.update(mode=run["mode"], run=run)
                counts = dict(conn.execute("SELECT state,count(*) FROM distillation_atoms WHERE run_id=? GROUP BY state", (run["run_id"],)).fetchall())
                result.update(accepted=counts.get("accepted", 0) + counts.get("committed", 0), skipped=counts.get("skipped", 0), failed=counts.get("failed", 0))
        return result
