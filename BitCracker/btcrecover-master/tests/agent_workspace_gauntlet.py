"""Reproduce the agent workspace gauntlet with synthetic data only."""

import argparse
import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier
from typing import Any
from unittest import mock
from xml.etree import ElementTree

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.agent_workspace import (
    MAX_SHARED_TEXT_BYTES,
    ConflictError,
    Workspace,
)
from tools.agent_workspace_mcp import (
    ALLOWED_TOOL_NAMES,
    MAX_RESPONSE_BYTES,
    tool_definitions,
)

BASE_COMMIT = "9a6a7212dd854ebc0bb069f70a2cbc2220d6715b"
BUILD_DIRECTORY = PROJECT_ROOT / ".cuda-build" / "agent-workspace-gauntlet"
COVERAGE_DATA = BUILD_DIRECTORY / ".coverage"
COVERAGE_JSON = BUILD_DIRECTORY / "coverage.json"
COVERAGE_MINIMUM = 90.0
STRESS_SEED = 20260927
STRESS_ROUNDS = 40
STRESS_CLIENTS = 8

IMPLEMENTATION_PATHS = (
    "tools/agent_workspace.py",
    "tools/agent_workspace_mcp.py",
)
RUFF_PATHS = (
    *IMPLEMENTATION_PATHS,
    "tests/test_agent_workspace.py",
    "tests/agent_workspace_gauntlet.py",
)
FORBIDDEN_CAPABILITIES = {
    "approve_gate",
    "delete_branch",
    "git_commit",
    "git_push",
    "read_artifact_contents",
    "read_secret",
    "resume_recovery",
    "run_recovery",
    "stop_recovery",
}


class GauntletError(RuntimeError):
    """Raised when one gauntlet layer fails closed."""


@dataclass(frozen=True)
class Mutation:
    """Describe one unique source mutation and its killing test."""

    name: str
    path: str
    before: str
    after: str
    tests: tuple[str, ...]


