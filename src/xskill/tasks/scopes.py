"""Resolve stable Tenant, Task and Source scopes from Registry evidence."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from xskill.tasks.locking import task_file_lock


@dataclass(frozen=True)
class ScopeIdentity:
    tenant_id: str
    task_scope_id: str
    source_scope_id: str
    actor_id: str
    workspace_id: str


def _opaque(prefix: str, value: str, length: int = 24) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def _read_sidecar(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid trajectory metadata: {path}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"trajectory metadata must contain an object: {path}")
    return value


class ScopeResolver:
    """Map mutable paths and labels to persisted opaque scope identifiers."""

    IDENTITY_SCHEMA_VERSION = 1

    def __init__(self, state_root: Path, *, db_path: Path | None = None,
                 server_mode: bool = False):
        self.state_root = Path(state_root).expanduser().resolve()
        self.db_path = Path(db_path) if db_path is not None else None
        self.server_mode = bool(server_mode)
        self.graph_root = self.state_root / "task_graph"
        self._tenant_id: str | None = None

    def _read_tenant_identity(self) -> str | None:
        identity_path = self.graph_root / "identity.json"
        if not identity_path.is_file():
            return None
        try:
            payload = json.loads(identity_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"invalid Task Graph identity: {identity_path}"
            ) from error
        tenant_id = payload.get("tenant_id")
        if (
            payload.get("schema_version") != self.IDENTITY_SCHEMA_VERSION
            or not isinstance(tenant_id, str)
            or not tenant_id
        ):
            raise RuntimeError(f"invalid Task Graph identity: {identity_path}")
        return tenant_id

    @property
    def existing_tenant_id(self) -> str | None:
        """Read the tenant boundary without creating state on query paths."""
        if self._tenant_id is not None:
            return self._tenant_id
        tenant_id = self._read_tenant_identity()
        if tenant_id is not None:
            self._tenant_id = tenant_id
        return tenant_id

    @property
    def tenant_id(self) -> str:
        if self._tenant_id is not None:
            return self._tenant_id
        identity_path = self.graph_root / "identity.json"
        lock_path = self.graph_root / "locks" / "identity.lock"
        with task_file_lock(lock_path):
            tenant_id = self._read_tenant_identity()
            if tenant_id is not None:
                self._tenant_id = tenant_id
                return tenant_id
            identity_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": self.IDENTITY_SCHEMA_VERSION,
                "instance_id": f"ins_{uuid.uuid4().hex}",
                "tenant_id": f"ten_{uuid.uuid4().hex}",
                "mode": "team_server" if self.server_mode else "standalone",
            }
            temporary = identity_path.with_name(f".{identity_path.name}.{uuid.uuid4().hex}.tmp")
            encoded = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")
            with temporary.open("wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            if os.name != "nt":
                temporary.chmod(0o600)
            os.replace(temporary, identity_path)
            if os.name != "nt":
                directory_fd = os.open(identity_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            self._tenant_id = payload["tenant_id"]
            return payload["tenant_id"]

    def resolve(self, *, watch_dir: dict, trajectory: dict) -> ScopeIdentity:
        source_scope_id = str(watch_dir.get("source_scope_id") or "").strip()
        if not source_scope_id:
            from xskill.pipeline.registry import ensure_watch_dir_source_scope

            source_scope_id = ensure_watch_dir_source_scope(
                int(watch_dir["id"]), db_path=self.db_path,
            )
        user_key = str(trajectory.get("user_key") or "").strip()
        if self.server_mode:
            # Team actors must never be merged merely because they ingest into
            # the same server-side watch directory.  Anonymous uploads stay
            # isolated by SourceScope because no stronger actor evidence exists.
            actor_seed = (
                f"team:{user_key}" if user_key else f"source:{source_scope_id}"
            )
        else:
            # One standalone xskill instance represents one local actor.  Using
            # SourceScope here would prevent an explicitly identical workspace
            # observed through Codex and another Harness from ever sharing a
            # Logical Task.
            actor_seed = f"standalone:{self.tenant_id}"
        actor_id = _opaque("act", actor_seed)
        filename = str(trajectory.get("filename") or "")
        traj_id = filename.removesuffix(".md")
        sidecar = _read_sidecar(Path(watch_dir["path"]) / f"{traj_id}.json")
        workspace_seed = ""
        for key in ("workspace_id", "repo_id", "cwd", "workspace_dir", "workspace", "repo"):
            candidate = sidecar.get(key)
            if isinstance(candidate, str) and candidate.strip():
                workspace_seed = candidate.strip()
                break
        if workspace_seed:
            workspace_id = _opaque("wrk", workspace_seed)
        else:
            workspace_id = _opaque("wrk", f"source:{source_scope_id}")
        tenant_id = self.tenant_id
        task_scope_id = _opaque(
            "tsc", f"{tenant_id}\x1f{actor_id}\x1f{workspace_id}", length=32,
        )
        return ScopeIdentity(
            tenant_id=tenant_id,
            task_scope_id=task_scope_id,
            source_scope_id=source_scope_id,
            actor_id=actor_id,
            workspace_id=workspace_id,
        )

    def scope_dir(self, task_scope_id: str) -> Path:
        if (
            not isinstance(task_scope_id, str)
            or len(task_scope_id) != 36
            or not task_scope_id.startswith("tsc_")
            or any(character not in "0123456789abcdef" for character in task_scope_id[4:])
        ):
            raise ValueError("invalid task_scope_id")
        return self.graph_root / "scopes" / task_scope_id
