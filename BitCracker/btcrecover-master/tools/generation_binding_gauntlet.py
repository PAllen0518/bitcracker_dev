"""Reproduce generation-binding checks using public, isolated fixtures only."""

import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
OLD_CHECKER = "344c40acb2ee5cc4416d98cbe1923d980df3fec3"
CHECKER = Path("tools/validate_save_format.py")
DRIVER_TEST = "tests/test_mutation_driver.py"
LINT_FILES = [
    "tests/test_cuda.py",
    "tests/test_cuda_cli.py",
    "tests/test_generation_pins.py",
    DRIVER_TEST,
    str(CHECKER),
    "tools/generation_binding_gauntlet.py",
]


def digest(path):
    """Return a file's byte SHA-256."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(output, name, command, cwd=ROOT, expected=0, environment=None):
    """Persist a bounded command and fail on an unexpected exit code."""
    process = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=2400,
    )
    log = process.stdout + process.stderr
    (output / f"{name}.log").write_text(log, encoding="utf-8")
    record = {
        "command": command,
        "cwd": str(cwd),
        "exit_code": process.returncode,
        "expected": expected,
    }
    (output / f"{name}.json").write_text(
        json.dumps(record, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"{name}: exit {process.returncode}", flush=True)
    print(log[-1800:], flush=True)
    if process.returncode != expected:
        raise RuntimeError(f"{name}: expected exit {expected}")
    return log


def copy_sources(destination, native=False):
    """Copy only allowlisted source and the public test wallet, never saves."""
    paths = [CHECKER, Path(DRIVER_TEST), Path("tests/conftest.py")]
    if native:
        paths += list(ROOT.glob("*.hpp")) + list(ROOT.glob("*.cuh"))
        paths += [
            Path("multibit_cuda_threads.cu"),
            Path("tools/build_cuda.py"),
            Path("tests/test_cuda.py"),
            Path("tests/test_cuda_cli.py"),
            Path("tests/cuda_host_contracts.hpp"),
            Path("tests/cuda_harness.cu"),
            Path("tests/test_generation_pins.py"),
            Path("tools/generation_binding_gauntlet.py"),
            Path("btcrecover/test/test-wallets/multibit-wallet.key"),
        ]
    manifest = {}
    for path in paths:
        relative = path.relative_to(ROOT) if path.is_absolute() else path
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
        manifest[relative.as_posix()] = digest(ROOT / relative)
        if digest(target) != manifest[relative.as_posix()]:
            raise RuntimeError(f"copy mismatch: {relative}")
    return manifest


def red(output):
    """Require all seven descendant regressions to fail on the old checker."""
    destination = output / "old-checker"
    copy_sources(destination)
    old = subprocess.run(
        [
            "git",
            "show",
            (
                f"{OLD_CHECKER}:BitCracker/btcrecover-master/"
                "tools/validate_save_format.py"
            ),
        ],
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout
    (destination / CHECKER).write_bytes(old)
    # The old module has no new control constants. Omit that unrelated test
    # section in this scratch copy only, leaving all topology assertions intact.
    test_path = destination / DRIVER_TEST
    test_path.write_text(
        test_path.read_text(encoding="utf-8").split(
            "def test_hidden_error_fault_keeps_the_visible_failure():"
        )[0],
        encoding="utf-8",
    )
    log = run(
        output,
        "red-old-checker",
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            DRIVER_TEST,
            "-q",
            "-k",
            "hidden_elements_are_a_runner_error",
        ],
        cwd=destination,
        expected=1,
    )
    if "7 failed" not in log or "DID NOT RAISE" not in log:
        raise RuntimeError("RED did not observe all seven assertion failures")


def checker_mutations(output):
    """Falsify each new checker defense in a separate interpreter/copy."""
    edits = [
        ("allow_output_children", "elif len(element):", "elif False:"),
        ("allow_property_children", "or len(item)", "or False"),
        ("ignore_suite_output", 'if element.tag != "testcase":', "if False:"),
        (
            "accept_partial_controls",
            "len(results) == len(REPORT_CONTROLS) and all(",
            "bool(results) and any(",
        ),
    ]
    source = (ROOT / CHECKER).read_text(encoding="utf-8")
    for name, before, after in edits:
        if source.count(before) != 1:
            raise RuntimeError(f"{name}: ambiguous mutation")
        destination = output / name
        copy_sources(destination)
        mutated = source.replace(before, after, 1)
        (destination / CHECKER).write_text(mutated, encoding="utf-8")
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        log = run(
            output,
            name,
            [sys.executable, "-B", "-m", "pytest", DRIVER_TEST, "-q"],
            cwd=destination,
            expected=1,
            environment=environment,
        )
        if "FAILED tests/test_mutation_driver.py::" not in log:
            raise RuntimeError(f"{name}: no observed test assertion failure")


def coverage(output):
    """Gate executable lines in the two pure report-validation helpers."""
    environment = os.environ.copy()
    environment["COVERAGE_FILE"] = str(output / ".coverage")
    run(
        output,
        "checker-coverage-tests",
        [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--branch",
            "-m",
            "pytest",
            DRIVER_TEST,
            "-q",
        ],
        environment=environment,
    )
    report = output / "coverage.json"
    run(
        output,
        "coverage-json",
        [
            sys.executable,
            "-m",
            "coverage",
            "json",
            "-o",
            str(report),
        ],
        environment=environment,
    )
    data = json.loads(report.read_text(encoding="utf-8"))
    entry = next(
        value
        for name, value in data["files"].items()
        if name.replace("\\", "/").endswith(CHECKER.as_posix())
    )
    tree = ast.parse((ROOT / CHECKER).read_text(encoding="utf-8"))
    ranges = [
        (node.lineno, node.end_lineno)
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"check_report_leaf", "report_control_exit"}
    ]
    missed = [
        line
        for line in entry["missing_lines"]
        if any(start <= line <= end for start, end in ranges)
    ]
    covered = [
        line
        for line in entry["executed_lines"]
        if any(start <= line <= end for start, end in ranges)
    ]
    summary = {
        "covered_lines": covered,
        "missing_lines": missed,
        "scope": "check_report_leaf and report_control_exit only",
    }
    (output / "helper-coverage.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )
    if len(ranges) != 2 or not covered or missed:
        raise RuntimeError(f"report-helper coverage misses: {missed}")
    print(
        f"Helper executable lines: {len(covered)}/{len(covered)}", flush=True
    )


def checks(output):
    """Run checker tests, pinned local lint versions, coverage and mutants."""
    for name, command in (
        ("python-version", [sys.executable, "--version"]),
        ("pytest-version", [sys.executable, "-m", "pytest", "--version"]),
        ("ruff-version", [sys.executable, "-m", "ruff", "--version"]),
        (
            "checker-tests",
            [
                sys.executable,
                "-m",
                "pytest",
                DRIVER_TEST,
                "tests/test_generation_pins.py",
                "-q",
            ],
        ),
        (
            "lint",
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                *LINT_FILES,
                "--line-length",
                "100",
                "--extend-select",
                "RUF100,E402",
            ],
        ),
    ):
        run(output, name, command)
    python310 = Path(sys.executable).parents[1] / "Python310/python.exe"
    if not python310.is_file():
        raise RuntimeError("approved Python 3.10 interpreter unavailable")
    run(output, "python310-version", [str(python310), "--version"])
    run(output, "ruff310-version", [str(python310), "-m", "ruff", "--version"])
    run(
        output,
        "checker-tests310",
        [
            str(python310),
            "-m",
            "pytest",
            DRIVER_TEST,
            "tests/test_generation_pins.py",
            "-q",
        ],
    )
    run(
        output,
        "lint310",
        [
            str(python310),
            "-m",
            "ruff",
            "check",
            *LINT_FILES,
            "--line-length",
            "100",
            "--extend-select",
            "RUF100,E402",
        ],
    )
    coverage(output)
    checker_mutations(output)


def native(output):
    """Build both targets and run the full suite and isolated driver sweep."""
    manifest = copy_sources(output / "source-manifest-copy", native=True)
    for target in ("optimized_test", "optimized"):
        run(
            output,
            f"build-{target}",
            [
                sys.executable,
                "tools/build_cuda.py",
                target,
            ],
        )
    environment = os.environ.copy()
    environment["CUDA_TEST_EXE"] = str(ROOT / ".cuda-build/optimized_test.exe")
    environment["CUDA_APP_EXE"] = str(ROOT / ".cuda-build/optimized.exe")
    environment["BITCRACKER_AGENT_BACKUP_DIR"] = str(output / "backups")
    run(
        output,
        "full-suite",
        [
            sys.executable,
            "-m",
            "pytest",
            "tests",
            "-q",
            f"--basetemp={output / 'suite-temp'}",
            f"--junitxml={output / 'suite-junit.xml'}",
        ],
        environment=environment,
    )
    destination = ROOT / ".cuda-build" / f"gm{uuid.uuid4().hex[:4]}"
    copied = copy_sources(destination, native=True)
    if copied != manifest:
        raise RuntimeError("source changed before mutation copy")
    for name, flag, expected in (
        ("required-mutants", None, 0),
        ("negative-control", "--negative-control", 1),
        ("runner-control", "--runner-control", 2),
        ("report-control", "--report-control", 2),
    ):
        command = [sys.executable, str(CHECKER)]
        if flag:
            command.append(flag)
        run(output, name, command, cwd=destination, expected=expected)
        report_name = {
            "required-mutants": "mutation-report.json",
            "negative-control": "mutation-negative-control.json",
            "runner-control": "mutation-runner-control.json",
            "report-control": "mutation-report-control.json",
        }[name]
        report = json.loads(
            (
                destination
                / ".cuda-build"
                / "save-format-mutation"
                / report_name
            ).read_text(encoding="utf-8")
        )
        expected_count = (
            17
            if flag is None
            else (2 if expected == 2 and flag == "--report-control" else 1)
        )
        if report["total"] != expected_count:
            raise RuntimeError(f"{name}: missing control or mutant")
        expected_status, expected_test = {
            "required-mutants": ("killed", 1),
            "negative-control": ("survived", 0),
            "runner-control": ("runner_error", 4),
            "report-control": ("runner_error", 1),
        }[name]
        for item in report["results"]:
            if (
                item["status"] != expected_status
                or item.get("build_exit_code") != 0
                or item.get("test_exit_code") != expected_test
                or item.get("restored") is not True
            ):
                raise RuntimeError(f"{name}: unexpected result: {item}")
            if (
                flag == "--report-control"
                and item.get("clean_status") != "killed"
            ):
                raise RuntimeError("report control had no genuine clean kill")
    for name, sha in manifest.items():
        if digest(destination / name) != sha or digest(ROOT / name) != sha:
            raise RuntimeError(f"source restore mismatch: {name}")
    summary = {
        "source_sha256": manifest,
        "mutation_copy": str(destination),
        "all_source_bytes_restored": True,
    }
    (output / "restoration.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )


def main():
    """Create fresh artifacts and run the selected verification phase."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase", choices=("red", "checks", "all"), default="all"
    )
    args = parser.parse_args()
    output = (
        ROOT / ".cuda-build" / f"binding-{args.phase}-{uuid.uuid4().hex[:8]}"
    )
    output.mkdir(parents=True)
    print(f"Artifacts: {output}", flush=True)
    if args.phase == "red":
        red(output)
    else:
        checks(output)
        if args.phase == "all":
            native(output)


if __name__ == "__main__":
    main()
