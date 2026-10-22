"""Immutable recipes backed by complete, checked search evidence.

The version-1 Recipe envelope is unchanged. The versioned stage06 extension
contains applicability and evidence digests; a bare schema fixture is not a
replay authorization. Digests detect damage, not maliciously forged experiments.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .experiments import ExperimentStore, _atomic_bytes, _atomic_json
from .hardware import hardware_context
from .models import (
    ActionStatus,
    CheckKind,
    CompatibilityRules,
    EvidenceResolution,
    Outcome,
    Phase,
    Recipe,
    ResourceSnapshot,
    TrialStatus,
    ValidationResult,
    ValidationState,
    WorkloadSpec,
)
from .search import STATE_KEY, SearchConfig, _read_trials
from .statistics import confirm_policy, score_policy
from .validation import ValidationError, artifact_path, load_json
from .workloads import (
    RUNTIME_ENV_NAMES,
    LoadedWorkload,
    _canonical_sha256,
    environment_fingerprint,
    load_workload,
    sha256_file,
)

RECIPE_KEY = "simtune.stage06"


def topology_description(snapshot: ResourceSnapshot, selected: list[int] | None = None) -> dict:
    """Normalize IDs while preserving socket/core/SMT/node relationships.

    Sorted eligible IDs define the ordering; arbitrary topology-preserving
    permutations can conservatively require revalidation. Counts alone and
    matching unknown observations never constitute a topology class.
    """
    snapshot.to_dict()
    eligible = sorted(snapshot.effective_cpu_ids or [])
    if not eligible or snapshot.conflicts or snapshot.inherited_cpu_ids is None:
        raise ValidationError(
            "topology: known nonempty inherited boundary without conflicts required"
        )
    rows = {row.cpu_id: row for row in snapshot.cpus or []}
    nodes = {node.node_id: node for node in snapshot.numa_nodes or []}
    allowed = snapshot.allowed_memory_node_ids
    if not allowed or any(cpu not in rows for cpu in eligible):
        raise ValidationError(
            "topology: complete CPU and allowed-memory-node observations required"
        )
    sockets, cores, node_order = {}, {}, {}
    cpu_index = {cpu: index for index, cpu in enumerate(eligible)}
    descriptions = []
    for cpu in eligible:
        row = rows[cpu]
        if any(
            value is None
            for value in (row.socket_id, row.core_id, row.smt_siblings, row.numa_node_id)
        ):
            raise ValidationError("topology: socket/core/SMT/NUMA identity is incomplete")
        if row.numa_node_id not in nodes or cpu not in nodes[row.numa_node_id].cpu_ids:
            raise ValidationError("topology: CPU and NUMA relationships disagree")
        siblings = set(row.smt_siblings)
        core = (row.socket_id, row.core_id)
        for other in eligible:
            peer = rows[other]
            if ((peer.socket_id, peer.core_id) == core) != (other in siblings):
                raise ValidationError("topology: core and SMT relationships disagree")
        sockets.setdefault(row.socket_id, len(sockets))
        cores.setdefault(core, len(cores))
        node_order.setdefault(row.numa_node_id, len(node_order))
        descriptions.append(
            {
                "socket": sockets[row.socket_id],
                "core": cores[core],
                "node": node_order[row.numa_node_id],
                "eligible_siblings": sorted(cpu_index[s] for s in siblings if s in cpu_index),
                "total_siblings": len(siblings),
                "selected": selected is None or cpu in selected,
            }
        )
    for node in sorted(allowed):
        if node not in nodes:
            raise ValidationError("topology: an allowed memory node lacks topology")
        node_order.setdefault(node, len(node_order))
    node_rows = []
    for node_id in node_order:
        node = nodes[node_id]
        if node.distances is None or any(str(other) not in node.distances for other in node_order):
            raise ValidationError("topology: NUMA distance observations are incomplete")
        node_rows.append(
            {
                "allowed": node_id in allowed,
                "cpus": sorted(cpu_index[cpu] for cpu in node.cpu_ids if cpu in cpu_index),
                "distances": [node.distances[str(other)] for other in node_order],
            }
        )
    if selected is not None and (not selected or not set(selected) <= set(eligible)):
        raise ValidationError("topology: selected mask is outside the effective allocation")
    return {
        "schema_version": 1,
        "platform": snapshot.platform.value,
        "is_wsl": snapshot.is_wsl,
        "cpus": descriptions,
        "nodes": node_rows,
    }


def topology_class(snapshot: ResourceSnapshot, selected: list[int] | None = None) -> str:
    return "relationships-v1:" + _canonical_sha256(topology_description(snapshot, selected))


def logical_budget(budget):
    """Absolute evidence IDs never become replay placement instructions."""
    return replace(
        budget,
        reference_cpu_ids=None,
        unavailable={
            **budget.unavailable,
            "reference_cpu_ids": "resolve logical reference in the current allocation",
        },
    )


def _trial_sources(trial) -> set[str]:
    return {
        f"trials/{trial.id}/trial.json",
        *trial.raw_artifacts,
        *trial.validation.evidence,
        *(metric.raw_source for metric in trial.metrics if metric.raw_source is not None),
        *(source for action in trial.actions for source in action.evidence),
    }


@dataclass(frozen=True)
class RecipeBundle:
    path: Path
    recipe: Recipe
    workload: WorkloadSpec
    snapshot: ResourceSnapshot
    details: dict[str, Any]
    execution_errors: tuple[str, ...] = ()
    stored_recipe: Recipe | None = None


def _source_facts(root: Path) -> dict:
    store = ExperimentStore.open(root)
    if not store.is_complete():
        raise ValidationError(
            "recipe requires a completed experiment; uncertain evidence cannot be saved"
        )
    record = store.record
    state = record.extensions.get(STATE_KEY, {})
    if state.get("schema_version") != 1 or state.get("phase") != "done":
        raise ValidationError("recipe requires supported terminal search evidence")
    config = SearchConfig(**state["config"])
    workload = WorkloadSpec.from_json(artifact_path(root, "workload.json").read_text())
    snapshot = ResourceSnapshot.from_json(artifact_path(root, "resources.json").read_text())
    context = LoadedWorkload(
        workload,
        root / "workload.json",
        (),
        record.workload_fingerprint,
        tuple(sorted(workload.fixed_env.items())),
        (),
    )
    if (
        environment_fingerprint(context, snapshot, state["capabilities"])
        != record.environment_fingerprint
    ):
        raise ValidationError("saved runtime/resource context differs from environment fingerprint")
    from .models import PolicySpec

    policies = {data["id"]: PolicySpec.from_dict(data) for data in state["policies"]}
    selected = policies.get(record.selected_policy_id)
    if selected is None:
        raise ValidationError("experiment has no supported selected policy to save")
    trials = _read_trials(store)
    if set(record.training_trial_ids) != {t.id for t in trials if t.phase == Phase.TRAINING} or set(
        record.confirmation_trial_ids
    ) != {t.id for t in trials if t.phase == Phase.CONFIRMATION}:
        raise ValidationError("experiment evidence IDs disagree with the terminal journal")
    reference = state["reference_policy_id"]
    proposed = state.get("proposed_policy_id")
    scores = {
        key: asdict(
            score_policy(
                trials,
                workload=workload,
                baseline_policy_id=reference,
                candidate_policy_id=key,
                phase=Phase.TRAINING,
                seed=config.seed,
                minimum_pairs=config.repeat,
                bootstrap_samples=config.bootstrap_samples,
            )
        )
        for key in policies
        if key != reference and any(t.policy_id == key for t in trials)
    }
    confirmation = None
    if proposed is not None:
        confirmation = confirm_policy(
            trials,
            workload=workload,
            baseline_policy_id=reference,
            candidate_policy_id=proposed,
            rule=record.selection_rule,
            minimum_training_pairs=config.repeat,
            bootstrap_samples=config.bootstrap_samples,
        )
    if record.outcome == Outcome.VALIDATED_IMPROVEMENT and (
        confirmation is None
        or confirmation.outcome != record.outcome
        or confirmation.selected_policy_id != selected.id
        or not scores.get(selected.id, {}).get("eligible")
    ):
        raise ValidationError("claimed improvement does not follow from the actual trial evidence")
    if record.outcome != Outcome.VALIDATED_IMPROVEMENT and selected.id != reference:
        raise ValidationError("unconfirmed search must retain its reference")
    selected_trials = [t for t in trials if t.policy_id == selected.id]
    science = bool(selected_trials) and all(
        t.status == TrialStatus.SUCCEEDED and t.validation.state == ValidationState.PASSED
        for t in selected_trials
    )
    heldout = bool(confirmation and confirmation.protocol_valid)
    limitations = list(
        record.extensions.get("simtune.stage04", {}).get("untracked_external_dependencies", [])
    )
    if not science:
        limitations.append("Selected policy lacks complete successful scientific checks.")
    if not heldout:
        limitations.append("Independent held-out confirmation is absent or ineligible.")
    runtime_known = workload.runtime.version is not None and workload.runtime.identity is not None
    controls = state.get("runtime_environment")
    if controls is not None and (
        not isinstance(controls, dict)
        or set(controls) != set(RUNTIME_ENV_NAMES)
        or any(value is not None and type(value) is not str for value in controls.values())
    ):
        raise ValidationError("unsupported/incomplete saved runtime control context")
    runtime_known = runtime_known and controls is not None
    if not runtime_known:
        limitations.append(
            "Declared runtime version/identity is incomplete; replay is unvalidated."
        )
    try:
        topology = topology_class(snapshot)
        topology_data = topology_description(snapshot)
    except ValidationError as exc:
        topology, topology_data = "unavailable", None
        limitations.append(str(exc))
    resolutions = {}
    plans = []
    sources = {
        "experiment.json",
        "workload.json",
        "resources.json",
        record.journal,
        record.completion_marker,
    }
    for trial in trials:
        sources.update(_trial_sources(trial))
        plan_ref = f"trials/{trial.id}/plan.json"
        if plan_ref not in trial.raw_artifacts:
            if trial.status == TrialStatus.SUCCEEDED:
                raise ValidationError("successful trial lacks its strict plan")
            continue
        plan = load_json(artifact_path(root, plan_ref).read_text())
        if plan.get("kind") != "strict_launch_plan" or plan.get("schema_version") != 1:
            raise ValidationError("unsupported evidence launch plan")
        if (
            plan["policy"] != policies[trial.policy_id].to_dict()
            or plan["snapshot_id"] != snapshot.id
        ):
            raise ValidationError("evidence plan differs from its policy/resource identity")
        launch_ref = f"trials/{trial.id}/launch.json"
        if launch_ref in trial.raw_artifacts:
            launch = load_json(artifact_path(root, launch_ref).read_text())
            metrics = launch["metrics"]
            elapsed = [metric.value for metric in trial.metrics if metric.name == "elapsed"]
            if (
                launch.get("policy_id") != trial.policy_id
                or launch.get("snapshot_id") != snapshot.id
                or (
                    trial.status == TrialStatus.SUCCEEDED
                    and (
                        metrics.get("return_code") != trial.return_code
                        or metrics.get("timed_out") != trial.timed_out
                    )
                )
                or elapsed != [metrics.get("wall_time_sec")]
            ):
                raise ValidationError("trial measurement/status differs from its launch evidence")
        elif trial.status == TrialStatus.SUCCEEDED:
            raise ValidationError("successful trial lacks actual launch evidence")
        if trial.validation.state == ValidationState.PASSED:
            case = next(c for c in workload.cases if c.id == trial.case_id)
            expected_checks = [*workload.output_checks, *case.checks]
            if set(trial.validation.checks) != {check.id for check in expected_checks}:
                raise ValidationError("trial checks differ from declared scientific checks")
            for check in expected_checks:
                check_ref = f"trials/{trial.id}/checks/{check.id}.json"
                if check_ref not in trial.validation.evidence:
                    raise ValidationError("missing scientific check evidence")
                evidence = load_json(artifact_path(root, check_ref).read_text())
                if (
                    evidence.get("kind") != "output_check_result"
                    or evidence.get("schema_version") != 1
                    or evidence.get("check_id") != check.id
                    or evidence.get("method") != check.method.value
                    or evidence.get("passed") is not True
                ):
                    raise ValidationError("scientific validation differs from its check evidence")
                if check.method == CheckKind.FILE_HASH:
                    if (
                        evidence.get("expected_sha256") != check.sha256
                        or evidence.get("observed_sha256") != check.sha256
                    ):
                        raise ValidationError(
                            "output hash evidence does not pass the declared check"
                        )
                elif check.method == CheckKind.NUMERIC_JSON:
                    value = evidence.get("observed")
                    if (
                        type(value) not in (int, float)
                        or not math.isfinite(value)
                        or any(
                            evidence.get(field) != getattr(check, field)
                            for field in (
                                "expected",
                                "json_field",
                                "absolute_tolerance",
                                "relative_tolerance",
                            )
                        )
                        or abs(value - check.expected)
                        > max(
                            check.absolute_tolerance,
                            check.relative_tolerance * max(abs(value), abs(check.expected)),
                        )
                    ):
                        raise ValidationError(
                            "numeric evidence does not pass the declared scientific check"
                        )
                elif evidence.get("return_code") != 0 or evidence.get("timed_out") is not False:
                    raise ValidationError("validator evidence does not show successful completion")
        if trial.policy_id != selected.id:
            continue
        plans.append(plan)
        if any(
            action.setting.required and action.status == ActionStatus.SKIPPED
            for action in trial.actions
        ):
            raise ValidationError("required setting was skipped in actual trial actions")
        if any(
            action["setting"]["required"] and action["status"] == ActionStatus.SKIPPED
            for action in plan["actions"]
        ):
            raise ValidationError("required setting was skipped in recipe evidence")
        key = (tuple(plan["cpu_ids"]), tuple(plan["numa_node_ids"]))
        resolutions.setdefault(key, []).append(trial.id)
    evidence_resolutions = [
        EvidenceResolution(
            resource_snapshot_id=snapshot.id,
            cpu_ids=list(cpus),
            memory_node_ids=list(nodes),
            trial_ids=ids,
        )
        for (cpus, nodes), ids in resolutions.items()
    ]
    selected_topologies = (
        sorted({topology_class(snapshot, list(cpus)) for cpus, _ in resolutions})
        if topology_data
        else []
    )
    required = sorted(
        {
            action["tool"]["name"]
            for plan in plans
            for action in plan["actions"]
            if action["setting"]["required"]
            and action["tool"] is not None
            and action["tool"]["name"] in {"taskset", "numactl"}
        }
    )
    return dict(
        record=record,
        state=state,
        workload=workload,
        snapshot=snapshot,
        selected=selected,
        trials=trials,
        science=science,
        heldout=heldout,
        runtime_known=runtime_known,
        topology=topology,
        topology_data=topology_data,
        selected_topologies=selected_topologies,
        plans=plans,
        required=required,
        resolutions=evidence_resolutions,
        sources=sorted(sources),
        scores=json.loads(json.dumps(scores)),
        confirmation=json.loads(json.dumps(asdict(confirmation))) if confirmation else None,
        limitations=limitations,
    )


def save_recipe(experiment: str | Path, destination: str | Path) -> Recipe:
    """Publish a new recipe last, after copying and hashing its evidence bundle.

    Existing recipes and experiment files are never replaced. A failed export
    leaves any partial sidecar for diagnosis but no successful recipe pointer.
    """
    root = Path(experiment).resolve(strict=True)
    target = Path(destination).absolute()
    if target.exists() or target.is_symlink():
        raise ValidationError("recipe destination already exists; choose a new path")
    # Do not add new files inside an immutable source experiment.
    if target.resolve().is_relative_to(root):
        raise ValidationError("save the recipe outside its immutable source experiment")
    facts = _source_facts(root)
    loaded = load_workload(root / "workload.json")
    if loaded.workload_fingerprint != facts["record"].workload_fingerprint:
        raise ValidationError("saved workload fingerprint differs from experiment identity")
    target.parent.mkdir(parents=True, exist_ok=True)
    sidecar_name = target.name + ".evidence"
    sidecar = artifact_path(target.parent, sidecar_name)
    sidecar.mkdir(exist_ok=False)
    hashes = {}
    for source in facts["sources"]:
        original = artifact_path(root, source)
        copied = artifact_path(sidecar, source)
        copied.parent.mkdir(parents=True, exist_ok=True)
        if not original.is_file():
            raise ValidationError(f"missing recipe evidence: {source}")
        before = sha256_file(original)
        shutil.copyfile(original, copied)
        if sha256_file(copied) != before or sha256_file(original) != before:
            raise ValidationError("source evidence changed during recipe export")
        hashes[f"{sidecar_name}/{source}"] = before
    identity = {
        "kind": "recipe_workload_identity",
        "schema_version": 1,
        "payload": {
            "workload": loaded.spec.to_dict(),
            "resolved_artifacts": [
                {"id": name, "path": str(path)} for name, path in loaded.artifact_paths
            ],
            "declared_environment": dict(loaded.relevant_environment),
        },
    }
    _atomic_json(sidecar / "recipe-identity.json", identity)
    hashes[f"{sidecar_name}/recipe-identity.json"] = sha256_file(sidecar / "recipe-identity.json")
    # Re-read the owned copy: checking a source once is not a race-proof boundary.
    facts = _source_facts(sidecar)
    recipe = _recipe_from_facts(facts, sidecar_name, hashes)
    # Publish a new immutable name atomically without a check/replace race.
    pending = target.with_name(f".{target.name}.{uuid.uuid4().hex}.pending")
    _atomic_bytes(pending, recipe.to_json(root=target.parent).encode("utf-8"))
    try:
        os.link(pending, target)
        descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        pending.unlink()
    return recipe


def _recipe_from_facts(facts, sidecar_name, hashes, *, version=3) -> Recipe:
    record, workload, selected = facts["record"], facts["workload"], facts["selected"]
    prefix = sidecar_name + "/"
    policy = replace(selected, budget=logical_budget(selected.budget))
    science = facts["science"]
    validation = ValidationResult(
        state=ValidationState.PASSED if science else ValidationState.UNCHECKED,
        checks=sorted(
            {
                check
                for trial in facts["trials"]
                if trial.policy_id == selected.id
                for check in trial.validation.checks
            }
        )
        if science
        else [],
        explanation="Scientific checks passed on the saved selected-policy trials."
        if science
        else "No complete checked selected-policy evidence; explicit unvalidated replay required.",
        evidence=[
            prefix + f"trials/{trial.id}/trial.json"
            for trial in facts["trials"]
            if trial.policy_id == selected.id
        ],
    )
    replay_validated = bool(
        science and facts["heldout"] and facts["runtime_known"] and facts["topology_data"]
    )
    confirmation = facts["confirmation"]
    if version == 1 and confirmation is not None:
        # Explicit legacy wire representation, derived from the full protocol.
        # Never restore the old phase-only authorization predicate.
        confirmation = {
            key: value
            for key, value in confirmation.items()
            if key not in {"protocol_valid", "protocol_reasons"}
        }
    recipe = Recipe(
        id="recipe-" + record.id.removeprefix("search-")[:100],
        workload_family=workload.family,
        variable_arguments=workload.case_substitutions,
        policy=policy,
        resource_constraints=policy.budget,
        compatibility=CompatibilityRules(
            workload_fingerprint=record.workload_fingerprint,
            runtime=workload.runtime,
            topology_class=facts["topology"],
            input_classes=sorted({case.input_class for case in workload.cases}),
            required_capabilities=facts["required"],
            require_artifact_hashes=True,
            require_runtime_match=True,
            require_topology_match=True,
            rationale="Match declared identity, whole-argument substitutions, runtime, topology relationships and resource requirements; resolve new IDs.",
        ),
        validation=validation,
        evidence=sorted(hashes),
        evidence_resolutions=facts["resolutions"],
        performance_outcome=record.outcome,
        explanation=record.explanation
        + " Selected policy is the best tested eligible choice or retained reference, never a global optimum.",
        extensions={
            RECIPE_KEY: {
                "schema_version": version,
                "evidence_root": sidecar_name,
                "sha256": hashes,
                "experiment_id": record.id,
                "environment_fingerprint": record.environment_fingerprint,
                "replay_validated": replay_validated,
                "science_checked": science,
                "heldout_confirmed": facts["heldout"],
                "runtime_identity_complete": facts["runtime_known"],
                "runtime_environment": facts["state"].get("runtime_environment"),
                "trial_options": facts["state"]["options"],
                "topology": facts["topology_data"],
                "selected_topology_classes": facts["selected_topologies"],
                "training_scores": facts["scores"],
                "confirmation": confirmation,
                "selection_rule": record.selection_rule.to_dict(),
                "budget": record.budget.to_dict(),
                "break_even_runs": facts["state"].get("break_even_runs"),
                "validated_saved_seconds": facts["state"].get("validated_saved_seconds"),
                "limitations": [
                    *facts["limitations"],
                    "Small samples, correlated timings, drift and the declared case mix limit inference.",
                    "A planned or invoked mechanism does not observe thread placement or page residency.",
                    "Runtime identity is declared metadata; dynamic dependencies must be explicitly pinned as artifacts.",
                ],
                "source_recipe": facts["state"].get("source_recipe"),
            }
        },
    )
    if version >= 3:
        recipe.extensions[RECIPE_KEY].update(
            hardware=hardware_context(facts["snapshot"]),
            label_scope="historical scientific/protocol/runtime/topology evidence only; current replay output is unchecked",
        )
    return recipe


def load_recipe(path: str | Path, *, diagnostic: bool = False) -> RecipeBundle:
    """Recompute evidence facts; inconsistent summaries cannot authorize execution.

    Diagnostic access still verifies the evidence and identity. It exposes the
    original record alongside corrected facts without writing either to disk.
    """
    path = Path(path).absolute()
    recipe = Recipe.from_json(path.read_text(encoding="utf-8"), root=path.parent)
    data = recipe.extensions.get(RECIPE_KEY)
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data["schema_version"] not in {1, 2, 3}
    ):
        raise ValidationError("recipe lacks supported stage-06 applicability/evidence metadata")
    hashes = data.get("sha256")
    if not isinstance(hashes, dict) or not hashes or set(hashes) != set(recipe.evidence):
        raise ValidationError("recipe evidence inventory differs from its digest manifest")
    evidence_root = artifact_path(path.parent, data["evidence_root"])
    for reference, digest in hashes.items():
        file = artifact_path(path.parent, reference)
        if not file.is_relative_to(evidence_root) or sha256_file(file) != digest:
            raise ValidationError(f"recipe evidence digest/path mismatch: {reference}")
    facts = _source_facts(evidence_root)
    record, workload = facts["record"], facts["workload"]
    expected_sources = {data["evidence_root"] + "/" + name for name in facts["sources"]}
    expected_sources.add(data["evidence_root"] + "/recipe-identity.json")
    if set(hashes) != expected_sources:
        raise ValidationError("recipe evidence inventory is incomplete")
    identity = load_json(artifact_path(evidence_root, "recipe-identity.json").read_text())
    if (
        identity.get("kind") != "recipe_workload_identity"
        or identity.get("schema_version") != 1
        or identity["payload"]["workload"] != workload.to_dict()
        or _canonical_sha256(identity["payload"]) != record.workload_fingerprint
    ):
        raise ValidationError("recipe workload identity does not match experiment fingerprint")
    expected = _recipe_from_facts(
        facts, data["evidence_root"], hashes, version=data["schema_version"]
    )
    errors = ()
    if recipe != expected:
        errors = (
            "recipe facts/validation/applicability differ from actual evidence; execution refused. "
            "Inspect with simtune explain; create fresh checked evidence with simtune tune "
            "in a new directory. Source files were not changed.",
        )
        if not diagnostic:
            raise ValidationError(errors[0])
    # Explanations use recomputed facts, never unverified performance summaries.
    details = {
        **expected.extensions[RECIPE_KEY],
        "training_scores": facts["scores"],
        "confirmation": facts["confirmation"],
        "plans": facts["plans"],
        "candidates": [c.to_dict() for c in record.candidates],
        "budget": record.budget.to_dict(),
        "selection_rule": record.selection_rule.to_dict(),
    }
    from .reporting import decision_report

    details["decision_report"] = decision_report(evidence_root)
    return RecipeBundle(
        path,
        expected if errors else recipe,
        workload,
        facts["snapshot"],
        details,
        execution_errors=errors,
        stored_recipe=recipe if errors else None,
    )


def explain_recipe(bundle: RecipeBundle) -> dict:
    recipe = bundle.recipe
    report = {
        "recipe": recipe.to_dict(),
        "label": "rejected"
        if bundle.execution_errors
        else "validated"
        if bundle.details["replay_validated"]
        else "unvalidated",
        "label_scope": "historical evidence only; current output is unchecked",
        "execution_errors": list(bundle.execution_errors),
        "measured_evidence": bundle.details,
        "decision_report": bundle.details["decision_report"],
        "applicability": {
            "checked_on_current_host": False,
            "failures": [],
            "current_compatibility": "not_checked",
            "hardware_class": {"status": "not_checked"},
            "current_output_validation": "not_run; exec output is unchecked",
            "validated_reuse_authorized": False,
            "current_performance": "unproven",
            "explanation": "Use --workload to check current identity/resources without launching.",
        },
    }
    if bundle.stored_recipe is not None:
        report["stored_recipe"] = bundle.stored_recipe.to_dict()
    return report
