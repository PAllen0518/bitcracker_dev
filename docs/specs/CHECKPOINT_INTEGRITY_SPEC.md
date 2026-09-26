# Checkpoint / save integrity spec

Status: **draft — awaiting Paul's approval (gate 1). No code until approved.**

Scope: `BitCracker/btcrecover-master/multibit_cuda_threads.cu` save/restore.
Base commit: `a01d2b6` (`docs(cuda): explain parallel-generation chunk pool and merge invariants`).
Loop: Old Coder — SPEC -> RED -> GREEN -> REFACTOR -> GAUNTLET -> EVIDENCE.
Risk tier: **high** (checkpoint/save format + wallet identity). Rigor scaled up.

This spec does not authorize any Git mutation, dependency install, executable
replacement, or any run against real recovery artifacts. It authorizes, once
approved: the implementation, host-side tests, fault injection, and an evidence
report, all in an isolated worktree using synthetic fixtures only.

---

## 1. Verified current state (read this session, not assumed)

Source: `multibit_cuda_threads.cu` @ `a01d2b6`.

**Save record (lines 913-921).** No magic, no version, no hashes, no checksum:

```c
struct SaveState {
    char     tokenlist[512];   // offset    0
    char     wallet[512];      // offset  512
    uint64_t combo_idx;        // offset 1024
    uint64_t total_combos;     // offset 1032
    uint64_t passwords_checked;// offset 1040
    uint64_t perm_idx;         // offset 1048
    uint64_t typo_idx;         // offset 1056
};                             // sizeof = 1064
static const size_t OLD_SAVE_STATE_SIZE = 1048; // pre perm/typo saves
```

**Write path (lines 927-930)** — not atomic, not durable, return codes unchecked:

```c
FILE* f = fopen(path,"wb");
if (f) { fwrite(&s,sizeof(s),1,f); fclose(f); }  // no flush/fsync, no rename, no error check
```

Called every 30 s (line 1188) and once at end (line 1198), overwriting the
primary save in place. A crash or full disk mid-`fwrite` corrupts the only save.

**Restore validation (lines 1054-1113)** — the ONLY integrity check is combo count:

```c
} else if (total_combos != state.total_combos) { ... refuse ... }
```

Confirmed gaps:
- **No token-list binding.** A different token list that re-parses to the same
  `total_combos` passes validation → silently searches the wrong space.
- **No wallet binding.** Restoring against a different wallet is accepted.
- **No delimiter / typo binding.** `--delimiter` and all `--typos*` flags
  (lines 1038-1047) are re-supplied by the user and never checked. Wrong options
  with a coincidentally-equal combo count resume against the wrong space.
- **No corruption detection.** Any bit flip or truncation to 1064/1048 bytes is
  accepted.

**Wallet identity is available cheaply.** `load_wallet` (lines 436-455) yields
`h_salt[8]` + `h_enc[32]` — 40 bytes that uniquely identify the wallet without
storing wallet contents or path.

**HARD CONSTRAINT — Search60.** `cuda_threads_save60.bin` is **exactly 1064
bytes** (verified by `stat`, contents not read): the current struct format, ~76%
complete. The migration path below must resume it at the exact same
combo/perm/typo indices. Five `*.bak-*` copies are also 1064 bytes.

---

## 2. New save format (v1)

A fixed-layout record with a magic prefix, version, identity bindings, the
existing progress fields at the end, and a trailing checksum. Exact field packing
(padding, endianness) is pinned by RED round-trip + known-vector tests before
GREEN; the layout below is the contract.

