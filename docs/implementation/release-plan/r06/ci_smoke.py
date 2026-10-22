"""Installed-wheel Linux CI functional check, using the existing native gate.

Tiny heat inputs are correctness diagnostics, never performance samples. Requires
an existing GCC/OpenMP toolchain and more than two allowed CPUs; no tool install.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

from record_command import record

import simtune
from simtune.recipes import load_recipe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "linux" or "site-packages" not in Path(simtune.__file__).parts:
        parser.error("requires an installed wheel on actual Linux")
    original = sorted(os.sched_getaffinity(0))
    if len(original) <= 2:
        parser.error("requires more than two inherited CPUs; will use only two")
    kit, out = args.kit.resolve(), args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    python = Path(sys.executable)
    cli = python.parent / "simtune"
    overrides = {}

    def run(name, argv, timeout):
        metrics, raw = record(
            list(map(str, argv)),
            out / (name + "-command"),
            timeout,
            cwd=kit,
            overrides=overrides,
        )
        print(name, metrics["return_code"], flush=True)
        if metrics["return_code"] != 0 or metrics.get("timed_out"):
            print(raw)
            raise RuntimeError(f"{name} failed; preserve evidence, do not retry")
        return raw.split("STDOUT:\n", 1)[1].split("\nSTDERR:\n", 1)[0].strip()

    gcc, ldd = shutil.which("gcc"), shutil.which("ldd")
    assert gcc and ldd, "existing GCC and linkage inspector required"
    version = run("compiler-version", [gcc, "--version"], 5)
    binary = out / "heat2d"
    build = [gcc, "-O3", "-std=c11", "-fopenmp", kit / "examples/heat2d.c", "-lm", "-o", binary]
    run("build", build, 30)
    linkage = run("linkage", [ldd, binary], 5)
    match = re.search(r"libgomp\.so[^ ]*\s+=>\s+(\S+)", linkage)
    assert match, "loaded libgomp path not observed"
    library = Path(match[1]).resolve(strict=True)
    digest = hashlib.sha256(library.read_bytes()).hexdigest()
    (out / "heat-build.txt").write_text(
        version + "\n" + repr(list(map(str, build))) + "\n" + linkage
    )
    (out / "context.json").write_text(
        json.dumps(
            {
                "inherited_cpu_ids": original,
                "functional_cpu_ids": original[:2],
                "runtime_content_version": "sha256:" + digest,
                "inherited_runtime_environment": {
                    k: v for k, v in os.environ.items() if k.startswith(("OMP_", "KMP_", "GOMP_"))
                },
                "scope": "GitHub-hosted Linux functional checks; not a performance study",
            },
            indent=2,
        )
        + "\n"
    )
    tools = out / "tools"
    tools.mkdir()
    for name in ("taskset", "ps"):
        executable = shutil.which(name)
        assert executable, name
        (tools / name).symlink_to(Path(executable).resolve())
    overrides.update(
        PATH=str(tools),
        OMP_NUM_THREADS="2",
        OMP_DYNAMIC="FALSE",
        OMP_PLACES="cores",
        OMP_PROC_BIND="FALSE",
        OMP_DISPLAY_ENV="FALSE",
        OMP_DISPLAY_AFFINITY="FALSE",
    )
    workload = out / "heat.toml"
    run(
        "manifest",
        [
            python,
            kit / "examples/make_heat_workload.py",
            "--binary",
            binary,
            "--cpus",
            "2",
            "--threads",
            "2",
            "--training-size",
            "64",
            "--validation-size",
            "80",
            "--steps",
            "3",
            "--wave",
            "1",
            "--first-touch",
            "parallel",
            "--runtime-name",
            "libgomp",
            "--runtime-version",
            "sha256:" + digest,
            "--runtime-library",
            library,
            "--build-record",
            out / "heat-build.txt",
            "--out",
            workload,
        ],
        15,
    )
    gate = kit / "docs/implementation/release-plan/r06/native_gate.py"
    run(
        "native-basic",
        [python, gate, "--workload", workload, "--kit", kit, "--out", out / "basic"],
        180,
    )
    observed = {}
    for path in sorted((out / "basic").glob("*/heat.txt")):
        masks = re.findall(r"\baffinity ([0-9,-]+)", path.read_text())
        assert len(masks) >= 2, f"missing OpenMP worker diagnostics: {path}"
        for mask in masks:
            cpus = set()
            for part in mask.split(","):
                endpoints = list(map(int, part.split("-")))
                cpus.update(range(endpoints[0], endpoints[-1] + 1))
            assert cpus and cpus <= set(original[:2]), masks
        observed[path.parent.name] = masks
    assert observed, "no worker affinity observations"
    (out / "worker-affinity.json").write_text(json.dumps(observed, indent=2) + "\n")
    restricted = [tools / "taskset", "-c", "--", ",".join(map(str, original[:2]))]
    tune = [
        cli,
        "tune",
        "--workload",
        workload,
        "--budget",
        "30s",
        "--repeat",
        "1",
        "--warmups",
        "0",
        "--seed",
        "2718",
        "--collectors",
        "timing",
        "--out",
        out / "search",
        "--save",
        out / "heat.recipe.json",
        "--json",
    ]
    dry = json.loads(run("dry", [*restricted, *tune, "--dry-run"], 15))
    assert dry["recipe_path"] is None and not (out / "search").exists()
    result = json.loads(run("tune", [*restricted, *tune], 40))
    assert result["recipe_export"]["status"] == "saved"
    load_recipe(out / "heat.recipe.json")
    run(
        "validate",
        [
            *restricted,
            cli,
            "validate",
            "--recipe",
            out / "heat.recipe.json",
            "--workload",
            workload,
            "--budget",
            "60s",
            "--out",
            out / "validation",
        ],
        70,
    )
    assert load_recipe(out / "validation.recipe.json").details["replay_validated"]
    run(
        "native-replay",
        [
            python,
            gate,
            "--workload",
            workload,
            "--kit",
            kit,
            "--recipe",
            out / "validation.recipe.json",
            "--out",
            out / "replay",
        ],
        40,
    )
    assert sorted(os.sched_getaffinity(0)) == original
    (out / "completed.json").write_text(
        json.dumps({"state": "passed", "performance_claim": False}) + "\n"
    )
    print("Checked native CLI workflow passed; no performance claim.")


if __name__ == "__main__":
    main()
