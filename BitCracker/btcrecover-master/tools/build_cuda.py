"""Build isolated CUDA executables using the installed Windows toolchain."""

import argparse
import ctypes
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
BUILD = ROOT / ".cuda-build"
BASE_COMMIT = "837f5651046c2207ec2773b8cd524bc9e3b79ec7"
CUDA = Path(
    os.environ.get(
        "CUDA_TOOLKIT_DIR",
        r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.0",
    )
)
VS = Path(
    os.environ.get(
        "VS_BUILD_TOOLS",
        r"C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools",
    )
)


def short_path(path):
    """Return a Windows short path for nvcc's temporary directory."""
    buffer = ctypes.create_unicode_buffer(32768)
    result = ctypes.windll.kernel32.GetShortPathNameW(
        str(path),
        buffer,
        len(buffer),
    )
    if not result or result >= len(buffer):
        raise OSError(f"Cannot resolve a short path for {path}")
    return buffer.value


def build_environment():
    """Load MSVC's environment without logging private environment values."""
    setup = VS / "Common7/Tools/VsDevCmd.bat"
    command = (
        f'call "{setup}" -no_logo -arch=x64 -host_arch=x64 '
        "-vcvars_ver=14.44 -winsdk=10.0.22621.0 >nul && set"
    )
    process = subprocess.run(
        f'cmd.exe /d /s /c "{command}"',
        capture_output=True,
        text=True,
        check=True,
    )
    environment = os.environ.copy()
    for line in process.stdout.splitlines():
        if "=" in line and not line.startswith("="):
            key, value = line.split("=", 1)
            environment[key] = value
    temporary = BUILD / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    try:
        short = short_path(temporary)
    except OSError:
        short = None
    # nvcc's sub-tools (cudafe++/cicc/cl) fail silently when TEMP contains a
    # space. GetShortPathName only strips spaces from components that have an
    # 8.3 short name, so on volumes where 8.3 generation is disabled it still
    # returns a spaced path (e.g. "...\Paul Allen\..."). A path relative to
    # ROOT (nvcc's working directory) is inherently space-free and stays inside
    # the workspace.
    if not short or " " in short:
        short = os.path.relpath(temporary, ROOT)
    environment["TEMP"] = short
    environment["TMP"] = short
    return environment


def baseline_source():
    """Extract the exact approved baseline from local Git history."""
    source = subprocess.run(
        [
            "git",
            "show",
            (
                f"{BASE_COMMIT}:BitCracker/"
                "btcrecover-master/multibit_cuda_threads.cu"
            ),
        ],
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout
    destination = BUILD / "baseline.cu"
    destination.write_bytes(source)
    return destination


def compile_target(target, source=None, suffix=""):
    """Compile a target and persist its exact command and diagnostics."""
    BUILD.mkdir(parents=True, exist_ok=True)
    reference = target.startswith("baseline")
    selected = (
        Path(source)
        if source
        else (
            baseline_source()
            if reference
            else ROOT / "multibit_cuda_threads.cu"
        )
    )
    if target == "benchmark":
        selected = ROOT / "tools/cuda_benchmark.cu"
    if target == "baseline_metrics":
        original = selected.read_text(encoding="utf-8")
        marker = "    producer.join();"
        if original.count(marker) != 1:
            raise ValueError("Baseline timing seam is ambiguous")
        addition = (
            '\n    printf("\\nTIMINGS {\\"count\\":%llu,'
            '\\"wall_s\\":%.9f}\\n", '
            "(unsigned long long)(pw_checked - pw_session_base), "
            "secs_since(start_time));\n"
        )
        selected = BUILD / "baseline_metrics.cu"
        selected.write_text(
            original.replace(marker, marker + addition), encoding="utf-8"
        )
    harness = target.endswith("test")
    if harness:
        wrapper = BUILD / f"{target}{suffix}.cu"
        # The baseline's fixed batch size is reduced only for the test
        # harness. The unmodified baseline executable is built separately.
        if reference:
            original = selected.read_text(encoding="utf-8")
            marker = "#define BATCH_SIZE     (1 << 20)"
            if original.count(marker) != 1:
                raise ValueError("Baseline batch-size seam is ambiguous")
            selected = BUILD / "baseline_test_source.cu"
            selected.write_text(
                original.replace(marker, "#define BATCH_SIZE     64"),
                encoding="utf-8",
            )
        wrapper.write_text(
            "#define MULTIBIT_CUDA_TESTING 1\n"
            f'#define CUDA_SOURCE "{selected.as_posix()}"\n'
            f'#include "{(ROOT / "tests/cuda_harness.cu").as_posix()}"\n',
            encoding="utf-8",
        )
        selected = wrapper
    output = BUILD / f"{target}{suffix}.exe"
    command = [
        str(CUDA / "bin/nvcc.exe"),
        os.path.relpath(selected, ROOT),
        "-o",
        os.path.relpath(output, ROOT),
        "-O3",
        "-arch=sm_75",
        "--allow-unsupported-compiler",
        "--compiler-bindir",
        str(VS / "VC/Tools/MSVC/14.44.35207/bin/Hostx64/x64"),
        "-std=c++17",
        "-lineinfo",
        "-Xptxas=-v",
        "-Xcompiler=/W3,/EHsc",
        "-I",
        ".",
    ]
    environment = build_environment()
    process = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    (BUILD / f"{target}{suffix}.build.json").write_text(
        json.dumps(
            {
                "command": command,
                "exit_code": process.returncode,
                "output": process.stdout + process.stderr,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(process.stdout + process.stderr, end="")
    process.check_returncode()
    return output


def main():
    """Build the requested isolated executable."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "target",
        choices=[
            "baseline",
            "baseline_test",
            "baseline_metrics",
            "optimized",
            "optimized_test",
            "benchmark",
        ],
    )
    parser.add_argument("--source", type=Path)
    parser.add_argument("--suffix", default="")
    parser.add_argument("--install", action="store_true")
    arguments = parser.parse_args()
    if arguments.install and arguments.target != "optimized":
        parser.error("--install applies only to the optimized executable")
    output = compile_target(
        arguments.target, arguments.source, arguments.suffix,
    )
    if arguments.install:
        destination = ROOT / "multibit_cuda_threads_optimized.exe"
        shutil.copy2(output, destination)
        print(destination)
    else:
        print(output)


if __name__ == "__main__":
    main()
