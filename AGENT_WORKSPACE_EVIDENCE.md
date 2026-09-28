# Agent Workspace Evidence Report

## Outcome

Status: **complete with declared limitations** for implementation commit
`6879d73e33652c8e658bcba51c71ee047f40de4e`.

The approved local coordination workspace is implemented, documented,
tested, and reproducible. The final fresh gauntlet passed. Claude completed
the required non-builder review and reported no issues. The stricter
experimental Old Coder independent-verifier protocol was not performed and
is not claimed.

This report is evidence of the approved constraints, not proof that the
specification is complete or a security boundary against a malicious local
process.

## Contract and source state

- Risk tier: Old Coder Tier 3 because the change handles concurrency, durable
  coordination state, backup and restore, and hostile protocol input.
- Gate 1 approval: Paul approved `AGENT_WORKSPACE_SPEC.md` on 2026-09-27.
- Approved contract hash:
  `7e2a99cce0b4f1357deaddb8c2e80274c65be909a238d56f71f6d1d5a177c627`.
- Amendment 1: Paul approved the review-correction contract on 2026-09-27.
- Current LF-normalized specification hash:
  `2df10eccd24a705d83c3abeb60de414988f668f1705ae58c3353829a323b535f`.
- Implementation commit:
  `6879d73e33652c8e658bcba51c71ee047f40de4e`.
- Implementation parent:
  `9a6a7212dd854ebc0bb069f70a2cbc2220d6715b`.
- Branch: `codex/agent-workspace-builder`.
- Reproducible entry point, from `BitCracker/btcrecover-master`:
  `python tests/agent_workspace_gauntlet.py`.

Tool versions in the final fresh run:

- Python 3.14.6
- pytest 8.4.1
- Ruff 0.16.0
- coverage.py 7.15.2 with C extension

No dependency was added or changed.

## Final implementation identity

LF-normalized SHA-256 values at commit `6879d73`:

- `AGENT_WORKSPACE_SPEC.md`:
  `2df10eccd24a705d83c3abeb60de414988f668f1705ae58c3353829a323b535f`
- `BitCracker/btcrecover-master/tests/test_agent_workspace.py`:
  `2f0911c8ae439755abc3d23e6dedee33b1c63fd74e09c527b6b731a913591396`
- `BitCracker/btcrecover-master/tests/agent_workspace_gauntlet.py`:
  `e5dc47c2afacbb85ea97e2b1716034106a3c48ffac1c371592de7d4b733eee9e`
- `BitCracker/btcrecover-master/tools/agent_workspace.py`:
  `daba14634679996613ebb35021a8925c9aca658d3e1ec080efee93313c9892b8`
- `BitCracker/btcrecover-master/tools/agent_workspace_mcp.py`:
  `55f9bacdce5322247344fce3cbd19d0dee642f1d317c6c051c6cd9339f1c8361`

The operational guide added after the implementation review has LF-normalized
SHA-256:

- `docs/AGENT_WORKSPACE.md`:
  `adf45d95ab7e279a065a1cb68f2c7de687e63056ff8ad207d1fab7f36fea2f20`

The evidence file identifies the implementation commit rather than claiming a
self-hash. Its exact hash is recorded in the final handoff.

## Acceptance-test mapping

All tests below are in `tests/test_agent_workspace.py`. Every row passed in the
final fresh gauntlet's 23-test focused coverage run and in the final maintained
suite of 175 tests.

- `test_schema_enforces_constraints`, invalid foreign keys, duplicate claims,
  missing fields, and invalid states fail without partial writes: **pass**.
- `test_queries_treat_input_as_data`, SQL-looking text remains data and cannot
  alter the schema or other rows: **pass**.
- `test_concurrent_claim_has_one_winner`, one of two racing claims wins and the
  other receives a conflict: **pass**.
- `test_stale_update_is_rejected`, an old version cannot overwrite newer task
  state: **pass**.
- `test_invalid_transition_rolls_back`, a rejected transition leaves task and
  event state unchanged: **pass**.
- `test_sensitive_content_is_rejected`, prohibited markers and configured
  sensitive paths fail before persistence: **pass**.
- `test_artifact_registration_stores_metadata_only`, only label, kind, size,
  and SHA-256 are stored: **pass**.
- `test_run_classification_requires_consensus`, complete-not-found requires all
  evidence to agree and conflicts return unknown: **pass**.
- `test_current_search_shape_is_incomplete`, partial checkpoint coordinates
  remain incomplete: **pass**.
- `test_backup_round_trip_preserves_state`, SQLite backup, integrity checks,
  restore, and row comparison preserve state: **pass**.
- `test_corrupt_backup_fails_closed`, damaged backup data cannot replace the
  active database: **pass**.
- `test_handoff_is_immutable_and_hashed`, publication creates one immutable
  file and database identity: **pass**.
- `test_handoff_requires_acknowledgment`, exact recipient and hash
  acknowledgment is required before a transferred-task claim: **pass**.
- `test_superseding_handoff_preserves_history`, a correction links its
  predecessor without rewriting history: **pass**.
- `test_latest_superseding_handoff_controls_claim_gate`, only the latest
  non-superseded handoff gates a claim: **pass**.
