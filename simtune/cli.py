"""Command-line interface for simtune."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from . import __version__, capabilities, compare, metrics, profiles, runner, util


def _parse_cmd(args: list[str]) -> list[str]:
    if not args:
        return []
    if args[0] == "--":
        return args[1:]
    return args


def _positive_repeat(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("repeat must be a positive integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("repeat must be a positive integer")
    return number


def _positive_timeout(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a finite positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("timeout must be a finite positive number")
    return number


def _stored_status(run_dir: str | None) -> int:
    if run_dir is None:
        return 0
    data = util.load_json(Path(run_dir) / "metrics.json")
    code = metrics.exit_status(data["iterations"])
    if code:
        print(
            f"Workload or wrapper failed (exit {code}); see {run_dir}/raw_profiler.txt",
            file=sys.stderr,
        )
    return code


def _parse_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out", required=True, help="Output directory for run data")
    parser.add_argument(
        "--timeout", type=_positive_timeout, default=None, help="Timeout in seconds"
    )
    parser.add_argument("--repeat", type=_positive_repeat, default=1, help="Repeat count")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without executing")


def _parse_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mode", required=True, help="Profile name or 'auto'")
    parser.add_argument("--latency-important", action="store_true", help="Prefer latency mode")
    parser.add_argument(
        "--respect-desktop", action="store_true", help="Prefer desktop friendly mode"
    )
    parser.add_argument(
        "--notify", action="store_true", help="Send a desktop notification when done"
    )


def cmd_doctor(_args: argparse.Namespace) -> int:
    caps = capabilities.detect_capabilities()
    print(capabilities.doctor_report(caps))
    return 0


def cmd_list_modes(_args: argparse.Namespace) -> int:
    profiles_data = profiles.load_profiles(getattr(_args, "profiles", None))
    rows = profiles.list_modes(profiles_data)
    print(util.render_table(rows, ["name", "description", "required"]))
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    cmd = _parse_cmd(args.cmd)
    if not cmd:
        print("No command provided", file=sys.stderr)
        return 2
    caps = capabilities.detect_capabilities()
    profiles_data = {}
    profiles_data["baseline"] = {
        "description": "baseline",
        "actions": [],
        "fallback_mode": "baseline",
        "safety_notes": "",
    }

    run_dir = runner.run_command(
        cmd,
        args.out,
        "baseline",
        profiles_data,
        caps,
        args.timeout,
        args.repeat,
        args.dry_run,
        auto_mode=False,
        latency_important=False,
        respect_desktop=False,
    )
    if run_dir:
        print(f"Run stored in {run_dir}")
    return _stored_status(run_dir)


def cmd_run(args: argparse.Namespace) -> int:
    cmd = _parse_cmd(args.cmd)
    if not cmd:
        print("No command provided", file=sys.stderr)
        return 2

    caps = capabilities.detect_capabilities()
    profiles_data = profiles.load_profiles(args.profiles)

    if args.mode != "auto" and args.mode not in profiles_data:
        raise ValueError(f"unknown mode {args.mode!r}; available: {', '.join(profiles_data)}")
    if args.mode != "auto" and (args.latency_important or args.respect_desktop):
        raise ValueError("--latency-important and --respect-desktop apply only to --mode auto")
    if args.mode == "auto" and not set(profiles.BUILTIN_PROFILES).issubset(profiles_data):
        raise ValueError(
            "auto mode requires desktop_friendly, throughput, isolated, and latency profiles"
        )

    baseline_metrics = None
    baseline_dir = None
    if args.mode == "auto" and not args.dry_run:
        baseline_dir = runner.run_command(
            cmd,
            args.out,
            "baseline",
            {"baseline": {"actions": [], "fallback_mode": None}},
            caps,
            args.timeout,
            1,
            False,
            False,
            False,
            False,
        )
        print(f"Auto baseline stored in {baseline_dir}")
        code = _stored_status(baseline_dir)
        if code:
            return code
        baseline_metrics = util.load_json(Path(baseline_dir) / "metrics.json")["iterations"][0]

    run_dir = runner.run_command(
        cmd,
        args.out,
        args.mode,
        profiles_data,
        caps,
        args.timeout,
        args.repeat,
        args.dry_run,
        auto_mode=args.mode == "auto",
        latency_important=args.latency_important,
        respect_desktop=args.respect_desktop,
        baseline_metrics=baseline_metrics,
        notify=args.notify,
        baseline_run_dir=baseline_dir,
    )
    if run_dir:
        print(f"Run stored in {run_dir}")
    return _stored_status(run_dir)


def cmd_compare(args: argparse.Namespace) -> int:
    table, _rows = compare.compare_runs(args.run_dirs, args.out)
    print(table)
    print(f"Wrote {args.out}/compare.json and {args.out}/compare.csv")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    from .lookup import find_run, show_run

    report = show_run(find_run(args.run, args.runs_dir))
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
    elif report["record_kind"] == "experiment":
        from .reporting import render_decision_report

        budget = report["budget"]
        print(render_decision_report(report["decision_report"]))
        print(
            f"Phase: {report['phase']}; trials: {report['next_index']}/{report['scheduled_trials']}"
        )
        print(
            f"Budget: {budget['spent_seconds']:.3f}s spent of "
            f"{budget['authorized_seconds'] + sum(budget['additional_seconds']):.3f}s authorized"
        )
        print(f"Selected: {report['selected_policy_id'] or 'none'}; {report['label']}")
        for item in report["candidates"]:
            if item["exclusion_reason"]:
                print(f"Excluded {item['policy_id']}: {item['exclusion_reason']}")
        print(f"Evidence: {report['path']}")
        if not report["complete"]:
            print(f"Next: simtune tune --resume {report['path']}")
    else:
        print(
            f"{report['id']}: legacy run schema {report['meta'].get('schema_version', 'unknown')}"
        )
        print(f"Evidence: {report['path']}")
        print(json.dumps(report["metrics"].get("aggregate", {}), indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simtune", description="Inspect, profile, and tune command workloads"
    )
    parser.add_argument("--version", action="version", version=f"simtune {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Inspect platform capabilities")
    doctor.set_defaults(func=cmd_doctor)

    list_modes = subparsers.add_parser("list-modes", help="List available tuning profiles")
    list_modes.add_argument(
        "--profiles", action="append", metavar="PATH", help="Custom YAML file (repeatable)"
    )
    list_modes.set_defaults(func=cmd_list_modes)

    profile_cmd = subparsers.add_parser("profile", help="Run baseline profiling")
    _parse_common(profile_cmd)
    profile_cmd.add_argument("cmd", nargs=argparse.REMAINDER)
    profile_cmd.set_defaults(func=cmd_profile)

    run_cmd = subparsers.add_parser("run", help="Run with tuning profile")
    _parse_common(run_cmd)
    _parse_run(run_cmd)
    run_cmd.add_argument(
        "--profiles", action="append", metavar="PATH", help="Custom YAML file (repeatable)"
    )
    run_cmd.add_argument("cmd", nargs=argparse.REMAINDER)
    run_cmd.set_defaults(func=cmd_run)

    compare_cmd = subparsers.add_parser("compare", help="Compare run directories")
    compare_cmd.add_argument("run_dirs", nargs="+", help="Run directories to compare")
    compare_cmd.add_argument(
        "--out", default=".", help="Directory for comparison exports (default: current directory)"
    )
    compare_cmd.set_defaults(func=cmd_compare)

    show = subparsers.add_parser("show", help="Inspect a saved experiment or legacy run")
    show.add_argument("run", help="Exact directory or unique ID prefix")
    show.add_argument(
        "--runs-dir", default="runs", help="Directory searched for IDs (default: runs)"
    )
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_show)

    imported = subparsers.add_parser("import-profile", help="Import external perf stat diagnostics")
    imported.add_argument("--format", choices=("perf-stat",), required=True)
    imported.add_argument("--input", required=True, help="UTF-8 perf stat -x ';' output")
    imported.add_argument("--context", required=True, help="Versioned JSON measurement context")
    imported.add_argument("--out", required=True, help="New import directory")
    imported.set_defaults(func=cmd_import_profile)

    from .replay import add_parsers
    from .tune import add_parser

    add_parser(subparsers)
    add_parsers(subparsers)
    return parser


def cmd_import_profile(args: argparse.Namespace) -> int:
    from .importers import import_profile

    record = import_profile(args.input, args.context, args.out)
    print(
        f"Imported {len(record['metrics'])} perf rows to {args.out}; external context remains declared, not verified."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        with util.cancellation_scope() as cancellation:
            status = args.func(args)
            return 128 + cancellation.signum if cancellation.signum is not None else status
    except (OSError, ValueError, KeyError) as exc:
        print(f"simtune: {exc}", file=sys.stderr)
        return 2
    except util.CommandCancelled as exc:
        print("simtune: interrupted or subprocess termination uncertain", file=sys.stderr)
        code = exc.metrics.get("return_code")
        return 128 - code if code is not None and code < 0 else code or 130
    except KeyboardInterrupt:
        print("simtune: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
