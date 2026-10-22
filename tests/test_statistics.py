"""Synthetic evidence tests for the frozen paired-ratio decision rule."""

import math
from dataclasses import replace
from pathlib import Path

import pytest

from simtune.models import (
    ExperimentRecord,
    Metric,
    Outcome,
    Phase,
    Scope,
    TrialRecord,
    TrialStatus,
    ValidationResult,
    ValidationState,
)
from simtune.statistics import break_even_runs, confirm_policy, score_policy
from simtune.workloads import load_workload

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def workload():
    spec = load_workload(FIXTURES / "experiments" / "workload.toml").spec
    train = replace(spec.cases[0], id="train", arguments={"input": "training"}, input_artifacts=[])
    heldout = replace(train, id="heldout", arguments={"input": "heldout"})
    return replace(
        spec,
        cases=[train, heldout],
        training_case_ids=[train.id],
        validation_case_ids=[heldout.id],
    )


@pytest.fixture
def rule():
    return ExperimentRecord.from_json(
        (FIXTURES / "extension" / "valid" / "experiment.json").read_text()
    ).selection_rule


def trial(workload, case, phase, index, policy, seconds):
    identity = f"{phase.value}-{case.id}-{index}-{policy}"
    workspace = f"/synthetic/trials/{identity}/work"
    raw = f"trials/{identity}/launch.json"
    metric = Metric.from_json((FIXTURES / "extension" / "valid" / "metric.json").read_text())
    metric = replace(
        metric,
        id=f"elapsed-{identity}",
        value=seconds,
        raw_source=raw,
        unavailable={
            "scaling_factor": "not scaled",
            "multiplex_running_fraction": "not multiplexed",
        },
    )
    return TrialRecord(
        id=identity,
        experiment_id="synthetic-experiment",
        case_id=case.id,
        policy_id=policy,
        pair_id=f"{phase.value}-{case.id}-{index}" if phase != Phase.WARMUP else None,
        phase=phase,
        order=index * 2 + (policy != "baseline"),
        argv=[workload.resolved_executable, case.arguments["input"], workspace],
        cwd=workspace,
        env_overrides=workload.fixed_env,
        started_at="2026-09-18T10:00:00Z",
        ended_at="2026-09-18T10:00:01Z",
        status=TrialStatus.SUCCEEDED,
        status_reason="Synthetic successful observation.",
        return_code=0,
        timed_out=False,
        actions=[],
        metrics=[metric],
        validation=ValidationResult(
            state=ValidationState.PASSED,
            checks=[check.id for check in workload.output_checks + case.checks],
            explanation="Synthetic checked evidence.",
            evidence=[f"trials/{identity}/check.json"],
        ),
        raw_artifacts=[raw],
        unavailable={"pair_id": "warmup has no comparison pair"} if phase == Phase.WARMUP else {},
    )


def evidence(workload, *, training=(1.25,), confirmation=(1.25,) * 5):
    records = []
    for case in workload.cases:
        phase = Phase.TRAINING if case.id in workload.training_case_ids else Phase.CONFIRMATION
        ratios = training if phase == Phase.TRAINING else confirmation
        for index, ratio in enumerate(ratios):
            records.extend(
                [
                    trial(workload, case, phase, index, "baseline", 10),
                    trial(workload, case, phase, index, "candidate", 10 / ratio),
                ]
            )
    return [replace(record, order=index) for index, record in enumerate(records)]


def score(workload, records, **kwargs):
    return score_policy(
        records,
        workload=workload,
        baseline_policy_id="baseline",
        candidate_policy_id="candidate",
        phase=kwargs.pop("phase", Phase.CONFIRMATION),
        **kwargs,
    )


def confirm(workload, rule, records):
    return confirm_policy(
        records,
        workload=workload,
        baseline_policy_id="baseline",
        candidate_policy_id="candidate",
        rule=rule,
    )


def test_clear_gain_fresh_checked_confirmation(workload, rule):
    result = confirm(workload, rule, evidence(workload))
    assert result.outcome == Outcome.VALIDATED_IMPROVEMENT
    assert result.protocol_valid and result.protocol_reasons == ()
    assert result.selected_policy_id == "candidate"
    assert result.confirmation.ratio == pytest.approx(1.25)
    assert result.confirmation.lower == pytest.approx(1.25)
    assert result.confirmation.upper == pytest.approx(1.25)
    assert result.conditional_saved_seconds == pytest.approx(2)
    assert result.confirmation.cases[0].log_ratio_stddev == 0
    assert break_even_runs(21, result.conditional_saved_seconds) == 11


