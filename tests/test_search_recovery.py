"""Adversarial stage-05 recovery uses real journals and harmless fake timing."""

import fcntl
import json
import subprocess
import sys
from dataclasses import replace

import pytest
from process_helpers import ROOT, SIGNALS, cleanup, living, signal_parent, wait_for
from test_search import (
    CAPABILITIES,
    FakeClock,
    SyntheticExecution,
    make_workload,
    trials,
)
from test_search import (
    snapshot as snapshot,
)

from simtune.experiments import ExperimentStore, TrialExecutor
from simtune.models import Outcome, Phase
from simtune.search import STATE_KEY, SearchConfig, resume_search, run_search
from simtune.validation import ValidationError
from simtune.workloads import load_workload


def crash_after_terminal(
    tmp_path, snapshot, monkeypatch, *, count=2, failed=(), budget=100, failed_policy=None
):
    workload = make_workload(tmp_path / "manifest")
    clock = FakeClock()
    synthetic = SyntheticExecution(clock, failed=failed)
    root = tmp_path / "search"
    original = TrialExecutor._sync_experiment
    terminal = []

    def crash(self, record):
        terminal.append(record)
        if (failed_policy is not None and record.policy_id == failed_policy) or (
            failed_policy is None and len(terminal) == count
        ):
            raise RuntimeError("crash after terminal append before experiment synchronization")
        return original(self, record)

    with monkeypatch.context() as patch:
        patch.setattr(TrialExecutor, "_sync_experiment", crash)
        with pytest.raises(RuntimeError, match="crash after terminal append"):
            run_search(
                workload,
                root,
                SearchConfig(budget, repeat=1, warmups=0, bootstrap_samples=100),
                snapshot_provider=lambda: snapshot,
                capabilities=CAPABILITIES,
                clock=clock,
                executor_kwargs=synthetic.kwargs,
            )
    assert terminal
    store = ExperimentStore.open(root)
    assert not store.is_complete()
    assert terminal[-1].id not in (
        store.record.training_trial_ids + store.record.confirmation_trial_ids
    )
    assert store.recover().states[terminal[-1].id].value == terminal[-1].status.value
    return root, clock, synthetic, terminal[-1]


def resume(root, clock, synthetic, snapshot, **kwargs):
    return resume_search(
        root,
        clock=clock,
        snapshot_provider=lambda: snapshot,
        capabilities=CAPABILITIES,
        executor_kwargs=synthetic.kwargs,
        **kwargs,
    )


@pytest.mark.parametrize("count", [1, 2, 5])
def test_terminal_append_before_experiment_sync_resumes_without_duplicate_work(
    tmp_path, snapshot, monkeypatch, count
):
    root, clock, synthetic, lost_reference = crash_after_terminal(
        tmp_path, snapshot, monkeypatch, count=count
    )
    previous = {
        trial.id: (root / "trials" / trial.id / "trial.json").read_bytes() for trial in trials(root)
    }
    assert len(previous) == count
    result = resume(root, clock, synthetic, snapshot)
    assert result.outcome == Outcome.VALIDATED_IMPROVEMENT
    assert len(synthetic.calls) == result.budget.launches_started == 14
    assert len({call["cwd"] for call in synthetic.calls}) == 14
    assert lost_reference.id in result.training_trial_ids + result.confirmation_trial_ids
    evidence = trials(root)
    assert set(result.training_trial_ids) == {
        trial.id for trial in evidence if trial.phase == Phase.TRAINING
    }
    assert set(result.confirmation_trial_ids) == {
        trial.id for trial in evidence if trial.phase == Phase.CONFIRMATION
    }
    for trial_id, contents in previous.items():
        assert (root / "trials" / trial_id / "trial.json").read_bytes() == contents
    assert ExperimentStore.open(root).is_complete()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("case_id", "heldout"),
        ("policy_id", "different-policy"),
        ("phase", Phase.CONFIRMATION),
        ("order", 99),
        ("pair_id", "different-pair"),
    ],
)
def test_recovered_trial_must_match_every_saved_schedule_field(
    tmp_path, snapshot, monkeypatch, field, value
):
    root, clock, synthetic, last = crash_after_terminal(tmp_path, snapshot, monkeypatch)
    store = ExperimentStore.open(root)
    store.write_trial(replace(last, **{field: value}))
    count = len(synthetic.calls)
    with pytest.raises(ValidationError, match="saved schedule"):
        resume(root, clock, synthetic, snapshot)
    assert len(synthetic.calls) == count
    assert not store.is_complete()


