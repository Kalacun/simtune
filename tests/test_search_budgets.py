"""Stage-05 adversarial deadlines without performance assertions or host tuning."""

import hashlib
import json
from dataclasses import replace

import pytest
from test_checks import command_check, file_check
from test_experiments import CAPABILITIES, FIXTURE, policy, snapshot

from simtune.checks import run_checks
from simtune.experiments import (
    BudgetController,
    BudgetExhausted,
    ExperimentStore,
    HookSpec,
    TrialExecutionOptions,
    TrialExecutor,
    TrialRequest,
    new_experiment_record,
)
from simtune.models import Phase, TrialStatus, ValidationState
from simtune.workloads import load_workload


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def controller(clock, **overrides):
    arguments = dict(
        authorized_seconds=10,
        confirmation_reserved_seconds=0,
        cleanup_allowance_seconds=1,
        max_trials=20,
        max_launches=40,
        clock=clock,
    )
    arguments.update(overrides)
    return BudgetController(**arguments)


def executor_for(tmp_path, clock, budget, *, discovery=snapshot, command_runner=None, checks=None):
    loaded = load_workload(FIXTURE)
    if checks is not None:
        loaded = replace(loaded, spec=replace(loaded.spec, output_checks=checks))
    selected = policy(loaded)
    record = new_experiment_record(
        experiment_id="budget-tests",
        workload=loaded,
        snapshot=snapshot(),
        capabilities=CAPABILITIES,
        policies=[selected],
        budget=budget,
        seed=1,
    )
    store = ExperimentStore.create(tmp_path / "experiment", record)
    executor = TrialExecutor(
        store=store,
        workload=loaded,
        capabilities=CAPABILITIES,
        budget=budget,
        snapshot_provider=discovery,
        command_runner=command_runner,
        clock=clock,
    )
    return selected, store, executor


def trial(selected, trial_id="trial-1", *, phase=Phase.TRAINING, options=None):
    return TrialRequest(
        id=trial_id,
        case_id="tiny",
        policy=selected,
        phase=phase,
        order=0,
        pair_id="pair-1",
        options=options or TrialExecutionOptions(),
    )


def harmless_workload(monkeypatch, clock, *, elapsed=0.1):
    calls = []

    def run(argv, env, timeout, **kwargs):
        calls.append(timeout)
        clock.advance(elapsed)
        (kwargs["cwd"] / "result.json").write_text('{"science": {"value": 6.0}}')
        return {"return_code": 0, "timed_out": False, "wall_time_sec": elapsed}, "synthetic"

    monkeypatch.setattr("simtune.launch.profiler.execute_command", run)
    return calls


def test_whole_session_elapsed_includes_overhead_and_never_double_counts_spans():
    clock = FakeClock()
    budget = controller(clock)
    clock.advance(2)
    started = clock()
    clock.advance(3)
    assert budget.available_seconds(Phase.TRAINING) == 5
    assert budget.ledger().charges_seconds == {"overhead": 5}
    budget.charge("setup", started)
    clock.advance(1)
    budget.charge("check", started)  # overlapping time is already classified
    ledger = budget.ledger()
    assert ledger.spent_seconds == 6
    assert ledger.charges_seconds == {"setup": 3, "check": 1, "overhead": 2}
    assert sum(ledger.charges_seconds.values()) == budget.spent_seconds


def test_confirmation_count_reserve_blocks_exploration_and_cleanup():
    budget = controller(
        FakeClock(),
        max_trials=3,
        max_launches=4,
        confirmation_reserved_trials=2,
        confirmation_reserved_launches=3,
    )
    budget.begin_trial(Phase.WARMUP)
    budget.begin_launch(Phase.WARMUP, 1)
    with pytest.raises(BudgetExhausted, match="trial count"):
        budget.begin_trial(Phase.TRAINING)
    with pytest.raises(BudgetExhausted, match="launch count"):
        budget.begin_launch(Phase.TRAINING, 1, cleanup=True)
    budget.begin_trial(Phase.CONFIRMATION)
    budget.begin_launch(Phase.CONFIRMATION, 1)
    ledger = budget.ledger()
    assert ledger.extensions["simtune.stage05"]["confirmation_trials_started"] == 1
    assert ledger.extensions["simtune.stage05"]["confirmation_launches_started"] == 1


def test_resume_uses_only_unspent_budget_and_explicit_addition_without_downtime():
    clock = FakeClock()
    budget = controller(
        clock,
        confirmation_reserved_seconds=2,
        confirmation_reserved_trials=2,
        confirmation_reserved_launches=2,
    )
    budget.begin_trial(Phase.TRAINING)
    budget.begin_launch(Phase.TRAINING, 5)
    clock.advance(6)
    ledger = budget.ledger()
    clock.advance(10000)
    restored = BudgetController.from_ledger(ledger, clock=clock)
    assert restored.spent_seconds == 6
    assert restored.available_seconds(Phase.TRAINING) == 2
    assert restored.available_seconds(Phase.CONFIRMATION) == 4
    assert restored.ledger().trials_started == 1
    assert restored.ledger().launches_started == 1
    clock.advance(2)
    with pytest.raises(BudgetExhausted):
        restored.begin_trial(Phase.TRAINING)
    extended = BudgetController.from_ledger(restored.ledger(), additional_seconds=3, clock=clock)
    assert extended.available_seconds(Phase.TRAINING) == 3
    assert extended.ledger().additional_seconds == [3]
    assert extended.confirmation_reserved_trials == 2
    assert extended.confirmation_reserved_launches == 2


