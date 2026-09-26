# Generation throughput evidence

Status: complete. The correctness gauntlet and performance gate passed.

Contract: [approved throughput spec](CUDA_GENERATION_THROUGHPUT_SPEC.md).
Baseline: `3c1d78df57b53b082686eb52e7aa253ff4da3a52`.

## Implementation

- Producers write adaptive 32/64/128-byte candidate slots, lengths and
  per-candidate resume metadata into chunks. The merge copies compatible
  runs into batches in bulk, with a row-copy fallback when strides differ.
- A full chunk remains private until its final metadata update is known.
  Splitting a chunk across batches preserves the exact checkpoint after
  each completed batch, including typo boundaries and skipped candidates.
- Each worker has a reservation for four reusable chunks. Later work can
  exhaust its own reservation without taking storage from the worker that
  must supply the next ordered chunk. Ownership passes through unique
  pointers; recycling happens after the merge finishes reading.
- Pool, merge and producer waits honor cancellation. The prior shutdown
  cleanup and exception propagation remain in place.
- Typo-enabled work is scheduled one combination per ordered unit. This keeps
  the four-chunk worker pool bounded without forcing one producer to generate
  an oversized typo unit while the other producers wait.
- The serial producer path, CUDA cryptography and installed executable are
  unchanged. The optimization applies to parallel generation.

### Memory and measurement scope

At most `4 * producers` chunks exist in the default implementation. Each
chunk contains 8192 slots, each at most 128 bytes, plus a four-byte length
and 24-byte resume position per slot. Thus payload capacity is at most
`4 * producers * 8192 * 156` bytes on this toolchain, plus bounded chunk,
map and pool bookkeeping. This excludes the existing host/device batches.

For four producers, uniform short candidates use at most 7.5 MiB of chunk
payload; fully widened chunks use at most 19.5 MiB. Pool memory does not
grow with the number of work units. Allocation counters measure the three
payload-vector allocations per new chunk and subsequent capacity growth,
not all process allocations. Map nodes still allocate per queued chunk.

`generation_s` and `merge_s` are elapsed host-stage measurements, not OS
CPU utilization. Merge time includes waits for free output batches.
CUDA copy/kernel fields use device events; overlapping stage times must
not be added together. Benchmark `process_s` includes startup; reported
password rates use the application's search interval, `wall_s`.

## Reproduction

From `BitCracker/btcrecover-master`, run the complete entry point:

```powershell
python tools/validate_generation.py
```

The commands can also be run separately:

```powershell
python tools/validate_generation.py --skip-benchmarks
python tools/benchmark_generation.py
```

The build helper extracts all six baseline source/header files from the
recorded commit. Four isolated executables compare the baseline, bulk merge
without pooling, pooling with scalar merge, and both optimizations. The
individual controls use the new slot representation, so they isolate the
merge and pool switches within that representation.

Temporary source copies, compiler logs, XML results, binaries and JSON
measurements are retained under `.cuda-build`. No dependency installation,
Git mutation or installed-binary replacement is part of these commands. The
user separately authorized stopping the verified active recovery process
before the final measurements. Its latest autosave remained in place.

## RED and correctness results

Before implementing block merging or pooling, the added instrumentation and
tests produced **2 failures and 1 pass**:

- `block_merge`: "parallel merge did not copy candidate blocks".
- `chunk_reuse`: "chunk storage allocations grow with emitted chunks".
- `block_boundaries`: existing candidate/checkpoint behavior passed.

Recorded in `.cuda-build/throughput-red.xml` and reproducible with the
retained `.cuda-build/optimized_test_throughput_red.exe`.

The first complete performance matrix exposed a second RED result on the
typo-heavy workload: baseline was 38.13M passwords/s, while combined was
29.73M passwords/s with 0/5 paired wins. The fixed four-chunk pool serialized
generation behind 16-combination typo units. This report is retained as
`.cuda-build/generation-benchmarks-pool-stall-red.json`.

After scheduling one combination per unit when typos are enabled, a focused
GREEN probe measured 89.79M passwords/s combined versus 38.69M baseline. The
final five-trial matrix below then confirmed the result.

The fresh final validation was run after the last source edit:

- Full suite: **118 passed in 21.31 seconds**.
- Ruff: **all checks passed** for the two new tools and changed Python tests.
- Native and application CUDA builds: **exit 0 with no compiler warnings**.
- Randomized concurrency stress: **21/21 passed**, covering ordering, raw
  mixed-stride blocks, boundaries, pool ownership, progress and cancellation.
- Compute Sanitizer memcheck: **PASS, error summary: 0 errors**.
- Fault injection: allocation, coordinator and partial startup failures were
  all detected and terminated with the expected nonzero result.
