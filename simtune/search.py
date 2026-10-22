"""Small deterministic, budgeted search over strict isolated trials.

Training proposes exactly one challenger. Only fresh held-out confirmation may
replace the reference. State lives in the experiment's versioned extensions;
there is no subprocess, journal, or recipe implementation in this module.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import random
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import util
from .candidates import CandidateSet, generate_candidates, revalidation_candidates
from .collectors import probe as probe_collector
from .experiments import (
    BudgetController,
    BudgetExhausted,
    ExperimentStore,
    TrialExecutionOptions,
    TrialExecutor,
    TrialRequest,
    TrialState,
    new_experiment_record,
)
from .launch import StrictPlanningError
from .models import (
    Candidate,
    CheckKind,
    ExperimentRecord,
    Outcome,
    Phase,
    PolicySpec,
    ResourceSnapshot,
    TrialRecord,
    TrialStatus,
    ValidationState,
)
from .statistics import break_even_runs, confirm_policy, score_policy
from .topology import TopologyPaths, discover_resource_snapshot
from .validation import ValidationError, artifact_path
from .workloads import LoadedWorkload, environment_fingerprint, load_workload, runtime_environment

STATE_KEY = "simtune.stage05"


@dataclass(frozen=True)
class SearchConfig:
    budget_seconds: float
    repeat: int = 3
    warmups: int = 1
    seed: int = 0
    confirmation_pairs: int = 5
    confirmation_fraction: float = 0.5
    minimum_time_reduction: float = 0.02
    bootstrap_samples: int = 2000
    cleanup_allowance_seconds: float = 1.0
    max_trials: int | None = None
    max_launches: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "budget_seconds",
            "confirmation_fraction",
            "minimum_time_reduction",
            "cleanup_allowance_seconds",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValidationError(f"{name} must be finite numeric data")
        if self.budget_seconds <= 0 or not 0 < self.confirmation_fraction < 1:
            raise ValidationError(
                "budget must be positive and confirmation fraction between 0 and 1"
            )
        if not 0 <= self.minimum_time_reduction < 1 or self.cleanup_allowance_seconds < 0:
            raise ValidationError("invalid improvement threshold or cleanup allowance")
        for name, minimum in (
            ("repeat", 1),
            ("warmups", 0),
            ("seed", 0),
            ("confirmation_pairs", 5),
            ("bootstrap_samples", 100),
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < minimum:
                raise ValidationError(f"{name} must be an integer at least {minimum}")
        for name in ("max_trials", "max_launches"):
            if getattr(self, name) is not None and (
                type(getattr(self, name)) is not int or getattr(self, name) < 1
            ):
                raise ValidationError(f"{name} must be a positive integer")


def search_snapshot() -> ResourceSnapshot:
    # Explicit filesystem paths disable the topology module's automatic optional
    # hwloc subprocess. Search does no unbudgeted capability/version probes.
    return discover_resource_snapshot(paths=TopologyPaths())


def search_capabilities(snapshot: ResourceSnapshot) -> dict[str, Any]:
    return {
        "platform": snapshot.platform.value,
        "tools": {
            name: {
                "path": shutil.which(name),
                "available": shutil.which(name) is not None,
                "version": None,
            }
            for name in ("taskset", "numactl")
        },
    }


def _same_case_resources(workload: LoadedWorkload) -> None:
    fields = ("basis", "cpu_count", "thread_count", "memory_bytes", "reference_cpu_ids")
    first = workload.spec.cases[0].resource_needs
    if any(
        any(getattr(case.resource_needs, key) != getattr(first, key) for key in fields)
        for case in workload.spec.cases
    ):
        raise ValidationError("stage 05 requires the same declared resources across all cases")


def paired_schedule(
    *,
    baseline: PolicySpec,
    candidates: Sequence[PolicySpec],
    case_ids: Sequence[str],
    phase: Phase,
    pairs: int,
    warmups: int,
    seed: int,
    start_order: int = 0,
    options: TrialExecutionOptions = TrialExecutionOptions(),
) -> list[TrialRequest]:
    """Balanced AB/BA per candidate/case (odd counts differ by one), seeded."""
    rng = random.Random(seed)
    requests: list[TrialRequest] = []
    for candidate_index, candidate in enumerate(candidates):
        for case_index, case_id in enumerate(case_ids):
            prefix = f"{phase.value}-p{candidate_index}-c{case_index}"
            for warmup in range(warmups):
                order = [baseline, candidate]
                if rng.randrange(2):
                    order.reverse()
                for policy in order:
                    number = start_order + len(requests)
                    requests.append(
                        TrialRequest(
                            id=f"t{number:06d}",
                            case_id=case_id,
                            policy=policy,
                            phase=Phase.WARMUP,
                            order=number,
                            pair_id=None,
                            options=options,
                        )
                    )
            orders = [index % 2 == 0 for index in range(pairs)]
            if rng.randrange(2):
                orders = [not value for value in orders]
            rng.shuffle(orders)
            for pair_index, baseline_first in enumerate(orders):
                order = [baseline, candidate] if baseline_first else [candidate, baseline]
                for policy in order:
                    number = start_order + len(requests)
                    requests.append(
                        TrialRequest(
                            id=f"t{number:06d}",
                            case_id=case_id,
                            policy=policy,
                            phase=phase,
                            order=number,
                            pair_id=f"{prefix}-n{pair_index}",
                            options=options,
                        )
                    )
    return requests


def _request_data(request: TrialRequest) -> dict[str, Any]:
    return {
        "id": request.id,
        "case_id": request.case_id,
        "policy_id": request.policy.id,
        "phase": request.phase.value,
        "order": request.order,
        "pair_id": request.pair_id,
    }


def _schedule_data(requests: Sequence[TrialRequest], reference_id: str) -> list[dict[str, Any]]:
    result = []
    for index in range(0, len(requests), 2):
        pair = requests[index : index + 2]
        comparison = next(
            request.policy.id for request in pair if request.policy.id != reference_id
        )
        result.extend(
            {**_request_data(request), "comparison_policy_id": comparison} for request in pair
        )
    return result


def _exclude(store: ExperimentStore, policy_id: str, reason: str) -> None:
    store.update_record(
        replace(
            store.record,
            candidates=[
                Candidate(policy_id=policy_id, exclusion_reason=reason)
                if candidate.policy_id == policy_id
                else candidate
                for candidate in store.record.candidates
            ],
        )
    )


def _state(store: ExperimentStore) -> dict[str, Any]:
    return dict(store.record.extensions[STATE_KEY])


def _save(store: ExperimentStore, budget: BudgetController, **changes: Any) -> None:
    state = json.loads(json.dumps({**_state(store), **changes}, allow_nan=False))
    store.update_record(
        replace(
            store.record,
            budget=budget.ledger(),
            extensions={**store.record.extensions, STATE_KEY: state},
        )
    )


class _SearchLease:
    def __init__(self, root: Path) -> None:
        self.path = artifact_path(root, "search.lock")

    def __enter__(self) -> None:
        self.stream = self.path.open("a+")
        try:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            raise ValidationError("another search owns this experiment") from exc

    def __exit__(self, *args: Any) -> None:
        fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()


def _read_trials(store: ExperimentStore) -> list[TrialRecord]:
    recovered = store.recover()
    if recovered.ignored_partial_line:
        raise ValidationError("partial journal requires inspection; no uncertain trial is retried")
    state = _state(store)
    schedule = state["schedule"]
    by_id = {item["id"]: item for item in schedule}
    if len(by_id) != len(schedule):
        raise ValidationError("saved schedule has duplicate trial IDs")
    records = []
    evidence_owners: dict[tuple[int, int], str] = {}
    for trial_id, trial_state in recovered.states.items():
        if trial_state in {TrialState.NOT_STARTED, TrialState.RUNNING, TrialState.UNCERTAIN}:
            raise ValidationError(f"uncertain trial {trial_id}; automatic resume refused")
        record = TrialRecord.from_json(
            artifact_path(store.root, f"trials/{trial_id}/trial.json").read_text(), root=store.root
        )
        if (
            record.status.value != trial_state.value
            or record.id != trial_id
            or record.experiment_id != store.record.id
        ):
            raise ValidationError(f"journal/record identity or status mismatch for {trial_id}")
        expected = by_id.get(trial_id)
        if expected is None or any(
            getattr(record, field) != expected[field]
            for field in ("case_id", "policy_id", "phase", "order", "pair_id")
        ):
            raise ValidationError(f"trial {trial_id} does not match its saved schedule")
        evidence_root = artifact_path(store.root, f"trials/{trial_id}")
        sources = {
            *record.raw_artifacts,
            *record.validation.evidence,
            *(metric.raw_source for metric in record.metrics if metric.raw_source is not None),
            *(source for action in record.actions for source in action.evidence),
        }
        for source in sources:
            path = artifact_path(store.root, source)
            if not path.is_file() or not path.is_relative_to(evidence_root):
                raise ValidationError(f"missing or reused evidence for trial {trial_id}: {source}")
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity in evidence_owners and evidence_owners[identity] != trial_id:
                raise ValidationError(f"reused evidence for trial {trial_id}: {source}")
            evidence_owners[identity] = trial_id
        records.append(record)
    existing = {record.id for record in records}
    skipped = state.get("skipped_trials", {})
    cursor = state["next_index"]
    if type(cursor) is not int or not 0 <= cursor <= len(schedule):
        raise ValidationError("invalid saved schedule cursor")
    if any(item["id"] not in existing and item["id"] not in skipped for item in schedule[:cursor]):
        raise ValidationError("saved cursor omits unfinished trial evidence")
    return records


def _finish(
    store: ExperimentStore,
    budget: BudgetController,
    outcome: Outcome,
    explanation: str,
    *,
    selected: str | None = None,
    resumable: bool = False,
    saved_seconds: float | None = None,
    interruption_exit_status: int | None = None,
) -> ExperimentRecord:
    state = _state(store)
    reference = state["reference_policy_id"]
    selected = selected or reference
    included = {item.policy_id for item in store.record.candidates if item.exclusion_reason is None}
    if selected not in included:
        selected = None
    unavailable = dict(store.record.unavailable)
    if selected is None:
        unavailable["selected_policy_id"] = "reference could not be resolved; no policy selected"
    else:
        unavailable.pop("selected_policy_id", None)
    completed = not resumable and outcome != Outcome.INTERRUPTED
    if completed:
        unavailable.pop("completion_marker", None)
    state.update(
        parent_signal=util.cancellation_signal(),
        interruption_exit_status=interruption_exit_status
        if interruption_exit_status is not None
        else 128 + util.cancellation_signal()
        if util.cancellation_signal() is not None
        else 130
        if outcome == Outcome.INTERRUPTED
        else None,
        phase="done" if completed else state["phase"],
        resumable=resumable,
        tuning_seconds=budget.spent_seconds,
        # Conservatively charge the entire tuning job, including reference runs.
        additional_tuning_seconds=budget.spent_seconds,
        expected_case_mix="normalized held-out case weights declared before execution",
        validated_saved_seconds=saved_seconds if outcome == Outcome.VALIDATED_IMPROVEMENT else None,
        break_even_runs=break_even_runs(budget.spent_seconds, saved_seconds)
        if outcome == Outcome.VALIDATED_IMPROVEMENT
        else None,
    )
    store.update_record(
        replace(
            store.record,
            outcome=outcome,
            explanation=explanation,
            selected_policy_id=selected,
            budget=budget.ledger(),
            completion_marker="completion.json" if completed else None,
            unavailable=unavailable,
            extensions={**store.record.extensions, STATE_KEY: state},
        )
    )
    if completed:
        return store.complete(explanation)
    return store.record


def _launches_per_trial(
    workload: LoadedWorkload, case_id: str, options: TrialExecutionOptions
) -> int:
    checks = [*workload.spec.output_checks, *workload.case(case_id).checks]
    return (
        1
        + int(options.setup is not None)
        + int(options.cleanup is not None)
        + sum(check.method == CheckKind.COMMAND for check in checks)
    )


def _checked_snapshot(
    provider: Callable[[], ResourceSnapshot],
    workload: LoadedWorkload,
    caps: Mapping[str, Any],
    expected: str,
) -> ResourceSnapshot:
    snapshot = provider()
    if environment_fingerprint(workload, snapshot, caps) != expected:
        raise StrictPlanningError(
            code="resource_changed",
            setting="environment",
            requested=expected,
            restriction="environment fingerprint changed during the paired experiment",
            remedy="start a separate experiment in the new environment",
        )
    return snapshot


def _continue(
    store: ExperimentStore,
    workload: LoadedWorkload,
    budget: BudgetController,
    capabilities: Mapping[str, Any],
    snapshot_provider: Callable[[], ResourceSnapshot],
    *,
    clock: Callable[[], float],
    options: TrialExecutionOptions,
    executor_kwargs: Mapping[str, Any] | None,
    progress: Callable[[str, int, int, float], None] | None = None,
) -> ExperimentRecord:
    state = _state(store)
    config = SearchConfig(**state["config"])
    recorded_runtime = state.get("runtime_environment")
    current_runtime = runtime_environment(
        dict(executor_kwargs or {}).get("base_env", os.environ), workload.spec.fixed_env
    )
    if recorded_runtime is not None and recorded_runtime != current_runtime:
        raise ValidationError("declared runtime environment changed; start a new experiment")
    policies = {value["id"]: PolicySpec.from_dict(value) for value in state["policies"]}
    reference = policies[state["reference_policy_id"]]
    eligible = [
        policies[item.policy_id]
        for item in store.record.candidates
        if item.exclusion_reason is None and item.policy_id != reference.id
    ]
    if reference.id not in {
        item.policy_id for item in store.record.candidates if item.exclusion_reason is None
    }:
        return _finish(
            store, budget, Outcome.INCONCLUSIVE, "Reference is unsupported; no workload launched."
        )
    if state.get("protocol_unfunded_reason"):
        return _finish(store, budget, Outcome.INCONCLUSIVE, state["protocol_unfunded_reason"])
    if not eligible:
        return _finish(
            store,
            budget,
            Outcome.INCONCLUSIVE,
            "All distinct candidates are unavailable. Reference retained; no improvement evidence.",
        )
    executor = TrialExecutor(
        store=store,
        workload=workload,
        capabilities=capabilities,
        budget=budget,
        clock=clock,
        snapshot_provider=lambda: _checked_snapshot(
            snapshot_provider, workload, capabilities, store.record.environment_fingerprint
        ),
        **dict(executor_kwargs or {}),
    )
    records = _read_trials(store)
    existing = {trial.id: trial for trial in records}
    # A terminal append may survive a crash before the experiment-ID list update.
    # Reconstruct these references from validated journal records, never timing guesses.
    store.update_record(
        replace(
            store.record,
            training_trial_ids=[trial.id for trial in records if trial.phase == Phase.TRAINING],
            confirmation_trial_ids=[
                trial.id for trial in records if trial.phase == Phase.CONFIRMATION
            ],
        )
    )
    for trial in records:
        if trial.status == TrialStatus.TIMED_OUT:
            return _finish(
                store,
                budget,
                Outcome.INCONCLUSIVE,
                "A recovered trial timed out; reference retained without further launches.",
            )
        if trial.policy_id == reference.id and (
            trial.status != TrialStatus.SUCCEEDED
            or trial.validation.state == ValidationState.FAILED
        ):
            return _finish(
                store,
                budget,
                Outcome.FAILED,
                "Recovered reference execution or scientific validation failed; comparison stopped.",
            )
        if trial.policy_id != reference.id and (
            trial.status != TrialStatus.SUCCEEDED
            or trial.validation.state == ValidationState.FAILED
        ):
            _exclude(
                store,
                trial.policy_id,
                f"Ineligible recovered trial {trial.id}: {trial.status_reason}",
            )
    if state.get("pending_trial_id") and state["pending_trial_id"] not in existing:
        raise ValidationError("pending trial has no terminal evidence; automatic retry refused")
    while True:
        state = _state(store)
        schedule = state["schedule"]
        for index in range(state["next_index"], len(schedule)):
            if util.cancellation_signal() is not None:
                return _finish(
                    store,
                    budget,
                    Outcome.INTERRUPTED,
                    "Parent cancelled search; no further subprocess is authorized.",
                )
            data = schedule[index]
            if data["id"] in existing:
                _save(store, budget, next_index=index + 1, pending_trial_id=None)
                continue
            excluded = {
                item.policy_id: item.exclusion_reason
                for item in store.record.candidates
                if item.exclusion_reason is not None
            }
            if data["comparison_policy_id"] in excluded:
                skipped = dict(_state(store).get("skipped_trials", {}))
                skipped[data["id"]] = excluded[data["comparison_policy_id"]]
                _save(
                    store,
                    budget,
                    next_index=index + 1,
                    pending_trial_id=None,
                    skipped_trials=skipped,
                )
                continue
            # Persist intent before TrialExecutor can create any side effect.
            _save(store, budget, pending_trial_id=data["id"])
            request = TrialRequest(
                id=data["id"],
                case_id=data["case_id"],
                policy=policies[data["policy_id"]],
                phase=Phase(data["phase"]),
                order=data["order"],
                pair_id=data["pair_id"],
                options=options,
            )
            try:
                record = executor.run(request)
            except BudgetExhausted as exc:
                _save(store, budget, pending_trial_id=None)
                return _finish(
                    store,
                    budget,
                    Outcome.INCONCLUSIVE,
                    f"Protocol incomplete: {exc}. Reference retained.",
                    resumable=True,
                )
            except KeyboardInterrupt:
                return _finish(
                    store,
                    budget,
                    Outcome.INTERRUPTED,
                    "Interrupted; inspect durable trial state before continuing.",
                )
            records.append(record)
            existing[record.id] = record
            if record.policy_id != reference.id and (
                record.status != TrialStatus.SUCCEEDED
                or record.validation.state == ValidationState.FAILED
            ):
                _exclude(
                    store,
                    record.policy_id,
                    f"Ineligible after {record.id}: {record.status_reason}; {record.validation.explanation}",
                )
            _save(store, budget, next_index=index + 1, pending_trial_id=None)
            if progress is not None:
                progress(
                    data["phase"],
                    index + 1,
                    len(schedule),
                    max(0.0, budget.available_seconds(Phase(data["phase"]))),
                )
            if record.status in {TrialStatus.INTERRUPTED, TrialStatus.UNKNOWN}:
                return _finish(
                    store,
                    budget,
                    Outcome.INTERRUPTED,
                    "Trial interrupted or termination uncertain; automatic retry refused.",
                    interruption_exit_status=(
                        128 + record.extensions["simtune.cancellation"]["parent_signal"]
                        if record.extensions.get("simtune.cancellation", {}).get("parent_signal")
                        is not None
                        else 124
                        if record.return_code == 124
                        else 130
                    ),
                )
            if record.status == TrialStatus.TIMED_OUT:
                return _finish(
                    store,
                    budget,
                    Outcome.INCONCLUSIVE,
                    "A trial timed out; reference retained and failed evidence preserved.",
                )
            if record.policy_id == reference.id and (
                record.status != TrialStatus.SUCCEEDED
                or record.validation.state == ValidationState.FAILED
            ):
                return _finish(
                    store,
                    budget,
                    Outcome.FAILED,
                    "Reference execution or scientific validation failed; comparison stopped.",
                )
        if state["phase"] == "training":
            scores = {
                candidate.id: score_policy(
                    records,
                    workload=workload.spec,
                    baseline_policy_id=reference.id,
                    candidate_policy_id=candidate.id,
                    phase=Phase.TRAINING,
                    seed=config.seed,
                    minimum_pairs=config.repeat,
                    bootstrap_samples=config.bootstrap_samples,
                )
                for candidate in eligible
            }
            _save(
                store, budget, training_scores={key: asdict(value) for key, value in scores.items()}
            )
            for policy_id, policy_score in scores.items():
                if not policy_score.eligible:
                    _exclude(store, policy_id, "; ".join(policy_score.reasons))
            usable = [(key, value) for key, value in scores.items() if value.eligible]
            if not usable:
                return _finish(
                    store,
                    budget,
                    Outcome.INCONCLUSIVE,
                    "No candidate has complete checked compatible training evidence; reference retained.",
                )
            proposed, score = max(usable, key=lambda item: (item[1].ratio, item[0]))
            if score.ratio <= 1 / (1 - config.minimum_time_reduction) and not state.get(
                "revalidation"
            ):
                return _finish(
                    store,
                    budget,
                    Outcome.NO_IMPROVEMENT,
                    "Training found no challenger meeting the prespecified gain; reference retained.",
                )
            if not workload.spec.validation_case_ids:
                return _finish(
                    store,
                    budget,
                    Outcome.INCONCLUSIVE,
                    "Input-specific provisional training result: no held-out cases; reference retained.",
                )
            confirmation = paired_schedule(
                baseline=reference,
                candidates=[policies[proposed]],
                case_ids=workload.spec.validation_case_ids,
                phase=Phase.CONFIRMATION,
                pairs=config.confirmation_pairs,
                warmups=0,
                seed=config.seed + 1,
                start_order=len(schedule),
                options=options,
            )
            _save(
                store,
                budget,
                phase="confirmation",
                proposed_policy_id=proposed,
                schedule=[*schedule, *_schedule_data(confirmation, reference.id)],
            )
            continue
        result = confirm_policy(
            records,
            workload=workload.spec,
            baseline_policy_id=reference.id,
            candidate_policy_id=state["proposed_policy_id"],
            rule=store.record.selection_rule,
            minimum_training_pairs=config.repeat,
            bootstrap_samples=config.bootstrap_samples,
        )
        _save(store, budget, confirmation_result=asdict(result))
        if not result.confirmation.eligible:
            _exclude(store, state["proposed_policy_id"], "; ".join(result.confirmation.reasons))
        return _finish(
            store,
            budget,
            result.outcome,
            result.explanation,
            selected=result.selected_policy_id,
            saved_seconds=result.conditional_saved_seconds,
        )


def _probe_search_collector(
    caps: dict[str, Any],
    options: TrialExecutionOptions,
    budget: BudgetController,
    record_probe: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    caps = {
        **caps,
        "tools": {
            **caps.get("tools", {}),
            "perf": {
                "path": caps.get("tools", {}).get("perf", {}).get("path") or shutil.which("perf")
            },
        },
    }
    collector = probe_collector(
        options.collectors, caps, authorization=budget, record_probe=record_probe
    )
    caps["tools"]["perf"]["version"] = collector.perf_version
    return caps


def _collector_failure(exc: Exception) -> tuple[Outcome, str]:
    if getattr(exc, "parent_signal", None) is not None or not getattr(
        exc, "termination_known", True
    ):
        return Outcome.INTERRUPTED, f"Collector probe interrupted: {exc}"
    if isinstance(exc, BudgetExhausted):
        return (
            Outcome.INCONCLUSIVE,
            f"Collector authorization exhausted: {exc}; no new workload launched.",
        )
    return Outcome.FAILED, f"Collector preparation failed: {exc}; no new workload launched."


def _collector_interruption_status(exc: Exception, outcome: Outcome) -> int | None:
    if outcome != Outcome.INTERRUPTED:
        return None
    if getattr(exc, "interruption_exit_status", None) is not None:
        return exc.interruption_exit_status
    signum = getattr(exc, "parent_signal", None)
    return 128 + signum if signum is not None else 130


@util.cancellable
def run_search(
    workload: LoadedWorkload | Callable[[], LoadedWorkload],
    out: str | Path,
    config: SearchConfig,
    *,
    dry_run: bool = False,
    snapshot_provider: Callable[[], ResourceSnapshot] = search_snapshot,
    capabilities: Mapping[str, Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
    options: TrialExecutionOptions = TrialExecutionOptions(),
    executor_kwargs: Mapping[str, Any] | None = None,
    profile_hint: Mapping[str, Any] | None = None,
    revalidate_policy: PolicySpec | None = None,
    source_recipe: Mapping[str, Any] | None = None,
    experiment_id: str | None = None,
    progress: Callable[[str, int, int, float], None] | None = None,
) -> ExperimentRecord | dict[str, Any]:
    budget = BudgetController(
        authorized_seconds=config.budget_seconds,
        confirmation_reserved_seconds=0,
        cleanup_allowance_seconds=config.cleanup_allowance_seconds,
        max_trials=config.max_trials or 100,
        max_launches=config.max_launches or 300,
        clock=clock,
    )
    started = clock()
    loaded = workload() if callable(workload) else workload
    _same_case_resources(loaded)
    budget.max_trials = min(
        config.max_trials or loaded.spec.limits.max_trials, loaded.spec.limits.max_trials
    )
    budget.max_launches = min(
        config.max_launches or loaded.spec.limits.max_launches, loaded.spec.limits.max_launches
    )
    validation_cases = loaded.spec.validation_case_ids
    if validation_cases:
        budget.confirmation_reserved_seconds = config.budget_seconds * config.confirmation_fraction
        budget.reserve_counts(
            trials=2 * config.confirmation_pairs * len(validation_cases),
            launches=2
            * config.confirmation_pairs
            * sum(_launches_per_trial(loaded, case_id, options) for case_id in validation_cases),
        )
    snapshot = snapshot_provider()
    caps = dict(capabilities) if capabilities is not None else search_capabilities(snapshot)
    probe_events: list[dict[str, Any]] = []
    if options.collectors == "perf-stat" and not dry_run:
        try:
            caps = _probe_search_collector(caps, options, budget, probe_events.append)
        except (BudgetExhausted, ValidationError) as exc:
            budget.charge("discovery", started)
            outcome, explanation = _collector_failure(exc)
            return {
                "kind": "simtune_tune_startup_result",
                "schema_version": 1,
                "outcome": outcome.value,
                "explanation": explanation,
                "collector": options.collectors,
                "budget": budget.ledger().to_dict(),
                "collector_probes": probe_events,
                "parent_signal": getattr(exc, "parent_signal", None),
                "interruption_exit_status": _collector_interruption_status(exc, outcome),
                "remedy": "Inspect probe evidence; explicitly start a new search with sufficient time/count authorization or --collectors timing.",
                "completion_marker": None,
            }
    fingerprint = environment_fingerprint(loaded, snapshot, caps)
    candidates: CandidateSet = generate_candidates(
        snapshot,
        caps,
        runtime=loaded.spec.runtime,
        budget=loaded.spec.cases[0].resource_needs,
        profile_hint=profile_hint,
        workload_fingerprint=loaded.workload_fingerprint,
        environment_fingerprint=fingerprint,
    )
    if revalidate_policy is not None:
        candidates = revalidation_candidates(
            snapshot,
            caps,
            policy=revalidate_policy,
            runtime=loaded.spec.runtime,
            budget=loaded.spec.cases[0].resource_needs,
        )
    budget.charge("discovery", started)
    eligible = [policy for policy in candidates.policies if policy.id != candidates.reference.id]
    # Fixed scientific environment always takes precedence over proposed runtime settings.
    conflicts = {
        plan.policy.id
        for plan in candidates.plans
        if any(
            key in loaded.spec.fixed_env and loaded.spec.fixed_env[key] != value
            for key, value in plan.env_overrides
        )
    }
    ledger = [
        Candidate(
            policy_id=item.policy_id,
            exclusion_reason="policy conflicts with fixed workload environment",
        )
        if item.policy_id in conflicts
        else item
        for item in candidates.ledger
    ]
    eligible = [policy for policy in eligible if policy.id not in conflicts]
    training_trials = (
        2 * (config.repeat + config.warmups) * len(eligible) * len(loaded.spec.training_case_ids)
    )
    training_launches = (
        2
        * (config.repeat + config.warmups)
        * len(eligible)
        * sum(
            _launches_per_trial(loaded, case_id, options)
            for case_id in loaded.spec.training_case_ids
        )
    )
    unfunded = None
    if (
        training_trials + budget.confirmation_reserved_trials > budget.max_trials
        or budget.launches_started + training_launches + budget.confirmation_reserved_launches
        > budget.max_launches
    ):
        unfunded = "Declared exploration and confirmation exceed the trial/launch caps; reference retained."
    if training_trials + budget.confirmation_reserved_trials > 10000:
        unfunded = "Stage 05 limits an explicit schedule to 10000 trials; use a smaller protocol. Reference retained."
    schedule = (
        []
        if unfunded
        else paired_schedule(
            baseline=candidates.reference,
            candidates=eligible,
            case_ids=loaded.spec.training_case_ids,
            phase=Phase.TRAINING,
            pairs=config.repeat,
            warmups=config.warmups,
            seed=config.seed,
            options=options,
        )
    )
    state = {
        "schema_version": 1,
        "revalidation": revalidate_policy is not None,
        "source_recipe": dict(source_recipe) if source_recipe is not None else None,
        "runtime_environment": runtime_environment(
            dict(executor_kwargs or {}).get("base_env", os.environ), loaded.spec.fixed_env
        ),
        "config": asdict(config),
        "phase": "training",
        "next_index": 0,
        "pending_trial_id": None,
        "proposed_policy_id": None,
        "protocol_unfunded_reason": unfunded,
        "resumable": True,
        "reference_policy_id": candidates.reference.id,
        "policies": [policy.to_dict() for policy in candidates.declared_policies],
        "schedule": _schedule_data(schedule, candidates.reference.id),
        "notes": list(candidates.notes),
        "options": asdict(options),
        "capabilities": caps,
        "collector_probes": probe_events,
        "label": (
            "scientific checks declared; held-out cases declared"
            if validation_cases
            else "scientific checks declared; input-specific; held-out confirmation unavailable"
        )
        if all(loaded.spec.output_checks or case.checks for case in loaded.spec.cases)
        else "input-specific/unchecked; complete scientific checks unavailable",
    }
    # Options contain enums/path/tuples; normalize for strict JSON and resume.
    state["options"]["workspace_mode"] = options.workspace_mode.value
    state["options"]["fixed_cwd"] = str(options.fixed_cwd) if options.fixed_cwd else None
    for hook in ("setup", "cleanup"):
        if state["options"][hook]:
            state["options"][hook]["argv"] = list(state["options"][hook]["argv"])
    if dry_run:
        launch_breakdown = {
            case.id: {
                "workload": 1,
                "setup": int(options.setup is not None),
                "cleanup": int(options.cleanup is not None),
                "validator": _launches_per_trial(loaded, case.id, options)
                - 1
                - int(options.setup is not None)
                - int(options.cleanup is not None),
            }
            for case in loaded.spec.cases
        }
        return {
            "dry_run": True,
            "state": state,
            "candidates": [item.to_dict() for item in ledger],
            "budget": budget.ledger().to_dict(),
            "plans": [plan.to_dict() for plan in candidates.plans],
            "launches_per_trial_by_case": launch_breakdown,
            "scheduled_training_trials": len(schedule),
            "reserved_confirmation_trials": budget.confirmation_reserved_trials,
        }
    record = new_experiment_record(
        experiment_id=experiment_id or f"search-{uuid.uuid4().hex}",
        workload=loaded,
        snapshot=snapshot,
        capabilities=caps,
        policies=candidates.declared_policies,
        budget=budget,
        seed=config.seed,
    )
    record = replace(
        record,
        candidates=ledger,
        explanation="Search protocol declared before trials.",
        selection_rule=replace(
            record.selection_rule,
            minimum_time_reduction=config.minimum_time_reduction,
            minimum_confirmation_pairs=config.confirmation_pairs,
        ),
        extensions={**record.extensions, STATE_KEY: state},
    )
    store = ExperimentStore.create(out, record)
    with _SearchLease(store.root):
        store.write_artifact_json("workload.json", loaded.spec.to_dict())
        store.write_artifact_json("resources.json", snapshot.to_dict())
        return _continue(
            store,
            loaded,
            budget,
            caps,
            snapshot_provider,
            clock=clock,
            options=options,
            executor_kwargs=executor_kwargs,
            progress=progress,
        )


@util.cancellable
def resume_search(
    out: str | Path,
    *,
    additional_seconds: float = 0,
    snapshot_provider: Callable[[], ResourceSnapshot] = search_snapshot,
    capabilities: Mapping[str, Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
    executor_kwargs: Mapping[str, Any] | None = None,
    progress: Callable[[str, int, int, float], None] | None = None,
) -> ExperimentRecord:
    """Continue only never-started scheduled work, with prior unspent authorization.

    Completed experiments and uncertain/interrupted trials are refused. A missing
    terminal record after durable intent is uncertain, even before actual spawn.
    """
    from .experiments import HookSpec, WorkspaceMode

    session_started = clock()
    root = Path(out).resolve(strict=True)
    with _SearchLease(root):
        store = ExperimentStore.open(root)
        if store.is_complete():
            raise ValidationError("completed search is immutable; start a new experiment")
        state = _state(store)
        if state.get("schema_version") != 1:
            raise ValidationError("unsupported search-state version")
        records = _read_trials(store)
        if any(record.status == TrialStatus.INTERRUPTED for record in records):
            raise ValidationError(
                "interrupted trial requires inspection; it is not automatically retried"
            )
        if state.get("pending_trial_id") and state["pending_trial_id"] not in {
            trial.id for trial in records
        }:
            raise ValidationError("uncertain pending trial; automatic retry refused")
        if state.get("pending_collector_probe") or any(
            not ((event.get("metrics") or {}).get("cancellation") or {}).get(
                "termination_known", True
            )
            for event in state.get("collector_probes", [])
        ):
            raise ValidationError("uncertain collector probe; inspect owned work before resuming")
        budget = BudgetController.from_ledger(
            store.record.budget,
            additional_seconds=additional_seconds,
            clock=clock,
            session_started=session_started,
        )
        if (
            budget.ledger().extensions["simtune.release03"]["collector_probe_counts"]
            == "historical_unknown"
        ):
            note = "Historical collector probe counts are unknown; saved spent time/counts are retained without reconstruction. New probes consume remaining authorization."
            _save(store, budget, notes=list(dict.fromkeys([*state["notes"], note])))
        loaded = load_workload(artifact_path(root, "workload.json"))
        if loaded.workload_fingerprint != store.record.workload_fingerprint:
            raise ValidationError("workload fingerprint changed; cannot resume")
        snapshot = snapshot_provider()
        caps = dict(capabilities) if capabilities is not None else search_capabilities(snapshot)
        option_data = state["options"]
        options = TrialExecutionOptions(
            workspace_mode=WorkspaceMode(option_data["workspace_mode"]),
            fixed_cwd=Path(option_data["fixed_cwd"]) if option_data["fixed_cwd"] else None,
            collectors=option_data.get("collectors", "timing"),
            **{
                name: HookSpec(
                    tuple(option_data[name]["argv"]), option_data[name]["timeout_seconds"]
                )
                if option_data[name]
                else None
                for name in ("setup", "cleanup")
            },
        )
        if options.collectors == "perf-stat":
            probe_events = list(state.get("collector_probes", []))

            def record_probe(event: dict[str, Any]) -> None:
                probe_events.append(event)
                _save(
                    store,
                    budget,
                    collector_probes=probe_events,
                    pending_collector_probe=event["step"]
                    if event["status"] in {"reserved", "uncertain"}
                    else None,
                )

            try:
                caps = _probe_search_collector(caps, options, budget, record_probe)
            except (BudgetExhausted, ValidationError) as exc:
                outcome, explanation = _collector_failure(exc)
                _save(store, budget, collector_preparation_failure=explanation)
                return _finish(
                    store,
                    budget,
                    outcome,
                    explanation,
                    resumable=True,
                    interruption_exit_status=_collector_interruption_status(exc, outcome),
                )
        if environment_fingerprint(loaded, snapshot, caps) != store.record.environment_fingerprint:
            _save(store, budget, collector_preparation_failure="environment fingerprint changed")
            raise ValidationError("environment fingerprint changed; cannot resume")
        _save(store, budget, pending_trial_id=None)
        return _continue(
            store,
            loaded,
            budget,
            caps,
            snapshot_provider,
            clock=clock,
            options=options,
            executor_kwargs=executor_kwargs,
            progress=progress,
        )
