"""CLI adapter for measured search; legacy ``run --mode auto`` is unchanged."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import LABEL, ProjectConfig, load_project_config
from .experiments import TrialExecutionOptions
from .importers import load_imported_profile
from .lookup import find_run
from .models import (
    BudgetBasis,
    CaseSpec,
    Outcome,
    ResourceBudget,
    RuntimeIdentity,
    WorkloadLimits,
    WorkloadSpec,
)
from .search import SearchConfig, resume_search, run_search
from .validation import ValidationError, load_json
from .workloads import LoadedWorkload, _canonical_sha256, load_workload, sha256_file


def duration(value: str) -> float:
    units = {"s": 1, "m": 60, "h": 3600}
    try:
        number = float(value[:-1]) * units[value[-1]] if value[-1:] in units else float(value)
    except (ValueError, IndexError) as exc:
        raise argparse.ArgumentTypeError(
            "budget must be positive seconds or a duration such as 10m"
        ) from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("budget must be finite and positive")
    return number


def bare_workload(
    argv: list[str], *, timeout: float, max_trials: int, max_launches: int
) -> LoadedWorkload:
    if not argv:
        raise ValidationError("No command provided; use --workload or -- COMMAND")
    executable = shutil.which(argv[0])
    if executable is None:
        raise ValidationError(f"executable not found: {argv[0]!r}")
    executable = str(Path(executable).resolve(strict=True))
    budget = ResourceBudget(
        basis=BudgetBasis.INHERITED,
        cpu_count=None,
        thread_count=None,
        memory_bytes=None,
        reference_cpu_ids=None,
        rationale="Keep the inherited resources for this unchanged command.",
        unavailable={
            name: "inherited, not explicitly constrained"
            for name in ("cpu_count", "thread_count", "memory_bytes", "reference_cpu_ids")
        },
    )
    case = CaseSpec(
        id="input",
        input_class="bare-command",
        arguments={},
        input_artifacts=[],
        weight=1.0,
        checks=[],
        resource_needs=budget,
        regression_tolerance=0.0,
    )
    spec = WorkloadSpec(
        id="bare-command",
        family="bare-command",
        argv_template=[executable, *argv[1:]],
        resolved_executable=executable,
        executable_sha256=sha256_file(executable),
        artifacts=[],
        base_directory=str(Path.cwd().resolve()),
        runtime=RuntimeIdentity(
            name="undeclared",
            version=None,
            identity=None,
            unavailable={"version": "not declared", "identity": "not declared"},
        ),
        fixed_env={},
        case_substitutions=[],
        cases=[case],
        training_case_ids=[case.id],
        validation_case_ids=[],
        output_checks=[],
        limits=WorkloadLimits(
            trial_timeout_seconds=timeout, max_trials=max_trials, max_launches=max_launches
        ),
        extensions={"simtune.literal_argv": True},
    )
    artifacts = (("executable", Path(executable)),)
    fingerprint = _canonical_sha256(
        {
            "workload": spec.to_dict(),
            "resolved_artifacts": [{"id": name, "path": str(path)} for name, path in artifacts],
            "declared_environment": {},
        }
    )
    return LoadedWorkload(
        spec=spec,
        manifest_path=Path.cwd() / "bare-command.json",
        artifact_paths=artifacts,
        workload_fingerprint=fingerprint,
        relevant_environment=(),
        untracked_external_dependencies=(
            "Bare command: unchecked and input-specific; only executable identity is pinned.",
        ),
    )


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("tune", help="Measure strict candidates with a bounded search")
    parser.add_argument("--workload", help="Strict TOML/JSON workload with scientific checks")
    parser.add_argument(
        "--config", help="Explicit TOML config (default: ./simtune.toml if present)"
    )
    parser.add_argument(
        "--show-config", action="store_true", help="Show effective tuning options and sources"
    )
    parser.add_argument("--json", action="store_true", help="Emit one JSON result on stdout")
    parser.add_argument("--label", help="Safe label for the default run ID")
    parser.add_argument(
        "--budget",
        type=duration,
        help="Total tuning time (e.g. 30s, 10m); additional time on resume",
    )
    parser.add_argument("--out", help="New experiment directory")
    parser.add_argument("--save", help="Save a new explainable recipe and its evidence bundle")
    parser.add_argument(
        "--resume",
        metavar="EXPERIMENT_DIR",
        help="Continue only never-started work using unspent authorization",
    )
    parser.add_argument(
        "--repeat", type=int, default=None, help="Exploration pairs per case/candidate (default: 3)"
    )
    parser.add_argument(
        "--warmups",
        type=int,
        default=None,
        help="Warmup pairs per training case/candidate (default: 1)",
    )
    parser.add_argument(
        "--seed", type=int, default=None, help="Schedule and bootstrap seed (default: 0)"
    )
    parser.add_argument(
        "--confirmation-pairs",
        type=int,
        default=None,
        help="Fresh held-out pairs per case (minimum: 5)",
    )
    parser.add_argument(
        "--confirmation-fraction",
        type=float,
        default=None,
        help="Time reserved for confirmation (default: 0.5)",
    )
    parser.add_argument(
        "--minimum-improvement",
        type=float,
        default=None,
        help="Required relative time reduction (default: 0.02)",
    )
    parser.add_argument(
        "--max-trials", type=int, help="Lower trial cap; manifest limit remains a ceiling"
    )
    parser.add_argument(
        "--max-launches", type=int, help="Lower subprocess cap, including validators and hooks"
    )
    parser.add_argument(
        "--timeout", type=duration, default=None, help="Bare-command trial timeout (default: 60s)"
    )
    parser.add_argument(
        "--from-run", help="Existing or imported profile as identity-checked hints only"
    )
    parser.add_argument(
        "--collectors",
        choices=("timing", "perf-stat"),
        default=None,
        help="Optional trial diagnostics (default: timing, no external tool)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show protocol; launch no processes and create no evidence directory",
    )
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    parser.set_defaults(func=cmd_tune)


def cmd_tune(args: argparse.Namespace) -> int:
    argv = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if args.resume and args.config:
        raise ValidationError("--resume uses the saved protocol; --config is incompatible")
    config = ProjectConfig(None, {}) if args.resume else load_project_config(args.config)
    sources: dict[str, str] = {}
    for key, value in config.tune.items():
        if getattr(args, key) is None:
            if key in {"budget", "timeout"}:
                try:
                    value = duration(value)
                except argparse.ArgumentTypeError as exc:
                    raise ValidationError(f"invalid [tune].{key}: {exc}") from exc
            setattr(args, key, value)
            sources[key] = str(config.path)
    for key in (
        "workload",
        "budget",
        "out",
        "save",
        "repeat",
        "warmups",
        "seed",
        "confirmation_pairs",
        "confirmation_fraction",
        "minimum_improvement",
        "max_trials",
        "max_launches",
        "timeout",
        "from_run",
        "label",
        "collectors",
    ):
        if key not in sources and getattr(args, key) is not None:
            sources[key] = "CLI"
    if args.label is not None and not LABEL.fullmatch(args.label):
        raise ValidationError("label must be a safe ASCII name of at most 64 characters")
    if args.show_config:
        if argv or args.resume or args.dry_run:
            raise ValidationError("--show-config cannot launch, dry-run or resume")
        defaults = {
            "out": "runs/search-DATE-LABEL-ID",
            "repeat": 3,
            "warmups": 1,
            "seed": 0,
            "confirmation_pairs": 5,
            "confirmation_fraction": 0.5,
            "minimum_improvement": 0.02,
            "timeout": 60,
            "collectors": "timing",
        }
        effective = {
            key: {
                "value": getattr(args, key)
                if getattr(args, key) is not None
                else defaults.get(key),
                "source": sources.get(key, "default"),
            }
            for key in (
                "workload",
                "budget",
                "out",
                "save",
                "repeat",
                "warmups",
                "seed",
                "confirmation_pairs",
                "confirmation_fraction",
                "minimum_improvement",
                "max_trials",
                "max_launches",
                "timeout",
                "from_run",
                "label",
                "collectors",
            )
        }
        print(
            json.dumps(
                {
                    "kind": "simtune_config",
                    "schema_version": 1,
                    "config_path": str(config.path) if config.path else None,
                    "tune": effective,
                },
                indent=2,
                ensure_ascii=False,
                allow_nan=False,
            )
        )
        return 0
    if argv and args.workload:
        raise ValidationError("provide exactly one of --workload or -- COMMAND")
    run_id = None
    if not args.resume:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        run_id = f"search-{stamp}-{args.label or 'run'}-{uuid.uuid4().hex[:8]}"
        if args.out is None:
            args.out = str(Path("runs") / run_id)
    if args.resume:
        overrides = (
            "repeat",
            "warmups",
            "seed",
            "confirmation_pairs",
            "confirmation_fraction",
            "minimum_improvement",
            "max_trials",
            "max_launches",
            "timeout",
            "label",
        )
        if (
            args.workload
            or args.out
            or argv
            or args.from_run
            or args.collectors is not None
            or args.dry_run
            or any(getattr(args, name) is not None for name in overrides)
        ):
            raise ValidationError(
                "--resume accepts only the saved protocol and an optional additional --budget"
            )
        out = str(find_run(args.resume))
        print(f"Resuming {out}; additional authorization: {args.budget or 0:.3f}s", file=sys.stderr)
        result = resume_search(out, additional_seconds=args.budget or 0, progress=_progress)
    else:
        if args.budget is None:
            raise ValidationError("tune requires --budget")
        if bool(args.workload) == bool(argv):
            raise ValidationError("provide exactly one of --workload or -- COMMAND")
        config = SearchConfig(
            budget_seconds=args.budget,
            repeat=3 if args.repeat is None else args.repeat,
            warmups=1 if args.warmups is None else args.warmups,
            seed=0 if args.seed is None else args.seed,
            confirmation_pairs=5 if args.confirmation_pairs is None else args.confirmation_pairs,
            confirmation_fraction=0.5
            if args.confirmation_fraction is None
            else args.confirmation_fraction,
            minimum_time_reduction=0.02
            if args.minimum_improvement is None
            else args.minimum_improvement,
            max_trials=args.max_trials,
            max_launches=args.max_launches,
        )
        # Manifest hashing/loading and hint reads occur inside the tuning deadline.
        hint = {}

        def load() -> LoadedWorkload:
            loaded = (
                load_workload(args.workload)
                if args.workload
                else bare_workload(
                    argv,
                    timeout=args.timeout or 60,
                    max_trials=args.max_trials or 100,
                    max_launches=args.max_launches or 300,
                )
            )
            if args.from_run:
                root = Path(args.from_run)
                if (root / "profile.json").exists():
                    profile = load_imported_profile(root)
                    context = profile["context"]
                    if (
                        context["command"] != loaded.spec.argv_template
                        or context["fixed_env"] != loaded.spec.fixed_env
                        or context["input_identity"] != loaded.workload_fingerprint
                        or context["workload_fingerprint"] != loaded.workload_fingerprint
                        or context["environment_fingerprint"] is None
                    ):
                        raise ValidationError(
                            "imported profile command, input, fixed environment or fingerprints do not match this workload"
                        )
                    hint.update(profile)
                    hint.update(
                        workload_fingerprint=context["workload_fingerprint"],
                        environment_fingerprint=context["environment_fingerprint"],
                    )
                else:
                    meta = load_json((root / "meta.json").read_text())
                    hint.update(
                        workload_fingerprint=meta.get("workload_fingerprint"),
                        environment_fingerprint=meta.get("environment_fingerprint"),
                        metrics=load_json((root / "metrics.json").read_text()),
                    )
            return loaded

        if not args.dry_run:
            if not args.workload:
                print(
                    "Bare-command tuning is scientifically unchecked and input-specific; "
                    "it cannot select an eligible winner. Use a checked workload manifest; "
                    "see examples/HEAT_EXPERIMENT.md and docs/experiments.md for the checked example.",
                    file=sys.stderr,
                )
            print(f"Discovering resources and preparing search: {args.out}", file=sys.stderr)
        result = run_search(
            load,
            args.out,
            config,
            dry_run=args.dry_run,
            profile_hint=hint if args.from_run else None,
            options=TrialExecutionOptions(collectors=args.collectors or "timing"),
            experiment_id=run_id,
            progress=None if args.dry_run else _progress,
        )
        if args.dry_run:
            result["kind"] = "simtune_tune_dry_run"
            result["schema_version"] = 1
            result.update(_recipe_export_metadata(args.save, "dry_run"))
            print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
            return 0
        out = args.out
    if isinstance(result, dict):
        # Collector preparation can stop before an experiment store exists.
        result.update(_recipe_export_metadata(args.save, "experiment_not_started"))
        if args.json:
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        else:
            print(f"{result['outcome']}: {result['explanation']}")
            print(result["remedy"])
        signum = result.get("parent_signal")
        return result.get("interruption_exit_status") or (128 + signum if signum is not None else 1)
    export = _recipe_export_metadata(
        args.save,
        "incomplete_experiment" if result.completion_marker is None else "no_selected_policy",
    )
    if args.save:
        from .recipes import save_recipe

        if result.completion_marker is None or result.selected_policy_id is None:
            print(
                "Recipe not saved: experiment is incomplete or has no supported selected policy.",
                file=sys.stderr,
            )
        else:
            save_recipe(out, args.save)
            export["recipe_path"] = args.save
            export["recipe_export"].update(status="saved", reason=None)
            print(f"Recipe saved: {args.save}", file=sys.stderr if args.json else sys.stdout)
    state = result.extensions["simtune.stage05"]
    from .reporting import decision_report, render_decision_report

    report = decision_report(out)
    if args.json:
        print(
            json.dumps(
                {
                    "kind": "simtune_tune_result",
                    "schema_version": 1,
                    "id": result.id,
                    "path": str(Path(out).resolve()),
                    "outcome": result.outcome.value,
                    "explanation": result.explanation,
                    "selected_policy_id": result.selected_policy_id,
                    "label": state["label"],
                    "budget": result.budget.to_dict(),
                    "candidates": [item.to_dict() for item in result.candidates],
                    "notes": state["notes"],
                    **export,
                    "decision_report": report,
                },
                ensure_ascii=False,
                allow_nan=False,
            )
        )
        return _status(result)
    print(render_decision_report(report))
    label = "Selected" if result.outcome == Outcome.VALIDATED_IMPROVEMENT else "Reference retained"
    print(f"{label}: {result.selected_policy_id or 'unsupported'}; {state['label']}")
    for candidate in result.candidates:
        if candidate.exclusion_reason is not None:
            print(f"Excluded {candidate.policy_id}: {candidate.exclusion_reason}")
    for note in state["notes"]:
        print(note)
    print(
        f"Budget: {result.budget.spent_seconds:.3f}s; {result.budget.trials_started} trials; "
        f"{result.budget.launches_started} launches. Evidence: {out}"
    )
    print(f"Next: simtune show {out}")
    return _status(result)


def _recipe_export_metadata(requested_path: str | None, skip_reason: str) -> dict:
    """Describe this invocation only; an existing destination proves no export."""
    return {
        "recipe_path": None,
        "recipe_export": {
            "status": "skipped" if requested_path else "not_requested",
            "reason": skip_reason if requested_path else None,
            "requested_path": requested_path,
        },
    }


def _progress(phase: str, completed: int, total: int, remaining: float) -> None:
    print(
        f"{phase}: {completed}/{total} scheduled trials; {remaining:.1f}s available",
        file=sys.stderr,
    )


def _status(result) -> int:
    if result.outcome == Outcome.INTERRUPTED:
        return result.extensions.get("simtune.stage05", {}).get("interruption_exit_status") or 130
    if "timed out" in result.explanation:
        return 124
    return 0 if result.outcome in {Outcome.VALIDATED_IMPROVEMENT, Outcome.NO_IMPROVEMENT} else 1
