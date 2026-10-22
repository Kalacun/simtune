"""Stage-05 orchestration through real isolated trials and synthetic metrics."""

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from simtune.candidates import generate_candidates
from simtune.experiments import ExperimentStore, HookSpec, TrialExecutionOptions
from simtune.launch import StrictLaunchResult, plan_strict_launch
from simtune.models import (
    CheckKind,
    Outcome,
    OutputCheck,
    Phase,
    Platform,
    ResourceSnapshot,
    TrialRecord,
    TrialStatus,
    ValidationState,
)
from simtune.search import STATE_KEY, SearchConfig, paired_schedule, resume_search, run_search
from simtune.validation import ValidationError
from simtune.workloads import load_workload, sha256_file

FIXTURE = Path(__file__).parent / "fixtures/experiments/workload.toml"
CAPABILITIES = {"platform": "linux", "tools": {}}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def snapshot():
    return ResourceSnapshot(
        id="search-synthetic-linux",
        platform=Platform.LINUX,
        is_wsl=False,
        online_cpu_ids=[0, 1],
        inherited_cpu_ids=[0, 1],
        permitted_cpu_ids=[0, 1],
        effective_cpu_ids=[0, 1],
        cpus=None,
        numa_nodes=None,
        allowed_memory_node_ids=None,
        memory_limits=[],
        sources={
            name: "synthetic two-CPU fixture; no native-platform claim"
            for name in (
                "online_cpu_ids",
                "inherited_cpu_ids",
                "permitted_cpu_ids",
                "effective_cpu_ids",
            )
        },
        scheduler_hints={},
        conflicts=[],
        unavailable={
            "cpus": "not required for inherited CPU placement",
            "numa_nodes": "NUMA search unavailable in this fixture",
            "allowed_memory_node_ids": "NUMA search unavailable in this fixture",
        },
    )


def make_workload(tmp_path, *, checked=True, command_check=False, heldout=True):
    """Write/reload fixed metadata for synthetic-metric orchestration tests."""
    loaded = load_workload(FIXTURE)
    resource = replace(
        loaded.spec.cases[0].resource_needs,
        cpu_count=2,
        thread_count=2,
        reference_cpu_ids=[0, 1],
    )
    training = replace(
        loaded.spec.cases[0],
        id="training",
        arguments={"input": "training-data"},
        input_artifacts=[],
        resource_needs=resource,
    )
    validation = replace(training, id="heldout", arguments={"input": "independent-heldout-data"})
    checks = loaded.spec.output_checks if checked else []
    if command_check:
        checks = [
            *checks,
            OutputCheck(
                id="external-validator",
                method=CheckKind.COMMAND,
                output=None,
                sha256=None,
                json_field=None,
                expected=None,
                absolute_tolerance=None,
                relative_tolerance=None,
                argv=["synthetic-validator"],
                timeout_seconds=2.0,
                rationale="Explicit bounded synthetic validator for launch-accounting tests.",
                unavailable={
                    name: "command validator has no numeric/hash configuration"
                    for name in (
                        "output",
                        "sha256",
                        "json_field",
                        "expected",
                        "absolute_tolerance",
                        "relative_tolerance",
                    )
                },
            ),
        ]
    spec = replace(
        loaded.spec,
        id="stage05-search-fixture",
        cases=[training, validation] if heldout else [training],
        training_case_ids=[training.id],
        validation_case_ids=[validation.id] if heldout else [],
        output_checks=checks,
        limits=replace(
            loaded.spec.limits,
            max_trials=100,
            max_launches=400,
            trial_timeout_seconds=10.0,
        ),
    )
    path = tmp_path / "workload.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(spec.to_json(), encoding="utf-8")
    return load_workload(path)