@pytest.mark.parametrize("mode", ["missing_raw", "missing_check", "symlink_raw"])
def test_missing_or_symlink_reused_evidence_refuses_resume(tmp_path, snapshot, monkeypatch, mode):
    root, clock, synthetic, last = crash_after_terminal(tmp_path, snapshot, monkeypatch)
    evidence = trials(root)
    reference = (
        last.validation.evidence[0] if mode == "missing_check" else last.metrics[0].raw_source
    )
    path = root / reference
    path.unlink()
    if mode == "symlink_raw":
        path.symlink_to(root / evidence[0].metrics[0].raw_source)
    count = len(synthetic.calls)
    with pytest.raises(ValidationError, match="missing or reused evidence"):
        resume(root, clock, synthetic, snapshot)
    assert len(synthetic.calls) == count


def test_saved_cursor_cannot_skip_unfinished_evidence(tmp_path, snapshot, monkeypatch):
    root, clock, synthetic, _ = crash_after_terminal(tmp_path, snapshot, monkeypatch)
    store = ExperimentStore.open(root)
    state = {**store.record.extensions[STATE_KEY], "next_index": 3}
    store.update_record(
        replace(store.record, extensions={**store.record.extensions, STATE_KEY: state})
    )
    with pytest.raises(ValidationError, match="cursor omits unfinished trial"):
        resume(root, clock, synthetic, snapshot)
    assert len(synthetic.calls) == 2


def test_concurrent_lease_refuses_and_stale_unlocked_file_allows_resume(
    tmp_path, snapshot, monkeypatch
):
    root, clock, synthetic, _ = crash_after_terminal(tmp_path, snapshot, monkeypatch)
    lock = root / "search.lock"
    lock.write_text("stale descriptive text is not a live kernel lease")
    with lock.open("a+") as owner:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValidationError, match="another search owns"):
            resume(root, clock, synthetic, snapshot)
        assert len(synthetic.calls) == 2
        fcntl.flock(owner.fileno(), fcntl.LOCK_UN)
    result = resume(root, clock, synthetic, snapshot)
    assert result.outcome == Outcome.VALIDATED_IMPROVEMENT
    assert len({call["cwd"] for call in synthetic.calls}) == len(synthetic.calls) == 14


def test_resume_record_open_cost_spends_existing_authorization_before_next_launch(
    tmp_path, snapshot, monkeypatch
):
    root, clock, synthetic, _ = crash_after_terminal(tmp_path, snapshot, monkeypatch, budget=1)
    before = ExperimentStore.open(root).record.budget
    original = ExperimentStore.open.__func__

    def slow_open(cls, path):
        clock.advance(0.6)
        return original(cls, path)

    clock.advance(1000)  # Inactive time between sessions remains uncharged.
    with monkeypatch.context() as patch:
        patch.setattr(ExperimentStore, "open", classmethod(slow_open))
        result = resume(root, clock, synthetic, snapshot)
    assert result.outcome == Outcome.INCONCLUSIVE
    assert result.extensions[STATE_KEY]["resumable"] is True
    assert result.budget.additional_seconds == []
    assert result.budget.spent_seconds == pytest.approx(before.spent_seconds + 0.6)
    assert result.budget.launches_started == len(synthetic.calls) == 2
    continued = resume(root, clock, synthetic, snapshot, additional_seconds=2)
    assert continued.outcome == Outcome.VALIDATED_IMPROVEMENT
    assert continued.budget.additional_seconds == [2]
    assert len({call["cwd"] for call in synthetic.calls}) == len(synthetic.calls) == 14


