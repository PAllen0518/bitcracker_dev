# Agent Workspace Operations Guide

## Purpose

The agent workspace is a local coordination service for Claude Code and Codex.
It stores bounded, redacted metadata in SQLite so agents can share tasks,
artifact identities, and immutable handoffs without sharing recovery secrets.

Version 1 provides:

- a Python `Workspace` library;
- a newline-delimited JSON-RPC stdio MCP process;
- atomic task claims and version-checked state transitions;
- immutable, hashed handoff files with recipient acknowledgment;
- metadata-only artifact registration;
- conservative run classification; and
- verified SQLite backup and restore methods.

It does not run recovery, read artifact contents, approve gates, perform Git
operations, expose a network listener, or control ChatGPT Work.

## Safety boundary

Store coordination metadata only. Never place any of the following in the
database, handoffs, tool arguments, logs, or model-visible output:

- wallet contents, private keys, or seed phrases;
- token-list contents, personal hints, or candidate strings;
- recovered passwords or `RECOVERED_PASSWORD.txt` contents;
- credentials, API tokens, or signing passwords; or
- recovery artifact paths configured as sensitive.

This is an operational control, not a security boundary against a malicious
local process running with the user's filesystem permissions.

## Prerequisites

- Python 3.10 or newer.
- A runtime directory outside every Git worktree.
- The same absolute SQLite path configured for both agent processes.
- No additional runtime package or database server.

The implementation lives under `BitCracker/btcrecover-master`. Run the commands
below from that directory.

## Quick start

Choose a local runtime path outside all worktrees. The example is a
placeholder, not a prescribed backup location:

```powershell
$env:BITCRACKER_AGENT_WORKSPACE = `
  'C:\Path\Outside\Worktrees\agent-workspace\workspace.db'
python -m tools.agent_workspace_mcp
```

The process reads one JSON-RPC request per UTF-8 line from standard input and
writes one bounded JSON-RPC response line to standard output. An MCP client
should launch the command with the same `BITCRACKER_AGENT_WORKSPACE` value for
each agent.

The process creates and validates the SQLite schema on startup. Runtime handoff
files default to a sibling `handoffs` directory beside the database.

## Protocol startup

The supported MCP protocol version is `2025-06-18`. A client normally sends:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
```

Notifications contain no `id` and receive no response. Malformed requests,
invalid envelopes, invalid tool parameters, workspace conflicts, and expected
SQLite or I/O failures return bounded JSON-RPC errors. One bad request does not
terminate the process.

Every encoded request line and complete response is limited to 64 KiB. Task
listing is limited to 100 rows per call.

## MCP tool catalog

Version 1 advertises exactly these tools:

| Tool | Purpose |
| --- | --- |
| `create_run` | Create one metadata-only coordination run. |
| `create_task` | Add one open task to a run. |
| `list_tasks` | Return at most 100 bounded task records. |
| `claim_task` | Atomically claim one open task at an expected version. |
| `transition_task` | Apply one permitted version-checked state change. |
| `register_artifact` | Store label, kind, size, and SHA-256 only. |
| `publish_handoff` | Publish one immutable runtime handoff. |
| `acknowledge_handoff` | Acknowledge the exact recipient and SHA-256. |

The authoritative input schemas are returned by `tools/list`. Unknown tools,
unknown fields, and invalid JSON types fail closed.

## Typical transfer flow

1. Create a run and an open task.
2. Claim the task using its current version.
3. Transition it through the allowed states while recording checkpoints.
4. Publish a redacted handoff for the receiving agent.
5. Give the recipient the handoff ID and SHA-256 through the coordination
   channel, never the secret contents of a recovery artifact.
6. The recipient re-reads the handoff file and acknowledges that exact hash.
7. The recipient may then claim the transferred task at its expected version.

An unacknowledged latest handoff blocks the recipient's claim. A correction may
supersede a prior handoff only when run, task, and recipient all match. History
is preserved, and only the latest non-superseded handoff controls the claim
gate.

Repeated acknowledgment of the same exact handoff is idempotent. It returns the
original timestamp and appends no duplicate event.

## Task-state rules

The supported states are:

```text
open, claimed, in_progress, review, blocked, done, cancelled
```

Each mutation supplies the expected current version. A stale version, invalid
transition, duplicate active claim, or conflicting write fails without a
partial state change.

## Python library operations

Backup, restore, and run classification are Python library operations and are
not MCP tools. A local trusted process may import:

```python
from pathlib import Path

from tools.agent_workspace import Workspace

workspace = Workspace(Path(r"C:\Path\Outside\Worktrees\workspace.db"))
workspace.initialize()
```

`Workspace.create_backup(path)` uses SQLite's backup API, checks integrity,
hashes the result, and atomically publishes a manifest. `restore_backup(path)`
verifies the manifest, size, SHA-256, SQLite integrity, and foreign keys before
replacing the active database.

Do not select an external or off-machine backup destination without Paul's
separate approval. Until an approved off-machine destination exists, local
coordination data has reduced backup assurance.

Run classification accepts metadata evidence only. It must not start, stop,
resume, or edit a recovery run. Conflicting evidence returns `unknown`.

## Verification

From `BitCracker/btcrecover-master`, run the complete reproducible gauntlet:

```powershell
python tests/agent_workspace_gauntlet.py
```

The command recreates its ignored temporary output, runs deterministic
concurrency stress, Ruff, changed-line coverage, manual mutation, negative
controls, real two-process stdio, backup and restore drills, a secret scan, and
the maintained test suite.

Temporary output stays under:

```text
.cuda-build/agent-workspace-gauntlet
```

Do not commit `.cuda-build` contents.

## Troubleshooting

### `BITCRACKER_AGENT_WORKSPACE is not set`

Set the variable to the same absolute database path for both clients before
launching the stdio process.

### A task claim reports an unacknowledged handoff

Identify the latest non-superseded handoff for the task. The named recipient
must acknowledge its exact current SHA-256 before claiming the task.

### Handoff acknowledgment fails after a file edit

Published handoffs are immutable. Do not repair the old file. Publish a new
handoff that explicitly supersedes it and preserves the earlier record.

### A request produces a bounded error but the process remains open

This is expected fail-closed behavior. Correct the request and send a later
valid line through the same process.

### Backup protection is reduced

Local backup and restore behavior is verified, but no external destination is
selected automatically. Paul must approve an off-machine destination before
live coordination data is considered protected.

## Related records

- `AGENT_WORKSPACE_SPEC.md` is the approved behavioral contract.
- `AGENT_WORKSPACE_EVIDENCE.md` records the final validation results.
- `docs/handoffs/TEMPLATE.md` defines durable milestone handoff fields.
- `COLLABORATION.md` defines shared agent and Git ownership rules.
