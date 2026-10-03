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
deviation.

## Behavior → test mapping

| Spec item | Test(s) |
|---|---|
| F1 cross-build resume refused | `save_reject_generation` (host); `test_cli_restore_refuses_generation_mismatch` (real CLI) |
| F2 forgotten bump caught | `save_generation_canary` (ordering fingerprint pinned to the version) |
| F3 existing v1 saves still resume | `save_generation_migrate_v1` |
| F4 fail closed, no override | `save_generation_no_override` (value sweep); CLI test above; `.cu` generic refusal |
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

Canary negative control (home-grown checker must fail): reversed the fingerprint
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
independent Codex verifier remain pending Paul's commit; neither is an agent step.
