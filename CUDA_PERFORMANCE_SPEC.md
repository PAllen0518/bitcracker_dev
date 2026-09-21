# CUDA performance implementation specification

Status: awaiting approval before implementation or test-code changes.

Request: implement all six improvements identified in the CUDA review.
Risk tier: Old Coder Tier 3, because cryptographic verification, concurrent
buffer ownership, and search checkpoints must remain correct.

Baseline source commit: `837f5651046c2207ec2773b8cd524bc9e3b79ec7`.
Primary source: `BitCracker/btcrecover-master/multibit_cuda_threads.cu`.

## Outcome

Implement and validate all six changes below. Deliver a separately named
optimized executable, reproducible correctness checks, and measured benchmark
results with their limitations. There is no promised percentage speedup.
An optimization that regresses a measured workload must be tuned or given a
documented fallback, rather than described as an improvement without evidence.

## Six implementation changes

1. **Shared AES tables.** Cooperatively load the AES lookup tables into shared
   memory once per block. Synchronize before any thread can return, including
   threads beyond the end of a partial batch. Keep uniform wallet data in
   constant memory. Compare against the existing constant-table kernel.
2. **Second-block rejection before IV derivation.** Derive the two MD5 digests
   needed for the AES key, decrypt ciphertext block two, and check its base58
   characters. Calculate the third MD5 digest, the IV, only for survivors.
   Then perform every existing first-block check and CPU hit verification.
3. **Reusable asynchronous pipeline.** Replace per-batch allocations with a
   bounded pool of pinned host buffers and reusable device buffers. Use CUDA
   streams and events to overlap transfers and checking. Process completed
   batches in generation order, with an explicit owner and completion state
   for every buffer. Stop the producer safely when a hit or error occurs.
   Include a synchronous fallback for allocation constraints and comparison.
4. **Short-password MD5 specialization.** Add a path for encoded password
   lengths from 1 through 31 bytes, where every derivation hash needs only
   one MD5 block. Construct message words directly and minimize intermediate
   buffers. Retain general hashing for longer passwords. Kernel selection
   must handle mixed lengths without losing any candidate.
5. **Compact transfers.** Use 32-, 64-, or 128-byte slots according to the
   longest candidate in each ordered batch. Transfer only populated slots
   and their lengths. Preserve the existing maximum supported length and
   candidate order. Evaluate a coalesced layout only if its total packing
   and transfer cost improves on compact slots; it is not a required default.
6. **Faster CPU generation.** Prepare anchor placement once per combination,
   write ordinary candidates directly into their batch slots, and stream
   typo variants through bounded scratch storage instead of materializing
   successive vectors. Preserve the existing CUDA variant order, including
   duplicates, typo-budget rules, and resume offsets. Deduplication and
   additional producer threads are outside this change.

## Compatibility and operating constraints

- Existing wallet, token-list, delimiter, typo, and restore arguments retain
  their supported behavior. This work does not expand the token-list grammar.
- Existing checkpoint layouts continue to load. Resume tests supply the same
  wallet, token list, delimiter, and typo options as the original search.
- Persist only the contiguous prefix of fully checked batches. Enqueued,
  copied, or generated candidates do not count as completed work.
- Compact slots must not truncate passwords or write a terminating zero
  beyond a full-width 32-, 64-, or 128-byte slot.
- Preserve the current treatment of empty candidates and the current
  generator's supported length semantics. Long raw-candidate tests exercise
  the verifier separately from the token generator.
- Keep CPU verification of GPU hits. Report CUDA failures as errors, never
  as successful searches that found nothing.
- Preserve the running recovery process, its executable, wallet, token lists,
  and checkpoint files. Do not suspend, terminate, or restart that process.
- Compile to `multibit_cuda_threads_optimized.exe` while the current executable
  is in use. Test and benchmark outputs use an isolated build directory.
- Run bounded GPU validation after approval. These workloads share the RTX
  2060 with the active recovery run and may temporarily reduce its throughput.
- Never use personal wallet data or search candidates in test logs or reports.
  Use the public test wallet and deterministic synthetic fixtures.

