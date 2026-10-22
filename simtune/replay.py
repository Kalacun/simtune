"""Compatibility, one-shot minimal execution and explicit recipe revalidation."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import util
from .experiments import _atomic_json, _utc_now
from .hardware import compare_hardware
from .launch import StrictLaunchPlan, execute_strict_launch, plan_strict_launch
from .models import BudgetBasis, Outcome, Placement, WorkloadSpec
from .recipes import (
    RecipeBundle,
    explain_recipe,
    load_recipe,
    logical_budget,
    save_recipe,
    topology_class,
)
from .search import SearchConfig, run_search, search_capabilities, search_snapshot
from .tune import duration
from .validation import ValidationError, artifact_path
from .workloads import (
    _artifact_path,
    expand_trial_argv,
    load_workload,
    runtime_environment,
    sha256_file,
)


def _resource_values(budget):
    return (budget.basis, budget.cpu_count, budget.thread_count, budget.memory_bytes)


def _compatible_manifest(bundle: RecipeBundle, current: WorkloadSpec) -> None:
    saved = bundle.workload
    fields = ("family", "case_substitutions", "runtime", "fixed_env")
    changed = [name for name in fields if getattr(saved, name) != getattr(current, name)]
    if (
        saved.executable_sha256 != current.executable_sha256
        or saved.argv_template[1:] != current.argv_template[1:]
        or saved.extensions.get("simtune.literal_argv", False)
        != current.extensions.get("simtune.literal_argv", False)
    ):
        changed.append("executable/command structure")

    def artifacts(spec):
        return sorted(
            (a.id, a.role, str((Path(spec.base_directory) / a.path).resolve()), a.sha256)
            for a in spec.artifacts
        )

    if artifacts(saved) != artifacts(current):
        changed.append("declared source/fixed artifacts")
    if any(
        case.input_class not in bundle.recipe.compatibility.input_classes for case in current.cases
    ):
        changed.append("input class")
    if any(
        _resource_values(case.resource_needs)
        != _resource_values(bundle.recipe.resource_constraints)
        for case in current.cases
    ):
        changed.append("resource constraints")
    if changed:
        raise ValidationError(
            "recipe incompatible: "
            + ", ".join(changed)
            + "; explicitly revalidate in a new directory"
        )


def _resolve_command(argv: list[str], spec: WorkloadSpec, case_id: str | None, cwd: Path):
    if not argv:
        raise ValidationError("exec requires -- COMMAND")
    executable = shutil.which(argv[0])
    if executable is None:
        raise ValidationError("replay executable cannot be resolved")
    executable = str(Path(executable).resolve(strict=True))
    if sha256_file(executable) != spec.executable_sha256:
        raise ValidationError("recipe incompatible: executable hash changed")
    if Path(executable) != Path(spec.resolved_executable).resolve(strict=True):
        raise ValidationError("replay executable differs from the manifest declaration")
    normalized = [executable, *argv[1:]]
    cases = [case for case in spec.cases if case_id is None or case.id == case_id]
    matches = [case for case in cases if expand_trial_argv(spec, case, cwd) == normalized]
    if not matches:
        raise ValidationError(
            "command does not match a declared case/whole-argument template; new values require --workload (and {trial_dir} must equal cwd)"
        )
    identities = {
        (case.input_class, tuple((a.id, a.sha256) for a in case.input_artifacts))
        for case in matches
    }
    if len(identities) > 1:
        raise ValidationError("command matches ambiguous input classes/artifacts; supply --case")
    case = matches[0]
    # Only the selected case's inputs are required for bare replay of saved cases.
    # Fixed source/code artifacts remain pinned for every invocation.
    declared_paths = {
        _artifact_path(Path(spec.base_directory), artifact, "replay declared artifact")
        for artifact in [*spec.artifacts, *case.input_artifacts]
    }
    # An unchanged relative script/input token must not select a different file
    # merely because replay has a different cwd. Never rewrite the user's argv.
    for argument in normalized[1:]:
        try:
            declared = (Path(spec.base_directory) / argument).resolve()
            actual = (cwd / argument).resolve()
        except (OSError, ValueError, RuntimeError):
            continue  # Ordinary literal arguments need not be filesystem paths.
        if declared in declared_paths and actual != declared:
            raise ValidationError(
                "relative file argument resolves outside its declared artifact identity; use matching cwd or an explicit absolute input path"
            )
    return normalized, case


def resolve_recipe(
    bundle: RecipeBundle,
    snapshot,
    capabilities,
    *,
    workload: WorkloadSpec | None = None,
    base_env=None,
    cwd=None,
) -> StrictLaunchPlan:
    if bundle.execution_errors:
        raise ValidationError("; ".join(bundle.execution_errors))
    spec = workload or bundle.workload
    options = bundle.details["trial_options"]
    if options.get("setup") or options.get("cleanup"):
        raise ValidationError(
            "recipe evidence uses trial hooks; explicitly validate without hooks before single-launch replay"
        )
    if (
        options.get("workspace_mode") == "fixed_cwd"
        and Path(cwd or Path.cwd()).resolve() != Path(options["fixed_cwd"]).resolve()
    ):
        raise ValidationError("recipe requires its explicit fixed working directory")
    _compatible_manifest(bundle, spec)
    if topology_class(snapshot) != bundle.recipe.compatibility.topology_class:
        raise ValidationError(
            "recipe incompatible: topology relationships changed; explicit revalidation required"
        )
    policy = bundle.recipe.policy
    # A current reference is determined logically, without interpreting the old
    # absolute mask as an affinity request.
    budget = logical_budget(policy.budget)
    if budget.basis == BudgetBasis.RESOURCE_CAPPED:
        placement = (
            Placement.INHERITED
            if budget.cpu_count == len(snapshot.effective_cpu_ids or [])
            else Placement.COMPACT
        )
        reference = replace(policy, placement=placement, budget=budget)
        reference_plan = plan_strict_launch(reference, snapshot, capabilities)
        budget = replace(
            budget,
            reference_cpu_ids=list(reference_plan.cpu_ids),
            unavailable={k: v for k, v in budget.unavailable.items() if k != "reference_cpu_ids"},
        )
    plan = plan_strict_launch(replace(policy, budget=budget), snapshot, capabilities)
    if (
        topology_class(snapshot, list(plan.cpu_ids))
        not in bundle.details["selected_topology_classes"]
    ):
        raise ValidationError(
            "recipe incompatible: logical placement has no matching evidence topology"
        )
    for name in bundle.recipe.compatibility.required_capabilities:
        tool = capabilities.get("tools", {}).get(name, {})
        if not tool.get("available", bool(tool.get("path"))) or not tool.get("path"):
            raise ValidationError(f"recipe missing required capability: {name}")
    if any(
        key in spec.fixed_env and spec.fixed_env[key] != value for key, value in plan.env_overrides
    ):
        raise ValidationError("recipe settings conflict with fixed workload environment")
    recorded_runtime = bundle.details["runtime_environment"]
    current_runtime = runtime_environment(
        os.environ if base_env is None else base_env, spec.fixed_env
    )
    overrides = dict(plan.env_overrides)
    effective_runtime = {**current_runtime, **overrides}
    places = effective_runtime.get("OMP_PLACES")
    kmp = (effective_runtime.get("KMP_AFFINITY") or "").lower()
    if (
        effective_runtime.get("GOMP_CPU_AFFINITY")
        or places
        and places.lower() not in {"threads", "cores", "sockets", "ll_caches", "numa_domains"}
        or "proclist" in kmp
        or "explicit" in kmp
    ):
        raise ValidationError(
            "absolute/unsupported runtime affinity cannot be reused as logical placement; declare logical settings and revalidate"
        )
    if recorded_runtime is not None:
        if {**recorded_runtime, **overrides} != {**current_runtime, **overrides}:
            raise ValidationError(
                "recipe incompatible: inherited runtime settings changed; explicitly revalidate"
            )
    # The strict planner owns both allocation and selected-node feasibility,
    # including its immediate pre-launch refresh. A declared need establishes
    # neither an RSS limit nor reserved memory; unknown allocation caps stay unknown.
    return plan


@dataclass(frozen=True)
class ReplayResult:
    status: int
    record: dict[str, Any]
    directory: Path | None


@util.cancellable
def exec_recipe(
    recipe_path,
    argv,
    *,
    workload_path=None,
    case_id=None,
    allow_unvalidated=False,
    out=None,
    cwd=None,
    timeout=None,
    dry_run=False,
    snapshot_provider=None,
    capabilities=None,
    base_env=None,
) -> ReplayResult:
    """Exactly one strict invocation; compatibility failure launches zero times."""
    provider = snapshot_provider or search_snapshot
    bundle = load_recipe(recipe_path)
    spec = load_workload(workload_path).spec if workload_path is not None else bundle.workload
    directory = Path(cwd or Path.cwd()).resolve(strict=True)
    if not directory.is_dir():
        raise ValidationError("replay cwd is not a directory")
    command, case = _resolve_command(argv, spec, case_id, directory)
    snapshot = provider()
    caps = dict(capabilities) if capabilities is not None else search_capabilities(snapshot)
    plan = resolve_recipe(bundle, snapshot, caps, workload=spec, base_env=base_env, cwd=directory)
    requested_timeout = spec.limits.trial_timeout_seconds
    if timeout is not None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (float, int))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValidationError("replay timeout must be finite and positive")
        requested_timeout = min(requested_timeout, timeout)
    historical_validated = bundle.details["replay_validated"]
    hardware = compare_hardware(bundle.details.get("hardware", {"class_id": None}), snapshot)
    authorized = historical_validated and hardware["status"] == "matched"
    if not authorized and not allow_unvalidated:
        raise ValidationError(
            "validated reuse requires historical validation and known matching hardware class; "
            "explicitly revalidate, or use --allow-unvalidated after inspecting explain. "
            + hardware["explanation"]
        )
    # Even when replaying a validated recipe, this execution adds no scientific
    # validator or baseline. Applicability and historical validation are distinct.
    record = {
        "kind": "recipe_replay",
        "schema_version": 1,
        "id": "replay-" + uuid.uuid4().hex,
        "recipe_id": bundle.recipe.id,
        "recipe_sha256": sha256_file(bundle.path),
        "recipe_path": str(bundle.path),
        "label": "validated" if historical_validated else "unvalidated",
        "label_scope": "historical evidence only; this invocation's output is unchecked",
        "historical_validation": bundle.details["decision_report"]["historical_validation"],
        "historical_scientific_check_state": bundle.recipe.validation.state.value,
        "historical_protocol_valid": bundle.details["heldout_confirmed"],
        "historical_performance_outcome": bundle.recipe.performance_outcome.value,
        "hardware_class": hardware,
        "validated_reuse_authorized": authorized,
        "current_compatibility": "passed",
        "current_output_validation": "unchecked",
        "current_performance": "unproven; no baseline or validators run by exec",
        "allow_unvalidated": bool(allow_unvalidated),
        "case_id": case.id,
        "input_class": case.input_class,
        "argv": command,
        "cwd": str(directory),
        "compatibility_passed": True,
        "required_settings_passed": True,
        "plan": plan.to_dict(),
        "science": "unchecked: replay runs no validators",
        "status": "not_started",
        "return_code": None,
        "launches": 0,
        "started_at": None,
        "ended_at": None,
        "unavailable": {"return_code": "not launched", "ended_at": "not launched"},
    }
    print(
        f"simtune exec: historically {record['label']} recipe {bundle.recipe.id}; "
        f"current mechanical compatibility passed; hardware class {hardware['status']}; "
        f"validated reuse authorized: {authorized}; current output unchecked; current performance unproven",
        file=sys.stderr,
        flush=True,
    )
    if dry_run:
        return ReplayResult(0, record, None)
    root = Path(out) if out is not None else Path.cwd() / ".simtune" / "replays" / record["id"]
    root = root.absolute()
    if root.resolve().is_relative_to(
        artifact_path(bundle.path.parent, bundle.details["evidence_root"])
    ):
        raise ValidationError("replay logs must not modify source evidence")
    # Protect source recipe/evidence and workload directories from accidental log replacement.
    root.mkdir(parents=True, exist_ok=False)
    log = artifact_path(root, "replay.json")
    _atomic_json(log, record)
    print(
        f"simtune exec: record: {log}",
        file=sys.stderr,
        flush=True,
    )

    def checked_snapshot():
        fresh = provider()
        resolve_recipe(bundle, fresh, caps, workload=spec, base_env=base_env, cwd=directory)
        current_hardware = compare_hardware(
            bundle.details.get("hardware", {"class_id": None}), fresh
        )
        current_authorized = historical_validated and current_hardware["status"] == "matched"
        record.update(
            hardware_class=current_hardware, validated_reuse_authorized=current_authorized
        )
        if not current_authorized and not allow_unvalidated:
            raise ValidationError(
                "hardware class changed or became unknown before launch; explicit revalidation required"
            )
        return fresh

    def before_launch(requested):
        # Hash again after resource discovery and immediately before the one launch.
        _resolve_command(command, spec, case.id, directory)
        if sha256_file(bundle.path) != record["recipe_sha256"]:
            raise ValidationError("recipe changed before replay launch")
        record.update(status="running", launches=1, started_at=_utc_now())
        _atomic_json(log, record)
        print(
            f"simtune exec: current hardware class {record['hardware_class']['status']}; "
            f"validated reuse authorized: {record['validated_reuse_authorized']}; current output unchecked",
            file=sys.stderr,
            flush=True,
        )
        return requested

    try:
        result = execute_strict_launch(
            plan,
            command,
            caps,
            requested_timeout,
            base_env={**dict(os.environ if base_env is None else base_env), **spec.fixed_env},
            snapshot_provider=checked_snapshot,
            cwd=directory,
            before_launch=before_launch,
            passthrough=True,
        )
        code = result.metrics["return_code"]
        shell_status = 128 - code if code < 0 else code
        record.update(
            status="unknown"
            if not result.metrics.get("cancellation", {}).get("termination_known", True)
            else "interrupted"
            if result.metrics.get("cancellation", {}).get("parent_signal") is not None
            else "timed_out"
            if result.metrics["timed_out"]
            else "succeeded"
            if code == 0
            else "interrupted"
            if code < 0
            else "failed",
            return_code=code,
            shell_status=shell_status,
            metrics=result.metrics,
            actions=[a.to_dict() for a in result.actions],
            unobserved=list(result.unobserved),
            unavailable={},
        )
        if result.metrics.get("error"):
            print(f"simtune exec: {result.metrics['error']}", file=sys.stderr)
    except BaseException as exc:
        if record["launches"] == 0:
            record.update(
                compatibility_passed=False,
                required_settings_passed=False,
                current_compatibility="rejected",
            )
        record.update(
            status="interrupted"
            if isinstance(exc, KeyboardInterrupt)
            else "unknown"
            if record["launches"]
            else "rejected",
            error=str(exc),
            ended_at=_utc_now(),
        )
        _atomic_json(log, record)
        print(
            f"simtune exec: {record['status']}; current compatibility {record['current_compatibility']}; "
            f"hardware class {record['hardware_class']['status']}; current output unchecked",
            file=sys.stderr,
        )
        raise
    record["ended_at"] = _utc_now()
    _atomic_json(log, record)
    return ReplayResult(shell_status, record, root)


def validate_recipe(
    recipe_path, workload_path, out, *, config=None, dry_run=False, **search_kwargs
):
    """Fresh paired evidence for this policy only; source files stay immutable."""
    bundle = load_recipe(recipe_path)
    workload = load_workload(workload_path)
    if workload.spec.family != bundle.recipe.workload_family:
        raise ValidationError(
            "revalidation requires the declared workload family; use tune for another family"
        )
    if not workload.spec.validation_case_ids or not all(
        workload.spec.output_checks or c.checks for c in workload.spec.cases
    ):
        raise ValidationError(
            "revalidation requires scientific checks and independent held-out cases"
        )
    root = Path(out).absolute()
    derived_path = root.with_name(root.name + ".recipe.json")
    if not dry_run and (
        derived_path.exists()
        or derived_path.is_symlink()
        or derived_path.with_name(derived_path.name + ".evidence").exists()
    ):
        raise ValidationError(
            "derived recipe destination already exists; choose a new validation directory"
        )
    source_root = artifact_path(bundle.path.parent, bundle.details["evidence_root"])
    if root.resolve().is_relative_to(source_root) or root.resolve() == bundle.path:
        raise ValidationError("revalidation output must not modify source recipe/evidence")
    result = run_search(
        workload,
        root,
        config or SearchConfig(600),
        dry_run=dry_run,
        revalidate_policy=bundle.recipe.policy,
        source_recipe={
            "id": bundle.recipe.id,
            "sha256": sha256_file(bundle.path),
            "path": str(bundle.path),
            "record": bundle.recipe.to_dict(),
        },
        **search_kwargs,
    )
    derived = None
    if (
        not dry_run
        and not isinstance(result, dict)
        and result.completion_marker is not None
        and result.selected_policy_id is not None
    ):
        # Keep the complete experiment and a derived recipe as sibling artifacts.
        derived = derived_path
        save_recipe(root, derived)
    return result, derived


def add_parsers(subparsers):
    explain = subparsers.add_parser("explain", help="Explain recipe evidence and applicability")
    explain.add_argument("recipe")
    explain.add_argument("--json", action="store_true")
    explain.add_argument(
        "--workload", help="Check this manifest and the current resources without launching"
    )
    explain.set_defaults(func=cmd_explain)
    execute = subparsers.add_parser("exec", help="Replay a compatible recipe exactly once")
    execute.add_argument("--recipe", required=True)
    execute.add_argument("--allow-unvalidated", action="store_true")
    execute.add_argument(
        "--workload", help="Declare permitted new case values/input identity/runtime"
    )
    execute.add_argument("--case", help="Disambiguate a declared case ID")
    execute.add_argument("--out", help="New replay log directory (default: .simtune/replays/ID)")
    execute.add_argument("--cwd", help="Workload working directory (default: current directory)")
    execute.add_argument("--timeout", type=duration, help="Lower the saved workload timeout")
    execute.add_argument("--dry-run", action="store_true")
    execute.add_argument("cmd", nargs=argparse.REMAINDER)
    execute.set_defaults(func=cmd_exec)
    validate = subparsers.add_parser(
        "validate", help="Revalidate a recipe with fresh checked pairs"
    )
    validate.add_argument("--recipe", required=True)
    validate.add_argument("--workload", required=True)
    validate.add_argument(
        "--out", required=True, help="New experiment directory; derived recipe is DIR.recipe.json"
    )
    validate.add_argument(
        "--budget", type=duration, default=600, help="Total validation budget (default: 10m)"
    )
    validate.add_argument("--dry-run", action="store_true")
    validate.set_defaults(func=cmd_validate)


def cmd_explain(args):
    bundle = load_recipe(args.recipe, diagnostic=True)
    report = explain_recipe(bundle)
    if args.workload:
        applicability = report["applicability"]
        applicability["checked_on_current_host"] = True
        try:
            workload = load_workload(args.workload)
            snapshot = search_snapshot()
            plan = resolve_recipe(
                bundle, snapshot, search_capabilities(snapshot), workload=workload.spec
            )
            applicability.update(
                plan=plan.to_dict(),
                current_compatibility="passed",
                hardware_class=compare_hardware(
                    bundle.details.get("hardware", {"class_id": None}), snapshot
                ),
                explanation="Declared identity and current resource checks passed; no workload launched.",
            )
            applicability["validated_reuse_authorized"] = bool(
                bundle.details["replay_validated"]
                and applicability["hardware_class"]["status"] == "matched"
            )
        except (OSError, ValueError) as exc:
            applicability["current_compatibility"] = "rejected"
            applicability["failures"].append(str(exc))
            applicability["explanation"] = "Applicability check failed; no workload launched."
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
    else:
        recipe, details = bundle.recipe, report["measured_evidence"]
        print(
            f"{recipe.id}: historically {report['label']}; {recipe.performance_outcome.value}\n{recipe.explanation}"
        )
        print(f"Policy: {recipe.policy.id}; {recipe.policy.rationale}")
        print(
            f"Science: {recipe.validation.state.value}. Held-out confirmation: {details['heldout_confirmed']}"
        )
        from .reporting import render_decision_report

        print(render_decision_report(report["decision_report"]))
        print(
            f"Applicability: family={recipe.workload_family}; variables={recipe.variable_arguments}; input classes={recipe.compatibility.input_classes}; runtime={recipe.compatibility.runtime.name}; topology={recipe.compatibility.topology_class}"
        )
        print("Resources: " + json.dumps(recipe.resource_constraints.to_dict()))
        print("Required capabilities: " + ", ".join(recipe.compatibility.required_capabilities))
        for limitation in details["limitations"]:
            print("Limitation: " + limitation)
        print(report["applicability"]["explanation"])
        print(
            "Current mechanical compatibility: " + report["applicability"]["current_compatibility"]
        )
        print("Hardware class: " + report["applicability"]["hardware_class"]["status"])
        print(
            f"Validated reuse authorized: {report['applicability']['validated_reuse_authorized']}"
        )
        if "explanation" in report["applicability"]["hardware_class"]:
            print(report["applicability"]["hardware_class"]["explanation"])
        print("Current output: " + report["applicability"]["current_output_validation"])
        for failure in report["applicability"]["failures"]:
            print(failure)
        for error in report["execution_errors"]:
            print("Execution rejected: " + error)
    return 2 if report["applicability"]["failures"] or report["execution_errors"] else 0


def cmd_exec(args):
    argv = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    result = exec_recipe(
        args.recipe,
        argv,
        workload_path=args.workload,
        case_id=args.case,
        allow_unvalidated=args.allow_unvalidated,
        out=args.out,
        cwd=args.cwd,
        timeout=args.timeout,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        print(json.dumps(result.record, indent=2, ensure_ascii=False, allow_nan=False))
    return result.status


def cmd_validate(args):
    result, derived = validate_recipe(
        args.recipe, args.workload, args.out, config=SearchConfig(args.budget), dry_run=args.dry_run
    )
    if args.dry_run:
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    if isinstance(result, dict):
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        signum = result.get("parent_signal")
        return result.get("interruption_exit_status") or (128 + signum if signum is not None else 1)
    print(f"{result.outcome.value}: {result.explanation}")
    print(f"Fresh evidence: {args.out}; derived recipe: {derived or 'unavailable'}")
    if result.outcome == Outcome.INTERRUPTED:
        return result.extensions.get("simtune.stage05", {}).get("interruption_exit_status") or 130
    if "timed out" in result.explanation:
        return 124
    return 0 if result.outcome in {Outcome.NO_IMPROVEMENT, Outcome.VALIDATED_IMPROVEMENT} else 1