MUTATIONS = (
    Mutation(
        name="claim_conflict_bypass",
        path="tools/agent_workspace.py",
        before=(
            '            if task.version != expected_version or '
            'task.state != "open":\n'
            "                raise ConflictError(\n"
            "                    f\"task {task_id!r} is not claimable at "
            "version \"\n"
            "                    f\"{expected_version}\"\n"
            "                )\n"
        ),
        after=(
            '            if task.version != expected_version or '
            'task.state != "open":\n'
            "                return task\n"
        ),
        tests=("test_concurrent_claim_has_one_winner",),
    ),
    Mutation(
        name="advertise_approval_tool",
        path="tools/agent_workspace_mcp.py",
        before=(
            'ALLOWED_TOOL_NAMES = (\n'
            '    "create_run",\n'
        ),
        after=(
            'ALLOWED_TOOL_NAMES = (\n'
            '    "approve_gate",\n'
            '    "create_run",\n'
        ),
        tests=("test_mcp_has_no_approval_or_execution_tool",),
    ),
    Mutation(
        name="weaken_sensitive_pattern_check",
        path="tools/agent_workspace.py",
        before="            has_sensitive_pattern = any(\n",
        after="            has_sensitive_pattern = all(\n",
        tests=("test_sensitive_content_is_rejected",),
    ),
    Mutation(
        name="weaken_run_consensus",
        path="tools/agent_workspace.py",
        before=(
            "        and not evidence.recovered_output_present\n"
            "        and hashes_match\n"
            "    )\n"
            "    if complete:\n"
        ),
        after=(
            "        and not evidence.recovered_output_present\n"
            "        or hashes_match\n"
            "    )\n"
            "    if complete:\n"
        ),
        tests=("test_run_classification_requires_consensus",),
    ),
    Mutation(
        name="invert_backup_hash_validation",
        path="tools/agent_workspace.py",
        before=(
            '            if _sha256_file(source_path) != manifest["sha256"]:\n'
        ),
        after=(
            '            if _sha256_file(source_path) == manifest["sha256"]:\n'
        ),
        tests=(
            "test_backup_round_trip_preserves_state",
            "test_corrupt_backup_fails_closed",
        ),
    ),
    Mutation(
        name="ignore_latest_handoff_gate",
        path="tools/agent_workspace.py",
        before='                "AND NOT EXISTS ("\n',
        after='                "AND EXISTS ("\n',
        tests=("test_handoff_requires_acknowledgment",),
    ),
    Mutation(
        name="invert_supersession_lineage",
        path="tools/agent_workspace.py",
        before=(
            "                    or tuple(predecessor) != expected_lineage\n"
        ),
        after=(
            "                    or tuple(predecessor) == expected_lineage\n"
        ),
        tests=(
            "test_superseding_handoff_preserves_history",
            "test_supersession_requires_same_run_task_and_recipient",
        ),
    ),
    Mutation(
        name="invert_handoff_rehash_check",
        path="tools/agent_workspace.py",
        before='            if current_digest != row["sha256"]:\n',
        after='            if current_digest == row["sha256"]:\n',
        tests=(
            "test_handoff_requires_acknowledgment",
            "test_acknowledgment_rehashes_published_file",
        ),
    ),
    Mutation(
        name="invert_acknowledgment_idempotence",
        path="tools/agent_workspace.py",
        before="            if acknowledged_at is not None:\n",
        after="            if acknowledged_at is None:\n",
        tests=(
            "test_repeated_acknowledgment_is_idempotent",
            "test_handoff_requires_acknowledgment",
        ),
    ),
    Mutation(
        name="invert_request_line_bound",
        path="tools/agent_workspace_mcp.py",
        before="        if len(line) > MAX_RESPONSE_BYTES:\n",
        after="        if len(line) < MAX_RESPONSE_BYTES:\n",
        tests=(
            "test_stdio_rejects_bad_requests_and_continues",
            "test_stdio_bounds_complete_response_and_request_line",
        ),
    ),
    Mutation(
        name="drop_structured_tool_result",
        path="tools/agent_workspace_mcp.py",
        before='            "structuredContent": structured_result,\n',
        after='            "structuredContent": {},\n',
        tests=("test_two_clients_exchange_checkpoint",),
    ),
)

SURVIVOR_CONTROL = Mutation(
    name="harmless_docstring_survivor",
    path="tools/agent_workspace.py",
    before=(
        '"""Local coordination workspace for Claude and Codex."""\n'
    ),
    after='"""Local coordination workspace for two agents."""\n',
    tests=("test_queries_treat_input_as_data",),
)


class _BinaryInput:
    """Provide a binary buffer compatible with the stdio adapter."""

    def __init__(self, payload: bytes) -> None:
        self.buffer = io.BytesIO(payload)


class _BinaryOutput:
    """Capture a binary buffer compatible with the stdio adapter."""

    def __init__(self) -> None:
        self.buffer = io.BytesIO()


