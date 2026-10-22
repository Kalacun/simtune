"""One diagnostic launch per arm/case/policy, using existing minimal timing APIs."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import simtune
from simtune.candidates import generate_candidates
from simtune.checks import run_checks
from simtune.launch import execute_strict_launch
from simtune.profiler import execute_command
from simtune.recipes import load_recipe
from simtune.replay import resolve_recipe
from simtune.search import search_capabilities, search_snapshot
from simtune.workloads import expand_trial_argv, load_workload


def write(path, data):
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", type=Path, required=True)
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "linux" or "site-packages" not in Path(simtune.__file__).parts:
        parser.error("run installed simtune on the authorized Linux target")
    if len(os.sched_getaffinity(0)) != 2:
        parser.error("use the frozen two-CPU mask")
    workload = load_workload(args.workload)
    bundle = load_recipe(args.recipe)
    if not bundle.details["replay_validated"]:
        parser.error("historical confirmation required; no override in this protocol")
    root = args.out.resolve()
    root.mkdir(parents=True, exist_ok=False)
    snapshot = search_snapshot()
    caps = search_capabilities(snapshot)
    reference = generate_candidates(
        snapshot,
        caps,
        runtime=workload.spec.runtime,
        budget=workload.spec.cases[0].resource_needs,
    ).plans[0]
    selected = resolve_recipe(bundle, snapshot, caps, workload=workload.spec, cwd=root)
    write(root / "resources.json", snapshot.to_dict())
    for name, plan in (("reference", reference), ("selected", selected)):
        assert not plan.rlimits and plan.policy.numa_policy.value == "inherited"
        directory = root / name
        directory.mkdir()
        write(directory / "plan.json", plan.to_dict())
        child = execute_strict_launch(
            plan,
            [
                sys.executable,
                "-c",
                "import json,os; print(json.dumps(sorted(os.sched_getaffinity(0))))",
            ],
            caps,
            10,
            cwd=directory,
            snapshot_provider=search_snapshot,
        )
        write(directory / "child-metrics.json", child.metrics)
        (directory / "child.txt").write_text(child.raw_output)
        assert child.metrics["return_code"] == 0
        mask = json.loads(child.raw_output.split("STDOUT:\n", 1)[1].splitlines()[0])
        assert set(mask) == set(plan.cpu_ids) <= set(os.sched_getaffinity(0))
        for case in workload.spec.cases:
            for arm in ("simtune-minimal", "direct"):
                cwd = directory / case.id / arm
                cwd.mkdir(parents=True, exist_ok=False)
                argv = expand_trial_argv(workload.spec, case, cwd)
                final_argv = [arg for wrapper in plan.wrappers for arg in wrapper] + argv
                overrides = {**workload.spec.fixed_env, **dict(plan.env_overrides)}
                env = {**os.environ, **overrides}
                write(
                    cwd / "intent.json",
                    {
                        "argv": final_argv,
                        "cwd": str(cwd),
                        "env_overrides": overrides,
                        "timeout_seconds": 60,
                        "state": "running",
                        "classification": "single launch-equivalence diagnostic; not a study pair",
                    },
                )
                if arm == "simtune-minimal":
                    result = execute_strict_launch(
                        plan,
                        argv,
                        caps,
                        60,
                        cwd=cwd,
                        snapshot_provider=search_snapshot,
                        base_env=env,
                    )
                    assert list(result.final_argv) == final_argv
                    metrics, raw = result.metrics, result.raw_output
                else:
                    metrics, raw = execute_command(final_argv, env, 60, cwd=cwd)
                write(cwd / "metrics.json", metrics)
                (cwd / "stdout-stderr.txt").write_text(raw)
                assert metrics["return_code"] == 0 and not metrics["timed_out"]
                assert metrics["profiler"]["used_perf"] is False
                assert metrics["profiler"]["used_time"] is False
                checked = run_checks(
                    [*workload.spec.output_checks, *case.checks],
                    cwd,
                    cwd / "checks",
                    evidence_prefix="checks",
                    base_env=env,
                    deadline=time.monotonic() + 10,
                    clock=time.monotonic,
                )
                assert checked.result.state.value == "passed"
    write(
        root / "completed.json",
        {
            "state": "diagnostics_complete",
            "performance_claim": None,
            "reason": "No paired inference or overhead estimate from single diagnostics",
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
