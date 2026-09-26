# Checkpoint / save integrity — evidence

Status: **implementation complete; correctness gauntlet passed. Awaiting human
gates (public-fixture/coverage sign-off, then the user-owned commit).**

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