def make_real_workload(tmp_path):
    """Pin an input-sensitive integer sum program for actual-execution smoke runs.

    OpenMP and Linux metadata are synthetic launch fixtures: the Python program
    does not use OpenMP, and this helper establishes no native placement claim.
    Each case has its own prespecified numeric scientific reference.
    """
    loaded = make_workload(tmp_path)
    script = (Path(__file__).parent / "fixtures/search/workload.py").resolve()
    digest = sha256_file(script)
    cases = [
        replace(
            case,
            arguments={"input": str(count)},
            checks=[
                replace(
                    loaded.spec.output_checks[0],
                    expected=float(count * (count - 1) // 2),
                    rationale="Integer sum must match the declared case's closed-form reference.",
                )
            ],
        )
        for case, count in zip(loaded.spec.cases, (100, 1000))
    ]
    spec = replace(
        loaded.spec,
        id="stage05-real-integer-sum",
        family="integer-sum",
        argv_template=[str(script), "{case.input}", "{trial_dir}"],
        resolved_executable=str(script),
        executable_sha256=digest,
        base_directory=str(script.parent),
        artifacts=[
            replace(
                loaded.spec.artifacts[0],
                id="integer-sum-source",
                path=str(script),
                sha256=digest,
            )
        ],
        cases=cases,
        output_checks=[],
        extensions={
            "simtune.stage05-smoke": {
                "context": "actual Python workload with synthetic Linux/OpenMP launch metadata",
                "claim": "scientific output and orchestration only; no native placement or speedup",
            }
        },
    )
    path = tmp_path / "workload.json"
    path.write_text(spec.to_json(), encoding="utf-8")
    return load_workload(path)


class SyntheticExecution:
    """Strict executor seam; production TrialExecutor retains journals/checks."""

    def __init__(self, clock, *, times=None, failed=(), cost=0.05, hook_cost=0.01):
        self.clock = clock
        self.times = times or {"baseline": 2.0, "runtime-close": 1.0, "runtime-spread": 2.0}
        self.failed = set(failed)
        self.cost = cost
        self.hook_cost = hook_cost
        self.calls = []
        self.hooks = []

    def strict(
        self,
        plan,
        argv,
        capabilities,
        timeout,
        *,
        base_env,
        snapshot_provider,
        cwd,
        before_launch,
    ):
        refreshed = plan_strict_launch(plan.policy, snapshot_provider(), capabilities)
        assert refreshed.resolution_key() == plan.resolution_key()
        timeout = before_launch(timeout)
        assert timeout > 0
        self.calls.append(
            {"policy": plan.policy.id, "case": argv[1], "cwd": cwd, "timeout": timeout}
        )
        self.clock.advance(min(self.cost, timeout))
        timed_out = self.cost >= timeout
        (cwd / "result.json").write_text('{"science":{"value":6}}', encoding="utf-8")
        elapsed = (
            self.times(plan.policy.id, argv[1])
            if callable(self.times)
            else self.times[plan.policy.id]
        )
        return StrictLaunchResult(
            final_argv=tuple(argv),
            metrics={
                "return_code": 124 if timed_out else 17 if plan.policy.id in self.failed else 0,
                "timed_out": timed_out,
                "wall_time_sec": elapsed,
            },
            raw_output="Synthetic timing fixture; no real speedup claim.",
            policy_id=plan.policy.id,
            snapshot_id=plan.snapshot_id,
            actions=plan.actions,
            unobserved=plan.unobserved,
        )

    def command(self, argv, env, timeout, cwd):
        assert timeout > 0
        self.hooks.append({"argv": argv, "timeout": timeout, "cwd": cwd})
        self.clock.advance(min(self.hook_cost, timeout))
        timed_out = self.hook_cost >= timeout
        return {
            "return_code": 124 if timed_out else 0,
            "timed_out": timed_out,
            "wall_time_sec": self.hook_cost,
        }, "synthetic hook/validator"

    @property
    def kwargs(self):
        return {"strict_executor": self.strict, "command_runner": self.command}


def execute(tmp_path, snapshot, *, config=None, checked=True, times=None, failed=(), **kwargs):
    workload = make_workload(tmp_path / "manifest", checked=checked)
    clock = FakeClock()
    synthetic = SyntheticExecution(clock, times=times, failed=failed)
    result = run_search(
        workload,
        tmp_path / "search",
        config or SearchConfig(100, bootstrap_samples=100),
        clock=clock,
        snapshot_provider=lambda: snapshot,
        capabilities=CAPABILITIES,
        executor_kwargs=synthetic.kwargs,
        **kwargs,
    )
    return result, synthetic, workload


def trials(root):
    return [
        TrialRecord.from_json(path.read_text(), root=root)
        for path in sorted((root / "trials").glob("*/trial.json"))
    ]


def test_clear_gain_uses_fresh_heldout_confirmation_and_durable_science(tmp_path, snapshot):
    result, synthetic, workload = execute(tmp_path, snapshot)
    assert result.outcome == Outcome.VALIDATED_IMPROVEMENT
    assert result.selected_policy_id == "runtime-close"
    evidence = trials(tmp_path / "search")
    confirmation = [trial for trial in evidence if trial.phase == Phase.CONFIRMATION]
    training = [trial for trial in evidence if trial.phase == Phase.TRAINING]
    warmups = [trial for trial in evidence if trial.phase == Phase.WARMUP]
    assert len(confirmation) == 10 and len(training) == 12 and len(warmups) == 4
    assert {trial.case_id for trial in confirmation} == {"heldout"}
    assert {trial.case_id for trial in training} == {"training"}
    assert not {trial.pair_id for trial in confirmation} & {trial.pair_id for trial in training}
    assert len({trial.cwd for trial in evidence}) == len(evidence) == len(synthetic.calls)
    assert all(trial.validation.state == ValidationState.PASSED for trial in evidence)
    assert all(trial.status == TrialStatus.SUCCEEDED for trial in evidence)
    assert result.budget.launches_started == result.budget.trials_started == 26
    assert result.workload_fingerprint == workload.workload_fingerprint
    assert ExperimentStore.open(tmp_path / "search").is_complete()
    state = result.extensions[STATE_KEY]
    assert state["confirmation_result"]["confirmation"]["ratio"] == 2
    assert state["break_even_runs"] == 2
    assert state["additional_tuning_seconds"] == pytest.approx(1.3)


@pytest.mark.parametrize("elapsed", [2.0, 3.0, 1.99])
def test_equal_slower_and_weak_gain_keep_reference(tmp_path, snapshot, elapsed):
    result, synthetic, _ = execute(
        tmp_path,
        snapshot,
        times={"baseline": 2.0, "runtime-close": elapsed, "runtime-spread": 2.0},
    )
    assert result.outcome == Outcome.NO_IMPROVEMENT
    assert result.selected_policy_id == "baseline"
    assert result.confirmation_trial_ids == []
    assert len(synthetic.calls) == 16
    assert result.extensions[STATE_KEY]["break_even_runs"] is None


def test_training_winner_losing_heldout_keeps_reference(tmp_path, snapshot):
    def timing(policy, case):
        if policy == "runtime-close":
            return 1 if case == "training-data" else 3
        return 2

    result, _, _ = execute(tmp_path, snapshot, times=timing)
    assert result.extensions[STATE_KEY]["proposed_policy_id"] == "runtime-close"
    assert result.outcome == Outcome.NO_IMPROVEMENT
    assert result.selected_policy_id == "baseline"
    assert len(result.confirmation_trial_ids) == 10


def test_failed_fast_candidate_cannot_win_on_surviving_metrics(tmp_path, snapshot):
    result, _, _ = execute(tmp_path, snapshot, failed=["runtime-close"])
    assert result.outcome == Outcome.NO_IMPROVEMENT
    assert result.selected_policy_id == "baseline"
    score = result.extensions[STATE_KEY]["training_scores"]["runtime-close"]
    assert score["eligible"] is False
    assert any("did not succeed" in reason for reason in score["reasons"])
    assert any(trial.status == TrialStatus.FAILED for trial in trials(tmp_path / "search"))


def test_failed_baseline_stops_search_with_failure_evidence(tmp_path, snapshot):
    result, synthetic, _ = execute(tmp_path, snapshot, failed=["baseline"])
    assert result.outcome == Outcome.FAILED
    assert result.selected_policy_id == "baseline"
    assert result.confirmation_trial_ids == []
    baseline = [trial for trial in trials(tmp_path / "search") if trial.policy_id == "baseline"]
    assert len(baseline) == 1 and baseline[0].status == TrialStatus.FAILED
    assert len(synthetic.calls) <= 2


def test_measurement_timeout_consumes_authorization_without_retry(tmp_path, snapshot):
    workload = make_workload(tmp_path / "manifest")
    clock = FakeClock()
    synthetic = SyntheticExecution(clock, cost=10)
    result = run_search(
        workload,
        tmp_path / "search",
        SearchConfig(2, bootstrap_samples=100),
        snapshot_provider=lambda: snapshot,
        capabilities=CAPABILITIES,
        clock=clock,
        executor_kwargs=synthetic.kwargs,
    )
    assert result.outcome == Outcome.INCONCLUSIVE
    assert result.selected_policy_id == "baseline"
    assert len(synthetic.calls) == result.budget.launches_started == 1
    assert synthetic.calls[0]["timeout"] == 1
    evidence = trials(tmp_path / "search")
    assert len(evidence) == 1 and evidence[0].status == TrialStatus.TIMED_OUT
    assert evidence[0].validation.state == ValidationState.UNKNOWN


def test_unchecked_exploration_cannot_select_or_confirm(tmp_path, snapshot):
    result, _, _ = execute(tmp_path, snapshot, checked=False)
    assert result.outcome == Outcome.INCONCLUSIVE
    assert result.selected_policy_id == "baseline"
    assert result.confirmation_trial_ids == []
    assert "unchecked" in result.extensions[STATE_KEY]["label"]
    assert all(
        trial.validation.state == ValidationState.UNCHECKED for trial in trials(tmp_path / "search")
    )


def test_no_heldout_cases_is_explicitly_provisional(tmp_path, snapshot):
    workload = make_workload(tmp_path / "manifest", heldout=False)
    clock = FakeClock()
    synthetic = SyntheticExecution(clock)
    result = run_search(
        workload,
        tmp_path / "search",
        SearchConfig(100, bootstrap_samples=100),
        snapshot_provider=lambda: snapshot,
        capabilities=CAPABILITIES,
        clock=clock,
        executor_kwargs=synthetic.kwargs,
    )
    assert result.outcome == Outcome.INCONCLUSIVE
    assert "provisional" in result.explanation
    assert result.selected_policy_id == "baseline"
    assert result.confirmation_trial_ids == []


@pytest.mark.parametrize("launches", [1, 9, 10])
def test_unfunded_confirmation_launch_protocol_starts_nothing(tmp_path, snapshot, launches):
    result, synthetic, _ = execute(
        tmp_path,
        snapshot,
        config=SearchConfig(100, max_launches=launches, bootstrap_samples=100),
    )
    assert result.outcome == Outcome.INCONCLUSIVE
    assert result.selected_policy_id == "baseline"
    assert synthetic.calls == []
    assert result.budget.launches_started == 0


def test_hook_validator_and_cleanup_launches_are_reserved_and_counted(tmp_path, snapshot):
    workload = make_workload(tmp_path / "manifest", command_check=True)
    clock = FakeClock()
    synthetic = SyntheticExecution(clock)
    options = TrialExecutionOptions(
        setup=HookSpec(("synthetic-setup", "{trial_dir}"), 2),
        cleanup=HookSpec(("synthetic-cleanup", "{trial_dir}"), 2),
    )
    result = run_search(
        workload,
        tmp_path / "search",
        SearchConfig(100, repeat=1, warmups=0, bootstrap_samples=100),
        snapshot_provider=lambda: snapshot,
        capabilities=CAPABILITIES,
        clock=clock,
        options=options,
        executor_kwargs=synthetic.kwargs,
    )
    assert result.outcome == Outcome.VALIDATED_IMPROVEMENT
    assert len(synthetic.calls) == 14 and len(synthetic.hooks) == 42
    assert result.budget.launches_started == 56
    reserve = result.budget.extensions[STATE_KEY]
    assert reserve["confirmation_reserved_launches"] == 40
    assert reserve["confirmation_launches_started"] == 40
    assert result.budget.charges_seconds["setup"] == pytest.approx(0.14)
    assert result.budget.charges_seconds["check"] == pytest.approx(0.14)
    assert result.budget.charges_seconds["cleanup"] == pytest.approx(0.14)


def test_schedule_is_seeded_balanced_and_retains_unpaired_warmups(tmp_path, snapshot):
    workload = make_workload(tmp_path / "manifest")
    generated = generate_candidates(
        snapshot,
        CAPABILITIES,
        runtime=workload.spec.runtime,
        budget=workload.spec.cases[0].resource_needs,
    )
    kwargs = dict(
        baseline=generated.reference,
        candidates=[policy for policy in generated.policies if policy.id != "baseline"],
        case_ids=workload.spec.training_case_ids,
        phase=Phase.TRAINING,
        pairs=6,
        warmups=2,
        seed=42,
    )
    first = paired_schedule(**kwargs)
    assert first == paired_schedule(**kwargs)
    assert first != paired_schedule(**(kwargs | {"seed": 41}))
    assert len({request.id for request in first}) == len(first)
    assert all(request.pair_id is None for request in first if request.phase == Phase.WARMUP)
    pairs = {}
    for request in first:
        if request.phase == Phase.TRAINING:
            pairs.setdefault(request.pair_id, []).append(request.policy.id)
    assert len(pairs) == 12
    for candidate in ("runtime-close", "runtime-spread"):
        orders = [value for value in pairs.values() if candidate in value]
        assert orders.count(["baseline", candidate]) == orders.count([candidate, "baseline"]) == 3


def exhausted_discovery(tmp_path, snapshot):
    workload = make_workload(tmp_path / "manifest")
    clock = FakeClock()
    synthetic = SyntheticExecution(clock)

    def slow_load():
        clock.advance(1)
        return workload

    result = run_search(
        slow_load,
        tmp_path / "search",
        SearchConfig(0.01, bootstrap_samples=100),
        snapshot_provider=lambda: snapshot,
        capabilities=CAPABILITIES,
        clock=clock,
        executor_kwargs=synthetic.kwargs,
    )
    return result, synthetic, clock


def test_discovery_consumes_deadline_and_no_extra_launches_follow_exhaustion(tmp_path, snapshot):
    result, synthetic, _ = exhausted_discovery(tmp_path, snapshot)
    assert result.outcome == Outcome.INCONCLUSIVE
    assert result.selected_policy_id == "baseline"
    assert result.budget.spent_seconds == result.budget.charges_seconds["discovery"] == 1
    assert result.budget.trials_started == result.budget.launches_started == 0
    assert synthetic.calls == []
    assert result.extensions[STATE_KEY]["resumable"] is True
    assert result.completion_marker is None


def test_resume_adds_time_to_old_ledger_without_rerunning_any_trial(tmp_path, snapshot):
    first, synthetic, clock = exhausted_discovery(tmp_path, snapshot)
    clock.advance(10000)  # Time between active sessions consumes no authorization.
    result = resume_search(
        tmp_path / "search",
        additional_seconds=10,
        snapshot_provider=lambda: snapshot,
        capabilities=CAPABILITIES,
        clock=clock,
        executor_kwargs=synthetic.kwargs,
    )
    assert result.outcome == Outcome.VALIDATED_IMPROVEMENT
    assert result.budget.authorized_seconds == first.budget.authorized_seconds == 0.01
    assert result.budget.additional_seconds == [10]
    assert result.budget.spent_seconds == pytest.approx(2.3)
    assert result.budget.launches_started == len(synthetic.calls) == 26
    assert len({call["cwd"] for call in synthetic.calls}) == 26


def test_resume_without_additional_time_uses_only_unspent_authorization(tmp_path, snapshot):
    _, synthetic, clock = exhausted_discovery(tmp_path, snapshot)
    result = resume_search(
        tmp_path / "search",
        snapshot_provider=lambda: snapshot,
        capabilities=CAPABILITIES,
        clock=clock,
        executor_kwargs=synthetic.kwargs,
    )
    assert result.outcome == Outcome.INCONCLUSIVE
    assert result.budget.additional_seconds == []
    assert synthetic.calls == []


def test_changed_environment_refuses_resume_before_launch(tmp_path, snapshot):
    _, synthetic, clock = exhausted_discovery(tmp_path, snapshot)
    changed = replace(snapshot, id="different-resource-evidence")
    with pytest.raises(ValidationError, match="environment fingerprint"):
        resume_search(
            tmp_path / "search",
            additional_seconds=10,
            snapshot_provider=lambda: changed,
            capabilities=CAPABILITIES,
            clock=clock,
            executor_kwargs=synthetic.kwargs,
        )
    assert synthetic.calls == []


def test_pending_trial_without_terminal_evidence_is_never_retried(tmp_path, snapshot):
    _, synthetic, clock = exhausted_discovery(tmp_path, snapshot)
    store = ExperimentStore.open(tmp_path / "search")
    state = {**store.record.extensions[STATE_KEY], "pending_trial_id": "t000000"}
    store.update_record(
        replace(store.record, extensions={**store.record.extensions, STATE_KEY: state})
    )
    with pytest.raises(ValidationError, match="uncertain pending trial"):
        resume_search(
            store.root,
            additional_seconds=10,
            snapshot_provider=lambda: snapshot,
            capabilities=CAPABILITIES,
            clock=clock,
            executor_kwargs=synthetic.kwargs,
        )
    assert synthetic.calls == []


def test_uncertain_execution_is_durable_and_refuses_resume(tmp_path, snapshot):
    workload = make_workload(tmp_path / "manifest")
    clock = FakeClock()
    calls = []

    def uncertain(plan, argv, capabilities, timeout, *, before_launch, **kwargs):
        before_launch(timeout)
        calls.append(argv)
        raise RuntimeError("synthetic crash after launch authorization")

    with pytest.raises(RuntimeError, match="synthetic crash"):
        run_search(
            workload,
            tmp_path / "search",
            SearchConfig(100, bootstrap_samples=100),
            snapshot_provider=lambda: snapshot,
            capabilities=CAPABILITIES,
            clock=clock,
            executor_kwargs={"strict_executor": uncertain},
        )
    evidence = trials(tmp_path / "search")
    assert len(evidence) == 1 and evidence[0].status == TrialStatus.UNKNOWN
    assert ExperimentStore.open(tmp_path / "search").record.budget.launches_started == 1
    with pytest.raises(ValidationError, match="uncertain trial"):
        resume_search(
            tmp_path / "search",
            additional_seconds=10,
            snapshot_provider=lambda: snapshot,
            capabilities=CAPABILITIES,
            clock=clock,
            executor_kwargs={"strict_executor": uncertain},
        )
    assert len(calls) == 1


def test_dry_run_creates_no_evidence_and_launches_no_work_or_hooks(tmp_path, snapshot):
    result, synthetic, _ = execute(tmp_path, snapshot, dry_run=True)
    assert result["dry_run"] is True
    assert len(result["candidates"]) == 9
    assert result["budget"]["launches_started"] == 0
    assert not (tmp_path / "search").exists()
    assert synthetic.calls == synthetic.hooks == []


def test_completed_experiment_refuses_reexecution(tmp_path, snapshot):
    _, synthetic, _ = execute(tmp_path, snapshot)
    count = len(synthetic.calls)
    with pytest.raises(ValidationError, match="completed search is immutable"):
        resume_search(
            tmp_path / "search",
            additional_seconds=10,
            snapshot_provider=lambda: snapshot,
            capabilities=CAPABILITIES,
            executor_kwargs=synthetic.kwargs,
        )
    assert len(synthetic.calls) == count


def test_persisted_schedule_and_ledger_are_json_exchange_records(tmp_path, snapshot):
    result, _, _ = execute(tmp_path, snapshot)
    stored = json.loads((tmp_path / "search/experiment.json").read_text())
    assert stored["kind"] == "experiment_record" and stored["schema_version"] == 1
    assert stored["selected_policy_id"] == result.selected_policy_id
    assert stored["extensions"][STATE_KEY]["next_index"] == 26
    assert stored["extensions"][STATE_KEY]["pending_trial_id"] is None
    assert len(stored["candidates"]) == 9


def test_tiny_real_workload_with_synthetic_host_validates_outputs(tmp_path, snapshot):
    # The inherited launch executes the actual tiny fixture on the test host.
    # Synthetic Linux resources permit exercising orchestration on macOS too;
    # this asserts no native Linux affinity, NUMA, OpenMP behavior, or speedup.
    workload = make_real_workload(tmp_path / "manifest")
    result = run_search(
        workload,
        tmp_path / "real-search",
        SearchConfig(30, repeat=1, warmups=0, bootstrap_samples=100),
        snapshot_provider=lambda: snapshot,
        capabilities=CAPABILITIES,
    )
    assert result.outcome in {
        Outcome.VALIDATED_IMPROVEMENT,
        Outcome.NO_IMPROVEMENT,
        Outcome.INCONCLUSIVE,
    }
    evidence = trials(tmp_path / "real-search")
    assert evidence
    assert all(trial.status == TrialStatus.SUCCEEDED for trial in evidence)
    assert all(trial.validation.state == ValidationState.PASSED for trial in evidence)
    assert all(Path(trial.cwd, "result.json").is_file() for trial in evidence)
    for trial in evidence:
        observed = json.loads(Path(trial.cwd, "result.json").read_text())["science"]["value"]
        assert observed == workload.case(trial.case_id).checks[0].expected
    assert result.budget.launches_started == len(evidence)


def test_real_smoke_program_uses_distinct_training_and_heldout_inputs(tmp_path):
    workload = make_real_workload(tmp_path / "manifest")
    outputs = []
    for case in workload.spec.cases:
        workspace = tmp_path / case.id
        workspace.mkdir()
        result = subprocess.run(
            [workload.spec.resolved_executable, case.arguments["input"], str(workspace)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        value = json.loads((workspace / "result.json").read_text())["science"]["value"]
        assert value == case.checks[0].expected
        outputs.append(value)
    assert outputs == [4950, 499500]