@pytest.mark.parametrize("ratios", [(1,) * 5, (0.8,) * 5, (0.6, 0.8, 1, 1.4, 2)])
def test_equal_slow_or_noisy_confirmation_retains_baseline(workload, rule, ratios):
    result = confirm(workload, rule, evidence(workload, confirmation=ratios))
    assert result.outcome == Outcome.NO_IMPROVEMENT
    assert result.protocol_valid and result.protocol_reasons == ()
    assert result.selected_policy_id == "baseline"
    assert result.conditional_saved_seconds is None


def test_exact_time_reduction_threshold_is_strict(workload, rule):
    exact = 1 / (1 - rule.minimum_time_reduction)
    result = confirm(workload, rule, evidence(workload, confirmation=(exact,) * 5))
    assert result.outcome == Outcome.NO_IMPROVEMENT
    result = confirm(workload, rule, evidence(workload, confirmation=(exact + 0.000001,) * 5))
    assert result.outcome == Outcome.VALIDATED_IMPROVEMENT


def test_fewer_than_five_pairs_is_inconclusive(workload, rule):
    result = confirm(workload, rule, evidence(workload, confirmation=(10,) * 4))
    assert result.outcome == Outcome.INCONCLUSIVE
    assert not result.protocol_valid
    assert any("needs 5 complete pairs" in reason for reason in result.protocol_reasons)
    assert "needs 5 complete pairs" in result.explanation


def test_weighted_geometric_ratios_not_mean_times_or_pooled_pairs(workload):
    first = replace(workload.cases[1], weight=1)
    second = replace(first, id="heldout-large", arguments={"input": "large"}, weight=3)
    workload = replace(
        workload,
        cases=[workload.cases[0], first, second],
        validation_case_ids=[first.id, second.id],
    )
    records = evidence(workload)
    for index, record in enumerate(records):
        if record.phase != Phase.CONFIRMATION:
            continue
        metric = record.metrics[0]
        if record.case_id == first.id:
            seconds = 8 if record.policy_id == "baseline" else 2
        else:
            seconds = 1 if record.policy_id == "baseline" else 2
        records[index] = replace(record, metrics=[replace(metric, value=seconds)])
    result = score(workload, records)
    expected = 4**0.25 * 0.5**0.75
    assert result.eligible
    assert result.ratio == pytest.approx(expected)
    assert result.lower == pytest.approx(expected)
    assert result.baseline_seconds == pytest.approx(2.75)
    assert result.candidate_seconds == pytest.approx(2)


def test_one_case_regression_cannot_hide_behind_weights(workload, rule):
    good = replace(workload.cases[1], weight=100)
    bad = replace(good, id="bad", arguments={"input": "bad"}, weight=1)
    workload = replace(
        workload, cases=[workload.cases[0], good, bad], validation_case_ids=[good.id, bad.id]
    )
    records = evidence(workload, confirmation=(2,) * 5)
    records = [
        replace(record, metrics=[replace(record.metrics[0], value=11)])
        if record.case_id == bad.id and record.policy_id == "candidate"
        else record
        for record in records
    ]
    result = confirm(workload, rule, records)
    assert result.confirmation.lower > 1.5
    assert result.outcome == Outcome.NO_IMPROVEMENT
    assert "bad" in result.explanation
    permitted = replace(bad, regression_tolerance=0.1)
    workload = replace(workload, cases=[workload.cases[0], good, permitted])
    assert confirm(workload, rule, records).outcome == Outcome.VALIDATED_IMPROVEMENT


@pytest.mark.parametrize("phase", [Phase.WARMUP, Phase.TRAINING, Phase.CONFIRMATION])
@pytest.mark.parametrize("failure", ["failed", "timed_out", "unchecked", "bad_science"])
def test_any_selected_policy_failure_or_unchecked_evidence_disqualifies(
    workload, rule, phase, failure
):
    records = evidence(workload)
    if phase == Phase.WARMUP:
        records.append(trial(workload, workload.cases[0], phase, 100, "candidate", 0.0001))
    index = next(
        i for i, t in enumerate(records) if t.phase == phase and t.policy_id == "candidate"
    )
    record = records[index]
    if failure in ("failed", "timed_out"):
        record = replace(
            record,
            status=TrialStatus(failure),
            return_code=1,
            timed_out=failure == "timed_out",
        )
    else:
        record = replace(
            record,
            validation=replace(
                record.validation,
                state=ValidationState.UNCHECKED
                if failure == "unchecked"
                else ValidationState.FAILED,
            ),
        )
    records[index] = record
    result = confirm(workload, rule, records)
    assert result.outcome == Outcome.INCONCLUSIVE
    assert result.selected_policy_id == "baseline"
    assert not result.protocol_valid and result.protocol_reasons