def test_recovered_failed_baseline_stops_before_any_new_trial(tmp_path, snapshot, monkeypatch):
    root, clock, synthetic, _ = crash_after_terminal(
        tmp_path, snapshot, monkeypatch, failed=["baseline"], failed_policy="baseline"
    )
    count = len(synthetic.calls)
    result = resume(root, clock, synthetic, snapshot)
    assert result.outcome == Outcome.FAILED
    assert result.selected_policy_id == "baseline"
    assert result.confirmation_trial_ids == []
    assert len(synthetic.calls) == count


def test_recovered_failed_candidate_is_excluded_before_any_new_trial_for_it(
    tmp_path, snapshot, monkeypatch
):
    root, clock, synthetic, _ = crash_after_terminal(
        tmp_path, snapshot, monkeypatch, failed=["runtime-close"], failed_policy="runtime-close"
    )
    count = sum(call["policy"] == "runtime-close" for call in synthetic.calls)
    result = resume(root, clock, synthetic, snapshot)
    assert result.outcome != Outcome.VALIDATED_IMPROVEMENT
    assert result.selected_policy_id == "baseline"
    assert sum(call["policy"] == "runtime-close" for call in synthetic.calls) == count
    candidate = next(item for item in result.candidates if item.policy_id == "runtime-close")
    assert candidate.exclusion_reason


def test_root_search_rejects_identical_science_disguised_as_a_heldout_case(tmp_path, snapshot):
    loaded = make_workload(tmp_path / "manifest")
    spec = replace(
        loaded.spec,
        cases=[
            loaded.spec.cases[0],
            replace(loaded.spec.cases[1], arguments=loaded.spec.cases[0].arguments),
        ],
    )
    loaded.manifest_path.write_text(spec.to_json())
    workload = load_workload(loaded.manifest_path)
    clock = FakeClock()
    synthetic = SyntheticExecution(clock)
    result = run_search(
        workload,
        tmp_path / "search",
        SearchConfig(100, repeat=1, warmups=0, bootstrap_samples=100),
        snapshot_provider=lambda: snapshot,
        capabilities=CAPABILITIES,
        clock=clock,
        executor_kwargs=synthetic.kwargs,
    )
    assert result.outcome == Outcome.INCONCLUSIVE
    assert result.selected_policy_id == "baseline"
    assert "reuse training scientific inputs" in result.explanation
    assert result.extensions[STATE_KEY]["break_even_runs"] is None


@pytest.mark.parametrize("sig", SIGNALS)
@pytest.mark.parametrize("phase", ["workload", "setup", "validator", "cleanup"])
def test_real_parent_cancellation_is_durable_and_never_resumed(tmp_path, sig, phase):
    driver = f"""import json,sys
from pathlib import Path
from process_helpers import cancellation_workload
from test_search import snapshot as snapshot_fixture
snapshot=lambda: snapshot_fixture.__wrapped__()
from simtune import util
from simtune.search import SearchConfig,run_search,resume_search
from simtune.experiments import ExperimentStore
from simtune.validation import ValidationError
root=Path({str(tmp_path)!r}); workload,options=cancellation_workload(root,{phase!r})
with util.cancellation_scope() as state:
    record=run_search(workload,root/'search',SearchConfig(60,repeat=1,warmups=0,bootstrap_samples=100),
        snapshot_provider=snapshot,capabilities={{'platform':'linux','tools':{{}}}},options=options)
    signal=state.signum
assert record.outcome.value=='interrupted'
assert not ExperimentStore.open(root/'search').is_complete()
before=(root/'launches').read_bytes()
try:
    resume_search(root/'search',additional_seconds=10,snapshot_provider=snapshot,
        capabilities={{'platform':'linux','tools':{{}}}})
except ValidationError as exc:
    assert 'interrupted' in str(exc)
else: raise AssertionError('resume accepted cancelled work')
assert (root/'launches').read_bytes()==before
sys.exit(128+signal)
"""
    status, out, err, ready = signal_parent(tmp_path, driver, sig)
    assert status == 128 + sig, (out, err)
    assert not living(ready["pid"])
    store = ExperimentStore.open(tmp_path / "search")
    assert store.record.outcome == Outcome.INTERRUPTED
    assert store.record.extensions[STATE_KEY]["parent_signal"] == sig
    assert list(store.recover().states.values())[0].value == "interrupted"
    paths = list((tmp_path / "search/trials").glob("*/trial.json"))
    assert len(paths) == 1
    data = json.loads(paths[0].read_text())
    assert data["status"] == "interrupted" and data["return_code"] == -sig
    assert data["validation"]["state"] == ("passed" if phase == "cleanup" else "unknown")
    assert data["extensions"]["simtune.cancellation"]["parent_signal"] == sig
    assert not (tmp_path / "completion").exists()
    assert (tmp_path / "launches").read_text() == "launch\n"
    if phase == "setup":
        assert not (tmp_path / "workload-launch").exists()
    trial_root = paths[0].parent
    evidence = (
        trial_root / "checks/validator.json"
        if phase == "validator"
        else trial_root / f"hooks/{phase}.json"
        if phase in {"setup", "cleanup"}
        else trial_root / "launch.txt"
    )
    assert "partial-output" in evidence.read_text()
    assert "partial-error" in evidence.read_text()
    assert str(evidence.relative_to(store.root)) in data["raw_artifacts"]