def _run_command(
    label: str,
    command: list[str],
    *,
    expected_exit: int = 0,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one visible command and require its expected exit code."""
    print(f"\n[{label}] {' '.join(command)}", flush=True)
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        check=False,
    )
    if completed.returncode != expected_exit:
        raise GauntletError(
            f"{label} exited {completed.returncode}, expected "
            f"{expected_exit}"
        )
    return completed


def _clean_build_directory() -> None:
    """Remove only the gauntlet's validated ignored output directory."""
    resolved = BUILD_DIRECTORY.resolve()
    if PROJECT_ROOT.resolve() not in resolved.parents:
        raise GauntletError("gauntlet build directory escaped project root")
    if resolved.name != "agent-workspace-gauntlet":
        raise GauntletError("unexpected gauntlet build directory")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


def _pytest_command(*tests: str, base_name: str) -> list[str]:
    """Build one isolated pytest command."""
    base_temp = BUILD_DIRECTORY / base_name
    return [
        sys.executable,
        "-m",
        "pytest",
        *tests,
        "-q",
        "-p",
        "no:cacheprovider",
        f"--basetemp={base_temp}",
    ]


def _claim_once(
    database_path: Path,
    agent: str,
    barrier: Barrier,
) -> str:
    """Race one synthetic client for one task claim."""
    workspace = Workspace(database_path)
    barrier.wait()
    try:
        workspace.claim_task("task-1", agent, expected_version=0)
    except ConflictError:
        return "conflict"
    return "claimed"


def run_stress() -> None:
    """Run deterministic randomized concurrent claim stress loops."""
    generator = random.Random(STRESS_SEED)
    with tempfile.TemporaryDirectory(
        prefix="agent-workspace-stress-"
    ) as directory:
        root = Path(directory)
        for round_number in range(STRESS_ROUNDS):
            database_path = root / f"round-{round_number}.db"
            workspace = Workspace(database_path)
            workspace.initialize()
            workspace.create_run("run-1", "Synthetic stress run")
            workspace.create_task("task-1", "run-1", "Stress claim")

            agents = [
                f"agent-{index}" for index in range(STRESS_CLIENTS)
            ]
            generator.shuffle(agents)
            barrier = Barrier(STRESS_CLIENTS)
            with ThreadPoolExecutor(
                max_workers=STRESS_CLIENTS
            ) as executor:
                futures = [
                    executor.submit(
                        _claim_once,
                        database_path,
                        agent,
                        barrier,
                    )
                    for agent in agents
                ]
                results = [future.result() for future in futures]
            if results.count("claimed") != 1:
                raise GauntletError(
                    f"stress round {round_number} had "
                    f"{results.count('claimed')} winners"
                )
            if results.count("conflict") != STRESS_CLIENTS - 1:
                raise GauntletError(
                    f"stress round {round_number} lost conflicts"
                )
    print(
        f"stress passed: {STRESS_ROUNDS} rounds, "
        f"{STRESS_CLIENTS} clients, seed {STRESS_SEED}"
    )


def _rpc_request(
    request_id: Any,
    method: str,
    params: dict[str, Any],
) -> bytes:
    """Encode one bounded JSON-RPC request line."""
    request = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }
    return json.dumps(request, separators=(",", ":")).encode() + b"\n"