@pytest.mark.parametrize("value", [0, -1, None, float("nan"), float("inf"), True])
def test_zero_nonfinite_or_unknown_elapsed_never_scores(workload, value):
    records = evidence(workload)
    # Simulate hostile in-memory evidence as well as values rejected by the wire schema.
    object.__setattr__(records[-1].metrics[0], "value", value)
    result = score(workload, records)
    assert not result.eligible
    assert "finite positive" in " ".join(result.reasons)


@pytest.mark.parametrize("change", ["unit", "scope", "version", "mechanism", "missing", "extra"])
def test_mismatched_collectors_never_score(workload, change):
    records = evidence(workload)
    metric = records[-1].metrics[0]
    if change == "unit":
        metric = replace(metric, unit="milliseconds")
    elif change == "scope":
        metric = replace(metric, scope=Scope.PROCESS)
    elif change in ("version", "mechanism"):
        collector = replace(
            metric.collector,
            **{change: "different"},
            unavailable={} if change == "version" else metric.collector.unavailable,
        )
        metric = replace(metric, collector=collector)
    metrics = [] if change == "missing" else [metric]
    if change == "extra":
        metrics.append(replace(metric, id="extra", name="rss", unit="bytes"))
    records[-1] = replace(records[-1], metrics=metrics)
    result = score(workload, records)
    assert not result.eligible
    assert "mismatched collectors" in " ".join(result.reasons)


@pytest.mark.parametrize("reuse", ["trial", "pair", "raw_source", "check", "experiment"])
def test_reused_or_cross_experiment_evidence_rejected(workload, rule, reuse):
    records = evidence(workload)
    if reuse == "trial":
        records.append(records[-1])
    elif reuse == "pair":
        records[-1] = replace(records[-1], pair_id=records[1].pair_id)
        records[-2] = replace(records[-2], pair_id=records[0].pair_id)
    elif reuse == "raw_source":
        records[-1] = replace(
            records[-1],
            metrics=[replace(records[-1].metrics[0], raw_source=records[1].metrics[0].raw_source)],
        )
    elif reuse == "check":
        records[-1] = replace(
            records[-1],
            validation=replace(records[-1].validation, evidence=records[1].validation.evidence),
        )
    else:
        records[-1] = replace(records[-1], experiment_id="different-experiment")
    result = confirm(workload, rule, records)
    assert result.outcome == Outcome.INCONCLUSIVE
    assert not result.protocol_valid and result.protocol_reasons


def test_distinct_case_names_do_not_make_identical_inputs_held_out(workload, rule):
    heldout = replace(workload.cases[1], arguments=workload.cases[0].arguments)
    workload = replace(workload, cases=[workload.cases[0], heldout])
    result = confirm(workload, rule, evidence(workload))
    assert result.outcome == Outcome.INCONCLUSIVE
    assert result.training.eligible and result.confirmation.eligible
    assert not result.protocol_valid
    assert result.protocol_reasons == ("held-out cases reuse training scientific inputs: heldout",)
    assert "reuse training scientific inputs" in result.explanation


def test_renamed_input_artifact_with_same_hash_is_not_held_out(workload, rule):
    template = (
        load_workload(FIXTURES / "experiments" / "workload.toml").spec.cases[0].input_artifacts[0]
    )
    train = replace(
        workload.cases[0], arguments={"input": "a"}, input_artifacts=[replace(template, path="a")]
    )
    heldout = replace(
        workload.cases[1], arguments={"input": "b"}, input_artifacts=[replace(template, path="b")]
    )
    workload = replace(workload, cases=[train, heldout])
    assert confirm(workload, rule, evidence(workload)).outcome == Outcome.INCONCLUSIVE