def test_sigkill_parent_recovers_uncertain_and_launches_nothing(tmp_path):
    driver = f"""import sys
from pathlib import Path
from process_helpers import cancellation_workload
from test_search import snapshot as snapshot_fixture
snapshot=lambda: snapshot_fixture.__wrapped__()
from simtune.search import SearchConfig,run_search
workload,options=cancellation_workload(Path({str(tmp_path)!r}),'workload')
run_search(workload,Path({str(tmp_path)!r})/'search',SearchConfig(60,repeat=1,warmups=0),
    snapshot_provider=snapshot,capabilities={{'platform':'linux','tools':{{}}}},options=options)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", f"import sys;sys.path.insert(0,{str(ROOT / 'tests')!r})\n" + driver],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        wait_for(lambda: (tmp_path / "ready.json").exists(), proc)
        proc.kill()
        proc.communicate(timeout=5)
        store = ExperimentStore.open(tmp_path / "search")
        assert list(store.recover().states.values())[0].value == "uncertain"
        before = (tmp_path / "launches").read_bytes()
        with pytest.raises(ValidationError, match="uncertain.*automatic resume refused"):
            resume_search(tmp_path / "search", additional_seconds=10)
        assert (tmp_path / "launches").read_bytes() == before
    finally:
        # SIGKILL cannot forward; this test explicitly kills its surviving owned child.
        cleanup(proc, tmp_path)


def test_uncertain_timeout_keeps_124_and_refuses_resume(tmp_path, snapshot):
    from simtune.launch import StrictLaunchResult
    from simtune.tune import _status

    calls = []

    def uncertain(plan, argv, capabilities, timeout, **kwargs):
        calls.append(argv)
        return StrictLaunchResult(
            final_argv=tuple(argv),
            policy_id=plan.policy.id,
            snapshot_id=plan.snapshot_id,
            actions=plan.actions,
            unobserved=(),
            raw_output="partial timeout output",
            metrics={
                "return_code": 124,
                "timed_out": True,
                "wall_time_sec": 0.1,
                "cancellation": {
                    "parent_signal": None,
                    "workload_return_code": None,
                    "launch_attempted": True,
                    "termination_known": False,
                    "output_complete": False,
                },
            },
        )

    root = tmp_path / "search"
    result = run_search(
        make_workload(tmp_path / "manifest"),
        root,
        SearchConfig(100, repeat=1, warmups=0, bootstrap_samples=100),
        snapshot_provider=lambda: snapshot,
        capabilities=CAPABILITIES,
        executor_kwargs={"strict_executor": uncertain},
    )
    assert result.outcome == Outcome.INTERRUPTED
    assert _status(result) == 124
    assert not ExperimentStore.open(root).is_complete()
    assert list(ExperimentStore.open(root).recover().states.values())[0].value == "uncertain"
    with pytest.raises(ValidationError, match="uncertain trial.*automatic resume refused"):
        resume_search(root, additional_seconds=10)
    assert len(calls) == 1
