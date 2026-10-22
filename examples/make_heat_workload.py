"""Create a new hashed heat manifest; never build, run, or refresh an experiment."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from check_heat_result import TOLERANCE, positive_int

from simtune.models import WorkloadSpec
from simtune.workloads import sha256_file


# The encoder handles only the strings, finite numbers, arrays and tables emitted
# by this example. Null fields are omitted with the schema's unavailable reasons.
def toml_document(data):
    lines = []

    def scalar(value):
        return json.dumps(value, ensure_ascii=True, allow_nan=False)

    def table(mapping, path=(), header=None):
        if header:
            lines.extend(["", header])
        for key, value in mapping.items():
            if value is None or isinstance(value, dict):
                continue
            if isinstance(value, list) and value and isinstance(value[0], dict):
                continue
            lines.append(f"{scalar(key)} = {scalar(value)}")
        for key, value in mapping.items():
            subpath = (*path, key)
            name = ".".join(scalar(part) for part in subpath)
            if isinstance(value, dict):
                table(value, subpath, f"[{name}]")
            elif isinstance(value, list) and value and isinstance(value[0], dict):
                for item in value:
                    table(item, subpath, f"[[{name}]]")

    table(data)
    return "\n".join(lines) + "\n"


def resolved_file(value, *, executable=False):
    path = value.resolve(strict=True)
    if not path.is_file() or (executable and not os.access(path, os.X_OK)):
        raise ValueError(f"expected {'executable ' if executable else ''}regular file: {path}")
    # Current strict templates reserve braces even in literal validator paths.
    if "{" in str(path) or "}" in str(path):
        raise ValueError("artifact paths must not contain reserved placeholder braces")
    return path


def make_manifest(args):
    if not 1 <= args.threads <= args.cpus:
        raise ValueError("require 1 <= threads <= cpus (no oversubscribed example)")
    if not 3 <= args.training_size <= 32768 or not 3 <= args.validation_size <= 32768:
        raise ValueError("sizes must be in 3..32768")
    if args.training_size == args.validation_size:
        raise ValueError(
            "training and held-out scientific identities must differ: choose distinct sizes"
        )
    if args.steps > 1000000 or args.wave >= min(args.training_size, args.validation_size) - 1:
        raise ValueError("require steps <= 1000000 and wave < each size - 1")
    if not args.runtime_version.strip():
        raise ValueError("supply the known runtime version from the actual build environment")
    binary = resolved_file(args.binary, executable=True)
    source = resolved_file(Path(__file__).with_name("heat2d.c"))
    validator = resolved_file(Path(__file__).with_name("check_heat_result.py"))
    library = resolved_file(args.runtime_library)
    python = resolved_file(Path(sys.executable), executable=True)
    artifacts = []
    for id_, role, path in (
        ("heat-source", "declared source; build correspondence is user supplied", source),
        ("heat-validator", "pinned command validator", validator),
        ("openmp-library", "declared runtime library; linkage not automatically verified", library),
        ("validator-python", "validator interpreter; dynamic dependencies not tracked", python),
    ):
        artifacts.append(dict(id=id_, role=role, path=str(path), sha256=sha256_file(path)))
    if args.build_record:
        record = resolved_file(args.build_record)
        artifacts.append(
            dict(
                id="build-record",
                role="user supplied compiler/flags provenance",
                path=str(record),
                sha256=sha256_file(record),
            )
        )
    cases = []
    for id_, size in (("training", args.training_size), ("heldout", args.validation_size)):
        check_argv = [
            str(python),
            str(validator),
            "--trial-dir",
            "{trial_dir}",
            "--size",
            str(size),
            "--steps",
            str(args.steps),
            "--wave",
            str(args.wave),
            "--first-touch",
            args.first_touch,
            "--threads",
            str(args.threads),
        ]
        cases.append(
            dict(
                kind="case_spec",
                schema_version=1,
                id=id_,
                input_class="heat-fixed-grid",
                arguments={"size": str(size)},
                input_artifacts=[],
                weight=1.0,
                regression_tolerance=0.0,
                checks=[
                    dict(
                        id="heat-result",
                        method="command",
                        argv=check_argv,
                        timeout_seconds=5.0,
                        rationale="Check fixed case metadata, actual threads and discrete-reference error.",
                        unavailable={
                            key: "command check uses explicit argv, not a numeric/hash field"
                            for key in (
                                "output",
                                "sha256",
                                "json_field",
                                "expected",
                                "absolute_tolerance",
                                "relative_tolerance",
                            )
                        },
                    )
                ],
                resource_needs=dict(
                    basis="resource_capped",
                    cpu_count=args.cpus,
                    thread_count=args.threads,
                    rationale="Common fixed CPU/thread budget for both sizes; resolve reference on target.",
                    unavailable={
                        "memory_bytes": "Not declared: grids alone exclude runtime overhead; binding requiring a need is excluded.",
                        "reference_cpu_ids": "Resolve deterministic resource-capped reference inside target inherited affinity.",
                    },
                ),
            )
        )
    data = dict(
        kind="workload_spec",
        schema_version=1,
        id="heat-checked",
        family="heat2d",
        argv_template=[
            str(binary),
            "--size",
            "{case.size}",
            "--steps",
            str(args.steps),
            "--wave",
            str(args.wave),
            "--first-touch",
            args.first_touch,
            "--output-dir",
            "{trial_dir}",
        ],
        resolved_executable=str(binary),
        executable_sha256=sha256_file(binary),
        artifacts=artifacts,
        base_directory=str(binary.parent),
        runtime=dict(
            name=args.runtime_name, version=args.runtime_version, identity=sha256_file(library)
        ),
        fixed_env={"OMP_DYNAMIC": "FALSE", "OMP_NUM_THREADS": str(args.threads)},
        case_substitutions=["size"],
        cases=cases,
        training_case_ids=["training"],
        validation_case_ids=["heldout"],
        output_checks=[],
        limits=dict(trial_timeout_seconds=60.0, max_trials=256, max_launches=512),
        extensions={
            "simtune.heat-example": dict(
                version=1,
                runtime_metadata="declared by user; library linkage/version not verified",
                build_provenance="Build explicitly once with fixed flags; helper hashes declared source and supplied binary, does not prove correspondence.",
                reference_tolerance=TOLERANCE,
                smoke_only=max(args.training_size, args.validation_size) <= 80,
                claim="No performance gain asserted; tiny inputs test correctness only.",
            )
        },
    )
    document = (
        "# Fresh declared identities: do not refresh this experiment's hashes.\n"
        + toml_document(data)
    )
    WorkloadSpec.from_toml(document)  # Existing strict schema, including null reasons.
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Example: python examples/make_heat_workload.py --binary build/heat2d --cpus 2 --threads 2 --training-size 64 --validation-size 80 --steps 3 --wave 1 --first-touch parallel --runtime-name libgomp --runtime-version VERSION --runtime-library /absolute/path/libgomp.so --out heat.toml. Inputs resolve from the invocation directory; output must be new. Install simtune first. Use known site runtime values; no discovery/build/solver is invoked.",
    )
    parser.add_argument("--binary", required=True, type=Path)
    for name in ("cpus", "threads", "training-size", "validation-size", "steps", "wave"):
        parser.add_argument(f"--{name}", required=True, type=positive_int)
    parser.add_argument("--first-touch", required=True, choices=("parallel", "serial"))
    parser.add_argument(
        "--runtime-name", required=True, choices=("libgomp", "gomp", "libomp", "openmp")
    )
    parser.add_argument("--runtime-version", required=True)
    parser.add_argument("--runtime-library", required=True, type=Path)
    parser.add_argument(
        "--build-record", type=Path, help="optional existing compiler/version/flags log to hash"
    )
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.out.suffix != ".toml":
            raise ValueError("--out must be a new .toml path")
        if args.out.exists() or args.out.is_symlink():
            raise ValueError("refusing overwrite; use a new manifest path for a new experiment")
        document = make_manifest(args)
        with args.out.open("x", encoding="utf-8") as stream:
            stream.write(document)
        print(f"Created {args.out.resolve()} (runtime/build metadata declared, not verified)")
    except (OSError, ValueError) as exc:
        parser.exit(2, f"heat manifest: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
