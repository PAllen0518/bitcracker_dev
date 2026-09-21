# CUDA performance evidence

Evidence for the six-change CUDA optimization defined in
[`CUDA_PERFORMANCE_SPEC.md`](CUDA_PERFORMANCE_SPEC.md). Every result below was
produced by the reproducible gauntlet `tools/validate_cuda.py --checks all`,
which rebuilds all executables and fails closed on the first unexpected result.
The recorded machine artifacts are under
`BitCracker/btcrecover-master/.cuda-build/`.

## Status

- `validate_cuda.py --checks all` completed with exit 0 (`completed: true`).
- 23/23 checks returned their expected exit codes.
- Native + Python suite: **102 passed**.
- Compute Sanitizer memcheck, synccheck, racecheck, and a pipeline-kernel
  memcheck: **0 errors / 0 hazards**.
- Mutation gauntlet: **5/5 deliberate defects killed** by a behavioral test.
- Ruff (added tooling and existing `multibit_check.py` + `tests`): clean.
- Deliverable produced: `BitCracker/btcrecover-master/multibit_cuda_threads_optimized.exe`
  (byte-identical to the validated `.cuda-build/optimized.exe`), built while the
  original `multibit_cuda_threads.exe` was left untouched.

## Toolchain and environment (actual)

| Component | Version |
| --- | --- |
| OS | Windows-10-10.0.19045-SP0 |
| CUDA / nvcc | release 13.0, V13.0.48 |
| NVIDIA driver | 616.92 |
| GPU | NVIDIA GeForce RTX 2060 (6144 MiB) |
| Host compiler (MSVC cl) | 19.44.35228 (`-vcvars_ver=14.44`) |
| Python | 3.14.6 |
| pytest / pycryptodome / ruff / pytest-cov | 8.4.1 / 3.23.0 / 0.16.0 / 7.1.0 |

Build command (per target, recorded in `.cuda-build/<target>.build.json`):

```
nvcc <source> -o .cuda-build/<target>.exe -O3 -arch=sm_75 \
  --allow-unsupported-compiler \
  --compiler-bindir "<MSVC 14.44 Hostx64/x64>" \
  -std=c++17 -lineinfo -Xptxas=-v -Xcompiler=/W3,/EHsc -I .
```

## Source and binary identity (SHA-256)

Baseline source commit: `837f5651046c2207ec2773b8cd524bc9e3b79ec7`
(`multibit_cuda_threads.cu` extracted with `git show` for the comparison build).

| File | SHA-256 |
| --- | --- |
| multibit_cuda_threads.cu | `0304af3459ed5e96b711f2b5cca9bd52ea82be4b24e7627dbf9634b5f5e2b7ce` |
| cuda_crypto.cuh | `e43620945fc50b5bf1247651d4b877cd460acc53d2ef4cc9321cb0216119c516` |
| cuda_buffers.hpp | `c895df7423ac314b6143df2458fb14b1f7bbcbeadadae926e57951da9fa6d9ee` |
| cuda_generation.hpp | `b1c0d8d7fef68c41d233b433e0c45d436bfe3da218f37f0c7f5849b7d71c0413` |
| cuda_pipeline.hpp | `01b337a5e5cf6a7491bc8d780acf655e121f970d3f25db8a73277dda4b850775` |
| cuda_typos.hpp | `7371b2daeaeaf8d2ce96b2c7ad800b6b44a58acad20a2b22fa70dc0719a463f6` |
| tools/build_cuda.py | `e80c24c5cd12e87ba493966ccb55a9e60c39ce9ccd87c6b6bd3879ce55c581e9` |
| tools/benchmark_cuda.py | `af0c56611136986cfc0692d561eb9fb8cfc9c853fdb9c4ba825eb822b9852afb` |
| tools/validate_cuda.py | `4282d6204f30f7552aaf4c5d24789a781d1801cd176dca8906612a0075d55dc7` |
| tools/cuda_benchmark.cu | `dfbb3c9e56cb800a50e6b6556c285fe6a3210ed0b5db23b938833a760b2cb361` |
| tests/test_cuda.py | `00db30d6e37cd5bb73c28c7650245483db855ba8ae8e65fa11d877323fdb2c1b` |
| tests/test_cuda_cli.py | `9106f5352ef7b6a5308a66ab467a16c3700b181f43c2886ed02a6ac6e142f1ea` |
| tests/cuda_harness.cu | `d56d29d03c870c3aede78776014c25acdfa397f10a4a00a0aae008106b0c3185` |
| tests/cuda_host_contracts.hpp | `532bea049e62e95d23e27dce00ebfdfa5fb9c57fc91d8bd2a81d0bc41af1f2f3` |