def test_slow_discovery_spends_budget_without_workload_launch(tmp_path, monkeypatch):
    clock = FakeClock()
    budget = controller(clock, authorized_seconds=1)
    calls = harmless_workload(monkeypatch, clock)

    def discovery():
        clock.advance(2)
        return snapshot()

    selected, store, executor = executor_for(tmp_path, clock, budget, discovery=discovery)
    result = executor.run(trial(selected))
    assert result.status == TrialStatus.FAILED
    assert result.validation.state == ValidationState.UNKNOWN
    assert calls == [] and budget.launches_started == 0
    assert store.record.budget.spent_seconds == 2
    assert store.record.budget.charges_seconds["discovery"] == 2


def test_immediate_strict_replan_is_inside_deadline_and_before_launch_count(tmp_path, monkeypatch):
    clock = FakeClock()
    budget = controller(clock, authorized_seconds=1)
    calls = harmless_workload(monkeypatch, clock)
    discoveries = []

    def discovery():
        discoveries.append(1)
        if len(discoveries) == 2:
            clock.advance(2)
        return snapshot()

    selected, store, executor = executor_for(tmp_path, clock, budget, discovery=discovery)
    result = executor.run(trial(selected))
    assert result.status == TrialStatus.FAILED
    assert calls == []
    assert budget.launches_started == store.record.budget.launches_started == 0
    assert len(discoveries) == 2


def test_timeout_is_recapped_after_replan_and_durable_authorization(tmp_path, monkeypatch):
    clock = FakeClock()
    budget = controller(clock, authorized_seconds=2)
    calls = harmless_workload(monkeypatch, clock)
    discoveries = []

    def discovery():
        discoveries.append(1)
        if len(discoveries) == 2:
            clock.advance(0.5)
        return snapshot()

    selected, store, executor = executor_for(tmp_path, clock, budget, discovery=discovery)
    original_sync = executor._sync_budget

    def slow_sync():
        if budget.launches_started:
            clock.advance(0.2)
        original_sync()

    monkeypatch.setattr(executor, "_sync_budget", slow_sync)
    result = executor.run(trial(selected))
    assert result.status == TrialStatus.SUCCEEDED
    assert calls == [pytest.approx(1.3)]
    assert store.record.budget.launches_started == 1


def test_slow_persistence_does_not_start_authorized_but_expired_launch(tmp_path, monkeypatch):
    clock = FakeClock()
    budget = controller(clock, authorized_seconds=1)
    calls = harmless_workload(monkeypatch, clock)
    selected, store, executor = executor_for(tmp_path, clock, budget)
    original_sync = executor._sync_budget

    def slow_sync():
        if budget.launches_started:
            clock.advance(2)
        original_sync()

    monkeypatch.setattr(executor, "_sync_budget", slow_sync)
    result = executor.run(trial(selected))
    assert result.status == TrialStatus.FAILED
    assert calls == []
    # The persisted authorization conservatively remains consumed.
    assert store.record.budget.launches_started == 1


def test_setup_overrun_stops_before_workload_and_retains_evidence(tmp_path, monkeypatch):
    clock = FakeClock()
    budget = controller(clock, authorized_seconds=1)
    workload_calls = harmless_workload(monkeypatch, clock)
    hook_timeouts = []

    def runner(argv, env, timeout, cwd):
        hook_timeouts.append(timeout)
        clock.advance(2)
        return {"return_code": 0, "timed_out": False}, "setup returned after deadline"

    selected, store, executor = executor_for(tmp_path, clock, budget, command_runner=runner)
    result = executor.run(
        trial(selected, options=TrialExecutionOptions(setup=HookSpec(("setup",), 8)))
    )
    assert result.status == TrialStatus.FAILED
    assert hook_timeouts == [1] and workload_calls == []
    assert store.record.budget.launches_started == 1
    assert store.record.budget.charges_seconds["setup"] == 2
    assert "trials/trial-1/hooks/setup.json" in result.raw_artifacts


@pytest.mark.parametrize("phase", [Phase.WARMUP, Phase.TRAINING, Phase.CONFIRMATION])
def test_workload_overrun_keeps_real_exit_status_but_cannot_validate(tmp_path, monkeypatch, phase):
    clock = FakeClock()
    budget = controller(clock, authorized_seconds=1)
    calls = harmless_workload(monkeypatch, clock, elapsed=2)
    selected, store, executor = executor_for(tmp_path, clock, budget)
    result = executor.run(trial(selected, phase=phase))
    assert calls == [1]
    assert result.status == TrialStatus.SUCCEEDED and result.return_code == 0
    assert result.validation.state == ValidationState.UNKNOWN
    assert "deadline" in result.status_reason
    with pytest.raises(BudgetExhausted):
        executor.run(trial(selected, "trial-2", phase=phase))
    assert store.record.budget.launches_started == 1


def test_successive_validators_share_remaining_time_and_cannot_validate_overrun(
    tmp_path, monkeypatch
):
    clock = FakeClock()
    budget = controller(clock, authorized_seconds=2)
    harmless_workload(monkeypatch, clock, elapsed=0.5)
    timeouts = []

    def validator(argv, env, timeout, cwd):
        timeouts.append(timeout)
        clock.advance(1)
        return {"return_code": 0, "timed_out": False}, "validator"

    checks = [replace(command_check(["validator"], 5), id=f"check-{n}") for n in range(3)]
    selected, store, executor = executor_for(
        tmp_path, clock, budget, command_runner=validator, checks=checks
    )
    result = executor.run(trial(selected))
    assert timeouts == [1.5, 0.5]
    assert result.return_code == 0 and result.status == TrialStatus.SUCCEEDED
    assert result.validation.state != ValidationState.PASSED
    assert store.record.budget.launches_started == 3
    last = json.loads((store.root / "trials/trial-1/checks/check-2.json").read_text())
    assert last["passed"] is False and "exhausted" in last["explanation"]