def run_coverage_probe() -> None:
    """Exercise stdio branches in-process for coverage attribution."""
    from tools import agent_workspace_mcp

    with tempfile.TemporaryDirectory(
        prefix="agent-workspace-coverage-"
    ) as directory:
        database_path = Path(directory) / "workspace.db"
        oversized_line = b"x" * (MAX_RESPONSE_BYTES + 1) + b"\n"
        invalid_utf8 = b"\xff\n"
        malformed_json = b"{not-json}\n"
        non_object = b"[]\n"
        invalid_envelope = b'{"jsonrpc":"1.0","id":1}\n'
        missing_method = b'{"jsonrpc":"2.0","id":5}\n'
        invalid_id = (
            b'{"jsonrpc":"2.0","id":true,"method":"tools/list"}\n'
        )
        notification = (
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        )
        initialize_notification = (
            b'{"jsonrpc":"2.0","method":"initialize","params":{}}\n'
        )
        create_run = _rpc_request(
            2,
            "tools/call",
            {
                "name": "create_run",
                "arguments": {
                    "run_id": "run-1",
                    "label": "Synthetic coverage run",
                },
            },
        )
        invalid_call = _rpc_request(
            3,
            "tools/call",
            {
                "name": "create_run",
                "arguments": {"run_id": "/", "label": "Invalid"},
            },
        )
        list_tools = _rpc_request(4, "tools/list", {})
        huge_identifier = _rpc_request("x" * 65_450, "tools/list", {})
        payload = b"".join(
            (
                oversized_line,
                invalid_utf8,
                malformed_json,
                non_object,
                invalid_envelope,
                missing_method,
                invalid_id,
                notification,
                initialize_notification,
                create_run,
                invalid_call,
                list_tools,
                huge_identifier,
            )
        )

        input_stream = _BinaryInput(payload)
        output_stream = _BinaryOutput()
        prior_input = sys.stdin
        prior_output = sys.stdout
        prior_database = os.environ.get("BITCRACKER_AGENT_WORKSPACE")
        try:
            sys.stdin = input_stream
            sys.stdout = output_stream
            os.environ["BITCRACKER_AGENT_WORKSPACE"] = str(database_path)
            result = agent_workspace_mcp.main()
        finally:
            sys.stdin = prior_input
            sys.stdout = prior_output
            if prior_database is None:
                os.environ.pop("BITCRACKER_AGENT_WORKSPACE", None)
            else:
                os.environ["BITCRACKER_AGENT_WORKSPACE"] = prior_database

        if result != 0:
            raise GauntletError("coverage probe stdio process failed")
        lines = output_stream.buffer.getvalue().splitlines()
        if len(lines) != 11:
            raise GauntletError(
                f"coverage probe returned {len(lines)} responses"
            )
        if any(len(line) > MAX_RESPONSE_BYTES for line in lines):
            raise GauntletError("coverage probe found oversized response")
        responses = [json.loads(line) for line in lines]
        if responses[-1].get("id") is not None:
            raise GauntletError("oversized response identifier was retained")

        workspace = Workspace(Path(directory) / "acknowledgment.db")
        workspace.initialize()
        workspace.create_run("run-1", "Synthetic acknowledgment run")
        workspace.create_task("task-1", "run-1", "Acknowledge safely")

        invalid_hash = workspace.publish_handoff(
            "handoff-invalid-hash",
            "run-1",
            "task-1",
            "codex",
            "claude",
            "# Invalid hash probe\n",
        )
        try:
            workspace.acknowledge_handoff(
                invalid_hash.handoff_id,
                "claude",
                "invalid",
            )
        except ValueError:
            pass
        else:
            raise GauntletError("invalid acknowledgment hash was accepted")

        missing = workspace.publish_handoff(
            "handoff-missing",
            "run-1",
            "task-1",
            "codex",
            "claude",
            "# Missing file probe\n",
        )
        missing_path = workspace.handoff_directory / "handoff-missing.md"
        missing_path.unlink()
        try:
            workspace.acknowledge_handoff(
                missing.handoff_id,
                "claude",
                missing.sha256,
            )
        except ConflictError:
            pass
        else:
            raise GauntletError("missing handoff file was acknowledged")

        oversized = workspace.publish_handoff(
            "handoff-oversized",
            "run-1",
            "task-1",
            "codex",
            "claude",
            "# Oversized file probe\n",
        )
        oversized_path = (
            workspace.handoff_directory / "handoff-oversized.md"
        )
        oversized_path.write_bytes(b"x" * (MAX_SHARED_TEXT_BYTES + 1))
        try:
            workspace.acknowledge_handoff(
                oversized.handoff_id,
                "claude",
                oversized.sha256,
            )
        except ConflictError:
            pass
        else:
            raise GauntletError("oversized handoff file was acknowledged")

        unreadable = workspace.publish_handoff(
            "handoff-unreadable",
            "run-1",
            "task-1",
            "codex",
            "claude",
            "# Unreadable file probe\n",
        )
        with mock.patch.object(
            Path,
            "read_bytes",
            side_effect=OSError("synthetic read failure"),
        ):
            try:
                workspace.acknowledge_handoff(
                    unreadable.handoff_id,
                    "claude",
                    unreadable.sha256,
                )
            except ConflictError:
                pass
            else:
                raise GauntletError(
                    "unreadable handoff file was acknowledged"
                )