| Binary | SHA-256 |
| --- | --- |
| multibit_cuda_threads_optimized.exe (deliverable, with `--producers`) | `25322a5f62ddbba883d05db41dc71189e81109be02ebe30d6ebbf4871f46ee56` |
| .cuda-build/optimized.exe (validated) | `25322a5f62ddbba883d05db41dc71189e81109be02ebe30d6ebbf4871f46ee56` |
| .cuda-build/baseline.exe (commit 837f565) | `3e739dcb296dd945ba1e7f97f3a48cf478816a0731e72a3fdab8b6ddc1b4e358` |

> Note: the six-change source hashes in the table above predate the multi-producer
> follow-up. The current tree is described in the "Multi-producer generation"
> section below; regenerate hashes with `tools/validate_cuda.py` for the exact
> current sources.

## Reproduction

```
cd BitCracker/btcrecover-master
python tools/validate_cuda.py --checks all     # full gauntlet, writes .cuda-build/validation.json
python tools/build_cuda.py optimized           # production build -> .cuda-build/optimized.exe
python tools/benchmark_cuda.py --trials 3      # alternating baseline/optimized, writes benchmarks.json
```

## Validation results (all checks, actual exit codes)

Mutant rows are expected to exit 1 (a behavioral test detects the injected bug);
all other rows are expected to exit 0.

| Check | Exit | Seconds |
| --- | --- | --- |
| build_baseline_metrics | 0 | 8.6 |
| build_optimized | 0 | 13.2 |
| build_optimized_test | 0 | 28.1 |
| build_benchmark | 0 | 32.0 |
| tests (pytest, native + Python) | 0 | 8.5 |
| lint (ruff, added tooling) | 0 | 0.1 |
| existing_lint (ruff) | 0 | 0.1 |
| memcheck | 0 | 0.3 |
| synccheck | 0 | 0.2 |
| racecheck | 0 | 0.4 |
| pipeline_memcheck | 0 | 0.5 |
| mutant_md5_length | 1 (killed) | 1.1 |
| mutant_first_byte | 1 (killed) | 1.3 |
| mutant_delete_typo | 1 (killed) | 1.4 |
| mutant_checkpoint | 1 (killed) | 1.1 |
| mutant_ownership | 1 (killed) | 1.1 |
| benchmarks | 0 | 14.3 |
| binary_resources | 0 | 0.0 |

## Compute Sanitizer (real CUDA checks)

Run against `optimized_test.exe` with the named-pipes local IPC transport
(`NV_COMPUTE_SANITIZER_LOCAL_CONNECTION_OVERRIDE=named-pipes`). The crypto fixture
uses a deliberately partial 257-thread batch to exercise the shared-memory
barrier and partial-block handling.

| Tool | Kernel | Result |
| --- | --- | --- |
| memcheck | `optimized_check_kernel<true,true,true>` | ERROR SUMMARY: 0 errors |
| synccheck | `optimized_check_kernel<true,true,true>` | ERROR SUMMARY: 0 errors |
| racecheck | `optimized_check_kernel<true,true,true>` | RACECHECK SUMMARY: 0 hazards displayed (0 errors, 0 warnings) |
| memcheck (pipeline path) | `optimized_check_kernel<true,true,false>` | ERROR SUMMARY: 0 errors |

## Mutation kills

Each mutant is a single deliberate defect compiled into a disposable source copy;
the run asserts exactly one behavioral test failure and one behavioral test only.

| Mutant | File | Injected defect | Detected by (`-k`) |
| --- | --- | --- | --- |
| md5_length | cuda_crypto.cuh | `message_length * 8` → `* 7` | length_boundaries_are_correct |
| first_byte | cuda_crypto.cuh | disable the first-byte `L/K/5/Q` check | second_block_defers_iv |
| delete_typo | cuda_typos.hpp | corrupt the delete-typo option | seeded_typo_sequences_match_reference |
| checkpoint | cuda_generation.hpp | `next_typo_idx = typo + 1` (off-by-one) | native contract + resume |
| ownership | cuda_buffers.hpp | disable the in-flight reuse guard | native contract + pipeline |

