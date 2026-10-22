"""Prepared Linux functional gate. No performance study or synthetic host provider."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

import simtune
from simtune.candidates import generate_candidates
from simtune.checks import run_checks
from simtune.experiments import ExperimentStore
from simtune.launch import (
    StrictPlanningError,
    execute_strict_launch,
    plan_strict_launch,
)
from simtune.models import CheckKind, NumaPolicy, OutputCheck
from simtune.replay import exec_recipe
from simtune.search import resume_search, search_capabilities, search_snapshot
from simtune.validation import ValidationError
from simtune.workloads import expand_trial_argv, load_workload


def write(path, data):
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")


def cancellation_gate(root, kit, heat):
    # Reuse existing harmless readiness/liveness helpers, replacing their fixture
    # resource intent with the declared budget. Never use their synthetic hosts.
    sys.path.insert(0, str(kit / "tests"))
    from process_helpers import cleanup, living, wait_for

    for phase in ("workload", "validator", "uncertain"):
        directory = root / phase
        directory.mkdir()
        driver = f"""import sys
from pathlib import Path
sys.path.insert(0, {str(kit / "tests")!r})
from process_helpers import cancellation_workload
from dataclasses import replace
from simtune import util
from simtune.workloads import load_workload
from simtune.search import SearchConfig,run_search,search_snapshot
root=Path({str(directory)!r})
heat=load_workload({str(heat.manifest_path)!r})
workload,options=cancellation_workload(root,{"workload" if phase == "uncertain" else phase!r})
spec=replace(workload.spec,runtime=heat.spec.runtime,
    cases=[replace(c,resource_needs=heat.spec.cases[0].resource_needs) for c in workload.spec.cases])
workload.manifest_path.write_text(spec.to_json())
workload=load_workload(workload.manifest_path)
assert search_snapshot().platform.value=='linux'
with util.cancellation_scope() as state:
    result=run_search(workload,root/'search',SearchConfig(30,repeat=1,warmups=0),options=options)
    signum=state.signum
