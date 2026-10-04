# Generation-version binding, evidence report

## Current checkpoint, 2026-10-04 UTC

Paul committed the candidate as
`6cb35e838d997e339ec4ccdc1dcd51853fbb7dce`. Fresh committed-source results
and documentary corrections appear in the final section below. The earlier
working candidate was based on `c996a00d506a67dc1188e849fe451604b8c46944`,
whose underlying reviewed code was
`344c40acb2ee5cc4416d98cbe1923d980df3fec3`. Earlier results are historical.
Round 4 failed despite its passing standard gauntlet. Round 5's final Phase 2
verdict is bounded PASS for the approved contract at `6cb35e8`. Paul approved
closure under the existing named-test-failure rule on 2026-10-04. Its
stricter-input Phase 1 FAIL remains frozen and explicit. Merge and
shared-database setup remain unauthorized.

Companion to `GENERATION_BINDING_SPEC.md` (approved, gate 1 cleared 2026-09-27).
Loop: Old Coder. Risk tier 3. This report is written so the change can be trusted
without reading the implementation, within the spec's boundaries.

## Source state

- Worktree: `.claude/worktrees/generation-binding`, branch
  `worktree-generation-binding`, based on `a2f171f` (verified `git rev-parse HEAD`).
- Spec approval (human, correlation-breaking review): **obtained** — Paul,
  verbatim "The spec and the §3 call are approved".
- Independent verification (Codex): Codex is running the Old Coder validation of
  this task in parallel per COLLABORATION.md ownership; status recorded below.

## Files changed

| File | Change |
|---|---|
| `save_format.hpp` | `SAVE_GENERATION_VERSION` constant (=1); `SAVE_TOOL_VERSION` kept as its alias so the pre-existing `save_identity` contract compiles unchanged; `SaveMismatch::Generation` + message; one gating check in `validate_save` (after Corrupt, before Tokenlist); `build_record_v1` writes `SAVE_GENERATION_VERSION`. On-disk layout unchanged. |
| `tests/cuda_host_contracts.hpp` | 5 new host contracts + `generation_fingerprint` helper + dispatch entries. |
| `tests/test_cuda.py` | 5 new names in the native-contract parametrization. |
| `tests/test_cuda_cli.py` | `test_cli_restore_refuses_generation_mismatch` + `V1_TOOL_VERSION_OFF` (offset 16). |
| `tools/validate_save_format.py` | `drop_generation_check` mutant registered in the reproducible mutation harness. |
| `docs/specs/GENERATION_BINDING_SPEC.md`, this file | spec + evidence. |
| `multibit_cuda_threads.cu` | **no change** — the restore path already refuses any non-`None` mismatch (`if (mismatch != SaveMismatch::None) refuse`), so `Generation` is fail-closed with no override. This satisfies spec N4/F4 without new code. |

## Spec deviation (disclosed)

§3/§4.1 said "rename the constant to `SAVE_GENERATION_VERSION`." The implementation
keeps `SAVE_TOOL_VERSION` as a documented alias equal to `SAVE_GENERATION_VERSION`
rather than a hard rename, to honor negative constraint **N2** (no existing test
edited). Semantics are exactly as §3 specifies; only the symbol survives. No other
deviation. **[Superseded: see "Verifier Round 1 corrections" below.]**

## Behavior → test mapping

| Spec item | Test(s) |
|---|---|
| F1 cross-build resume refused | `save_reject_generation` (host); `test_cli_restore_refuses_generation_mismatch` (real CLI) |
| F2 forgotten bump caught **[Superseded: see "Verifier Round 1 corrections" below.]** | `save_generation_canary` (ordering fingerprint pinned to the version) |
| F3 existing v1 saves still resume **[Superseded: see "Verifier Round 1 corrections" below.]** | `save_generation_migrate_v1` |
| F4 fail closed, no override | `save_generation_no_override` (value sweep) **[Superseded: see "Verifier Round 1 corrections" below.]**; CLI test above; `.cu` generic refusal |
| F5 gate ordered before identity | `save_generation_order` |
| N1 ABI unchanged | `sizeof==1216` + offset `static_assert`s compile; all 26 `save_*` contracts green |
| N2 no existing test edited | `SAVE_TOOL_VERSION` alias retained; diff shows no assertion edits |

## RED (each new behavior observed failing before implementation)

Scaffolding (constant + enum + message) added first so tests compile and fail on
behavior, not on import. `validate_save` check withheld.

| Contract | RED result | Message |
|---|---|---|
| `save_reject_generation` | FAIL exit 2 | "save from a different candidate-generation order was not rejected" |
| `save_generation_order` | FAIL exit 2 | "generation mismatch was not reported before the token-list mismatch" |
| `save_generation_no_override` | FAIL exit 2 | "non-matching generation value accepted (no fail-closed gate)" |
| `save_generation_canary` | FAIL exit 2 | revealed gen-1 fingerprint `595e40e8…3e74fde0` |
| `save_generation_migrate_v1` | PASS (guards current behavior) | non-vacuity proven below |

## GREEN

Added one line to `validate_save`; pinned the gen-1 fingerprint. All 5 new
contracts pass; full native suite 50 passed / 0 regressions (baseline was 45).

## Manual mutation (validate_save generation branch)

Each mutant built and run against the mapped contract, then source restored.

| Mutant | Expected | Result |
|---|---|---|
| MUT-C flip `!=`→`==` | kill | KILLED — `save_reject_generation`, `save_generation_no_override`, `save_generation_migrate_v1` all exit 2 |
| MUT-D delete the check | kill | KILLED — `save_reject_generation`, `save_generation_order`, `save_generation_no_override` exit 2 |
| MUT-E move check after tokenlist | only `save_generation_order` kills | KILLED by `save_generation_order` (exit 2); the other two correctly survive (exit 0), confirming the order test is load-bearing |