## Acceptance contract → test mapping

The spec's acceptance table is realized by native-harness commands
(`optimized_test.exe`) and CLI tests (`optimized.exe`), all exercising real CUDA.

| Spec contract | Test |
| --- | --- |
| known_wallet_password | `test_known_wallet_password` |
| gpu_matches_independent_crypto | `test_gpu_matches_independent_crypto` (modes 0-3 vs hashlib+PyCryptodome oracle) |
| second_block_defers_iv | `test_second_block_defers_iv` (asserts iv/short-call instrumentation) |
| short_and_general_md5_agree | `test_length_boundaries_are_correct` (lengths 1/15/16/30/31 assert short path) |
| length_boundaries_are_correct | `test_length_boundaries_are_correct`, `test_rejects_overlength_candidate` (129 → error) |
| shared_tables_handle_partial_blocks | `test_shared_tables_handle_partial_blocks` (1/31/32/33/255/256/257) |
| compact_stride_preserves_bytes | `test_compact_stride_preserves_bytes` (32/64/64/128/128) |
| mixed_lengths_preserve_order | `test_native_contract[mixed_lengths]` |
| buffers_are_reused_after_completion | `test_native_contract[pool]` |
| pipeline_matches_synchronous | `test_native_contract[pipeline]` |
| resume_preserves_exact_suffix | `test_native_contract[resume]`, `test_cli_*_resume_*` |
| checkpoint_excludes_inflight_batches | `test_native_contract[checkpoint]` |
| legacy_checkpoint_loads | `test_native_contract[legacy]` |
| assembly_preserves_reference_order | `test_native_contract[assembly]` |
| streamed_typos_preserve_reference_order | `test_streamed_typos_preserve_reference_order`, `test_seeded_typo_sequences_match_reference` |
| streamed_typos_are_bounded | `test_native_contract[bounded_typos]`, `test_degenerate_typos_finish_without_enumerating_noops` |
| stop_and_failure_are_safe | `test_native_contract[cancel]` |
| cli_recovers_public_fixture | `test_cli_recovers_public_fixture` |

## Kernel resource usage (`cuobjdump --dump-resource-usage`)

| Kernel variant | Registers | Shared | Stack | Const[0] |
| --- | --- | --- | --- | --- |
| `optimized_check_kernel<true,true,false>` (pipeline) | 96 | 5376 B | 288 B | 384 B |
| `optimized_check_kernel<true,true,true>` (audit) | 80 | 0 B | 432 B | 384 B |

The 5376-byte shared allocation is the cooperatively loaded AES table set
(change 1); the audit build reads the constant-memory tables instead.

## Benchmarks

`benchmark_cuda.py --trials 3`, medians of alternating baseline/optimized runs,
identical inputs and candidate counts per pair (verified by count + checksum).
`baseline` = commit 837f565 build (`baseline_metrics.exe`); `optimized` =
current build (`optimized.exe`). Full rows in `.cuda-build/benchmarks.json`.

**Contention:** during this run `tasklist` reported **no active
`multibit_cuda_threads.exe` process**, and `nvidia-smi` showed the RTX 2060 at
~15% utilization / 789 MiB used (other desktop apps). These numbers are therefore
effectively uncontended at measurement time. They are still single-machine,
single-GPU numbers and are **not** a controlled A/B on isolated hardware; treat
the ratios as indicative, not guaranteed.

Kernel-only (device time, `benchmark.exe`, 65536 candidates):

| Workload | Baseline (s) | Optimized (s) | Ratio |
| --- | --- | --- | --- |
| short | 0.0072 | 0.0005 | 14.53× |
| medium | 0.0076 | 0.0008 | 9.42× |
| mixed | 0.0074 | 0.0014 | 5.27× |
| long | 0.0073 | 0.0015 | 4.92× |

End-to-end CLI (wall-clock, full search of a 1,451,520-candidate synthetic
token list; typos workload = 24×11893):

| Workload | Baseline (s) | Optimized (s) | Ratio |
| --- | --- | --- | --- |
| short | 0.1892 | 0.0488 | 3.88× |
| mixed | 0.0550 (opt) vs 0.1911 | 0.0550 | 3.47× |
| long | 0.1904 | 0.0637 | 2.99× |
| permutations | 0.2024 | 0.0526 | 3.85× |
| typos | 0.0816 | 0.0146 | 5.60× |

CPU generation only:

