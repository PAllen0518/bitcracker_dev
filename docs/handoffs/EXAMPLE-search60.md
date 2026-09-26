# Example handoff (illustrative, not maintained)

Illustration of the handoff template filled in, based on Codex's read-only
inspection on 2026-09-26. This file is a teaching example only. It is not a real
checkpoint and is not kept up to date. Do not rely on its numbers.

---

# Checkpoint: Search60 read-only inspection

## 1. Metadata

- Checkpoint ID: `2026-09-26-01-codex`
- Written (America/Denver): `2026-09-26 15:10`
- Author agent: `Codex`
- Intended recipient: `human`
- Approved specification hash: `n/a` (spec not yet approved)
- Old Coder phase: `SPEC`
- Git HEAD: `a01d2b6` on branch `master` (no isolated worktrees exist yet)
- Working tree: `dirty` (user-modified `.gitignore`, plus untracked search logs
  and checkpoint backups, see warnings)
- Coordination DB run ID: `n/a` (workspace not built yet)

## 2. What was done this session

- Read-only inspection of repo, git state, token workflow, tests, and logs.
  Evidence: `git status`, `git log -1`, `git worktree list`, each run under a
  per-command `safe.directory` override. No config changed, no files written.
- Identified checkpoint-integrity gaps in `multibit_cuda_threads.cu`.
  Evidence: read of save/restore code; restore compares recomputed combination
  count only.

## 3. Recovery state (no secrets)

- Active search: `Search60`
- Progress: `125,104,074,844 / 164,206,490,176` (`76.19%`)
- Passwords checked: `~90.075T`
- Checkpoint file: `cuda_threads_save60.bin`, sha256 recorded separately, last
  write `2026-09-26 14:47`
- Token-list file: `search60.txt`, sha256 recorded separately (contents NOT
  reproduced)
- Recovery output present: `no` (`RECOVERED_PASSWORD.txt` absent)
- Recovery process running: `no`
- Classification: `incomplete` (combo_idx < total_combos; not a completed
  failure)

## 4. Changed / relevant files

- None (read-only session).

## 5. Open approval gates (human-only)

- [ ] Gate: `Run outcome validated` (`waiting`), blocks: any failure-analysis
      workflow. Search is at 76 percent, so it is NOT complete.
- [ ] Gate: `Specification approved` (`waiting`), blocks: building the
      coordination workspace.

## 6. Next actions for the picking-up agent

1. Do NOT run failure analysis. Search60 is incomplete, not failed.
2. Decide with Paul: resume Search60 to completion, or fix native checkpoint
   integrity first (recommended, since a resumed run currently cannot prove it
   is searching the same space). Native checkpoint hardening is its own spec.
3. Hold on building the workspace until the spec is approved.

## 7. Blockers and warnings

- Untracked artifacts that must NOT enter any worktree or commit: `search60.txt`,
  `cuda_threads_save60.bin`, `search60*.log`, local `.gitignore` change.
- Known gap: restore validates combination count only, not token-list hash,
  wallet identity, delimiter, or typo flags. Saves are not written atomically.
- Risk: a resumed run against a changed token list with the same combo count
  would pass validation and silently search the wrong space.

## 8. Verification the next agent must run

- `git status --short --branch` (expect a dirty tree as above).
- Re-hash `cuda_threads_save60.bin` and `search60.txt`; confirm unchanged.
- Confirm `RECOVERED_PASSWORD.txt` still absent before assuming not-found.
- If any code changed since this checkpoint, re-run `python -m pytest tests/ -v`.
