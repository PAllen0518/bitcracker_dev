# Checkpoint / save integrity — evidence

Status: **corrective round 2 complete; fuzz and mutation layers now pass.
Awaiting Codex re-review. Gate 5 remains blocked.**

Contract: [approved spec](CHECKPOINT_INTEGRITY_SPEC.md) (gate 1 cleared).
Base commit: `a01d2b6`. Worktree: `claude/checkpoint-integrity-spec`.
Toolchain (verified present): nvcc v13.0, MSVC 2022 BuildTools, Python 3.14 +
pytest + pycryptodome, RTX 2060. No new dependencies.

All commands run from `BitCracker/btcrecover-master` in the worktree. No Git
mutation, no installed-binary replacement, no recovery artifact touched.

## What was built

- New host header `save_format.hpp`: the legacy `SaveState` reader/writer moved
  verbatim from the `.cu` (behavior-preserving extraction) plus the v1 format:
  dependency-free SHA-256; `SaveRecordV1` (magic, versions, delimiter,
  token-list / wallet-id / typo hashes, paths, progress, trailing record
  checksum); `build_record_v1`, `validate_save`, atomic durable `save_bytes_v1`
  with prior-generation retention, `detect_save_kind`, and the `REBIND`
  confirmation predicate.
- `multibit_cuda_threads.cu`: includes the header; restore now detects format,
  fails loud on any v1 binding mismatch, and requires an interactive typed
  `REBIND` to migrate a legacy save (backing the original up to `<path>.legacy`);
  autosave and final save now write v1 atomically with `<path>.prev` retention.
  CUDA cryptography and the search kernels are unchanged.
- Tests: 18 host contracts in `tests/cuda_host_contracts.hpp` (+ names in
  `tests/test_cuda.py`); `tests/test_cuda_cli.py` updated to the v1 layout and
  extended with four end-to-end fail-loud / migration tests.

## Verified current-state findings (before building)

- Legacy `SaveState` is 1064 bytes; restore validated only `total_combos`; the
  writer was a bare `fopen`/`fwrite`/`fclose` with no checks. `cuda_threads_save60.bin`
  is 1064 bytes (the current struct format), ~76% run. (See the spec §1.)

## RED (features proven absent before implementing)

`validate_save` stubbed to accept-all and the writer left as a plain in-place
write. Built `optimized_test` and ran the 14 new contracts —
`.cuda-build/checkpoint-red.txt`:

```
save_sha256                PASS
save_roundtrip             PASS
save_identity              PASS
save_reject_tokenlist      FAIL  (different token list with equal combo count was not rejected)
save_reject_wallet         FAIL  (different wallet was not rejected)
save_reject_delimiter      FAIL  (different delimiter was not rejected)
save_reject_typos          FAIL  (different typo options were not rejected)
save_detect_corruption     FAIL  (corrupted record was not detected)
save_atomic                FAIL  (a faulted save falsely reported success)
save_prev                  FAIL  (previous generation was not retained)
save_migrate_1064          PASS
save_migrate_1048          PASS
save_rebind_confirm        PASS
save_rebind_roundtrip      PASS
```

The 7 failures are exactly the new behaviors (identity rejection for each of
token-list / wallet / delimiter / typos, corruption detection, atomic durability,
prior-generation retention). This is the spec's "inject defects, record
detections" evidence: with validation and the atomic writer absent, each
corresponding test detects the gap. The 7 passes rest on the SHA-256 helper (real
throughout), the record round-trip, and the existing legacy reader.

## GREEN

`validate_save` and `save_bytes_v1` implemented per spec; `main()` wired.
`.cuda-build/checkpoint-green.txt` — all 14 contracts PASS:

```
save_sha256 .. save_rebind_roundtrip : 14/14 PASS
```

## Gauntlet (scaled up for high-stakes save/crypto)

- **Full suite:** `python -m pytest tests/ -q` → **140 passed, 0 skipped** in
  ~23 s (both native targets built, GPU present). No regressions.