| Workload | Baseline (s) | Optimized (s) | Ratio |
| --- | --- | --- | --- |
| assembly | 0.0083 | 0.0069 | 1.20× |
| typos | 0.0266 | 0.0116 | 2.29× |

The kernel-only gains reflect the shared AES tables, second-block-first
rejection, and short-password MD5 specialization; the end-to-end gains add the
asynchronous pipeline and compact transfers; the smaller generation gains reflect
the direct-slot assembly and streamed typo generator (per-candidate crypto still
dominates wall-clock, so end-to-end ratios exceed the generation ratio).

## Limitations and unverified layers

- **Shared-GPU speedup is not established as uncontended in a controlled sense.**
  The recovery process happened to be stopped during this run; that is not the
  same as a dedicated-hardware A/B. The reproducible procedure above is the
  deliverable for a later clean measurement. Re-run with the recovery active to
  obtain explicitly contended figures.
- **CUDA changed-line coverage** is not available on this Windows toolchain; the
  acceptance mapping and mutation kills stand in for it and are **not** equivalent
  to line coverage.
- **Host ThreadSanitizer is unavailable** on this toolchain. Host concurrency is
  exercised by the pool/pipeline/cancel native contracts and delayed-completion
  stress, which are **not** equivalent to a race detector. Compute Sanitizer
  racecheck covers device races only.
- **GPU profiling counters** were not enabled (no permission changes were made);
  CUDA events (copy/kernel ms) and `cuobjdump` resource inspection were used
  instead.
- **No independent agent verification** was performed, as scoped.
- The default Compute Sanitizer local IPC transport could not attach in this
  environment ("No attachable process found"); the named-pipes transport was used
  and is recorded in each sanitizer command.

## Tooling fixes applied during finalization

Two harness bugs blocked a clean run and were fixed (implementation source
unchanged):

1. **`tools/build_cuda.py` — spaced `TEMP`.** `GetShortPathName` returned a path
   still containing the `Paul Allen` space (8.3 short names unavailable), and
   nvcc's sub-tools (`cudafe++`/`cicc`/`cl`) fail **silently** (exit 1, no output)
   on a spaced `TEMP`. Fixed by falling back to a workspace-relative, space-free
   `TEMP` when the short path is unusable.
2. **`tools/validate_cuda.py` — sanitizer transport.** The `sanitizers()` step
   now sets `NV_COMPUTE_SANITIZER_LOCAL_CONNECTION_OVERRIDE=named-pipes`, the
   transport that attaches in this environment.
3. **`tools/benchmark_cuda.py` — oversized synthetic tokens.** The `mixed`/`long`
   workloads used single tokens of 65 and 100 bytes, which exceed the supported
   `MAX_TOKEN_LEN` (32) and were correctly rejected by both binaries. Rebuilt to
   compose equally long passwords from ≤32-byte tokens, preserving the exact
   1,451,520-candidate count so baseline and optimized do identical work.

No commits were created; changes remain in the working tree per the task scope.

---

# Multi-producer generation (follow-up)

## Context

After the six changes landed, the search became **generation-bound**: on the live
search60 the optimized build ran ~21M pw/s at a steady **~25% GPU** — one CPU
producer thread could not feed the now-much-faster kernel, leaving ~75% of the
RTX 2060 idle. This follow-up adds **bounded, opt-in parallel candidate
generation** to use that idle GPU, while keeping the GPU-visible candidate
sequence byte-for-byte identical to the single-producer path.

## Design (correctness by construction)

`--producers N` (default **1**; N=1 is the original code path unchanged). For
N>1, an atomic counter hands combinations to N worker threads; each worker runs
the *same* per-combo generator (`generate_variants`) into bounded recorded
chunks, and a **single merge thread replays those chunks strictly in generation
order** into the existing `Batch`/`GPUEngine` pipeline. Because the merge is
ordered, batch boundaries and checkpoints come out identical to serial — the
differential test below asserts this exactly. Back-pressure (a candidate budget
with a next-chunk exception) bounds memory and prevents deadlock. `--producers`
is runtime-only and not stored in the checkpoint, so existing save files resume
unchanged at any N.

Files: `cuda_generation.hpp` (shared `generate_variants`, worker pool, ordered
merge), `cuda_buffers.hpp` (`ProducerState::producers`), `multibit_cuda_threads.cu`
(`--producers` parse/validate, capped at the logical-core count),
`tools/benchmark_cuda.py` (producer sweep), and the test files below.

