"""Paired trials of heat2d using today's CLI; no automatic tuning or speedup claims."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import signal
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def positive_seconds(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, data: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def execute(command: list[str], env: dict[str, str], timeout: float) -> tuple[int, str, float]:
    """Keep diagnostic output and allow the CLI to clean its workload on interruption."""
    start = time.perf_counter()
    process = subprocess.Popen(
        command,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            output, _ = process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            output, _ = process.communicate(timeout=3)
        if isinstance(error, KeyboardInterrupt):
            raise
        return 124, output, time.perf_counter() - start
    return process.returncode, output, time.perf_counter() - start


def read_workload_output(text: str, expected: dict[str, Any]) -> dict[str, Any]:
    records = []
    for line in text.splitlines():
        if line.startswith('{"workload":"heat2d"'):
            records.append(json.loads(line))
    if len(records) != 1:
        raise ValueError("expected exactly one heat2d result")
    record = records[0]
    if record.get("valid") is not True:
        raise ValueError("heat2d numerical validation failed")
    for key, value in expected.items():
        if record.get(key) != value:
            raise ValueError(f"heat2d {key}: expected {value!r}, got {record.get(key)!r}")
    for key in ("kernel_seconds", "checksum", "max_abs_error", "tolerance"):
        value = record.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError(f"heat2d {key} is not a finite number")
    # Tiny correctness tests can fall below the workload timer's resolution.
    # Primary ratios use the profiler's positive process wall time, not this field.
    if record["kernel_seconds"] < 0 or not 0 <= record["max_abs_error"] <= 1e-9:
        raise ValueError("invalid timing or numerical error above the fixed tolerance")
    return record


def paired_summary(trials: list[dict[str, Any]], pairs: int, seed: int) -> dict[str, Any]:
    """Descriptive paired ratios; bootstrap interval assumes exchangeable pairs."""
    groups: dict[int, dict[str, dict[str, Any]]] = {}
    all_valid = all(trial["eligible"] for trial in trials)
    for trial in trials:
        if trial["phase"] == "measurement" and trial["arm"] != "direct":
            groups.setdefault(trial["pair"], {})[trial["arm"]] = trial
    reasons = []
    ratios = []
    if not all_valid:
        reasons.append("at least one trial or warmup failed validation")
    if len(groups) != pairs:
        reasons.append("measurement pairs are incomplete")
    for group in groups.values():
        if set(group) != {"baseline", "candidate"}:
            reasons.append("a pair is missing an arm")
            continue
        baseline, candidate = group["baseline"], group["candidate"]
        if not baseline["eligible"] or not candidate["eligible"]:
            continue
        if baseline["collectors"] != candidate["collectors"]:
            reasons.append("profiler configuration changed within a pair")
            continue
        a, b = baseline["wall_time_sec"], candidate["wall_time_sec"]
        if not all(
            not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v) and v > 0
            for v in (a, b)
        ):
            reasons.append("a pair has invalid wall time")
            continue
        ratio = a / b
        if not math.isfinite(ratio) or ratio <= 0:
            reasons.append("a pair has an unrepresentable ratio")
            continue
        ratios.append(ratio)
    if len(ratios) != pairs:
        reasons.append("not every requested pair is eligible")
    result: dict[str, Any] = {
        "status": "invalid" if reasons else "complete",
        "reasons": sorted(set(reasons)),
        "requested_pairs": pairs,
        "eligible_pairs": len(ratios),
        "failed_trials": sum(not trial["eligible"] for trial in trials),
        "observed_wall_ratio": None,
        "paired_bootstrap_95pct": None,
        "interpretation": "baseline / candidate; >1 favors candidate; descriptive, not proof",
        "policy_verification": "planned actions checked; kernel/runtime effects not independently verified",
    }
    if reasons:
        return result
    logs = [math.log(ratio) for ratio in ratios]
    result["observed_wall_ratio"] = math.exp(statistics.mean(logs))
    result["pair_ratios"] = ratios
    if pairs >= 5:
        rng = random.Random(seed)
        samples = sorted(math.exp(statistics.mean(rng.choices(logs, k=pairs))) for _ in range(2000))

        # Linear interpolation between ordered bootstrap estimates.
        def quantile(fraction: float) -> float:
            position = (len(samples) - 1) * fraction
            low = math.floor(position)
            return samples[low] + (position - low) * (
                samples[min(low + 1, len(samples) - 1)] - samples[low]
            )

        result["paired_bootstrap_95pct"] = [quantile(0.025), quantile(0.975)]
    result["interval_limit"] = (
        "2000 paired bootstrap resamples; absent below 5 pairs; within this session only, "
        "not independent-node uncertainty or a multiple-candidate selection correction"
    )
    return result


def run_trial(
    args: argparse.Namespace,
    arm: str,
    phase: str,
    pair: int,
    order: int,
    command: list[str],
    env: dict[str, str],
    expected: dict[str, Any],
) -> dict[str, Any]:
    folder = args.out / f"{phase}-{pair:03d}-{order}-{arm}"
    folder.mkdir()
    trial: dict[str, Any] = {
        "phase": phase,
        "pair": pair,
        "order": order,
        "arm": arm,
        "eligible": False,
        "error": None,
        "path": str(folder.relative_to(args.out)),
    }
    launch = command
    if arm != "direct":
        launch = [sys.executable, "-m", "simtune.cli", "profile" if arm == "baseline" else "run"]
        launch += ["--out", str(folder / "run"), "--timeout", str(args.timeout)]
        if arm == "candidate":
            launch += ["--profiles", str(args.profiles), "--mode", args.mode]
        launch += ["--", *command]
    trial["launch_argv"] = launch
    try:
        code, output, elapsed = execute(launch, env, args.timeout + (60 if arm != "direct" else 0))
        (folder / "launcher.log").write_text(output, encoding="utf-8")
        trial.update({"launcher_return_code": code, "launcher_elapsed_sec": elapsed})
        if code != 0:
            raise ValueError(f"launcher or workload failed with exit {code}")
        if arm == "direct":
            record = read_workload_output(output, expected)
            trial["wall_time_sec"] = elapsed
        else:
            runs = list((folder / "run").glob("run_*"))
            if len(runs) != 1:
                raise ValueError("expected exactly one stored run")
            run = runs[0]
            metrics = json.loads((run / "metrics.json").read_text())
            actions = json.loads((run / "applied_actions.json").read_text())
            meta = json.loads((run / "meta.json").read_text())
            if len(metrics["iterations"]) != 1:
                raise ValueError("expected exactly one iteration")
            sample = metrics["iterations"][0]
            if sample.get("return_code") != 0 or sample.get("timed_out"):
                raise ValueError("stored workload status is not successful")
            if meta["mode"] != ("baseline" if arm == "baseline" else args.mode):
                raise ValueError("profile fallback occurred")
            if actions["skipped"] or actions["adjusted"]:
                raise ValueError("actions were skipped or adjusted; inspect this trial")
            record = read_workload_output((run / "raw_profiler.txt").read_text(), expected)
            trial["collectors"] = {
                key: sample["profiler"].get(key) for key in ("used_time", "used_perf")
            }
            trial["wall_time_sec"] = sample["wall_time_sec"]
        if arm == "candidate" and args.mode.startswith("omp_") and not record.get("openmp"):
            raise ValueError("an OpenMP policy requires an OpenMP build")
        trial.update({"workload": record, "eligible": True})
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        trial["error"] = str(error)
    write_json(folder / "trial.json", trial)
    return trial


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--profiles", type=Path, default=Path("profiles/heat-experiment.yaml"))
    parser.add_argument("--mode", default="inherited", help="Explicit candidate, never auto")
    parser.add_argument("--out", type=Path, required=True, help="New directory; never overwritten")
    parser.add_argument("--size", type=positive_int, default=512)
    parser.add_argument("--steps", type=positive_int, default=100)
    parser.add_argument("--wave", type=positive_int, default=1)
    parser.add_argument("--threads", type=positive_int, default=2)
    parser.add_argument("--pairs", type=positive_int, default=6)
    parser.add_argument(
        "--warmups", type=positive_int, default=1, help="Unmeasured pairs, retained"
    )
    parser.add_argument("--seed", type=int, default=20260912)
    parser.add_argument("--first-touch", choices=["parallel", "serial"], default="parallel")
    parser.add_argument("--timeout", type=positive_seconds, default=60)
    parser.add_argument(
        "--include-direct", action="store_true", help="Additional untuned diagnostic arm"
    )
    parser.add_argument(
        "--build-info", type=Path, help="Text recording compiler, flags and source identity"
    )
    args = parser.parse_args(argv)
    if args.mode == "auto":
        parser.error("choose an explicit candidate; auto adds a baseline and changes the protocol")
    if not 3 <= args.size <= 32768 or args.wave >= args.size - 1 or args.steps > 1000000:
        parser.error("require size 3..32768, wave < size-1, steps <= 1000000")
    args.binary = args.binary.resolve(strict=True)
    args.profiles = args.profiles.resolve(strict=True)
    args.out = args.out.resolve()
    if not args.binary.is_file() or not os.access(args.binary, os.X_OK):
        parser.error("--binary must be an executable file")
    from simtune.profiles import load_profiles

    available = load_profiles([str(args.profiles)])
    if args.mode not in available:
        parser.error(f"unknown candidate {args.mode!r}; choose from {', '.join(available)}")
    if args.out.exists():
        parser.error("--out must be a new directory")
    affinity = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    if affinity is not None and args.threads > len(affinity):
        parser.error("thread count exceeds inherited CPU affinity")
    # Use only declared environment settings; do not collect the full environment.
    env = os.environ.copy()
    env.update({"OMP_NUM_THREADS": str(args.threads), "OMP_DYNAMIC": "FALSE"})
    args.out.mkdir(parents=True)
    if args.build_info:
        (args.out / "build-info.txt").write_text(args.build_info.read_text(), encoding="utf-8")
    import simtune

    command = [
        str(args.binary),
        "--size",
        str(args.size),
        "--steps",
        str(args.steps),
        "--wave",
        str(args.wave),
        "--first-touch",
        args.first_touch,
    ]
    manifest = {
        "schema_version": 1,
        "status": "running",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "command": command,
        "binary_sha256": sha256(args.binary),
        "profiles_sha256": sha256(args.profiles),
        "profile_file": str(args.profiles),
        "candidate": args.mode,
        "threads": args.threads,
        "pairs": args.pairs,
        "warmup_pairs": args.warmups,
        "seed": args.seed,
        "timeout_sec": args.timeout,
        "include_direct": args.include_direct,
        "python": sys.version,
        "simtune_version": simtune.__version__,
        "platform": platform.platform(),
        "hostname": platform.node(),
        "affinity": affinity,
        "environment": {
            key: value
            for key, value in env.items()
            if key.startswith(("OMP_", "GOMP_", "KMP_"))
            or key in {"SLURM_JOB_ID", "SLURM_STEP_ID", "SLURM_CPUS_PER_TASK", "SLURM_MEM_PER_NODE"}
        },
        "interpretation": "manual single-candidate comparison, not an automatic optimizer result",
    }
    write_json(args.out / "manifest.json", manifest)
    (args.out / "candidate-profiles.yaml").write_bytes(args.profiles.read_bytes())
    expected = {
        "size": args.size,
        "steps": args.steps,
        "wave": args.wave,
        "first_touch": args.first_touch,
        "threads": args.threads,
    }
    rng, trials = random.Random(args.seed), []
    try:
        for phase, count in (("warmup", args.warmups), ("measurement", args.pairs)):
            for pair in range(1, count + 1):
                arms = ["baseline", "candidate"] + (["direct"] if args.include_direct else [])
                rng.shuffle(arms)
                for order, arm in enumerate(arms, 1):
                    trial = run_trial(args, arm, phase, pair, order, command, env, expected)
                    trials.append(trial)
                    with (args.out / "trials.jsonl").open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(trial, allow_nan=False) + "\n")
                    print(
                        f"{phase} {pair}/{count} {arm}: {'ok' if trial['eligible'] else trial['error']}",
                        flush=True,
                    )
        summary = paired_summary(trials, args.pairs, args.seed)
        if (
            sha256(args.binary) != manifest["binary_sha256"]
            or sha256(args.profiles) != manifest["profiles_sha256"]
        ):
            summary.update(
                {"status": "invalid", "observed_wall_ratio": None, "paired_bootstrap_95pct": None}
            )
            summary["reasons"].append("binary or profiles changed during the experiment")
        write_json(args.out / "summary.json", summary)
        manifest["status"] = summary["status"]
        print(json.dumps(summary, indent=2, allow_nan=False))
        return 0 if summary["status"] == "complete" else 1
    except KeyboardInterrupt:
        manifest["status"] = "interrupted"
        return 130
    finally:
        if manifest["status"] == "running":
            manifest["status"] = "failed"
        write_json(args.out / "manifest.json", manifest)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"heat_benchmark: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