def test_validator_count_exhaustion_retains_successful_workload_and_failed_science(
    tmp_path, monkeypatch
):
    clock = FakeClock()
    budget = controller(clock, max_launches=1)
    harmless_workload(monkeypatch, clock)
    calls = []

    def forbidden(*args):
        calls.append(1)
        raise AssertionError("validator must not launch")

    selected, store, executor = executor_for(
        tmp_path, clock, budget, command_runner=forbidden, checks=[command_check(["validator"])]
    )
    result = executor.run(trial(selected))
    assert calls == []
    assert result.status == TrialStatus.SUCCEEDED and result.return_code == 0
    assert result.validation.state == ValidationState.FAILED
    assert store.record.budget.launches_started == 1


@pytest.mark.parametrize("already_exhausted", [False, True])
def test_file_checks_enforce_deadline_before_and_after_read(
    tmp_path, monkeypatch, already_exhausted
):
    clock = FakeClock()
    (tmp_path / "answer.bin").write_bytes(b"fixed")
    digest = hashlib.sha256(b"fixed").hexdigest()
    reads = []

    def slow_hash(path):
        reads.append(path)
        clock.advance(2)
        return digest

    monkeypatch.setattr("simtune.checks.sha256_file", slow_hash)
    if already_exhausted:
        clock.advance(2)
    report = run_checks(
        [file_check(digest)],
        tmp_path,
        tmp_path / "evidence",
        evidence_prefix="checks",
        base_env={},
        deadline=1,
        clock=clock,
    )
    assert report.result.state == ValidationState.FAILED
    assert len(reads) == (0 if already_exhausted else 1)


def test_cleanup_allowance_is_aggregate_and_still_obeys_launch_cap(tmp_path, monkeypatch):
    clock = FakeClock()
    budget = controller(clock, max_launches=4, cleanup_allowance_seconds=1)
    harmless_workload(monkeypatch, clock)
    cleanup_timeouts = []

    def cleanup(argv, env, timeout, cwd):
        cleanup_timeouts.append(timeout)
        clock.advance(timeout)
        return {"return_code": 0, "timed_out": False}, "cleanup"

    selected, store, executor = executor_for(tmp_path, clock, budget, command_runner=cleanup)
    options = TrialExecutionOptions(cleanup=HookSpec(("cleanup",), 0.6))
    first = executor.run(trial(selected, options=options))
    second = executor.run(trial(selected, "trial-2", options=options))
    assert first.validation.state == second.validation.state == ValidationState.PASSED
    assert cleanup_timeouts == [0.6, pytest.approx(0.4)]
    assert store.record.budget.charges_seconds["cleanup"] == pytest.approx(1)
    assert store.record.budget.launches_started == 4
    with pytest.raises(BudgetExhausted):
        budget.begin_launch(Phase.TRAINING, 1, cleanup=True)


def test_cleanup_may_overrun_primary_deadline_only_within_separate_allowance(tmp_path, monkeypatch):
    clock = FakeClock()
    budget = controller(clock, authorized_seconds=0.5, cleanup_allowance_seconds=1)
    harmless_workload(monkeypatch, clock, elapsed=0.2)

    def cleanup(argv, env, timeout, cwd):
        assert timeout == 1
        clock.advance(0.8)
        return {"return_code": 0, "timed_out": False}, "cleanup"

    selected, store, executor = executor_for(tmp_path, clock, budget, command_runner=cleanup)
    result = executor.run(
        trial(selected, options=TrialExecutionOptions(cleanup=HookSpec(("cleanup",), 2)))
    )
    assert result.validation.state == ValidationState.PASSED
    assert store.record.budget.spent_seconds == 1
    assert store.record.budget.charges_seconds["cleanup"] == 0.8
    assert budget.available_seconds(Phase.TRAINING) == pytest.approx(0.3)
    clock.advance(0.31)
    with pytest.raises(BudgetExhausted):
        executor.run(trial(selected, "trial-2"))


def test_workload_own_timeout_overrun_is_ineligible_with_global_time_remaining(
    tmp_path, monkeypatch
):
    clock = FakeClock()
    budget = controller(clock)
    calls = harmless_workload(monkeypatch, clock, elapsed=6)
    selected, _, executor = executor_for(tmp_path, clock, budget)
    result = executor.run(trial(selected))
    assert calls == [5]  # fixed workload manifest limit
    assert budget.available_seconds(Phase.TRAINING) == 4
    assert result.return_code == 0
    assert result.validation.state == ValidationState.UNKNOWN


@pytest.mark.parametrize("hook_name", ["setup", "cleanup"])
def test_hook_own_timeout_overrun_cannot_produce_valid_trial(tmp_path, monkeypatch, hook_name):
    clock = FakeClock()
    budget = controller(clock)
    workload_calls = harmless_workload(monkeypatch, clock)

    def runner(argv, env, timeout, cwd):
        assert timeout == 0.25
        clock.advance(0.5)
        return {"return_code": 0, "timed_out": False}, "late hook"

    selected, store, executor = executor_for(tmp_path, clock, budget, command_runner=runner)
    options = TrialExecutionOptions(**{hook_name: HookSpec((hook_name,), 0.25)})
    result = executor.run(trial(selected, options=options))
    assert result.validation.state == ValidationState.UNKNOWN
    assert len(workload_calls) == (hook_name == "cleanup")
    assert result.return_code == (0 if hook_name == "cleanup" else None)
    artifact = json.loads((store.root / f"trials/trial-1/hooks/{hook_name}.json").read_text())
    assert artifact["metrics"]["return_code"] == 0
    assert artifact["metrics"]["simtune_budget_overrun"] is True


def test_validator_own_timeout_overrun_fails_before_global_deadline(tmp_path):
    clock = FakeClock()

    def runner(argv, env, timeout, cwd):
        assert timeout == 0.25
        clock.advance(0.5)
        return {"return_code": 0, "timed_out": False}, "late validator"

    report = run_checks(
        [command_check(["validator"], 0.25)],
        tmp_path,
        tmp_path / "checks",
        evidence_prefix="checks",
        base_env={},
        deadline=10,
        clock=clock,
        command_runner=runner,
    )
    assert report.result.state == ValidationState.FAILED
    assert "authorized timeout" in report.outcomes[0].explanation