- `test_supersession_requires_same_run_task_and_recipient`, unrelated handoffs
  cannot be linked as corrections: **pass**.
- `test_acknowledgment_rehashes_published_file`, acknowledgment checks the
  current regular file and fails closed on changed data: **pass**.
- `test_repeated_acknowledgment_is_idempotent`, an exact repeat preserves the
  first timestamp and adds no duplicate event: **pass**.
- `test_stdio_rejects_bad_requests_and_continues`, malformed and invalid input
  returns bounded errors without ending the process: **pass**.
- `test_stdio_bounds_complete_response_and_request_line`, request lines and
  complete response envelopes remain at or below 64 KiB: **pass**.
- `test_two_clients_exchange_checkpoint`, two real stdio processes share one
  database and transfer one synthetic task using MCP results: **pass**.
- `test_mcp_has_no_approval_or_execution_tool`, the catalog exposes no
  approval, recovery execution, Git, deletion, or secret-reading tool:
  **pass**.
- `test_mcp_validates_and_bounds_requests`, invalid tools, fields, and types
  fail while pagination and structured output remain bounded: **pass**.

## Safeguard mapping

- Coordination metadata only: sensitive-content and metadata-only artifact
  tests, plus the final added-diff secret scan: **pass**.
- Parameterized SQL and constrained schema: schema and SQL-as-data tests:
  **pass**.
- Atomic claims and optimistic versions: concurrency, stale-update, and
  transition rollback tests plus 40 stress rounds: **pass**.
- Immutable, hash-bound handoffs: publication, acknowledgment, supersession,
  rehash, and idempotence tests: **pass**.
- Bounded hostile-input handling: stdio continuation, line, response, schema,
  and row-limit tests: **pass**.
- No privileged MCP capability: catalog test, capability checker, and its
  negative control: **pass**.
- SQLite API backup and fail-closed restore: round-trip, corruption, integrity,
  and live drill checks: **pass**.
- No network listener or external service: capability and implementation diff
  review: **pass within the approved local-process design**.
- No recovery execution or personal-data access: tool catalog, tests, diff, and
  milestone handoffs show only synthetic metadata use: **pass within reviewed
  project scope**.
- No automatic Git operations: no such tool exists, and Codex performed none.
  Paul alone created commit `6879d73`: **pass**.
- Operational documentation: `docs/AGENT_WORKSPACE.md` now records setup,
  protocol use, transfer flow, backup boundaries, verification, and failure
  recovery: **pass**.

## Final fresh gauntlet

Command:

```powershell
python tests/agent_workspace_gauntlet.py
```

Actual exit: **0**. Final output ended with
`agent workspace gauntlet passed`.

- Randomized concurrency: 40/40 rounds passed, eight clients per round, seed
  `20260927`; every round had one winner and seven conflicts.
- Ruff: exit 0, `All checks passed!`, over both implementation files, the test
  file, and the gauntlet helper.
- Focused coverage tests: 23 passed in 2.57 seconds.
- Changed-line coverage: 98/99, 98.99%, above the enforced 90% floor.
  `agent_workspace_mcp.py` covered 73/73 and `agent_workspace.py` covered 25/26
  changed executable lines.
- Mutation-runner control: the harmless docstring mutant survived, the runner
  returned exit 1, and byte-exact restoration succeeded.
- Manual mutation: 11/11 killed by JUnit-confirmed test failures with zero
  execution errors.
- Privacy checker control: exit 1 on a constructed synthetic credential.
- Capability checker control: exit 1 on synthetic `approve_gate`.
- Coverage checker control: exit 1 at 0/1 changed lines covered.
- Real execution: the two-process stdio smoke passed, 1 test in 0.60 seconds.
- Backup drill: valid round-trip and corrupt restore checks passed, 2 tests in
  0.29 seconds.
- Added-diff privacy and capability scan: passed.
- Maintained suite after all mutant restores: 175 passed in 32.73 seconds.

The eleven mutants covered claim uniqueness, prohibited approval capability,
sensitive-text rejection, run consensus, backup validation, latest handoff
gating, supersession lineage, handoff rehashing, acknowledgment idempotence,
request-line bounds, and MCP structured results.

Final documentation and generated-evidence scan:

```powershell
python tests/agent_workspace_gauntlet.py --secret-scan
```

Result after adding this report and the operations guide: exit **0**,
`privacy and capability checks passed`. The final scan after handoff 26 is
recorded in that handoff.

## Review and verification

### Required project review

Claude reviewed the exact implementation and gauntlet-helper state that Paul
then committed as `6879d73`. Verdict: **no issues found**.

Claude independently checked the repository rather than trusting checkpoint
25. The review confirmed:

- all 11 mutations are present;
- imports resolve;
- the eight-tool catalog matches `ALLOWED_TOOL_NAMES` and advertises zero
  forbidden capabilities;
- the coverage-probe response count is consistent with its payload;
- all eight checkpoint source hashes match the files on disk;
- HEAD, parent, branch, staging state, coverage arithmetic, and ignored
  `.cuda-build` behavior match checkpoint 25; and
