"""Tests for the local Claude and Codex coordination workspace."""

import hashlib
import inspect
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from tools.agent_workspace import (
    BackupResult,
    ConflictError,
    InvalidTransitionError,
    RunClassificationEvidence,
    SensitiveContentError,
    Workspace,
    WorkspaceError,
)
from tools.agent_workspace_mcp import (
    ALLOWED_TOOL_NAMES,
    MAX_RESPONSE_BYTES,
    McpRequestError,
    McpResponseTooLarge,
    dispatch_tool,
    tool_definitions,
)

REQUIRED_TABLES = {
    "approvals",
    "artifacts",
    "claims",
    "events",
    "findings",
    "handoffs",
    "reviews",
    "runs",
    "schema_versions",
    "tasks",
}


def initialized_workspace(tmp_path):
    """Return an initialized workspace and its temporary database path."""
    database_path = tmp_path / "agent-workspace.db"
    workspace = Workspace(database_path)
    workspace.initialize()
    return workspace, database_path


def create_open_task(workspace):
    """Create the common run and open task used by state-machine tests."""
    workspace.create_run("run-1", "Synthetic coordination run")
    return workspace.create_task("task-1", "run-1", "Build schema")


def event_count(database_path):
    """Return the number of persisted state-transition events."""
    with sqlite3.connect(database_path) as connection:
        row = connection.execute("SELECT COUNT(*) FROM events").fetchone()
    return row[0]


def register_classification_artifacts(workspace):
    """Register synthetic metadata and return its expected manifest."""
    payloads = {
        "checkpoint-1": b"synthetic checkpoint metadata",
        "log-1": b"synthetic completion log metadata",
    }
    expected_hashes = {}
    for artifact_id, payload in payloads.items():
        digest = hashlib.sha256(payload).hexdigest()
        workspace.register_artifact(
            artifact_id,
            "run-1",
            f"Synthetic {artifact_id}",
            "test_fixture",
            len(payload),
            digest,
        )
        expected_hashes[artifact_id] = digest
    return expected_hashes


def classification_evidence(expected_hashes, **overrides):
    """Build one complete synthetic evidence record with overrides."""
    values = {
        "process_running": False,
        "process_exit_code": 0,
        "checkpoint_combo_idx": 100,
        "checkpoint_total_combos": 100,
        "log_has_completion_marker": True,
        "log_has_not_found_marker": True,
        "recovered_output_present": False,
        "expected_artifact_hashes": expected_hashes,
    }
    values.update(overrides)
    return RunClassificationEvidence(**values)


def read_run_state(database_path, run_id):
    """Read one persisted run state for classification assertions."""
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT state FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    return row[0]


def snapshot_database(database_path):
    """Return every required table row in a deterministic representation."""
    snapshot = {}
    with sqlite3.connect(database_path) as connection:
        for table_name in sorted(REQUIRED_TABLES):
            rows = connection.execute(
                f"SELECT * FROM {table_name} ORDER BY rowid"
            ).fetchall()
            snapshot[table_name] = rows
    return snapshot


def populate_backup_fixture(workspace):
    """Populate several related tables using only synthetic metadata."""
    workspace.create_run("run-1", "Synthetic backup drill")
    workspace.create_task("task-1", "run-1", "Verify backup state")
    workspace.claim_task("task-1", "codex", expected_version=0)
    digest = hashlib.sha256(b"synthetic backup artifact").hexdigest()
    workspace.register_artifact(
        "artifact-1",
        "run-1",
        "Synthetic backup artifact",
        "test_fixture",
        25,
        digest,
    )


