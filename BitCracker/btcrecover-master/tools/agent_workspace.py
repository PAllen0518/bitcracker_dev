"""Local coordination workspace for Claude and Codex."""

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
TASK_STATES = (
    "open",
    "claimed",
    "in_progress",
    "review",
    "blocked",
    "done",
    "cancelled",
)
ALLOWED_TASK_TRANSITIONS = {
    "open": {"claimed", "cancelled"},
    "claimed": {"open", "in_progress", "blocked", "cancelled"},
    "in_progress": {"review", "blocked", "cancelled"},
    "review": {"in_progress", "done", "blocked"},
    "blocked": {"open", "in_progress", "cancelled"},
    "done": set(),
    "cancelled": set(),
}
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SENSITIVE_TEXT_PATTERNS = (
    re.compile(
        r"\b(?:recovered[ _-]*)?password\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\bcandidate(?:\s+strings?|\s+list)\s*:", re.IGNORECASE),
    re.compile(r"\bwallet\s+bytes?\s*:", re.IGNORECASE),
)
MAX_SHARED_TEXT_BYTES = 65_536
MAX_MANIFEST_ARTIFACTS = 100
MAX_UINT64 = (1 << 64) - 1
MIN_PROCESS_EXIT_CODE = -(1 << 31)
MAX_PROCESS_EXIT_CODE = (1 << 31) - 1
BACKUP_FORMAT = "bitcracker-agent-workspace-backup-v1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_versions (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    )
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL CHECK (length(trim(label)) BETWEEN 1 AND 4096),
    state TEXT NOT NULL DEFAULT 'planned' CHECK (
        state IN (
            'planned', 'running', 'incomplete', 'complete_not_found',
            'recovered', 'failed', 'unknown'
        )
    ),
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    updated_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    )
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    label TEXT NOT NULL CHECK (length(trim(label)) BETWEEN 1 AND 4096),
    kind TEXT NOT NULL CHECK (length(trim(kind)) BETWEEN 1 AND 128),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL CHECK (
        length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    )
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 4096),
    state TEXT NOT NULL DEFAULT 'open' CHECK (
        state IN (
            'open', 'claimed', 'in_progress', 'review', 'blocked',
            'done', 'cancelled'
        )
    ),
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    updated_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    )
);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    agent TEXT NOT NULL CHECK (length(trim(agent)) BETWEEN 1 AND 128),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    claimed_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    released_at TEXT,
    CHECK (
        (active = 1 AND released_at IS NULL)
        OR (active = 0 AND released_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS claims_one_active_per_task
ON claims(task_id) WHERE active = 1;

CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    task_id TEXT REFERENCES tasks(id) ON DELETE RESTRICT,
    author TEXT NOT NULL CHECK (length(trim(author)) BETWEEN 1 AND 128),
    severity TEXT NOT NULL CHECK (
        severity IN ('low', 'medium', 'high', 'critical')
    ),
    summary TEXT NOT NULL CHECK (length(trim(summary)) BETWEEN 1 AND 65536),
    state TEXT NOT NULL DEFAULT 'open' CHECK (
        state IN ('open', 'accepted', 'rejected', 'resolved')
    ),
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    )
);

CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    reviewer TEXT NOT NULL CHECK (length(trim(reviewer)) BETWEEN 1 AND 128),
    decision TEXT NOT NULL CHECK (
        decision IN ('pending', 'approved', 'changes_requested')
    ),
    summary TEXT NOT NULL CHECK (length(summary) <= 65536),
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    )
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    gate INTEGER NOT NULL CHECK (gate BETWEEN 1 AND 7),
    status TEXT NOT NULL DEFAULT 'requested' CHECK (
        status IN ('requested', 'approved', 'denied', 'revoked')
    ),
    requested_by TEXT NOT NULL CHECK (
        length(trim(requested_by)) BETWEEN 1 AND 128
    ),
    decided_by TEXT,
    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    UNIQUE (run_id, gate)
);