| Field | Type | Purpose |
| --- | --- | --- |
| `magic` | `char[8]` = `"MBCKSV1\0"` | Distinguishes v1 from legacy (legacy byte 0 is token-list path text or `\0`). |
| `format_version` | `uint32` = 1 | Bumped on any layout change. |
| `header_size` | `uint32` | `sizeof(record)`; lets a future reader skip unknown tails. |
| `tool_version` | `uint32` | New `SAVE_TOOL_VERSION` constant; recorded, not gating. |
| `delimiter` | `uint8` + 3 pad | Exact delimiter byte used. |
| `tokenlist_hash` | `uint8[32]` | SHA-256 of the **raw token-list file bytes**. |
| `wallet_id_hash` | `uint8[32]` | SHA-256 of `h_salt(8) || h_enc(32)` — identity, not contents. |
| `typo_hash` | `uint8[32]` | SHA-256 of a canonical serialization of the full `TypoConfig` (every flag, count, and the `insert_charset` bytes). |
| `tokenlist_path` | `char[512]` | Convenience default for `--restore` (as today). Not an identity check. |
| `wallet_path` | `char[512]` | Convenience default (as today). Not an identity check. |
| `combo_idx` | `uint64` | Progress (unchanged semantics). |
| `total_combos` | `uint64` | Progress + coarse check (unchanged). |
| `passwords_checked` | `uint64` | Progress (unchanged). |
| `perm_idx` | `uint64` | Progress (unchanged). |
| `typo_idx` | `uint64` | Progress (unchanged). |
| `record_sha256` | `uint8[32]` | SHA-256 over every preceding byte. Integrity/corruption gate. |

Binding rationale: `tokenlist_hash` catches any file edit; `delimiter` + `typo_hash`
catch runtime-option drift that the file hash can't see; `wallet_id_hash` catches
a wrong wallet; `record_sha256` catches corruption/truncation independent of
identity. `total_combos` is kept as a fast pre-check but is no longer the sole gate.

Hashing: reuse the tool's existing host-side SHA-256 if one is host-callable;
otherwise vendor a small public-domain SHA-256 in a new host header. Either way,
RED pins it against published vectors (see §5). No new third-party dependency.

Paths are stored in the local save file only (never in docs/logs/DB), consistent
with today's behavior and the secret rule (§7).

---

## 3. Fail-loud restore

On `--restore`, in order, refusing (nonzero exit, specific message) on the first
failure and **never** falling through to a search:

1. **Format detect.** First 8 bytes == `magic` and size >= record size → v1.
   Else size ∈ {1064, 1048} → legacy (§4). Else → refuse ("unrecognized save").
