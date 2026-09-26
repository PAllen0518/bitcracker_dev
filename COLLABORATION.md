# Project Standards: Multi-Agent Collaboration

How Claude and Codex work together on BitCracker_V2. This is the general
collaboration standard. For the authoritative detail on the coordination
database, checkpoint handoffs, approval gates, and run classification, follow
`AGENT_WORKSPACE_SPEC.md` (source of truth once approved). This file does not
restate that detail, to avoid drift.

`AGENTS.md` and `CLAUDE.md` reference this file rather than duplicate it.

## 1. Who does what

- Claude and Codex are both builders. Either may take builder, reviewer, or
  investigator work. No role is permanently assigned to one product.
- Paul (human) is the only actor who approves gates, confirms a run's outcome,
  authorizes a personal-wallet run, supplies sensitive inputs locally, and
  performs all Git history operations (stage, commit, merge, rebase, push,
  branch delete). Agents prepare changes; Paul lands them.
- When Paul denies an approval, the agent pauses and waits for clarification
  instead of taking an assumed next step. (Also in CLAUDE.md; applies to both.)

## 2. Source of truth

- Git is the source of truth for code and committed docs.
- For coordination and process state, the order defined in
  `AGENT_WORKSPACE_SPEC.md` applies: the approved spec and amendments, then the
  coordination database, then immutable checkpoint handoffs and their hashes,
  then the final evidence report.
- Agent chats, terminal scrollback, and editor tabs are not authoritative.

## 3. Old Coder loop (applies to both agents)

For non-trivial coding tasks: SPEC -> RED -> GREEN -> REFACTOR -> GAUNTLET ->
EVIDENCE. Get spec approval before code, prove RED before GREEN, and produce a
reproducible evidence report rather than a claim of completion. Never weaken
tests to pass. Scale rigor to risk; anything touching crypto, the checkpoint or
save format, or wallet handling is high-stakes. Full detail in `AGENTS.md`.

## 4. Approval gates (human-only)

No agent creates, approves, revokes, or bypasses a gate. The gates and their
binding definitions are in `AGENT_WORKSPACE_SPEC.md` (Human approval gates).
Each is a hard stop until Paul records approval through the command he runs
himself.

## 5. Worktrees and artifacts

- The primary checkout is the human-controlled integration checkout.
- Each agent works in its own worktree from the same approved base commit. One
  owner per task. A reviewer reads the implementer's diff and handoff without
  editing the implementer's worktree.
- Worktrees contain tracked project files only. Untracked recovery artifacts
  (`search*.txt`, `token*.txt`, `cuda_threads_save*.bin`, `savefile*`,
  `search*.log`, local `.gitignore` changes) stay out of every worktree and are
  never auto-merged, auto-committed, or copied between worktrees.
- The `.agent-workspace/` runtime directory is git-ignored and lives outside
  every worktree.

## 6. Handoffs (two tiers)

- Runtime handoffs: untracked, written under the shared runtime directory,
  hashed, and registered in the coordination database. Used for live agent-to-
  agent transfer. May contain local paths and operational metadata.
- Milestone handoffs: sanitized and tracked under `docs/handoffs/` for durable
  project history. No secrets, no sensitive local detail.
- Template: `docs/handoffs/TEMPLATE.md`. Milestone files are named
  `docs/handoffs/<UTC-date>-<seq>-<agent>.md`.
- When to publish, required fields, immutability, and acknowledgment are defined
  in `AGENT_WORKSPACE_SPEC.md` (Mandatory checkpoint handoffs). The incoming
  agent re-verifies critical state before trusting a handoff; prior claims are
  context, not proof.

## 7. Avoiding conflicts on the standards files

- Each standards file has one owner agent who makes structural edits.
- The other agent proposes changes through a handoff or a PR comment, not by
  editing the file directly.
- Shared logs (findings, session notes) are append-only with dated entries.

## 8. Secret handling (all shared surfaces)

Never write the recovered password, token-list contents, wallet contents,
candidate strings, personal hints, private keys, seed phrases, credentials, or
API tokens into the database, handoffs, findings, commits, logs, or any tool
output shared between agents. Refer to artifacts by name, size, and SHA-256 hash
only. The recovered password, when found, goes to Paul directly and never
through a tool or shared file. `RECOVERED_PASSWORD.txt` is never read or
returned.
