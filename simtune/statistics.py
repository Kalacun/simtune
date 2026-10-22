"""Conservative, dependency-free scoring of strict isolated-trial evidence.

Callers pass the complete experiment ledger, including warmups and unsuccessful
trials, after verifying its workload/environment identity. This module performs
no launches or disk reads. Case weights are fixed by the workload; bootstrap
draws resample complete pairs independently within each case, never individual
baseline and candidate observations. All intervals are two-sided percentile
95% intervals with linear interpolation and a local seeded PRNG.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import (
    ActionStatus,
    CaseSpec,
    Outcome,
    Phase,
    SelectionRule,
    Target,
    TrialRecord,
    TrialStatus,
    ValidationState,
    WorkloadSpec,
)


@dataclass(frozen=True)
class CaseScore:
    case_id: str
    weight: float
    pair_ids: tuple[str, ...]
    trial_ids: tuple[str, ...]
    ratios: tuple[float, ...]
    mean_log_ratio: float
    log_ratio_stddev: float
    ratio: float
    lower: float
    upper: float
    baseline_seconds: float
    candidate_seconds: float


@dataclass(frozen=True)
class PolicyScore:
    policy_id: str
    phase: Phase
    eligible: bool
    reasons: tuple[str, ...]
    ratio: float | None
    lower: float | None
    upper: float | None
    cases: tuple[CaseScore, ...]
    baseline_seconds: float | None
    candidate_seconds: float | None


@dataclass(frozen=True)
class ConfirmationResult:
    outcome: Outcome
    selected_policy_id: str
    explanation: str
    training: PolicyScore
    confirmation: PolicyScore
    conditional_saved_seconds: float | None
    protocol_valid: bool
    protocol_reasons: tuple[str, ...]


def _mean(values: Sequence[float]) -> float:
    # Division before summation also avoids overflow for large finite timings.
    return math.fsum(value / len(values) for value in values)


def _weighted(values: Sequence[float], cases: Sequence[CaseSpec]) -> float:
    scale = max(case.weight for case in cases)
    weights = [case.weight / scale for case in cases]
    total = math.fsum(weights)
    return math.fsum(value * (weight / total) for value, weight in zip(values, weights))


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _interval(logs: Sequence[float]) -> tuple[float, float]:
    return math.exp(_percentile(logs, 0.025)), math.exp(_percentile(logs, 0.975))


def _scientific_argv(workload: WorkloadSpec, case: CaseSpec) -> tuple[str, ...]:
    """Canonical identity also detects same input bytes renamed as a new case."""
    if workload.extensions.get("simtune.literal_argv") is True:
        return tuple(workload.argv_template)
    artifacts: dict[str, str] = {}
    for artifact in [*workload.artifacts, *case.input_artifacts]:
        identity = f"sha256:{artifact.sha256}"
        artifacts[artifact.path] = identity
        artifacts[str(Path(workload.base_directory) / artifact.path)] = identity
    return tuple(
        artifacts.get(case.arguments[arg[6:-1]], case.arguments[arg[6:-1]])
        if arg.startswith("{case.")
        else arg
        for arg in workload.argv_template
    )


def _case_identity(workload: WorkloadSpec, case: CaseSpec) -> str:
    return json.dumps(
        [_scientific_argv(workload, case), sorted(a.sha256 for a in case.input_artifacts)],
        sort_keys=True,
    )


def _argv_matches(trial: TrialRecord, workload: WorkloadSpec, case: CaseSpec) -> bool:
    if workload.extensions.get("simtune.literal_argv") is True:
        return trial.argv == workload.argv_template
    if len(trial.argv) != len(workload.argv_template):
        return False
    workspace_values = []
    stage04 = trial.extensions.get("simtune.stage04", {})
    for actual, declared in zip(trial.argv, workload.argv_template):
        if declared == "{trial_dir}":
            workspace_values.append(actual)
            if not Path(actual).is_absolute():
                return False
            if isinstance(stage04, dict) and stage04.get("workspace_mode") == "fixed_cwd":
                if not actual.endswith(f"/trials/{trial.id}/work"):
                    return False
            elif actual != trial.cwd:
                return False
        elif actual != (
            case.arguments[declared[6:-1]] if declared.startswith("{case.") else declared
        ):
            return False
    return len(set(workspace_values)) <= 1


def _problem(trial: TrialRecord, workload: WorkloadSpec, case: CaseSpec) -> str | None:
    if (
        trial.status != TrialStatus.SUCCEEDED
        or trial.return_code != 0
        or trial.timed_out is not False
    ):
        return f"trial {trial.id} did not succeed ({trial.status.value})"
    expected_checks = {check.id for check in [*workload.output_checks, *case.checks]}
    if (
        trial.validation.state != ValidationState.PASSED
        or not expected_checks
        or set(trial.validation.checks) != expected_checks
    ):
        return f"trial {trial.id} lacks passed declared scientific checks"
    if not _argv_matches(trial, workload, case):
        return f"trial {trial.id} has incompatible scientific argv"
    if any(trial.env_overrides.get(key) != value for key, value in workload.fixed_env.items()):
        return f"trial {trial.id} changes the fixed scientific environment"
    if any(
        action.setting.required and action.status != ActionStatus.APPLIED
        for action in trial.actions
    ):
        return f"trial {trial.id} did not apply a required action"
    return None


def _metric(trial: TrialRecord):
    elapsed = [metric for metric in trial.metrics if metric.name == "elapsed"]
    if len(elapsed) != 1:
        return None
    metric = elapsed[0]
    if (
        metric.unit != "seconds"
        or type(metric.value) not in (int, float)
        or not math.isfinite(metric.value)
        or metric.value <= 0
    ):
        return None
    return metric


def _collectors(trial: TrialRecord) -> list[str]:
    return sorted(
        json.dumps(
            [
                metric.name,
                metric.unit,
                metric.collector.to_dict(),
                metric.scope,
            ],
            sort_keys=True,
        )
        for metric in trial.metrics
    )


def _environment_matches(baseline: TrialRecord, candidate: TrialRecord) -> bool:
    """Only applied, explicitly recorded runtime/environment overrides may differ."""
    changed = {
        key
        for key in baseline.env_overrides.keys() | candidate.env_overrides.keys()
        if baseline.env_overrides.get(key) != candidate.env_overrides.get(key)
    }
    allowed = {
        action.setting.name
        for trial in (baseline, candidate)
        for action in trial.actions
        if action.status == ActionStatus.APPLIED
        and action.setting.target in (Target.RUNTIME, Target.ENVIRONMENT)
        and str(action.resolved_value) == trial.env_overrides.get(action.setting.name)
    }
    return changed <= allowed


def _reuse_reasons(trials: Sequence[TrialRecord]) -> list[str]:
    reasons = []
    seen_ids: set[str] = set()
    seen_sources: dict[str, str] = {}
    experiments = {trial.experiment_id for trial in trials}
    if len(experiments) > 1:
        reasons.append("evidence belongs to different experiments")
    for trial in trials:
        if trial.id in seen_ids:
            reasons.append(f"reused trial identity {trial.id}")
        seen_ids.add(trial.id)
        sources = {
            *trial.raw_artifacts,
            *trial.validation.evidence,
            *(metric.raw_source for metric in trial.metrics if metric.raw_source is not None),
        }
        for source in sources:
            if source in seen_sources and seen_sources[source] != trial.id:
                reasons.append(f"reused trial evidence {source}")
            seen_sources[source] = trial.id
    training_pairs = {trial.pair_id for trial in trials if trial.phase == Phase.TRAINING}
    confirmation_pairs = {trial.pair_id for trial in trials if trial.phase == Phase.CONFIRMATION}
    if (training_pairs & confirmation_pairs) - {None}:
        reasons.append("training and confirmation reuse pair identities")
    return reasons


def score_policy(
    trials: Sequence[TrialRecord],
    *,
    workload: WorkloadSpec,
    baseline_policy_id: str,
    candidate_policy_id: str,
    phase: Phase,
    seed: int = 0,
    minimum_pairs: int = 1,
    bootstrap_samples: int = 2000,
) -> PolicyScore:
    """Score a policy only when all its evidence is checked and compatible.

    Baseline pairs belonging to other candidates are ignored. Candidate failures
    anywhere in the full ledger, including retained warmups, disqualify it; no
    survivor averages or outlier removal are permitted. Incomplete/invalid case
    evidence leaves the aggregate unavailable, with every reason retained.
    """
    if phase not in (Phase.TRAINING, Phase.CONFIRMATION):
        raise ValueError("only training or confirmation measurements can be scored")
    if type(minimum_pairs) is not int or minimum_pairs < 1:
        raise ValueError("minimum_pairs must be a positive integer")
    if type(bootstrap_samples) is not int or bootstrap_samples < 100:
        raise ValueError("bootstrap_samples must be at least 100")
    if type(seed) is not int or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    if baseline_policy_id == candidate_policy_id:
        raise ValueError("baseline and candidate policy identities must differ")
    cases_by_id = {case.id: case for case in workload.cases}
    requested_ids = (
        workload.training_case_ids if phase == Phase.TRAINING else workload.validation_case_ids
    )
    cases = [cases_by_id[case_id] for case_id in sorted(requested_ids)]
    relevant = [trial for trial in trials if trial.policy_id == candidate_policy_id]
    reasons = _reuse_reasons(trials)
    if not cases:
        reasons.append(f"no declared {phase.value} cases")
    for reference in trials:
        if (
            reference.policy_id == baseline_policy_id
            and reference.phase == phase
            and reference.case_id in requested_ids
            and not any(
                other.phase == reference.phase
                and other.case_id == reference.case_id
                and other.pair_id == reference.pair_id
                and other.policy_id != baseline_policy_id
                for other in trials
            )
        ):
            reasons.append(f"baseline trial {reference.id} has no candidate counterpart")
    for trial in relevant:
        case = cases_by_id.get(trial.case_id)
        if case is None:
            reasons.append(f"trial {trial.id} uses undeclared case {trial.case_id}")
        elif problem := _problem(trial, workload, case):
            reasons.append(problem)
        if trial.phase == phase and trial.case_id not in requested_ids:
            reasons.append(f"trial {trial.id} uses the wrong case partition")
    baseline_warmups = [
        trial
        for trial in trials
        if trial.policy_id == baseline_policy_id and trial.phase == Phase.WARMUP
    ]
    for trial in baseline_warmups:
        case = cases_by_id.get(trial.case_id)
        if case is None or (problem := _problem(trial, workload, case)):
            reasons.append(problem if case is not None else "baseline warmup case is undeclared")
    rng = random.Random(seed)
    reports: list[CaseScore] = []
    bootstrap_by_case: list[list[float]] = []
    for case in cases:
        measurements = [
            trial for trial in relevant if trial.phase == phase and trial.case_id == case.id
        ]
        pair_ids = sorted({trial.pair_id for trial in measurements if trial.pair_id is not None})
        logs: list[float] = []
        baselines: list[float] = []
        candidates: list[float] = []
        trial_ids: list[str] = []
        for pair_id in pair_ids:
            pair = [
                trial
                for trial in trials
                if trial.phase == phase and trial.case_id == case.id and trial.pair_id == pair_id
            ]
            baseline = [trial for trial in pair if trial.policy_id == baseline_policy_id]
            candidate = [trial for trial in pair if trial.policy_id == candidate_policy_id]
            if len(pair) != 2 or len(baseline) != 1 or len(candidate) != 1:
                reasons.append(f"case {case.id} pair {pair_id} is incomplete or duplicated")
                continue
            reference, proposal = baseline[0], candidate[0]
            if reference.order == proposal.order:
                reasons.append(f"case {case.id} pair {pair_id} has duplicate trial order")
                continue
            if problem := _problem(reference, workload, case):
                reasons.append(problem)
                continue
            if _collectors(reference) != _collectors(proposal):
                reasons.append(f"case {case.id} pair {pair_id} has mismatched collectors")
                continue
            if not _environment_matches(reference, proposal):
                reasons.append(f"case {case.id} pair {pair_id} has incompatible environment")
                continue
            baseline_metric, candidate_metric = _metric(reference), _metric(proposal)
            if baseline_metric is None or candidate_metric is None:
                reasons.append(
                    f"case {case.id} pair {pair_id} lacks finite positive elapsed seconds"
                )
                continue
            baseline_value, candidate_value = baseline_metric.value, candidate_metric.value
            logs.append(math.log(baseline_value) - math.log(candidate_value))
            baselines.append(baseline_value)
            candidates.append(candidate_value)
            trial_ids.extend((reference.id, proposal.id))
        if len(logs) < minimum_pairs:
            reasons.append(f"case {case.id} needs {minimum_pairs} complete pairs; got {len(logs)}")
        if len(logs) != len(pair_ids) or not logs:
            continue
        mean_log = _mean(logs)
        draws = [_mean(rng.choices(logs, k=len(logs))) for _ in range(bootstrap_samples)]
        try:
            lower, upper = _interval(draws)
            ratios = tuple(math.exp(value) for value in logs)
            ratio = math.exp(mean_log)
        except OverflowError:
            reasons.append(f"case {case.id} ratio cannot be represented as a finite number")
            continue
        if min(lower, ratio, *ratios) <= 0:
            reasons.append(f"case {case.id} ratio underflow")
            continue
        reports.append(
            CaseScore(
                case_id=case.id,
                weight=case.weight,
                pair_ids=tuple(pair_ids),
                trial_ids=tuple(trial_ids),
                ratios=ratios,
                mean_log_ratio=mean_log,
                log_ratio_stddev=math.sqrt(_mean([(value - mean_log) ** 2 for value in logs])),
                ratio=ratio,
                lower=lower,
                upper=upper,
                baseline_seconds=_mean(baselines),
                candidate_seconds=_mean(candidates),
            )
        )
        bootstrap_by_case.append(draws)
    if reasons or len(reports) != len(cases):
        return PolicyScore(
            candidate_policy_id,
            phase,
            False,
            tuple(dict.fromkeys(reasons)),
            None,
            None,
            None,
            tuple(reports),
            None,
            None,
        )
    aggregate_draws = [
        _weighted([draws[index] for draws in bootstrap_by_case], cases)
        for index in range(bootstrap_samples)
    ]
    lower, upper = _interval(aggregate_draws)
    return PolicyScore(
        candidate_policy_id,
        phase,
        True,
        (),
        math.exp(_weighted([report.mean_log_ratio for report in reports], cases)),
        lower,
        upper,
        tuple(reports),
        _weighted([report.baseline_seconds for report in reports], cases),
        _weighted([report.candidate_seconds for report in reports], cases),
    )


def confirm_policy(
    trials: Sequence[TrialRecord],
    *,
    workload: WorkloadSpec,
    baseline_policy_id: str,
    candidate_policy_id: str,
    rule: SelectionRule,
    minimum_training_pairs: int = 1,
    bootstrap_samples: int = 2000,
) -> ConfirmationResult:
    """Apply the prespecified improvement and every-case regression gates.

    ``protocol_valid`` includes phase eligibility and cross-phase independence
    and freshness. It is separate from passing the performance thresholds.
    Complete valid evidence that misses a threshold is ``no_improvement``;
    insufficient, unchecked, failed or reused evidence is ``inconclusive``.
    The reference remains selected for every non-win. The saved-seconds value
    is conditional on this fixed case mix and comparable future executions.
    """
    kwargs = dict(
        workload=workload,
        baseline_policy_id=baseline_policy_id,
        candidate_policy_id=candidate_policy_id,
        seed=rule.bootstrap_seed,
        bootstrap_samples=bootstrap_samples,
    )
    training = score_policy(
        trials, phase=Phase.TRAINING, minimum_pairs=minimum_training_pairs, **kwargs
    )
    confirmation = score_policy(
        trials, phase=Phase.CONFIRMATION, minimum_pairs=rule.minimum_confirmation_pairs, **kwargs
    )
    cases = {case.id: case for case in workload.cases}
    training_identity = {
        _case_identity(workload, cases[case_id]) for case_id in workload.training_case_ids
    }
    reused_cases = [
        case_id
        for case_id in workload.validation_case_ids
        if _case_identity(workload, cases[case_id]) in training_identity
    ]
    reasons = [*training.reasons, *confirmation.reasons]
    if reused_cases:
        reasons.append(
            "held-out cases reuse training scientific inputs: " + ", ".join(reused_cases)
        )
    training_orders = [trial.order for trial in trials if trial.phase == Phase.TRAINING]
    confirmation_orders = [trial.order for trial in trials if trial.phase == Phase.CONFIRMATION]
    if training_orders and confirmation_orders and min(confirmation_orders) <= max(training_orders):
        reasons.append("confirmation is not fresh: its trial order precedes completed training")
    if reasons:
        return ConfirmationResult(
            Outcome.INCONCLUSIVE,
            baseline_policy_id,
            "; ".join(dict.fromkeys(reasons)),
            training,
            confirmation,
            None,
            False,
            tuple(dict.fromkeys(reasons)),
        )
    threshold = 1 / (1 - rule.minimum_time_reduction)
    # Equality at the strict improvement threshold must not win due to exp/log
    # roundoff; 1e-12 relative resolution is far below meaningful timing precision.
    improved = confirmation.lower > threshold and not math.isclose(
        confirmation.lower, threshold, rel_tol=1e-12
    )
    regressions = [
        report.case_id
        for report in confirmation.cases
        if report.lower < 1 / (1 + cases[report.case_id].regression_tolerance)
    ]
    if not improved or regressions:
        explanation = "Held-out evidence did not exceed the minimum improvement threshold."
        if regressions:
            explanation += " Case regression was not ruled out: " + ", ".join(regressions) + "."
        return ConfirmationResult(
            Outcome.NO_IMPROVEMENT,
            baseline_policy_id,
            explanation,
            training,
            confirmation,
            None,
            True,
            (),
        )
    saved = confirmation.baseline_seconds - confirmation.candidate_seconds
    return ConfirmationResult(
        Outcome.VALIDATED_IMPROVEMENT,
        candidate_policy_id,
        "Fresh held-out pairs passed the aggregate improvement and every-case regression gates.",
        training,
        confirmation,
        saved if saved > 0 else None,
        True,
        (),
    )


def break_even_runs(additional_tuning_seconds: float, validated_saved_seconds: float | None):
    """Return conditional ceil(cost/savings); absent/nonpositive savings stay unknown."""
    if (
        type(additional_tuning_seconds) not in (int, float)
        or not math.isfinite(additional_tuning_seconds)
        or additional_tuning_seconds < 0
    ):
        raise ValueError("additional tuning seconds must be finite and nonnegative")
    if (
        type(validated_saved_seconds) not in (int, float)
        or not math.isfinite(validated_saved_seconds)
        or validated_saved_seconds <= 0
    ):
        return None
    quotient = additional_tuning_seconds / validated_saved_seconds
    return math.ceil(quotient) if math.isfinite(quotient) else None
