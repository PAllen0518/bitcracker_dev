# Parallel shutdown fix

## Approved scope

The review proposed waking blocked parallel producers before joining them,
covering exceptional cleanup, and adding a parallel cancellation regression.
The user approved that proposal with: "go ahead and implement that".

Acceptance criteria:

- A four-producer search with a stalled consumer stops and joins all workers
  within the regression test's 18-second process deadline, including its
  eight-second queue-fill period.
- Coordinator or worker-start failures stop generation, wake the merge queue,
  join all successfully started threads, and propagate the original error.
- Existing ordered generation, checkpoint, recovery, and CUDA tests still pass.

Use existing build/test dependencies and isolated `.cuda-build` executables.
Do not alter recovery data, install dependencies, commit, or replace the
installed recovery executable. Evidence below will distinguish executed checks
from unverified paths.

## Evidence

All commands below run from `BitCracker/btcrecover-master`.

### RED

Before changing `cuda_generation.hpp`, added the native `parallel_cancel`
contract and `test_parallel_cancellation_wakes_blocked_workers`.

```powershell
python tools/build_cuda.py optimized_test --suffix _shutdown
$env:CUDA_TEST_EXE=(Join-Path (Get-Location) '.cuda-build/optimized_test_shutdown.exe')
python -m pytest tests/test_cuda.py -q -k parallel_cancellation -p no:cacheprovider --basetemp .cuda-build/shutdown-red --junitxml .cuda-build/shutdown-red.xml
```

Observed: exit 1, one failed test, `subprocess.TimeoutExpired` after 18 seconds.
The test child is killed and reaped by Python's `subprocess.run` timeout.

### GREEN and regression checks

The coordinator now aborts/notifies the merge before joining workers. Workers
that observe cancellation also wake a coordinator waiting for output. Startup
and coordinator exceptions stop generation, abort the merge, join started
threads, and rethrow.

```powershell
python tools/build_cuda.py optimized_test --suffix _shutdown
python tools/build_cuda.py optimized --suffix _shutdown
$env:CUDA_TEST_EXE=(Join-Path (Get-Location) '.cuda-build/optimized_test_shutdown.exe')
$env:CUDA_APP_EXE=(Join-Path (Get-Location) '.cuda-build/optimized_shutdown.exe')
python -m pytest tests/test_cuda.py -q -k parallel_cancellation -p no:cacheprovider --basetemp .cuda-build/shutdown-green --junitxml .cuda-build/shutdown-green.xml
python -m pytest tests -q -p no:cacheprovider --basetemp .cuda-build/shutdown-full --junitxml .cuda-build/shutdown-full.xml
python -m ruff check tests/test_cuda.py
git diff --check
```

Observed: regression **1 passed in 8.22s**; complete suite **110 passed in
19.86s**, including real CUDA tests, CLI recovery, ordering and checkpoint
contracts. Both rebuilt targets produced no compiler warnings. Ruff passed;
diff checking passed (Git reports the existing LF/CRLF normalization notices).
The application used by CLI tests was the freshly rebuilt suffixed executable.

### Exceptional cleanup

```powershell
python .cuda-build/shutdown_fault_checks.py
```

This retained local probe builds disposable copies of the current source and
injects `std::runtime_error` immediately before `merge.run()`, and separately
before starting worker two. A direct caller catches the propagated exception.
Both completed within an 18-second deadline with exit 2 and the exact injected
error message. Results: `.cuda-build/shutdown-fault-results.json`.

The first version of this probe used the collection test helper. That helper
checks for producer failure before its guard joins, so it sometimes reported
an output mismatch instead of the injected error. The direct caller above
avoids that test-helper race and exercises the production cleanup directly.

### Mutation checks

```powershell
python .cuda-build/shutdown_mutation_checks.py
```

All three disposable mutants compiled successfully and were detected:

| Deliberate defect | Observed failure |
| --- | --- |
| Remove the normal-path `merge.abort()` | Cancellation timed out at 18s |
| Remove producer notification from `ParallelMerge::abort()` | Cancellation timed out at 18s |
| Remove exceptional cleanup before rethrow | Injected coordinator failure terminated with exit 77 instead of propagated exit 2 |

For the last probe only, a disposable `std::set_terminate` handler exits 77
instead of opening a platform crash dialog. It does not exist in production.
Results: `.cuda-build/shutdown-mutation-results.json`. The two cancellation
mutants run the same native contract as the maintained regression test.

### Source and binary identity

SHA-256 values are also saved in `.cuda-build/shutdown-sha256.json`.

| File | SHA-256 |
| --- | --- |
| cuda_generation.hpp | `9d007c0ddf08707f1835c71f3a554014138de21e40f6ed29262bf6ab837c1e90` |
| tests/cuda_host_contracts.hpp | `f4b440cbaf35dad8b7abef057536c9bc9629dae71b2a2b1d87ad787a897e3c49` |
| tests/test_cuda.py | `f83d0e752f57dabe7bfc02117bbfbbbb24da7390e521b17689ca295ee1860d21` |
| .cuda-build/optimized_shutdown.exe | `50d684f15dc92ad2ccb49a86e62f7104e1cbf2364fe01db85ed9f4e7fd886a30` |
| .cuda-build/optimized_test_shutdown.exe | `b7b956d0ddf329e70294b61cf484641e160bbbfc014bb4ab917ba0c34fb152ff` |

### Limits and artifact handling

- The regression deliberately stalls its consumer for eight seconds. Its RED
  run confirms the original deadlock on this machine; it does not instrument
  queue occupancy or prove scheduling behavior on every machine.
- No host ThreadSanitizer or enforced C++ changed-line coverage was run, these
  are unavailable in the existing Windows validation setup. Native execution
  and bounded fault injection do not prove freedom from every possible race.
- No new independent verifier, GPU sanitizer run, or performance benchmark was
  performed for this host-only fix. Existing seeded generation/crypto checks
  and public-wallet CLI tests ran as part of the complete suite.
- No dependencies, installed executable, recovery data, or Git history were
  changed. New executables and disposable probes remain under `.cuda-build`.
- The pre-existing Ruff finding in `tools/run_sanitizer.py:54` is outside this
  change; only changed Python code is claimed lint-clean here.
