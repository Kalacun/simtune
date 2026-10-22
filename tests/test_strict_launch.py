import json
import os
import resource
import signal
import stat
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from simtune import launch, profiler, runner
from simtune.models import (
    BudgetBasis,
    CpuTopology,
    NumaNode,
    NumaPolicy,
    Placement,
    PolicySpec,
    ResourceSnapshot,
    SettingSpec,
    Target,
)

FIXTURES = Path(__file__).parent / "fixtures/extension/valid"


@pytest.fixture
def snapshot():
    return ResourceSnapshot.from_json((FIXTURES / "resource.json").read_text())


@pytest.fixture
def policy():
    return PolicySpec.from_json((FIXTURES / "policy.json").read_text())


@pytest.fixture
def strict_caps():
    return {
        "platform": "linux",
        "is_wsl": False,
        "tools": {
            "taskset": {"path": "/usr/bin/taskset", "available": True, "version": "test"},
            "numactl": {"path": "/usr/bin/numactl", "available": True, "version": "test"},
            "time": {"path": "/usr/bin/time", "functional": True},
            "perf": {"path": "/usr/bin/perf", "available": True},
        },
        "perf": {"functional": True},
    }


def inherited(policy):
    return replace(policy, placement=Placement.INHERITED)


def test_plan_is_serializable_and_resolves_openmp(policy, snapshot, strict_caps):
    plan = launch.plan_strict_launch(policy, snapshot, strict_caps)
    data = json.loads(plan.to_json())
    assert data["kind"] == "strict_launch_plan"
    assert data["policy"]["id"] == "spread"
    assert data["cpu_ids"] == [2, 6]
    assert data["wrappers"] == [["/usr/bin/taskset", "-c", "--", "2,6"]]
    assert data["env_overrides"] == {"OMP_NUM_THREADS": "2", "OMP_PROC_BIND": "spread"}
    assert all(action["status"] == "planned" for action in data["actions"])
    assert any("page residency" in item for item in data["unobserved"])


def test_missing_required_tool_fails_before_any_launch(policy, snapshot, strict_caps, monkeypatch):
    strict_caps["tools"]["taskset"] = {"path": None, "available": False}
    monkeypatch.setattr(profiler, "execute_command", lambda *a, **k: pytest.fail("launched"))
    with pytest.raises(launch.StrictPlanningError) as exc:
        launch.plan_strict_launch(policy, snapshot, strict_caps)
    assert exc.value.code == "missing_capability"
    assert exc.value.setting == "placement"
    assert "manual run path" in exc.value.remedy


@pytest.mark.parametrize(
    "changed,code",
    [
        ({"inherited_cpu_ids": [], "effective_cpu_ids": []}, "empty_restriction"),
        ({"effective_cpu_ids": None}, "unknown_restriction"),
        ({"conflicts": ["synthetic boundary disagreement"]}, "resource_conflict"),
    ],
)
def test_invalid_cpu_boundaries_are_fail_closed(
    policy, snapshot, strict_caps, changed, code, monkeypatch
):
    unavailable = dict(snapshot.unavailable)
    if changed.get("effective_cpu_ids", object()) is None:
        unavailable["effective_cpu_ids"] = "synthetic unknown boundary"
        changed["unavailable"] = unavailable
    bad = replace(snapshot, **changed)
    monkeypatch.setattr(profiler, "execute_command", lambda *a, **k: pytest.fail("launched"))
    with pytest.raises(launch.StrictPlanningError) as exc:
        launch.plan_strict_launch(policy, bad, strict_caps)
    assert exc.value.code == code


def test_cpu_budget_cannot_widen_effective_mask(policy, snapshot, strict_caps):
    too_large = replace(policy.budget, cpu_count=3, reference_cpu_ids=[2, 6, 10])
    widened = replace(policy, budget=too_large)
    with pytest.raises(launch.StrictPlanningError) as exc:
        launch.plan_strict_launch(widened, snapshot, strict_caps)
    assert exc.value.code == "budget_exceeds_restriction"
    assert "[2, 6]" in exc.value.restriction