Non-vacuity of `save_generation_migrate_v1`: set `SAVE_GENERATION_VERSION=2`,
rebuilt → test FAILED exit 2 ("shipping generation != 1 would refuse every existing
v1 save"). Restored to 1.

Canary negative control (home-grown checker must fail) **[Superseded: see "Verifier Round 1 corrections" below.]**: reversed the fingerprint
battery order with the version pin unchanged, rebuilt → `save_generation_canary`
FAILED exit 2 (fingerprint `0237b214…` != pinned `595e40e8…`). Restored. Proves the
canary detects an unbumped ordering change.

Known mutation blind spot (disclosed): comparing `r.tool_version` against any other
constant whose value is also 1 (e.g. `SAVE_FORMAT_VERSION`) is behaviourally
indistinguishable from the correct comparison **at generation 1**, so such a mutant
survives. It is not semantically equivalent globally — the two diverge the moment
`SAVE_GENERATION_VERSION` is bumped — but no gen-1 test can kill it. Documented,
not gamed.

## Tool-based mutation (reproducible)

`py -3.10 tools/validate_save_format.py` — report at
`.cuda-build/save-format-mutation/mutation-report.json`.

**6 / 6 killed** (all `test_exit=1`, all `restored=True`):
`drop_tokenlist_check`, `drop_wallet_check`, **`drop_generation_check`** (new),
`drop_prev_fail_loud`, `weaken_exact_size`, `drop_migration_backup_abort`. The five
pre-existing gates still kill after the `SaveMismatch` enum gained `Generation`,
confirming no regression from the enum change. Driver fails closed
(`py -3.10 tools/validate_save_format.py --negative-control` is the harness's own
survivor control, unchanged by this work). **[Superseded: the driver did not fail
closed on pytest errors; see "Verifier Round 2 corrections" below.]**

## Gauntlet layers

All numbers below are from one fresh run after the last source edit: both
executables rebuilt clean, then the suites run.

| Layer | Command | Result |
|---|---|---|
| Full suite (native + CLI) | `py -3.10 -m pytest tests/test_cuda.py tests/test_cuda_cli.py -q` | **134 passed, 0 failed** (29.83s) |
| New tests confirmed run (not skipped) | `pytest … -v` on the 6 new tests | **6 passed** (5 contracts + CLI adversarial) |
| Baseline delta | native contracts 45 → 50 | +5 new, 0 regressions |
| Lint | `ruff check … --line-length 100` | **All checks passed** (exit 0) |
| nvcc build | `build_cuda.py optimized_test` / `optimized` | both compile, no new warnings |
| ABI invariants | 1216-byte + offset `static_assert`s | compile clean (build fails otherwise) |
| Tool mutation | `validate_save_format.py` | **6/6 killed** |
| Manual mutation | MUT-C/D/E + non-vacuity + canary control | all killed / proven |
| Adversarial (real CLI) | `test_cli_restore_refuses_generation_mismatch` | PASSED — re-sealed generation-bumped save refused, no `RECOVERED_PASSWORD.txt` |

### Changed-line coverage (qualitative)

No C++ coverage tool is wired in this repo (consistent with the prior checkpoint
evidence), so changed-line coverage is argued by mapping, not a percentage gate:
the one `validate_save` gate line is executed by `save_reject_generation`,
`save_generation_order`, `save_generation_no_override`, `save_generation_migrate_v1`
and the CLI test; the `build_record_v1` writer line by every `save_*` round-trip;
the `Generation` message by the CLI refusal path; `generation_fingerprint` by the
canary. Changed Python lines are executed by the six new tests and the mutation
harness. Limitation: the absence of an automated changed-line coverage gate for C++
means this rests on the mapping above, backed by mutation (6/6) rather than a
coverage number.

## Failure modes deliberately not covered (known limits)

- **F6 spurious bump**: bumping the version when order did not change refuses
  resumable saves (availability, not data loss); the canary catches missing bumps,
  not extra ones. Accepted.
- **Historical same-number, different-order builds**: cannot be detected
  retroactively — no record exists to check against. Only saves written after this
  ships, plus existing `tool_version==1` saves under generation 1, are protected.
- **Gen-1 constant-value mutant blind spot**: see manual-mutation section.

## Reproduce from repo

1. `python tools/build_cuda.py optimized_test`
2. `python tools/build_cuda.py optimized`
3. `py -3.10 -m pytest tests/test_cuda.py -k native_contract tests/test_cuda_cli.py -q`
4. `py -3.10 tools/validate_save_format.py`
5. `py -3.10 -m ruff check tests/test_cuda.py tests/test_cuda_cli.py tools/validate_save_format.py --line-length 100`

Dev tools: CUDA v13.0, MSVC 14.44 (VsDevCmd), Python 3.10.3 for pytest/ruff.

## Bump procedure (for the next developer)

When a change alters candidate-generation ORDER: (1) bump `SAVE_GENERATION_VERSION`
by one; (2) run `save_generation_canary` once — it prints the new fingerprint;
(3) add a `{new_version, "<printed hex>"}` row to the canary's pin table. The gate
then refuses saves from every earlier order, as intended.

## Independent reproduction (2026-10-03, pickup session)

The interrupted builder thread's numbers were re-run from scratch on branch
`worktree-generation-binding` (base `a2f171f`, source byte-identical to the diff
above), not trusted as prior claims:

- `build_cuda.py optimized_test` and `optimized`: both compile clean, no new warnings.
- `pytest tests/test_cuda.py tests/test_cuda_cli.py`: **134 passed, 0 failed** (26.3s).
- `ruff check`: all checks passed.
- `validate_save_format.py`: **6/6 mutants killed** (`drop_tokenlist_check`,
  `drop_wallet_check`, `drop_generation_check`, `drop_prev_fail_loud`,
  `weaken_exact_size`, `drop_migration_backup_abort`), each `restored=True`.
- `validate_save_format.py --negative-control`: survivor control survived as designed.
- Worktree source verified byte-clean after the mutation run (unchanged 5-file diff;
  `validate_save` generation-gate line intact).

This is the worktree gauntlet. The authoritative committed-source gauntlet and the
independent Codex verifier remain pending Paul's commit; neither is an agent step. **[Superseded: see "Verifier Round 1 corrections" below.]**

## Verifier Round 1 corrections (2026-10-03, Amendment 1)

### Round 1 outcome (recorded, not rewritten)

Codex's independent verifier, Round 1 of the default two-round cap, **failed**
(`docs/handoffs/2026-10-03-30-codex.md`). It reviewed implementation `b093dc8` at
HEAD `a8403da` (handoff 29 added on top). It found the core equality gate correct
but the bump-enforcement claim false. Its run substituted Python 3.14.6 for 3.10,
which was unavailable in its environment; it reproduced 134 / 181 passed and 6/6
mutants. Attacks that succeeded against `b093dc8`:

- **V1:** a coherent capslock-order change (reference and live generators) left the
  canary and all 134 tests green with the version unchanged. Delete and closecase
  were absent from the canary entirely.
- **V2:** after a coherent permutation-order change, repinning only the fingerprint
  with the version left at `1` turned the canary and suite green again. The §5
  claim "green only after repin *and* bump" was false.
- **V3:** no randomized generation-value property; "value sweep" was six fixed values.
- **V4:** replacing the refusal message with generic text left all tests green.
- **V5:** generation mutants beyond the drop were prose only; `save_generation_migrate_v1`
  validates a record but never resumes one.

So these earlier claims were wrong and are withdrawn: "F2 forgotten bump caught",
"value sweep", the F3 mapping to a validation-only contract, "No other deviation",
and the prose-only canary control. The earlier "committed-source run pending" line
was overtaken by handoff 29, which recorded that run completing green on `b093dc8`.
That run was correct for that source; it just didn't test what the verifier attacked.

Paul approved the fixes and then the written Amendment 1 (spec SHA-256
`9b491d58…bb662b7960` as approved; `63497d44…7a06b385` after the approval marking).

### What changed

| Finding | Change | Test / control |
|---|---|---|
| V1 | Canary test set gains capslock, delete, closecase (each alone) and one all-modes case | `canary_capslock_reorder`, `canary_delete_reorder`, `canary_closecase_reorder` mutants |
| V2 | Pin rows between `GENERATION-PINS-BEGIN/END`; append-only checker vs `master` merge-base; new failure message | `tests/test_generation_pins.py` (real repo + 13 fixtures); `canary_permutation_reverse` mutant |
| V3 | `save_generation_property`: 7 boundary values + 4,096 from `mt19937_64(20261003)` | `accept_generation_mismatch` mutant |
| V4 | Exact message pinned in a host contract and the CLI refusal test | `generic_generation_message`, `generic_generation_message_host` mutants |
| V5 | New CLI test resumes a gen-1 save at combo 0 / permutation 1 and checks exactly the remaining 3; generation mutants persisted | `generation_wrong_constant`, `generation_after_tokenlist` mutants |
| harness | `validate_save_format.py`: multi-edit mutants (several exact-once edits, all restored or the run fails) and `--only NAME` | dry anchor check: all 16 mutants match exactly once |

The one-time gen-1 repin allowed by A1: fingerprint
`595e40e868fca39275a52b5c17d6854a6696282755dfbe57c71b66393e74fde0` →
`9c66679756f0a61c6c269f13ab37464340bc5d9a104009d734e2dc2af44b967f`, stable over three
runs. `SAVE_GENERATION_VERSION` stays `1`; candidate order did not change, only
the set of cases hashed. `master` had no pin table, so this is the bootstrap row.

### Additions beyond the amendment text (disclosed)

- **All-modes canary case.** Option order at one position (repeat vs delete vs
  closecase) only matters when two modes are on together, so single-mode cases
  cannot pin it. The delete and closecase reorder mutants are killed by this case,
  not by the single-mode cases. The single-mode delete and closecase cases have no
  dedicated mutant; their coverage is argued, not proven.
- **`generic_generation_message_host`.** A second message mutant, so both the CLI
  and host-contract message pins have a persisted kill.
- **`--only` and `mutation-selected.json`.** Selecting mutants writes a separate
  report, so it never overwrites the full `mutation-report.json`.

### RED → GREEN

| Check | RED (before fix) | GREEN |
|---|---|---|
| V1 canary coverage | `--only` the 4 canary mutants: capslock, delete, closecase **survived**; permutation killed (log `.cuda-build/red-v1-canary.log`) | all 4 killed |
| V1 new cases | canary exit 2: fingerprint drift with the new message (log `red-v1-battery.log`) | repinned per A1, PASS |
| V2 pin checker | stub checker: **10/10** violation fixtures failed, 4 passed (log `red-v2-pins-stub.log`) | 14/14 passed |
| V3, V4, V5 guards | guard existing behavior, so RED is the persisted mutants above | all killed |

### Gauntlet after the last edit (uncommitted worktree, base `a8403da`)

| Layer | Result |
|---|---|
| Builds | `optimized_test` and `optimized` rc 0; saved build output has **0** warning mentions |
| `pytest tests/test_cuda.py tests/test_cuda_cli.py tests/test_generation_pins.py` | **151 passed** (134 + 2 contracts + 1 CLI + 14 pin tests) |
| `pytest tests` | **198 passed** (verifier saw 181 at `b093dc8`; +17 new) |
| ruff (4 files, line length 100) | all passed |
| `validate_save_format.py` | **15/15 killed**, all `restored=True` **[Superseded: most likely 14 real kills; `drop_migration_backup_abort` errored in setup. See "Verifier Round 2 corrections".]** |
| `--negative-control` | survivor survived, exit 1 as designed |
| Tree after run | only the intended edits; no mutant bytes left |

### Claims now, and their limits

- **F2:** a forgotten bump makes the branch's suite fail. The canary drifts on any
  order change in the hashed cases **[Qualified in Round 3: the canary hashes the
  reference generator; live-generator changes are caught by the separate
  live-vs-reference tests, not by the canary]**, and the only way back to green is a new row
  plus a version bump. This does not stop a deliberate edit of the checker, or an
  edit committed straight to `master` (where merge-base equals HEAD). It catches
  an honest mistake.
- **Test set is now frozen.** After merge, changing the canary's cases changes the
  fingerprint and needs a bump, which refuses existing saves (availability).
- Still disclosed: comparing against `SAVE_FORMAT_VERSION` is indistinguishable at
  generation 1. The old "set version 2" non-vacuity check stays prose; the persisted
  `generation_wrong_constant` covers the same resume path.
- Not run: automated changed-line coverage for C++ (no tool wired) and
  test-order randomization.

### Reproduce (from `BitCracker/btcrecover-master`)

1. `python tools/build_cuda.py optimized_test` and `python tools/build_cuda.py optimized`
2. `py -3.10 -m pytest tests -q`
3. `py -3.10 tools/validate_save_format.py` then `--negative-control` (expect exit 1)
4. `py -3.10 -m ruff check tests/test_cuda.py tests/test_cuda_cli.py tests/test_generation_pins.py tools/validate_save_format.py --line-length 100`
5. Set `GENERATION_PINS_BASE_REF` if the integration branch is not `master`.

### Bump procedure (replaces the earlier one)

When a change alters candidate-generation order: (1) bump `SAVE_GENERATION_VERSION`
by one; (2) run `save_generation_canary`, which prints the new fingerprint; (3)
**append** `{new_version, "<hex>"}` between the pin markers. Never edit an existing
row: `test_generation_pins.py` fails if you do.

Status: corrections complete in the worktree, uncommitted. Committed-source
gauntlet and verifier Round 2 are pending. **[Superseded: Paul committed these as
`a8d3d5d`; handoff 31 recorded the committed-source run; Round 2 then failed. See
below.]**

## Verifier Round 2 corrections (2026-10-03)

### Round 2 outcome (recorded, not rewritten)

Codex's verifier Round 2 **failed** (`docs/handoffs/2026-10-03-32-codex.md`). It
reviewed candidate `a8d3d5d` at HEAD `60378bd` (handoff 31 on top), under Python
3.14.6. Its own runs reproduced both builds, 198 passed, 15/15 required mutants
with real test failures, ruff, and the survivor control. It found the runtime gate
correct and the Amendment 1 corrections satisfied. Findings, graded by Paul
("Accepted as proposed"):

- **R2-F1 (blocker):** the mutation driver counted any nonzero pytest exit as a
  kill. A real mutant pointed at a nonexistent test (pytest exit 4) was reported
  killed and the driver exited 0. The claim "Driver fails closed" was false.
- **R2-F2:** no test combined a corrupt checksum with a generation mismatch, so
  moving the generation check ahead of the checksum check passed all tests.
- **R2-F3:** the fixed and random generation values missed `3`; a mutant also
  accepting `3` passed all tests.
- **R2-F4:** under shuffled test order, the agent-workspace backup test failed
  (`WinError 5` at `os.replace`). That code is unchanged since `a2f171f`. Paul
  graded it out of scope for this branch; it is tracked separately and not fixed here.

Paul has authorized further verifier rounds until one passes.

### What changed

| Finding | Change | Test / control |
|---|---|---|
| R2-F1 | Kill only when pytest exits 1 **and** its JUnit report shows the named test ran and failed; exit 0 with it passing is a survivor; anything else is a runner error. Driver exits 0 all killed, 1 survivor, 2 runner error. | `tests/test_mutation_driver.py` (20 cases); `--runner-control` end-to-end |
| R2-F1 (found while fixing) | Fresh pytest temp directory per mutant run | `drop_migration_backup_abort` rerun |
| R2-F2 | `save_generation_after_checksum`: sealed record with another generation → `Generation`; same record with a stale checksum → `Corrupt` | `generation_before_checksum` mutant |
| R2-F3 | Property tests every value 0..65535, plus boundaries and the 4,096 seeded values (0.86 s) | `accept_generation_three` mutant |

No product code changed: `save_format.hpp`, `multibit_cuda_threads.cu` and
`cuda_typos.hpp` are still byte-identical to `b093dc8`.

### A false kill found in my own runs (disclosed)

On its first full run, the fixed driver reported `drop_migration_backup_abort` as a
**runner error**. Pytest could not clear that mutant's leftover temp directory
(`Access is denied`, even when reading its permissions), so the test errored in
setup and never ran. The old driver counted that error as a kill.

The directory was created at 13:43. That is after my 13:27 committed-source run of
`b093dc8` and during Codex's Round 1 work in this worktree. No process holds it.
So the two later builder runs, the 16:08 worktree run and the 16:26 committed-source
run of `a8d3d5d`, **most likely** had 14 real kills, not the 15/15 reported. Their
per-mutant logs were overwritten, so this can't be confirmed for those runs; the
cause was in place for both. Codex's Round 2 kills ran in a separate copy and are
unaffected. The driver now uses a fresh temp directory per run. Rerun alone, the
mutant is a real kill (JUnit `failure`). The locked directory was left in place:
removing it needs admin ownership changes, and it sits in ignored scratch space.

### RED → GREEN

| Check | RED | GREEN |
|---|---|---|
| R2-F1 unit | stub with the old rule: **15 failed**, 5 passed (`red-r2f1-driver-unit.log`) | 20/20 passed |
| R2-F1 end-to-end | old rule: `runner_error_control` reported **killed** (pytest exit 4), driver exit 0 (`red-r2-end-to-end.log`) | `runner_error`, exit 2 |
| R2-F2 | `generation_before_checksum` "killed" only by pytest exit 4: its test did not exist yet (same log) | killed by a real failure |
| R2-F3 | `accept_generation_three` **survived** (same log) | killed |

### Gauntlet after the last edit (uncommitted worktree, base `60378bd`)

Log `.cuda-build/r2fix-gauntlet2.log` (git-ignored):

| Layer | Result |
|---|---|
| Builds | `optimized_test` and `optimized` rc 0; 0 warning mentions in saved build output |
| `pytest tests` | **219 passed** (198 + 20 driver unit tests + 1 contract) |
| ruff (5 files, line length 100) | all passed |
| `validate_save_format.py` | **17/17 required mutants killed**, each by its named test running and failing; exit 0 |
| `--negative-control` (separate) | survivor survived, exit 1 as designed |
| `--runner-control` (separate) | runner error (pytest exit 4), exit 2 as designed |
| Sources | all 19 runs `restored=True`; tree after shows only the intended edits |

### Claims now, and their limits

- **Driver:** a required mutant counts as killed only when its named test ran and
  failed. Usage errors, nothing collected, interruptions, setup errors, skips, a
  different test, or a missing or unreadable report all fail the run.
  **[Superseded: a parseable report with an unrecognized structure was still
  classified. See "Verifier Round 3 corrections".]**
- **Generation values:** every value 0..65535 is checked exhaustively; the rest of
  the 32-bit range is sampled.
- **Pin protection:** prefix protection becomes useful once the bootstrap pin is in
  integration history. `master` has no pins yet.
- Runtimes: builder runs used Python 3.10.3; Codex used 3.14.6. Neither
  reproduced the other's runtime.
- Not run: thresholded C++ coverage, sanitizers, and a passing shuffled-order suite
  (the shuffled failure is R2-F4, out of scope).

### Reproduce (from `BitCracker/btcrecover-master`)

1. `python tools/build_cuda.py optimized_test` and `python tools/build_cuda.py optimized`
2. `py -3.10 -m pytest tests -q`
3. `py -3.10 tools/validate_save_format.py` (expect exit 0), `--negative-control`
   (expect 1), `--runner-control` (expect 2)
4. `py -3.10 -m ruff check tests/test_cuda.py tests/test_cuda_cli.py tests/test_generation_pins.py tests/test_mutation_driver.py tools/validate_save_format.py --line-length 100`

Status: Round 2 corrections complete in the worktree, uncommitted. **[Superseded:
Paul committed these as `dcb3b2a`; handoff 33 recorded the committed-source run;
Round 3 then failed. See below.]**

## Verifier Round 3 corrections (2026-10-03)

### Round 3 outcome (recorded, not rewritten)

Codex's verifier Round 3 **failed** (`docs/handoffs/2026-10-03-34-codex.md`). It
reviewed candidate `dcb3b2a` at HEAD `f59a978` (handoff 33 on top), under Python
3.14.6 and Ruff 0.16.0. It reproduced both builds, 219 passed with no skips, 17/17
required kills (each checked against its named JUnit test case), and all three
controls, and found the runtime gate correct. Findings, graded by Paul ("accept
as proposed"):

- **R3-B1 (blocker):** the driver checked only that the report was parseable XML
  with a matching test case. Codex let a real mutant's named test fail, then
  renamed the JUnit root to `not_junit`. The driver still reported a kill and
  exited 0. Unknown elements inside a test case read as a pass, and duplicate
  failure elements were merged into one.
- **R3-B2:** `--only` silently replaced a requested control.
  `--negative-control --only drop_generation_check` ran a required mutant, exited
  0, and labelled the report as the negative control.
- **R3-G1 (lint only):** the five-file Ruff command passed under my Ruff 0.15.18
  but failed under 0.16.0 (RUF100: unused `# noqa: E402`). Both results were
  accurate for their versions.
- **Canary wording:** the canary hashes the reference generator only. Codex's
  live-only repeat/delete reorder survived the canary but failed 4 of the
  live-vs-reference tests. It is not a forgotten-bump survivor of the full suite,
  but the evidence implied the canary covered both layers.

### What changed

| Finding | Change | Test / control |
|---|---|---|
| R3-B1 | `report_testcase` accepts only the pytest JUnit layout: `<testsuites>` holding one `<testsuite>`, or a bare `<testsuite>`; one `<testcase>`; only outcome (`failure`, `error`, `skipped`) and output (`properties`, `system-out`, `system-err`) elements; at most one outcome. Anything else is a runner error. | 7 structure cases plus 4 accepted-layout cases in `tests/test_mutation_driver.py`; end-to-end `--report-control` |
| R3-B2 | `--negative-control`, `--runner-control`, `--report-control` and `--only` are mutually exclusive; any combination stops at argument parsing before anything runs | 6 combination cases (no CUDA build) |
| R3-G1 | `from tools import validate_save_format` (conftest already puts the project root on the path); no path insert, no `noqa` | Ruff 0.15.18 default rules plus `--extend-select RUF100,E402` |

`--report-control` builds a real mutant whose named test genuinely runs and fails
(the on-disk report shows `failure`), then corrupts the report root before
classification. The fault hook applies only to that control mutant.

No product code changed: `save_format.hpp`, `multibit_cuda_threads.cu` and
`cuda_typos.hpp` are still byte-identical to `b093dc8`.

### RED → GREEN

| Check | RED (old classifier and argument rules) | GREEN |
|---|---|---|
| Driver unit tests | **11 failed**, 27 passed (`red-r3-driver-unit.log`): wrong root, nested suite, unknown child on pass and fail, duplicate failure, 5 control combinations, report fault | 38/38 passed |
| `--report-control` end to end | reported **killed**, exit 0 (`red-r3-report-control.log`) | runner error "not a JUnit report: root is <not_junit>", exit 2 |
| Control plus `--only` | ran the required mutant, exit 0 (`red-r3-control-combo.log`) | argparse error, exit 2, nothing run |

Two structure cases already failed closed before the fix (two outcomes; a failure
nested inside `system-out`). They are kept as regressions.

### Gauntlet after the last edit (uncommitted worktree, base `f59a978`)

Log `.cuda-build/r3fix-gauntlet.log` (git-ignored). Python 3.10.3, Ruff 0.15.18.

| Layer | Result |
|---|---|
| Builds | `optimized_test` and `optimized` rc 0; 0 warning mentions |
| `pytest tests` | **237 passed** (219 + 18 new driver cases) |
| Ruff, 5 files, line length 100 | all passed |
| Ruff `--extend-select RUF100,E402` on the changed test | all passed |
| `validate_save_format.py` | **17/17 required mutants killed**, exit 0 |
| `--negative-control` | survivor survived, exit 1 |
| `--runner-control` | runner error (pytest exit 4), exit 2 |
| `--report-control` | runner error (unrecognized report root), exit 2 |
| `--negative-control --only …` | rejected at parsing, exit 2, nothing run |
| Sources | all 20 runs `restored=True`; tree after shows only the intended edits |

### Claims now, and their limits

- **Driver:** a required mutant counts as killed only when its named test ran and
  failed, in a report with the recognized pytest JUnit layout. Every other outcome
  or layout is a runner error (exit 2). Controls run only on their own.
- **Two detection layers for order changes:** the canary fingerprints the
  reference generator, and the live-vs-reference tests (`assembly`, `resume`,
  `parallel` and the typo comparisons) catch live-generator changes that
  the reference does not share. Both must hold; neither alone covers both.
- **Lint:** verified here under Ruff 0.15.18 only, with RUF100 and E402 explicitly
  enabled. Ruff 0.16.0 was not installed here (no installs authorized); the next
  verifier run checks it.
- Runtimes: builder Python 3.10.3 / Ruff 0.15.18; Codex Python 3.14.6 / Ruff 0.16.0.
- Unchanged limits: honest-branch bump enforcement; pin prefix protection is useful
  only once the bootstrap pin is in integration history; values above 65,535
  sampled; `SAVE_FORMAT_VERSION` constant indistinguishable at generation 1; no
  thresholded C++ coverage, sanitizers, or full shuffled-order pass (R2-F4 out of
  scope). The uncertainty about my overwritten 16:08 and 16:26 logs stands.

### Reproduce (from `BitCracker/btcrecover-master`)

1. `python tools/build_cuda.py optimized_test` and `python tools/build_cuda.py optimized`
2. `py -3.10 -m pytest tests -q`
3. `py -3.10 tools/validate_save_format.py` (expect 0), then separately
   `--negative-control` (1), `--runner-control` (2), `--report-control` (2)
4. `py -3.10 -m ruff check tests/test_cuda.py tests/test_cuda_cli.py tests/test_generation_pins.py tests/test_mutation_driver.py tools/validate_save_format.py --line-length 100`

Status: Round 3 corrections complete in the worktree, uncommitted.

## Descendant-report correction, 2026-10-04 UTC

### Contract and historical failure

The accepted correction rejects element children hidden inside JUnit outcomes
or output. A property container may contain only property leaves. Legitimate
properties, ordinary text, escaped markup and CDATA remain valid. Invalid
reports must classify as `runner_error`, never as a kill or survivor.

Round 4 reviewed code `344c40a` at docs-only HEAD `c996a00`. Both builds,
237 tests, 17 required mutants and the original controls passed. Nevertheless,
the independent verifier demonstrated a genuine named assertion failure with
an added `<error>` inside `<system-err>`. The driver called it `killed` and
returned gate 0. This was an independent **FAIL**, not certification.

Frozen Round 4 artifacts are unchanged under
`BitCracker/btcrecover-master/.cuda-build/verifier-round4/`:

- Phase 1 SHA-256:
  `2c68fac9bf1ace45bab7799a78405b2abd850ba01f5f9e1d71579d331eab5766`.
- Phase 2 SHA-256:
  `a68edb4d5180603cc681cff70d18f7ab67952e37149e9684d2c93c3fe7ec0a64`.
- The 201-file artifact manifest SHA-256:
  `f8c92047666d8219b803b1b4094839187c7ca277e0628563c459a54a8359c696`.

### Implementation and source identity

The takeover preserved the existing partial edits in the driver and its test
file. Output and outcome elements are now leaves. Both suite and testcase
output are checked. Properties contain only property leaves, so deeper
descendants cannot conceal outcomes. No string blacklist is used.

The original report-root control is retained. A second standalone control
builds a real `drop_generation_check` mutant, verifies its named assertion
fails cleanly, then adds a nested error while preserving that failure.
`--report-control` succeeds as a diagnostic only when both controls build,
execute, reject their injected reports and restore the source. It returns 2
when both controls hold, otherwise 3. Faulted XML is retained for inspection.

Working-file byte SHA-256 values at the local run:

| File relative to `BitCracker/btcrecover-master` | SHA-256 |
|---|---|
| `tools/validate_save_format.py` | `f0119e261d9424d43374883d7ce2fa694511e20c1a3a411603b8b72a29df95c8` |
| `tests/test_mutation_driver.py` | `c1b714d812797e91b9857c2ec7b8738caeeb733ec8677b831dbbe37c77ec09f4` |
| `tools/generation_binding_gauntlet.py`, approved replay runner | `a98bc6a1e7553e67942653a621e29660a6c3bd28f6473d51e057b1570b4e88f0` |

At the fresh local gauntlet, the approved spec's byte SHA-256 was
`63497d447818ab14f2975842b9d57aedfe841723fcc2805b4378658b7a06b385`.
Paul subsequently approved tracked inclusion of the replay runner with "yes"
to the direct approval request. Append-only amendment A8 records this setup
approval. The current spec's byte SHA-256 is
`1a9b2c047c47b67b3c83c0e2675ce03460aacc7d98e4d9848c86e8e0c49eead9`.
Only documentation changed after that gauntlet. All 18 recorded executable,
test, harness and public-fixture hashes still match the tested source state.
The original frozen local record retains its approval-pending status as
history; checkpoint 38 records the later approval. Handoff 37 is unchanged.

The three product files have no Git content diff from `b093dc8`. A fresh raw
byte comparison found CRLF checkout bytes differ from that commit's LF blobs.
Their LF-normalized bytes match. This qualifies earlier "byte-identical"
wording, without changing the historical reports. Restoration is checked with
raw-byte hashes against the actual current source, not normalized Git blobs.
No product file was edited during this correction.

### RED, GREEN and bounded checker gauntlet

Commands below ran from `BitCracker/btcrecover-master` in the implementation
worktree. This is local builder validation, not fresh independent verification.

1. `python tools/generation_binding_gauntlet.py --phase red`
   copied the committed `344c40a` checker and current descendant regressions
   into an isolated directory. All **7 assertions failed**, each with
   `DID NOT RAISE`; process exit 1 was the expected RED. The unrelated new
   control tests were omitted only in that scratch copy because the old
   checker lacks their constants. Candidate tests were not weakened.
   Final RED artifacts: `.cuda-build/binding-red-eb0557d2/`.
2. Before strengthening the partial control aggregator, the command
   `python -m pytest tests/test_mutation_driver.py -q -k 'missing_control or not_restored'`
   produced **2 failed** assertions. The partial implementation incorrectly
   accepted both cases. Requiring both controls and successful restoration
   made these tests pass.
3. Focused GREEN is **149 passed**, comprising 135 driver tests and 14 pin
   tests, under Python 3.14.6 and Python 3.10.3. The archived primary pytest
   version is 8.4.1. Prior labels "9.0.3" and "9.0.2" were incorrect and
   have been withdrawn; the archived runner did not record pytest310's
   version separately. Current versions are recorded in the follow-up below.
   The 72 generated XML shapes exercise suite and testcase contexts, three
   containers, four hidden tags, depths 0/1/3, and both pytest exits 0/1.
   Positive tests preserve text, properties, escaped markup and CDATA,
   including suite-level output. This is deterministic generated testing,
   not a claimed Hypothesis or random-fuzz run.
4. Four isolated checker mutants each produced assertion failures and exit 1:
   allowing output children, allowing property descendants, ignoring suite
   output, and accepting partial controls with `any`. No production source
   was mutated for these probes. This is a separate **4/4** checker sweep,
   not part of the required 17 native mutants.
5. Coverage.py measured **10/10 executable lines** across
   `check_report_leaf` and `report_control_exit`. This is a narrow line gate,
   not 100% whole-driver, native, branch or repository coverage.
6. Ruff 0.16.0 and 0.15.18 both passed on the six scoped Python files, using
   `--line-length 100 --extend-select RUF100,E402`, the established rule set.
   New Python formatting uses 79 columns. A separate
   `python -m ruff format --check tests/test_mutation_driver.py tools/generation_binding_gauntlet.py --line-length 79`
   passed for both files. Whole-driver format certification is not claimed.

The original 11 committed `assert` expressions in `test_mutation_driver.py`
were compared as AST nodes and remain present unchanged after formatting.
An initial scratch RED attempt failed at collection because of the new control
constants; it was not counted as RED. Early replay-runner lint failures were
corrected before the fresh all-layer run. These discarded attempts are not
passes.

### Fresh local replay and remaining gates

Immediately before the builds, a read-only process check found no
`multibit`, `optimized`, `bitcracker` or `btcrecover` recovery processes.
The GPU query showed no compute-app recovery process. Only authorized builds
and public/synthetic fixtures were used. No live checkpoint, installed
recovery executable, personal token list or shared database was changed.

After confirming recovery remains stopped, the replay command is:

```powershell
python tools/generation_binding_gauntlet.py
```

Actual fresh run artifacts: `.cuda-build/binding-all-72de1a55/`.
Every subprocess has an individual `.json` command/exit record and `.log`.
The suite has JUnit XML; helper coverage and source-copy hashes are retained.
The runner fails on unexpected command exits, wrong mutant/control results,
missing controls, absent matching named-test failures or restoration mismatch.
Native mutations operate only on an allowlisted copy under `.cuda-build`.
The classifier does not enforce Python exception type: a valid named
call-phase RuntimeError or TimeoutExpired failure can also count as killed.
The default full-suite baseline passes before the mutation sweep, but
`run_mutant` alone does not prove the same test passed on the baseline.
All 17 official kills in this independent run were separately confirmed to
contain actual assertion failures. An assertion-only rule or additional
baseline discrimination would be a new policy requiring human approval,
not an already-approved requirement or an automatic product correction.

| Layer | Actual result |
|---|---|
| Fresh `optimized_test` build | PASS, exit 0 |
| Fresh `optimized` build | PASS, exit 0 |
| Full `pytest tests -q` suite | 334 passed, exit 0, no skipped outcomes |
| Required native mutants | 17/17 killed, build 0, test 1, restored, gate 0 |
| Survivor negative control | `survived`, build 0, test 0, restored, gate 1 |
| Runner-error control | `runner_error`, build 0, test 4, restored, gate 2 |
| Root and nested report controls | Both `runner_error`, clean `killed`, build 0, test 1, restored, gate 2 |
| Final raw-byte restoration | PASS, all 18 allowlisted source/fixture hashes match in both source trees |

The replay command completed with exit 0. In both report controls the named
test really failed before injection. The nested fault's exact diagnostic was
`unexpected elements inside JUnit <system-err>: ['error']`.

A separate read-only replay of these actual faulted XML files through the
isolated `allow_output_children` checker mutant retained the root control as
`runner_error`, misclassified the nested control as `killed`, and returned
**gate 3**. This proves the two-control aggregator notices the missing defense.
It reused the real reports, not a second native build or independent review.
The following replay ran successfully, including its assertions:

```powershell
$reportProbe = @'
import copy
import importlib.util
import json
from pathlib import Path
import sys

output = Path('.cuda-build/binding-all-72de1a55')
state = json.loads((output / 'restoration.json').read_text())
source = Path(state['mutation_copy'])
report = json.loads((source / '.cuda-build/save-format-mutation/'
                     'mutation-report-control.json').read_text())
path = output / 'allow_output_children/tools/validate_save_format.py'
spec = importlib.util.spec_from_file_location('defeated_checker', path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
results = copy.deepcopy(report['results'])
for item in results:
    xml = (source / item['faulted_report']).read_text(encoding='utf-8')
    try:
        item['status'] = module.classify_test(
            item['test_exit_code'], xml, item['test'])
    except RuntimeError as error:
        item['status'] = 'runner_error'
        item['runner_error'] = str(error)
assert results[0]['status'] == 'runner_error'
assert results[1]['status'] == 'killed'
assert module.report_control_exit(results) == 3
print('Defense removed: nested control falsely killed, gate 3')
'@
python -c $reportProbe
```

The full suite includes the property/reference canary, low-generation rejection
and precedence cases, generation-1 stored-position restore, approved pin guard,
the original checker tests and the new topology/control tests. The standalone
controls are not included in the required mutation tally.

No new types layer, repository-wide coverage gate, shuffled full-suite gate or
random fuzz run was performed. The separately human-excluded workspace-backup
shuffle issue remains out of scope. Earlier independent findings and failed
rounds remain historical evidence, not erased by local GREEN.

At the local working-tree checkpoint, the next mandatory gate was Paul's
commit, then a fresh blind verifier against that exact candidate. Paul has
since committed `6cb35e8`; the committed follow-up is recorded below.
Phase 1 receives only the approved contract,
spec, exact source state and replay commands, not this evidence, handoffs or
prior findings. No new independent verdict exists for this working candidate.
After the blind report freezes, Phase 2 may inspect the historical evidence.
There is no authorization to merge or create the external shared database.

## Committed-source follow-up, 2026-10-04 UTC

Source: `6cb35e838d997e339ec4ccdc1dcd51853fbb7dce`, branch
`worktree-generation-binding`. The approved spec raw SHA-256 is
`1a9b2c047c47b67b3c83c0e2675ce03460aacc7d98e4d9848c86e8e0c49eead9`.
The worktree was clean at preflight. The user explicitly approved this fresh
gauntlet and blind verification and confirmed recovery was not running.
Read-only process checks found no recovery process. Desktop GPU applications
were present, so GPU use was not described as zero.

Command from `BitCracker/btcrecover-master`:

```powershell
python tools/generation_binding_gauntlet.py
```

Fresh artifacts: `.cuda-build/binding-all-d72b55b6/`. The replay created new
isolated mutant sources under `.cuda-build/gmabb2/`; no old native artifacts
were substituted. Before independent execution, the current source was also
bound to the commit with the persisted read-only check:

```powershell
python .cuda-build/committed-6cb35e8/check_source_state.py
```

It verified **69** tracked code/config files in the native root, tests and
tools against the commit's blobs after LF normalization, while recording
raw working-file hashes. The file selection intentionally excludes deeper
upstream packages and is not a whole-repository tree hash. A scratch path
calculation initially selected the wrong ancestor and failed before checking;
the corrected check completed and wrote `SOURCE_STATE.json`.

Actual tool versions recorded by subprocess commands in that artifact:

| Tool | Python 3.14 installation | Python 3.10 installation |
|---|---|---|
| Python | 3.14.6 | 3.10.3 |
| pytest | 8.4.1 | 9.1.1 |
| Ruff | 0.16.0 | 0.15.18 |
| Coverage.py | 7.15.2 | Not used for coverage |

The committed report incorrectly labeled pytest versions as 9.0.3/9.0.2 and
Coverage.py as 7.13.5. The primary archived pytest-version log contradicts
9.0.3; the other historical version labels were unsupported by archived
version records. Those labels are withdrawn, not silently treated as an
environment change. This is a documentary tool-metadata correction; no code,
test, spec, dependency or executed source changed. The corrected draft is
prepared before the new full independent verifier execution. Historical
handoffs and frozen artifacts remain unchanged.

| Fresh committed layer | Actual result |
|---|---|
| Focused tests | 149 passed on both Python installations |
| Scoped Ruff | Both versions passed |
| Helper coverage | 10/10 executable lines, narrow scope only |
| Separate checker mutants | 4/4 caught by actual assertion failures |
| Both fresh CUDA builds | Passed, exits 0 |
| Full suite | 334 passed, exit 0 |
| Required native mutants | 17/17 killed, build 0, test 1, restored, gate 0 |
| Survivor negative control | `survived`, build 0, test 0, restored, gate 1 |
| Runner-error control | `runner_error`, build 0, test 4, restored, gate 2 |
| Both report controls | `runner_error`, clean `killed`, build 0, test 1, restored, gate 2 |
| Final restoration | All 18 raw hashes match source and isolated copy |

The full replay completed with exit 0. Native build/test resources were then
released to the fresh-context verifier. The parent will not build while that
verifier owns them. Its fresh artifacts and frozen report are separate from
these builder outputs. No prior evidence, findings or handoffs were given to
its blind phase.

Draft evidence changes are the only tracked working-file modifications after
the clean source preflight. They are withheld from the fresh-context verifier
until Phase 1 freezes. Executable/test/spec state remains the exact committed
candidate. Before the verifier completed, independent status was **not
performed**, not inherited from the builder's passing layers or previous
rounds. The completed source-specific review is recorded below.

## Independent verification, Round 5, 2026-10-04 UTC

Verified executable/test/spec state:
`6cb35e838d997e339ec4ccdc1dcd51853fbb7dce`.

- Fresh-context verifier: no history fork; same inherited model family.
  Context correlation was broken, model-family correlation was not.
- Initial inputs: task contract, approved spec, exact source and the tracked
  entry point. Builder counts, evidence, handoffs and prior findings were
  withheld until Phase 1 froze. Native build resources were serialized.
- Final Phase 2 verdict: **bounded PASS for the approved generation-binding
  and nested-report correction**. Paul approved closure under the existing
  named-test-failure rule; the approval context is recorded below. This is
  not blanket certification or operational approval.
- Historical Phase 1 verdict: **FAIL under the literal stricter task input**.
  It was not rewritten after the contract comparison.
- No code, test or approved-spec behavior changed after the verified state.
  Only documentary corrections and publication records changed.

Frozen artifacts under
`.cuda-build/blind-6cb-20261004-35b10f67/`:

| Frozen artifact | Raw SHA-256 |
|---|---|
| `PHASE1_FROZEN.md` | `92263caf63afc508c4db60759ee2f879ccc97c5dd2c3d0c82242f2000df21a10` |
| `PHASE2_FROZEN.md` | `1eb79c59e6d3f18543ae41ac493a146d81ea9046a6a81a7ea76e486ac31b86d2` |

Both hashes were independently checked at publication. The final narrowed
draft actually compared in Phase 2 had raw SHA-256
`8d4003dee76c0dc2cde956210e995a0b897f0f91a4226c8cc3f6dd7378e4e236`.
This final results section is subsequent documentary publication, not a claim
that the entire newly appended document was hashed during that comparison.

### Executed independent run and attacks

The verifier's persisted `probe.py replay` executed the full tracked entry
point. Fresh run: `.cuda-build/binding-all-09e5e0c2/`; fresh native mutation
copy: `.cuda-build/gmfc10/`. Both focused lanes passed 149 tests; the full
suite passed 334; helper lines were 10/10; all four checker mutants were
caught. Both CUDA builds passed. All 17 native mutants and all standalone
controls held, with raw restoration verified.

Additional independently executed attacks:

- 288 invalid descendant report trees rejected; ordinary properties, text,
  escaped markup and CDATA accepted.
- Bounded pin histories of lengths 1 through 4, including removal, reversal,
  historical edits, valid unchanged/appended histories and invalid current
  rows, produced the expected outcomes.
- All 11 ambiguous mode combinations refused before builds.
- 100 real synthetic CLI refusal cases covered five generation values,
  identity variations, checksum corruption and REBIND/no input. Every refusal
  preserved raw checkpoint bytes and created no recovery/migration output.
- Every one of the 17 official kill XML files contained a genuine failing
  assertion, independently inspected rather than inferred from its status.
- A separately built divergent generation-gate mutant and a named native
  timeout test demonstrated that call-phase TimeoutExpired can count as a
  kill even when the baseline test fails identically. This intentionally
  failing test was not part of the approved baseline-green test set.

One extra native probe first failed compilation because of nvcc's nested
temporary-path limit. That attempt was retained as `build_failed`, not a kill;
the shorter isolated-path rerun built successfully. A version-only `cl /Bv`
diagnostic returned 2 because no source was supplied. Coverage.py was absent
in the Python 3.10 installation, whose diagnostic returned 1; no secondary
coverage layer was claimed or installed. Other limits are in the frozen files.

Phase 2's `phase2_compare.py` completed with exit 0: all 334 named cases and
outcomes match the builder, all required/control rows match, all 18 replay
source hashes match, all 41 independent source/spec/fixture hashes remain
unchanged, and all 11 previous assertion ASTs are preserved. The builder's
69-path source check has a different, explicitly bounded selection.

### V1 contract reconciliation and next human gate

The parent incorrectly labeled a stricter named-assertion interpretation as
human-approved in the initial verifier task. Approved A5 names the tests that
must kill the mutants; handoff 33 describes exit 1 with the named test failing.
A8 explicitly adds no new acceptance criterion. Neither establishes an
assertion-only exception policy. Phase 2 disclosed this input error instead
of treating it as an approved new product requirement.

The timeout/baseline observation remains real. It does not establish inflation
of the 17 official scores: their baseline suite passed and their kills were
individually confirmed as assertions. A call-phase exception caused by a DUT
mutation may legitimately be a behavioral test failure. Automatic rejection
of all such exceptions is not an already-approved rule.

The universal assertion-enforcement wording and tool-version labels have been
qualified without changing implementation. Proposed V1 disposition is a
contract/input-description issue, with optional strengthening separately
specified. Paul owns final grading and the choice to retain the approved
named-test-failure rule or approve new baseline/exception semantics. No new
policy, code fix or additional blind round is automatic from this observation.

Further rounds were explicitly authorized; historical failed rounds remain
recorded. An optional blind planted-defect canary was not run. Reference canary
and property tests are separate finite-domain checks, not universal proofs.
No additional types, random fuzz, broad coverage or shuffled-full-suite layer
was performed. Existing human exclusions remain. No source changed after
verification, no Git mutation occurred, and no live recovery artifact,
installed executable or shared database was touched.

## Human disposition and documentary closure, 2026-10-04 UTC

Paul was asked: "May I close this round under the existing named-test-failure
rule, leaving stricter exception handling for a separately approved change?"
He replied: "yes, what comes next?"

The subsequent documentation-only request was: "May I update the evidence
report and create handoff 41 now?" Paul replied: "yes".

This records acceptance of the disclosed V1 contract/input-description
correction and closure under the existing approved rule. It does not approve
an assertion-only classifier, stronger baseline discrimination or any new
behavior. Such changes require a separately approved specification and the
full applicable coding and verification loop.

The bounded Phase 2 PASS applies only to executable/test/spec state
`6cb35e838d997e339ec4ccdc1dcd51853fbb7dce`. The stricter-input Phase 1 FAIL,
prior failed rounds, handoffs 39/40 and frozen reports remain unchanged.
Handoff `2026-10-04-41-codex` records this later human decision without
rewriting those historical records.

This closure changes documentation only. No tests, CUDA builds, mutation
sweeps or independent verifier rounds were rerun for this update. The actual
fresh runs above remain the evidence for the unchanged candidate; this
appendix claims no new execution results.

Next: Paul reviews and commits the four documentary files named in handoff
41. A separate integration-review approval is then required. No staging,
commit, merge, rebase, push, installed-executable replacement, recovery run,
personal-artifact access or shared-database operation is authorized by this
closure. No coordination-database gate was recorded or changed. Current
shared-space readiness was not inspected and is not established here.
