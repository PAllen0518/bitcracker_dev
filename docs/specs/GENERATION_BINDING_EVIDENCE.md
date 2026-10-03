# Generation-version binding — evidence report

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
survivor control, unchanged by this work).

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
| `validate_save_format.py` | **15/15 killed**, all `restored=True` |
| `--negative-control` | survivor survived, exit 1 as designed |
| Tree after run | only the intended edits; no mutant bytes left |

### Claims now, and their limits

- **F2:** a forgotten bump makes the branch's suite fail. The canary drifts on any
  order change in the hashed cases, and the only way back to green is a new row
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
gauntlet and verifier Round 2 are pending.
