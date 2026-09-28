"""Bounded stdio MCP adapter for the local agent workspace."""

import copy
import json
import os
import re
import sqlite3
import sys
from collections.abc import Iterator
from dataclasses import asdict
from typing import Any

from tools.agent_workspace import Workspace, WorkspaceError

MAX_PAGE_SIZE = 100
MAX_RESPONSE_BYTES = 65_536
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "bitcracker-agent-workspace", "version": "1"}
ALLOWED_TOOL_NAMES = (
    "create_run",
    "create_task",
    "list_tasks",
    "claim_task",
    "transition_task",
    "register_artifact",
    "publish_handoff",
    "acknowledge_handoff",
)

_TOOL_DEFINITIONS = (
    {
        "name": "create_run",
        "description": "Create one metadata-only coordination run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "maxLength": 128},
                "label": {"type": "string", "maxLength": 4096},
            },
            "required": ["run_id", "label"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_task",
        "description": "Create one open coordination task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "maxLength": 128},
                "run_id": {"type": "string", "maxLength": 128},
                "title": {"type": "string", "maxLength": 4096},
            },
            "required": ["task_id", "run_id", "title"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_tasks",
        "description": "List at most 100 bounded task records.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_PAGE_SIZE,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "claim_task",
        "description": "Atomically claim an open task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "maxLength": 128},
                "agent": {"type": "string", "maxLength": 128},
                "expected_version": {"type": "integer", "minimum": 0},
            },
            "required": ["task_id", "agent", "expected_version"],
            "additionalProperties": False,
        },
    },
    {
        "name": "transition_task",
        "description": "Version-check and transition one task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "maxLength": 128},
                "new_state": {
                    "type": "string",
                    "enum": [
                        "open",
                        "claimed",
                        "in_progress",
                        "review",
                        "blocked",
                        "done",
                        "cancelled",
                    ],
                },
                "actor": {"type": "string", "maxLength": 128},
                "expected_version": {"type": "integer", "minimum": 0},
            },
            "required": [
                "task_id",
                "new_state",
                "actor",
                "expected_version",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "register_artifact",
        "description": "Register artifact metadata without content or paths.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "maxLength": 128},
                "run_id": {"type": "string", "maxLength": 128},
                "label": {"type": "string", "maxLength": 4096},
                "kind": {"type": "string", "maxLength": 128},
                "size_bytes": {"type": "integer", "minimum": 0},
                "sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
            },
            "required": [
                "artifact_id",
                "run_id",
                "label",
                "kind",
                "size_bytes",
                "sha256",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "publish_handoff",
        "description": "Publish one immutable runtime handoff.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "handoff_id": {"type": "string", "maxLength": 128},
                "run_id": {"type": "string", "maxLength": 128},
                "task_id": {
                    "type": ["string", "null"],
                    "maxLength": 128,
                },
                "author": {"type": "string", "maxLength": 128},
                "recipient": {"type": "string", "maxLength": 128},
                "supersedes_handoff_id": {
                    "type": ["string", "null"],
                    "maxLength": 128,
                },
                "content": {
                    "type": "string",
                    "maxLength": MAX_RESPONSE_BYTES,
                },
            },
            "required": [
                "handoff_id",
                "run_id",
                "task_id",
                "author",
                "recipient",
                "content",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "acknowledge_handoff",
        "description": "Acknowledge one exact immutable handoff hash.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "handoff_id": {"type": "string", "maxLength": 128},
                "recipient": {"type": "string", "maxLength": 128},
                "sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
            },
            "required": ["handoff_id", "recipient", "sha256"],
            "additionalProperties": False,
        },
    },
)
_TOOL_SCHEMAS = {
    definition["name"]: definition["inputSchema"]
    for definition in _TOOL_DEFINITIONS
}


class McpRequestError(ValueError):
    """Raised when an MCP tool request is unknown or invalid."""


class McpResponseTooLarge(McpRequestError):
    """Raised when a response would exceed the protocol output bound."""


def tool_definitions() -> list[dict[str, Any]]:
    """Return the bounded coordination tool catalog."""
    return copy.deepcopy(list(_TOOL_DEFINITIONS))


