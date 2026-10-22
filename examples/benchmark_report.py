"""Sequential baseline/tuned demonstration using the same profiling path.

This is a convenience report, not a controlled benchmark experiment. It makes
no inference about statistical significance and refuses ratios on failed runs.
"""

import argparse
import sys
from pathlib import Path
from typing import Any

from simtune import capabilities, metrics, profiles, runner, storage, util
from simtune.cli import _positive_repeat, _positive_timeout


def render_report(
    plain: dict[str, Any],
    tuned: dict[str, Any],
    tuned_dir: str | None,
    rationale: dict[str, Any],
    applied_actions: dict[str, Any],
) -> str:
    baseline_agg = metrics.aggregate_metrics(plain.get("iterations", []))
    tuned_agg = metrics.aggregate_metrics(tuned.get("iterations", []))
    lines = ["simtune benchmark report", "", "Sequential measurements; no significance claim."]
    for label, agg in (("baseline", baseline_agg), ("tuned", tuned_agg)):
        lines.append(
            f"{label}: successful={agg.get('successful_count', 0)}, failed={agg.get('failed_count', 0)}, "
            f"wall_time_sec_mean={agg.get('wall_time_sec_mean')}"
        )
    baseline_wall = baseline_agg.get("wall_time_sec_mean")
    tuned_wall = tuned_agg.get("wall_time_sec_mean")
    if (
        baseline_wall
        and tuned_wall
        and not baseline_agg.get("failed_count")
        and not tuned_agg.get("failed_count")
    ):
        lines.append(
            f"Observed wall-time ratio (baseline / tuned): {baseline_wall / tuned_wall:.3f}x"
        )
    else:
        lines.append("Wall-time ratio unavailable: incomplete or failed measurements.")
    lines.extend([f"Tuned run: {tuned_dir}", f"Rationale: {rationale}"])
    for label in ("applied", "skipped", "adjusted"):
        lines.append(f"{label}: {applied_actions.get(label, [])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="runs")
    parser.add_argument("--repeat", type=_positive_repeat, default=3)
    parser.add_argument("--timeout", type=_positive_timeout)
    parser.add_argument("--mode", default="auto", choices=["auto", *profiles.BUILTIN_PROFILES])
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    cmd = cmd or [sys.executable, str(Path(__file__).with_name("cpu_bound.py"))]
    caps = capabilities.detect_capabilities()
    data = profiles.load_profiles()
    data["baseline"] = {"actions": [], "fallback_mode": None}
    try:
        baseline_dir = runner.run_command(
            cmd,
            args.out,
            "baseline",
            data,
            caps,
            args.timeout,
            args.repeat,
            False,
            False,
            False,
            False,
        )
        baseline = util.load_json(Path(baseline_dir) / "metrics.json")
        code = metrics.exit_status(baseline["iterations"])
        if code:
            print(f"Baseline failed; inspect {baseline_dir}/raw_profiler.txt", file=sys.stderr)
            return code
        agg = baseline["aggregate"]
        inputs = {
            key: agg.get(f"{key}_mean")
            for key in ("wall_time_sec", "user_time_sec", "sys_time_sec", "in_block", "out_block")
        }
        tuned_dir = runner.run_command(
            cmd,
            args.out,
            args.mode,
            data,
            caps,
            args.timeout,
            args.repeat,
            False,
            args.mode == "auto",
            False,
            False,
            baseline_metrics=inputs,
            baseline_run_dir=baseline_dir,
        )
        tuned = util.load_json(Path(tuned_dir) / "metrics.json")
        actions = util.load_json(Path(tuned_dir) / "applied_actions.json")
        rationale = util.load_json(Path(tuned_dir) / "rationale.json")
        report = render_report(baseline, tuned, tuned_dir, rationale, actions)
        report_dir = storage.create_run_dir(args.out, "report")
        util.write_json(report_dir / "plain_metrics.json", baseline)
        util.write_json(report_dir / "simtune_metrics.json", tuned)
        util.write_json(report_dir / "sources.json", {"baseline": baseline_dir, "tuned": tuned_dir})
        util.write_text(report_dir / "report.txt", report)
        print(report)
        print(f"Report directory: {report_dir}")
        return metrics.exit_status(tuned["iterations"])
    except (OSError, ValueError, KeyError) as exc:
        print(f"benchmark report: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