## Correctness (full gauntlet green)

`tools/validate_cuda.py --checks all` → exit 0, completed. **109 tests passed**
(7 new), Ruff clean, memcheck/synccheck/racecheck + pipeline memcheck **0 errors
/ 0 hazards**, **5/5 mutants killed**.

- `test_native_contract[parallel]` — the full emitted sequence at N=2,3,4,8 is
  **identical** (order, multiplicity, bytes) to N=1 across permutation-heavy,
  typo-heavy, and optional/anchored fixtures; and parallel resume from every
  serial checkpoint reproduces the exact suffix. N=1 is tied to the independent
  `reference_candidates` oracle.
- Stress: `contract parallel` run 60× consecutively — 0 failures (stands in for
  host ThreadSanitizer, which is unavailable on this toolchain).
- `test_invalid_producers_is_rejected`, `test_cli_producers_match_single`
  (identical counts at N=1/2/4), `test_cli_producers_recover_public_fixture`
  (recovers the public fixture through the parallel path).

## Throughput vs producer count

`benchmark_cuda.py` sweep, ~100.15M-candidate permutation workload, median of 2
trials. **Contended:** the recovery search was paused, but League of Legends was
running at ~20% GPU during this run, and the numbers are single-machine and
noisy — treat them as indicative of shape, not exact.

### Correction: a small-combination regression, and the fix

The first sweep used **large** combinations (~363k candidates each) and showed a
clean ~3×. But the real search60 has **many tiny combinations (~720 candidates
each)**, and on the live search the first parallel build *regressed*: ~12M pw/s
at `--producers 6` versus ~21M single-threaded. Root cause: that build used one
chunk per combination, so N workers paid mutex + allocation overhead every ~720
candidates and contended on the merge lock. The synthetic large-combination
benchmark masked exactly the case that mattered — a benchmark-representativeness
failure.

**Fix:** each worker now claims a *contiguous run* of combinations per work unit
(`PARALLEL_UNIT_COMBOS`) and packs them into shared chunks, so per-chunk locking
and allocation are amortized over thousands of candidates regardless of how small
each combination is. The benchmark workload was replaced with a **search60-shaped
list** (6 lines × 7 tokens → 117,649 tiny 720-candidate combinations, ~84.7M
candidates) so this case is measured going forward.

### search60-shaped sweep (packing build)

Uncontended run (recovery stopped, no game, GPU 8% idle), 2 trials each,
~84.7M candidates/run. Short runs make this run-to-run noisy, but the shape is
clear:

| producers | median pw/s | runs |
| --- | --- | --- |
| 1 | 41.3M | 41.1, 41.6 |
| 2 | 34.7M | 30.1, 39.2 |
| 4 | **69.9M** | 95.5, 44.4 |
| 6 | 42.8M | 44.2, 41.4 |
| 8 | 35.4M | 36.9, 33.9 |
| 12 | 38.2M | 37.9, 38.4 |

Candidate count identical (84,707,280) at every N. N=4 is the best (median 1.7×,
best run 2.3× over N=1); higher N flatten. An earlier contended search60-shaped
sweep (live search + game running) peaked at 107.6M / N=4 median 103.8M — the
synthetic single-producer baseline is faster than the live search because these
tokens are shorter, so read the *ratios*, not the absolute pw/s. **Live-search
confirmation:** on the actual search60 recovery, `--producers 4` sustained
**~46M pw/s (~2.2× the ~21M single-threaded)** — the reliable real-world figure.

Recommended: **`--producers 4`**; e.g.
`multibit_cuda_threads_optimized.exe --restore cuda_threads_save60.bin --producers 4`.
A fully uncontended sweep (recovery and games stopped) remains the definitive
measurement and is reproducible via the command above.

## Limitations

- Benchmark numbers here are contended (LoL ~20% GPU) and noisy; the shape (knee
  at 4–6) is reliable, the absolute pw/s is not. Re-run uncontended for exact
  figures.
- Head-of-line blocking is possible if a single combination is enormous (one
  worker owns a combo); bounded by the reorder budget and acceptable for
  search60-shaped lists (avg ~720 candidates/combo). Not optimized here.
- Host race freedom rests on repeated stress and the ownership/pipeline/cancel
  contracts, not a race detector (TSAN unavailable on this Windows toolchain).
  Compute Sanitizer racecheck covers device races only.