def _changed_lines(path: str) -> set[int]:
    """Return added line numbers relative to the approved base commit."""
    completed = subprocess.run(
        [
            "git",
            "diff",
            "--no-ext-diff",
            "--unified=0",
            BASE_COMMIT,
            "--",
            str(PROJECT_ROOT / path),
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise GauntletError(f"git diff failed for {path}")
    changed = set()
    pattern = re.compile(
        r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)"
        r"(?:,(?P<count>\d+))? @@"
    )
    for line in completed.stdout.splitlines():
        match = pattern.match(line)
        if match is None:
            continue
        start = int(match.group("start"))
        count = int(match.group("count") or "1")
        changed.update(range(start, start + count))
    if not changed:
        raise GauntletError(f"no changed lines found for {path}")
    return changed


def _enforce_coverage(covered: int, total: int) -> float:
    """Enforce the approved nonzero changed-line coverage threshold."""
    if total <= 0:
        raise GauntletError("changed-line coverage found no executable lines")
    percentage = covered * 100.0 / total
    if percentage < COVERAGE_MINIMUM:
        raise GauntletError(
            f"changed-line coverage {percentage:.2f}% is below "
            f"{COVERAGE_MINIMUM:.2f}%"
        )
    return percentage


def check_changed_line_coverage() -> None:
    """Compare final coverage data to implementation diff line numbers."""
    report = json.loads(COVERAGE_JSON.read_text(encoding="utf-8"))
    total_covered = 0
    total_measurable = 0
    for path in IMPLEMENTATION_PATHS:
        file_data = next(
            (
                data
                for reported_path, data in report["files"].items()
                if reported_path.replace("\\", "/") == path
            ),
            None,
        )
        if file_data is None:
            raise GauntletError(f"coverage report omitted {path}")
        executed = set(file_data["executed_lines"])
        missing = set(file_data["missing_lines"])
        measurable = _changed_lines(path) & (executed | missing)
        covered = measurable & executed
        missed = sorted(measurable & missing)
        total_covered += len(covered)
        total_measurable += len(measurable)
        print(
            f"coverage {path}: {len(covered)}/{len(measurable)} "
            f"changed executable lines"
        )
        if missed:
            print(f"coverage misses {path}: {missed}")
    percentage = _enforce_coverage(total_covered, total_measurable)
    print(
        f"changed-line coverage passed: {total_covered}/"
        f"{total_measurable}, {percentage:.2f}%"
    )


def run_coverage() -> None:
    """Collect branch coverage and enforce changed-line coverage."""
    BUILD_DIRECTORY.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["COVERAGE_FILE"] = str(COVERAGE_DATA)
    source = ",".join(
        path.removesuffix(".py").replace("/", ".")
        for path in IMPLEMENTATION_PATHS
    )
    _run_command(
        "coverage tests",
        [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--branch",
            f"--source={source}",
            "-m",
            "pytest",
            "tests/test_agent_workspace.py",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--basetemp={BUILD_DIRECTORY / 'pytest-coverage'}",
        ],
        environment=environment,
    )
    _run_command(
        "coverage stdio probe",
        [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--append",
            "--branch",
            f"--source={source}",
            "tests/agent_workspace_gauntlet.py",
            "--coverage-probe",
        ],
        environment=environment,
    )
    _run_command(
        "coverage JSON",
        [
            sys.executable,
            "-m",
            "coverage",
            "json",
            "-o",
            str(COVERAGE_JSON),
        ],
        environment=environment,
    )
    check_changed_line_coverage()


def _mutation_environment(name: str) -> dict[str, str]:
    """Return an isolated no-cache Python environment for one mutant."""
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPYCACHEPREFIX"] = str(
        BUILD_DIRECTORY / "pycache" / name
    )
    return environment


def _apply_mutation(mutation: Mutation, source: str) -> str:
    """Apply one unique exact source replacement."""
    occurrences = source.count(mutation.before)
    if occurrences != 1:
        raise GauntletError(
            f"{mutation.name} matched {occurrences} sites, expected 1"
        )
    mutated = source.replace(mutation.before, mutation.after, 1)
    if mutated == source:
        raise GauntletError(f"{mutation.name} did not alter source")
    return mutated


