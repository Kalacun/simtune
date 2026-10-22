"""Strict, serializable launch planning and exactly-once execution.

This module is deliberately separate from the legacy profile runner.  A strict
plan is either completely resolved or raises :class:`StrictPlanningError`
before a workload is launched.  The legacy runner remains the user-facing
best-effort/manual compatibility path.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

try:
    import resource
except ImportError:  # Strict planning reports process limits as unsupported.
    resource = None

from . import profiler, util
from .models import (
    ActionRecord,
    ActionStatus,
    BudgetBasis,
    CpuTopology,
    NumaPolicy,
    Placement,
    Platform,
    PolicySpec,
    ResourceSnapshot,
    SettingSpec,
    Target,
    ToolIdentity,
)
from .topology import discover_resource_snapshot

SnapshotProvider = Callable[[], ResourceSnapshot]
LimitReader = Callable[[int], tuple[int, int]]

_OPENMP_SETTINGS = {
    "OMP_NUM_THREADS",
    "OMP_DYNAMIC",
    "OMP_PROC_BIND",
    "OMP_PLACES",
    "OMP_MAX_ACTIVE_LEVELS",
}


class StrictPlanningError(ValueError):
    """A requested strict setting cannot be applied without changing its meaning."""

    def __init__(
        self,
        *,
        code: str,
        setting: str,
        requested: Any,
        restriction: str,
        remedy: str,
    ) -> None:
        self.code = code
        self.setting = setting
        self.requested = requested
        self.restriction = restriction
        self.remedy = remedy
        super().__init__(
            f"strict launch {code} for {setting}: requested {requested!r}; "
            f"restriction: {restriction}; remedy: {remedy}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "setting": self.setting,
            "requested": self.requested,
            "restriction": self.restriction,
            "remedy": self.remedy,
        }


@dataclass(frozen=True)
class StrictLaunchPlan:
    """Resolved launch data; unlike a callable preexec hook, this is serializable."""

    schema_version: int
    kind: str
    policy: PolicySpec
    snapshot_id: str
    cpu_ids: tuple[int, ...]
    numa_node_ids: tuple[int, ...]
    wrappers: tuple[tuple[str, ...], ...]
    env_overrides: tuple[tuple[str, str], ...]
    rlimits: tuple[tuple[int, int, int, str], ...]
    actions: tuple[ActionRecord, ...]
    preflight_checks: tuple[str, ...]
    unobserved: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "policy": self.policy.to_dict(),
            "snapshot_id": self.snapshot_id,
            "cpu_ids": list(self.cpu_ids),
            "numa_node_ids": list(self.numa_node_ids),
            "wrappers": [list(item) for item in self.wrappers],
            "env_overrides": dict(self.env_overrides),
            "rlimits": [
                {"resource": name, "resource_id": key, "soft": soft, "hard": hard}
                for key, soft, hard, name in self.rlimits
            ],
            "actions": [action.to_dict() for action in self.actions],
            "preflight_checks": list(self.preflight_checks),
            "unobserved": list(self.unobserved),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False, indent=2) + "\n"

    def resolution_key(self) -> tuple[Any, ...]:
        """Fields that must remain identical at the immediate pre-launch recheck."""
        return (
            self.cpu_ids,
            self.numa_node_ids,
            self.wrappers,
            self.env_overrides,
            self.rlimits,
        )


@dataclass(frozen=True)
class StrictLaunchResult:
    """One launch result.  Wrapper success is not a placement observation."""

    final_argv: tuple[str, ...]
    metrics: dict[str, Any]
    raw_output: str
    policy_id: str
    snapshot_id: str
    actions: tuple[ActionRecord, ...]
    unobserved: tuple[str, ...]


def _error(
    code: str, setting: str, requested: Any, restriction: str, remedy: str
) -> StrictPlanningError:
    return StrictPlanningError(
        code=code,
        setting=setting,
        requested=requested,
        restriction=restriction,
        remedy=remedy,
    )


def _tool(capabilities: Mapping[str, Any], name: str, setting: str) -> tuple[str, str | None]:
    tool = capabilities.get("tools", {}).get(name, {})
    path = tool.get("path") if tool.get("available", bool(tool.get("path"))) else None
    if not isinstance(path, str) or not path or "\0" in path:
        raise _error(
            "missing_capability",
            setting,
            name,
            f"{name} is not available on this host",
            "install/enable the tool, choose an inherited policy, or use the manual run path",
        )
    version = tool.get("version")
    return path, version if isinstance(version, str) and version else None


def _tool_identity(name: str, mechanism: str, version: str | None) -> ToolIdentity:
    return ToolIdentity(
        name=name,
        version=version,
        mechanism=mechanism,
        unavailable={} if version is not None else {"version": "tool version was not observed"},
    )


def _planned_action(
    setting: SettingSpec,
    *,
    tool: ToolIdentity,
    resolved: Any,
    explanation: str,
) -> ActionRecord:
    return ActionRecord(
        setting=setting,
        tool=tool,
        status=ActionStatus.PLANNED,
        resolved_value=resolved,
        observed_value=None,
        explanation=explanation,
        observation_scope="preflight resolution only; child/runtime effect was not observed",
        evidence=[],
        unavailable={"observed_value": "execution and runtime observation have not occurred"},
    )


def _skipped_action(setting: SettingSpec, reason: str) -> ActionRecord:
    return ActionRecord(
        setting=setting,
        tool=_tool_identity("none", "not scheduled", None),
        status=ActionStatus.SKIPPED,
        resolved_value=None,
        observed_value=None,
        explanation=reason,
        observation_scope="not applicable because the optional setting was not scheduled",
        evidence=[],
        unavailable={"resolved_value": reason, "observed_value": reason},
    )


def _synthetic_setting(
    *, setting_id: str, target: Target, name: str, value: Any, unit: str, rationale: str
) -> SettingSpec:
    return SettingSpec(
        id=setting_id,
        target=target,
        name=name,
        value=value,
        unit=unit,
        required=True,
        rationale=rationale,
        expected_effect="Launch semantics only; no performance effect is claimed.",
    )


def _internal_setting_id(policy: PolicySpec, base: str) -> str:
    """Avoid colliding with user-declared setting IDs in later TrialRecords."""
    occupied = {setting.id for setting in policy.settings}
    candidate = base
    suffix = 2
    while candidate in occupied:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _known_effective_cpus(snapshot: ResourceSnapshot, setting: str) -> list[int]:
    # A partial intersection is useful discovery, but an allocation envelope
    # cannot establish a narrower voluntary task mask. Strict plans resolve a
    # concrete CPU budget (also used by OpenMP controls), so require the task's
    # kernel affinity observation even when optional cgroup discovery is absent.
    if snapshot.inherited_cpu_ids is None:
        raise _error(
            "unknown_restriction",
            setting,
            "inherited task affinity",
            snapshot.unavailable.get("inherited_cpu_ids", "inherited task affinity is unknown"),
            "collect kernel task affinity before strict planning or use inherited/manual execution",
        )
    if snapshot.effective_cpu_ids is None:
        raise _error(
            "unknown_restriction",
            setting,
            "eligible CPUs",
            snapshot.unavailable.get("effective_cpu_ids", "effective CPU boundary is unknown"),
            "collect a trusted resource snapshot or use inherited/manual execution",
        )
    if not snapshot.effective_cpu_ids:
        raise _error(
            "empty_restriction",
            setting,
            "nonempty CPU mask",
            "known online/permitted/inherited CPU boundaries intersect to an empty set",
            "repair the allocation or run manually after inspecting host restrictions",
        )
    return sorted(snapshot.effective_cpu_ids)


def _topology_rows(
    snapshot: ResourceSnapshot, eligible: list[int], setting: str
) -> list[CpuTopology]:
    if snapshot.cpus is None:
        raise _error(
            "unknown_topology",
            setting,
            "logical placement",
            snapshot.unavailable.get("cpus", "CPU topology is unknown"),
            "collect socket/core topology or choose inherited placement",
        )
    by_id = {row.cpu_id: row for row in snapshot.cpus}
    missing = [cpu for cpu in eligible if cpu not in by_id]
    if missing:
        raise _error(
            "incomplete_topology",
            setting,
            eligible,
            f"eligible CPUs lack topology rows: {missing}",
            "refresh resource discovery or choose inherited placement",
        )
    rows = [by_id[cpu] for cpu in eligible]
    if any(row.socket_id is None or row.core_id is None for row in rows):
        raise _error(
            "incomplete_topology",
            setting,
            "socket/core placement",
            "one or more eligible CPUs lack socket/core identity",
            "collect complete topology or choose inherited placement",
        )
    return rows


def _resolve_cpu_ids(policy: PolicySpec, snapshot: ResourceSnapshot) -> tuple[int, ...]:
    eligible = _known_effective_cpus(snapshot, "placement")
    count = policy.budget.cpu_count or len(eligible)
    if count > len(eligible):
        raise _error(
            "budget_exceeds_restriction",
            "budget.cpu_count",
            count,
            f"only {len(eligible)} effective CPUs are available: {eligible}",
            "reduce the CPU budget or request a larger allocation",
        )
    if policy.placement == Placement.INHERITED:
        if count != len(eligible):
            raise _error(
                "semantic_adjustment",
                "placement",
                policy.placement.value,
                f"inherited affinity contains {len(eligible)} CPUs but the budget requests {count}",
                "use an explicit compact/spread policy or match the inherited CPU budget",
            )
        return tuple(eligible)

    rows = _topology_rows(snapshot, eligible, "placement")
    if policy.placement == Placement.COMPACT:
        ordered = sorted(rows, key=lambda row: (row.socket_id, row.core_id, row.cpu_id))
    else:
        cores: dict[tuple[int, int], list[int]] = {}
        for row in rows:
            cores.setdefault((row.socket_id, row.core_id), []).append(row.cpu_id)
        if policy.placement == Placement.ONE_THREAD_PER_CORE:
            ordered_ids = [min(cpus) for _, cpus in sorted(cores.items())]
            if count > len(ordered_ids):
                raise _error(
                    "budget_exceeds_cores",
                    "placement",
                    policy.placement.value,
                    f"{count} CPUs requested but only {len(ordered_ids)} physical cores are known",
                    "reduce the CPU budget or use compact/spread placement",
                )
            return tuple(ordered_ids[:count])

        sockets: dict[int, list[tuple[int, list[int]]]] = {}
        for (socket_id, core_id), cpus in cores.items():
            sockets.setdefault(socket_id, []).append((core_id, sorted(cpus)))
        for socket_cores in sockets.values():
            socket_cores.sort()
        ordered_ids = []
        max_siblings = max(
            len(cpus) for socket_cores in sockets.values() for _, cpus in socket_cores
        )
        max_cores = max(len(socket_cores) for socket_cores in sockets.values())
        # Use a core on each socket before returning to another core, and use
        # every first hardware thread before any SMT sibling.
        for sibling_index in range(max_siblings):
            for core_index in range(max_cores):
                for socket_id in sorted(sockets):
                    socket_cores = sockets[socket_id]
                    if core_index >= len(socket_cores):
                        continue
                    cpus = socket_cores[core_index][1]
                    if sibling_index < len(cpus):
                        ordered_ids.append(cpus[sibling_index])
        return tuple(ordered_ids[:count])
    return tuple(row.cpu_id for row in ordered[:count])


def _eligible_numa_nodes(snapshot: ResourceSnapshot, cpu_ids: tuple[int, ...]) -> list[int]:
    if snapshot.allowed_memory_node_ids is None:
        raise _error(
            "unknown_restriction",
            "numa_policy",
            "eligible memory nodes",
            snapshot.unavailable.get(
                "allowed_memory_node_ids", "allowed memory-node boundary is unknown"
            ),
            "collect allowed memory nodes or choose inherited NUMA policy",
        )
    allowed = set(snapshot.allowed_memory_node_ids)
    if not allowed:
        raise _error(
            "empty_restriction",
            "numa_policy",
            "nonempty memory-node set",
            "the allowed memory-node set is known empty",
            "repair the allocation or choose inherited/manual execution",
        )
    if snapshot.numa_nodes is None:
        raise _error(
            "unknown_topology",
            "numa_policy",
            "NUMA topology",
            snapshot.unavailable.get("numa_nodes", "NUMA topology is unknown"),
            "collect NUMA topology or choose inherited NUMA policy",
        )
    cpu_set = set(cpu_ids)
    mapped = {node.node_id for node in snapshot.numa_nodes if cpu_set & set(node.cpu_ids)}
    eligible = sorted(allowed & mapped)
    if not eligible:
        raise _error(
            "empty_restriction",
            "numa_policy",
            sorted(allowed),
            f"no allowed memory node is associated with resolved CPUs {sorted(cpu_set)}",
            "choose compatible CPU/NUMA intent or refresh topology",
        )
    return eligible


def _check_allocation_memory(policy: PolicySpec, snapshot: ResourceSnapshot) -> None:
    requested = policy.budget.memory_bytes
    if requested is None:
        return
    finite_caps = [item.bytes for item in snapshot.memory_limits if item.bytes is not None]
    if finite_caps and requested > min(finite_caps):
        raise _error(
            "budget_exceeds_restriction",
            "budget.memory_bytes",
            requested,
            f"tightest known process/allocation memory cap is {min(finite_caps)} bytes",
            "reduce the memory budget or request a larger allocation",
        )


def _check_memory_capacity(
    policy: PolicySpec, snapshot: ResourceSnapshot, node_ids: tuple[int, ...]
) -> None:
    requested = policy.budget.memory_bytes
    if requested is None:
        return
    by_id = {node.node_id: node for node in snapshot.numa_nodes or []}
    capacities = [by_id[node].memory_bytes for node in node_ids if node in by_id]
    if not capacities or any(value is None for value in capacities):
        raise _error(
            "unknown_capacity",
            "budget.memory_bytes",
            requested,
            "selected NUMA-node capacity is not fully observed",
            "collect node capacity or omit strict memory binding",
        )
    if requested > sum(value for value in capacities if value is not None):
        raise _error(
            "budget_exceeds_restriction",
            "budget.memory_bytes",
            requested,
            f"selected nodes expose {sum(value for value in capacities if value is not None)} bytes",
            "reduce the memory budget or select more eligible nodes",
        )


def _runtime_is_openmp(policy: PolicySpec) -> bool:
    normalized = policy.runtime.name.lower().replace("_", "-")
    return "openmp" in normalized or normalized in {"omp", "gomp", "libgomp", "libomp"}


def _openmp_value(name: str, value: Any) -> str:
    if name in {"OMP_NUM_THREADS", "OMP_MAX_ACTIVE_LEVELS"}:
        if type(value) not in {int, str} or not str(value).isdigit() or int(value) < 1:
            raise ValueError("must be a positive integer")
        return str(value)
    if not isinstance(value, str) or "\0" in value or not value:
        raise ValueError("must be a nonempty NUL-free string")
    lowered = value.lower()
    if name == "OMP_DYNAMIC" and lowered not in {"true", "false"}:
        raise ValueError("must be TRUE or FALSE")
    if name == "OMP_PROC_BIND" and lowered not in {
        "true",
        "false",
        "master",
        "primary",
        "close",
        "spread",
    }:
        raise ValueError("uses an unsupported OpenMP binding value")
    return value


def _resolve_limit(
    setting: SettingSpec, limit_reader: LimitReader
) -> tuple[tuple[int, int, int, str], ActionRecord]:
    if resource is None:
        raise _error(
            "unsupported_setting",
            setting.name,
            setting.value,
            "the POSIX resource module is unavailable",
            "remove the required process limit or use a supported POSIX host",
        )
    resource_id = util.allowlist_resource_names().get(setting.name)
    if resource_id is None:
        raise _error(
            "unsupported_setting",
            setting.name,
            setting.value,
            "the process-limit name is not allowlisted",
            f"use one of {sorted(util.allowlist_resource_names())}",
        )
    value = setting.value
    pair = value if isinstance(value, list) and len(value) == 2 else [value, value]
    if any(type(item) is not int or item < -1 for item in pair):
        raise _error(
            "invalid_setting",
            setting.name,
            value,
            "limits must be integers >= -1, either one value or [soft, hard]",
            "provide a valid child-process limit",
        )
    soft, hard = [resource.RLIM_INFINITY if item == -1 else item for item in pair]
    if hard != resource.RLIM_INFINITY and (soft == resource.RLIM_INFINITY or soft > hard):
        raise _error(
            "invalid_setting",
            setting.name,
            value,
            "soft limit exceeds requested hard limit",
            "make the soft limit no greater than the hard limit",
        )
    try:
        _, current_hard = limit_reader(resource_id)
    except (OSError, ValueError) as exc:
        raise _error(
            "unknown_restriction",
            setting.name,
            value,
            f"could not read the inherited hard cap: {type(exc).__name__}: {exc}",
            "inspect the host limit and retry planning",
        ) from exc
    if current_hard != resource.RLIM_INFINITY and (
        hard == resource.RLIM_INFINITY or hard > current_hard
    ):
        raise _error(
            "hard_limit_increase",
            setting.name,
            value,
            f"current hard cap is {current_hard}",
            "request a limit no greater than the inherited hard cap",
        )
    action = _planned_action(
        setting,
        tool=_tool_identity("python-resource", "child pre-exec setrlimit", None),
        resolved=[soft, hard],
        explanation="The child will request this exact limit before exec; failure aborts exec.",
    )
    return (resource_id, soft, hard, setting.name), action


def plan_strict_launch(
    policy: PolicySpec,
    snapshot: ResourceSnapshot,
    capabilities: Mapping[str, Any],
    *,
    limit_reader: LimitReader | None = None,
) -> StrictLaunchPlan:
    """Resolve one strict policy without launching or mutating process state."""
    if snapshot.platform != Platform.LINUX:
        raise _error(
            "unsupported_platform",
            "platform",
            snapshot.platform.value,
            "strict CPU/NUMA planning currently requires Linux observations",
            "use the existing manual runner on this platform",
        )
    capability_platform = capabilities.get("platform")
    if capability_platform is not None and capability_platform != snapshot.platform.value:
        raise _error(
            "resource_conflict",
            "platform",
            capability_platform,
            f"capabilities disagree with resource snapshot platform {snapshot.platform.value!r}",
            "refresh capabilities and the resource snapshot together",
        )
    if snapshot.is_wsl and policy.numa_policy != NumaPolicy.INHERITED:
        raise _error(
            "unsupported_platform",
            "numa_policy",
            policy.numa_policy.value,
            "WSL does not provide trusted NUMA-policy evidence",
            "use inherited NUMA policy or the manual runner",
        )
    if snapshot.conflicts:
        raise _error(
            "resource_conflict",
            "resource_snapshot",
            snapshot.id,
            "; ".join(snapshot.conflicts),
            "resolve the discovery conflict and refresh the snapshot",
        )

    _check_allocation_memory(policy, snapshot)
    cpu_ids = _resolve_cpu_ids(policy, snapshot)
    wrappers: list[tuple[str, ...]] = []
    actions: list[ActionRecord] = []
    checks = [
        "platform is Linux and the snapshot has no recorded resource conflicts",
        "resolved CPU IDs are within the snapshot's online/permitted/inherited intersection",
        "inherited task affinity is observed; optional missing cgroup data grants no wider CPUs",
        "declared memory need does not exceed any known allocation cap; no RSS enforcement is implied",
    ]

    if policy.placement == Placement.INHERITED:
        cpu_tool = _tool_identity("kernel-inherited-affinity", "no affinity change", None)
    else:
        taskset, taskset_version = _tool(capabilities, "taskset", "placement")
        mask = ",".join(str(cpu) for cpu in cpu_ids)
        # taskset requires `--` before the CPU list, not after it.
        wrappers.append((taskset, "-c", "--", mask))
        cpu_tool = _tool_identity("taskset", "process affinity wrapper", taskset_version)
    actions.append(
        _planned_action(
            _synthetic_setting(
                setting_id=_internal_setting_id(policy, "launch-cpu-placement"),
                target=Target.CPU_PLACEMENT,
                name="placement",
                value=policy.placement.value,
                unit="policy",
                rationale=policy.rationale,
            ),
            tool=cpu_tool,
            resolved=list(cpu_ids),
            explanation="Logical placement was resolved to eligible Linux CPU IDs.",
        )
    )

    node_ids: tuple[int, ...] = ()
    if policy.numa_policy == NumaPolicy.INHERITED:
        numa_tool = _tool_identity("kernel-inherited-mempolicy", "no NUMA policy change", None)
        numa_resolved: Any = "inherited"
    else:
        numactl, numactl_version = _tool(capabilities, "numactl", "numa_policy")
        eligible_nodes = _eligible_numa_nodes(snapshot, cpu_ids)
        count = policy.numa_node_count or len(eligible_nodes)
        if count > len(eligible_nodes):
            raise _error(
                "budget_exceeds_restriction",
                "numa_node_count",
                count,
                f"only eligible nodes are {eligible_nodes}",
                "reduce the node count or request a compatible allocation",
            )
        node_ids = tuple(eligible_nodes[:count])
        _check_memory_capacity(policy, snapshot, node_ids)
        if policy.numa_policy == NumaPolicy.LOCAL:
            args = ("--localalloc",)
            node_ids = tuple(eligible_nodes)
        elif policy.numa_policy == NumaPolicy.BIND:
            args = (f"--membind={','.join(map(str, node_ids))}",)
        elif policy.numa_policy == NumaPolicy.INTERLEAVE:
            args = (f"--interleave={','.join(map(str, node_ids))}",)
        else:  # Enum validation makes this defensive branch unreachable.
            raise _error(
                "unsupported_setting",
                "numa_policy",
                policy.numa_policy.value,
                "unknown NUMA policy",
                "choose inherited/local/bind/interleave",
            )
        wrappers.append((numactl, *args, "--"))
        numa_tool = _tool_identity("numactl", "memory-policy wrapper", numactl_version)
        numa_resolved = list(node_ids) if node_ids else policy.numa_policy.value
        checks.append("NUMA node IDs are allowed and associated with the resolved CPU mask")
    actions.append(
        _planned_action(
            _synthetic_setting(
                setting_id=_internal_setting_id(policy, "launch-numa-policy"),
                target=Target.NUMA_POLICY,
                name="numa_policy",
                value=policy.numa_policy.value,
                unit="policy",
                rationale=policy.rationale,
            ),
            tool=numa_tool,
            resolved=numa_resolved,
            explanation="No raw numactl CPU option is accepted by the strict planner.",
        )
    )

    env: dict[str, str] = {}
    rlimits: list[tuple[int, int, int, str]] = []
    reader = limit_reader or (resource.getrlimit if resource is not None else lambda _: (0, 0))
    explicit_threads = None
    for setting in policy.settings:
        if setting.target in {Target.RUNTIME, Target.ENVIRONMENT}:
            if setting.name not in _OPENMP_SETTINGS or not _runtime_is_openmp(policy):
                reason = (
                    f"unsupported runtime setting {setting.name!r} for runtime "
                    f"{policy.runtime.name!r}"
                )
                if setting.required:
                    raise _error(
                        "unsupported_setting",
                        setting.name,
                        setting.value,
                        reason,
                        "use the documented OpenMP allowlist/runtime or make the setting optional",
                    )
                actions.append(_skipped_action(setting, reason))
                continue
            try:
                value = _openmp_value(setting.name, setting.value)
            except ValueError as exc:
                if setting.required:
                    raise _error(
                        "invalid_setting",
                        setting.name,
                        setting.value,
                        str(exc),
                        "provide a supported OpenMP value",
                    ) from exc
                actions.append(_skipped_action(setting, str(exc)))
                continue
            if setting.name in env and env[setting.name] != value:
                raise _error(
                    "conflicting_setting",
                    setting.name,
                    setting.value,
                    f"another setting resolved to {env[setting.name]!r}",
                    "remove the duplicate/conflicting setting",
                )
            env[setting.name] = value
            if setting.name == "OMP_NUM_THREADS":
                explicit_threads = int(value)
            actions.append(
                _planned_action(
                    setting,
                    tool=_tool_identity(
                        policy.runtime.name, "allowlisted child environment", policy.runtime.version
                    ),
                    resolved=value,
                    explanation=(
                        "The environment value is passed literally to the child. Runtime support "
                        "does not prove that every application thread obeys it."
                    ),
                )
            )
        elif setting.target == Target.PROCESS_LIMIT:
            try:
                limit, action = _resolve_limit(setting, reader)
            except StrictPlanningError as exc:
                if setting.required:
                    raise
                actions.append(_skipped_action(setting, str(exc)))
                continue
            rlimits.append(limit)
            actions.append(action)
        else:
            reason = "top-level placement/NUMA intent cannot be overridden by raw settings"
            if setting.required:
                raise _error(
                    "conflicting_setting",
                    setting.name,
                    setting.value,
                    reason,
                    "express placement and NUMA intent in the PolicySpec fields",
                )
            actions.append(_skipped_action(setting, reason))

    if policy.budget.thread_count is not None:
        if not _runtime_is_openmp(policy):
            raise _error(
                "unsupported_runtime",
                "budget.thread_count",
                policy.budget.thread_count,
                f"runtime {policy.runtime.name!r} has no strict thread-count adapter",
                "declare a supported OpenMP runtime or omit the strict thread budget",
            )
        if explicit_threads is not None and explicit_threads != policy.budget.thread_count:
            raise _error(
                "conflicting_setting",
                "OMP_NUM_THREADS",
                explicit_threads,
                f"policy thread budget is {policy.budget.thread_count}",
                "make OMP_NUM_THREADS match the declared thread budget",
            )
        if explicit_threads is None:
            env["OMP_NUM_THREADS"] = str(policy.budget.thread_count)
            actions.append(
                _planned_action(
                    _synthetic_setting(
                        setting_id=_internal_setting_id(policy, "launch-thread-count"),
                        target=Target.RUNTIME,
                        name="OMP_NUM_THREADS",
                        value=policy.budget.thread_count,
                        unit="threads",
                        rationale=policy.budget.rationale,
                    ),
                    tool=_tool_identity(
                        policy.runtime.name, "allowlisted child environment", policy.runtime.version
                    ),
                    resolved=str(policy.budget.thread_count),
                    explanation="The declared OpenMP thread budget is passed to the child.",
                )
            )
    if policy.budget.basis == BudgetBasis.RESOURCE_CAPPED and policy.budget.cpu_count is None:
        raise _error(
            "invalid_budget",
            "budget.cpu_count",
            None,
            "resource-capped policy lacks a CPU count",
            "declare the CPU count",
        )

    checks.extend(
        [
            "required runtime/environment settings are allowlisted and value-validated",
            "requested process hard limits do not exceed inherited hard caps",
            "wrapper argv uses explicit option boundaries and no shell",
        ]
    )
    return StrictLaunchPlan(
        schema_version=1,
        kind="strict_launch_plan",
        policy=policy,
        snapshot_id=snapshot.id,
        cpu_ids=cpu_ids,
        numa_node_ids=node_ids,
        wrappers=tuple(wrappers),
        env_overrides=tuple(sorted(env.items())),
        rlimits=tuple(rlimits),
        actions=tuple(actions),
        preflight_checks=tuple(checks),
        unobserved=(
            "preflight does not prove wrapper effectiveness after exec",
            "application runtimes may later rebind threads",
            "NUMA wrapper invocation does not prove page residency or first-touch placement",
            "declared memory need establishes neither reserved memory nor an enforced RSS limit",
            "no performance or scientific-output property is observed by this stage",
        ),
    )


def _build_preexec(rlimits: tuple[tuple[int, int, int, str], ...]) -> Callable[[], None] | None:
    if not rlimits:
        return None

    def apply_limits() -> None:
        for resource_id, soft, hard, _ in rlimits:
            resource.setrlimit(resource_id, (soft, hard))

    return apply_limits


def _combine(wrappers: tuple[tuple[str, ...], ...], argv: list[str]) -> list[str]:
    return [part for wrapper in wrappers for part in wrapper] + argv


def execute_strict_launch(
    plan: StrictLaunchPlan,
    argv: list[str],
    capabilities: Mapping[str, Any],
    timeout: float | None,
    *,
    base_env: Mapping[str, str] | None = None,
    snapshot_provider: SnapshotProvider = discover_resource_snapshot,
    limit_reader: LimitReader | None = None,
    with_profiling: bool = False,
    collector_mode: str = "timing",
    cwd: str | Path | None = None,
    before_launch: Callable[[float | None], float | None] | None = None,
    passthrough: bool = False,
) -> StrictLaunchResult:
    """Revalidate and launch exactly once; a failure is returned, never retried."""
    command = util.sanitize_cmd(argv)
    util.validate_run_options(1, timeout)
    if passthrough and with_profiling:
        raise ValueError("stream passthrough requires minimal execution")
    current_snapshot = snapshot_provider()
    current = plan_strict_launch(
        plan.policy, current_snapshot, capabilities, limit_reader=limit_reader
    )
    if current.resolution_key() != plan.resolution_key():
        raise _error(
            "resource_changed",
            "preflight",
            plan.snapshot_id,
            "the immediate pre-launch resolution differs from the saved plan",
            "discard the stale plan and explicitly approve a newly resolved plan",
        )

    final_argv = _combine(current.wrappers, command)
    env = util.apply_env_overrides(
        dict(os.environ if base_env is None else base_env), dict(current.env_overrides)
    )
    preexec = _build_preexec(current.rlimits)
    # Discovery and strict re-resolution may be slow. Obtain the final timeout
    # and durable launch authorization only after all such work has completed.
    if before_launch is not None:
        timeout = before_launch(timeout)
        util.validate_run_options(1, timeout)
    if with_profiling and collector_mode != "timing":
        raise ValueError("legacy profiling and new collectors cannot be combined")
    if collector_mode == "perf-stat":
        perf_info = capabilities.get("tools", {}).get("perf", {})
        result, raw = profiler.collect_perf_command(
            final_argv,
            env,
            timeout,
            perf_info["path"],
            perf_info["version"],
            preexec_fn=preexec,
            cwd=cwd,
        )
    elif collector_mode != "timing":
        raise ValueError("unsupported collector mode")
    elif with_profiling:
        kwargs = {"preexec_fn": preexec}
        if cwd is not None:
            kwargs["cwd"] = cwd
        result, raw = profiler.profile_command(
            final_argv, env, timeout, dict(capabilities), **kwargs
        )
    else:
        kwargs = {"preexec_fn": preexec}
        if passthrough:
            kwargs["passthrough"] = True
        if cwd is not None:
            kwargs["cwd"] = cwd
        result, raw = profiler.execute_command(final_argv, env, timeout, **kwargs)

    # Invocation is evidence that one process path was attempted, not proof that
    # a runtime retained affinity/policy. Stage 04 will attach durable evidence.
    attempted = tuple(
        replace(
            action,
            explanation=action.explanation
            + " The planned mechanism was invoked once; its continuing effect was not observed.",
        )
        if action.status == ActionStatus.PLANNED
        else action
        for action in current.actions
    )
    return StrictLaunchResult(
        final_argv=tuple(final_argv),
        metrics=result,
        raw_output=raw,
        policy_id=plan.policy.id,
        snapshot_id=current_snapshot.id,
        actions=attempted,
        unobserved=current.unobserved,
    )


def write_plan(path: str | Path, plan: StrictLaunchPlan) -> None:
    """Write a reviewable plan; execution evidence storage belongs to stage 04."""
    Path(path).write_text(plan.to_json(), encoding="utf-8")