CREATE TABLE IF NOT EXISTS handoffs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    task_id TEXT REFERENCES tasks(id) ON DELETE RESTRICT,
    author TEXT NOT NULL CHECK (length(trim(author)) BETWEEN 1 AND 128),
    recipient TEXT NOT NULL CHECK (
        length(trim(recipient)) BETWEEN 1 AND 128
    ),
    sha256 TEXT NOT NULL CHECK (
        length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    acknowledged_at TEXT,
    supersedes_handoff_id TEXT REFERENCES handoffs(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    )
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL CHECK (
        entity_type IN (
            'run', 'artifact', 'task', 'claim', 'finding', 'review',
            'approval', 'handoff'
        )
    ),
    entity_id TEXT NOT NULL CHECK (length(trim(entity_id)) BETWEEN 1 AND 128),
    event_type TEXT NOT NULL CHECK (
        length(trim(event_type)) BETWEEN 1 AND 128
    ),
    actor TEXT NOT NULL CHECK (length(trim(actor)) BETWEEN 1 AND 128),
    detail TEXT NOT NULL DEFAULT '' CHECK (length(detail) <= 65536),
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    )
);

CREATE INDEX IF NOT EXISTS artifacts_run_id ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS tasks_run_id ON tasks(run_id);
CREATE INDEX IF NOT EXISTS tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS claims_task_id ON claims(task_id);
CREATE INDEX IF NOT EXISTS findings_run_id ON findings(run_id);
CREATE INDEX IF NOT EXISTS findings_task_id ON findings(task_id);
CREATE INDEX IF NOT EXISTS reviews_task_id ON reviews(task_id);
CREATE INDEX IF NOT EXISTS approvals_run_id ON approvals(run_id);
CREATE INDEX IF NOT EXISTS handoffs_run_id ON handoffs(run_id);
CREATE INDEX IF NOT EXISTS handoffs_task_id ON handoffs(task_id);
CREATE INDEX IF NOT EXISTS events_entity ON events(entity_type, entity_id);