def _run_mutation(mutation: Mutation) -> bool:
    """Apply, execute, and restore one mutant, returning whether killed."""
    BUILD_DIRECTORY.mkdir(parents=True, exist_ok=True)
    path = PROJECT_ROOT / mutation.path
    original_bytes = path.read_bytes()
    original_hash = hashlib.sha256(original_bytes).hexdigest()
    source = original_bytes.decode("utf-8")
    mutated = _apply_mutation(mutation, source)
    mutated_bytes = mutated.encode("utf-8")
    mutated_hash = hashlib.sha256(mutated_bytes).hexdigest()
    if mutated_hash == original_hash:
        raise GauntletError(f"{mutation.name} retained original hash")

    nodes = [
        f"tests/test_agent_workspace.py::{test_name}"
        for test_name in mutation.tests
    ]
    command = _pytest_command(
        *nodes,
        base_name=f"pytest-mutant-{mutation.name}",
    )
    junit_path = BUILD_DIRECTORY / f"mutant-{mutation.name}.xml"
    command.append(f"--junitxml={junit_path}")
    command.append("--tb=no")
    command.insert(1, "-B")
    try:
        path.write_bytes(mutated_bytes)
        if hashlib.sha256(path.read_bytes()).hexdigest() != mutated_hash:
            raise GauntletError(f"{mutation.name} was not written")
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=_mutation_environment(mutation.name),
            text=True,
            check=False,
        )
        if not junit_path.is_file():
            raise GauntletError(
                f"{mutation.name} produced no JUnit execution report"
            )
        report = ElementTree.parse(junit_path).getroot()
        suites = (
            [report]
            if report.tag == "testsuite"
            else report.findall(".//testsuite")
        )
        failures = sum(
            int(suite.attrib.get("failures", "0")) for suite in suites
        )
        errors = sum(
            int(suite.attrib.get("errors", "0")) for suite in suites
        )
        if errors:
            raise GauntletError(
                f"{mutation.name} had {errors} pytest execution errors"
            )
        if completed.returncode not in {0, 1}:
            raise GauntletError(
                f"{mutation.name} pytest exited {completed.returncode}"
            )
        killed = completed.returncode == 1 and failures > 0
    finally:
        path.write_bytes(original_bytes)
    restored_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if restored_hash != original_hash:
        raise GauntletError(f"{mutation.name} source restore failed")
    outcome = "killed" if killed else "survived"
    print(
        f"mutation {mutation.name}: {outcome}, "
        f"source {mutated_hash[:12]}"
    )
    return killed


def run_mutations(mutations: tuple[Mutation, ...]) -> None:
    """Require every supplied manual mutant to be killed."""
    killed = 0
    for mutation in mutations:
        if _run_mutation(mutation):
            killed += 1
    if killed != len(mutations):
        raise GauntletError(
            f"manual mutation killed {killed}/{len(mutations)}"
        )
    print(f"manual mutation passed: {killed}/{len(mutations)} killed")


def check_capabilities(names: set[str]) -> None:
    """Fail closed if an advertised tool has a prohibited capability."""
    prohibited = sorted(names & FORBIDDEN_CAPABILITIES)
    if prohibited:
        raise GauntletError(
            f"prohibited MCP capabilities advertised: {prohibited}"
        )


def _secret_patterns() -> tuple[re.Pattern[str], ...]:
    """Build secret patterns without embedding a matching credential."""
    aws_prefix = "A" + "KIA"
    private_header = "-----" + "BEGIN "
    return (
        re.compile(aws_prefix + r"[0-9A-Z]{16}"),
        re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
        re.compile(
            private_header
            + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
        ),
        re.compile(
            r"(?i)(?:api[_-]?key|credential|password|secret)"
            r"\s*[:=]\s*['\"][^'\"\n]{8,}['\"]"
        ),
    )


def check_secret_text(text: str, label: str) -> None:
    """Fail closed when text contains a likely embedded credential."""
    for pattern in _secret_patterns():
        if pattern.search(text):
            raise GauntletError(
                f"secret-pattern scan matched {pattern.pattern!r} "
                f"in {label}"
            )


