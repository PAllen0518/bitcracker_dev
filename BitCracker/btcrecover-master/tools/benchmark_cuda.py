"""Run bounded, alternating comparisons without stopping another search."""

import argparse
import json
import statistics
import subprocess
import time

from build_cuda import BUILD, ROOT


def execute(command, cwd=ROOT):
    """Run a bounded benchmark and require a successful native exit."""
    result = subprocess.run(
        list(map(str, command)),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return result.stdout


def inputs():
    """Return synthetic token lists and exact expected candidate counts."""
    tail = "".join(f"+ {letter}2\n" for letter in "efghijkl")
    # A 13-char required tail keeps every token within MAX_TOKEN_LEN (32) while
    # composing passwords long enough to exercise the 128-byte compact stride
    # and the general multi-block MD5 path. Single tokens wider than 32 bytes
    # are rejected by the parser, so length is varied through token count, not
    # by exceeding the supported token width. The line shapes are unchanged
    # (four first-line alternatives plus eight permuted tokens), so every
    # workload still enumerates 4 * 9! = 1,451,520 candidates.
    long_tail = "".join(f"+ {letter * 13}\n" for letter in "efghijkl")
    short = "+ a1 b1 c1 d1\n" + tail
    mixed = "+ a " + " ".join(["b" * 16, "c" * 17, "d" * 32]) + "\n" + tail
    long = "+ " + " ".join(letter * 13 for letter in "abcd") + "\n" + long_tail
    permutation = "+ ^HEAD\n" + short + "+ TAIL$\n"
    typo = (
        "+ "
        + " ".join(
            letter + "BcDeFgHiJ" for letter in "abcdefghijklmnopqrstuvwx"
        )
        + "\n"
    )
    flags = [
        "--typos",
        "2",
        "--typos-capslock",
        "--typos-swap",
        "--typos-repeat",
        "--typos-delete",
        "--typos-closecase",
        "--typos-insert",
        "0123456789",
    ]
    return {
        "short": (short, [], 1451520),
        "mixed": (mixed, [], 1451520),
        "long": (long, [], 1451520),
        "permutations": (permutation, [], 1451520),
        "typos": (typo, flags, 24 * 11893),
    }


def producer_sweep(report, trials=2):
    """Measure optimized throughput vs producer count to locate the GPU knee."""
    directory = BUILD / "benchmark_work" / "producers"
    directory.mkdir(parents=True, exist_ok=True)
    # search60-shaped: 6 lines x 7 tokens -> 7**6 = 117649 combos, each a small
    # 6! = 720-candidate permutation (~84.7M candidates total). This is the
    # many-tiny-combinations case the real search hits, where per-combination
    # merge overhead matters; a large-combination workload would mask it.
    tokens = "".join(
        "+ " + " ".join(f"{chr(ord('a') + line)}{k}" for k in range(7)) + "\n"
        for line in range(6)
    )
    token_file = directory / "synthetic.txt"
    token_file.write_text(tokens, encoding="utf-8")
    wallet = ROOT / "btcrecover/test/test-wallets/multibit-wallet.key"
    sweep = []
    for producers in [1, 2, 4, 6, 8, 12]:
        rates = []
        expected = None
        for _ in range(trials):
            command = [
                BUILD / "optimized.exe", "--wallet", wallet,
                "--tokenlist", token_file, "--timings",
                "--producers", str(producers),
            ]
            values = None
            for line in execute(command, directory).splitlines():
                if line.startswith("TIMINGS "):
                    values = json.loads(line[len("TIMINGS "):])
            if values is None or values["wall_s"] <= 0:
                raise AssertionError("producer sweep produced no timing")
            if expected is None:
                expected = values["count"]
            elif values["count"] != expected:
                raise AssertionError("producer sweep candidate count drifted")
            rates.append(values["count"] / values["wall_s"])
        sweep.append({
            "producers": producers,
            "count": expected,
            "rate_pw_s_median": statistics.median(rates),
            "rate_pw_s_runs": rates,
        })
        print(f"producers={producers}: "
              f"{statistics.median(rates)/1e6:.1f}M pw/s", flush=True)
    report["producer_sweep"] = sweep


def run_benchmarks(trials=3):
    """Compare kernels, generation, and end-to-end searches; save raw runs."""
    report = {
        "note": "GPU shared with any active search; timings are contended",
        "rows": [],
        "commands": [],
    }
    query = [
        "nvidia-smi",
        (
            "--query-gpu=name,driver_version,utilization.gpu,"
            "memory.used,memory.total"
        ),
        "--format=csv,noheader",
    ]
    report["gpu"] = execute(query).strip()
    try:
        report["active_search"] = execute(
            ["tasklist", "/fi", "IMAGENAME eq multibit_cuda_threads.exe",
             "/fo", "csv", "/nh"],
        ).strip()
    except subprocess.CalledProcessError as error:
        report["active_search"] = "unavailable, assume possible contention"
        report["process_query_error"] = error.stderr
    executable = BUILD / "benchmark.exe"
    for workload in ["short", "medium", "mixed", "long"]:
        for mode in [0, 1, 2, 3]:
            command = [executable, workload, mode, 65536]
            report["commands"].append(list(map(str, command)))
            report["rows"].extend(
                json.loads(line) for line in execute(command).splitlines()
            )
    for workload, count in [("assembly", 262144), ("typos", 20)]:
        for trial in range(trials + 1):
            pair = []
            for mode in [0, 1] if trial % 2 == 0 else [1, 0]:
                command = [executable, workload, mode, count]
                report["commands"].append(list(map(str, command)))
                row = json.loads(execute(command))
                row["trial"] = trial
                pair.append(row)
                if trial:
                    report["rows"].append(row)
            if len({(row["count"], row["checksum"]) for row in pair}) != 1:
                raise AssertionError(
                    "generation benchmark produced different work"
                )
    for workload, (tokens, flags, expected) in inputs().items():
        directory = BUILD / "benchmark_work" / workload
        directory.mkdir(parents=True, exist_ok=True)
        token_file = directory / "synthetic.txt"
        token_file.write_text(tokens, encoding="utf-8")
        for trial in range(trials + 1):
            for mode in [0, 1] if trial % 2 == 0 else [1, 0]:
                target = (
                    "baseline_metrics.exe" if mode == 0 else "optimized.exe"
                )
                command = [
                    BUILD / target,
                    "--wallet",
                    ROOT / "btcrecover/test/test-wallets/multibit-wallet.key",
                    "--tokenlist",
                    token_file,
                    "--timings",
                    *flags,
                ]
                report["commands"].append(list(map(str, command)))
                start = time.perf_counter()
                output = execute(command, directory)
                process_time = time.perf_counter() - start
                values = [
                    json.loads(line[8:])
                    for line in output.splitlines()
                    if line.startswith("TIMINGS ")
                ]
                if len(values) != 1 or values[0]["count"] != expected:
                    raise AssertionError(
                        f"{target}: incorrect {workload} count"
                    )
                if "Not found" not in output:
                    raise AssertionError(
                        "unexpected recovery in synthetic benchmark"
                    )
                row = values[0] | {
                    "kind": "cli",
                    "workload": workload,
                    "mode": mode,
                    "trial": trial,
                    "process_s": process_time,
                }
                if trial:
                    report["rows"].append(row)
        print(f"Measured {workload}", flush=True)
    summaries = []
    keys = {
        (row["kind"], row.get("workload", row["kind"]))
        for row in report["rows"]
    }
    for kind, workload in sorted(keys):
        selected = [
            row
            for row in report["rows"]
            if row["kind"] == kind and row.get("workload", kind) == workload
        ]
        medians = {
            mode: statistics.median(
                row["wall_s"] for row in selected if row["mode"] == mode
            )
            for mode in [0, 1]
        }
        summaries.append(
            {
                "kind": kind,
                "workload": workload,
                "baseline_s": medians[0],
                "optimized_s": medians[1],
                "ratio": medians[0] / medians[1],
            }
        )
    report["summary"] = summaries
    producer_sweep(report, trials=min(trials, 2))
    destination = BUILD / "benchmarks.json"
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(summaries, indent=2))
    return report


def main():
    """Run bounded comparisons using previously built executables."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=3)
    arguments = parser.parse_args()
    if not 1 <= arguments.trials <= 10:
        parser.error("trials must be from 1 to 10")
    run_benchmarks(arguments.trials)


if __name__ == "__main__":
    main()