## Acceptance tests

The test names below define the contract. Tests must execute the real native
code or CUDA kernel for the behavior they claim to verify. Python results
alone do not establish correctness of the CUDA implementation.

| Test | Input and required result |
| --- | --- |
| `known_wallet_password` | The public MultiBit fixture accepts `btcr-test-password` and rejects the existing known wrong passwords. |
| `gpu_matches_independent_crypto` | Seeded synthetic wallets and candidate batches agree with a Python `hashlib` plus PyCryptodome oracle for every candidate, including positive hits. |
| `second_block_defers_iv` | A fixture whose second plaintext block is not base58 is rejected before IV derivation; a survivor still requires the first byte and all remaining first-block checks. Test instrumentation distinguishes these paths. |
| `short_and_general_md5_agree` | At lengths 1, 15, 16, 30, and 31, specialized and general hashing match independent MD5 results. |
| `length_boundaries_are_correct` | Lengths 0, 31, 32, 47, 48, 55, 56, 63, 64, 95, 96, 111, 112, 127, and 128 produce the expected verification result. A 129-byte candidate is rejected at the bounded test interface. |
| `shared_tables_handle_partial_blocks` | Batches with counts 1, 31, 32, 33, 255, 256, and 257 complete without a barrier error or out-of-bounds access and match the reference results. |
| `compact_stride_preserves_bytes` | A batch whose maximum length is 32, 33, 64, 65, or 128 selects stride 32, 64, 64, 128, or 128 respectively, and transfers the exact candidate bytes. |
| `mixed_lengths_preserve_order` | Alternating lengths around the 31/32, 32/33, and 64/65 boundaries return the same ordered candidates and verification results as the reference path. |
| `buffers_are_reused_after_completion` | Repeated small batches reuse a fixed number of allocations; a delayed copy or kernel prevents its buffer from returning to the free pool prematurely. |
| `pipeline_matches_synchronous` | Empty input, one candidate, partial final batches, and many full batches produce identical results and completed counts in both modes. |
| `resume_preserves_exact_suffix` | Checkpoints within a permutation, within a typo expansion, and at a batch boundary resume the exact reference suffix, with no newly introduced missing or repeated candidates. |
| `checkpoint_excludes_inflight_batches` | With later work queued but incomplete, the saved offsets identify only the fully completed prefix. |
| `legacy_checkpoint_loads` | Both existing save-file sizes load with their existing defaults and documented precision. |
| `assembly_preserves_reference_order` | Small required, optional, first-position, positional, and last-position anchor fixtures match the existing CUDA generator byte for byte and in sequence. |
| `streamed_typos_preserve_reference_order` | Capslock, swap, repeat, delete, closecase, and insertion, individually and in mixed budgets 0, 1, and 2, match the existing staged CUDA generator in sequence and multiplicity. |
| `streamed_typos_are_bounded` | Generating many variants does not retain all prior variants; maximum live scratch storage depends on password length, typo budget, and the fixed batch pool, not total emitted variants. |
| `stop_and_failure_are_safe` | A hit or simulated pipeline error while queues are occupied causes an orderly join, no buffer overrun, no deadlock, and no checkpoint advancement past completed work. |
| `cli_recovers_public_fixture` | A separately built executable recovers the public fixture password from a small synthetic token list and writes it only inside its test directory. |

## Failure model and checks

| Failure | Required check |
| --- | --- |
| Wrong crypto, missing valid password, or extra accepted password | Independent crypto oracle, positive and negative fixtures, seeded differential cases, and mutation tests. |
| MD5 padding or slot-boundary error | Boundary-length fixtures, guard regions, and CUDA memory checking. |
| Divergent shared-memory barrier | Partial-block fixtures and CUDA synchronization checking. |
| Host buffer reused during a copy, device buffer reused during a kernel | Ownership assertions, delayed-completion tests, and repeated queue stress. |
| Save point moves past unchecked candidates | Completed-prefix assertions and interrupted/resumed sequence comparisons. |
| Streamed typo generator changes order or exhausts memory | Reference sequence comparison, bounded-storage checks, and large-output tests. |
| Hit or failure leaves producer running | Queue-full cancellation and join-timeout tests. |
| Faster reported rate comes from skipped work or historical counts | Independent candidate counts and separate generation, transfer, kernel, and wall-clock timing. |
| Benchmark reflects unrelated GPU load | Record concurrent workloads, alternate baseline and optimized runs, and label shared-GPU results as contended. |

