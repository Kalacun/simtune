"""Versioned, read-only decision projection shared by tune, show and explain.

Domain scoring owns eligibility and protocol validity. This projection neither
selects a policy nor executes a workload, and never scores surviving failures.
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

from .experiments import ExperimentStore
from .hardware import LIMITATIONS, hardware_context
from .models import ActionRecord, Outcome, Phase, ResourceSnapshot, TrialRecord, WorkloadSpec
from .search import STATE_KEY, SearchConfig, _read_trials
from .statistics import break_even_runs, confirm_policy, score_policy
from .validation import ValidationError, artifact_path, load_json


def time_reduction_percent(ratio):
    if type(ratio) not in (int, float) or not math.isfinite(ratio) or ratio <= 0:
        return None
    value = 100 * (1 - 1 / ratio)
    return value if math.isfinite(value) else None


def _measurement(score, case=None, reasons=()):
    source = case if case is not None else score
    usable = score is not None and score.eligible and source is not None and not reasons
    data = {
        "baseline_seconds": source.baseline_seconds if usable else None,
        "candidate_seconds": source.candidate_seconds if usable else None,
        "paired_geometric_ratio": source.ratio if usable else None,
        "ratio_interval_95": [source.lower, source.upper]
        if usable and source.lower is not None and source.upper is not None
        else None,
        "time_reduction_percent": time_reduction_percent(source.ratio) if usable else None,
        "time_reduction_interval_95_percent": [
            time_reduction_percent(source.lower),
            time_reduction_percent(source.upper),
        ]
        if usable and source.lower is not None and source.upper is not None
        else None,
    }
    why = "; ".join([*reasons, *(score.reasons if score else ())]) or "no eligible paired evidence"
    data["unavailable"] = {key: why for key, value in data.items() if value is None}
    return data


def decision_report(root: str | Path) -> dict:
    root = Path(root).resolve(strict=True)
    store = ExperimentStore.open(root)
    try:
        return _decision_report(root)
    except (OSError, ValueError, KeyError) as exc:
        # Older/incomplete records remain inspectable when newer evidence is
        # absent. No saved claims or observations are repaired on this path.
        record = store.record
        why = f"decision evidence unavailable: {exc}"
        return dict(
            kind="simtune_decision_report",
            schema_version=1,
            experiment_id=record.id,
            outcome=record.outcome.value,
            explanation=record.explanation,
            selected_policy_id=record.selected_policy_id,
            reference_policy_id=None,
            complete=store.is_complete(),
            completion_marker=record.completion_marker,
            phase=record.extensions.get(STATE_KEY, {}).get("phase"),
            case_policy_rows=[],
            aggregates=[],
            trials=[],
            historical_validation=dict(
                scientific_checks={},
                protocol_valid=None,
                protocol_reasons=[why],
                performance_outcome=record.outcome.value,
                unavailable={"protocol_valid": why},
            ),
            settings=dict(
                requested_policies=[], resolved_plans=[], observed_actions=[], resource_counts=[]
            ),
            candidates=[c.to_dict() for c in record.candidates],
            cost=dict(
                tuning_seconds=record.budget.spent_seconds,
                budget=record.budget.to_dict(),
                conditional_saved_seconds_per_run=None,
                break_even_runs=None,
                case_weights={},
                assumption="No conditional savings inferred from unavailable evidence.",
                unavailable={"conditional_saved_seconds_per_run": why, "break_even_runs": why},
            ),
            hardware=dict(class_id=None, reasons=[why], unavailable={"class_id": why}),
            applicability=dict(
                current_compatibility="not_checked",
                current_output_science="not_run",
                explanation="Historical record only; no current performance claim.",
            ),
            evidence=dict(
                root=str(root), journal=record.journal, completion_marker=record.completion_marker
            ),
            evidence_errors=[why],
            limitations=[why, *LIMITATIONS],
            unavailable={"reference_policy_id": why, **record.unavailable},
        )


def _decision_report(root: str | Path) -> dict:
    """Inspect completed or partial experiments without requiring recipe export."""
    root = Path(root).resolve(strict=True)
    store = ExperimentStore.open(root)
    record = store.record
    state = record.extensions.get(STATE_KEY, {})
    workload = WorkloadSpec.from_json(artifact_path(root, "workload.json").read_text())
    snapshot = ResourceSnapshot.from_json(artifact_path(root, "resources.json").read_text())
    errors = []
    trials = []
    journal_states = {}
    try:
        recovery = store.recover()
        journal_states = {key: value.value for key, value in recovery.states.items()}
        trials = _read_trials(store)
    except (OSError, ValueError, KeyError) as exc:
        errors.append(str(exc))
        # Keep partial/uncertain evidence visible, but suppress every numeric score.
        for trial_id in journal_states:
            path = artifact_path(root, f"trials/{trial_id}/trial.json")
            if not path.is_file():
                continue
            trial = TrialRecord.from_json(path.read_text(), root=root)
            if trial.id != trial_id or trial.experiment_id != record.id:
                raise ValidationError("report trial identity differs from journal")
            trials.append(trial)
    complete = store.is_complete()
    reference = state.get("reference_policy_id")
    config = SearchConfig(**state["config"]) if state.get("config") else None
    scores = {}
    confirmation = None
    if reference is not None and config is not None and not errors:
        for candidate in record.candidates:
            if candidate.policy_id == reference:
                continue
            for phase in (Phase.TRAINING, Phase.CONFIRMATION):
                scores[(candidate.policy_id, phase)] = score_policy(
                    trials,
                    workload=workload,
                    baseline_policy_id=reference,
                    candidate_policy_id=candidate.policy_id,
                    phase=phase,
                    seed=config.seed,
                    minimum_pairs=config.repeat
                    if phase == Phase.TRAINING
                    else record.selection_rule.minimum_confirmation_pairs,
                    bootstrap_samples=config.bootstrap_samples,
                )
        if state.get("proposed_policy_id"):
            confirmation = confirm_policy(
                trials,
                workload=workload,
                baseline_policy_id=reference,
                candidate_policy_id=state["proposed_policy_id"],
                rule=record.selection_rule,
                minimum_training_pairs=config.repeat,
                bootstrap_samples=config.bootstrap_samples,
            )
    case_rows, aggregates = [], []
    cases = {case.id: case for case in workload.cases}
    for candidate in record.candidates:
        if candidate.policy_id == reference:
            continue
        for phase, case_ids in (
            (Phase.TRAINING, workload.training_case_ids),
            (Phase.CONFIRMATION, workload.validation_case_ids),
        ):
            score = scores.get((candidate.policy_id, phase))
            why = list(errors)
            if candidate.exclusion_reason:
                why.append(candidate.exclusion_reason)
            aggregates.append(
                dict(
                    policy_id=candidate.policy_id,
                    phase=phase.value,
                    eligible=bool(score and score.eligible and not why),
                    reasons=list(dict.fromkeys([*why, *(score.reasons if score else ())])),
                    **_measurement(score, reasons=why),
                )
            )
            for case_id in case_ids:
                relevant = [
                    t
                    for t in trials
                    if t.phase == phase
                    and t.case_id == case_id
                    and t.policy_id in {reference, candidate.policy_id}
                ]
                proposals = [t for t in relevant if t.policy_id == candidate.policy_id]
                pairs = {t.pair_id for t in proposals if t.pair_id is not None}
                complete_pairs = sum(
                    len(pair := [t for t in relevant if t.pair_id == pair_id]) == 2
                    and {t.policy_id for t in pair} == {reference, candidate.policy_id}
                    and len({t.order for t in pair}) == 2
                    for pair_id in pairs
                )
                case_score = (
                    next((c for c in score.cases if c.case_id == case_id), None) if score else None
                )
                case_rows.append(
                    dict(
                        case_id=case_id,
                        policy_id=candidate.policy_id,
                        phase=phase.value,
                        phase_label="training selection"
                        if phase == Phase.TRAINING
                        else "held-out confirmation",
                        weight=cases[case_id].weight,
                        complete_pair_count=complete_pairs,
                        eligible=bool(score and score.eligible and case_score and not why),
                        checks=dict(Counter(t.validation.state.value for t in proposals)),
                        reasons=list(dict.fromkeys([*why, *(score.reasons if score else ())])),
                        **_measurement(score, case_score, reasons=why),
                    )
                )
    plans = []
    for trial in trials:
        reference_path = f"trials/{trial.id}/plan.json"
        if reference_path not in trial.raw_artifacts:
            continue
        plan = load_json(artifact_path(root, reference_path).read_text())
        # Deduplicate display only; raw trials and every failure stay in evidence.
        projection = {
            key: plan.get(key)
            for key in (
                "policy",
                "cpu_ids",
                "numa_node_ids",
                "env_overrides",
                "actions",
                "unobserved",
            )
        }
        if projection not in plans:
            plans.append(projection)
    saved = None
    if (
        complete
        and confirmation
        and confirmation.protocol_valid
        and record.outcome == confirmation.outcome == Outcome.VALIDATED_IMPROVEMENT
        and record.selected_policy_id == confirmation.selected_policy_id
        and not errors
    ):
        saved = confirmation.conditional_saved_seconds
    cost = record.budget.spent_seconds
    breakeven = break_even_runs(cost, saved)
    historical_science = dict(Counter(t.validation.state.value for t in trials))
    counts = []
    policies = {p["id"]: p for p in state.get("policies", [])}
    reference_budget = policies.get(reference, {}).get("budget")
    for policy_id, policy in policies.items():
        budget = policy["budget"]
        fields = ("basis", "cpu_count", "thread_count", "memory_bytes")
        counts.append(
            dict(
                policy_id=policy_id,
                declared={k: budget[k] for k in fields},
                same_declared_resources_as_reference=all(
                    budget[k] == reference_budget[k] for k in fields
                )
                if reference_budget
                else None,
                explanation="Equality of declared budgets; inherited/null counts supply no observed capacity guarantee.",
            )
        )
    return dict(
        kind="simtune_decision_report",
        schema_version=1,
        experiment_id=record.id,
        outcome=record.outcome.value,
        explanation=record.explanation,
        selected_policy_id=record.selected_policy_id,
        reference_policy_id=reference,
        complete=complete,
        completion_marker=record.completion_marker,
        phase=state.get("phase"),
        case_policy_rows=case_rows,
        aggregates=aggregates,
        historical_validation=dict(
            scientific_checks=historical_science,
            protocol_valid=confirmation.protocol_valid if confirmation else None,
            protocol_reasons=list(confirmation.protocol_reasons)
            if confirmation
            else ["no proposed-policy confirmation"],
            performance_outcome=record.outcome.value,
            unavailable={}
            if confirmation
            else {"protocol_valid": "no proposed-policy confirmation"},
        ),
        settings=dict(
            requested_policies=state.get("policies", []),
            resolved_plans=plans,
            observed_actions=[
                dict(trial_id=t.id, actions=[a.to_dict() for a in t.actions]) for t in trials
            ],
            resource_counts=counts,
            explanation="Resource counts are declared in each policy budget and resolved CPU mask; observations do not prove every thread or page's placement.",
        ),
        trials=[
            dict(
                id=t.id,
                case_id=t.case_id,
                policy_id=t.policy_id,
                phase=t.phase.value,
                status=t.status.value,
                journal_status=journal_states.get(t.id),
                validation=t.validation.to_dict(),
                evidence=t.raw_artifacts,
            )
            for t in trials
        ],
        journal_states=journal_states,
        candidates=[c.to_dict() for c in record.candidates],
        cost=dict(
            tuning_seconds=cost,
            budget=record.budget.to_dict(),
            conditional_saved_seconds_per_run=saved,
            break_even_runs=breakeven,
            case_weights={
                c.id: c.weight for c in workload.cases if c.id in workload.validation_case_ids
            },
            assumption="Normalized declared held-out case weights; comparable future executions. Full tuning cost includes discovery, probes, warmups, checks and bounded teardown; historical probe counts may be unknown.",
            unavailable={
                key: "no complete validated positive arithmetic savings; conditional break-even unavailable"
                for key, value in (
                    ("conditional_saved_seconds_per_run", saved),
                    ("break_even_runs", breakeven),
                )
                if value is None
            },
        ),
        hardware=hardware_context(snapshot),
        applicability=dict(
            current_compatibility="not_checked",
            current_output_science="not_run",
            explanation="Historical evidence only; current compatibility requires a separate check and exec output remains unchecked.",
        ),
        evidence=dict(
            root=str(root),
            experiment="experiment.json",
            resources="resources.json",
            workload="workload.json",
            journal=record.journal,
            completion_marker=record.completion_marker,
            unavailable={}
            if record.completion_marker
            else {"completion_marker": "experiment is incomplete"},
        ),
        evidence_errors=errors,
        limitations=[
            *record.extensions.get("simtune.stage04", {}).get(
                "untracked_external_dependencies", []
            ),
            "Small samples, correlated timings, drift and declared case weights limit inference.",
            "Settings and mechanism invocation do not prove unobserved thread placement or page residency.",
            *LIMITATIONS,
        ],
        unavailable={
            key: "not recorded"
            for key, value in (
                ("selected_policy_id", record.selected_policy_id),
                ("reference_policy_id", reference),
                ("phase", state.get("phase")),
                ("completion_marker", record.completion_marker),
            )
            if value is None
        },
    )


def render_decision_report(report: dict) -> str:
    """Plain text is predictable with redirected stdout and no optional Rich."""

    def number(value):
        return "—" if value is None else f"{value:.6g}"

    lines = [
        f"{report['experiment_id']}: {report['outcome']}",
        report["explanation"],
        f"Selected: {report['selected_policy_id'] or 'none'}; reference: {report['reference_policy_id'] or 'unknown'}; complete: {report['complete']}",
        "Case | Policy | Phase | Pairs | Checks | Baseline s | Candidate s | B/C ratio [95%] | Time reduction % [95%]",
    ]
    for row in report["case_policy_rows"]:
        interval = row["ratio_interval_95"]
        reduction = row["time_reduction_interval_95_percent"]
        lines.append(
            " | ".join(
                [
                    row["case_id"],
                    row["policy_id"],
                    row["phase_label"],
                    str(row["complete_pair_count"]),
                    str(row["checks"]),
                    number(row["baseline_seconds"]),
                    number(row["candidate_seconds"]),
                    number(row["paired_geometric_ratio"])
                    + (f" [{number(interval[0])}, {number(interval[1])}]" if interval else ""),
                    number(row["time_reduction_percent"])
                    + (f" [{number(reduction[0])}, {number(reduction[1])}]" if reduction else ""),
                ]
            )
        )
        if row["reasons"]:
            lines.append("  Reason: " + "; ".join(row["reasons"]))
    for candidate in report["candidates"]:
        if candidate["exclusion_reason"]:
            lines.append(f"Excluded {candidate['policy_id']}: {candidate['exclusion_reason']}")
    for aggregate in report["aggregates"]:
        if not aggregate["eligible"]:
            continue
        interval = aggregate["ratio_interval_95"]
        lines.append(
            f"Weighted {aggregate['phase']} {aggregate['policy_id']}: arithmetic baseline "
            f"{number(aggregate['baseline_seconds'])} s, candidate {number(aggregate['candidate_seconds'])} s; "
            f"paired geometric B/C {number(aggregate['paired_geometric_ratio'])} "
            + (f"[95%: {number(interval[0])}, {number(interval[1])}]; " if interval else "; ")
            + f"time reduction {number(aggregate['time_reduction_percent'])}%"
        )
    for plan in report["settings"]["resolved_plans"]:
        policy = plan.get("policy") or {}
        lines.append(
            f"Policy {policy.get('id')}: requested resources {policy.get('budget')}; resolved CPUs {plan['cpu_ids']}; memory nodes {plan['numa_node_ids']}"
        )
        for action in plan["actions"] or []:
            lines.append(ActionRecord.from_dict(action).describe())
        for effect in plan["unobserved"] or []:
            lines.append("Unobserved: " + effect)
    observed = []
    for trial in report["settings"]["observed_actions"]:
        for action in trial["actions"]:
            description = ActionRecord.from_dict(action).describe()
            if description not in observed:
                observed.append(description)
                lines.append("Actual trial action: " + description)
    for counts in report["settings"]["resource_counts"]:
        lines.append(
            f"Resources {counts['policy_id']}: {counts['declared']}; same declared resources as reference: {counts['same_declared_resources_as_reference']}"
        )
    historical = report["historical_validation"]
    lines.append(
        f"Historical checks: {historical['scientific_checks']}; confirmation protocol: {historical['protocol_valid']}"
    )
    for reason in historical["protocol_reasons"]:
        lines.append("Protocol reason: " + reason)
    cost = report["cost"]
    lines.extend(
        [
            f"Cost: {number(cost['tuning_seconds'])} seconds; conditional saved seconds/run: {number(cost['conditional_saved_seconds_per_run'])}; break-even runs: {cost['break_even_runs'] if cost['break_even_runs'] is not None else 'unknown'}",
            cost["assumption"],
            report["applicability"]["explanation"],
            "Hardware class: " + (report["hardware"]["class_id"] or "unknown"),
        ]
    )
    for reason in [
        *report["evidence_errors"],
        *report["hardware"]["reasons"],
        *cost["unavailable"].values(),
    ]:
        lines.append("Reason: " + reason)
    for limitation in report["limitations"]:
        lines.append("Limitation: " + limitation)
    lines.append(
        f"Evidence: {report['evidence']['root']}; journal: {report['evidence']['journal']}; completion: {report['complete']}"
    )
    return "\n".join(lines)
