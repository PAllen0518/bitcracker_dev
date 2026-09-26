# AGENTS.md

Guidance for Codex when working with code in this repository.

See `COLLABORATION.md` for the shared multi-agent collaboration standards (roles,
source-of-truth split, approval gates, worktree discipline, and the handoff
protocol) that apply to both Claude and Codex. For the authoritative detail on
handoffs, coordination database, gates, and run classification, follow
`AGENT_WORKSPACE_SPEC.md` once it is approved.

## Old Coder Methodology

For non-trivial coding tasks in this repo, follow the Old Coder loop:
SPEC -> RED -> GREEN -> REFACTOR -> GAUNTLET -> EVIDENCE.

- Get spec approval before writing code.
- Prove tests fail before making them pass (RED).
- Produce a reproducible evidence report (commands run, actual results) instead
  of asserting completion.
- Never weaken tests to pass; never report a gauntlet layer that wasn't
  actually run.
- Scale gauntlet rigor to risk (trivial change vs. high-stakes: auth, crypto,
  data handling).

Full reference: `~/.codex/skills/old-coder/SKILL.md` (gauntlet layers and
verifier protocol in `~/.codex/skills/old-coder/references/`).

Source: github.com/AmazingAng/old-coder (MIT)

## Claude and Codex Coordination

Both agents may contribute. The rules live in one place to avoid drift:

- General collaboration standards: `COLLABORATION.md`.
- Authoritative handoff, database, gate, and classification detail:
  `AGENT_WORKSPACE_SPEC.md` (source of truth once approved).

Do not restate those rules here.