assert result.outcome.value=='interrupted'
sys.exit(128+signum)
"""
        (directory / "driver.py").write_text(driver)
        command = [sys.executable, str(directory / "driver.py")]
        write(directory / "driver-command.json", {"argv": command, "phase": phase})
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            wait_for(lambda: (directory / "ready.json").exists(), proc)
            ready = json.loads((directory / "ready.json").read_text())
            proc.send_signal(signal.SIGKILL if phase == "uncertain" else signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=8)
            (directory / "stdout.txt").write_bytes(stdout)
            (directory / "stderr.txt").write_bytes(stderr)
            status = proc.returncode
            write(directory / "process-result.json", {"status": status, "ready": ready})
            if phase == "uncertain":
                assert status == -signal.SIGKILL
            else:
                assert status == 143, (status, stdout, stderr)
                wait_for(lambda: not living(ready["pid"]), seconds=3)
                assert not (directory / "completion").exists()
            verify_resume(directory, uncertain=phase == "uncertain")
        except subprocess.TimeoutExpired as exc:
            (directory / "stdout.txt").write_bytes(exc.stdout or b"")
            (directory / "stderr.txt").write_bytes(exc.stderr or b"")
            write(directory / "process-result.json", {"state": "timeout", "partial_output": True})
            raise
        finally:
            cleanup(proc, directory)


def verify_resume(directory, *, uncertain):
    store = ExperimentStore.open(directory / "search")
    states = [s.value for s in store.recover().states.values()]
    assert ("uncertain" if uncertain else "interrupted") in states
    before = (directory / "launches").read_bytes()
    try:
        resume_search(directory / "search", additional_seconds=1)
    except ValidationError as exc:
        write(directory / "resume-refused.json", {"reason": str(exc), "states": states})
    else:
        raise AssertionError("resume accepted interrupted/uncertain work")
    assert before == (directory / "launches").read_bytes()


def basic_gate(root, workload, caps):
    snapshot = search_snapshot()
    write(root / "resources.json", snapshot.to_dict())
    candidates = generate_candidates(
        snapshot,
        caps,
        runtime=workload.spec.runtime,
        budget=workload.spec.cases[0].resource_needs,
    )
    write(root / "candidate-ledger.json", [asdict(c) for c in candidates.ledger])
    assert candidates.plans and candidates.plans[0].policy.id == "baseline"
    for plan in candidates.plans:
        assert plan.policy.numa_policy == NumaPolicy.INHERITED
        directory = root / plan.policy.id
        directory.mkdir()
        write(directory / "plan.json", plan.to_dict())
        # All launches here are diagnostics, including the heat elapsed fields.
        child = execute_strict_launch(
            plan,
            [
                sys.executable,
                "-c",
                "import json,os; print(json.dumps(sorted(os.sched_getaffinity(0))))",
            ],
            caps,
            10,
            snapshot_provider=search_snapshot,
            cwd=directory,
        )
        write(directory / "child-metrics.json", child.metrics)
        (directory / "child.txt").write_text(child.raw_output)
        assert child.metrics["return_code"] == 0
        observed = json.loads(child.raw_output.split("STDOUT:\n", 1)[1].splitlines()[0])
        assert set(observed) == set(plan.cpu_ids) <= set(os.sched_getaffinity(0))
        argv = expand_trial_argv(workload.spec, workload.case("training"), directory)
        result = execute_strict_launch(
            plan,
            argv,
            caps,
            10,
            snapshot_provider=search_snapshot,
            cwd=directory,
            base_env={
                **os.environ,
                **workload.spec.fixed_env,
                "OMP_DISPLAY_ENV": "VERBOSE",
                "OMP_DISPLAY_AFFINITY": "TRUE",
            },
        )
        write(directory / "heat-metrics.json", result.metrics)
        (directory / "heat.txt").write_text(result.raw_output)
        assert result.metrics["return_code"] == 0
        checks = workload.case("training").checks
        checked = run_checks(
            checks,
            directory,
            directory / "checks",
            evidence_prefix="checks",
            base_env=os.environ,
            deadline=time.monotonic() + 10,
            clock=time.monotonic,
        )
        assert checked.result.state.value == "passed"
        checksum = json.loads((directory / "heat-result.json").read_text())["checksum"]
        wrong_reference = OutputCheck(
            id="wrong-checksum-reference",
            method=CheckKind.NUMERIC_JSON,
            output="heat-result.json",
            sha256=None,
            json_field=["checksum"],
            expected=checksum + abs(checksum) * 1e-6 + 1,
            absolute_tolerance=0.0,
            relative_tolerance=0.0,
            argv=None,
            timeout_seconds=None,
            rationale="Deliberately wrong declared numerical reference.",
            unavailable={
                name: "numeric reference check" for name in ("sha256", "argv", "timeout_seconds")
            },
        )
        write(directory / "wrong-numerical-reference.json", wrong_reference.to_dict())
        negative = run_checks(
            [wrong_reference],
            directory,
            directory / "wrong-numerical-reference-check",
            evidence_prefix="wrong-numerical-reference-check",
            base_env=os.environ,
            deadline=time.monotonic() + 10,
            clock=time.monotonic,
        )
        assert negative.result.state.value == "failed"
        wrong_argv = list(checks[0].argv)
        index = wrong_argv.index("--steps") + 1
        wrong_argv[index] = str(int(wrong_argv[index]) + 1)
        wrong = run_checks(
            [replace(checks[0], argv=wrong_argv)],
            directory,
            directory / "wrong-reference",
            evidence_prefix="wrong-reference",
            base_env=os.environ,
            deadline=time.monotonic() + 10,
            clock=time.monotonic,
        )
        assert wrong.result.state.value == "failed"
    try:
        plan_strict_launch(
            replace(candidates.reference, numa_policy=NumaPolicy.LOCAL), snapshot, caps
        )
    except StrictPlanningError as exc:
        assert exc.code == "missing_capability", exc
        write(root / "required-numactl-refused.json", {**exc.to_dict(), "launches": 0})
    else:
        raise AssertionError("missing required numactl was accepted")


def replay_gate(root, workload, recipe):
    directory = root / "replay-workspace"
    directory.mkdir()
    argv = expand_trial_argv(workload.spec, workload.case("heldout"), directory)
    result = exec_recipe(
        recipe,
        argv,
        workload_path=workload.manifest_path,
        cwd=directory,
        out=root / "replay-record",
        timeout=10,
    )
    assert result.status == 0 and result.record["launches"] == 1
    assert result.record["current_output_validation"] == "unchecked"
    # A changed declared binary/runtime identity is rejected with no log root.
    for name in ("binary", "runtime"):
        spec = workload.spec
        if name == "binary":
            spec = replace(spec, executable_sha256="0" * 64)
        else:
            spec = replace(
                spec,
                artifacts=[
                    replace(a, sha256="0" * 64) if a.id == "openmp-library" else a
                    for a in spec.artifacts
                ],
            )
        path = root / (name + ".json")
        path.write_text(spec.to_json())
        rejected_replay(root, name, recipe, argv, directory, path)
    before = set(os.sched_getaffinity(0))
    try:
        os.sched_setaffinity(0, {min(before)})
        rejected_replay(root, "resources", recipe, argv, directory, workload.manifest_path)
    finally:
        os.sched_setaffinity(0, before)


def rejected_replay(root, name, recipe, argv, directory, workload):
    output = root / (name + "-replay")
    try:
        exec_recipe(recipe, argv, workload_path=workload, cwd=directory, out=output, timeout=10)
    except (ValidationError, StrictPlanningError) as exc:
        assert not output.exists()
        write(root / (name + "-refused.json"), {"reason": str(exc), "launches": 0})
    else:
        raise AssertionError("incompatible replay launched")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", type=Path, required=True)
    parser.add_argument("--kit", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--recipe", type=Path)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("native Linux required; no synthetic fallback")
    if "site-packages" not in Path(simtune.__file__).parts:
        parser.error("run the installed package outside the source checkout")
    if shutil.which("perf") or shutil.which("numactl"):
        parser.error("use the reviewed PATH excluding perf and numactl")
    workload = load_workload(args.workload)
    cpus = workload.spec.cases[0].resource_needs.cpu_count
    original = sorted(os.sched_getaffinity(0))
    if cpus != 2 or len(original) <= cpus:
        parser.error("gate requires a two-CPU manifest and >2 permitted inherited CPUs")
    root = args.out.resolve()
    root.mkdir(parents=True, exist_ok=False)
    write(root / "inherited-before.json", {"cpu_ids": original, "simtune": simtune.__file__})
    os.sched_setaffinity(0, set(original[:cpus]))
    write(root / "narrowed.json", {"cpu_ids": sorted(os.sched_getaffinity(0))})
    caps = search_capabilities(search_snapshot())
    if args.recipe:
        replay_gate(root, workload, args.recipe)
    else:
        basic_gate(root, workload, caps)
        cancellation_gate(root, args.kit.resolve(), workload)
    write(root / "completed.json", {"state": "functional_script_passed", "study_samples": False})
    print("Functional checks passed; independently review OpenMP worker diagnostics.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
