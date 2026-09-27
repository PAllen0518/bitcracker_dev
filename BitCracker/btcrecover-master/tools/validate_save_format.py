"""Run fail-closed manual mutation tests for the v1 save format."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".cuda-build"
REPORT_DIR = BUILD / "save-format-mutation"


@dataclass(frozen=True)
class Mutant:
    """Describe one unique source mutation and its killing test."""

    name: str
    source: Path
    before: str
    after: str
    build_target: str
    test: str
    executable_variable: str


SAVE_FORMAT = ROOT / "save_format.hpp"
CUDA_SOURCE = ROOT / "multibit_cuda_threads.cu"


MUTANTS = [
    Mutant(
        name="drop_tokenlist_check",
        source=SAVE_FORMAT,
        before=(
            "    if (memcmp(r.tokenlist_hash, expected_tokenlist_hash, 32)"
            " != 0)\n"
            "        return SaveMismatch::Tokenlist;"
        ),
        after=(
            "    if (false && memcmp(r.tokenlist_hash, "
            "expected_tokenlist_hash, 32) != 0)\n"
            "        return SaveMismatch::Tokenlist;"
        ),
        build_target="optimized_test",
        test=(
            "tests/test_cuda.py::test_native_contract"
            "[save_reject_tokenlist]"
        ),
        executable_variable="CUDA_TEST_EXE",
    ),
    Mutant(
        name="drop_wallet_check",
        source=SAVE_FORMAT,
        before=(
            "    if (memcmp(r.wallet_id_hash, expected_wallet_hash, 32)"
            " != 0)\n"
            "        return SaveMismatch::Wallet;"
        ),
        after=(
            "    if (false && memcmp(r.wallet_id_hash, "
            "expected_wallet_hash, 32) != 0)\n"
            "        return SaveMismatch::Wallet;"
        ),
        build_target="optimized_test",
        test=(
            "tests/test_cuda.py::test_native_contract[save_reject_wallet]"
        ),
        executable_variable="CUDA_TEST_EXE",
    ),
    Mutant(
        name="drop_prev_fail_loud",
        source=SAVE_FORMAT,
        before=(
            "        if (prev_state == FileState::Error\n"
            "            || (prev_state == FileState::Present\n"
            "                && (prev_attributes & (FILE_ATTRIBUTE_DIRECTORY\n"
            "                                       "
            "| FILE_ATTRIBUTE_READONLY))"
            " != 0)) {"
        ),
        after="        if (false) {",
        build_target="optimized_test",
        test=(
            "tests/test_cuda.py::test_native_contract[save_prev_failure]"
        ),
        executable_variable="CUDA_TEST_EXE",
    ),
    Mutant(
        name="weaken_exact_size",
        source=SAVE_FORMAT,
        before=(
            "    if (size != static_cast<long>(sizeof(out))) "
            "{ fclose(f); return false; }"
        ),
        after=(
            "    if (size < static_cast<long>(sizeof(out))) "
            "{ fclose(f); return false; }"
        ),
        build_target="optimized_test",
        test=(
            "tests/test_cuda.py::test_native_contract[save_reject_oversized]"
        ),
        executable_variable="CUDA_TEST_EXE",
    ),
    Mutant(
        name="drop_migration_backup_abort",
        source=CUDA_SOURCE,
        before=(
            "        if (!copy_file_durable(restore_path,\n"
            "                               (std::string(restore_path)"
            " + \".legacy\").c_str())) {"
        ),
        after=(
            "        if (false && !copy_file_durable(restore_path,\n"
            "                               (std::string(restore_path)"
            " + \".legacy\").c_str())) {"
        ),
        build_target="optimized",
        test=(
            "tests/test_cuda_cli.py::"
            "test_cli_migration_aborts_when_backup_fails"
        ),
        executable_variable="CUDA_APP_EXE",
    ),
]


NEGATIVE_CONTROL = Mutant(
    name="survivor_negative_control",
    source=SAVE_FORMAT,
    before="// Atomic, durable writes with a retained previous generation.",
    after=(
        "// Atomic, durable writes with a retained previous generation."
        " MUTANT"
    ),
    build_target="optimized_test",
    test="tests/test_cuda.py::test_native_contract[save_roundtrip]",
    executable_variable="CUDA_TEST_EXE",
)


def sha256(data):
    """Return the SHA-256 hex digest for bytes."""
    return hashlib.sha256(data).hexdigest()


def run_command(command, environment, log_path):
    """Run one bounded command and persist combined output."""
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    log_path.write_text(
        result.stdout + result.stderr,
        encoding="utf-8",
    )
    return result


def apply_unique_mutation(mutant, original):
    """Apply exactly one expected replacement or fail closed."""
    text = original.decode("utf-8").replace("\r\n", "\n")
    count = text.count(mutant.before)
    if count != 1:
        raise RuntimeError(
            f"{mutant.name}: expected one source match, found {count}"
        )
    mutated = text.replace(mutant.before, mutant.after, 1).encode("utf-8")
    if sha256(mutated) == sha256(original):
        raise RuntimeError(f"{mutant.name}: mutation did not change source")
    return mutated


def run_mutant(mutant):
    """Build a mutant, require a test kill, and restore source bytes."""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    original = mutant.source.read_bytes()
    original_hash = sha256(original)
    suffix = f"_mut_{mutant.name}"
    executable = BUILD / f"{mutant.build_target}{suffix}.exe"
    result = {
        "name": mutant.name,
        "source": str(mutant.source.relative_to(ROOT)),
        "original_sha256": original_hash,
        "build_target": mutant.build_target,
        "test": mutant.test,
        "restored": False,
    }
    try:
        mutated = apply_unique_mutation(mutant, original)
        mutant.source.write_bytes(mutated)
        result["mutated_sha256"] = sha256(mutant.source.read_bytes())
        if result["mutated_sha256"] != sha256(mutated):
            raise RuntimeError(
                f"{mutant.name}: mutated source was not written"
            )

        environment = os.environ.copy()
        build_command = [
            sys.executable,
            "tools/build_cuda.py",
            mutant.build_target,
            "--suffix",
            suffix,
        ]
        build = run_command(
            build_command,
            environment,
            REPORT_DIR / f"{mutant.name}-build.txt",
        )
        result["build_exit_code"] = build.returncode
        if build.returncode != 0:
            result["status"] = "build_failed"
            return result

        environment[mutant.executable_variable] = str(executable)
        pytest_temp = REPORT_DIR / f"pytest-{mutant.name}"
        test_command = [
            sys.executable,
            "-m",
            "pytest",
            mutant.test,
            "-q",
            f"--basetemp={pytest_temp}",
        ]
        test = run_command(
            test_command,
            environment,
            REPORT_DIR / f"{mutant.name}-test.txt",
        )
        result["test_exit_code"] = test.returncode
        result["status"] = "killed" if test.returncode != 0 else "survived"
        return result
    finally:
        mutant.source.write_bytes(original)
        restored_hash = sha256(mutant.source.read_bytes())
        result["restored"] = restored_hash == original_hash
        result["restored_sha256"] = restored_hash
        if not result["restored"]:
            raise RuntimeError(f"{mutant.name}: exact source restore failed")


def main():
    """Run all required mutants or the known-survivor negative control."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--negative-control",
        action="store_true",
        help="prove the driver fails when a mutant survives",
    )
    arguments = parser.parse_args()
    selected = [NEGATIVE_CONTROL] if arguments.negative_control else MUTANTS
    results = [run_mutant(mutant) for mutant in selected]
    report_name = (
        "mutation-negative-control.json"
        if arguments.negative_control
        else "mutation-report.json"
    )
    report = {
        "negative_control": arguments.negative_control,
        "results": results,
        "killed": sum(item["status"] == "killed" for item in results),
        "total": len(results),
    }
    (REPORT_DIR / report_name).write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    for item in results:
        print(
            f"{item['name']}: {item['status']} "
            f"(build={item.get('build_exit_code')}, "
            f"test={item.get('test_exit_code')}, "
            f"restored={item['restored']})"
        )
    if any(item["status"] != "killed" for item in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