def test_spread_uses_distinct_sparse_sockets_before_smt(policy, snapshot, strict_caps):
    cpus = [
        CpuTopology(
            cpu_id=2,
            socket_id=0,
            core_id=0,
            smt_siblings=[2, 3],
            numa_node_id=0,
        ),
        CpuTopology(
            cpu_id=3,
            socket_id=0,
            core_id=0,
            smt_siblings=[2, 3],
            numa_node_id=0,
        ),
        CpuTopology(
            cpu_id=10,
            socket_id=1,
            core_id=0,
            smt_siblings=[10, 11],
            numa_node_id=1,
        ),
        CpuTopology(
            cpu_id=11,
            socket_id=1,
            core_id=0,
            smt_siblings=[10, 11],
            numa_node_id=1,
        ),
    ]
    node = snapshot.numa_nodes[0]
    nodes = [
        replace(node, node_id=0, cpu_ids=[2, 3], distances={"0": 10, "1": 20}),
        NumaNode(
            node_id=1,
            cpu_ids=[10, 11],
            memory_bytes=None,
            distances={"0": 20, "1": 10},
            unavailable={"memory_bytes": "synthetic capacity is unknown"},
        ),
    ]
    sparse = replace(
        snapshot,
        online_cpu_ids=[2, 3, 10, 11],
        inherited_cpu_ids=[2, 3, 10, 11],
        effective_cpu_ids=[2, 3, 10, 11],
        cpus=cpus,
        numa_nodes=nodes,
        allowed_memory_node_ids=[0, 1],
    )
    plan = launch.plan_strict_launch(policy, sparse, strict_caps)
    assert plan.cpu_ids == (2, 10)
    assert plan.wrappers[0][-1] == "2,10"


def test_inherited_policy_cannot_hide_a_smaller_cpu_budget(policy, snapshot, strict_caps):
    smaller = replace(
        policy.budget,
        basis=BudgetBasis.RESOURCE_CAPPED,
        cpu_count=1,
        reference_cpu_ids=[2],
    )
    requested = replace(policy, placement=Placement.INHERITED, budget=smaller)
    with pytest.raises(launch.StrictPlanningError) as exc:
        launch.plan_strict_launch(requested, snapshot, strict_caps)
    assert exc.value.code == "semantic_adjustment"


def test_numa_binding_uses_only_allowed_nodes(policy, snapshot, strict_caps):
    bound = replace(policy, numa_policy=NumaPolicy.BIND, numa_node_count=1, unavailable={})
    plan = launch.plan_strict_launch(bound, snapshot, strict_caps)
    assert plan.numa_node_ids == (0,)
    assert plan.wrappers == (
        ("/usr/bin/taskset", "-c", "--", "2,6"),
        ("/usr/bin/numactl", "--membind=0", "--"),
    )
    assert not any("physcpubind" in arg for wrapper in plan.wrappers for arg in wrapper)


def test_missing_numactl_does_not_become_a_named_numa_run(
    policy, snapshot, strict_caps, monkeypatch
):
    strict_caps["tools"]["numactl"] = {"path": None, "available": False}
    bound = replace(policy, numa_policy=NumaPolicy.BIND, numa_node_count=1, unavailable={})
    monkeypatch.setattr(profiler, "execute_command", lambda *a, **k: pytest.fail("launched"))
    with pytest.raises(launch.StrictPlanningError) as exc:
        launch.plan_strict_launch(bound, snapshot, strict_caps)
    assert exc.value.code == "missing_capability"
    assert exc.value.setting == "numa_policy"


def test_manual_fallback_still_executes_and_records_inherited_name(strict_caps, tmp_path):
    strict_caps["tools"]["numactl"] = {"path": None, "available": False}
    strict_caps["tools"]["time"] = {"path": None, "functional": False}
    strict_caps["perf"] = {"functional": False}
    marker = tmp_path / "ran"
    profiles = {
        "numa_candidate": {
            "actions": [{"type": "numactl", "membind": "0"}],
            "fallback_mode": "inherited",
        },
        "inherited": {"actions": []},
    }
    run_dir = runner.run_command(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path(__import__('sys').argv[1]).touch()",
            str(marker),
        ],
        str(tmp_path / "runs"),
        "numa_candidate",
        profiles,
        strict_caps,
        2,
        1,
        False,
        False,
        False,
        False,
    )
    assert marker.is_file()
    meta = json.loads((Path(run_dir) / "meta.json").read_text())
    assert meta["requested_mode"] == "numa_candidate"
    assert meta["mode"] == "inherited"


