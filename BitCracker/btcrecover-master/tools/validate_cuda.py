"""Rebuild and run the reproducible CUDA correctness and performance gauntlet."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

from build_cuda import BUILD, CUDA, ROOT

SOURCES = [
    "multibit_cuda_threads.cu",
    "cuda_crypto.cuh",
    "cuda_buffers.hpp",
    "cuda_generation.hpp",
    "cuda_pipeline.hpp",
    "cuda_typos.hpp",
]
PYTHON_FILES = [
    "tools/build_cuda.py",
    "tools/benchmark_cuda.py",
    "tools/validate_cuda.py",
    "tests/test_cuda.py",
    "tests/test_cuda_cli.py",
]


class Validation:
    """Persist actual commands and results, including failed checks."""

    def __init__(self):
        """Identify the precise source tree before running checks."""
        BUILD.mkdir(parents=True, exist_ok=True)
        tracked = (
            SOURCES
            + PYTHON_FILES
            + [
                "tests/cuda_harness.cu",
                "tests/cuda_host_contracts.hpp",
                "tools/cuda_benchmark.cu",
            ]
        )
        self.report = {
            "python": sys.version,
            "platform": platform.platform(),
            "packages": {
                name: importlib.metadata.version(name)
                for name in ["pytest", "pycryptodome", "ruff", "pytest-cov"]
            },
            "sha256": {
                name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                for name in tracked
            },
            "checks": [],
        }
        self.destination = BUILD / "validation.json"
        self.save()

    def save(self):
        """Write the accumulated results after every executed command."""
        self.destination.write_text(
            json.dumps(self.report, indent=2), encoding="utf-8"
        )

    def run(self, name, command, environment=None, expected=0, timeout=600):
        """Execute one check and fail closed on errors or unexpected exits."""
        command = list(map(str, command))
        start = time.perf_counter()
        print(f"Running {name}", flush=True)
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            self.report["checks"].append(
                {"name": name, "command": command, "error": str(error)}
            )
            self.save()
            raise
        row = {
            "name": name,
            "command": command,
            "seconds": time.perf_counter() - start,
            "exit_code": result.returncode,
            "output": result.stdout + result.stderr,
        }
        self.report["checks"].append(row)
        self.save()
        (BUILD / f"{name}.txt").write_text(row["output"], encoding="utf-8")
        if result.returncode != expected:
            print(row["output"])
            raise RuntimeError(
                f"{name}: exit {result.returncode}, expected {expected}"
            )
        print(f"{name}: exit {result.returncode}", flush=True)
        return result

    def builds(self):
        """Compile fresh comparison, production, and test executables."""
        for target in [
            "baseline_metrics",
            "optimized",
            "optimized_test",
            "benchmark",
        ]:
            self.run(
                f"build_{target}",
                [sys.executable, "tools/build_cuda.py", target],
            )
            details = json.loads((BUILD / f"{target}.build.json").read_text())
            if "warning" in details["output"].lower():
                raise RuntimeError(
                    f"{target}: compiler warnings require review"
                )

    def tests(self):
        """Run maintained tests, native acceptance tests, and Python lint."""
        self.run(
            "tests",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests",
                "-q",
                "-p",
                "no:cacheprovider",
                "--basetemp",
                BUILD / "pytest",
                "--junitxml",
                BUILD / "tests.xml",
            ],
        )
        self.run(
            "lint",
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--config",
                "line-length=79",
                *PYTHON_FILES,
            ],
        )
        self.run(
            "existing_lint",
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "multibit_check.py",
                "tests",
            ],
        )

    def sanitizers(self):
        """Check real CUDA memory access, synchronization, and shared races."""
        sanitizer = CUDA / "compute-sanitizer/compute-sanitizer.exe"
        executable = BUILD / "optimized_test.exe"
        # Compute Sanitizer's default local IPC transport cannot attach to the
        # target in this environment ("No attachable process found"); the
        # named-pipes transport is the documented, working alternative here.
        environment = os.environ.copy()
        environment["NV_COMPUTE_SANITIZER_LOCAL_CONNECTION_OVERRIDE"] = (
            "named-pipes"
        )
        # Public fixture bytes, with a deliberately partial 257-thread batch.
        import base64

        wallet = ROOT / "btcrecover/test/test-wallets/multibit-wallet.key"
        text = "".join(
            line
            for line in wallet.read_text().splitlines()
            if line and not line.startswith("#")
        )
        data = base64.b64decode(text)
        fixture = BUILD / "sanitizer-fixture.txt"
        fixture.write_text(
            f"{data[8:16].hex()}\n{data[16:48].hex()}\n257\n"
            + (b"wrong".hex() + "\n") * 256
            + b"btcr-test-password".hex()
            + "\n",
            encoding="ascii",
        )
        for kind in ["memcheck", "synccheck", "racecheck"]:
            self.run(
                kind,
                [
                    sanitizer,
                    "--tool",
                    kind,
                    "--error-exitcode",
                    "99",
                    "--target-processes",
                    "application-only",
                    "--kernel-name",
                    "kne=_Z22optimized_check_kernelILb1ELb1ELb1EEvPKhPKjijPi",
                    "--log-file",
                    BUILD / f"{kind}-native.log",
                    executable,
                    "crypto",
                    fixture,
                    "1",
                ],
                environment=environment,
            )
        self.run(
            "pipeline_memcheck",
            [
                sanitizer,
                "--tool",
                "memcheck",
                "--error-exitcode",
                "99",
                "--target-processes",
                "application-only",
                "--kernel-name",
                "kne=_Z22optimized_check_kernelILb1ELb1ELb0EEvPKhPKjijPi",
                "--log-file",
                BUILD / "pipeline-memcheck-native.log",
                executable,
                "contract",
                "pipeline",
            ],
            environment=environment,
        )

    def mutations(self):
        """Compile and execute five deliberate bugs in disposable copies."""
        mutations = [
            (
                "md5_length",
                "cuda_crypto.cuh",
                "message_length * 8",
                "message_length * 7",
                "length_boundaries_are_correct",
            ),
            (
                "first_byte",
                "cuda_crypto.cuh",
                "if (first_byte != 'L' && first_byte != 'K' && first_byte != '5'",
                "if (false && first_byte != 'L' && first_byte != 'K' && first_byte != '5'",
                "second_block_defers_iv",
            ),
            (
                "delete_typo",
                "cuda_typos.hpp",
                "else if (option == 2)",
                (
                    "else if (option == 1) changed.push_back(base[i]);\n"
                    "                    else if (option == 2)"
                ),
                "seeded_typo_sequences_match_reference",
            ),
            (
                "checkpoint",
                "cuda_generation.hpp",
                "current_->next_typo_idx = next.typo;",
                "current_->next_typo_idx = next.typo + 1;",
                "test_native_contract and resume",
            ),
            (
                "ownership",
                "cuda_buffers.hpp",
                "if (in_flight_) throw",
                "if (false && in_flight_) throw",
                "test_native_contract and pipeline",
            ),
        ]
        for name, filename, before, after, selector in mutations:
            directory = BUILD / "mutations" / name
            directory.mkdir(parents=True, exist_ok=True)
            for source in SOURCES:
                content = (ROOT / source).read_text(encoding="utf-8")
                if source == filename:
                    if content.count(before) != 1:
                        raise ValueError(f"Ambiguous mutation site: {name}")
                    content = content.replace(before, after)
                (directory / source).write_text(content, encoding="utf-8")
            self.run(
                f"build_mutant_{name}",
                [
                    sys.executable,
                    "tools/build_cuda.py",
                    "optimized_test",
                    "--source",
                    directory / "multibit_cuda_threads.cu",
                    "--suffix",
                    f"_mutant_{name}",
                ],
            )
            environment = os.environ.copy()
            environment["CUDA_TEST_EXE"] = str(
                BUILD / f"optimized_test_mutant_{name}.exe",
            )
            report = directory / "tests.xml"
            self.run(
                f"mutant_{name}",
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/test_cuda.py",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "-k",
                    selector,
                    "--maxfail=1",
                    "--basetemp",
                    directory / "pytest",
                    "--junitxml",
                    report,
                ],
                environment=environment,
                expected=1,
            )
            tree = ET.parse(report)
            failures = tree.findall(".//failure")
            errors = tree.findall(".//error")
            if len(failures) != 1 or errors:
                raise AssertionError(
                    f"{name}: expected one behavioral failure"
                )
        self.report["mutations_killed"] = len(mutations)
        self.save()


def main():
    """Run the selected validation layers and retain their actual outcomes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--checks",
        choices=[
            "all",
            "tests",
            "sanitizers",
            "mutations",
            "benchmarks",
        ],
        default="all",
    )
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    validation = Validation()
    if not args.skip_build:
        validation.builds()
    if args.checks in ["all", "tests"]:
        validation.tests()
    if args.checks in ["all", "sanitizers"]:
        validation.sanitizers()
    if args.checks in ["all", "mutations"]:
        validation.mutations()
    if args.checks in ["all", "benchmarks"]:
        validation.run(
            "benchmarks", [sys.executable, "tools/benchmark_cuda.py"]
        )
    validation.run(
        "binary_resources",
        [
            CUDA / "bin/cuobjdump.exe",
            "--dump-resource-usage",
            BUILD / "optimized.exe",
        ],
    )
    validation.report["completed"] = True
    validation.save()
    print(validation.destination)


if __name__ == "__main__":
    main()
