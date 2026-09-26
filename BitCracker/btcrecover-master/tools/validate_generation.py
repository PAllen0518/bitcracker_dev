"""Reproduce generation tests, fault probes, mutations, and benchmarks."""

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import time

from benchmark_generation import SOURCES
from build_cuda import BUILD, CUDA, ROOT

PROBE = r'''#define main original_application_main
#include "multibit_cuda_threads.cu"
#undef main
int main() {
    try {
        std::vector<TokenLine> lines(8, {{"a", "b"}, true, false, 0});
        ProducerState state(2, false);
        state.lines = &lines;
        state.total_combos = 256;
        state.producers = 4;
        generate_parallel(state, 0);
        return 0;
    } catch (const std::bad_alloc& error) {
        fprintf(stderr, "allocation failure: %s\n", error.what());
        return 3;
    } catch (const std::exception& error) {
        fprintf(stderr, "%s\n", error.what());
        return 2;
    }
}
'''


class Checks:
    """Keep exact commands, source hashes and failing results on disk."""

    def __init__(self):
        """Initialize a report without overwriting earlier CUDA evidence."""
        BUILD.mkdir(exist_ok=True)
        files = SOURCES + [
            "tests/test_cuda.py", "tests/test_cuda_cli.py",
            "tests/cuda_host_contracts.hpp", "tests/cuda_harness.cu",
            "tools/benchmark_generation.py", "tools/validate_generation.py",
        ]
        self.report = {
            "sha256": {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest()
                       for f in files}, "checks": [],
        }
        self.path = BUILD / "generation-validation.json"
        self.save()

    def save(self):
        """Persist results after each check, including failures."""
        self.path.write_text(
            json.dumps(self.report, indent=2), encoding="utf-8"
        )

    def run(self, name, command, expected=0, timeout=180, env=None,
            timeout_expected=False):
        """Fail closed on unexpected exits and unanticipated timeouts."""
        start = time.perf_counter()
        command = list(map(str, command))
        print(f"Running {name}", flush=True)
        row = {"name": name, "command": command}
        try:
            result = subprocess.run(
                command, cwd=ROOT, env=env, capture_output=True, text=True,
                check=False, timeout=timeout,
            )
            row.update(exit_code=result.returncode,
                       output=result.stdout + result.stderr)
        except subprocess.TimeoutExpired as error:
            row.update(timed_out=True, error=str(error))
        except OSError as error:
            row.update(error=str(error))
        row["seconds"] = time.perf_counter() - start
        self.report["checks"].append(row)
        self.save()
        if timeout_expected:
            assert row.get("timed_out"), row
        else:
            assert row.get("exit_code") == expected, row
        return row.get("output", "")

    def build(self, target, suffix, source=None):
        """Build an isolated executable and reject compiler warnings."""
        command = [sys.executable, "tools/build_cuda.py", target,
                   "--suffix", suffix]
        if source:
            command += ["--source", source]
        output = self.run(f"build_{target}{suffix}", command)
        assert "warning" not in output.lower(), output
        return BUILD / f"{target}{suffix}.exe"

    def altered_source(self, name, before, after):
        """Change exactly one site in a disposable copy of all headers."""
        directory = BUILD / "generation_probes" / name
        directory.mkdir(parents=True, exist_ok=True)
        for filename in SOURCES:
            content = (ROOT / filename).read_text(encoding="utf-8")
            if filename == "cuda_generation.hpp":
                assert content.count(before) == 1, (name, before)
                content = content.replace(before, after)
            (directory / filename).write_text(content, encoding="utf-8")
        return directory

    def faults(self):
        """Prove allocation, coordinator and partial-start errors propagate."""
        faults = [
            ("allocation", ("        auto chunk = std::make_unique<GenChunk>"
                            "(owner, state_);"),
             ("        throw std::bad_alloc();\n"
              "        auto chunk = std::make_unique<GenChunk>"
              "(owner, state_);"),
             3),
            ("coordinator", "        merge.run();",
             '        throw std::runtime_error("injected coordinator");', 2),
            ("startup", ("            workers.emplace_back(worker, "
                         "static_cast<size_t>(i));"),
             ('            if (i == 1) throw std::runtime_error('
              '"injected startup");\n'
              '            workers.emplace_back(worker, '
              'static_cast<size_t>(i));'),
             2),
        ]
        for name, before, after, expected in faults:
            directory = self.altered_source(name, before, after)
            probe = directory / "fault_probe.cu"
            probe.write_text(PROBE, encoding="utf-8")
            executable = self.build("optimized", f"_fault_{name}", probe)
            output = self.run(f"fault_{name}", [executable], expected,
                              timeout=18)
            assert ("allocation failure" if expected == 3
                    else f"injected {name}") in output, output

    def mutations(self):
        """Execute three plausible defects and require their exact failures."""
        mutants = [
            ("checkpoint", "            set_next(next[offset - 1]);",
             "            set_next(next[0]);", "block_boundaries",
             "checkpoint differs", False),
            ("ownership", "allocated_[owner] < CHUNKS_PER_WORKER", "true",
             "pool_ownership", "pool exceeded", False),
            ("wakeups", ("        produce_.notify_all();\n"
                         "        consume_.notify_all();"),
             "        consume_.notify_all();", "pool_cancel", "", True),
        ]
        for name, before, after, contract, message, timed_out in mutants:
            directory = self.altered_source(name, before, after)
            executable = self.build("optimized_test", f"_mutant_{name}",
                                    directory / SOURCES[0])
            output = self.run(f"mutant_{name}",
                              [executable, "contract", contract],
                              expected=2, timeout=5,
                              timeout_expected=timed_out)
            if not timed_out:
                assert message in output, output

    def tests(self):
        """Run the complete suite, randomized host stress and GPU memcheck."""
        harness = self.build("optimized_test", "_throughput")
        app = self.build("optimized", "_throughput")
        environment = os.environ.copy()
        environment.update(CUDA_TEST_EXE=str(harness), CUDA_APP_EXE=str(app))
        self.run("full_suite", [
            sys.executable, "-m", "pytest", "tests", "-q", "-p",
            "no:cacheprovider", "--basetemp", BUILD / "generation-pytest",
            "--junitxml", BUILD / "generation-tests.xml",
        ], env=environment)
        self.run("lint", [sys.executable, "-m", "ruff", "check",
                          "tools/benchmark_generation.py",
                          "tools/validate_generation.py",
                          "tests/test_cuda.py"])
        names = ["parallel", "pool_progress", "pool_ownership", "pool_cancel",
                 "raw_blocks", "seeded_blocks", "block_boundaries"] * 3
        random.Random(20260923).shuffle(names)
        for index, name in enumerate(names):
            self.run(f"stress_{index}_{name}", [harness, "contract", name],
                     timeout=30)
        environment["NV_COMPUTE_SANITIZER_LOCAL_CONNECTION_OVERRIDE"] = (
            "named-pipes"
        )
        output = self.run("pipeline_memcheck", [
            CUDA / "compute-sanitizer/compute-sanitizer.exe", "--tool",
            "memcheck", "--target-processes", "application-only",
            "--error-exitcode", "99", "--kernel-name",
            "kne=_Z22optimized_check_kernelILb1ELb1ELb0EEvPKhPKjijPi",
            harness, "contract", "pipeline",
        ], env=environment, timeout=45)
        assert "ERROR SUMMARY: 0 errors" in output and "PASS" in output


def main():
    """Run all checks, optionally deferring the longer benchmark matrix."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-benchmarks", action="store_true")
    args = parser.parse_args()
    checks = Checks()
    checks.tests()
    checks.faults()
    checks.mutations()
    if not args.skip_benchmarks:
        checks.run("benchmarks", [sys.executable,
                                  "tools/benchmark_generation.py"],
                   timeout=3600)
    checks.report["completed"] = True
    checks.save()


if __name__ == "__main__":
    main()