- **SHA-256 correctness:** `save_sha256` pins the empty-string, `"abc"`, and
  448-bit published vectors.
- **Fault injection (atomic writer):** `save_atomic` forces short-write,
  flush-failure, and rename-failure; each reports failure and leaves the prior
  save intact; a clean save replaces atomically. `save_prev` confirms
  `<path>.prev` holds the immediately prior generation.
- **Corruption / format:** `save_detect_corruption` (bit flip fails the record
  checksum → refused) and `detect_save_kind` (only 1216-byte-with-magic, 1064,
  and 1048 are recognized; any other size/prefix → Unknown → refused).
- **End-to-end fail-loud (real executable), `tests/test_cuda_cli.py`:**
  - swapped token list with the SAME combo count → refuses, no recovery file;
  - changed `--delimiter` → refuses (delimiter binding);
  - single-byte-corrupted save → refuses ("corrupt");
  - legacy save: wrong stdin leaves it untouched and refused; typed `REBIND`
    migrates, the run completes, the save becomes v1, and the original bytes are
    preserved as `<path>.legacy`.
  - existing CLI resume tests updated to the v1 layout still pass, including a
    mid-typo resume from a re-sealed v1 record (exact remaining count).
- **Migration preserves the 76%:** `save_migrate_1064` (Search60's exact format)
  and `save_migrate_1048` read the legacy struct at its documented offsets and
  return the exact combo/perm/typo/passwords indices; `save_rebind_roundtrip`
  confirms a re-bound v1 save re-loads to identical indices. The real
  `cuda_threads_save60.bin` was never used as a test input.
- **Builds:** `optimized_test` and `optimized` both exit 0 with **no compiler
  warnings** (`/W3`); logs in `.cuda-build/*.build.json`.
- **Lint:** `ruff check` on the two changed test files → all checks passed.

## Explicitly not done / unavailable (no substitutes claimed)

- Host ThreadSanitizer / ASan / UBSan are unavailable on this MSVC+nvcc setup, as
  in prior evidence. The save path adds no new concurrency (autosave runs on the
  single main-loop thread; producer threads never touch it), so this is a
  coverage gap, not a known race.
- No CUDA memcheck: this change is host-only; no kernel or device buffer changed.
- No independent verifier. No staging, commit, push, or attribution changes.
- The migration path was proven on synthetic 1064/1048 fixtures only. Migrating
  the real Search60 save is a deliberate, user-run, interactively-confirmed step.

## Final source identity (SHA-256)

- `save_format.hpp`: `89b75df44544021704ef0fe8e0e7c1ea0a146d7b737cb66e3be112293bb19832`
- `multibit_cuda_threads.cu`: `c9032479cec28bde25f0c52432704b0f4923f237f332ce2d05accc20a6aa9e04`
- `tests/cuda_host_contracts.hpp`: `6d7640fc34ca85e6af83cc2aa5203552ade604357851b15df043681b915e8bb5`
- `tests/test_cuda.py`: `4554c3ab16874a035ad76be05a258a42586b4c27a5f66f18f29d571be4d07a0e`
- `tests/test_cuda_cli.py`: `c1b268e34a3ca01f4a62d8ec030340af4607267b847eda9f326e4c1e15e15d78`

## Reproduction

```powershell
python tools/build_cuda.py optimized_test
python tools/build_cuda.py optimized
python -m pytest tests/ -q
```

`.cuda-build/` holds the built executables, per-contract RED/GREEN logs, and
build JSONs. It is not part of the change to commit.

## Coverage & public-fixture validation (gate 5 material)

**Public-fixture / secret audit.** All tests use only the public btcrecover
fixture `btcrecover/test/test-wallets/multibit-wallet.key` and synthetic or
`tmp_path` token files (`grep` over `tests/`). No test, source, or doc references
`cuda_threads_save60.bin`, `search60*`, a `*.bak-*` file, or any personal wallet
or token content; the sole `search60` string is a descriptive comment. No secret
(password, token-list bytes, wallet contents, candidate strings) appears in
`save_format.hpp`, the tests, the spec, the evidence, or the handoffs. The
recovered-password path writes only to `RECOVERED_PASSWORD.txt`; tests assert its
*absence* in the no-recovery cases.

**Branch coverage of the new logic** (host contracts + CLI tests; no automated
C++ line-coverage tool is available on this MSVC+nvcc host — mapping is by
inspection, stated without claiming an instrumented equivalent):

- `validate_save` — all 9 return branches exercised: `None` (roundtrip/identity),
  `BadMagic` (`save_reject_badmagic`), `BadVersion` (`save_reject_badversion`),
  `Corrupt` (`save_detect_corruption` + CLI corrupt), `Tokenlist`
  (`save_reject_tokenlist` + CLI swapped list), `Wallet` (`save_reject_wallet`),
  `Delimiter` (`save_reject_delimiter` + CLI delimiter), `Typos`
  (`save_reject_typos`), `ComboCount` (`save_reject_combocount`).
- `save_bytes_v1` — success, short-write, flush-fail, and rename-fail branches
  (`save_atomic`); prev retention (`save_prev`); `.legacy` copy + first v1 write
  on migration (CLI legacy test).
- `detect_save_kind` — V1, Legacy1064, Legacy1048 (`save_migrate_*`), and Unknown
  (garbage / zero-length / missing, `save_detect_unknown`).
- `build_record_v1` / `verify_record_checksum` / `sha256*` / `serialize_typo` /
  `wallet_id_hash` — `save_roundtrip`, `save_identity`, `save_sha256`,
  `save_reject_typos` (charset sensitivity); `sha256_file` and `sha256_hex` are
  exercised end-to-end by the CLI tests (real token-file hashing and the
  re-bind display).

Total: **18 host contracts + 18 CLI tests** covering the new behavior, all green.

The four branch contracts `save_reject_badmagic` / `save_reject_badversion` /
`save_reject_combocount` / `save_detect_unknown` were added in this coverage pass
to close gaps found by the branch inspection above, not during RED. Three of them
assert `validate_save` returns a non-`None` mismatch, so they would also have
failed against the RED accept-all stub; `save_detect_unknown` covers a defensive
branch of the (already-real) `detect_save_kind`.

- [ ] Public-fixture & coverage validation sign-off (COLLABORATION.md gate 5).
- [ ] User-owned commit + push (gate 7). Suggested commit set and message are in
      the handoff `docs/handoffs/2026-09-26-02-claude.md`.

---

# Corrective round — Codex review 2026-09-26-03 (defects 1–7)

Status: **fixes applied and verified in worktree `claude/checkpoint-integrity-fixes`
off master `740f8b0`. Awaiting Codex re-review, then Paul's gate 5 + commit.**

All work in an isolated worktree this round (Codex finding 8). No Git mutation,
no recovery artifact touched, synthetic 1064/1048 fixtures only. RED was captured
against the merged `740f8b0` source (restored via `git checkout`, plus a
signature-only shim so the new flush-seam test compiles against merged behavior).

## RED (against merged 740f8b0) — `.cuda-build/checkpoint-fixes-red.txt`

Host contracts:
```
save_reject_oversized    FAIL  (oversized magic-prefixed file still detected as v1)   [defect 4]
save_prev_failure        FAIL  (save advanced despite a .prev retention failure)       [defect 3]
save_backup_durable      FAIL  (backup reported success despite a flush failure)       [defect 2]
save_migrate_1064_raw    PASS  (rigor upgrade, defect 5 — reader already correct here)
```
CLI (`pytest -k`):
```
test_cli_migration_aborts_when_backup_fails            FAIL  [defect 1]
test_cli_found_password_not_printed_when_file_uncreatable FAIL  [defect 7]
test_cli_restore_refuses_wrong_wallet                  PASS  (coverage, defect 6)
test_cli_restore_refuses_wrong_typos                   PASS  (coverage, defect 6)
```
The defect-7 RED output shows the merged code printing `PASSWORD FOUND:
'btcr-test-password'` to stdout — the exact leak being fixed. Defects 5 and 6 are
a test-rigor upgrade and added CLI coverage of already-correct bindings, so they
pass pre-fix; that is stated rather than dressed up as RED.

## Fixes

- **1 (CRITICAL):** `multibit_cuda_threads.cu` migration now checks
  `copy_file_durable`; if the `.legacy` backup cannot be made durably it aborts
  fail-loud and leaves the original save byte-for-byte intact.
- **2 (HIGH):** `copy_file_durable` checks `fflush` and `_commit` return codes and
  renames with `MOVEFILE_WRITE_THROUGH`; it reports success only after a durable
  flush and rename. A `WriteFault` seam allows failure injection.
- **3 (HIGH):** `save_bytes_v1` fails loud and does not advance the primary if the
  `.prev` generation cannot be retained.
- **4 (HIGH):** `load_record_v1` and `detect_save_kind` require the file length to
  be exactly one record; trailing bytes are rejected as corruption.
- **5 (MEDIUM):** `static_assert`s pin `sizeof`/`offsetof` for `SaveState` and
  `SaveRecordV1`; `save_migrate_1064_raw` reads a raw explicit-offset fixture with
  nonzero perm/typo/passwords indices.
- **6 (MEDIUM):** new CLI tests exercise wrong-wallet and wrong-typo rejection.
- **7 (MEDIUM):** the recovered-password stdout fallback is removed; on file
  failure the tool errors WITHOUT the password (and writes it nowhere).

## GREEN — `.cuda-build/checkpoint-fixes-green.txt`

```
save_reject_oversized  PASS   save_prev_failure  PASS
save_backup_durable    PASS   save_migrate_1064_raw  PASS
Full suite: 148 passed, 0 skipped (~35 s, both targets built, GPU present)
Ruff (changed test files): all checks passed
optimized_test / optimized builds: exit 0, no compiler warnings
```

## Corrected final source identity (SHA-256 of LF-normalized content)

Normalization is now stated explicitly (Codex noted the earlier evidence did
not): each hash is over the file with `\r\n` collapsed to `\n`.

- `save_format.hpp`: `14d78f06be0f8e46b8266ef292480ec5f6bbb725df82e2b345b0f8dea7a088c8`
- `multibit_cuda_threads.cu`: `6823ef5cb016be518c72cfb4bd71c09d9124837603c43e6e61608c157c0a06dd`
- `tests/cuda_host_contracts.hpp`: `11396865a4d6519e5bce5f83fa004a6addc39b4b5637c40efe57954ca5be64de`
- `tests/test_cuda.py`: `84d6b879a318a49863ea1979edad5b5ae363e37ee24e7fe58095e43feb5c53b1`
- `tests/test_cuda_cli.py`: `e9816af5ddf5fba18ddf78ff632e1759afac727047caed1191ea3697871495f3`

## Still not done / unavailable (no substitutes claimed)

- No automated C++ line-coverage tool on this MSVC+nvcc host; branch mapping is by
  inspection. ASan/UBSan/TSan unavailable, as before. Defect 2's durability is
  verified by the flush/rename fault seam (a GREEN contract); its ignored-return
  consequence pre-fix is shown by the defect-1 and defect-3 RED failures.

---

# Corrective round 2, Codex findings F1 to F5

Status: **implementation and required gauntlet layers pass. Awaiting Codex
re-review, then Paul's gate 5 decision.**

Approved-spec record: gate 1 was cleared against hash prefix `4e742c73`.
Paul's F1 to F5 instruction is the approved corrective amendment. The spec now
records approved status and the corrected publish order. Its current
LF-normalized SHA-256 is
`b8ac5fd9c6878ab3af0b0e4ba2aa2ffcb73b42cbf8916c67968ba4e4dd872fa4`.

All work stayed in `claude/checkpoint-integrity-fixes` off `740f8b0`. No Git
operation ran. Tests used synthetic data or the existing public fixture. No
recovery artifact was read, copied, or modified. All build, pytest, fuzz, and
mutation artifacts are under `BitCracker/btcrecover-master/.cuda-build`.

## RED by finding

Artifact: `.cuda-build/checkpoint-integrity-round2-red.txt`.

Command:

```powershell
python tools/build_cuda.py optimized_test
python -m pytest tests/test_cuda.py -q `
  --basetemp=.cuda-build/pytest-red `
  -k "save_three_generation_rename_failure or save_file_state_error or save_commit_failure or save_fuzz"
```

Result: **4 failed, 102 deselected**.

- **F1:** `save_three_generation_rename_failure` failed with `.prev changed
  before the primary rename committed`. With generation 2 primary and
  generation 1 `.prev`, injected `RenameFail` incorrectly published generation
  2 to `.prev` before the primary rename.
- **F2:** `save_file_state_error` failed as an explicit unimplemented contract.
  Binary `file_exists()` had no Present/Absent/Error result and no fail-closed
  Error path.
- **F3:** `save_fuzz` failed as an explicit unimplemented contract. No property
  contract or mutation driver existed.
- **F4:** `save_commit_failure` failed as an explicit unimplemented contract.
  `CommitFail` did not exist and `FlushFail` short-circuited before `_commit`.
- **F5:** documentation-only correction, the spec still said draft.

## GREEN by finding

Artifact: `.cuda-build/checkpoint-integrity-round2-green-focused.txt`.

Result: **4 passed, 102 deselected in 0.66 s**.

- **F1:** the current primary is durably staged to `<path>.prevtmp`; the new
  primary commits first; only then is `.prevtmp` published as `.prev`. The
  three-generation `RenameFail` contract confirms both published files are
  byte-for-byte unchanged on primary failure.
- **F2:** `file_state()` uses `GetFileAttributesA` and returns `Present`,
  `Absent`, or `Error`. Only `ERROR_FILE_NOT_FOUND` and
  `ERROR_PATH_NOT_FOUND` mean absent. An injected attribute error refuses to
  advance the primary or create `.prev`.
- **F3:** `save_fuzz` runs 32 deterministic random valid records. Every record
  round-trips byte-for-byte. For every record, each of the 1,216 byte positions
  is flipped separately, for **38,912 single-byte corruptions**, and every one
  fails `verify_record_checksum`.
- **F4:** `CommitFail` is distinct from `FlushFail` in `copy_file_durable` and
  `save_bytes_v1`. Both injected commit failures return false, preserve the
  published destination, and clean the temporary write.
- **F5:** the spec status is approved, and section 5 now matches the corrected
  staged prior-generation publish order.

## Final fresh gauntlet

Final combined log:
`.cuda-build/checkpoint-integrity-round2-final-fresh.txt`.

### Ruff

```powershell
$env:RUFF_CACHE_DIR = '.cuda-build/ruff-cache'
python -m ruff check tools/validate_save_format.py tests/test_cuda.py tests/test_cuda_cli.py
```

Result: **All checks passed**, Ruff 0.16.0.

### Mutation checker negative control

```powershell
python tools/validate_save_format.py --negative-control
```

Result: expected exit **1**. The deliberately surviving no-behavior mutant
built with exit 0, its test passed with exit 0, the driver reported
`survived`, and exact source restoration was `True`. This proves the driver
does not report a survivor as killed.

Artifact:
`.cuda-build/save-format-mutation/mutation-negative-control.json`.

### Required mutation run

```powershell
python tools/validate_save_format.py
```

Result: **5/5 killed**. Every mutant built successfully with exit 0, then its
targeted test failed with exit 1. Every exact source restoration was `True`.

| Mutant | Killing test | Result |
| --- | --- | --- |
| Drop token-list binding check | `save_reject_tokenlist` | killed |
| Drop wallet binding check | `save_reject_wallet` | killed |
| Drop `.prev` fail-loud preflight | `save_prev_failure` | killed |
| Weaken exact-size check | `save_reject_oversized` | killed |
| Drop migration backup abort | `test_cli_migration_aborts_when_backup_fails` | killed |

Artifacts: `.cuda-build/save-format-mutation/mutation-report.json`, plus one
build log and one pytest log per mutant. The driver fails closed on a missing
or duplicate source match, build failure, surviving mutant, or failed exact
source restoration. Build failures do not count as kills.

### Restored-source builds

```powershell
python tools/build_cuda.py optimized_test
python tools/build_cuda.py optimized
```

Results:

- `optimized_test`: exit 0, compiler warnings 0.
- `optimized`: exit 0, compiler warnings 0.

Build records: `.cuda-build/optimized_test.build.json` and
`.cuda-build/optimized.build.json`.

### Full suite, including fuzz

```powershell
python -m pytest tests -q --basetemp=.cuda-build/pytest-final-fresh
```

Result: **152 passed, 0 failed, 0 skipped in 33.29 s**, pytest 8.4.1.
The `save_fuzz` property contract is part of this suite.

## Final LF-normalized source identity

Each SHA-256 is over UTF-8 content after collapsing `CRLF` to `LF`.

- `save_format.hpp`:
  `a161aea46bf1e3bc49f879aee6a5d270b81f0190784a56823c57db4230344c6e`
- `multibit_cuda_threads.cu`:
  `6823ef5cb016be518c72cfb4bd71c09d9124837603c43e6e61608c157c0a06dd`
- `tests/cuda_host_contracts.hpp`:
  `4cd42e22342c307112727026fae6f833db5eb214521add0bd6968d31fac5f7c9`
- `tests/test_cuda.py`:
  `12968a6a45311452dea8c3ce4f09cfb3c6424dee94b8beab130e11a9cae58075`
- `tests/test_cuda_cli.py`:
  `e9816af5ddf5fba18ddf78ff632e1759afac727047caed1191ea3697871495f3`
- `tools/validate_save_format.py`:
  `7dc96d8d39589208b15167cf5069ca1177bd6a36ca202b70ff0a71af158eb601`

## Known limitations of the .prev publication (independent review, 2026-09-26-06)

Two narrow, non-blocking behaviors of `save_bytes_v1` stage 5, noted during
Claude's independent verification. Neither loses a generation:

- If `.prevtmp → .prev` fails *after* the new primary has already committed
  (stage 4 succeeded), `save_bytes_v1` returns `false` even though the new
  primary is durably saved, and leaves the `.prevtmp` file behind (it still
  holds the prior generation, so nothing is lost). The autosave caller then
  logs "save failed; previous save retained," which is slightly misleading in
  that rare case because the primary did in fact advance. The stage-3 preflight
  already rejects known-unpublishable `.prev` targets (directory / read-only)
  before advancing, so this window requires a `.prev` that passes the preflight
  yet still fails an atomic write-through rename.
- The stage-3 preflight cannot anticipate every possible rename failure (for
  example a transient sharing violation on `.prev`); such a failure falls into
  the same post-commit stage-5 window above. Accepted because no generation is
  lost and the primary save is durable.

## Remaining limits and gates

- Automated C++ changed-line coverage remains unavailable on this MSVC and
  nvcc host. Branch mapping remains inspection-based and is not presented as
  instrumented coverage.
- ASan, UBSan, TSan, and host race detection remain unavailable here. No
  substitute is claimed.
- Independent verification is not performed against this final state. Codex
  re-review is the next required step.
- Gate 5 remains blocked until Paul reviews the public-fixture, coverage, fuzz,
  mutation, and corrective evidence.
- Gate 7 remains blocked. Paul alone stages, commits, and pushes.
