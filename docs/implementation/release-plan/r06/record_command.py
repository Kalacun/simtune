"""Record one bounded command; diagnostic elapsed time is never a study sample."""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from simtune.profiler import execute_command


def record(argv, directory, timeout, *, cwd=None, overrides=None):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    overrides = overrides or {}
    intent = {
        "argv": list(argv),
        "cwd": str(Path(cwd or Path.cwd()).resolve()),
        "environment_overrides": overrides,
        "timeout_seconds": timeout,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "state": "running",
        "scope": "whole command diagnostic; excluded from primary study samples",
    }
    (directory / "command.json").write_text(json.dumps(intent, indent=2) + "\n")
    metrics, raw = execute_command(
        list(argv), {**os.environ, **overrides}, timeout, cwd=intent["cwd"]
    )
    # The shared executor retains separate STDOUT/STDERR sections and cancellation.
    (directory / "stdout-stderr.txt").write_text(raw)
    (directory / "result.json").write_text(json.dumps(metrics, indent=2, allow_nan=False) + "\n")
    intent.update(state="recorded", ended_utc=datetime.now(timezone.utc).isoformat())
    (directory / "command.json").write_text(json.dumps(intent, indent=2) + "\n")
    return metrics, raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--env-json", default="{}")
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    overrides = json.loads(args.env_json)
    if not isinstance(overrides, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in overrides.items()
    ):
        parser.error("--env-json must map explicit environment names to strings")
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not argv or not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("supply argv and a finite positive timeout")
    metrics, raw = record(argv, args.out, args.timeout, cwd=args.cwd, overrides=overrides)
    print(raw, end="")
    code = metrics["return_code"]
    return 128 - code if code < 0 else code


if __name__ == "__main__":
    raise SystemExit(main())
