"""Compare the approved generation changes with the shutdown-fix baseline."""

import argparse
import hashlib
import json
import math
import statistics
import subprocess
import sys
import time

from benchmark_cuda import inputs
from build_cuda import BUILD, ROOT

BASELINE = "3c1d78df57b53b082686eb52e7aa253ff4da3a52"
SOURCES = [
    "multibit_cuda_threads.cu",
    "cuda_crypto.cuh",
    "cuda_buffers.hpp",
    "cuda_generation.hpp",
    "cuda_pipeline.hpp",
    "cuda_typos.hpp",
]
VARIANTS = {
    "baseline": "",
    "bulk": "#define MULTIBIT_DISABLE_CHUNK_POOL 1\n",
    "pool": "#define MULTIBIT_SCALAR_CHUNK_MERGE 1\n",
    "combined": "",
}
WORK = BUILD / "generation_benchmark"


def executable(variant):
    """Return an isolated executable path."""
    return BUILD / f"optimized_generation_{variant}.exe"


def build_variants(variants):
    """Build exact baseline headers and independently switched variants."""
    for variant in variants:
        directory = WORK / variant
        directory.mkdir(parents=True, exist_ok=True)
        hashes = {}
        for name in SOURCES:
            if variant == "baseline":
                result = subprocess.run(
                    ["git", "show", (f"{BASELINE}:BitCracker/"
                                     f"btcrecover-master/{name}")],
                    cwd=ROOT, capture_output=True, check=True,
                )
                data = result.stdout
            else:
                data = (ROOT / name).read_bytes()
            if name == SOURCES[0]:
                data = VARIANTS[variant].encode("utf-8") + data
            path = directory / name
            path.write_bytes(data)
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        (directory / "source_hashes.json").write_text(
            json.dumps(hashes, indent=2), encoding="utf-8"
        )
        result = subprocess.run(
            [sys.executable, "tools/build_cuda.py", "optimized", "--source",
             str(directory / SOURCES[0]), "--suffix",
             f"_generation_{variant}"],
            cwd=ROOT, capture_output=True, text=True, timeout=180, check=False,
        )
        (directory / "build.log").write_text(
            result.stdout + result.stderr, encoding="utf-8"
        )
        if result.returncode or "warning" in result.stdout.lower():
            raise RuntimeError(f"Build failed or warned: {variant}")
        print(f"Built {variant}", flush=True)


def workloads():
    """Use fixed counts, including many small work units and mixed lengths."""
    result = {}
    for name in ["primary", "mixed", "long"]:
        rows = []
        for line in range(6):
            tokens = []
            for index in range(7):
                length = 2
                if name == "long":
                    length = 20
                elif name == "mixed":
                    length = [2, 5, 10, 12, 16, 18, 20][index]
                tokens.append(chr(97 + line) * (length - 1) + str(index))
            rows.append("+ " + " ".join(tokens))
        result[name] = ("\n".join(rows) + "\n", [], 84707280)
    typo, flags, count = inputs()["typos"]
    # Keep the repeated alternatives below the parser's 8192-byte line limit.
    result["typos"] = (
        "+ " + " ".join(typo.split()[1:] * 30) + "\n", flags, count * 30
    )
    return result


def gpu_snapshot():
    """Record background GPU activity without changing any process."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used",
             "--format=csv,noheader"], capture_output=True, text=True,
            timeout=10, check=False,
        )
        return {"exit_code": result.returncode, "output": result.stdout}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"unavailable": str(error)}


def run_once(variant, name, flags, expected):
    """Run real CUDA, fail on count drift, and retain both timing scopes."""
    command = [
        str(executable(variant)), "--wallet",
        str(ROOT / "btcrecover/test/test-wallets/multibit-wallet.key"),
        "--tokenlist", str(WORK / f"{name}.txt"),
        "--producers", "4", "--batch-size", "1048576", "--timings", *flags,
    ]
    start = time.perf_counter()
    result = subprocess.run(
        command, cwd=WORK, capture_output=True, text=True,
        timeout=60, check=True,
    )
    process_s = time.perf_counter() - start
    rows = [json.loads(line[8:]) for line in result.stdout.splitlines()
            if line.startswith("TIMINGS ")]
    if (len(rows) != 1 or rows[0]["count"] != expected
            or "Not found" not in result.stdout or rows[0]["wall_s"] <= 0):
        raise AssertionError(f"Invalid benchmark result: {variant}/{name}")
    return rows[0] | {"process_s": process_s}


def measure(variants, names, trials, destination):
    """Alternate fixed-work trials and report the approved performance gate."""
    report = {
        "baseline": BASELINE, "trials": trials, "rows": [], "summary": [],
        "note": "Shared desktop GPU; no process was stopped for measurement.",
        "binaries": {name: hashlib.sha256(executable(name).read_bytes())
                     .hexdigest() for name in variants},
    }

    def save():
        destination.write_text(json.dumps(report, indent=2), encoding="utf-8")

    save()
    for name, (tokens, flags, expected) in workloads().items():
        if name not in names:
            continue
        (WORK / f"{name}.txt").write_text(tokens, encoding="ascii")
        pilots = {v: run_once(v, name, flags, expected) for v in variants}
        # Same repeated work for every variant; fastest warmup targets 3 s.
        repeats = max(1, math.ceil(3 / min(
            row["wall_s"] for row in pilots.values()
        )))
        for trial in range(trials):
            order = variants if trial % 2 == 0 else list(reversed(variants))
            for variant in order:
                activity = gpu_snapshot()
                runs = [run_once(variant, name, flags, expected)
                        for _ in range(repeats)]
                row = {
                    "workload": name, "variant": variant, "trial": trial,
                    "repeats": repeats, "runs": runs, "gpu_before": activity,
                    "rate": (expected * repeats
                             / sum(r["wall_s"] for r in runs)),
                    "search_s": sum(r["wall_s"] for r in runs),
                    "process_s": sum(r["process_s"] for r in runs),
                }
                report["rows"].append(row)
                save()
                print(f"{name} {trial + 1}/{trials} {variant}: "
                      f"{row['rate'] / 1e6:.2f}M/s", flush=True)
        baseline = [r["rate"] for r in report["rows"]
                    if r["workload"] == name and r["variant"] == "baseline"]
        for variant in variants:
            rates = [r["rate"] for r in report["rows"]
                     if r["workload"] == name and r["variant"] == variant]
            ratio = statistics.median(rates) / statistics.median(baseline)
            wins = sum(a > b for a, b in zip(rates, baseline, strict=True))
            report["summary"].append({
                "workload": name, "variant": variant, "ratio": ratio,
                "wins": wins, "median_pw_s": statistics.median(rates),
                "improvement_gate": ratio >= 1.05 and wins >= 4,
                "regression_over_5_percent": ratio < 0.95,
            })
        save()
    report["completed"] = True
    save()


def main():
    """Build or measure isolated variants without replacing installed files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--variants", nargs="+", choices=VARIANTS,
                        default=list(VARIANTS))
    parser.add_argument("--workloads", nargs="+", choices=workloads(),
                        default=list(workloads()))
    args = parser.parse_args()
    if args.trials < 1 or "baseline" not in args.variants:
        parser.error("positive trials and baseline variant are required")
    WORK.mkdir(parents=True, exist_ok=True)
    if not args.skip_build:
        build_variants(args.variants)
    if not args.build_only:
        measure(args.variants, args.workloads, args.trials,
                BUILD / "generation-benchmarks.json")


if __name__ == "__main__":
    main()
