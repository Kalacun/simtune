"""Paired manual-profile demonstration; not automatic tuning or recipe evidence.

Uses a fresh directory, freezes order before launches, checks every result and
retains failed/interrupted attempts. Requires simtune[yaml] and scikit-learn.
"""

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from simtune.cli import _positive_timeout


def equivalent(reference, result):
    """Require identical input/labels and numerically equivalent clustering objectives."""
    if result.get("checks") != "passed" or reference.get("checks") != "passed":
        return False
    for name in ("dataset_sha256", "shape", "sklearn_version"):
        if reference.get(name) != result.get(name):
            return False
    left, right = reference.get("results", []), result.get("results", [])
    if len(left) != 5 or len(right) != 5:
        return False
    if [row["seed"] for row in left] != list(range(5)):
        return False
    for a, b in zip(left, right):
        if a["seed"] != b["seed"] or a["labels_sha256"] != b["labels_sha256"]:
            return False
        if not all(math.isfinite(x) for x in (a["inertia"], b["inertia"])):
            return False
        if not math.isclose(a["inertia"], b["inertia"], rel_tol=1e-10, abs_tol=1e-6):
            return False
    return True


def summarize(rows, pairs):
    measured = [row for row in rows if row["phase"] == "measurement"]
    warmups = [row for row in rows if row["phase"] == "warmup"]
    if (
        len(measured) != 2 * pairs
        or len(rows) != 2 * pairs + 2
        or len(warmups) != 2
        or {row["mode"] for row in warmups} != {"inherited", "one_thread"}
        or any(row["status"] != "passed" for row in rows)
    ):
        return {"status": "inconclusive", "reason": "missing, failed or unchecked trial"}
    ratios = []
    for pair in range(pairs):
        pair_rows = [row for row in measured if row["pair"] == pair]
        if len(pair_rows) != 2 or {row["mode"] for row in pair_rows} != {"inherited", "one_thread"}:
            return {"status": "inconclusive", "reason": "incomplete paired evidence"}
        values = {row["mode"]: row["wall_seconds"] for row in pair_rows}
        if any(not math.isfinite(v) or v <= 0 for v in values.values()):
            return {"status": "inconclusive", "reason": "invalid wall time"}
        ratios.append(values["inherited"] / values["one_thread"])
    rng = random.Random(20261022)
    logs = [math.log(value) for value in ratios]
    draws = sorted(math.exp(statistics.mean(rng.choices(logs, k=pairs))) for _ in range(10000))
    return {
        "status": "complete",
        "pairs": pairs,
        "baseline_over_candidate": math.exp(statistics.mean(logs)),
        "descriptive_bootstrap_95_percent_interval": [draws[249], draws[9749]],
        "wall_seconds_mean": {
            mode: statistics.mean(row["wall_seconds"] for row in measured if row["mode"] == mode)
            for mode in ("inherited", "one_thread")
        },
        "pair_ratios": ratios,
        "scope": "one local session; manual thread budget change; no held-out or transfer claim",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="fresh output directory")
    parser.add_argument("--timeout", type=_positive_timeout, default=60)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    workload = root / "examples/digits_workload.py"
    profiles = root / "profiles/digits.yaml"
    rng = random.Random(20261022)
    orders = [["inherited", "one_thread"], ["one_thread", "inherited"]] * 5
    rng.shuffle(orders)
    protocol = {
        "schema_version": 1,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "pairs": 10,
        "warmups_per_mode": 1,
        "orders": orders,
        "timeout_seconds_per_trial": args.timeout,
        "workload_sha256": hashlib.sha256(workload.read_bytes()).hexdigest(),
        "profiles_sha256": hashlib.sha256(profiles.read_bytes()).hexdigest(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "logical_cpus": os.cpu_count(),
        },
        "python": platform.python_version(),
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("simtune", "numpy", "scipy", "scikit-learn", "threadpoolctl", "PyYAML")
        },
        "inherited_runtime_env": {
            key: os.environ.get(key)
            for key in (
                "OMP_NUM_THREADS",
                "OMP_PROC_BIND",
                "OMP_PLACES",
                "OPENBLAS_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS",
            )
        },
        "scientific_work": "UCI digits, 1797x64; KMeans(10 clusters, 50 initializations, Lloyd), seeds 0..4, fixed float64 input",
        "candidate": "manual OMP_NUM_THREADS=1; baseline preserves inherited settings",
        "validation": "every objective recomputed; all five label hashes identical and objectives close at rtol=1e-10, atol=1e-6 to first warmup",
        "limitations": "exploratory candidate followed by fresh pairs; one dataset/session; no automatic selection, equal-resource claim, held-out confirmation or validated recipe",
    }
    (out / "protocol.json").write_text(json.dumps(protocol, indent=2) + "\n")
    rows = []
    reference = None
    schedule = [("warmup", -1, ["inherited", "one_thread"])] + [
        ("measurement", i, order) for i, order in enumerate(orders)
    ]
    for phase, pair, order in schedule:
        for mode in order:
            trial = out / f"{phase}-{pair + 1:02d}-{mode}"
            trial.mkdir()
            command = [
                sys.executable,
                "-m",
                "simtune.cli",
                "run",
                "--mode",
                mode,
                "--profiles",
                str(profiles),
                "--out",
                str(trial / "runs"),
                "--timeout",
                str(args.timeout),
                "--",
                sys.executable,
                str(workload),
            ]
            (trial / "command.json").write_text(json.dumps(command) + "\n")
            row = {"phase": phase, "pair": pair, "mode": mode, "status": "interrupted"}
            rows.append(row)
            (out / "results.json").write_text(json.dumps(rows, indent=2) + "\n")
            started = time.monotonic()
            try:
                result = subprocess.run(
                    command,
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=args.timeout + 30,
                    check=False,
                )
                (trial / "cli.stdout").write_text(result.stdout)
                (trial / "cli.stderr").write_text(result.stderr)
                row["return_code"] = result.returncode
                row["cli_seconds"] = time.monotonic() - started
                directories = list((trial / "runs").glob("run_*"))
                if result.returncode != 0 or len(directories) != 1:
                    raise ValueError("CLI failed or produced an ambiguous run")
                run = directories[0]
                metrics = json.loads((run / "metrics.json").read_text())["iterations"]
                actions = json.loads((run / "applied_actions.json").read_text())
                if (
                    len(metrics) != 1
                    or metrics[0]["return_code"] != 0
                    or metrics[0].get("timed_out")
                    or actions["skipped"]
                    or actions["adjusted"]
                ):
                    raise ValueError("failed measurement or lost launch setting")
                raw = (run / "raw_profiler.txt").read_text()
                outputs = [
                    line.split("=", 1)[1]
                    for line in raw.splitlines()
                    if line.startswith("SIMTUNE_DIGITS_RESULT=")
                ]
                if len(outputs) != 1:
                    raise ValueError("missing or ambiguous scientific output")
                report = json.loads(outputs[0])
                reference = report if reference is None else reference
                if not equivalent(reference, report):
                    raise ValueError("scientific outputs differ from the reference")
                pools = [pool for pool in report["threadpools"] if pool["user_api"] == "openmp"]
                if mode == "one_thread" and (
                    not pools or any(pool["num_threads"] != 1 for pool in pools)
                ):
                    raise ValueError("one-thread runtime setting was not observed")
                row.update(
                    status="passed",
                    wall_seconds=metrics[0]["wall_time_sec"],
                    scientific_output=report,
                )
            except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
                row.update(status="failed", reason=str(exc))
            (out / "results.json").write_text(json.dumps(rows, indent=2, allow_nan=False) + "\n")
            print(f"{phase} {pair + 1} {mode}: {row['status']}", flush=True)
            if row["status"] != "passed":
                (out / "summary.json").write_text(json.dumps(summarize(rows, 10), indent=2) + "\n")
                return 1
    summary = summarize(rows, 10)
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
