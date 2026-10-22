from dataclasses import replace

import pytest

from simtune import candidates, profiler
from simtune.models import (
    BudgetBasis,
    CpuTopology,
    NumaNode,
    Placement,
    Platform,
    ResourceBudget,
    ResourceSnapshot,
    RuntimeIdentity,
)


@pytest.fixture
def host():
    cpu_ids = [2, 3, 6, 7, 10, 11, 14, 15]
    cpus = [
        CpuTopology(
            cpu_id=cpu,
            socket_id=int(cpu >= 10),
            core_id=(cpu % 8) // 4,
            smt_siblings=[cpu // 2 * 2, cpu // 2 * 2 + 1],
            numa_node_id=int(cpu >= 10),
        )
        for cpu in cpu_ids
    ]
    return ResourceSnapshot(
        id="synthetic-two-socket",
        platform=Platform.LINUX,
        is_wsl=False,
        online_cpu_ids=cpu_ids,
        cpus=cpus,
        numa_nodes=[
            NumaNode(
                node_id=node,
                cpu_ids=[cpu.cpu_id for cpu in cpus if cpu.numa_node_id == node],
                memory_bytes=1000,
                distances={str(node): 10, str(1 - node): 20},
            )
            for node in (0, 1)
        ],
        inherited_cpu_ids=cpu_ids,
        permitted_cpu_ids=cpu_ids,
        effective_cpu_ids=cpu_ids,
        allowed_memory_node_ids=[0, 1],
        memory_limits=[],
        sources={
            field: "synthetic fixture only"
            for field in (
                "online_cpu_ids",
                "cpus",
                "numa_nodes",
                "inherited_cpu_ids",
                "permitted_cpu_ids",
                "effective_cpu_ids",
                "allowed_memory_node_ids",
            )
        },
        scheduler_hints={},
        conflicts=[],
    )


@pytest.fixture
def caps():
    return {
        "platform": "linux",
        "tools": {
            name: {"path": f"/synthetic/{name}", "available": True, "version": "test"}
            for name in ("taskset", "numactl")
        },
    }


@pytest.fixture
def runtime():
    return RuntimeIdentity(
        name="unspecified",
        version=None,
        identity=None,
        unavailable={"version": "not declared", "identity": "not declared"},
    )


@pytest.fixture
def budget():
    return ResourceBudget(
        basis=BudgetBasis.INHERITED,
        cpu_count=None,
        thread_count=None,
        memory_bytes=None,
        reference_cpu_ids=None,
        rationale="Keep the inherited resources fixed.",
        unavailable={
            "cpu_count": "inherited",
            "thread_count": "inherited",
            "memory_bytes": "undeclared",
            "reference_cpu_ids": "inherited",
        },
    )


def change_budget(budget, **changes):
    unavailable = {
        key: reason
        for key, reason in budget.unavailable.items()
        if changes.get(key, getattr(budget, key)) is None
    }
    return replace(budget, **changes, unavailable=unavailable)


def generate(host, caps, runtime, budget, **kwargs):
    return candidates.generate_candidates(host, caps, runtime=runtime, budget=budget, **kwargs)


def exclusions(result):
    return {item.policy_id: item.exclusion_reason for item in result.ledger}


def test_inherited_mask_permutations_are_not_placement_candidates(host, caps, runtime, budget):
    result = generate(host, caps, runtime, budget)
    reasons = exclusions(result)
    assert result.reference.id == "baseline"
    assert result.reference.placement == Placement.INHERITED
    assert {plan.policy.id for plan in result.plans} == {
        "baseline",
        "numa-local",
        "numa-interleave",
    }
    assert "equivalent" in reasons["cpu-compact"]
    assert "equivalent" in reasons["cpu-spread"]
    assert "budget_exceeds_cores" in reasons["cpu-one-thread-per-core"]
    assert all(plan.cpu_ids and len(plan.cpu_ids) == 8 for plan in result.plans)
    assert len(result.ledger) == len(result.declared_policies) == 9
    assert all(policy.budget == budget for policy in result.declared_policies)


def test_capped_reference_resolved_before_equal_resource_comparison(host, caps, runtime, budget):
    budget = change_budget(budget, basis=BudgetBasis.RESOURCE_CAPPED, cpu_count=2)
    result = generate(host, caps, runtime, budget)
    assert result.reference.placement == Placement.COMPACT
    assert result.reference.budget.reference_cpu_ids == [2, 3]
    assert result.reference.budget.basis == BudgetBasis.RESOURCE_CAPPED
    assert budget.reference_cpu_ids is None  # Caller declaration remains immutable.
    masks = {plan.policy.id: set(plan.cpu_ids) for plan in result.plans}
    assert masks["baseline"] == {2, 3}
    assert masks["cpu-spread"] == {2, 10}
    assert masks["cpu-one-thread-per-core"] == {2, 6}
    assert all(len(mask) == 2 for mask in masks.values())
    assert all(policy.budget == result.reference.budget for policy in result.declared_policies)
    assert "reference_cpu_ids" not in result.reference.budget.unavailable


def test_capped_full_count_needs_no_affinity_wrapper(host, caps, runtime, budget):
    budget = change_budget(budget, basis=BudgetBasis.RESOURCE_CAPPED, cpu_count=8)
    caps["tools"].pop("taskset")
    result = generate(host, caps, runtime, budget)
    assert result.reference.placement == Placement.INHERITED
    assert result.reference.budget.reference_cpu_ids == host.effective_cpu_ids
    assert result.plans[0].wrappers == ()


@pytest.mark.parametrize("count,mask", [(2, [2, 10]), (10, None)])
def test_incompatible_reference_cannot_authorize_other_candidates(
    host, caps, runtime, budget, count, mask
):
    budget = change_budget(
        budget,
        basis=BudgetBasis.RESOURCE_CAPPED,
        cpu_count=count,
        reference_cpu_ids=mask,
    )
    result = generate(host, caps, runtime, budget)
    assert result.plans == result.policies == ()
    assert all(item.exclusion_reason for item in result.ledger)
    assert all("reference unavailable" in item.exclusion_reason for item in result.ledger[1:])


def test_inherited_declaration_never_silently_becomes_a_capped_budget(host, caps, runtime, budget):
    result = generate(host, caps, runtime, change_budget(budget, cpu_count=2))
    assert result.plans == ()
    assert "semantic_adjustment" in result.ledger[0].exclusion_reason


def test_missing_tools_and_runtime_are_explicit_without_fallback(host, caps, runtime, budget):
    caps["tools"] = {}
    result = generate(host, caps, runtime, budget)
    assert [policy.id for policy in result.policies] == ["baseline"]
    reasons = exclusions(result)
    assert "missing_capability" in reasons["numa-local"]
    assert "missing_capability" in reasons["cpu-spread"]
    assert "OpenMP" in reasons["runtime-close"]
    assert "capacity assumption" in reasons["numa-bind"]


@pytest.mark.parametrize("state", ["macos", "unknown", "conflict", "empty"])
def test_unavailable_reference_produces_complete_fail_closed_ledger(
    host, caps, runtime, budget, state, monkeypatch
):
    monkeypatch.setattr(profiler, "execute_command", lambda *a, **k: pytest.fail("launched"))
    if state == "macos":
        host = replace(host, platform=Platform.MACOS)
    elif state == "unknown":
        host = replace(
            host,
            effective_cpu_ids=None,
            unavailable={"effective_cpu_ids": "unobservable"},
        )
    elif state == "conflict":
        host = replace(host, conflicts=["synthetic restriction conflict"])
    else:
        host = replace(host, inherited_cpu_ids=[], effective_cpu_ids=[])
    result = generate(host, caps, runtime, budget)
    assert result.plans == ()
    assert len(result.ledger) == 9
    assert all(item.exclusion_reason for item in result.ledger)


def test_numa_binding_requires_declared_known_capacity(host, caps, runtime, budget):
    budget = change_budget(budget, memory_bytes=500)
    known = generate(host, caps, runtime, budget)
    bound = next(plan for plan in known.plans if plan.policy.id == "numa-bind")
    assert bound.numa_node_ids == (0,)
    assert bound.policy.budget.memory_bytes == 500
    assert bound.wrappers == (("/synthetic/numactl", "--membind=0", "--"),)
    host = replace(
        host,
        numa_nodes=[
            replace(node, memory_bytes=None, unavailable={"memory_bytes": "unknown"})
            for node in host.numa_nodes
        ],
    )
    unknown = generate(host, caps, runtime, budget)
    assert "unknown_capacity" in exclusions(unknown)["numa-bind"]


def test_interleave_never_guesses_nodes_or_widens_memory_boundary(host, caps, runtime, budget):
    host = replace(host, allowed_memory_node_ids=[1])
    result = generate(host, caps, runtime, change_budget(budget, memory_bytes=500))
    assert "at least two" in exclusions(result)["numa-interleave"]
    assert next(plan for plan in result.plans if plan.policy.id == "numa-bind").numa_node_ids == (
        1,
    )


def test_fixed_count_openmp_runtime_placement_is_distinct(host, caps, runtime, budget):
    runtime = replace(runtime, name="openmp")
    budget = change_budget(budget, thread_count=4)
    result = generate(host, caps, runtime, budget)
    runtime_plans = [plan for plan in result.plans if plan.policy.id.startswith("runtime-")]
    assert len(runtime_plans) == 2
    assert [dict(plan.env_overrides) for plan in runtime_plans] == [
        {"OMP_NUM_THREADS": "4", "OMP_PROC_BIND": "close"},
        {"OMP_NUM_THREADS": "4", "OMP_PROC_BIND": "spread"},
    ]
    assert all(plan.policy.budget.thread_count == 4 for plan in result.plans)


@pytest.mark.parametrize("threads", [None, 1])
def test_no_implicit_thread_count_search(host, caps, runtime, budget, threads):
    result = generate(
        host,
        caps,
        replace(runtime, name="openmp"),
        change_budget(budget, thread_count=threads),
    )
    assert "fixed declared thread count" in exclusions(result)["runtime-spread"]


def test_cpu_list_order_and_redundant_taskset_have_same_key(host, caps, runtime, budget):
    inherited = generate(host, caps, runtime, budget).plans[0]
    permuted = replace(
        inherited,
        cpu_ids=tuple(reversed(inherited.cpu_ids)),
        wrappers=(("/synthetic/taskset", "-c", "--", "15,14,11,10,7,6,3,2"),),
        policy=replace(inherited.policy, placement=Placement.SPREAD),
    )
    assert candidates.resolved_plan_key(inherited) == candidates.resolved_plan_key(permuted)


def hint():
    return {
        "workload_fingerprint": "a" * 64,
        "environment_fingerprint": "b" * 64,
        "metrics": {
            "aggregate": {
                "wall_time_sec_mean": 1,
                "user_time_sec_mean": 3,
                "sys_time_sec_mean": 0,
                "max_rss_kb_mean": 1_000_000,
            }
        },
    }


def test_profile_hint_requires_exact_workload_and_environment_identity(host, caps, runtime, budget):
    matching = {
        "profile_hint": hint(),
        "workload_fingerprint": "a" * 64,
        "environment_fingerprint": "b" * 64,
    }
    accepted = generate(host, caps, runtime, budget, **matching)
    assert accepted.declared_policies[1].placement == Placement.SPREAD
    assert "establishes no NUMA" in accepted.notes[0]
    for changed in (
        {"workload_fingerprint": "c" * 64},
        {"environment_fingerprint": "c" * 64},
        {"environment_fingerprint": None},
        {"profile_hint": {"metrics": hint()["metrics"]}},
    ):
        rejected = generate(host, caps, runtime, budget, **(matching | changed))
        assert rejected.declared_policies[1].placement == Placement.COMPACT
        assert "Profile hint ignored" in rejected.notes[0]


@pytest.mark.parametrize("wall", [0, -1, float("nan"), float("inf"), True, None])
def test_invalid_profile_metrics_cannot_create_hypotheses(host, caps, runtime, budget, wall):
    profile = hint()
    profile["metrics"]["aggregate"]["wall_time_sec_mean"] = wall
    result = generate(
        host,
        caps,
        runtime,
        budget,
        profile_hint=profile,
        workload_fingerprint="a" * 64,
        environment_fingerprint="b" * 64,
    )
    assert "Profile hint ignored" in result.notes[0]
    assert result.declared_policies[1].placement == Placement.COMPACT


def test_generation_is_deterministic_and_preserves_every_exclusion(host, caps, runtime, budget):
    first = generate(host, caps, runtime, budget)
    second = generate(host, caps, runtime, budget)
    assert first == second
    assert {item.policy_id for item in first.ledger} == {
        policy.id for policy in first.declared_policies
    }
    assert {item.policy_id for item in first.ledger if item.exclusion_reason is None} == {
        policy.id for policy in first.policies
    }
    assert len({candidates.resolved_plan_key(plan) for plan in first.plans}) == len(first.plans)