def test_slow_check_evidence_write_cannot_validate_overrun(tmp_path, monkeypatch):
    import simtune.checks as checks_module

    clock = FakeClock()
    (tmp_path / "answer.bin").write_bytes(b"fixed")
    digest = hashlib.sha256(b"fixed").hexdigest()
    original_write = checks_module._atomic_json

    def slow_write(path, value):
        clock.advance(2)
        original_write(path, value)

    monkeypatch.setattr(checks_module, "_atomic_json", slow_write)
    report = run_checks(
        [file_check(digest)],
        tmp_path,
        tmp_path / "checks",
        evidence_prefix="checks",
        base_env={},
        deadline=1,
        clock=clock,
    )
    assert report.result.state == ValidationState.FAILED
    evidence = json.loads((tmp_path / "checks/hash.json").read_text())
    assert evidence["passed"] is False


def test_additional_resume_time_does_not_reset_count_authorization():
    clock = FakeClock()
    budget = controller(clock, max_trials=1, max_launches=1)
    budget.begin_trial(Phase.CONFIRMATION)
    budget.begin_launch(Phase.CONFIRMATION, 1)
    resumed = BudgetController.from_ledger(budget.ledger(), additional_seconds=20, clock=clock)
    with pytest.raises(BudgetExhausted, match="trial count"):
        resumed.begin_trial(Phase.CONFIRMATION)
    with pytest.raises(BudgetExhausted, match="launch count"):
        resumed.begin_launch(Phase.CONFIRMATION, 1)


def test_resume_accounts_recovery_startup_but_not_inactive_downtime():
    clock = FakeClock()
    budget = controller(clock)
    clock.advance(2)
    saved = budget.ledger()
    clock.advance(10000)
    started = clock()
    clock.advance(3)
    restored = BudgetController.from_ledger(saved, clock=clock, session_started=started)
    assert restored.spent_seconds == 5
    assert restored.available_seconds(Phase.TRAINING) == 5
    assert restored.ledger().charges_seconds["overhead"] == 5


def test_unpaired_warmup_retains_null_pair_reason(tmp_path, monkeypatch):
    clock = FakeClock()
    budget = controller(clock)
    harmless_workload(monkeypatch, clock)
    selected, _, executor = executor_for(tmp_path, clock, budget)
    result = executor.run(replace(trial(selected, phase=Phase.WARMUP), pair_id=None))
    assert result.pair_id is None
    assert result.unavailable["pair_id"] == "warmup or unpaired trial"
    assert result.validation.state == ValidationState.PASSED


def test_exploratory_cleanup_preserves_confirmation_time_reserve(tmp_path, monkeypatch):
    clock = FakeClock()
    budget = controller(clock, authorized_seconds=2, confirmation_reserved_seconds=1)
    harmless_workload(monkeypatch, clock, elapsed=0.8)

    def cleanup(argv, env, timeout, cwd):
        clock.advance(0.9)
        return {"return_code": 0, "timed_out": False}, "cleanup"

    selected, _, executor = executor_for(tmp_path, clock, budget, command_runner=cleanup)
    result = executor.run(
        trial(selected, options=TrialExecutionOptions(cleanup=HookSpec(("cleanup",), 1)))
    )
    assert result.validation.state == ValidationState.PASSED
    assert budget.ledger().spent_seconds == pytest.approx(1.7)
    assert budget.available_seconds(Phase.TRAINING) == pytest.approx(0.2)
    assert budget.available_seconds(Phase.CONFIRMATION) == pytest.approx(1.2)
    restored = BudgetController.from_ledger(budget.ledger(), clock=clock)
    assert restored.available_seconds(Phase.CONFIRMATION) == pytest.approx(1.2)
    assert restored.available_seconds(Phase.CONFIRMATION, cleanup=True) == pytest.approx(0.1)


def test_cleanup_overrun_does_not_manufacture_remaining_primary_authorization():
    clock = FakeClock()
    budget = controller(clock, authorized_seconds=2, cleanup_allowance_seconds=1)
    started = clock()
    clock.advance(4)
    budget.charge("cleanup", started)
    assert budget.spent_seconds == 4
    assert budget.available_seconds(Phase.TRAINING) == 0
    assert budget.available_seconds(Phase.CONFIRMATION, cleanup=True) == 0
    with pytest.raises(BudgetExhausted):
        budget.begin_launch(Phase.CONFIRMATION, 1)


def fake_perf(monkeypatch, clock, *, elapsed=(0.1, 0.1), code=0):
    from simtune import profiler

    calls = []

    def run(argv, env, timeout, **kwargs):
        calls.append((argv, timeout))
        clock.advance(elapsed[min(len(calls) - 1, len(elapsed) - 1)])
        return {"return_code": code, "timed_out": False}, "STDOUT:\nperf synthetic\nSTDERR:\n"

    monkeypatch.setattr(profiler, "execute_command", run)
    return calls


PERF_CAPS = {"platform": "linux", "tools": {"perf": {"path": "/synthetic/perf"}}}


@pytest.mark.parametrize("exhausted", ["time", "count", "reserve"])
def test_collector_probes_require_remaining_primary_authorization(monkeypatch, exhausted):
    from simtune.collectors import probe

    clock = FakeClock()
    budget = controller(clock, max_launches=1, confirmation_reserved_seconds=5)
    calls = fake_perf(monkeypatch, clock)
    if exhausted == "time":
        clock.advance(5)
    elif exhausted == "count":
        budget.begin_launch(Phase.TRAINING, 1)
    else:
        budget.reserve_counts(trials=0, launches=1)
    with pytest.raises(BudgetExhausted):
        probe("perf-stat", PERF_CAPS, authorization=budget)
    assert calls == []
    assert budget.available_seconds(Phase.CONFIRMATION) > 0


