"""MIT licensed, pinned command validator for the fixed heat2d OpenMP example."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

TOLERANCE = 1e-9
FIELDS = {
    "workload",
    "schema_version",
    "size",
    "steps",
    "wave",
    "first_touch",
    "openmp",
    "threads",
    "grid_bytes",
    "updates",
    "init_seconds",
    "kernel_seconds",
    "checksum",
    "max_abs_error",
    "tolerance",
    "valid",
}


def positive_int(text: str) -> int:
    if not text.isascii() or not text.isdecimal() or int(text) < 1:
        raise argparse.ArgumentTypeError("must be a positive decimal integer")
    return int(text)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError(f"nonfinite JSON constant: {value}")


def number(value, name, *, nonnegative=False):
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or (nonnegative and value < 0):
        raise ValueError(f"{name} must be finite" + (" and nonnegative" if nonnegative else ""))
    return value


def check_result(
    directory: Path, *, size: int, steps: int, wave: int, first_touch: str, threads: int
):
    root = directory.resolve(strict=True)
    output = (root / "heat-result.json").resolve(strict=True)
    if output.parent != root or not output.is_file():
        raise ValueError("heat-result.json must be a regular file inside the trial directory")
    with output.open("rb") as stream:
        payload = stream.read(65537)
    if len(payload) > 65536:
        raise ValueError("heat result exceeds the 64 KiB validator limit")
    data = json.loads(payload, parse_constant=reject_constant, object_pairs_hook=unique_object)
    if type(data) is not dict or set(data) != FIELDS:
        raise ValueError("unexpected heat result schema/fields")
    for key, expected in {
        "schema_version": 1,
        "size": size,
        "steps": steps,
        "wave": wave,
        "threads": threads,
        "grid_bytes": 16 * size * size,
    }.items():
        if type(data[key]) is not int or data[key] != expected:
            raise ValueError(f"incorrect {key}: expected {expected}")
    if data["workload"] != "heat2d" or data["first_touch"] != first_touch:
        raise ValueError("incorrect workload/first_touch")
    if type(data["openmp"]) is not int or data["openmp"] <= 0:
        raise ValueError("this checked configuration requires an OpenMP build")
    if data["valid"] is not True:
        raise ValueError("valid must be exactly true")
    number(data["checksum"], "checksum")
    for name in ("init_seconds", "kernel_seconds", "max_abs_error", "tolerance", "updates"):
        number(data[name], name, nonnegative=True)
    if data["updates"] != (size - 2) ** 2 * steps:
        raise ValueError("incorrect amount of work")
    if data["tolerance"] != TOLERANCE or data["max_abs_error"] > TOLERANCE:
        raise ValueError("numerical error exceeds the fixed declared 1e-9 reference tolerance")
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-dir", required=True, type=Path)
    parser.add_argument("--size", required=True, type=positive_int)
    parser.add_argument("--steps", required=True, type=positive_int)
    parser.add_argument("--wave", required=True, type=positive_int)
    parser.add_argument("--first-touch", required=True, choices=("parallel", "serial"))
    parser.add_argument("--threads", required=True, type=positive_int)
    args = parser.parse_args(argv)
    try:
        check_result(
            args.trial_dir,
            size=args.size,
            steps=args.steps,
            wave=args.wave,
            first_touch=args.first_touch,
            threads=args.threads,
        )
    except (OSError, ValueError, OverflowError) as exc:
        print(f"heat check failed: {exc}", file=sys.stderr)
        return 1
    print("heat check passed (fixed discrete-reference tolerance 1e-9)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
