# Agent Handoff Checkpoint (template)

Template both Claude and Codex fill in when handing work off to the other agent
or pausing before a human approval gate. One file per checkpoint. Never contains
secrets.

For when to publish, immutability, acknowledgment, and the required-field
contract, follow `AGENT_WORKSPACE_SPEC.md` (Mandatory checkpoint handoffs). This
template is the human-readable form; the spec is authoritative.

## Two tiers

- Runtime handoff: untracked, under the shared runtime directory, hashed and
  registered in the coordination database. For live agent-to-agent transfer.
- Milestone handoff: sanitized, tracked at `docs/handoffs/<UTC-date>-<seq>-<agent>.md`
  for durable project history.

A filled-in illustration is kept separately at
`docs/handoffs/EXAMPLE-search60.md` so this template does not go stale.

## Secret rule (non-negotiable)

Never write into a handoff: the recovered password, any token-list contents,
wallet contents, candidate strings, personal hints, private keys, or seed
phrases, or file paths that reveal them. Refer to artifacts by name and hash
only.

---

# Checkpoint: <short title>

## 1. Metadata

- Checkpoint ID: `<UTC-date>-<seq>-<agent>`
- Written (America/Denver): `YYYY-MM-DD HH:MM`
- Author agent: `Claude | Codex`
- Intended recipient: `Claude | Codex | human`
- Approved specification hash: `<sha256 or n/a>`
- Old Coder phase: `SPEC | RED | GREEN | REFACTOR | GAUNTLET | EVIDENCE`
- Git HEAD: `<sha>` on branch/worktree `<name>`
- Working tree: `clean | dirty` (list tracked files changed in section 4)
- Coordination DB run ID (if applicable): `<id or n/a>`

## 2. What was done this session

Concrete changes. Each line claiming a result carries its evidence (command run
plus actual output), per the Old Coder EVIDENCE rule.

- ...

## 3. Recovery state (no secrets)

- Active search: `<name, e.g. Search60>`
- Progress: `<combos done> / <total>` (`<pct>%`)
- Passwords checked: `<count>`
- Checkpoint file: `<name>`, sha256 `<hash>`, last write `<time>`
- Token-list file: `<name>`, sha256 `<hash>` (contents NOT reproduced here)
- Recovery output present: `yes | no`
- Recovery process running: `yes | no`
- Classification (spec vocabulary): `running | incomplete | complete_not_found | recovered | failed | unknown`. Conflicting or uncertain evidence fails closed to `unknown`.

## 4. Changed / relevant files

Tracked files touched this session (path plus one-line reason). Do not list
untracked recovery artifacts here except to warn about them.

- ...

## 5. Open approval gates (human-only)

Which gates from `AGENT_WORKSPACE_SPEC.md` are waiting on Paul. Mark the one that
blocks the next step.

- [ ] Gate: `<name>` (`waiting | cleared`), blocks: `<what>`

## 6. Next actions for the picking-up agent

Ordered and specific. The first item is what to do next.

1. ...

## 7. Blockers and warnings

- Untracked artifacts that must NOT enter any worktree or commit: `<list>`
- Known bugs or risks relevant to the next step: `<list>`
- Anything the next agent could break by accident: `<list>`

## 8. Verification the next agent must run

Commands the incoming agent runs to confirm state before trusting it. Prior
claims are not evidence for the new session.

- `git -c safe.directory=<repo> -C <repo> status --short --branch`
- `git -c safe.directory=<repo> -C <repo> log -1 --format='%H %s'`
- Re-hash the checkpoint and token-list files; confirm they match section 3.
- Re-run the test suite (`python -m pytest tests/ -v`) if code changed.
- ...
