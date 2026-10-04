# Generation-version binding spec

Status: **APPROVED by Paul — gate 1 cleared 2026-09-27, incl. the §3 decision.**
Verbatim approval: "The spec and the §3 call are approved". Implementation, host
tests, fault injection, and the evidence report are authorized in the isolated
worktree; no Git mutation, dependency install, or executable replacement.

Scope: bind a BitCracker v1 checkpoint to the candidate-generation ordering of the
build that wrote it, so `--restore` refuses to resume a save whose progress indices
were produced by a different generation order. Files: `save_format.hpp`,
`multibit_cuda_threads.cu`, `tests/cuda_host_contracts.hpp`, `tests/test_cuda.py`,
`tools/validate_save_format.py`, and an evidence report.
Base commit: `a2f171f` on `master` (PR #3 merge; verified this session).
Loop: Old Coder — SPEC → RED → GREEN → REFACTOR → GAUNTLET → EVIDENCE.
Risk tier: **3 (high)** — silent skip/repeat of candidates is a data-loss mode on a
recovery tool. This is the exact failure the existing `validate_save` gate fights;
getting this gate wrong reintroduces it.

This spec authorizes nothing on its own. Approval of THIS document authorizes, once
granted: the implementation, host-side tests, fault injection, and an evidence
report, in an isolated worktree using synthetic fixtures only. It does **not**
authorize any Git mutation, dependency install, executable replacement, `.gitignore`
change, or any run against a real recovery save.

---

## 1. Verified current state (read this session, not assumed)

Source: `save_format.hpp` / `multibit_cuda_threads.cu` @ `a2f171f`.

- **The field already exists.** `SaveRecordV1` (packed, `static_assert`ed to 1216 B)
  carries `uint32_t tool_version` at a fixed offset, set from
  `SAVE_TOOL_VERSION = 1` in `build_record_v1`. Every v1 save ever written by this
  codebase therefore carries `tool_version == 1`.
- **The field is not gating.** `validate_save()` checks, in order:
  `BadMagic → BadVersion → Corrupt → Tokenlist → Wallet → Delimiter → Typos →
  ComboCount`. It never reads `tool_version`. `CHECKPOINT_INTEGRITY_SPEC.md` §2/§8
  says this was deliberate: "recorded, not gating." That is the gap this task
  closes.
- **The generation ordering has a trusted oracle.** `reference_candidates()` in
  `tests/cuda_host_contracts.hpp` deterministically enumerates the exact candidate
  sequence (radix mixing over token lines, `std::next_permutation` over free
  tokens, typo expansion via `generate_typo_variants`). `assembly_contract`,
  `resume_contract`, and `parallel_contract` already pin the live generator to this
  oracle byte-for-byte. This oracle is what "the generation order" means, testably.
- **Baseline contract count.** `run_host_contract()` dispatches **26** `save_*`
  contracts (`save_sha256` … `save_rebind_roundtrip`), a subset of which run under
  `pytest` via the parametrized list in `tests/test_cuda.py`. All 26 plus the
  non-`save_` contracts are the regression baseline that must stay green.
- **Restore flow.** `multibit_cuda_threads.cu` (~L1116–1206) computes current
  input hashes, calls `validate_save`, fails loud (nonzero exit, specific message)
  on mismatch, has a legacy `REBIND` migration path, then `write_v1_save`.

---

## 2. Failure model (Tier 3 — the ways this specific change can hurt)

| # | Failure mode | Consequence | Gauntlet layer that catches it |
|---|---|---|---|
| F1 | Cross-build resume: build B's generation order differs from build A's, but `--restore` accepts A's save and resumes at A's indices. | **Silent skip/repeat of candidates → the password is skipped and never found. Data-loss.** | `save_reject_generation` contract: mismatched generation refuses with `SaveMismatch::Generation`. |
| F2 | Forgotten bump: a developer changes generation ordering but does not bump the generation constant. Old and new builds share a number but not an order. | Same as F1, and undetectable by the number alone — the number lies. | `save_generation_canary` contract: pins a fingerprint of the reference-oracle output to the current generation number; any ordering change without a bump fails the build. |
| F3 | Migration fault: existing on-disk v1 saves (all carry `tool_version == 1`) stop resuming after this change ships. | The 76%-complete real run and its `.prev`/backups become unrestorable. | `save_generation_migrate_v1` contract: a v1 record with `tool_version == 1` resumes under the current build (generation 1). |
| F4 | Fail-open gate: the check is added but a mismatch is downgraded to a warning, or an override/rebind path lets the user accept a different order. | Reintroduces F1 through a back door. | Negative constraint N4 + `save_generation_no_override` contract: no code path resumes a generation-mismatched save. |
| F5 | Wrong-order gate: generation is checked after identity, so a save that mismatches both reports a confusing/incorrect first cause, or ABI drift breaks fixed-offset decoding. | Misleading diagnosis; or a layout change silently corrupts every existing save. | `save_generation_order` contract + the existing 1216-byte / offset `static_assert`s and all 26 baseline contracts staying green. |
| F6 | Spurious bump: the number is bumped when the order did not change. | Existing saves refuse to resume (fail-loud) — an availability hit, not data loss. | Accepted lesser harm; documented limit in §7. The canary catches missing bumps, not extra ones. |

---

## 3. Design decision — repurpose `tool_version` as the gating generation number

The handoff (§7) left one fork for the spec to settle: **add a new
`generation_version` field, or repurpose the existing `tool_version`.**

**Decision: repurpose `tool_version` as the gating generation-version integer.**
Rename its intent (constant → `SAVE_GENERATION_VERSION`, starting at `1`; field
comment updated) and make `validate_save` gate on it. Rationale:

- **Zero ABI change.** `SaveRecordV1` stays 1216 bytes; every `static_assert` and
  fixed-offset decode in the harness and `validate_save_format.py` is untouched.
  Adding a field would move offsets, break the 26 fixed-offset contracts, and force
  a `format_version` bump to v2 with its own migration path — a large blast radius
  for no behavioral gain.
- **Transparent back-compat (F3).** Every existing v1 save carries
  `tool_version == 1`. If the current build ships `SAVE_GENERATION_VERSION == 1`,
  those saves match and resume with no migration step. The number only ever
  advances the next time generation ordering actually changes.
- `tool_version` has no other reader, so no semantics are lost by regrading it from
  "recorded" to "gating."

Optional, non-gating diagnostic (kept out of the gate): record a short executable
build tag for human triage only. **Deferred** — it is not needed to close the gap
and adds surface; call it out here so a later spec can add it deliberately rather
than by drift.

Rejected in the handoff and not revisited: hashing the `.exe` (Option 2, brittle),
hashing generation source (Option 3, canonicalization cost).

---

## 4. Behavior (the contract)

### 4.1 New constant and enum value
- `SAVE_GENERATION_VERSION = 1` in `save_format.hpp`. Documented: **bump this by one
  whenever any change alters the candidate-generation ordering** (token radix
  mixing, permutation order, typo expansion order/content). A comment points the
  developer at the `save_generation_canary` contract that enforces the bump.
- `enum class SaveMismatch` gains `Generation`, with a `mismatch_message` entry:
  *"save was written by a build with a different candidate-generation order; resume
  it with the matching build or start a new search."*

### 4.2 `validate_save` gains one check, in this position
New order:
`BadMagic → BadVersion → Corrupt → **Generation** → Tokenlist → Wallet → Delimiter
→ Typos → ComboCount`.
Generation sits **after** structural checks (bytes intact) and **before** identity
checks (inputs), because if this build cannot reproduce the save's ordering, the
stored progress indices are meaningless regardless of which inputs are supplied.
The check: `if (r.tool_version != SAVE_GENERATION_VERSION) return
SaveMismatch::Generation;`

### 4.3 Restore path
On generation mismatch, `--restore` fails loud (nonzero exit, the message above)
and **never** falls through to a search. There is **no** override, no confirm-token,
no rebind for a generation mismatch — unlike the legacy `REBIND` path, which
re-binds a save that carries *no* ordering claim. A v1 generation mismatch is an
affirmative statement that the orders differ; accepting it is the data-loss bug.

### 4.4 Write path
`build_record_v1` writes `r.tool_version = SAVE_GENERATION_VERSION` (mechanically
identical to today at value 1; the meaning, not the byte, changes).

---

## 5. Acceptance criteria → new host contracts (RED before GREEN)

Each new contract is added to `run_host_contract()` and to the `test_cuda.py`
parametrized list. Each must be observed failing before its implementation exists;
where it guards behavior that partially exists, RED is shown by a throwaway mutant.

| Contract | Asserts | Expected RED |
|---|---|---|
| `save_reject_generation` (F1) | A valid v1 record with `tool_version != SAVE_GENERATION_VERSION` → `validate_save` returns `SaveMismatch::Generation`; identity/checksum all otherwise valid. | Today: field unread → returns `None`, save accepted. |
| `save_generation_order` (F5) | A record that mismatches **both** generation and tokenlist reports `Generation` first (order proof). | No `Generation` value exists. |
| `save_generation_migrate_v1` (F3) | A record with `tool_version == 1`, current build generation `1`, all inputs matching → resumes at the stored indices (no refusal, no rebind). | New behavior; also guards against a wrong default that would break existing saves. |
| `save_generation_no_override` (F4) | No restore code path resumes a generation-mismatched save; the mismatch is not downgraded to a warning. | Verifies the fail-closed property explicitly. |
| `save_generation_canary` (F2) | A stored fingerprint (SHA-256 of the concatenated `reference_candidates` output over a fixed fixture set) equals the value pinned for `SAVE_GENERATION_VERSION`. Changing generation order without bumping the number → fingerprint drifts → fail. | Locks ordering to the number so a forgotten bump is a red build, not a silent data-loss ship. |

Negative-control note (per gauntlet checker rule): `save_generation_canary` is a
home-grown gate. Before trusting its pass I will (a) run it against a deliberately
reordered oracle and watch it go red, then (b) confirm it goes green again only
after the pinned fingerprint is updated *and* the number bumped — proving it
enforces the bump, not just a spelling. Recorded in EVIDENCE.

---

## 6. Gauntlet (Tier 3 floor)

- **Full host suite green**: all 26 `save_*` contracts + every other contract, plus
  `python -m pytest tests/`. Zero new failures against the `a2f171f` baseline.
- **ABI invariants**: `sizeof(SaveRecordV1) == 1216` and all offset `static_assert`s
  still compile; `tools/validate_save_format.py` still decodes real-layout records.
- **nvcc build**: the app still compiles, no new warnings.
- **Mutation**: on the new `Generation` branch and its ordering position — a mutant
  that skips the generation check, that accepts a mismatch, that reorders it after
  identity, or that compares against the wrong constant must be killed.
- **Property/fuzz**: extend the existing `save_fuzz` style — random valid records
  with random `tool_version` values resume iff the value equals the build constant.
- **Canary negative control**: §5 note, both directions.
- **Adversarial pass**: one explicit attempt to resume a generation-mismatched
  synthetic save through the built CLI and confirm it refuses.
- Layers unavailable on this MSVC/CUDA host (e.g. host thread sanitizers) are
  reported as skipped-with-reason, not claimed.

---

## 7. Negative constraints (must survive) and known limits

- **N1** `SaveRecordV1` on-disk layout is unchanged: 1216 bytes, all offsets fixed,
  `format_version` stays `1`.
- **N2** All 26 baseline `save_*` contracts and every non-`save_` contract stay
  green. No existing assertion is edited to accommodate this change.
- **N3** No new third-party dependency; host SHA-256 and the existing harness only.
- **N4** No code path resumes a generation-mismatched v1 save — no warning
  downgrade, no override flag, no rebind. Fail closed.
- **N5** The real `cuda_threads_save*.bin` files are never test inputs; synthetic
  records only. No secrets (passwords, wallet/token contents, candidates) in code,
  tests, logs, or docs.
- **Known limit (F6)**: the number is a developer-maintained integer. The canary
  forces a bump when order changes; it does **not** prevent a *spurious* bump, whose
  only harm is a fail-loud refusal of resumable saves (availability, not data loss).
- **Known limit**: gating protects only saves written *after* this ships plus
  existing `tool_version == 1` saves under generation `1`. It cannot retroactively
  detect that two past builds at the same recorded number had different orders — no
  historical record exists to check against. Documented, not fixed.

---

## 8. Setup / delivery plan

- **Isolated worktree** off `a2f171f` (name at Paul's discretion, e.g.
  `claude/generation-binding`). Recovery artifacts stay out of it.
- **No Git mutations by the agent.** Paul owns all staging, commits, merges, and
  gate approvals. Commands will be handed to him in **cmd.exe** syntax.
- **Files changed at GREEN** (not now): `save_format.hpp` (constant, enum value,
  message, `validate_save` check), `multibit_cuda_threads.cu` (write path comment/
  wiring only — no behavior change at generation 1), `tests/cuda_host_contracts.hpp`
  (5 new contracts + dispatch), `tests/test_cuda.py` (parametrize the 5),
  `tools/validate_save_format.py` (surface the generation field in its decode/report
  if it decodes `tool_version`), and `docs/specs/GENERATION_BINDING_EVIDENCE.md`.
- **No installed-binary replacement.** Build artifacts and logs under `.cuda-build`,
  consistent with prior evidence.

---

## 9. Coordination

Per `docs/COLLABORATION.md` ownership: **Claude = builder** for this binding;
**Codex = OC-protocol / verifier governance owner.** Codex is running the Old Coder
validation of this task in parallel; the spec-review split follows that ownership.
This spec is written for an outside reviewer (Codex) to check against the code, not
just for Paul.

## 10. Open approval gate (human-only)

- [x] **Gate 1 — SPEC approval.** Cleared 2026-09-27. Paul approved this document
  and the §3 decision (repurpose `tool_version` rather than add a field) verbatim:
  "The spec and the §3 call are approved". RED/GREEN work is now authorized in the
  isolated worktree per §8.

No open questions remain.

---

## Amendment 1 — verifier Round 1 corrections (APPROVED 2026-10-03)

Status: **APPROVED by Paul 2026-10-03**, verbatim: "Amendment 1 approved". The
approved text is the draft whose spec file hashed to SHA-256
`9b491d5897c6a883296ed9b5d2b7a8df47fae5c72afb91d78943b1bb662b7960`; only this status
paragraph and the heading changed afterwards. Written in response to
`docs/handoffs/2026-10-03-30-codex.md` (verifier Round 1: failed). Sections 1–10 above remain
the approved base (SHA-256 `a829be75…e329b4fd`); where this amendment conflicts, it
governs. Invariants N1–N5 are unchanged.

### A1. Canary covers every generation-affecting typo mode (V1)

- `generation_fingerprint` gains one battery case per typo mode not already covered,
  each mode enabled **alone**: capslock, delete, closecase (closecase uses a synthetic
  token with a case transition). Existing cases are kept. Swap, repeat and insert stay
  covered by the existing typo case.
- **One-time gen-1 repin, no bump.** Growing the battery changes the gen-1 fingerprint
  without changing candidate order, so the version stays `1` and existing saves keep
  resuming (F3). This is allowed only because no pin table has reached the integration
  branch yet (`master` has none; see A2 bootstrap). The new pin is derived once and
  recorded in EVIDENCE with the old value.
- **Known cost, accepted:** after merge, the battery itself is append-only through A2.
  Changing the battery later changes the fingerprint and therefore needs a version bump,
  which refuses existing saves (F6, availability, not data loss). A1 makes the battery
  complete now to keep that rare.

### A2. Bump enforcement: append-only pins (V2), replaces §5's canary claim

The §5 claim that the canary "goes green only after the fingerprint is updated *and*
the number bumped" was false for an editable pin table. New design:

- The pin rows in `save_generation_canary_contract` sit between the marker comments
  `// GENERATION-PINS-BEGIN` and `// GENERATION-PINS-END`, one `{version, "hex"},` row
  per line.
- New pure-Python checker `check_generation_pins(base_text, current_text,
  generation_version)` plus a pytest that runs it on the real repo. It fails unless:
  1. the base rows are an exact prefix of the current rows (no edit, delete or reorder);
  2. versions are contiguous and ascending from 1;
  3. every fingerprint is unique (also blocks a bump that has no real order change);
  4. the last row's version equals `SAVE_GENERATION_VERSION` parsed from `save_format.hpp`.
  The C++ canary still requires the live fingerprint to equal the pin for the current
  version.
- **Base:** the file at `git merge-base HEAD <ref>`, where `<ref>` is env
  `GENERATION_PINS_BASE_REF` (default `master`). If the base file has no markers
  (bootstrap, the current state), base rows are empty. If git or the ref is
  unavailable, the test **fails** with a message naming the env var. It never skips.
- Net effect: after an order change, the only green path is a new row plus a version
  bump. Repinning row 1 fails (1). A duplicate version fails (2). A new row without a
  bump fails (4). A bump that reuses a fingerprint fails (3).
- The canary failure message drops "or the pinned fingerprint is stale" and says:
  never edit an existing pin; bump `SAVE_GENERATION_VERSION` and append a row.
- **Persisted negative controls, both directions** (pure-Python fixture tests of the
  checker): repin without bump → fail; append row without bump → fail; duplicate
  fingerprint → fail; delete or reorder → fail; bump plus new unique row → pass.
- **Known limits (narrowed claim, stated plainly):** this blocks honest mistakes on a
  branch measured against the integration branch. It does not stop a deliberate edit
  of the checker, or an edit committed directly to `master` (there, merge-base equals
  HEAD). F2's claim becomes "a forgotten bump fails the branch's suite", not that it is
  impossible.

### A3. Randomized generation-value property (V3)

New host contract `save_generation_property`: explicit cases `SAVE_GENERATION_VERSION`,
`0`, `SAVE_GENERATION_VERSION ± 1`, `0x7fffffff`, `0x80000000`, `0xffffffff`, then
4,096 values from `std::mt19937_64(20261003)` cast to `uint32_t`. Each is written into a
valid record, resealed, and passed to `validate_save`; the expected result is `None` iff
the value equals the build constant, otherwise `Generation`. Added to the dispatch and
the pytest list. Evidence stops calling the six-value test a "sweep".

### A4. The refusal message is behavior (V4)

The exact §4.1 message text is pinned two ways: a host contract asserting
`mismatch_message(SaveMismatch::Generation)` equals it, and the CLI generation test
asserting the full string appears in stderr (not just the word "generation").

### A5. Persisted mutants and a real resume test (V5)

- `tools/validate_save_format.py` gains multi-edit mutants (several exact-once edits,
  across files if needed, all restored byte-exact or the run fails), plus:
  `accept_generation_mismatch` (`!=`→`==`), `generation_after_tokenlist` (check moved
  below the token-list check), `generation_wrong_constant` (compare to `0u`),
  `generic_generation_message` (V4), `canary_capslock_reorder`,
  `canary_delete_reorder`, `canary_closecase_reorder` (V1, reference-generator
  reorders) and `canary_permutation_reverse` (the V2 attack). Each names the test that
  must kill it. All must be killed; the existing six stay.
- Disclosed survivor, not persisted as a required kill: comparing against
  `SAVE_FORMAT_VERSION` is indistinguishable at generation 1 (both are 1).
- `save_generation_migrate_v1` keeps its validation check, and its F3 mapping moves to
  a new CLI test, `test_cli_restore_resumes_gen1_save_at_stored_position`. It autosaves
  a synthetic search, confirms `tool_version == 1` in the file, sets a nonzero mid-run
  position, reseals, restores, and asserts exit 0 with exactly the remaining count
  checked. Synthetic fixtures only.

### A6. Evidence corrections

`GENERATION_BINDING_EVIDENCE.md` gets a new dated section; earlier sections are kept
as history and marked superseded where wrong. It must: withdraw "F2 forgotten bump
caught" in favour of the A2 claim and limits; disclose the original missing typo modes
and the repin-without-bump result; replace "value sweep"; correct the F3 and message
mappings; record verifier Round 1 (source `b093dc8`/`a8403da`, attacks, Python 3.14
substitution, failed status); reconcile the "committed-source run pending" wording with
handoff 29; and point every mutation and control at a persisted runner instead of prose.

### A7. Files and sequencing

Files: `tests/cuda_host_contracts.hpp`, `tests/test_cuda.py`, `tests/test_cuda_cli.py`,
`tools/validate_save_format.py`, a new `tests/test_generation_pins.py`, this spec and
the evidence report. `save_format.hpp` changes only if needed to expose the message
for A4; there is no behavior change and no ABI change. `multibit_cuda_threads.cu` and
`cuda_typos.hpp` are edited only inside the mutation harness's restored copies. Order:
RED for each new check → GREEN → full gauntlet after the last edit → Paul commits →
committed-source gauntlet → builder handoff → verifier Round 2 (the last round under
the default cap).

### A8. Replay-runner setup and file-scope amendment

Approval recorded: 2026-10-04 UTC. Paul replied "yes" to the direct request,
"May I include the replay runner in approved tracked scope?" This approval
adds `tools/generation_binding_gauntlet.py` under
`BitCracker/btcrecover-master` to A7's permitted file scope. Its approved
working-file byte SHA-256 is:
`a98bc6a1e7553e67942653a621e29660a6c3bd28f6473d51e057b1570b4e88f0`.

The runner provides a persisted local entry point for existing checker tests,
both established Ruff rule sets, narrow helper coverage, isolated checker
mutations, both CUDA builds, the full suite, required native mutations and
standalone controls. It copies only allowlisted public/synthetic inputs,
records command exits and logs, and checks raw-byte source restoration.

Setup uses Python's standard library and the existing pytest, Ruff and
Coverage.py installations. No new dependency, installation, network access,
product behavior, acceptance criterion or weakened assertion is authorized.
Before any CUDA build or native replay, recovery-process and GPU safety
checks remain required. This is not permission to run personal recovery,
change a real checkpoint, replace an installed executable, merge a branch
or initialize a shared database.

Paul still owns staging and commits. Fresh committed-source validation and
blind verification must follow his commit; this setup approval does not
replace either gate. Earlier spec text remains historical and unchanged.