def test_numa_cannot_use_disallowed_or_unknown_nodes(policy, snapshot, strict_caps):
    bound = replace(policy, numa_policy=NumaPolicy.BIND, numa_node_count=1, unavailable={})
    with pytest.raises(launch.StrictPlanningError, match="empty_restriction"):
        launch.plan_strict_launch(bound, replace(snapshot, allowed_memory_node_ids=[]), strict_caps)
    unavailable = dict(snapshot.unavailable)
    unavailable["allowed_memory_node_ids"] = "synthetic unknown"
    with pytest.raises(launch.StrictPlanningError, match="unknown_restriction"):
        launch.plan_strict_launch(
            bound,
            replace(snapshot, allowed_memory_node_ids=None, unavailable=unavailable),
            strict_caps,
        )


def test_raw_or_conflicting_numa_setting_is_rejected(policy, snapshot, strict_caps):
    raw = SettingSpec(
        id="raw-numa",
        target=Target.NUMA_POLICY,
        name="args",
        value="--interleave=all --physcpubind=0-99",
        unit="argv",
        required=True,
        rationale="synthetic invalid raw option",
        expected_effect="none",
    )
    requested = replace(policy, settings=[*policy.settings, raw])
    with pytest.raises(launch.StrictPlanningError) as exc:
        launch.plan_strict_launch(requested, snapshot, strict_caps)
    assert exc.value.code == "conflicting_setting"


def test_unsupported_runtime_setting_required_fails_optional_is_recorded(
    policy, snapshot, strict_caps
):
    unsupported = SettingSpec(
        id="unknown-runtime-control",
        target=Target.RUNTIME,
        name="MAGIC_THREADS",
        value="2",
        unit="threads",
        required=True,
        rationale="synthetic unsupported setting",
        expected_effect="none",
    )
    requested = replace(policy, settings=[unsupported])
    with pytest.raises(launch.StrictPlanningError) as exc:
        launch.plan_strict_launch(requested, snapshot, strict_caps)
    assert exc.value.code == "unsupported_setting"
    optional = replace(unsupported, required=False)
    plan = launch.plan_strict_launch(replace(policy, settings=[optional]), snapshot, strict_caps)
    assert any(action.status.value == "skipped" for action in plan.actions)


def test_unsupported_runtime_cannot_claim_thread_budget(policy, snapshot, strict_caps):
    runtime = replace(policy.runtime, name="synthetic-python")
    with pytest.raises(launch.StrictPlanningError) as exc:
        launch.plan_strict_launch(
            replace(policy, runtime=runtime, settings=[]), snapshot, strict_caps
        )
    assert exc.value.code == "unsupported_runtime"


def test_hard_limit_increase_is_rejected_without_parent_side_effect(policy, snapshot, strict_caps):
    setting = SettingSpec(
        id="nofile-cap",
        target=Target.PROCESS_LIMIT,
        name="nofile",
        value=[100, 201],
        unit="descriptors",
        required=True,
        rationale="synthetic child limit",
        expected_effect="none",
    )
    before = resource.getrlimit(resource.RLIMIT_NOFILE)
    with pytest.raises(launch.StrictPlanningError) as exc:
        launch.plan_strict_launch(
            replace(policy, settings=[setting]),
            snapshot,
            strict_caps,
            limit_reader=lambda _: (100, 200),
        )
    assert exc.value.code == "hard_limit_increase"
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == before


def test_process_limit_applies_only_in_child(policy, snapshot, strict_caps):
    before = resource.getrlimit(resource.RLIMIT_NOFILE)
    child_hard = 64 if before[1] == resource.RLIM_INFINITY else min(64, before[1])
    setting = SettingSpec(
        id="nofile-child-cap",
        target=Target.PROCESS_LIMIT,
        name="nofile",
        value=[child_hard, child_hard],
        unit="descriptors",
        required=True,
        rationale="synthetic child-only limit",
        expected_effect="none",
    )
    plan = launch.plan_strict_launch(
        replace(inherited(policy), settings=[setting]), snapshot, strict_caps
    )
    result = launch.execute_strict_launch(
        plan,
        [
            sys.executable,
            "-c",
            "import resource; print(resource.getrlimit(resource.RLIMIT_NOFILE)[1])",
        ],
        strict_caps,
        2,
        snapshot_provider=lambda: snapshot,
    )
    assert result.metrics["return_code"] == 0
    assert f"\n{child_hard}\n" in result.raw_output
    assert resource.getrlimit(resource.RLIMIT_NOFILE) == before