def test_second_collector_probe_sees_first_time_and_caps_timeout(monkeypatch):
    from simtune.collectors import probe

    clock = FakeClock()
    budget = controller(clock, authorized_seconds=1)
    calls = fake_perf(monkeypatch, clock, elapsed=(0.6, 0.2))
    result = probe("perf-stat", PERF_CAPS, authorization=budget)
    assert result.perf_version == "perf synthetic"
    assert [call[1] for call in calls] == [1, pytest.approx(0.4)]
    assert budget.launches_started == 2
    assert budget.ledger().spent_seconds == pytest.approx(0.8)
    assert budget.ledger().charges_seconds["collector_probe"] == pytest.approx(0.8)


@pytest.mark.parametrize("boundary", ["time", "count", "failure"])
def test_first_probe_exhaustion_or_failure_starts_no_second_probe(monkeypatch, boundary):
    from simtune.collectors import probe
    from simtune.validation import ValidationError

    clock = FakeClock()
    budget = controller(clock, authorized_seconds=1, max_launches=1 if boundary == "count" else 40)
    calls = fake_perf(
        monkeypatch,
        clock,
        elapsed=(1 if boundary == "time" else 0.1,),
        code=1 if boundary == "failure" else 0,
    )
    events = []
    with pytest.raises(ValidationError if boundary == "failure" else BudgetExhausted):
        probe("perf-stat", PERF_CAPS, authorization=budget, record_probe=events.append)
    assert len(calls) == budget.launches_started == 1
    assert events[0]["status"] == "reserved"
    assert events[1]["status"] == ("failed" if boundary == "failure" else "succeeded")
    assert budget.spent_seconds == pytest.approx(1 if boundary == "time" else 0.1)


def test_probe_persistence_overrun_keeps_reservation_without_spawn(monkeypatch):
    from simtune.collectors import probe

    clock = FakeClock()
    budget = controller(clock, authorized_seconds=1)
    calls = fake_perf(monkeypatch, clock)
    events = []

    def record(event):
        events.append(event)
        if event["status"] == "reserved":
            clock.advance(2)

    with pytest.raises(BudgetExhausted):
        probe("perf-stat", PERF_CAPS, authorization=budget, record_probe=record)
    assert calls == []
    assert budget.launches_started == 1
    assert events[-1]["status"] == "budget_exhausted"
    assert budget.ledger().spent_seconds == 2


def perf_search(tmp_path, monkeypatch, host, *, config=None, slow_discovery=0, dry_run=False):
    from test_search import SyntheticExecution, make_workload

    from simtune.search import SearchConfig, run_search

    clock = FakeClock()
    calls = fake_perf(monkeypatch, clock)
    synthetic = SyntheticExecution(clock)

    def strict(*args, collector_mode=None, before_launch, **kwargs):
        return synthetic.strict(*args, before_launch=before_launch, **kwargs)

    def discovery():
        clock.advance(slow_discovery)
        return host

    result = run_search(
        make_workload(tmp_path / "manifest"),
        tmp_path / "search",
        config or SearchConfig(100, repeat=1, warmups=0, bootstrap_samples=100),
        snapshot_provider=discovery,
        capabilities=PERF_CAPS,
        clock=clock,
        options=TrialExecutionOptions(collectors="perf-stat"),
        dry_run=dry_run,
        executor_kwargs={**synthetic.kwargs, "strict_executor": strict},
    )
    return result, clock, calls, synthetic


def test_search_discovery_exhaustion_returns_structured_no_store_result(tmp_path, monkeypatch):
    from test_search import snapshot as search_host

    from simtune.search import SearchConfig

    result, _, calls, synthetic = perf_search(
        tmp_path,
        monkeypatch,
        search_host.__wrapped__(),
        slow_discovery=1,
        config=SearchConfig(1, repeat=1, warmups=0, bootstrap_samples=100),
    )
    assert result["outcome"] == "inconclusive"
    assert "authorization exhausted" in result["explanation"]
    assert result["completion_marker"] is None
    assert result["budget"]["launches_started"] == 0
    assert result["budget"]["spent_seconds"] == 1
    assert calls == synthetic.calls == []
    assert not (tmp_path / "search").exists()


def test_search_probe_counts_enter_total_protocol_feasibility(tmp_path, monkeypatch):
    from test_search import snapshot as search_host

    from simtune.search import SearchConfig

    result, _, calls, synthetic = perf_search(
        tmp_path,
        monkeypatch,
        search_host.__wrapped__(),
        config=SearchConfig(100, repeat=1, warmups=0, max_launches=14, bootstrap_samples=100),
    )
    assert result.outcome.value == "inconclusive"
    assert "launch caps" in result.explanation
    assert len(calls) == result.budget.launches_started == 2
    assert synthetic.calls == []
    assert result.budget.extensions["simtune.stage05"]["confirmation_reserved_launches"] == 10


def test_perf_dry_run_probes_zero_and_creates_no_store(tmp_path, monkeypatch):
    from test_search import snapshot as search_host

    result, _, calls, synthetic = perf_search(
        tmp_path, monkeypatch, search_host.__wrapped__(), dry_run=True
    )
    assert result["dry_run"]
    assert calls == synthetic.calls == []
    assert result["budget"]["launches_started"] == 0
    assert not (tmp_path / "search").exists()