def _added_diff_text() -> str:
    """Return only added text in the full change from the approved base."""
    completed = subprocess.run(
        [
            "git",
            "diff",
            "--no-ext-diff",
            "--unified=0",
            BASE_COMMIT,
            "--",
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise GauntletError("final diff secret scan could not read diff")
    return "\n".join(
        line[1:]
        for line in completed.stdout.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )


def run_privacy_and_capability_checks() -> None:
    """Check the real catalog and the final added diff."""
    definitions = tool_definitions()
    names = {definition["name"] for definition in definitions}
    if tuple(definition["name"] for definition in definitions) != (
        ALLOWED_TOOL_NAMES
    ):
        raise GauntletError("MCP tool catalog order changed")
    check_capabilities(names)
    check_secret_text(_added_diff_text(), "final added diff")
    print("privacy and capability checks passed")


def run_negative_control(name: str) -> None:
    """Feed one known-bad input to a custom checker."""
    if name == "privacy":
        synthetic = "password" + " = 'synthetic-not-a-real-secret'"
        check_secret_text(synthetic, "privacy negative control")
        return
    if name == "capability":
        check_capabilities(set(ALLOWED_TOOL_NAMES) | {"approve_gate"})
        return
    if name == "coverage":
        _enforce_coverage(0, 1)
        return
    raise GauntletError(f"unknown negative control {name!r}")


def run_gauntlet() -> None:
    """Run every approved reproducible gauntlet layer."""
    _clean_build_directory()
    print(f"source base: {BASE_COMMIT}")
    print(f"python: {sys.version.split()[0]}")
    _run_command(
        "randomized concurrent stress",
        [sys.executable, __file__, "--stress"],
    )
    _run_command(
        "Ruff",
        [sys.executable, "-m", "ruff", "check", *RUFF_PATHS],
    )
    run_coverage()
    _run_command(
        "mutation survivor negative control",
        [sys.executable, __file__, "--mutation-negative-control"],
        expected_exit=1,
    )
    run_mutations(MUTATIONS)
    for control in ("privacy", "capability", "coverage"):
        _run_command(
            f"{control} negative control",
            [
                sys.executable,
                __file__,
                "--negative-control",
                control,
            ],
            expected_exit=1,
        )
    _run_command(
        "real two-process stdio smoke",
        _pytest_command(
            "tests/test_agent_workspace.py::"
            "test_two_clients_exchange_checkpoint",
            base_name="pytest-stdio-smoke",
        ),
    )
    _run_command(
        "backup and restore drill",
        _pytest_command(
            "tests/test_agent_workspace.py::"
            "test_backup_round_trip_preserves_state",
            "tests/test_agent_workspace.py::"
            "test_corrupt_backup_fails_closed",
            base_name="pytest-backup-drill",
        ),
    )
    run_privacy_and_capability_checks()
    _run_command(
        "complete maintained suite after mutation restore",
        _pytest_command("tests", base_name="pytest-full-final"),
    )
    print("\nagent workspace gauntlet passed")


def _parse_arguments() -> argparse.Namespace:
    """Parse gauntlet operation selection."""
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--stress", action="store_true")
    group.add_argument("--coverage", action="store_true")
    group.add_argument("--coverage-probe", action="store_true")
    group.add_argument("--mutations", action="store_true")
    group.add_argument("--secret-scan", action="store_true")
    group.add_argument(
        "--mutation-negative-control",
        action="store_true",
    )
    group.add_argument(
        "--negative-control",
        choices=("privacy", "capability", "coverage"),
    )
    return parser.parse_args()


def main() -> int:
    """Run the selected gauntlet operation and fail closed."""
    arguments = _parse_arguments()
    try:
        if arguments.stress:
            run_stress()
        elif arguments.coverage:
            run_coverage()
        elif arguments.coverage_probe:
            run_coverage_probe()
        elif arguments.mutations:
            run_mutations(MUTATIONS)
        elif arguments.secret_scan:
            run_privacy_and_capability_checks()
        elif arguments.mutation_negative_control:
            run_mutations((SURVIVOR_CONTROL,))
        elif arguments.negative_control:
            run_negative_control(arguments.negative_control)
        else:
            run_gauntlet()
    except (GauntletError, AssertionError) as error:
        print(f"GAUNTLET FAILED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