## Setup and authorized implementation scope

- Change the primary CUDA source, its build script, related documentation,
  and focused test and validation tooling. Split implementation into small
  local headers or source files if needed for testability.
- Use C++17, supported by the installed CUDA 13.0 and MSVC 14.44 toolchain.
  Use RAII for CUDA resources and explicit synchronization for shared state.
- Reuse installed Python 3.14.6, pytest 8.4.1, PyCryptodome 3.23.0,
  Ruff 0.16.0, and pytest-cov 7.1.0. Record the actual compiler and driver
  versions in the evidence report.
- Use the installed CUDA Compute Sanitizer and `cuobjdump`. Property tests
  can use deterministic seeded generators without adding a dependency.
- No new third-party dependencies or system configuration changes are planned.
  Keep build intermediates and temporary fixtures under the workspace.
- Add a native test/benchmark harness and a persisted validation entry point,
  planned as `BitCracker/btcrecover-master/tools/validate_cuda.py`.
- Add test-only comparison paths or instrumentation where necessary, without
  exposing candidate material in normal progress output.
- Allow an optional bounded batch-size argument for testing and tuning, with
  the current batch-size default preserved. Reject invalid sizes explicitly.
- Use Git diffs and recorded source hashes. Do not create commits, push,
  publish a release, or modify remote repository settings in this task.
- No independent agent verification is included; report it as not performed.

## Implementation and validation sequence

1. Record baseline tests, source identity, binary identity, and compiler
   resource usage. Build a baseline from the recorded source separately,
   because the existing executable predates the source file timestamp.
2. Write focused acceptance tests before each implementation increment and
   record actual failing runs. For behavior already present, use a temporary
   deliberate defect to demonstrate that the regression test detects it.
3. Implement the six changes incrementally and keep behavioral assertions
   fixed during refactoring.
4. Run the complete maintained Python suite and actual native/CUDA tests.
   Compile with diagnostics and run Ruff on added Python tooling.
5. Run CUDA memory, synchronization, and shared-memory race checking on
   bounded fixtures. Host concurrency requires separate stress tests; CUDA
   race checking is not evidence of host race freedom.
6. Run seeded property cases, an adversarial boundary pass, and 3 to 5
   plausible manual mutants in disposable source copies. Each mutant must
   compile successfully and fail a behavioral test. Rerun the relevant
   property tests alone before claiming those tests detect a mutant.
7. Measure short-password, mixed-length, long-password, permutation-heavy,
   and typo-heavy cases, using identical candidate counts and inputs.
   Warm up first, alternate repeated baseline/optimized trials, and report
   individual runs, medians, generation time, transfer time, kernel time,
   and end-to-end throughput. Use bounded workloads while recovery is active.
8. Rerun the final validation entry point after the final implementation
   change and produce `CUDA_PERFORMANCE_EVIDENCE.md` with actual commands,
   results, source hashes, test mapping, and all remaining limitations.

## Evidence limits

- CUDA changed-line coverage and host ThreadSanitizer support may not be
  available with this Windows toolchain. Record unsupported layers explicitly
  as unverified, and use test mapping and stress results without calling them
  equivalent to coverage or a race detector.
- GPU profiling counters may be restricted. Do not change system permissions
  to enable them. CUDA events and binary resource inspection remain usable.
- Shared-GPU benchmarks cannot establish an uncontended speedup. If the
  current recovery search is still running, deliver the reproducible clean
  benchmark procedure and label current timing results accordingly.
- Approval of this document authorizes the implementation and bounded tests
  described above. It does not authorize interruption of the active search
  or replacement of its running executable.

## Approval record

The user approved this specification with: "Spec approved".
The preceding specification is retained unchanged as the approved contract.
