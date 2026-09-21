"""Bound a Compute Sanitizer run and clean up only its own process tree."""

import argparse
import json
import os
import subprocess
import time

from build_cuda import BUILD, CUDA, ROOT


def run_check(kind, connection="named-pipes", pipeline=False, timeout=60):
    """Run NVIDIA's checker using an explicitly documented IPC transport."""
    environment = os.environ.copy()
    environment["NV_COMPUTE_SANITIZER_LOCAL_CONNECTION_OVERRIDE"] = connection
    kernel = ("_Z22optimized_check_kernelILb1ELb1ELb0EEvPKhPKjijPi"
              if pipeline else
              "_Z22optimized_check_kernelILb1ELb1ELb1EEvPKhPKjijPi")
    command = [
        CUDA / "compute-sanitizer/compute-sanitizer.exe", "--tool", kind,
        "--target-processes", "application-only", "--error-exitcode", "99",
        "--kernel-name", f"kne={kernel}", BUILD / "optimized_test.exe",
    ]
    if pipeline:
        command.extend(["contract", "pipeline"])
    else:
        command.extend(["crypto", BUILD / "sanitizer-fixture.txt", "1"])
    command = list(map(str, command))
    started = time.perf_counter()
    process = subprocess.Popen(
        command, cwd=ROOT, env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
    )
    timed_out = False
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        timed_out = True
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True, check=True,
        )
        output, _ = process.communicate(timeout=10)
        output += f"\nBounded checker timeout: {error}\n"
    report = {"command": command, "connection": connection,
              "exit_code": process.returncode, "timeout": timed_out,
              "seconds": time.perf_counter() - started, "output": output}
    name = f"sanitizer-{kind}-{connection}" + ("-pipeline" if pipeline else "")
    (BUILD / f"{name}.json").write_text(json.dumps(report, indent=2),
                                       encoding="utf-8")
    print(output, flush=True)
    if timed_out or process.returncode != 0:
        return 2
    if "ERROR SUMMARY: 0 errors" not in output:
        if kind != "racecheck" or "0 hazards displayed" not in output:
            raise RuntimeError("Missing sanitizer success summary")
    if not pipeline:
        rows = [json.loads(line)["rows"] for line in output.splitlines()
                if line.startswith('{"shared"')]
        if len(rows) != 1 or [row[0] for row in rows[0]] != [0] * 256 + [1]:
            raise RuntimeError("Sanitized kernel did not verify the fixture")
    elif "PASS" not in output.splitlines():
        raise RuntimeError("Sanitized pipeline did not pass its contracts")
    return 0


def main():
    """Run one bounded sanitizer invocation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["memcheck", "racecheck", "synccheck"])
    parser.add_argument("--connection", choices=["named-pipes", "tcp"],
                        default="named-pipes")
    parser.add_argument("--pipeline", action="store_true")
    parser.add_argument("--timeout", type=int, default=60)
    arguments = parser.parse_args()
    raise SystemExit(run_check(arguments.kind, arguments.connection,
                               arguments.pipeline, arguments.timeout))


if __name__ == "__main__":
    main()