def test_probe_failure_is_structured_and_never_retries_workload(tmp_path, monkeypatch):
    from test_search import make_workload
    from test_search import snapshot as search_host

    from simtune.search import SearchConfig, run_search

    clock = FakeClock()
    calls = fake_perf(monkeypatch, clock, code=1)
    result = run_search(
        make_workload(tmp_path / "manifest"),
        tmp_path / "search",
        SearchConfig(100),
        snapshot_provider=search_host.__wrapped__,
        capabilities=PERF_CAPS,
        clock=clock,
        options=TrialExecutionOptions(collectors="perf-stat"),
    )
    assert result["outcome"] == "failed"
    assert len(calls) == result["budget"]["launches_started"] == 1
    assert result["collector_probes"][-1]["raw_output"].startswith("STDOUT:")
    assert not (tmp_path / "search").exists()


def test_old_ledger_retains_totals_and_labels_unknown_historical_probe_counts():
    clock = FakeClock()
    budget = controller(clock)
    budget.begin_launch(Phase.TRAINING, 1)
    clock.advance(2)
    old = replace(
        budget.ledger(),
        extensions={"simtune.stage05": budget.ledger().extensions["simtune.stage05"]},
    )
    restored = BudgetController.from_ledger(old, clock=clock)
    ledger = restored.ledger()
    assert ledger.launches_started == old.launches_started == 1
    assert ledger.spent_seconds == old.spent_seconds == 2
    assert ledger.extensions["simtune.release03"]["collector_probe_counts"] == "historical_unknown"
    assert old.extensions.get("simtune.release03") is None