def test_literal_argv_and_child_env_are_preserved(policy, snapshot, strict_caps, monkeypatch):
    captured = {}

    def execute(argv, env, timeout, preexec_fn=None):
        captured.update(argv=argv, env=env, timeout=timeout, preexec=preexec_fn)
        return {"return_code": 0, "timed_out": False}, "ok"

    monkeypatch.setattr(profiler, "execute_command", execute)
    monkeypatch.setenv("SIMTUNE_PARENT_ONLY", "present")
    argv = ["program", "a b", "$HOME", "--", "α", "line\nbreak"]
    plan = launch.plan_strict_launch(inherited(policy), snapshot, strict_caps)
    result = launch.execute_strict_launch(
        plan,
        argv,
        strict_caps,
        5,
        snapshot_provider=lambda: snapshot,
    )
    assert captured["argv"] == argv
    assert captured["env"]["OMP_PROC_BIND"] == "spread"
    assert captured["env"]["SIMTUNE_PARENT_ONLY"] == "present"
    assert os.environ["SIMTUNE_PARENT_ONLY"] == "present"
    assert result.final_argv == tuple(argv)


def test_stale_preflight_resolution_launches_zero_times(policy, snapshot, strict_caps, monkeypatch):
    calls = []
    monkeypatch.setattr(profiler, "execute_command", lambda *a, **k: calls.append(a))
    plan = launch.plan_strict_launch(policy, snapshot, strict_caps)
    cpu_2, cpu_6 = snapshot.cpus
    node_0 = snapshot.numa_nodes[0]
    changed = replace(
        snapshot,
        online_cpu_ids=[2, 10],
        inherited_cpu_ids=[2, 10],
        effective_cpu_ids=[2, 10],
        cpus=[
            cpu_2,
            replace(cpu_6, cpu_id=10, smt_siblings=[10]),
        ],
        numa_nodes=[replace(node_0, cpu_ids=[2, 10])],
    )
    with pytest.raises(launch.StrictPlanningError) as exc:
        launch.execute_strict_launch(
            plan, ["unused"], strict_caps, 1, snapshot_provider=lambda: changed
        )
    assert exc.value.code == "resource_changed"
    assert calls == []


def test_minimal_launch_does_not_add_profiler_wrappers(policy, snapshot, strict_caps, monkeypatch):
    captured = []

    def execute(argv, *args, **kwargs):
        captured.append(argv)
        return {"return_code": 0, "timed_out": False}, ""

    monkeypatch.setattr(profiler, "execute_command", execute)
    monkeypatch.setattr(profiler, "profile_command", lambda *a, **k: pytest.fail("profiled"))
    plan = launch.plan_strict_launch(inherited(policy), snapshot, strict_caps)
    launch.execute_strict_launch(
        plan,
        ["workload"],
        strict_caps,
        1,
        snapshot_provider=lambda: snapshot,
        with_profiling=False,
    )
    assert captured == [["workload"]]


def test_start_and_permission_failures_are_returned_once(policy, snapshot, strict_caps, tmp_path):
    plan = launch.plan_strict_launch(inherited(policy), snapshot, strict_caps)
    missing = launch.execute_strict_launch(
        plan,
        [str(tmp_path / "missing")],
        strict_caps,
        2,
        snapshot_provider=lambda: snapshot,
    )
    assert missing.metrics["return_code"] == 127 and "could not launch" in missing.raw_output

    denied = tmp_path / "not-executable"
    denied.write_text("#!/bin/sh\nexit 0\n")
    denied.chmod(stat.S_IRUSR | stat.S_IWUSR)
    result = launch.execute_strict_launch(
        plan, [str(denied)], strict_caps, 2, snapshot_provider=lambda: snapshot
    )
    assert result.metrics["return_code"] == 126