- the worktree contained exactly the expected nine implementation files before
  Paul committed them.

Two robustness notes were classified as non-blocking: coverage keys are matched
by relative path, and the coverage probe expects exactly 11 responses. Both are
correct for the recorded environment but intentionally sensitive to future
coverage-path or notification behavior changes.

The operational guide and this evidence report were written after that review.
They change documentation only and are covered by formatting, diff, and secret
checks, not by Claude's implementation review.

### Experimental Old Coder verifier

Status: **not performed**.

There is no record that a fresh-context verifier read and executed
`references/verifier.md` with only the four protocol inputs. Claude's project
review is valuable and satisfies the approved workspace specification, but it
must not be relabeled as the stricter experimental verifier protocol.

## Skipped or limited layers

- Static type checking: skipped. The repository has no configured mypy,
  Pyright, or equivalent tool, and no new type-checker dependency was approved.
- Dependency and license audit: not applicable. No dependency changed.
- `pytest-randomly`: not installed or authorized. The maintained suite ran in
  fixed order; suite-health evidence is the explicit deterministic randomized
  concurrency loop.
- Separate property-testing package: not installed or authorized. The gauntlet
  exercises claim uniqueness over 40 randomized races and backup round-trip as
  executable invariants.
- Formal experimental independent verification: not performed, as stated
  above.
- External backup rehearsal: not performed. Selecting an off-machine target
  requires separate Paul approval. Only temporary local backup and restore were
  tested.

## Known limitations

- Changed executable line 932 in `agent_workspace.py` was not covered. It is a
  defensive guard for a conditional acknowledgment update losing a race after
  the same transaction read the timestamp as null.
- The exact-match manual mutation list is intentionally brittle when guarded
  source moves. Unique-site checks, isolated bytecode, JUnit reports, and
  post-restore hashes make that brittleness fail closed.
- Version 1 has no push notifications. Each client observes changes on its next
  tool call.
- The stdio command is the command-line interface. There is no separate human
  subcommand CLI for database mutations.
- The current native CUDA checkpoint is not cryptographically bound to the
  token list, wallet, delimiter, typo configuration, or executable. Workspace
  metadata can record hashes, but native checkpoint hardening remains outside
  this change.
- Coordination data has reduced backup assurance until Paul approves and
  configures an off-machine destination.
- Local privacy checks reduce accidental exposure but cannot defend against a
  malicious process with the user's filesystem access.
- No personal-wallet recovery execution was authorized or performed by this
  build. This evidence validates coordination tooling only.

## Honest failure history

- The initial schema, privacy, MCP, classification, backup, transfer, and
  review corrections were each observed RED before their corresponding GREEN
  phase.
- Codex review of checkpoint 21 found seven behavioral gaps. Paul approved
  amendment 1, and frozen tests reproduced all seven before implementation.
- The first corrective full-suite result reached 175 passed.
- During gauntlet development, the mutation-runner negative control exposed a
  false kill caused by a pytest setup error. The runner was changed to require
  a JUnit-confirmed failure, zero execution errors, and exact source
  restoration.
- The first enforced coverage attempt failed at 89.90%. The unchanged 90% gate
  passed at 98.99% only after synthetic probes exercised real fail-closed
  branches.
- Final evidence audit found the approved `docs/AGENT_WORKSPACE.md` deliverable
  missing. Paul approved adding it before evidence was finalized.

## Checkpoint history

- `2026-09-27-08-codex`: specification approved.
- `2026-09-27-09-codex`: schema and state RED.
- `2026-09-27-10-codex`: final schema RED source identity.
- `2026-09-27-11-codex`: schema and state GREEN.
- `2026-09-27-12-codex`: privacy and handoff RED.
- `2026-09-27-13-codex`: privacy and handoff GREEN.
- `2026-09-27-14-codex`: MCP capability RED.
- `2026-09-27-15-codex`: MCP capability GREEN.
- `2026-09-27-16-codex`: run classification RED.
- `2026-09-27-17-codex`: run classification GREEN.
- `2026-09-27-18-codex`: database backup RED.
- `2026-09-27-19-codex`: database backup GREEN.
- `2026-09-27-20-codex`: handoff transfer and stdio RED.
- `2026-09-27-21-claude`: acknowledgment, supersession, and stdio GREEN.
- `2026-09-27-22-codex`: review corrections RED.
- `2026-09-27-23-codex`: review corrections GREEN.
- `2026-09-27-24-codex`: implementation-only REFACTOR complete.
- `2026-09-27-25-codex`: final fresh GAUNTLET complete.
- `2026-09-27-26-codex`: final EVIDENCE handoff, written with this report.

## Final conclusion

The committed implementation at `6879d73` satisfies all 23 executable
acceptance tests and the approved amendment. The reproducible gauntlet passed
with the exact results above. The project-required non-builder review found no
issues. The remaining limitations are explicit and do not include an unreported
test, lint, mutation, backup, stdio, capability, or secret-scan failure.

No recovery artifact, wallet content, password, token list, candidate, private
key, or recovery log was accessed while producing this evidence. No dependency,
network operation, recovery execution, external backup selection, or Git
history operation was performed by Codex.