def dispatch_tool(
    workspace: Workspace,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Validate and dispatch one bounded coordination tool request."""
    validated = _validate_arguments(tool_name, arguments)

    if tool_name == "create_run":
        workspace.create_run(validated["run_id"], validated["label"])
        response = {"created": True, "run_id": validated["run_id"]}
    elif tool_name == "create_task":
        task = workspace.create_task(
            validated["task_id"],
            validated["run_id"],
            validated["title"],
        )
        response = {"task": asdict(task)}
    elif tool_name == "list_tasks":
        limit = validated.get("limit", MAX_PAGE_SIZE)
        response = {
            "tasks": [asdict(task) for task in workspace.list_tasks(limit)]
        }
    elif tool_name == "claim_task":
        task = workspace.claim_task(
            validated["task_id"],
            validated["agent"],
            validated["expected_version"],
        )
        response = {"task": asdict(task)}
    elif tool_name == "transition_task":
        task = workspace.transition_task(
            validated["task_id"],
            validated["new_state"],
            validated["actor"],
            validated["expected_version"],
        )
        response = {"task": asdict(task)}
    elif tool_name == "register_artifact":
        artifact = workspace.register_artifact(
            validated["artifact_id"],
            validated["run_id"],
            validated["label"],
            validated["kind"],
            validated["size_bytes"],
            validated["sha256"],
        )
        response = {"artifact": asdict(artifact)}
    elif tool_name == "publish_handoff":
        handoff = workspace.publish_handoff(
            validated["handoff_id"],
            validated["run_id"],
            validated["task_id"],
            validated["author"],
            validated["recipient"],
            validated["content"],
            validated.get("supersedes_handoff_id"),
        )
        response = {"handoff": asdict(handoff)}
    else:
        acknowledgment = workspace.acknowledge_handoff(
            validated["handoff_id"],
            validated["recipient"],
            validated["sha256"],
        )
        response = {"acknowledgment": asdict(acknowledgment)}

    return _bound_response(response)


def _validate_arguments(
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Validate one request against its frozen tool input schema."""
    if not isinstance(tool_name, str) or tool_name not in _TOOL_SCHEMAS:
        raise McpRequestError("unknown MCP tool")
    if not isinstance(arguments, dict):
        raise McpRequestError("tool arguments must be an object")

    schema = _TOOL_SCHEMAS[tool_name]
    properties = schema["properties"]
    argument_names = set(arguments)
    unknown_names = argument_names - set(properties)
    if unknown_names:
        raise McpRequestError("tool request contains unknown fields")

    missing_names = set(schema["required"]) - argument_names
    if missing_names:
        raise McpRequestError("tool request is missing required fields")

    for name, value in arguments.items():
        _validate_value(name, value, properties[name])
    return dict(arguments)


def _validate_value(
    name: str,
    value: Any,
    schema: dict[str, Any],
) -> None:
    """Validate one bounded scalar supported by the frozen catalog."""
    expected_types = schema["type"]
    if isinstance(expected_types, str):
        expected_types = [expected_types]

    if not any(_matches_type(value, item) for item in expected_types):
        raise McpRequestError(f"{name} has an invalid type")
    if value is None:
        return

    if isinstance(value, str):
        if len(value) > schema.get("maxLength", len(value)):
            raise McpRequestError(f"{name} is too long")
        pattern = schema.get("pattern")
        if pattern is not None and re.fullmatch(pattern, value) is None:
            raise McpRequestError(f"{name} has an invalid format")
        choices = schema.get("enum")
        if choices is not None and value not in choices:
            raise McpRequestError(f"{name} has an invalid value")

    if isinstance(value, int) and not isinstance(value, bool):
        if value < schema.get("minimum", value):
            raise McpRequestError(f"{name} is below its minimum")
        if value > schema.get("maximum", value):
            raise McpRequestError(f"{name} exceeds its maximum")


def _matches_type(value: Any, expected_type: str) -> bool:
    """Match JSON scalar types without treating bool as integer."""
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return False


def _bound_response(response: dict[str, Any]) -> dict[str, Any]:
    """Fail closed when stable encoded output would exceed 64 KiB."""
    encoded = json.dumps(
        response,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise McpResponseTooLarge("MCP response exceeds 64 KiB")
    return response


def _workspace_from_environment() -> Workspace:
    """Bind one workspace to the database named by the environment."""
    database_path = os.environ.get("BITCRACKER_AGENT_WORKSPACE")
    if not database_path:
        raise RuntimeError("BITCRACKER_AGENT_WORKSPACE is not set")
    workspace = Workspace(database_path)
    workspace.initialize()
    return workspace


def _handle_request(
    workspace: Workspace,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Return one JSON-RPC result payload for a supported method."""
    method = request.get("method")
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }
    if method == "tools/list":
        return {"tools": tool_definitions()}
    if method == "tools/call":
        params = request.get("params")
        if not isinstance(params, dict):
            raise McpRequestError("tool call params must be an object")
        structured_result = dispatch_tool(
            workspace,
            params.get("name"),
            params.get("arguments", {}),
        )
        text_result = json.dumps(
            structured_result,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return {
            "content": [{"type": "text", "text": text_result}],
            "structuredContent": structured_result,
        }
    raise McpRequestError(f"unknown method {method!r}")


def _request_identity(request: Any) -> tuple[bool, Any]:
    """Validate a JSON-RPC request and return its id state."""
    if not isinstance(request, dict):
        raise McpRequestError("JSON-RPC request must be an object")
    if request.get("jsonrpc") != "2.0":
        raise McpRequestError("jsonrpc must be '2.0'")
    method = request.get("method")
    if not isinstance(method, str) or not method:
        raise McpRequestError("method must be a non-empty string")

    has_id = "id" in request
    request_id = request.get("id")
    valid_id = (
        request_id is None
        or isinstance(request_id, str)
        or (
            isinstance(request_id, int)
            and not isinstance(request_id, bool)
        )
    )
    if has_id and not valid_id:
        raise McpRequestError("id must be a string, integer, or null")
    return has_id, request_id


def _encode_response(response: dict[str, Any]) -> bytes:
    """Encode one complete JSON-RPC response within the output bound."""
    encoded = json.dumps(
        response,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise McpResponseTooLarge("JSON-RPC response exceeds 64 KiB")
    return encoded


def _error_response(
    request_id: Any,
    code: int,
    message: str,
) -> bytes:
    """Return a bounded JSON-RPC error, dropping an oversized id."""
    response = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
    try:
        return _encode_response(response)
    except McpResponseTooLarge:
        response["id"] = None
        response["error"]["message"] = "JSON-RPC response exceeds 64 KiB"
        return _encode_response(response)


def _write_response(encoded: bytes) -> None:
    """Write and flush one encoded JSON-RPC response line."""
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


def _read_request_lines() -> Iterator[bytes | None]:
    """Yield bounded request lines and mark oversized physical lines."""
    while True:
        line = sys.stdin.buffer.readline(MAX_RESPONSE_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_RESPONSE_BYTES:
            while line and not line.endswith(b"\n"):
                line = sys.stdin.buffer.readline(MAX_RESPONSE_BYTES + 1)
            yield None
            continue
        yield line


def _process_request(
    workspace: Workspace,
    request: dict[str, Any],
    has_id: bool,
    request_id: Any,
) -> bytes | None:
    """Process one valid envelope and encode any required response."""
    try:
        result = _handle_request(workspace, request)
        if not has_id:
            return None
        response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        return _encode_response(response)
    except (
        McpRequestError,
        WorkspaceError,
        ValueError,
        TypeError,
        sqlite3.Error,
        OSError,
    ) as error:
        if not has_id:
            return None
        return _error_response(request_id, -32000, str(error))


def main() -> int:
    """Run the bounded newline-delimited JSON-RPC stdio server."""
    workspace = _workspace_from_environment()
    for line in _read_request_lines():
        if line is None:
            _write_response(
                _error_response(
                    None,
                    -32600,
                    "JSON-RPC request line exceeds 64 KiB",
                )
            )
            continue

        stripped = line.strip()
        if not stripped:
            continue
        try:
            request_text = stripped.decode("utf-8")
            request = json.loads(request_text)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            _write_response(_error_response(None, -32700, str(error)))
            continue

        try:
            has_id, request_id = _request_identity(request)
        except McpRequestError as error:
            _write_response(_error_response(None, -32600, str(error)))
            continue

        encoded = _process_request(
            workspace,
            request,
            has_id,
            request_id,
        )
        if encoded is not None:
            _write_response(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