def test_wrapper_failure_never_runs_or_retries_workload(policy, snapshot, strict_caps, tmp_path):
    wrapper_marker = tmp_path / "wrapper-launches"
    workload_marker = tmp_path / "workload-ran"
    wrapper = tmp_path / "fake-taskset"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        f"p = Path({str(wrapper_marker)!r})\n"
        "p.open('a').write('one\\n')\n"
        "raise SystemExit(77)\n"
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    strict_caps["tools"]["taskset"] = {
        "path": str(wrapper),
        "available": True,
        "version": "synthetic",
    }
    plan = launch.plan_strict_launch(policy, snapshot, strict_caps)
    result = launch.execute_strict_launch(
        plan,
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
            str(workload_marker),
        ],
        strict_caps,
        2,
        snapshot_provider=lambda: snapshot,
    )
    assert result.metrics["return_code"] == 77
    assert wrapper_marker.read_text().splitlines() == ["one"]
    assert not workload_marker.exists()


def test_timeout_retains_partial_output_and_never_retries(policy, snapshot, strict_caps, tmp_path):
    plan = launch.plan_strict_launch(inherited(policy), snapshot, strict_caps)
    marker = tmp_path / "launches"
    code = (
        "from pathlib import Path; import sys,time; "
        "p=Path(sys.argv[1]); p.open('a').write('one\\n'); "
        "print('partial', flush=True); time.sleep(30)"
    )
    result = launch.execute_strict_launch(
        plan,
        [sys.executable, "-c", code, str(marker)],
        strict_caps,
        0.2,
        snapshot_provider=lambda: snapshot,
    )
    assert result.metrics["return_code"] == 124 and result.metrics["timed_out"]
    assert "partial" in result.raw_output
    assert marker.read_text().splitlines() == ["one"]


def test_signal_status_and_exactly_once_execution(policy, snapshot, strict_caps, tmp_path):
    plan = launch.plan_strict_launch(inherited(policy), snapshot, strict_caps)
    marker = tmp_path / "launches"
    code = (
        "from pathlib import Path; import os,signal,sys; "
        "Path(sys.argv[1]).open('a').write('one\\n'); os.kill(os.getpid(), signal.SIGTERM)"
    )
    result = launch.execute_strict_launch(
        plan,
        [sys.executable, "-c", code, str(marker)],
        strict_caps,
        2,
        snapshot_provider=lambda: snapshot,
    )
    assert result.metrics["return_code"] == -signal.SIGTERM
    assert marker.read_text().splitlines() == ["one"]


def test_interrupt_kills_child_and_is_not_retried(policy, snapshot, strict_caps, monkeypatch):
    plan = launch.plan_strict_launch(inherited(policy), snapshot, strict_caps)
    calls = []

    def interrupt(*args, **kwargs):
        calls.append(args[0])
        raise KeyboardInterrupt

    monkeypatch.setattr(profiler, "execute_command", interrupt)
    with pytest.raises(KeyboardInterrupt):
        launch.execute_strict_launch(
            plan, ["workload"], strict_caps, 1, snapshot_provider=lambda: snapshot
        )
    assert calls == [["workload"]]


@pytest.mark.parametrize("permitted", [None, [2, 6]])
@pytest.mark.parametrize("placement", [Placement.COMPACT, Placement.INHERITED])
def test_partial_cpu_discovery_cannot_authorize_strict_affinity(
    policy, snapshot, strict_caps, permitted, placement
):
    partial = replace(
        snapshot,
        inherited_cpu_ids=None,
        permitted_cpu_ids=permitted,
        sources={**snapshot.sources, "permitted_cpu_ids": "synthetic optional envelope"},
        unavailable={
            **({"permitted_cpu_ids": "optional cgroup unavailable"} if permitted is None else {}),
            "inherited_cpu_ids": "kernel task mask unreadable",
        },
    )
    # INHERITED still declares OMP_PROC_BIND, which can rebind worker threads.
    with pytest.raises(launch.StrictPlanningError, match="kernel task mask unreadable") as exc:
        launch.plan_strict_launch(replace(policy, placement=placement), partial, strict_caps)
    assert exc.value.code == "unknown_restriction"


def test_known_narrow_inherited_mask_needs_no_optional_cgroup(policy, snapshot, strict_caps):
    narrow = replace(snapshot, inherited_cpu_ids=[6], effective_cpu_ids=[6])
    requested = replace(policy, budget=replace(policy.budget, cpu_count=1, reference_cpu_ids=[6]))
    plan = launch.plan_strict_launch(requested, narrow, strict_caps)
    assert plan.cpu_ids == (6,)