2. **v1 integrity.** Recompute `record_sha256`; mismatch → refuse ("save file is
   corrupt or truncated"). Reject unknown `format_version` newer than this build.
3. **Load inputs.** Parse wallet (`h_salt`/`h_enc`), token-list bytes, delimiter,
   typos from supplied args (paths default from the save).
4. **Identity checks**, each with its own message naming the mismatch:
   - `tokenlist_hash` vs SHA-256(current token-list bytes)
   - `wallet_id_hash` vs SHA-256(`h_salt||h_enc`)
   - `delimiter` vs supplied delimiter byte
   - `typo_hash` vs SHA-256(canonical current `TypoConfig`)
   - `total_combos` vs re-parsed count (kept as a defense-in-depth pre-check)
5. All pass → resume at stored `combo_idx/perm_idx/typo_idx`.

No mismatch is downgraded to a warning. Messages state which binding failed and
that resuming would search a different space, so the user fixes inputs rather
than corrupting progress.

---

## 4. Versioning and migration (protects the 76%)

**Legacy read.** A non-v1 file of size 1064 is read through the legacy struct at
its known offsets (`combo_idx`@1024, `total_combos`@1032, `passwords_checked`@1040,
`perm_idx`@1048, `typo_idx`@1056). Size 1048 is the older format: same offsets for
the first three fields, `perm_idx = typo_idx = 0`.

**One-time confirmed re-bind.** A legacy save carries no identity to check, so the
tool cannot silently trust it. On legacy restore:

1. Load wallet + token-list + delimiter + typos from supplied/defaulted inputs.
2. Re-parse and require `total_combos` to match the legacy value (today's only
   check). Mismatch → refuse.
3. Compute the v1 identity hashes and **display them (hashes + progress + pct
   only, no contents)**.
4. Require **explicit interactive confirmation**: print the hashes/progress, then
   prompt the user to type a fixed token (e.g. `REBIND`) on stdin. Any other input
   → refuse. If stdin is not interactive (EOF / redirected, no answer) → refuse
   with an explanation rather than proceeding. This is a per-restore human
   decision, consistent with the approval-gate culture; never an automatic upgrade.
   (Operational note: the one-time migration must therefore be run interactively;
   once re-bound, later resumes read a v1 save and never prompt.)
5. On confirmation, resume at the exact legacy indices. The record is now bound;
   the next autosave writes a v1 record via the atomic path in §5.

**Proof the 76% is preserved (tested, not asserted):**
- `legacy_1064_migration_preserves_indices`: a synthetic 1064-byte blob with known
  indices → migration reader returns those exact values; resume start equals them.
- `legacy_rebind_roundtrip`: after confirmed re-bind, the first v1 save re-loads to
  the identical `combo_idx/perm_idx/typo_idx/passwords_checked/total_combos`.
- **No-clobber guarantee:** on legacy restore the tool copies the original file to
  `<path>.legacy` before the first v1 write, and the first v1 write uses the atomic
  temp+flush+rename path — so the original 1064 bytes are replaced only after a
  complete, flushed v1 file exists. A failed or unconfirmed migration never writes
  over the legacy save.
- The real `cuda_threads_save60.bin` is **never** used as a test input. Migration
  is exercised only on synthetic blobs here; the real file is touched only later,
  by Paul, running the built tool.

---

## 5. Atomic, durable writes

Replace the in-place `fwrite` with a checked temp-write + durable-flush + atomic
rename, all return codes inspected:

1. Write the full record to `<path>.tmp` in the same directory (same volume).
2. Check the `fwrite` count; on short write → error out of the save (keep old file).
3. Flush to stable storage: `fflush`, then `_commit(_fileno(f))` (the Windows
   `fsync` equivalent) / `FlushFileBuffers`. Check the return.
4. `fclose`, checked.
5. **Retain the prior generation on every save:** if `<path>` exists, atomically
   rename it to `<path>.prev` (`MOVEFILE_REPLACE_EXISTING`, replacing any older
   `.prev`). This keeps one known-good previous save at all times.
6. Atomically move the new file into place: `MoveFileExW(tmp, path,
   MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)` (or `ReplaceFileW`). Check
   the return.
7. On any failure: leave the previous good save (and `.prev`) intact, emit a loud
   warning, and — for the periodic autosave — keep the search running (a transient
   autosave failure must not kill a long run; it must not silently succeed either).

The temp file is per-target and cleaned up on failure. The brief window between
steps 5 and 6 where `<path>` is momentarily absent is covered by `<path>.prev`
holding the last good save, so a crash there is still recoverable. On success the
primary path ends up with a valid save (as today) plus a retained prior generation,
and the write is crash-safe.

---

## 6. Test strategy (host-side, no GPU)

CI does not build the CUDA tool, so the logic is factored into a host-testable
unit — a new header (working name `save_format.hpp`) holding the record layout,
SHA-256, serialize/parse, validate, migrate, and atomic-write (with an injectable
write/flush/rename backend). The `.cu` includes it; a host C++ test harness
compiles it without CUDA, mirroring the existing `tests/cuda_host_contracts.hpp`
pattern. Tests are added to that harness and to `tests/test_cuda.py`.

**RED (prove failing before GREEN):**

| Test | Asserts | Expected RED |
| --- | --- | --- |
| `sha256_known_vectors` | Host SHA-256 matches published vectors ("", "abc", 448/896-bit cases). | New code absent → fails to build/assert. |
| `v1_roundtrip` | save→load returns identical fields; stable hashes. | v1 record absent. |
| `save_binds_identity` | v1 record stores tokenlist/wallet/delimiter/typo hashes + versions. | Fields absent. |
| `restore_rejects_wrong_tokenlist_same_count` | Two different token lists with equal `total_combos`: second refuses. | Today accepts silently. |
| `restore_rejects_wrong_wallet` | Save bound to wallet A; wallet B refuses. | Today accepts. |
| `restore_rejects_delimiter_mismatch` | Changed delimiter refuses even at equal count. | Today accepts. |
| `restore_rejects_typo_mismatch` | Changed `--typos*` (incl. `insert_charset`) refuses. | Today accepts. |
| `restore_detects_corruption` | Any single-byte flip / truncation refuses via checksum. | Today accepts. |
| `save_is_atomic_and_durable` | Injected short-write / flush-fail / rename-fail leaves prior save intact; success replaces atomically. | Today: no temp/flush/rename/return checks. |
| `save_retains_prev_generation` | After two successful saves, `<path>.prev` holds the immediately-prior valid record. | No `.prev` rotation today. |
| `legacy_1064_migration_preserves_indices` | Synthetic 1064-byte blob → exact indices; resume matches. | Migration reader absent. |
| `legacy_1048_migration` | Older 1048-byte blob → first three fields exact, perm/typo=0. | Absent. |
| `legacy_rebind_requires_confirmation` | Legacy restore refuses on wrong/absent/EOF stdin input; only the exact typed token (`REBIND`) proceeds, then resumes at identical indices and writes valid v1. | Absent. |
| `legacy_rebind_roundtrip` | Post-rebind v1 re-loads to identical indices (no-clobber). | Absent. |

RED evidence: build the host target against the logic extracted **as-is** (no
behavior change yet) and record the failing output for the new-behavior tests.
For invariants that already hold (e.g. the combo-count refusal), inject a
deliberate defect into a disposable copy and show the test catches it — same
methodology as `CUDA_GENERATION_THROUGHPUT_EVIDENCE.md`.

**GAUNTLET (scaled up for high-stakes):**
- Full host suite + existing `python -m pytest tests/` green; `nvcc` build of the
  app still compiles with no new warnings.
- Fault injection: short write, flush failure, rename failure; truncated,
  bit-flipped, zero-length, oversized, and wrong-magic save files.
- Property/fuzz: random valid states round-trip; every single-byte mutation of a
  valid v1 record is rejected.
- Mutation testing on validation + migration branches (kill mutants that skip a
  binding check, accept a mismatch, or drop the confirmation gate).
- ASan/UBSan on the host test build **if** available on this MSVC setup; if not,
  report that explicitly rather than claiming an equivalent (per prior evidence
  honesty). Host race detection remains unavailable here — say so.

**EVIDENCE:** reproducible commands + actual results, RED artifacts and JSON/logs
retained under `.cuda-build` (same convention as the throughput evidence), plus a
`docs/specs`-adjacent evidence report. No success claim without the RED→GREEN
transition and a green gauntlet on record.

---

## 7. Constraints and delivery

- **Isolated worktree** `claude/checkpoint-integrity-spec` (already created from
  `a01d2b6`). Recovery artifacts stay out of it.
- **No Git mutations.** Do not stage, commit, merge, rebase, push, or delete
  branches. Paul owns all Git operations and lands the change.
- **Do not touch** `search*.txt`, `cuda_threads_save*.bin`, `search*.log`, or local
  `.gitignore` changes. Never use the real Search60 save as a test input.
- **No secrets** in code, tests, logs, docs, or output: no passwords, token-list
  or wallet contents, or candidate strings. Artifacts are referred to by name/hash.
- Temp probes, comparison binaries, logs, and JSON under `.cuda-build`; distinct
  executable names. Use the existing Python/MSVC/CUDA toolchain; no new deps.
- Expected files to change (at GREEN, not now): new `save_format.hpp`;
  `multibit_cuda_threads.cu` (use the header, add `--confirm-rebind`, wire atomic
  save + fail-loud restore); `tests/cuda_host_contracts.hpp`; `tests/test_cuda.py`;
  an evidence report. No installed-binary replacement.

Approval of this spec authorizes the implementation, isolated builds, host tests,
fault injection, and the evidence report described above — nothing else.

## 8. Resolved decisions (from Paul, 2026-09-26)

1. **Re-bind UX:** interactive typed confirmation (type `REBIND` on stdin);
   refuse on any other input or non-interactive stdin. Folded into §4.4.
2. **Prior-generation retention:** keep `<path>.prev` on **every** v1 save.
   Folded into §5.5 and the `save_retains_prev_generation` test. The `.legacy`
   copy at migration (§4) is retained in addition.
3. **tool_version:** add a `SAVE_TOOL_VERSION` constant. Folded into §2.

No open questions remain. This draft is ready for gate-1 approval.