def test_changed_scientific_argument_and_unrecorded_env_override_rejected(workload):
    records = evidence(workload)
    records[-1] = replace(records[-1], argv=[*records[-1].argv[:1], "different", records[-1].cwd])
    assert not score(workload, records).eligible
    records = evidence(workload)
    records[-1] = replace(records[-1], env_overrides={"SCIENCE_SEED": "8"})
    assert not score(workload, records).eligible


def test_other_failed_policy_and_its_baselines_do_not_disqualify_candidate(workload, rule):
    records = evidence(workload)
    other = trial(workload, workload.cases[0], Phase.TRAINING, 50, "other", 0.001)
    other = replace(other, status=TrialStatus.FAILED, return_code=1)
    baseline = trial(workload, workload.cases[0], Phase.TRAINING, 50, "baseline", 10)
    records.extend([baseline, other])
    records.sort(key=lambda record: record.phase != Phase.TRAINING)
    records = [replace(record, order=index) for index, record in enumerate(records)]
    assert confirm(workload, rule, records).outcome == Outcome.VALIDATED_IMPROVEMENT


def test_confirmation_must_follow_completed_training(workload, rule):
    records = evidence(workload)
    records[1] = replace(records[1], order=999)
    result = confirm(workload, rule, records)
    assert result.outcome == Outcome.INCONCLUSIVE
    assert result.training.eligible and result.confirmation.eligible
    assert not result.protocol_valid
    assert any("not fresh" in reason for reason in result.protocol_reasons)
    assert "not fresh" in result.explanation


def test_missing_fixed_environment_is_incompatible_even_with_matched_pairs(workload):
    records = [replace(record, env_overrides={}) for record in evidence(workload)]
    result = score(workload, records)
    assert not result.eligible
    assert "fixed scientific environment" in " ".join(result.reasons)


def test_bootstrap_seed_is_reproducible_without_removing_slow_pairs(workload):
    records = evidence(workload, confirmation=(0.3, 0.9, 1.1, 2, 3, 1.3))
    first = score(workload, records, seed=13)
    second = score(workload, records, seed=13)
    assert first == second
    assert len(first.cases[0].ratios) == 6
    assert first.ratio == pytest.approx(math.prod((0.3, 0.9, 1.1, 2, 3, 1.3)) ** (1 / 6))
    assert first.cases[0].log_ratio_stddev > 0
    assert first.lower < first.ratio < first.upper


def test_incomplete_or_duplicate_pairs_are_not_survivor_averaged(workload):
    records = evidence(workload)
    del records[-2]
    result = score(workload, records)
    assert not result.eligible
    assert result.ratio is None
    assert "incomplete" in " ".join(result.reasons)


def test_baseline_only_incomplete_pair_is_not_silently_discarded(workload):
    records = evidence(workload)
    records.pop()
    result = score(workload, records)
    assert not result.eligible
    assert "no candidate counterpart" in " ".join(result.reasons)


def test_literal_argv_never_interprets_placeholder_looking_arguments(workload):
    records = evidence(workload)
    workload = replace(
        workload,
        argv_template=[workload.resolved_executable, "{case.anything}", "{trial_dir}"],
        extensions={"simtune.literal_argv": True},
        case_substitutions=[],
        cases=[replace(case, arguments={}) for case in workload.cases],
    )
    records = [replace(record, argv=list(workload.argv_template)) for record in records]
    assert score(workload, records).eligible


@pytest.mark.parametrize("saved", [None, 0, -1, float("nan"), float("inf"), True])
def test_break_even_requires_positive_validated_savings(saved):
    assert break_even_runs(10, saved) is None


@pytest.mark.parametrize("cost", [-1, float("nan"), float("inf"), True])
def test_break_even_rejects_invalid_cost(cost):
    with pytest.raises(ValueError):
        break_even_runs(cost, 1)


def test_break_even_exact_units_and_zero_cost():
    assert break_even_runs(10, 3) == 4
    assert break_even_runs(0, 3) == 0


def test_confirmation_preserves_declared_training_pair_requirement(workload, rule):
    result = confirm_policy(
        evidence(workload, training=(1.25, 1.25)),
        workload=workload,
        baseline_policy_id="baseline",
        candidate_policy_id="candidate",
        rule=rule,
        minimum_training_pairs=3,
    )
    assert result.confirmation.eligible
    assert not result.training.eligible and not result.protocol_valid
    assert result.selected_policy_id == "baseline" and result.outcome == Outcome.INCONCLUSIVE
    assert any("needs 3 complete pairs; got 2" in reason for reason in result.protocol_reasons)