def test_actual_probe_timeout_uses_owned_process_cleanup(tmp_path):
    import os
    import signal
    import sys

    from simtune.collectors import probe
    from simtune.validation import ValidationError

    ready = tmp_path / "ready"
    tool = tmp_path / "fake-perf"
    tool.write_text(
        f'#!{sys.executable}\nimport os, signal, time\nfrom pathlib import Path\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nPath({str(ready)!r}).write_text(str(os.getpid()))\nprint("probe ready", flush=True)\ntime.sleep(15)\n'
    )
    tool.chmod(0o755)
    budget = BudgetController(
        authorized_seconds=5,
        confirmation_reserved_seconds=0,
        cleanup_allowance_seconds=0,
        max_trials=1,
        max_launches=2,
    )
    events = []
    try:
        with pytest.raises(ValidationError, match="timed out"):
            probe(
                "perf-stat",
                {"platform": "linux", "tools": {"perf": {"path": str(tool)}}},
                authorization=budget,
                record_probe=events.append,
            )
        assert ready.exists()  # Readiness evidence, not a timing/speed assertion.
        pid = int(ready.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert budget.launches_started == 1
        assert events[-1]["metrics"]["timed_out"]
        assert "probe ready" in events[-1]["raw_output"]
    finally:
        if ready.exists():
            try:
                os.kill(int(ready.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def interrupted_perf_search(tmp_path, monkeypatch):
    from test_search import SyntheticExecution, make_workload
    from test_search import snapshot as search_host

    from simtune.search import SearchConfig, run_search

    host = search_host.__wrapped__()
    clock = FakeClock()
    calls = fake_perf(monkeypatch, clock)
    synthetic = SyntheticExecution(clock)

    def strict(*args, collector_mode=None, before_launch, **kwargs):
        return synthetic.strict(*args, before_launch=before_launch, **kwargs)

    def crash(self, record):
        raise RuntimeError("crash after durable terminal trial")

    root = tmp_path / "search"
    with monkeypatch.context() as patch:
        patch.setattr(TrialExecutor, "_sync_experiment", crash)
        with pytest.raises(RuntimeError, match="durable terminal"):
            run_search(
                make_workload(tmp_path / "manifest"),
                root,
                SearchConfig(100, repeat=1, warmups=0, bootstrap_samples=100),
                snapshot_provider=lambda: host,
                capabilities=PERF_CAPS,
                clock=clock,
                options=TrialExecutionOptions(collectors="perf-stat"),
                executor_kwargs={**synthetic.kwargs, "strict_executor": strict},
            )
    assert len(calls) == 2 and len(synthetic.calls) == 1
    calls.clear()
    return root, host, clock, calls, synthetic, {**synthetic.kwargs, "strict_executor": strict}


@pytest.mark.parametrize("boundary", ["time", "count", "reserve"])
def test_perf_resume_preserves_consumed_authorization_and_records_no_launch(
    tmp_path, monkeypatch, boundary
):
    from simtune.search import resume_search

    root, host, clock, calls, synthetic, kwargs = interrupted_perf_search(tmp_path, monkeypatch)
    store = ExperimentStore.open(root)
    ledger = store.record.budget
    if boundary == "time":
        ledger = replace(ledger, spent_seconds=100, charges_seconds={"historical": 100})
    elif boundary == "count":
        ledger = replace(ledger, max_launches=ledger.launches_started)
    else:
        ledger = replace(
            ledger,
            max_launches=ledger.launches_started
            + ledger.extensions["simtune.stage05"]["confirmation_reserved_launches"],
        )
    store.update_record(replace(store.record, budget=ledger))
    before = (root / "trials/t000000/trial.json").read_bytes()
    result = resume_search(
        root,
        additional_seconds=10 if boundary == "count" else 0,
        clock=clock,
        snapshot_provider=lambda: host,
        capabilities=PERF_CAPS,
        executor_kwargs=kwargs,
    )
    assert result.outcome.value == "inconclusive"
    assert calls == [] and len(synthetic.calls) == 1
    assert result.budget.launches_started == ledger.launches_started == 3
    assert result.budget.spent_seconds == ledger.spent_seconds
    assert result.completion_marker is None and not ExperimentStore.open(root).is_complete()
    assert (root / "trials/t000000/trial.json").read_bytes() == before
    assert result.budget.additional_seconds == ([10] if boundary == "count" else [])
    assert result.budget.extensions["simtune.stage05"]["confirmation_reserved_launches"] == 10


def test_perf_resume_second_probe_sees_prior_and_current_spend(tmp_path, monkeypatch):
    from simtune.search import resume_search

    root, host, clock, _, synthetic, kwargs = interrupted_perf_search(tmp_path, monkeypatch)
    store = ExperimentStore.open(root)
    ledger = replace(store.record.budget, spent_seconds=49, charges_seconds={"historical": 49})
    store.update_record(replace(store.record, budget=ledger))
    calls = fake_perf(monkeypatch, clock, elapsed=(0.6, 0.4))
    result = resume_search(
        root,
        clock=clock,
        snapshot_provider=lambda: host,
        capabilities=PERF_CAPS,
        executor_kwargs=kwargs,
    )
    assert [timeout for _, timeout in calls] == [1, pytest.approx(0.4)]
    assert result.budget.launches_started == 5
    assert result.budget.spent_seconds == 50
    assert len(synthetic.calls) == 1
    assert result.completion_marker is None
    assert result.budget.confirmation_reserved_seconds == 50


def test_perf_resume_failed_probe_retains_ledger_and_raw_failure(tmp_path, monkeypatch):
    from simtune.search import STATE_KEY, resume_search

    root, host, clock, _, synthetic, kwargs = interrupted_perf_search(tmp_path, monkeypatch)
    before = ExperimentStore.open(root).record.budget
    calls = fake_perf(monkeypatch, clock, code=17)
    result = resume_search(
        root,
        clock=clock,
        snapshot_provider=lambda: host,
        capabilities=PERF_CAPS,
        executor_kwargs=kwargs,
    )
    assert result.outcome.value == "failed"
    assert len(calls) == 1 and len(synthetic.calls) == 1
    assert result.budget.launches_started == before.launches_started + 1
    assert result.budget.spent_seconds == pytest.approx(before.spent_seconds + 0.1)
    assert result.extensions[STATE_KEY]["collector_probes"][-1]["metrics"]["return_code"] == 17
    assert result.completion_marker is None
    assert not ExperimentStore.open(root).is_complete()


def test_perf_resume_uncertain_probe_is_not_retried(tmp_path, monkeypatch):
    from simtune.search import STATE_KEY, resume_search
    from simtune.validation import ValidationError

    root, host, clock, calls, _, kwargs = interrupted_perf_search(tmp_path, monkeypatch)
    store = ExperimentStore.open(root)
    state = {**store.record.extensions[STATE_KEY], "pending_collector_probe": "version"}
    store.update_record(
        replace(store.record, extensions={**store.record.extensions, STATE_KEY: state})
    )
    before = (root / "experiment.json").read_bytes()
    with pytest.raises(ValidationError, match="uncertain collector probe"):
        resume_search(
            root,
            clock=clock,
            snapshot_provider=lambda: host,
            capabilities=PERF_CAPS,
            executor_kwargs=kwargs,
        )
    assert calls == []
    assert (root / "experiment.json").read_bytes() == before


def test_successful_perf_search_counts_all_probes_and_keeps_confirmation(tmp_path, monkeypatch):
    from test_search import snapshot as search_host

    result, _, calls, synthetic = perf_search(tmp_path, monkeypatch, search_host.__wrapped__())
    assert result.outcome.value == "validated_improvement"
    assert len(calls) == 2 and len(synthetic.calls) == 14
    assert result.budget.launches_started == 16
    counts = result.budget.extensions["simtune.stage05"]
    assert counts["confirmation_reserved_launches"] == counts["confirmation_launches_started"] == 10


def test_legacy_perf_resume_retains_unknown_history_and_charges_new_probes(tmp_path, monkeypatch):
    from simtune.search import STATE_KEY, resume_search

    root, host, clock, calls, synthetic, kwargs = interrupted_perf_search(tmp_path, monkeypatch)
    store = ExperimentStore.open(root)
    old = replace(
        store.record.budget,
        extensions={
            key: value
            for key, value in store.record.budget.extensions.items()
            if key != "simtune.release03"
        },
    )
    store.update_record(replace(store.record, budget=old))
    result = resume_search(
        root,
        clock=clock,
        snapshot_provider=lambda: host,
        capabilities=PERF_CAPS,
        executor_kwargs=kwargs,
    )
    assert (
        result.budget.launches_started == 18
    )  # Prior 3 + two fresh probes + 13 never-started trials.
    assert len(calls) == 2 and len(synthetic.calls) == 14
    assert (
        result.budget.extensions["simtune.release03"]["collector_probe_counts"]
        == "historical_unknown"
    )
    assert any(
        "Historical collector probe counts are unknown" in note
        for note in result.extensions[STATE_KEY]["notes"]
    )


def test_search_known_memory_contradiction_excludes_all_launches(tmp_path, monkeypatch):
    from test_search import make_workload
    from test_search import snapshot as search_host

    from simtune.models import LimitState, MemoryLimit
    from simtune.search import SearchConfig, run_search

    loaded = make_workload(tmp_path / "manifest")
    spec = replace(
        loaded.spec,
        cases=[
            replace(
                case, resource_needs=replace(case.resource_needs, memory_bytes=4096, unavailable={})
            )
            for case in loaded.spec.cases
        ],
    )
    loaded.manifest_path.write_text(spec.to_json())
    host = replace(
        search_host.__wrapped__(),
        memory_limits=[
            MemoryLimit(source="ancestor", scope="allocation", state=LimitState.LIMITED, bytes=1024)
        ],
    )
    monkeypatch.setattr("simtune.profiler.execute_command", lambda *a, **k: pytest.fail("launched"))
    result = run_search(
        load_workload(loaded.manifest_path),
        tmp_path / "search",
        SearchConfig(100),
        snapshot_provider=lambda: host,
        capabilities={"platform": "linux", "tools": {}},
    )
    assert result.outcome.value == "inconclusive"
    assert result.budget.launches_started == 0
    assert all(item.exclusion_reason is not None for item in result.candidates)
    assert any("1024 bytes" in item.exclusion_reason for item in result.candidates)


@pytest.mark.parametrize(
    "parent_signal,termination_known", [(None, True), (15, True), (None, False)]
)
def test_startup_probe_failure_cli_returns_actionable_result_without_store(
    tmp_path, monkeypatch, capsys, parent_signal, termination_known
):
    from test_search import make_workload
    from test_search import snapshot as search_host

    from simtune import cli, tune
    from simtune.search import run_search

    clock = FakeClock()
    loaded = make_workload(tmp_path / "manifest")

    def probe_command(argv, env, timeout, **kwargs):
        clock.advance(0.1)
        return {
            "return_code": 124
            if not termination_known
            else 17
            if parent_signal is None
            else -parent_signal,
            "timed_out": not termination_known,
            "cancellation": {
                "parent_signal": parent_signal,
                "termination_known": termination_known,
            },
        }, "partial probe output"

    def search(*args, **kwargs):
        return run_search(
            *args,
            **kwargs,
            clock=clock,
            snapshot_provider=search_host.__wrapped__,
            capabilities=PERF_CAPS,
        )

    monkeypatch.setattr("simtune.profiler.execute_command", probe_command)
    monkeypatch.setattr(tune, "run_search", search)
    status = cli.main(
        [
            "tune",
            "--workload",
            str(loaded.manifest_path),
            "--budget",
            "100s",
            "--collectors",
            "perf-stat",
            "--out",
            str(tmp_path / "search"),
            "--save",
            str(tmp_path / "recipe.json"),
            "--json",
        ]
    )
    assert status == (124 if not termination_known else 1 if parent_signal is None else 143)
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == (
        "failed" if parent_signal is None and termination_known else "interrupted"
    )
    assert result["budget"]["launches_started"] == 1
    assert result["collector_probes"][-1]["status"] == (
        "uncertain"
        if not termination_known
        else "failed"
        if parent_signal is None
        else "interrupted"
    )
    assert result["completion_marker"] is None and result["remedy"]
    assert result["kind"] == "simtune_tune_startup_result"
    assert result["recipe_path"] is None
    assert result["recipe_export"] == {
        "status": "skipped",
        "reason": "experiment_not_started",
        "requested_path": str(tmp_path / "recipe.json"),
    }
    assert not (tmp_path / "search").exists() and not (tmp_path / "recipe.json").exists()


def test_budget_limited_probe_timeout_is_inconclusive_with_raw_evidence(tmp_path, monkeypatch):
    from test_search import make_workload
    from test_search import snapshot as search_host

    from simtune.search import SearchConfig, run_search

    clock = FakeClock()
    calls = []

    def timeout_probe(argv, env, timeout, **kwargs):
        calls.append(timeout)
        clock.advance(timeout + 0.1)  # Include actual teardown overhead.
        return {"return_code": 124, "timed_out": True}, "partial probe output"

    monkeypatch.setattr("simtune.profiler.execute_command", timeout_probe)
    result = run_search(
        make_workload(tmp_path / "manifest"),
        tmp_path / "search",
        SearchConfig(1),
        snapshot_provider=search_host.__wrapped__,
        capabilities=PERF_CAPS,
        clock=clock,
        options=TrialExecutionOptions(collectors="perf-stat"),
    )
    assert calls == [0.5]
    assert result["outcome"] == "inconclusive"
    assert result["budget"]["spent_seconds"] == pytest.approx(0.6)
    assert result["budget"]["launches_started"] == 1
    event = result["collector_probes"][-1]
    assert event["status"] == "budget_exhausted"
    assert event["metrics"]["timed_out"] and event["raw_output"] == "partial probe output"
    assert not (tmp_path / "search").exists()


@pytest.mark.parametrize("drop_pending", [False, True])
def test_uncertain_probe_termination_blocks_resume_from_intent_or_metrics(
    tmp_path, monkeypatch, drop_pending
):
    from simtune.search import STATE_KEY, resume_search
    from simtune.validation import ValidationError

    root, host, clock, _, synthetic, kwargs = interrupted_perf_search(tmp_path, monkeypatch)
    before = ExperimentStore.open(root).record.budget
    calls = []

    def uncertain(argv, env, timeout, **kwargs):
        calls.append(argv)
        clock.advance(0.1)
        return {
            "return_code": 124,
            "timed_out": True,
            "cancellation": {"parent_signal": None, "termination_known": False},
        }, "partial unknown teardown"

    monkeypatch.setattr("simtune.profiler.execute_command", uncertain)
    result = resume_search(
        root,
        clock=clock,
        snapshot_provider=lambda: host,
        capabilities=PERF_CAPS,
        executor_kwargs=kwargs,
    )
    assert result.outcome.value == "interrupted"
    assert result.extensions[STATE_KEY]["interruption_exit_status"] == 124
    assert result.budget.launches_started == before.launches_started + 1
    assert result.extensions[STATE_KEY]["collector_probes"][-1]["status"] == "uncertain"
    assert result.extensions[STATE_KEY]["pending_collector_probe"] == "version"
    assert result.completion_marker is None and len(synthetic.calls) == 1
    if drop_pending:
        store = ExperimentStore.open(root)
        state = {**store.record.extensions[STATE_KEY], "pending_collector_probe": None}
        store.update_record(
            replace(store.record, extensions={**store.record.extensions, STATE_KEY: state})
        )
    with pytest.raises(ValidationError, match="uncertain collector probe"):
        resume_search(
            root,
            additional_seconds=10,
            clock=clock,
            snapshot_provider=lambda: host,
            capabilities=PERF_CAPS,
            executor_kwargs=kwargs,
        )
    assert len(calls) == 1 and len(synthetic.calls) == 1
    assert ExperimentStore.open(root).record.budget.additional_seconds == []