def create_synthetic_backup(database_path, backup_path):
    """Create a valid test backup without exercising the unit under test."""
    backup_path.parent.mkdir(parents=True)
    with (
        sqlite3.connect(database_path) as source,
        sqlite3.connect(backup_path) as target,
    ):
        source.backup(target)
    backup_bytes = backup_path.read_bytes()
    manifest = {
        "format": "bitcracker-agent-workspace-backup-v1",
        "sha256": hashlib.sha256(backup_bytes).hexdigest(),
        "size_bytes": len(backup_bytes),
    }
    manifest_path = backup_path.with_suffix(".db.manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def rpc_call(request_id, name, arguments):
    """Build one bounded JSON-RPC tool call request."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def run_stdio_client(project_root, database_path, requests):
    """Run one real stdio client process and return responses by ID."""
    environment = os.environ.copy()
    environment["BITCRACKER_AGENT_WORKSPACE"] = str(
        database_path.resolve()
    )
    request_stream = "".join(
        json.dumps(request, separators=(",", ":")) + "\n"
        for request in requests
    )
    completed = subprocess.run(
        [sys.executable, "-m", "tools.agent_workspace_mcp"],
        cwd=project_root,
        env=environment,
        input=request_stream,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    responses = [
        json.loads(line)
        for line in completed.stdout.splitlines()
        if line
    ]
    expected_ids = {
        request["id"] for request in requests if "id" in request
    }
    assert {response["id"] for response in responses} == expected_ids
    return {response["id"]: response for response in responses}


def run_stdio_stream(project_root, database_path, request_stream):
    """Run one raw synthetic request stream through the stdio process."""
    environment = os.environ.copy()
    environment["BITCRACKER_AGENT_WORKSPACE"] = str(
        database_path.resolve()
    )
    return subprocess.run(
        [sys.executable, "-m", "tools.agent_workspace_mcp"],
        cwd=project_root,
        env=environment,
        input=request_stream,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def structured_tool_result(response):
    """Validate and return one standard MCP structured tool result."""
    encoded = json.dumps(response, separators=(",", ":")).encode("utf-8")
    assert len(encoded) <= MAX_RESPONSE_BYTES
    result = response["result"]
    assert set(result) == {"content", "structuredContent"}
    assert len(result["content"]) == 1
    content = result["content"][0]
    assert content["type"] == "text"
    parsed_text = json.loads(content["text"])
    assert parsed_text == result["structuredContent"]
    return result["structuredContent"]


def test_schema_enforces_constraints(tmp_path):
    workspace, database_path = initialized_workspace(tmp_path)
    create_open_task(workspace)
    workspace.claim_task("task-1", "codex", expected_version=0)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert REQUIRED_TABLES <= tables

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO tasks "
                "(id, run_id, title, state, version) "
                "VALUES (?, ?, ?, ?, ?)",
                ("bad-fk", "missing-run", "Bad foreign key", "open", 0),
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO claims (task_id, agent, active) "
                "VALUES (?, ?, ?)",
                ("task-1", "claude", 1),
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO tasks "
                "(id, run_id, title, state, version) "
                "VALUES (?, ?, ?, ?, ?)",
                ("missing-title", "run-1", None, "open", 0),
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO tasks "
                "(id, run_id, title, state, version) "
                "VALUES (?, ?, ?, ?, ?)",
                ("bad-state", "run-1", "Bad state", "unknown", 0),
            )

        task_total = connection.execute(
            "SELECT COUNT(*) FROM tasks"
        ).fetchone()
        claim_total = connection.execute(
            "SELECT COUNT(*) FROM claims"
        ).fetchone()

    assert task_total[0] == 1
    assert claim_total[0] == 1


def test_queries_treat_input_as_data(tmp_path):
    workspace, database_path = initialized_workspace(tmp_path)
    workspace.create_run("run-1", "Synthetic coordination run")
    hostile_title = "'); DROP TABLE tasks; --"
    workspace.create_task("task-1", "run-1", hostile_title)

    tasks = workspace.list_tasks()
    assert [task.title for task in tasks] == [hostile_title]

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'tasks'"
        ).fetchone()
    assert row == ("tasks",)


def test_concurrent_claim_has_one_winner(tmp_path):
    workspace, database_path = initialized_workspace(tmp_path)
    create_open_task(workspace)
    barrier = Barrier(2)

    def claim(agent):
        client = Workspace(database_path)
        barrier.wait()
        try:
            client.claim_task("task-1", agent, expected_version=0)
        except ConflictError:
            return "conflict"
        return "claimed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, ("codex", "claude")))

    assert sorted(results) == ["claimed", "conflict"]
    task = workspace.get_task("task-1")
    assert task.state == "claimed"
    assert task.version == 1


def test_stale_update_is_rejected(tmp_path):
    workspace, _ = initialized_workspace(tmp_path)
    task = create_open_task(workspace)
    assert task.version == 0

    claimed = workspace.claim_task("task-1", "codex", expected_version=0)
    assert claimed.state == "claimed"
    assert claimed.version == 1

    with pytest.raises(ConflictError):
        workspace.transition_task(
            "task-1",
            "in_progress",
            "codex",
            expected_version=0,
        )

    unchanged = workspace.get_task("task-1")
    assert unchanged.state == "claimed"
    assert unchanged.version == 1


def test_invalid_transition_rolls_back(tmp_path):
    workspace, database_path = initialized_workspace(tmp_path)
    task = create_open_task(workspace)
    events_before = event_count(database_path)

    with pytest.raises(InvalidTransitionError):
        workspace.transition_task(
            "task-1",
            "done",
            "codex",
            expected_version=task.version,
        )

    unchanged = workspace.get_task("task-1")
    assert unchanged.state == "open"
    assert unchanged.version == task.version
    assert event_count(database_path) == events_before


def test_sensitive_content_is_rejected(tmp_path):
    database_path = tmp_path / "agent-workspace.db"
    handoff_directory = tmp_path / "handoffs"
    sensitive_path = tmp_path / "personal-token-list.txt"
    workspace = Workspace(
        database_path,
        handoff_directory=handoff_directory,
        sensitive_paths=[sensitive_path],
    )
    workspace.initialize()
    workspace.create_run("run-1", "Synthetic coordination run")

    sensitive_titles = (
        ("task-password", "password: synthetic-secret"),
        ("task-recovered", "recovered password = synthetic-secret"),
        ("task-candidates", "candidate list: alpha, beta"),
        ("task-wallet", "wallet bytes: 00112233"),
        ("task-path", f"inspect {sensitive_path}"),
    )
    for task_id, title in sensitive_titles:
        with pytest.raises(SensitiveContentError):
            workspace.create_task(task_id, "run-1", title)

    assert workspace.list_tasks() == []

    workspace.create_task("task-clean", "run-1", "Publish handoff")
    with pytest.raises(SensitiveContentError):
        workspace.publish_handoff(
            "handoff-sensitive",
            "run-1",
            "task-clean",
            "codex",
            "claude",
            "recovered password: synthetic-secret",
        )

    assert not (handoff_directory / "handoff-sensitive.md").exists()
    with sqlite3.connect(database_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM handoffs"
        ).fetchone()
    assert count[0] == 0


def test_artifact_registration_stores_metadata_only(tmp_path):
    workspace, database_path = initialized_workspace(tmp_path)
    workspace.create_run("run-1", "Synthetic coordination run")
    payload = b"synthetic public fixture bytes"
    digest = hashlib.sha256(payload).hexdigest()

    parameters = inspect.signature(workspace.register_artifact).parameters
    assert tuple(parameters) == (
        "artifact_id",
        "run_id",
        "label",
        "kind",
        "size_bytes",
        "sha256",
    )

    artifact = workspace.register_artifact(
        "artifact-1",
        "run-1",
        "Synthetic fixture",
        "test_fixture",
        len(payload),
        digest,
    )
    assert artifact.size_bytes == len(payload)
    assert artifact.sha256 == digest

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(artifacts)")
        }
        row = connection.execute(
            "SELECT label, kind, size_bytes, sha256 "
            "FROM artifacts WHERE id = ?",
            ("artifact-1",),
        ).fetchone()

    assert columns == {
        "id",
        "run_id",
        "label",
        "kind",
        "size_bytes",
        "sha256",
        "created_at",
    }
    assert row == ("Synthetic fixture", "test_fixture", len(payload), digest)
    assert payload not in database_path.read_bytes()


def test_run_classification_requires_consensus(tmp_path):
    workspace, database_path = initialized_workspace(tmp_path)
    workspace.create_run("run-1", "Synthetic coordination run")
    expected_hashes = register_classification_artifacts(workspace)

    complete = classification_evidence(expected_hashes)
    classification = workspace.classify_run("run-1", complete)
    assert classification == "complete_not_found"
    assert read_run_state(database_path, "run-1") == classification

    conflicting_evidence = (
        classification_evidence(
            expected_hashes,
            process_exit_code=1,
        ),
        classification_evidence(
            expected_hashes,
            process_running=True,
        ),
        classification_evidence(
            expected_hashes,
            checkpoint_combo_idx=101,
        ),
        classification_evidence(
            expected_hashes,
            log_has_completion_marker=False,
        ),
        classification_evidence(
            expected_hashes,
            log_has_not_found_marker=False,
        ),
        classification_evidence(
            expected_hashes,
            recovered_output_present=True,
        ),
        classification_evidence(
            {"checkpoint-1": "0" * 64},
        ),
    )
    for evidence in conflicting_evidence:
        classification = workspace.classify_run("run-1", evidence)
        assert classification == "unknown"
        assert read_run_state(database_path, "run-1") == classification


def test_current_search_shape_is_incomplete(tmp_path):
    workspace, database_path = initialized_workspace(tmp_path)
    workspace.create_run("run-1", "Synthetic coordination run")
    expected_hashes = register_classification_artifacts(workspace)
    evidence = classification_evidence(
        expected_hashes,
        process_exit_code=None,
        checkpoint_combo_idx=41,
        log_has_completion_marker=False,
        log_has_not_found_marker=False,
    )

    classification = workspace.classify_run("run-1", evidence)

    assert classification == "incomplete"
    assert read_run_state(database_path, "run-1") == classification


def test_backup_round_trip_preserves_state(tmp_path):
    workspace, database_path = initialized_workspace(tmp_path)
    populate_backup_fixture(workspace)
    expected_snapshot = snapshot_database(database_path)
    backup_path = tmp_path / "backups" / "workspace.db"

    result = workspace.create_backup(backup_path)

    assert isinstance(result, BackupResult)
    assert result.backup_path == backup_path
    assert result.manifest_path == backup_path.with_suffix(".db.manifest.json")
    assert result.size_bytes == backup_path.stat().st_size
    backup_digest = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    assert result.sha256 == backup_digest
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest == {
        "format": "bitcracker-agent-workspace-backup-v1",
        "sha256": result.sha256,
        "size_bytes": result.size_bytes,
    }
    with sqlite3.connect(backup_path) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
    assert integrity == ("ok",)

    workspace.create_task("task-after-backup", "run-1", "Discard on restore")
    assert snapshot_database(database_path) != expected_snapshot

    workspace.restore_backup(backup_path)

    assert snapshot_database(database_path) == expected_snapshot


def test_corrupt_backup_fails_closed(tmp_path):
    workspace, database_path = initialized_workspace(tmp_path)
    populate_backup_fixture(workspace)
    backup_path = tmp_path / "backups" / "workspace.db"
    manifest_path = create_synthetic_backup(database_path, backup_path)
    workspace.create_task(
        "task-active",
        "run-1",
        "Must survive failed restore",
    )
    active_snapshot = snapshot_database(database_path)

    corrupted = bytearray(backup_path.read_bytes())
    corrupted[:16] = b"not sqlite data!"
    backup_path.write_bytes(corrupted)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["sha256"] = hashlib.sha256(corrupted).hexdigest()
    manifest["size_bytes"] = len(corrupted)
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError):
        workspace.restore_backup(backup_path)

    assert snapshot_database(database_path) == active_snapshot


def test_handoff_is_immutable_and_hashed(tmp_path):
    database_path = tmp_path / "agent-workspace.db"
    handoff_directory = tmp_path / "handoffs"
    workspace = Workspace(
        database_path,
        handoff_directory=handoff_directory,
    )
    workspace.initialize()
    workspace.create_run("run-1", "Synthetic coordination run")
    workspace.create_task("task-1", "run-1", "Review handoff")
    content = "# Synthetic handoff\n\nNo sensitive content.\n"
    expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    handoff = workspace.publish_handoff(
        "handoff-1",
        "run-1",
        "task-1",
        "codex",
        "claude",
        content,
    )
    handoff_path = handoff_directory / "handoff-1.md"
    assert handoff.sha256 == expected_hash
    assert handoff_path.read_text(encoding="utf-8") == content

    with pytest.raises(ConflictError):
        workspace.publish_handoff(
            "handoff-1",
            "run-1",
            "task-1",
            "codex",
            "claude",
            "changed content",
        )

    assert handoff_path.read_text(encoding="utf-8") == content
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT id, sha256 FROM handoffs"
        ).fetchall()
    assert rows == [("handoff-1", expected_hash)]


def test_handoff_requires_acknowledgment(tmp_path):
    workspace, _ = initialized_workspace(tmp_path)
    workspace.create_run("run-1", "Synthetic coordination run")
    workspace.create_task("task-1", "run-1", "Transfer task")
    published = workspace.publish_handoff(
        "handoff-1",
        "run-1",
        "task-1",
        "codex",
        "claude",
        "# Transfer\n\nSynthetic metadata only.\n",
    )

    with pytest.raises(ConflictError):
        workspace.claim_task("task-1", "claude", expected_version=0)
    with pytest.raises(ConflictError):
        workspace.acknowledge_handoff(
            "handoff-1",
            "claude",
            "0" * 64,
        )

    acknowledgment = workspace.acknowledge_handoff(
        "handoff-1",
        "claude",
        published.sha256,
    )
    assert acknowledgment.handoff_id == "handoff-1"
    assert acknowledgment.recipient == "claude"
    assert acknowledgment.sha256 == published.sha256
    claimed = workspace.claim_task("task-1", "claude", expected_version=0)
    assert claimed.state == "claimed"


def test_superseding_handoff_preserves_history(tmp_path):
    database_path = tmp_path / "agent-workspace.db"
    handoff_directory = tmp_path / "handoffs"
    workspace = Workspace(
        database_path,
        handoff_directory=handoff_directory,
    )
    workspace.initialize()
    workspace.create_run("run-1", "Synthetic coordination run")
    workspace.create_task("task-1", "run-1", "Correct handoff")
    original_content = "# Original\n\nSynthetic metadata only.\n"
    original = workspace.publish_handoff(
        "handoff-1",
        "run-1",
        "task-1",
        "codex",
        "claude",
        original_content,
    )
    original_path = handoff_directory / "handoff-1.md"
    original_events = event_count(database_path)

    correction_content = "# Correction\n\nSupersedes handoff-1.\n"
    correction = workspace.publish_handoff(
        "handoff-2",
        "run-1",
        "task-1",
        "codex",
        "claude",
        correction_content,
        supersedes_handoff_id="handoff-1",
    )

    assert original_path.read_text(encoding="utf-8") == original_content
    correction_path = handoff_directory / "handoff-2.md"
    assert correction_path.read_text(encoding="utf-8") == correction_content
    assert correction.sha256 != original.sha256
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT id, supersedes_handoff_id FROM handoffs ORDER BY id"
        ).fetchall()
    assert rows == [("handoff-1", None), ("handoff-2", "handoff-1")]
    assert event_count(database_path) == original_events + 1


def test_latest_superseding_handoff_controls_claim_gate(tmp_path):
    workspace, _ = initialized_workspace(tmp_path)
    workspace.create_run("run-1", "Synthetic correction run")
    workspace.create_task("task-1", "run-1", "Receive correction")
    workspace.publish_handoff(
        "handoff-1",
        "run-1",
        "task-1",
        "codex",
        "claude",
        "# Original\n",
    )
    correction = workspace.publish_handoff(
        "handoff-2",
        "run-1",
        "task-1",
        "codex",
        "claude",
        "# Correction\n",
        supersedes_handoff_id="handoff-1",
    )
    workspace.acknowledge_handoff(
        "handoff-2",
        "claude",
        correction.sha256,
    )

    claimed = workspace.claim_task("task-1", "claude", expected_version=0)

    assert claimed.state == "claimed"


def test_supersession_requires_same_run_task_and_recipient(tmp_path):
    workspace, _ = initialized_workspace(tmp_path)
    workspace.create_run("run-1", "First synthetic run")
    workspace.create_run("run-2", "Second synthetic run")
    workspace.create_task("task-1", "run-1", "Original task")
    workspace.create_task("task-2", "run-1", "Different task")
    workspace.create_task("task-3", "run-2", "Different run")
    workspace.publish_handoff(
        "handoff-1",
        "run-1",
        "task-1",
        "codex",
        "claude",
        "# Original\n",
    )
    invalid_corrections = (
        ("handoff-2", "run-1", "task-2", "claude"),
        ("handoff-3", "run-2", "task-3", "claude"),
        ("handoff-4", "run-1", "task-1", "other-agent"),
    )

    for handoff_id, run_id, task_id, recipient in invalid_corrections:
        with pytest.raises(ConflictError):
            workspace.publish_handoff(
                handoff_id,
                run_id,
                task_id,
                "codex",
                recipient,
                "# Invalid correction\n",
                supersedes_handoff_id="handoff-1",
            )


def test_acknowledgment_rehashes_published_file(tmp_path):
    database_path = tmp_path / "agent-workspace.db"
    handoff_directory = tmp_path / "handoffs"
    workspace = Workspace(
        database_path,
        handoff_directory=handoff_directory,
    )
    workspace.initialize()
    workspace.create_run("run-1", "Synthetic tamper run")
    workspace.create_task("task-1", "run-1", "Detect changed handoff")
    published = workspace.publish_handoff(
        "handoff-1",
        "run-1",
        "task-1",
        "codex",
        "claude",
        "# Original\n",
    )
    handoff_path = handoff_directory / "handoff-1.md"
    handoff_path.write_text("# Altered\n", encoding="utf-8")

    with pytest.raises(ConflictError):
        workspace.acknowledge_handoff(
            "handoff-1",
            "claude",
            published.sha256,
        )


def test_repeated_acknowledgment_is_idempotent(tmp_path):
    workspace, database_path = initialized_workspace(tmp_path)
    workspace.create_run("run-1", "Synthetic acknowledgment run")
    workspace.create_task("task-1", "run-1", "Acknowledge once")
    published = workspace.publish_handoff(
        "handoff-1",
        "run-1",
        "task-1",
        "codex",
        "claude",
        "# Exact handoff\n",
    )

    first = workspace.acknowledge_handoff(
        "handoff-1",
        "claude",
        published.sha256,
    )
    events_after_first = event_count(database_path)
    second = workspace.acknowledge_handoff(
        "handoff-1",
        "claude",
        published.sha256,
    )

    assert second == first
    assert event_count(database_path) == events_after_first


def test_stdio_rejects_bad_requests_and_continues(tmp_path):
    database_path = tmp_path / "stdio-errors.db"
    project_root = Path(__file__).resolve().parents[1]
    requests = [
        "{not-json}\n",
        json.dumps(
            rpc_call(
                1,
                "create_run",
                {"run_id": "/", "label": "Invalid identifier"},
            ),
            separators=(",", ":"),
        )
        + "\n",
        json.dumps(
            rpc_call(
                2,
                "create_run",
                {"run_id": "run-1", "label": "Valid synthetic run"},
            ),
            separators=(",", ":"),
        )
        + "\n",
        json.dumps(
            rpc_call(
                3,
                "create_run",
                {"run_id": "run-1", "label": "Duplicate run"},
            ),
            separators=(",", ":"),
        )
        + "\n",
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/list",
                "params": {},
            },
            separators=(",", ":"),
        )
        + "\n",
    ]

    completed = run_stdio_stream(
        project_root,
        database_path,
        "".join(requests),
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert len(responses) == 5
    assert responses[0]["id"] is None
    assert "error" in responses[0]
    by_id = {
        response["id"]: response
        for response in responses
        if response["id"] is not None
    }
    assert "error" in by_id[1]
    assert structured_tool_result(by_id[2]) == {
        "created": True,
        "run_id": "run-1",
    }
    assert "error" in by_id[3]
    assert tuple(
        tool["name"] for tool in by_id[4]["result"]["tools"]
    ) == ALLOWED_TOOL_NAMES


def test_stdio_bounds_complete_response_and_request_line(tmp_path):
    database_path = tmp_path / "stdio-bounds.db"
    project_root = Path(__file__).resolve().parents[1]
    oversized = {
        "jsonrpc": "2.0",
        "id": "x" * MAX_RESPONSE_BYTES,
        "method": "tools/list",
        "params": {},
    }
    valid = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    }
    request_stream = (
        json.dumps(oversized, separators=(",", ":"))
        + "\n"
        + json.dumps(valid, separators=(",", ":"))
        + "\n"
    )

    completed = run_stdio_stream(
        project_root,
        database_path,
        request_stream,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    response_lines = completed.stdout.splitlines()
    assert len(response_lines) == 2
    for line in response_lines:
        assert len(line.encode("utf-8")) <= MAX_RESPONSE_BYTES
    oversized_response = json.loads(response_lines[0])
    assert oversized_response["id"] is None
    assert "error" in oversized_response
    assert json.loads(response_lines[1])["id"] == 1


def test_two_clients_exchange_checkpoint(tmp_path):
    database_path = tmp_path / "shared-agent-workspace.db"
    project_root = Path(__file__).resolve().parents[1]
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "synthetic-client", "version": "1"},
        },
    }
    initialized = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }
    first_responses = run_stdio_client(
        project_root,
        database_path,
        [
            initialize,
            initialized,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
            rpc_call(
                3,
                "create_run",
                {
                    "run_id": "run-1",
                    "label": "Synthetic two-client run",
                },
            ),
            rpc_call(
                4,
                "create_task",
                {
                    "task_id": "task-1",
                    "run_id": "run-1",
                    "title": "Transfer checkpoint",
                },
            ),
            rpc_call(
                5,
                "publish_handoff",
                {
                    "handoff_id": "handoff-1",
                    "run_id": "run-1",
                    "task_id": "task-1",
                    "author": "codex",
                    "recipient": "claude",
                    "content": "# Checkpoint\n\nSynthetic metadata only.\n",
                },
            ),
        ],
    )
    assert first_responses[1]["result"]["protocolVersion"] == "2025-06-18"
    tool_names = tuple(
        tool["name"] for tool in first_responses[2]["result"]["tools"]
    )
    assert tool_names == ALLOWED_TOOL_NAMES
    published = structured_tool_result(first_responses[5])
    handoff_hash = published["handoff"]["sha256"]

    second_responses = run_stdio_client(
        project_root,
        database_path,
        [
            initialize,
            initialized,
            rpc_call(
                2,
                "acknowledge_handoff",
                {
                    "handoff_id": "handoff-1",
                    "recipient": "claude",
                    "sha256": handoff_hash,
                },
            ),
            rpc_call(
                3,
                "claim_task",
                {
                    "task_id": "task-1",
                    "agent": "claude",
                    "expected_version": 0,
                },
            ),
        ],
    )
    acknowledgment_result = structured_tool_result(second_responses[2])
    acknowledgment = acknowledgment_result["acknowledgment"]
    assert acknowledgment["sha256"] == handoff_hash
    claim_result = structured_tool_result(second_responses[3])
    assert claim_result["task"]["state"] == "claimed"

    with sqlite3.connect(database_path) as connection:
        handoff_row = connection.execute(
            "SELECT acknowledged_at FROM handoffs WHERE id = ?",
            ("handoff-1",),
        ).fetchone()
        task_row = connection.execute(
            "SELECT state, version FROM tasks WHERE id = ?",
            ("task-1",),
        ).fetchone()
    assert handoff_row[0] is not None
    assert task_row == ("claimed", 1)


def test_mcp_has_no_approval_or_execution_tool():
    definitions = tool_definitions()
    names = tuple(definition["name"] for definition in definitions)
    assert names == ALLOWED_TOOL_NAMES

    prohibited_names = {
        "approve_gate",
        "run_recovery",
        "resume_recovery",
        "stop_recovery",
        "git_commit",
        "git_push",
        "delete_branch",
        "read_secret",
        "read_artifact_contents",
    }
    assert prohibited_names.isdisjoint(names)
    for definition in definitions:
        schema = definition["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False


def test_mcp_validates_and_bounds_requests(tmp_path):
    workspace, _ = initialized_workspace(tmp_path)
    workspace.create_run("run-1", "Synthetic coordination run")
    workspace.create_task("task-1", "run-1", "First task")

    with pytest.raises(McpRequestError):
        dispatch_tool(workspace, "unknown_tool", {})
    with pytest.raises(McpRequestError):
        dispatch_tool(workspace, "approve_gate", {"gate": 1})
    with pytest.raises(McpRequestError):
        dispatch_tool(
            workspace,
            "create_task",
            {
                "task_id": "task-invalid",
                "run_id": "run-1",
                "title": "Invalid request",
                "unexpected": True,
            },
        )
    with pytest.raises(McpRequestError):
        dispatch_tool(workspace, "list_tasks", {"limit": "100"})
    with pytest.raises(McpRequestError):
        dispatch_tool(workspace, "list_tasks", {"limit": 101})

    assert [task.task_id for task in workspace.list_tasks()] == ["task-1"]
    result = dispatch_tool(workspace, "list_tasks", {"limit": 1})
    assert result == {
        "tasks": [
            {
                "task_id": "task-1",
                "run_id": "run-1",
                "title": "First task",
                "state": "open",
                "version": 0,
            }
        ]
    }

    for index in range(2, 102):
        workspace.create_task(
            f"task-{index}",
            "run-1",
            f"Large response row {index}: " + ("x" * 1_000),
        )

    with pytest.raises(McpResponseTooLarge):
        dispatch_tool(workspace, "list_tasks", {"limit": 100})
