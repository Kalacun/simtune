"""Actual Linux-only strict subprocess checks, using this process's real boundary."""

import json
import os
import sys

import pytest
from test_heat_workflow import generate
from test_heat_workflow import openmp_heat as _openmp_heat

from simtune.candidates import generate_candidates
from simtune.checks import run_checks
from simtune.launch import StrictPlanningError, execute_strict_launch, plan_strict_launch
from simtune.search import search_capabilities, search_snapshot
from simtune.workloads import expand_trial_argv

openmp_heat = _openmp_heat

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="actual Linux kernel required")


def native_plan(workload):
    snapshot = search_snapshot()
    capabilities = search_capabilities(snapshot)
    if snapshot.inherited_cpu_ids is None or len(snapshot.inherited_cpu_ids) < 2:
        pytest.skip("native OpenMP gate needs an observed inherited mask with at least two CPUs")
    # The CPU envelope and class are actual discovery, with no injected host fixture.
    candidates = generate_candidates(
        snapshot,
        capabilities,
        runtime=workload.spec.runtime,
        budget=workload.spec.cases[0].resource_needs,
    )
    try:
        plan = plan_strict_launch(candidates.reference, snapshot, capabilities)
    except StrictPlanningError as exc:
        pytest.skip(f"native strict reference unsupported in this runner: {exc}")
    return plan, capabilities


def test_native_linux_strict_heat(openmp_heat, tmp_path):
    import time

    # Declared library fixture is not a linkage claim; real compiler numerical
    # tests separately check the OpenMP team. The launch snapshot is real Linux.
    workload, _ = generate(tmp_path, openmp_heat)
    plan, capabilities = native_plan(workload)
    workspace = tmp_path / "native trial"
    workspace.mkdir()
    argv = expand_trial_argv(workload.spec, workload.spec.cases[0], workspace)
    result = execute_strict_launch(
        plan,
        argv,
        capabilities,
        10,
        snapshot_provider=search_snapshot,
        base_env={**os.environ, **workload.spec.fixed_env},
        cwd=workspace,
    )
    assert result.metrics["return_code"] == 0, result.raw_output
    checked = run_checks(
        workload.spec.cases[0].checks,
        workspace,
        tmp_path / "checks",
        evidence_prefix="checks",
        base_env=os.environ,
        deadline=None,
        clock=time.monotonic,
    )
    assert checked.result.state.value == "passed"
    # Observe the child mask independently from the recorded planned action.
    result = execute_strict_launch(
        plan,
        [
            sys.executable,
            "-c",
            "import json,os,sys; print(json.dumps([sorted(os.sched_getaffinity(0)),sys.argv[1:]]))",
            "$HOME",
            "space argument",
        ],
        capabilities,
        10,
        snapshot_provider=search_snapshot,
        cwd=workspace,
    )
    assert result.metrics["return_code"] == 0
    observed = json.loads(result.raw_output.split("STDOUT:\n", 1)[1].split("\n", 1)[0])
    assert observed == [sorted(plan.cpu_ids), ["$HOME", "space argument"]]
