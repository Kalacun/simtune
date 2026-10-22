"""Small deterministic launch hypotheses, with a ledger of every exclusion.

This generator does not run programs, infer NUMA traffic from RSS/CPU time, or
change resource counts. All executable candidates pass the strict planner.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from .launch import StrictLaunchPlan, StrictPlanningError, plan_strict_launch
from .models import (
    BudgetBasis,
    Candidate,
    NumaPolicy,
    Placement,
    PolicySpec,
    ResourceBudget,
    ResourceSnapshot,
    RuntimeIdentity,
    SettingSpec,
    Target,
)


@dataclass(frozen=True)
class CandidateSet:
    """Accepted plans plus all proposals, including unavailable/duplicate ones."""

    reference: PolicySpec
    policies: tuple[PolicySpec, ...]
    plans: tuple[StrictLaunchPlan, ...]
    ledger: tuple[Candidate, ...]
    declared_policies: tuple[PolicySpec, ...]
    notes: tuple[str, ...]


def resolved_plan_key(plan: StrictLaunchPlan) -> tuple[Any, ...]:
    """Canonical launch behavior, ignoring affinity-list order and tool spelling.

    The strict generator's only wrappers are taskset/numactl. Explicit taskset
    of the inherited mask has the same placement as inheriting that mask. A
    reordered taskset list also does not bind individual workers differently.
    NUMA mode, runtime environment and process limits remain distinct settings.
    """
    return (
        tuple(sorted(plan.cpu_ids)),
        plan.policy.numa_policy.value,
        tuple(sorted(plan.numa_node_ids)),
        tuple(sorted(plan.env_overrides)),
        tuple(sorted(plan.rlimits)),
    )


def _profile_order(
    hint: Mapping[str, Any] | None,
    workload_fingerprint: str | None,
    environment_fingerprint: str | None,
) -> tuple[list[Placement], str]:
    order = [Placement.COMPACT, Placement.SPREAD, Placement.ONE_THREAD_PER_CORE]
    if hint is None:
        return order, "No profile hint supplied; use deterministic topology hypotheses."
    for name, expected in (
        ("workload_fingerprint", workload_fingerprint),
        ("environment_fingerprint", environment_fingerprint),
    ):
        if expected is None or hint.get(name) != expected:
            return order, f"Profile hint ignored: {name} is absent or incompatible."
    if hint.get("kind") == "simtune_imported_profile":
        rows = hint.get("metrics", [])

        def one(event: str, unit: str) -> float | None:
            selected = [row for row in rows if row.get("name") == event]
            if len(selected) != 1:
                return None
            row = selected[0]
            if (
                row.get("unit") != unit
                or row.get("scope") != "process_tree"
                or row.get("scaling_factor") is not None
                or row.get("multiplex_running_fraction") not in {None, 1.0}
            ):
                return None
            value = row.get("value")
            return (
                float(value)
                if type(value) in {int, float} and math.isfinite(value) and value > 0
                else None
            )

        clock_ms = one("task-clock", "msec")
        elapsed = one("seconds time elapsed", "seconds")
        if clock_ms is None or elapsed is None:
            return (
                order,
                "Imported perf hint has no unique unscaled task-clock/elapsed pair; deterministic candidate order retained.",
            )
        ratio = clock_ms / 1000 / elapsed
        if ratio > 1:
            order = [Placement.SPREAD, Placement.ONE_THREAD_PER_CORE, Placement.COMPACT]
        return order, (
            f"Compatible imported perf task-clock/elapsed ratio {ratio:.6g} only orders CPU hypotheses; "
            "external declarations are unverified and all selection needs fresh paired trials."
        )
    metrics = hint.get("metrics")
    aggregate = metrics.get("aggregate") if isinstance(metrics, Mapping) else None
    if not isinstance(aggregate, Mapping):
        return order, "Profile hint ignored: no existing simtune metric aggregate was supplied."
    values = [
        aggregate.get(name)
        for name in ("wall_time_sec_mean", "user_time_sec_mean", "sys_time_sec_mean")
    ]
    if (
        any(
            type(value) not in {float, int} or not math.isfinite(value) or value < 0
            for value in values
        )
        or values[0] <= 0
    ):
        return order, "Profile hint ignored: CPU/wall measurements are absent or invalid."
    ratio = (values[1] + values[2]) / values[0]
    if not math.isfinite(ratio):
        return order, "Profile hint ignored: CPU/wall ratio is not finite."
    if ratio > 1:
        order = [Placement.SPREAD, Placement.ONE_THREAD_PER_CORE, Placement.COMPACT]
    return order, (
        f"Identity-compatible profile CPU/wall ratio {ratio:.6g} only orders CPU hypotheses; "
        "it supplies no selection samples and establishes no NUMA behavior."
    )


def _policy(
    policy_id: str,
    placement: Placement,
    numa: NumaPolicy,
    runtime: RuntimeIdentity,
    budget: ResourceBudget,
    rationale: str,
    *,
    nodes: int | None = None,
    settings: list[SettingSpec] | None = None,
) -> PolicySpec:
    return PolicySpec(
        id=policy_id,
        placement=placement,
        numa_policy=numa,
        numa_node_count=nodes,
        runtime=runtime,
        settings=settings or [],
        budget=budget,
        rationale=rationale,
        unavailable={} if nodes is not None else {"numa_node_count": "no explicit node count"},
    )


def generate_candidates(
    snapshot: ResourceSnapshot,
    capabilities: Mapping[str, Any],
    *,
    runtime: RuntimeIdentity,
    budget: ResourceBudget,
    profile_hint: Mapping[str, Any] | None = None,
    workload_fingerprint: str | None = None,
    environment_fingerprint: str | None = None,
) -> CandidateSet:
    """Resolve at most nine transparent policies at the declared fixed budget.

    An inherited budget stays inherited. A resource-capped budget uses compact
    placement as its deterministic reference when fewer CPUs were requested,
    or inherited placement when its count equals the effective envelope. Its
    concrete reference mask is copied into every policy before execution; an
    incompatible explicitly declared reference mask is rejected.

    This stage searches placements only, not thread counts. NUMA bind requires
    an explicit memory-byte capacity assumption and strict known capacity;
    interleave requires at least two eligible memory nodes. There is no CPU/
    NUMA/runtime Cartesian expansion. Profiles only order fresh experiments.
    """
    placements, profile_note = _profile_order(
        profile_hint, workload_fingerprint, environment_fingerprint
    )
    notes = [
        profile_note,
        "CPU, thread and memory counts remain fixed; thread-count search is not implemented.",
        "Placement dimensions are explored individually without Cartesian expansion.",
    ]
    effective = snapshot.effective_cpu_ids
    capped = budget.basis == BudgetBasis.RESOURCE_CAPPED
    reference_placement = (
        Placement.COMPACT
        if capped and (effective is None or budget.cpu_count != len(effective))
        else Placement.INHERITED
    )
    reference = _policy(
        "baseline",
        reference_placement,
        NumaPolicy.INHERITED,
        runtime,
        budget,
        "Deterministic resource-capped reference."
        if capped
        else "Inherited reference at the declared unchanged resource budget.",
    )
    reference_error = None
    try:
        reference_plan = plan_strict_launch(reference, snapshot, capabilities)
        if budget.reference_cpu_ids is not None and set(budget.reference_cpu_ids) != set(
            reference_plan.cpu_ids
        ):
            reference_error = (
                "reference_mask_mismatch: declared reference_cpu_ids differ from the "
                f"deterministic reference mask {list(reference_plan.cpu_ids)}"
            )
        elif capped and budget.reference_cpu_ids is None:
            unavailable = dict(budget.unavailable)
            unavailable.pop("reference_cpu_ids", None)
            budget = replace(
                budget,
                reference_cpu_ids=list(reference_plan.cpu_ids),
                unavailable=unavailable,
            )
            reference = replace(reference, budget=budget)
    except StrictPlanningError as exc:
        reference_error = str(exc)

    proposals: list[tuple[PolicySpec, str | None]] = [(reference, reference_error)]
    for placement in placements:
        proposals.append(
            (
                _policy(
                    f"cpu-{placement.value.replace('_', '-')}",
                    placement,
                    NumaPolicy.INHERITED,
                    runtime,
                    budget,
                    f"Test {placement.value} CPU placement at the reference resource count.",
                ),
                None,
            )
        )

    runtime_name = runtime.name.lower().replace("_", "-")
    openmp = "openmp" in runtime_name or runtime_name in {"omp", "gomp", "libgomp", "libomp"}
    runtime_reason = None
    if not openmp:
        runtime_reason = "runtime placement requires an explicitly declared OpenMP runtime"
    elif budget.thread_count is None or budget.thread_count < 2:
        runtime_reason = "runtime placement requires a fixed declared thread count of at least two"
    for binding in ("close", "spread"):
        proposals.append(
            (
                _policy(
                    f"runtime-{binding}",
                    reference_placement,
                    NumaPolicy.INHERITED,
                    runtime,
                    budget,
                    f"Test OpenMP {binding} worker binding at the unchanged declared thread count.",
                    settings=[
                        SettingSpec(
                            id="omp-bind",
                            target=Target.RUNTIME,
                            name="OMP_PROC_BIND",
                            value=binding,
                            unit="runtime-enum",
                            required=True,
                            rationale="Worker placement is a launch hypothesis at fixed resources.",
                            expected_effect="May alter locality; no improvement is assumed.",
                        )
                    ],
                ),
                runtime_reason,
            )
        )

    reference_cpus = set(budget.reference_cpu_ids or effective or [])
    node_ids = sorted(
        node.node_id
        for node in snapshot.numa_nodes or []
        if node.node_id in (snapshot.allowed_memory_node_ids or [])
        and reference_cpus.intersection(node.cpu_ids)
    )
    for numa, node_count, reason in (
        (NumaPolicy.LOCAL, None, None),
        (
            NumaPolicy.BIND,
            1,
            "NUMA binding requires an explicit memory-byte capacity assumption"
            if budget.memory_bytes is None
            else None,
        ),
        (
            NumaPolicy.INTERLEAVE,
            max(1, len(node_ids)),
            "NUMA interleave requires at least two known eligible memory nodes"
            if len(node_ids) < 2
            else None,
        ),
    ):
        proposals.append(
            (
                _policy(
                    f"numa-{numa.value}",
                    reference_placement,
                    numa,
                    runtime,
                    budget,
                    f"Test {numa.value} allocation on eligible nodes at the reference CPU mask.",
                    nodes=node_count,
                ),
                reason,
            )
        )

    ledger: list[Candidate] = []
    plans: list[StrictLaunchPlan] = []
    seen: dict[tuple[Any, ...], str] = {}
    for policy, reason in proposals:
        plan = None
        if reference_error is not None and policy.id != reference.id:
            reason = "reference unavailable: " + reference_error
        if reason is None:
            try:
                plan = plan_strict_launch(policy, snapshot, capabilities)
            except StrictPlanningError as exc:
                reason = str(exc)
        if plan is not None:
            key = resolved_plan_key(plan)
            duplicate = seen.get(key)
            if duplicate is not None:
                reason = f"equivalent resolved launch behavior to {duplicate}"
            else:
                seen[key] = policy.id
                plans.append(plan)
        ledger.append(
            Candidate(
                policy_id=policy.id,
                exclusion_reason=reason,
                unavailable={"exclusion_reason": "eligible strict plan"} if reason is None else {},
            )
        )
    return CandidateSet(
        reference=reference,
        policies=tuple(plan.policy for plan in plans),
        plans=tuple(plans),
        ledger=tuple(ledger),
        declared_policies=tuple(policy for policy, _ in proposals),
        notes=tuple(notes),
    )


def revalidation_candidates(snapshot, capabilities, *, policy, runtime, budget):
    """Compare exactly the requested logical recipe against a fresh reference."""
    generated = generate_candidates(snapshot, capabilities, runtime=runtime, budget=budget)
    reference = generated.reference
    candidate = replace(policy, id="recipe-policy", runtime=runtime, budget=reference.budget)
    plans, ledger = [], []
    reference_entry = generated.ledger[0]
    for item in (reference, candidate):
        reason = reference_entry.exclusion_reason
        try:
            if reason is None:
                plans.append(plan_strict_launch(item, snapshot, capabilities))
        except StrictPlanningError as exc:
            reason = str(exc)
        ledger.append(
            Candidate(
                policy_id=item.id,
                exclusion_reason=reason,
                unavailable={"exclusion_reason": "eligible strict plan"} if reason is None else {},
            )
        )
    return CandidateSet(
        reference=reference,
        policies=tuple(p.policy for p in plans),
        plans=tuple(plans),
        ledger=tuple(ledger),
        declared_policies=(reference, candidate),
        notes=(
            "Explicit revalidation tests only the source recipe and an equal-resource reference; equivalent behavior is retained for honest no-improvement evidence.",
        ),
    )