def test_fresh_snapshot_losing_affinity_refuses_launch(policy, snapshot, strict_caps, monkeypatch):
    plan = launch.plan_strict_launch(policy, snapshot, strict_caps)
    fresh = replace(
        snapshot,
        inherited_cpu_ids=None,
        unavailable={**snapshot.unavailable, "inherited_cpu_ids": "fresh kernel read failed"},
    )
    monkeypatch.setattr(profiler, "execute_command", lambda *a, **k: pytest.fail("launched"))
    with pytest.raises(launch.StrictPlanningError, match="fresh kernel read failed"):
        launch.execute_strict_launch(
            plan, ["true"], strict_caps, 1, snapshot_provider=lambda: fresh
        )


@pytest.mark.parametrize("numa", list(NumaPolicy))
@pytest.mark.parametrize("need", [1023, 1024, 1025, None])
def test_memory_need_respects_tightest_ancestor_cap_for_every_numa_policy(
    policy, snapshot, strict_caps, numa, need, monkeypatch
):
    from simtune.models import LimitState, MemoryLimit

    host = replace(
        snapshot,
        memory_limits=[
            MemoryLimit(source="leaf", scope="allocation", state=LimitState.LIMITED, bytes=4096),
            MemoryLimit(
                source="ancestor", scope="allocation", state=LimitState.LIMITED, bytes=1024
            ),
        ],
        numa_nodes=[replace(snapshot.numa_nodes[0], memory_bytes=8192, unavailable={})],
    )
    requested = replace(
        policy,
        numa_policy=numa,
        numa_node_count=1 if numa in {NumaPolicy.BIND, NumaPolicy.INTERLEAVE} else None,
        unavailable={} if numa in {NumaPolicy.BIND, NumaPolicy.INTERLEAVE} else policy.unavailable,
        budget=replace(
            policy.budget,
            memory_bytes=need,
            unavailable=policy.budget.unavailable if need is None else {},
        ),
    )
    monkeypatch.setattr(profiler, "execute_command", lambda *a, **k: pytest.fail("launched"))
    if need == 1025:
        with pytest.raises(launch.StrictPlanningError, match="1024 bytes") as exc:
            launch.plan_strict_launch(requested, host, strict_caps)
        assert exc.value.code == "budget_exceeds_restriction"
    else:
        launch.plan_strict_launch(requested, host, strict_caps)


@pytest.mark.parametrize("numa", list(NumaPolicy))
@pytest.mark.parametrize("state", ["unknown", "unlimited"])
def test_unknown_or_unlimited_allocation_cap_is_not_guessed(
    policy, snapshot, strict_caps, numa, state
):
    from simtune.models import LimitState, MemoryLimit

    host = replace(
        snapshot,
        memory_limits=[
            MemoryLimit(
                source="synthetic",
                scope="allocation",
                state=LimitState(state),
                bytes=None,
                unavailable={"bytes": "unknown or unlimited cap"},
            )
        ],
    )
    requested = replace(
        policy,
        numa_policy=numa,
        numa_node_count=1 if numa in {NumaPolicy.BIND, NumaPolicy.INTERLEAVE} else None,
        unavailable={} if numa in {NumaPolicy.BIND, NumaPolicy.INTERLEAVE} else policy.unavailable,
        budget=replace(policy.budget, memory_bytes=1024, unavailable={}),
    )
    if numa == NumaPolicy.INHERITED:
        launch.plan_strict_launch(requested, host, strict_caps)
    else:
        with pytest.raises(launch.StrictPlanningError, match="capacity is not fully observed"):
            launch.plan_strict_launch(requested, host, strict_caps)


def test_memory_cap_is_refreshed_before_execution(policy, snapshot, strict_caps, monkeypatch):
    from simtune.models import LimitState, MemoryLimit

    requested = replace(policy, budget=replace(policy.budget, memory_bytes=1024, unavailable={}))
    plan = launch.plan_strict_launch(requested, snapshot, strict_caps)
    fresh = replace(
        snapshot,
        memory_limits=[
            MemoryLimit(
                source="new ancestor",
                scope="allocation",
                state=LimitState.LIMITED,
                bytes=512,
            )
        ],
    )
    monkeypatch.setattr(profiler, "execute_command", lambda *a, **k: pytest.fail("launched"))
    with pytest.raises(launch.StrictPlanningError, match="512 bytes"):
        launch.execute_strict_launch(
            plan, ["true"], strict_caps, 1, snapshot_provider=lambda: fresh
        )
