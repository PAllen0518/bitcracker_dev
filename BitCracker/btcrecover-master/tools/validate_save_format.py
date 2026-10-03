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
class Edit:
    """Describe one exact-once source replacement."""

    source: Path
    before: str
    after: str


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
    extra_edits: tuple = ()

    def edits(self):
        """Return every edit, the primary one first."""
        return (Edit(self.source, self.before, self.after),) + self.extra_edits


SAVE_FORMAT = ROOT / "save_format.hpp"
CUDA_SOURCE = ROOT / "multibit_cuda_threads.cu"
HOST_CONTRACTS = ROOT / "tests/cuda_host_contracts.hpp"
CANARY_TEST = (
    "tests/test_cuda.py::test_native_contract[save_generation_canary]"
)
GENERATION_GATE = (
    "    if (r.tool_version != SAVE_GENERATION_VERSION) "
    "return SaveMismatch::Generation;"
)
TOKENLIST_GATE = (
    "    if (memcmp(r.tokenlist_hash, expected_tokenlist_hash, 32) != 0)\n"
    "        return SaveMismatch::Tokenlist;"
)
GENERATION_MESSAGE = (
    '        case SaveMismatch::Generation: return "save was written by a '
    "build with a different candidate-generation order; resume it with the "
    'matching build or start a new search";'
)
GENERIC_MESSAGE = '        case SaveMismatch::Generation: return "save mismatch";'
# Reference typo stages hashed by the generation canary.
KEEP_ORIGINAL = "        out.push_back(c);\n"
CAPSLOCK_VARIANT = (
    "        if (cfg.capslock && c.typos_used < cfg.max_typos) {\n"
    "            std::string sw = swapcase_str(c.pw);\n"
    "            if (sw != c.pw) out.push_back({sw, c.typos_used + 1});\n"
    "        }\n"
)
REPEAT_OPTION = (
    "            if (cfg.repeat) opts.push_back(std::string(2, base[i]));\n"
)
DELETE_OPTION = "            if (cfg.del)    opts.push_back(std::string());\n"
CLOSECASE_OPTION = (
    "            if (cfg.closecase && is_case_transition(base, i)) {\n"
    "                char sc = swap_case_ch(base[i]);\n"
    "                if (sc != base[i]) opts.push_back(std::string(1, sc));\n"
    "            }\n"
)
FREE_GUARD = "        if (free_count == 0) continue;\n"
REFERENCE_PERMUTATION = (
    "        } while (std::next_permutation(permutation, "
    "permutation + free_count));"
)


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
        name="drop_generation_check",
        source=SAVE_FORMAT,
        before=(
            "    if (r.tool_version != SAVE_GENERATION_VERSION) "
            "return SaveMismatch::Generation;"
        ),
        after=(
            "    if (false && r.tool_version != SAVE_GENERATION_VERSION) "
            "return SaveMismatch::Generation;"
        ),
        build_target="optimized_test",
        test=(
            "tests/test_cuda.py::test_native_contract"
            "[save_reject_generation]"
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
    Mutant(
        name="accept_generation_mismatch",
        source=SAVE_FORMAT,
        before=GENERATION_GATE,
        after=GENERATION_GATE.replace("!=", "=="),
        build_target="optimized_test",
        test=(
            "tests/test_cuda.py::test_native_contract"
            "[save_generation_property]"
        ),
        executable_variable="CUDA_TEST_EXE",
    ),
    Mutant(
        name="generation_after_tokenlist",
        source=SAVE_FORMAT,
        before=GENERATION_GATE + "\n" + TOKENLIST_GATE,
        after=TOKENLIST_GATE + "\n" + GENERATION_GATE,
        build_target="optimized_test",
        test=(
            "tests/test_cuda.py::test_native_contract"
            "[save_generation_order]"
        ),
        executable_variable="CUDA_TEST_EXE",
    ),
    Mutant(
        name="generation_wrong_constant",
        source=SAVE_FORMAT,
        before=GENERATION_GATE,
        after=GENERATION_GATE.replace("SAVE_GENERATION_VERSION", "0u"),
        build_target="optimized",
        test=(
            "tests/test_cuda_cli.py::"
            "test_cli_restore_resumes_gen1_save_at_stored_position"
        ),
        executable_variable="CUDA_APP_EXE",
    ),
    Mutant(
        name="generic_generation_message",
        source=SAVE_FORMAT,
        before=GENERATION_MESSAGE,
        after=GENERIC_MESSAGE,
        build_target="optimized",
        test=(
            "tests/test_cuda_cli.py::"
            "test_cli_restore_refuses_generation_mismatch"
        ),
        executable_variable="CUDA_APP_EXE",
    ),
    Mutant(
        name="generic_generation_message_host",
        source=SAVE_FORMAT,
        before=GENERATION_MESSAGE,
        after=GENERIC_MESSAGE,
        build_target="optimized_test",
        test=(
            "tests/test_cuda.py::test_native_contract"
            "[save_generation_message]"
        ),
        executable_variable="CUDA_TEST_EXE",
    ),
    Mutant(
        name="canary_capslock_reorder",
        source=CUDA_SOURCE,
        before=KEEP_ORIGINAL + CAPSLOCK_VARIANT,
        after=CAPSLOCK_VARIANT + KEEP_ORIGINAL,
        build_target="optimized_test",
        test=CANARY_TEST,
        executable_variable="CUDA_TEST_EXE",
    ),
    Mutant(
        name="canary_delete_reorder",
        source=CUDA_SOURCE,
        before=REPEAT_OPTION + DELETE_OPTION,
        after=DELETE_OPTION + REPEAT_OPTION,
        build_target="optimized_test",
        test=CANARY_TEST,
        executable_variable="CUDA_TEST_EXE",
    ),
    Mutant(
        name="canary_closecase_reorder",
        source=CUDA_SOURCE,
        before=REPEAT_OPTION + DELETE_OPTION + CLOSECASE_OPTION,
        after=CLOSECASE_OPTION + REPEAT_OPTION + DELETE_OPTION,
        build_target="optimized_test",
        test=CANARY_TEST,
        executable_variable="CUDA_TEST_EXE",
    ),
    Mutant(
        name="canary_permutation_reverse",
        source=HOST_CONTRACTS,
        before=FREE_GUARD,
        after=FREE_GUARD
        + "        std::reverse(permutation, permutation + free_count);\n",
        extra_edits=(
            Edit(
                HOST_CONTRACTS,
                REFERENCE_PERMUTATION,
                REFERENCE_PERMUTATION.replace("next_", "prev_"),
            ),
        ),
        build_target="optimized_test",
        test=CANARY_TEST,
        executable_variable="CUDA_TEST_EXE",
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


def apply_unique_mutation(mutant, originals):
    """Apply every expected exact-once replacement or fail closed."""
    texts = {
        path: data.decode("utf-8").replace("\r\n", "\n")
        for path, data in originals.items()
    }
    for edit in mutant.edits():
        count = texts[edit.source].count(edit.before)
        if count != 1:
            raise RuntimeError(
                f"{mutant.name}: expected one source match, found {count}"
            )
        texts[edit.source] = texts[edit.source].replace(
            edit.before, edit.after, 1
        )
    mutated = {path: text.encode("utf-8") for path, text in texts.items()}
    for path, data in mutated.items():
        if sha256(data) == sha256(originals[path]):
            raise RuntimeError(
                f"{mutant.name}: mutation did not change {path.name}"
            )
    return mutated


def run_mutant(mutant):
    """Build a mutant, require a test kill, and restore source bytes."""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    sources = list(dict.fromkeys(edit.source for edit in mutant.edits()))
    originals = {path: path.read_bytes() for path in sources}
    original_hashes = {path: sha256(data) for path, data in originals.items()}
    suffix = f"_mut_{mutant.name}"
    executable = BUILD / f"{mutant.build_target}{suffix}.exe"
    result = {
        "name": mutant.name,
        "source": ", ".join(str(path.relative_to(ROOT)) for path in sources),
        "original_sha256": ", ".join(original_hashes.values()),
        "build_target": mutant.build_target,
        "test": mutant.test,
        "restored": False,
    }
    try:
        mutated = apply_unique_mutation(mutant, originals)
        for path, data in mutated.items():
            path.write_bytes(data)
            if sha256(path.read_bytes()) != sha256(data):
                raise RuntimeError(
                    f"{mutant.name}: mutated source was not written"
                )
        result["mutated_sha256"] = ", ".join(
            sha256(data) for data in mutated.values()
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
        for path, data in originals.items():
            path.write_bytes(data)
        restored = {path: sha256(path.read_bytes()) for path in sources}
        result["restored"] = restored == original_hashes
        result["restored_sha256"] = ", ".join(restored.values())
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
    parser.add_argument(
        "--only",
        action="append",
        metavar="NAME",
        help="run only the named mutant; repeat to select several",
    )
    arguments = parser.parse_args()
    selected = [NEGATIVE_CONTROL] if arguments.negative_control else MUTANTS
    if arguments.only:
        known = {mutant.name: mutant for mutant in MUTANTS}
        unknown = sorted(set(arguments.only) - set(known))
        if unknown:
            parser.error(f"unknown mutant: {', '.join(unknown)}")
        selected = [known[name] for name in arguments.only]
    results = [run_mutant(mutant) for mutant in selected]
    if arguments.negative_control:
        report_name = "mutation-negative-control.json"
    elif arguments.only:
        report_name = "mutation-selected.json"
    else:
        report_name = "mutation-report.json"
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
