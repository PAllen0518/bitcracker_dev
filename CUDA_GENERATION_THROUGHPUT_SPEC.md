# Candidate generation throughput spec

Status: approved and implemented.

Approval record: the user approved this spec with "Yes, the spec is approved".
The preceding status records the original draft state.

Measurement amendment: the user later explicitly authorized canceling the
active recovery search. The verified process was stopped before the final
benchmark matrix, after confirming its autosave had been updated.

Baseline: `3c1d78df57b53b082686eb52e7aa253ff4da3a52`, including the
parallel shutdown fix.

## Goal and scope

Increase completed passwords per second by reducing the CPU work required
to merge parallel candidate output. Implement and measure these two changes:

1. Merge candidate blocks into GPU batches instead of replaying a separate
   append operation for every candidate. Prepare a compatible block layout
   in producer workers where that reduces total packing and copying cost.
2. Reuse a bounded pool of generation chunks, including their byte and
   metadata storage, instead of allocating fresh vectors for every chunk.

Preserve the current command-line interface, default producer count, GPU
cryptography, accepted passwords, candidate order and duplicate multiplicity.
Preserve batch boundaries, candidate counts, checkpoint format and exact
resume positions for identical inputs and batch sizes. Keep the existing
single-producer path as a comparison and compatibility path.

Automatic tuning and GPU-side permutation generation are outside this change.

## Ownership and progress requirements

- A chunk has one owner at a time. Workers cannot reuse its storage while
  the merge thread still reads it.
- Bound total chunk storage by configuration and maximum supported candidate
  size, independently of the number of candidates generated. Document the
  actual bound, including worker-held and queued chunks.
- Pool exhaustion cannot prevent the earliest required work unit from making
  progress, even when later units fill the reorder queue.
- A full batch remains unpublished until its final next-candidate metadata
  is known. Metadata-only updates at chunk boundaries must not be lost.
- Cancellation and failures wake pool, merge and producer waiters, join
  started workers, and preserve the last fully checked checkpoint prefix.
- Retain adaptive 32/64/128-byte slots without truncation, including when
  one chunk or batch contains mixed lengths.

## Executable acceptance criteria

Add focused native contracts and Python runners for these behaviors:

| Contract | Required result |
| --- | --- |
| block_merge_matches_reference | Complete byte sequences and duplicate counts match the existing scalar generator for permutations, optional tokens, anchors and mixed typo settings. |
| block_boundaries_preserve_batches | Empty output, single candidates, partial chunks, multiple chunks and batch capacities smaller and larger than a chunk match reference batches and checkpoint metadata. |
| mixed_lengths_preserve_slots | Candidates around 31/32/33, 63/64/65 and 127/128 bytes retain their exact bytes and order across block copies and slot widening. |
| metadata_updates_preserve_resume | Resume at permutation, typo, chunk and batch boundaries yields the exact reference suffix, including metadata-only updates after a full batch. |
| chunks_are_reused | After warming a fixed-size workload, instrumented storage allocations do not grow with additional chunks; variable-length growth stays within the documented bound. |
| ownership_prevents_early_reuse | Delaying the merge reader prevents a worker from overwriting that chunk; exact output still matches. |
| pool_exhaustion_makes_progress | A delayed earliest unit and full later-unit queue complete within a bounded subprocess deadline without dropping candidates. |
| cancellation_wakes_all_waiters | Stop with pool/merge queues exhausted completes within a bounded deadline; existing parallel cancellation regression still passes. |
| errors_clean_up_workers | Injected allocation/startup/coordinator failures terminate cleanly with an error and do not advance saved progress past completed work. |

Compare producers 1, 2, 4 and 8 on small deterministic and seeded fixtures.
Use synthetic candidates and the repository's public test wallet only.

## Validation and measurement

1. Record baseline source and executable hashes. Build baseline source and
   headers from the stated commit into an isolated comparison directory.
2. Write the new behavioral tests first and record failing runs before fixing
   missing behavior. For already-correct invariants, demonstrate that the
   tests detect a deliberate defect in a disposable source copy.
3. Run the complete maintained suite on fresh application and native test
   builds. Check changed Python tooling with Ruff and compile C++ with the
   existing diagnostics. Preserve all existing shutdown tests.
4. Exercise seeded ordering/resume cases and bounded concurrency stress.
   Inject at least three relevant defects, covering checkpoint boundaries,
   buffer ownership/reuse and cancellation wakeups, and record detections.
5. Run the existing bounded CUDA pipeline memory check because host packing
   changes the buffers passed to CUDA. Report unsupported host race checking
   and C++ changed-line coverage explicitly, without claiming equivalents.
6. Measure CPU generation/merge time, chunk allocations, peak pool storage,
   transfer/kernel time and end-to-end completed passwords per second.

Benchmark the current baseline against each optimization separately and both
together. Use identical inputs, candidate counts, producer count and batch
size. Include the existing synthetic six-line, seven-token-per-line workload
(84,707,280 candidates), mixed/long passwords, and typo-heavy generation.

Warm up both executables, then collect five paired trials with alternating
run order. Repeat a fixed workload sufficiently to target at least three
seconds per measured trial, with a 60-second subprocess deadline. Record raw
trials, medians and background GPU activity. Do not stop other applications
or recovery searches to obtain cleaner numbers.

Claim an improvement only when the combined change increases median
throughput by at least 5% on the primary workload and wins at least four of
five paired trials. Check the other measured workloads for median regressions
greater than 5%. If results are noisy, inconclusive or slower, report that
instead of declaring success or silently changing these criteria. Keep the
existing implementation available until the measured replacement is justified.

## Files, permissions and delivery

Expected changes: `cuda_generation.hpp`, possibly `cuda_buffers.hpp`, native
contracts, Python test/benchmark helpers, and a separate throughput evidence
report. Keep any new benchmark helper small and reproducible.

Use the existing Python, MSVC and CUDA installations. No new dependencies or
system configuration changes. Put temporary probes, logs and comparison
executables under `.cuda-build`; use distinct executable names.

Do not alter personal wallets, token lists, recovery logs or checkpoints.
Do not replace the installed executable or interrupt a running search.
Do not stage, commit, amend, push, or add authorship/co-author credits.
Provide exact Git commands for the user when the changes are ready.

Approval of this spec authorizes the described implementation, isolated
builds, bounded tests, benchmarks and evidence report. It does not authorize
Git mutations, dependency installation or executable replacement.