- Manual mutation: **3/3 killed**, covering checkpoint metadata, ownership
  limits and producer wakeups.
- Checker controls: unexpected nonzero exit, unexpected timeout and missing
  expected timeout were all rejected.

The machine-readable reports are
`.cuda-build/generation-validation.json` and
`.cuda-build/generation-checker-controls.json`.

## Performance results

Five paired trials compare identical candidate counts at four producers
and batch size 1,048,576. Warmups determine a common repeat count targeting
at least three seconds of search time for the fastest variant. Trial order
alternates forward/reverse. Every individual process has a 60-second limit.
Each result checks the expected count and that no synthetic password was
reported as recovered.

Workloads include 84,707,280-candidate short, mixed-length and long searches,
plus an 8,562,960-candidate typo workload. The typo input fits the parser's
8192-byte line limit. Repetition preserves duplicate multiplicity.

Final medians and paired wins were:

| Workload | Baseline | Bulk only | Pool only | Combined | Ratio | Wins |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Primary | 40.32M/s | 128.00M/s | 46.08M/s | 168.25M/s | 4.17x | 5/5 |
| Mixed | 30.68M/s | 29.25M/s | 34.37M/s | 50.67M/s | 1.65x | 5/5 |
| Long | 29.03M/s | 28.28M/s | 31.29M/s | 49.38M/s | 1.70x | 5/5 |
| Typos | 39.53M/s | 71.23M/s | 47.66M/s | 89.37M/s | 2.26x | 5/5 |

The combined build exceeded the required 5% primary improvement and won all
five pairs on every workload. No measured variant regressed more than 5% in
the final matrix. Raw trials and metrics are in
`.cuda-build/generation-benchmarks.json`.

### Final source and binary identity

Validation recorded these SHA-256 values:

- `cuda_generation.hpp`:
  `9cc98b9c3800ee637c2576b16c2c88fe271f909fb72bdb1df5bf2cf738e05194`
- `cuda_buffers.hpp`:
  `5a2842e51f638ec57f26a7012abbfa738c4b08b559d06efe91128355969362f2`
- `multibit_cuda_threads.cu`:
  `7d94b19b0f4ff579eb5358a05766535ff05d502442b19d87d3610010e558b873`
- Final isolated executable, `.cuda-build/optimized_throughput.exe`:
  `71357f5d78dea1256655cd6e65021b8c26bfc315de2ee5e948d5e7040e595829`
- Final native test executable:
  `82e54e26585a3e38932ebfa455deb32305e7711e90cee4c8469ba89ce8f68390`
- Final benchmark combined executable:
  `df2e264b580d0e564d92a533900b00803333f9f8f7257820b734be348260d835`

The benchmark helper preserves source bytes, and all six benchmark source
hashes exactly match the validated files. Documentation was finalized after
those runs; no source or test file changed afterward.

## Limits

- Measurements use synthetic inputs and the public test wallet on a shared
  desktop GPU. They are not measurements of a private recovery search or a
  guarantee of the same gain for every token list.
- Host ThreadSanitizer and enforced C++ changed-line coverage remain
  unavailable in this Windows setup. Stress tests, fault injection and
  CUDA memcheck are not substitutes for a host race detector or coverage.
- CUDA cryptography was not changed. CPU ownership and ordering contracts
  are tested separately from the CUDA pipeline memory check.
- The final benchmark ran after the user-authorized recovery-process stop.
  Results on a busy shared GPU can be lower; the earlier affected report is
  retained as `.cuda-build/generation-benchmarks-with-search-running.json`.
- No independent verifier was used. No staging, commits, pushes, attribution
  additions or changes to personal recovery data were performed.

## Git commands for the user

From the repository root, after reviewing the finished changes:

```powershell
git add -- BitCracker/btcrecover-master/cuda_buffers.hpp BitCracker/btcrecover-master/cuda_generation.hpp BitCracker/btcrecover-master/multibit_cuda_threads.cu BitCracker/btcrecover-master/tests/cuda_host_contracts.hpp BitCracker/btcrecover-master/tests/test_cuda.py BitCracker/btcrecover-master/tools/benchmark_generation.py BitCracker/btcrecover-master/tools/validate_generation.py CUDA_GENERATION_THROUGHPUT_SPEC.md CUDA_GENERATION_THROUGHPUT_EVIDENCE.md
git diff --cached --stat
git commit -m "perf(cuda): batch parallel candidate merges and reuse chunks"
git push bitcracker_dev master
```

These commands intentionally omit unrelated `.gitignore` changes, instruction
files, recovery logs and checkpoint backups.