INSERT OR IGNORE INTO schema_versions (version) VALUES (1);
"""


class WorkspaceError(Exception):
    """Base exception for workspace operations."""


class ConflictError(WorkspaceError):
    """Raised when an optimistic or exclusive operation loses a race."""


class InvalidTransitionError(WorkspaceError):
    """Raised when a requested state transition is not allowed."""


class SensitiveContentError(WorkspaceError):
    """Raised before sensitive content can enter shared workspace state."""


@dataclass(frozen=True)
class Task:
    """A bounded task record returned by the workspace API."""

    task_id: str
    run_id: str
    title: str
    state: str
    version: int


@dataclass(frozen=True)
class Artifact:
    """Metadata-only description of an external artifact."""

    artifact_id: str
    run_id: str
    label: str
    kind: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class BackupResult:
    """Metadata for one verified SQLite backup and its manifest."""

    backup_path: Path
    manifest_path: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class Handoff:
    """Immutable metadata for one published runtime handoff."""

    handoff_id: str
    run_id: str
    task_id: str | None
    author: str
    recipient: str
    sha256: str


@dataclass(frozen=True)
class HandoffAcknowledgment:
    """Exact-hash acknowledgment for one immutable handoff."""

    handoff_id: str
    recipient: str
    sha256: str
    acknowledged_at: str


@dataclass(frozen=True)
class RunClassificationEvidence:
    """Synthetic-safe observations used to classify one recovery run."""

    process_running: bool
    process_exit_code: int | None
    checkpoint_combo_idx: int
    checkpoint_total_combos: int
    log_has_completion_marker: bool
    log_has_not_found_marker: bool
    recovered_output_present: bool
    expected_artifact_hashes: Mapping[str, str]


class Workspace:
    """Coordinate tasks through one local SQLite database."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        handoff_directory: str | Path | None = None,
        sensitive_paths: Iterable[str | Path] = (),
    ) -> None:
        """Bind this client to one workspace database path."""
        self.database_path = Path(database_path)
        if handoff_directory is None:
            handoff_directory = self.database_path.parent / "handoffs"
        self.handoff_directory = Path(handoff_directory)
        self.sensitive_paths = tuple(Path(path) for path in sensitive_paths)
        self._sensitive_path_markers = tuple(
            _normalize_path(path.resolve(strict=False))
            for path in self.sensitive_paths
        )

    def initialize(self) -> None:
        """Create or validate the constrained workspace schema."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                f"BEGIN IMMEDIATE;\n{SCHEMA_SQL}\nCOMMIT;"
            )
        except Exception as error:
            connection.rollback()
            message = "workspace schema initialization failed"
            raise WorkspaceError(message) from error
        finally:
            connection.close()

    def create_backup(self, backup_path: str | Path) -> BackupResult:
        """Create and verify one SQLite API backup plus manifest."""
        target_path = self._validated_backup_path(backup_path)
        manifest_path = _backup_manifest_path(target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        database_temp = _new_temporary_path(
            target_path.parent,
            ".agent-workspace-backup-",
            ".db",
        )
        manifest_temp = _new_temporary_path(
            target_path.parent,
            ".agent-workspace-manifest-",
            ".json",
        )

        source = None
        target = None
        try:
            source = self._connect()
            target = sqlite3.connect(database_temp, timeout=5.0)
            source.backup(target)
            target.commit()
            _require_database_integrity(target)
            target.close()
            target = None
            source.close()
            source = None

            size_bytes = database_temp.stat().st_size
            digest = _sha256_file(database_temp)
            manifest = {
                "format": BACKUP_FORMAT,
                "sha256": digest,
                "size_bytes": size_bytes,
            }
            manifest_temp.write_text(
                json.dumps(manifest, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(database_temp, target_path)
            os.replace(manifest_temp, manifest_path)
            return BackupResult(
                backup_path=target_path,
                manifest_path=manifest_path,
                size_bytes=size_bytes,
                sha256=digest,
            )
        except (OSError, sqlite3.Error) as error:
            raise WorkspaceError("database backup failed") from error
        finally:
            if target is not None:
                target.close()
            if source is not None:
                source.close()
            database_temp.unlink(missing_ok=True)
            manifest_temp.unlink(missing_ok=True)

    def restore_backup(self, backup_path: str | Path) -> None:
        """Verify and atomically restore one workspace database backup."""
        source_path = self._validated_backup_path(backup_path)
        manifest_path = _backup_manifest_path(source_path)
        manifest = _read_backup_manifest(manifest_path)
        restored_temp = _new_temporary_path(
            self.database_path.parent,
            ".agent-workspace-restore-",
            ".db",
        )

        source = None
        target = None
        active = None
        try:
            if not source_path.is_file():
                raise WorkspaceError("backup database does not exist")
            if source_path.stat().st_size != manifest["size_bytes"]:
                raise WorkspaceError("backup size does not match manifest")
            if _sha256_file(source_path) != manifest["sha256"]:
                raise WorkspaceError("backup hash does not match manifest")

            source_uri = source_path.resolve().as_uri() + "?mode=ro"
            source = sqlite3.connect(source_uri, uri=True, timeout=5.0)
            target = sqlite3.connect(restored_temp, timeout=5.0)
            source.backup(target)
            target.commit()
            _require_database_integrity(target)
            target.close()
            target = None
            source.close()
            source = None

            active = self._connect()
            checkpoint = active.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if checkpoint[0] != 0:
                raise WorkspaceError("active database is busy")
            active.execute("BEGIN EXCLUSIVE")
            active.rollback()
            active.close()
            active = None

            os.replace(restored_temp, self.database_path)
        except WorkspaceError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise WorkspaceError("database restore failed") from error
        finally:
            if active is not None:
                active.close()
            if target is not None:
                target.close()
            if source is not None:
                source.close()
            restored_temp.unlink(missing_ok=True)

    def _validated_backup_path(self, value: str | Path) -> Path:
        """Return a non-sensitive backup path distinct from the live DB."""
        if not isinstance(value, (str, os.PathLike)):
            raise TypeError("backup_path must be path-like")
        path = Path(value)
        if not path.name:
            raise ValueError("backup_path must name a file")
        self._validate_shared_text(str(path))
        if path.resolve(strict=False) == self.database_path.resolve(
            strict=False
        ):
            message = "backup_path must differ from the active database"
            raise ValueError(message)
        return path

    def create_run(self, run_id: str, label: str) -> None:
        """Create one coordination run."""
        _validate_identifier(run_id, "run_id")
        self._validate_shared_text(label)
        with self._write_connection() as connection:
            connection.execute(
                "INSERT INTO runs (id, label) VALUES (?, ?)",
                (run_id, label),
            )

    def classify_run(
        self,
        run_id: str,
        evidence: RunClassificationEvidence,
    ) -> str:
        """Classify one run only when its independent evidence agrees."""
        _validate_identifier(run_id, "run_id")
        expected_hashes = _validate_classification_evidence(evidence)

        with self._write_connection() as connection:
            run = connection.execute(
                "SELECT state, version FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise WorkspaceError(f"run {run_id!r} does not exist")

            artifact_rows = connection.execute(
                "SELECT id, sha256 FROM artifacts "
                "WHERE run_id = ? ORDER BY id LIMIT ?",
                (run_id, MAX_MANIFEST_ARTIFACTS + 1),
            ).fetchall()
            hashes_match = (
                len(artifact_rows) <= MAX_MANIFEST_ARTIFACTS
                and {
                    row["id"]: row["sha256"]
                    for row in artifact_rows
                }
                == expected_hashes
            )
            classification = _classify_run_evidence(
                evidence,
                hashes_match=hashes_match,
            )

            timestamp = _utc_now()
            cursor = connection.execute(
                "UPDATE runs "
                "SET state = ?, version = version + 1, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (classification, timestamp, run_id, run["version"]),
            )
            if cursor.rowcount != 1:
                raise ConflictError(
                    f"run {run_id!r} changed during classification"
                )
            self._record_event(
                connection,
                entity_type="run",
                entity_id=run_id,
                event_type="run_classified",
                actor="agent_workspace",
                detail=f"{run['state']} -> {classification}",
            )
            return classification

    def create_task(self, task_id: str, run_id: str, title: str) -> Task:
        """Create one open task for a run."""
        _validate_identifier(task_id, "task_id")
        _validate_identifier(run_id, "run_id")
        self._validate_shared_text(title)
        with self._write_connection() as connection:
            connection.execute(
                "INSERT INTO tasks (id, run_id, title) VALUES (?, ?, ?)",
                (task_id, run_id, title),
            )
            return self._get_task(connection, task_id)

    def claim_task(
        self,
        task_id: str,
        agent: str,
        expected_version: int,
    ) -> Task:
        """Atomically claim one open task using optimistic versioning."""
        _validate_identifier(task_id, "task_id")
        self._validate_shared_text(agent)
        with self._write_connection() as connection:
            task = self._get_task(connection, task_id)
            if task.version != expected_version or task.state != "open":
                raise ConflictError(
                    f"task {task_id!r} is not claimable at version "
                    f"{expected_version}"
                )

            unacknowledged = connection.execute(
                "SELECT 1 FROM handoffs AS current "
                "WHERE current.task_id = ? "
                "AND current.acknowledged_at IS NULL "
                "AND NOT EXISTS ("
                "SELECT 1 FROM handoffs AS newer "
                "WHERE newer.supersedes_handoff_id = current.id"
                ") LIMIT 1",
                (task_id,),
            ).fetchone()
            if unacknowledged is not None:
                raise ConflictError(
                    f"task {task_id!r} has an unacknowledged handoff"
                )

            try:
                connection.execute(
                    "INSERT INTO claims (task_id, agent) VALUES (?, ?)",
                    (task_id, agent),
                )
            except sqlite3.IntegrityError as error:
                raise ConflictError(
                    f"task {task_id!r} already has an active claim"
                ) from error

            timestamp = _utc_now()
            cursor = connection.execute(
                "UPDATE tasks "
                "SET state = 'claimed', version = version + 1, "
                "updated_at = ? "
                "WHERE id = ? AND state = 'open' AND version = ?",
                (timestamp, task_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise ConflictError(f"task {task_id!r} changed while claiming")

            self._record_event(
                connection,
                entity_type="task",
                entity_id=task_id,
                event_type="task_claimed",
                actor=agent,
                detail="open -> claimed",
            )
            return self._get_task(connection, task_id)

    def transition_task(
        self,
        task_id: str,
        new_state: str,
        actor: str,
        expected_version: int,
    ) -> Task:
        """Atomically move a task through an allowed state transition."""
        _validate_identifier(task_id, "task_id")
        self._validate_shared_text(actor)
        with self._write_connection() as connection:
            task = self._get_task(connection, task_id)
            if task.version != expected_version:
                raise ConflictError(
                    f"task {task_id!r} is at version {task.version}, not "
                    f"{expected_version}"
                )

            allowed_states = ALLOWED_TASK_TRANSITIONS[task.state]
            if new_state not in allowed_states:
                raise InvalidTransitionError(
                    f"task transition {task.state!r} -> {new_state!r} "
                    "is not allowed"
                )

            timestamp = _utc_now()
            cursor = connection.execute(
                "UPDATE tasks "
                "SET state = ?, version = version + 1, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (new_state, timestamp, task_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise ConflictError(
                    f"task {task_id!r} changed during transition"
                )

            if new_state in {"open", "done", "cancelled"}:
                connection.execute(
                    "UPDATE claims SET active = 0, released_at = ? "
                    "WHERE task_id = ? AND active = 1",
                    (timestamp, task_id),
                )

            self._record_event(
                connection,
                entity_type="task",
                entity_id=task_id,
                event_type="task_transitioned",
                actor=actor,
                detail=f"{task.state} -> {new_state}",
            )
            return self._get_task(connection, task_id)

    def get_task(self, task_id: str) -> Task:
        """Return one task by identifier."""
        connection = self._connect()
        try:
            return self._get_task(connection, task_id)
        finally:
            connection.close()

    def list_tasks(self, limit: int = 100) -> list[Task]:
        """Return at most ``limit`` tasks in stable identifier order."""
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise TypeError("limit must be an integer")
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")

        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT id, run_id, title, state, version "
                "FROM tasks ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            return [_task_from_row(row) for row in rows]
        finally:
            connection.close()

    def register_artifact(
        self,
        artifact_id: str,
        run_id: str,
        label: str,
        kind: str,
        size_bytes: int,
        sha256: str,
    ) -> Artifact:
        """Register metadata without accepting artifact contents or paths."""
        _validate_identifier(artifact_id, "artifact_id")
        _validate_identifier(run_id, "run_id")
        self._validate_shared_text(label, kind)
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool):
            raise TypeError("size_bytes must be an integer")
        if size_bytes < 0:
            raise ValueError("size_bytes must not be negative")
        if not isinstance(sha256, str) or not SHA256_PATTERN.fullmatch(sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal digits")

        with self._write_connection() as connection:
            connection.execute(
                "INSERT INTO artifacts "
                "(id, run_id, label, kind, size_bytes, sha256) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    artifact_id,
                    run_id,
                    label,
                    kind,
                    size_bytes,
                    sha256,
                ),
            )
        return Artifact(
            artifact_id=artifact_id,
            run_id=run_id,
            label=label,
            kind=kind,
            size_bytes=size_bytes,
            sha256=sha256,
        )

    def publish_handoff(
        self,
        handoff_id: str,
        run_id: str,
        task_id: str | None,
        author: str,
        recipient: str,
        content: str,
        supersedes_handoff_id: str | None = None,
    ) -> Handoff:
        """Publish one immutable, hashed runtime handoff."""
        if supersedes_handoff_id is not None:
            _validate_identifier(
                supersedes_handoff_id, "supersedes_handoff_id"
            )
        _validate_identifier(handoff_id, "handoff_id")
        _validate_identifier(run_id, "run_id")
        if task_id is not None:
            _validate_identifier(task_id, "task_id")
        self._validate_shared_text(author, recipient, content)
        content_bytes = content.encode("utf-8")
        if not content_bytes:
            raise ValueError("handoff content must not be empty")
        if len(content_bytes) > MAX_SHARED_TEXT_BYTES:
            raise ValueError("handoff content exceeds 64 KiB")

        digest = hashlib.sha256(content_bytes).hexdigest()
        self.handoff_directory.mkdir(parents=True, exist_ok=True)
        target_path = self.handoff_directory / f"{handoff_id}.md"
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.handoff_directory,
            prefix=f".{handoff_id}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        published = False
        connection = None

        try:
            with os.fdopen(descriptor, "wb") as temporary_file:
                temporary_file.write(content_bytes)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM handoffs WHERE id = ?",
                (handoff_id,),
            ).fetchone()
            if existing is not None or target_path.exists():
                raise ConflictError(
                    f"handoff {handoff_id!r} is already published"
                )

            if supersedes_handoff_id is not None:
                predecessor = connection.execute(
                    "SELECT run_id, task_id, recipient FROM handoffs "
                    "WHERE id = ?",
                    (supersedes_handoff_id,),
                ).fetchone()
                expected_lineage = (run_id, task_id, recipient)
                if (
                    predecessor is None
                    or tuple(predecessor) != expected_lineage
                ):
                    raise ConflictError(
                        "superseding handoff must match run, task, and "
                        "recipient"
                    )

            connection.execute(
                "INSERT INTO handoffs "
                "(id, run_id, task_id, author, recipient, sha256, "
                "supersedes_handoff_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    handoff_id,
                    run_id,
                    task_id,
                    author,
                    recipient,
                    digest,
                    supersedes_handoff_id,
                ),
            )
            self._record_event(
                connection,
                entity_type="handoff",
                entity_id=handoff_id,
                event_type="handoff_published",
                actor=author,
                detail=digest,
            )

            try:
                os.link(temporary_path, target_path)
            except FileExistsError as error:
                raise ConflictError(
                    f"handoff {handoff_id!r} is already published"
                ) from error
            published = True
            temporary_path.unlink()
            connection.commit()
        except Exception:
            if connection is not None:
                connection.rollback()
            if published:
                target_path.unlink(missing_ok=True)
            raise
        finally:
            if connection is not None:
                connection.close()
            temporary_path.unlink(missing_ok=True)

        return Handoff(
            handoff_id=handoff_id,
            run_id=run_id,
            task_id=task_id,
            author=author,
            recipient=recipient,
            sha256=digest,
        )

    def acknowledge_handoff(
        self,
        handoff_id: str,
        recipient: str,
        sha256: str,
    ) -> HandoffAcknowledgment:
        """Acknowledge the exact immutable handoff received by one agent."""
        _validate_identifier(handoff_id, "handoff_id")
        self._validate_shared_text(recipient)
        if not isinstance(sha256, str) or not SHA256_PATTERN.fullmatch(sha256):
            raise ValueError("sha256 must be 64 lowercase hexadecimal digits")
        with self._write_connection() as connection:
            row = connection.execute(
                "SELECT sha256, acknowledged_at FROM handoffs "
                "WHERE id = ? AND recipient = ?",
                (handoff_id, recipient),
            ).fetchone()
            if row is None or row["sha256"] != sha256:
                raise ConflictError(
                    f"handoff {handoff_id!r} acknowledgment does not match"
                )

            target_path = self.handoff_directory / f"{handoff_id}.md"
            try:
                if target_path.is_symlink() or not target_path.is_file():
                    raise ConflictError(
                        f"handoff {handoff_id!r} file is unavailable"
                    )
                if target_path.stat().st_size > MAX_SHARED_TEXT_BYTES:
                    raise ConflictError(
                        f"handoff {handoff_id!r} file exceeds 64 KiB"
                    )
                current_digest = hashlib.sha256(
                    target_path.read_bytes()
                ).hexdigest()
            except OSError as error:
                raise ConflictError(
                    f"handoff {handoff_id!r} file is unreadable"
                ) from error
            if current_digest != row["sha256"]:
                raise ConflictError(
                    f"handoff {handoff_id!r} file hash does not match"
                )

            acknowledged_at = row["acknowledged_at"]
            if acknowledged_at is not None:
                return HandoffAcknowledgment(
                    handoff_id=handoff_id,
                    recipient=recipient,
                    sha256=sha256,
                    acknowledged_at=acknowledged_at,
                )

            timestamp = _utc_now()
            cursor = connection.execute(
                "UPDATE handoffs SET acknowledged_at = ? "
                "WHERE id = ? AND acknowledged_at IS NULL",
                (timestamp, handoff_id),
            )
            if cursor.rowcount != 1:
                raise ConflictError(
                    f"handoff {handoff_id!r} changed during acknowledgment"
                )
            self._record_event(
                connection,
                entity_type="handoff",
                entity_id=handoff_id,
                event_type="handoff_acknowledged",
                actor=recipient,
                detail=sha256,
            )
            return HandoffAcknowledgment(
                handoff_id=handoff_id,
                recipient=recipient,
                sha256=sha256,
                acknowledged_at=timestamp,
            )

    def _connect(self) -> sqlite3.Connection:
        """Open one configured SQLite connection."""
        connection = sqlite3.connect(
            self.database_path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        """Yield one immediate transaction and commit or roll it back."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _get_task(connection: sqlite3.Connection, task_id: str) -> Task:
        """Return one task using an existing connection."""
        row = connection.execute(
            "SELECT id, run_id, title, state, version "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise WorkspaceError(f"task {task_id!r} does not exist")
        return _task_from_row(row)

    @staticmethod
    def _record_event(
        connection: sqlite3.Connection,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor: str,
        detail: str,
    ) -> None:
        """Append one task event inside the caller's transaction."""
        connection.execute(
            "INSERT INTO events "
            "(entity_type, entity_id, event_type, actor, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (entity_type, entity_id, event_type, actor, detail),
        )

    def _validate_shared_text(self, *values: str) -> None:
        """Reject sensitive markers and configured paths before persistence."""
        for value in values:
            if not isinstance(value, str):
                raise TypeError("shared text values must be strings")
            if len(value.encode("utf-8")) > MAX_SHARED_TEXT_BYTES:
                raise ValueError("shared text exceeds 64 KiB")
            has_sensitive_pattern = any(
                pattern.search(value)
                for pattern in SENSITIVE_TEXT_PATTERNS
            )
            if has_sensitive_pattern:
                raise SensitiveContentError(
                    "sensitive content is not allowed in shared state"
                )
            normalized_value = _normalize_path(value)
            if any(
                marker in normalized_value
                for marker in self._sensitive_path_markers
            ):
                message = "configured sensitive paths are not allowed"
                raise SensitiveContentError(message)


def _task_from_row(row: sqlite3.Row) -> Task:
    """Convert one SQLite row to an immutable task record."""
    return Task(
        task_id=row["id"],
        run_id=row["run_id"],
        title=row["title"],
        state=row["state"],
        version=row["version"],
    )


def _backup_manifest_path(backup_path: Path) -> Path:
    """Return the deterministic adjacent manifest path for one backup."""
    suffix = backup_path.suffix + ".manifest.json"
    return backup_path.with_suffix(suffix)


def _new_temporary_path(
    directory: Path,
    prefix: str,
    suffix: str,
) -> Path:
    """Create and close one temporary file in an atomic-replace directory."""
    descriptor, value = tempfile.mkstemp(
        dir=directory,
        prefix=prefix,
        suffix=suffix,
    )
    os.close(descriptor)
    return Path(value)


def _require_database_integrity(connection: sqlite3.Connection) -> None:
    """Fail unless SQLite integrity and foreign-key checks are clean."""
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    if len(rows) != 1 or rows[0][0] != "ok":
        raise WorkspaceError("backup database failed integrity_check")
    violation = connection.execute("PRAGMA foreign_key_check").fetchone()
    if violation is not None:
        raise WorkspaceError("backup database failed foreign_key_check")


def _sha256_file(path: Path) -> str:
    """Hash one file using bounded memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_backup_manifest(path: Path) -> dict[str, str | int]:
    """Read and validate one bounded backup manifest."""
    try:
        if not path.is_file():
            raise WorkspaceError("backup manifest does not exist")
        if path.stat().st_size > MAX_SHARED_TEXT_BYTES:
            raise WorkspaceError("backup manifest exceeds 64 KiB")
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except WorkspaceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WorkspaceError("backup manifest is unreadable") from error

    required_keys = {"format", "sha256", "size_bytes"}
    if not isinstance(manifest, dict) or set(manifest) != required_keys:
        raise WorkspaceError("backup manifest has an invalid schema")
    if manifest["format"] != BACKUP_FORMAT:
        raise WorkspaceError("backup manifest has an unknown format")
    digest = manifest["sha256"]
    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
        raise WorkspaceError("backup manifest has an invalid hash")
    size_bytes = manifest["size_bytes"]
    if (
        not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or size_bytes < 1
    ):
        raise WorkspaceError("backup manifest has an invalid size")
    return manifest


def _validate_identifier(value: str, field_name: str) -> None:
    """Require a bounded identifier that cannot escape a derived path."""
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} has an invalid format")


def _validate_classification_evidence(
    evidence: RunClassificationEvidence,
) -> dict[str, str]:
    """Validate and copy bounded run-classification observations."""
    if not isinstance(evidence, RunClassificationEvidence):
        raise TypeError("evidence must be RunClassificationEvidence")

    boolean_fields = {
        "process_running": evidence.process_running,
        "log_has_completion_marker": evidence.log_has_completion_marker,
        "log_has_not_found_marker": evidence.log_has_not_found_marker,
        "recovered_output_present": evidence.recovered_output_present,
    }
    for field_name, value in boolean_fields.items():
        if not isinstance(value, bool):
            raise TypeError(f"{field_name} must be a boolean")

    exit_code = evidence.process_exit_code
    if exit_code is not None:
        _require_bounded_integer(
            exit_code,
            "process_exit_code",
            MIN_PROCESS_EXIT_CODE,
            MAX_PROCESS_EXIT_CODE,
        )
    _require_bounded_integer(
        evidence.checkpoint_combo_idx,
        "checkpoint_combo_idx",
        0,
        MAX_UINT64,
    )
    _require_bounded_integer(
        evidence.checkpoint_total_combos,
        "checkpoint_total_combos",
        0,
        MAX_UINT64,
    )

    manifest = evidence.expected_artifact_hashes
    if not isinstance(manifest, Mapping):
        raise TypeError("expected_artifact_hashes must be a mapping")
    if not 1 <= len(manifest) <= MAX_MANIFEST_ARTIFACTS:
        raise ValueError(
            "expected_artifact_hashes must contain between 1 and 100 items"
        )

    expected_hashes = {}
    for artifact_id, digest in manifest.items():
        _validate_identifier(artifact_id, "artifact_id")
        if not isinstance(digest, str):
            raise TypeError("artifact hash must be a string")
        if not SHA256_PATTERN.fullmatch(digest):
            raise ValueError(
                "artifact hash must be 64 lowercase hexadecimal digits"
            )
        expected_hashes[artifact_id] = digest
    return expected_hashes


def _classify_run_evidence(
    evidence: RunClassificationEvidence,
    *,
    hashes_match: bool,
) -> str:
    """Return a fail-closed state from validated observations."""
    complete = (
        not evidence.process_running
        and evidence.process_exit_code == 0
        and evidence.checkpoint_combo_idx
        == evidence.checkpoint_total_combos
        and evidence.log_has_completion_marker
        and evidence.log_has_not_found_marker
        and not evidence.recovered_output_present
        and hashes_match
    )
    if complete:
        return "complete_not_found"

    incomplete = (
        not evidence.process_running
        and evidence.process_exit_code is None
        and evidence.checkpoint_combo_idx
        < evidence.checkpoint_total_combos
        and not evidence.log_has_completion_marker
        and not evidence.log_has_not_found_marker
        and not evidence.recovered_output_present
        and hashes_match
    )
    if incomplete:
        return "incomplete"
    return "unknown"


def _require_bounded_integer(
    value: int,
    field_name: str,
    minimum: int,
    maximum: int,
) -> None:
    """Require a non-boolean integer inside the supplied inclusive range."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(
            f"{field_name} must be between {minimum} and {maximum}"
        )


def _normalize_path(value: str | Path) -> str:
    """Normalize path-like text for case-insensitive substring checks."""
    return str(value).replace("\\", "/").casefold()


def _utc_now() -> str:
    """Return one stable UTC timestamp for related transaction writes."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
