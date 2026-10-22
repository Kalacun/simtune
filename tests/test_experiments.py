"""Stage-04 isolated trials, budgets, journals, and completion records."""

import fcntl
import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from simtune.experiments import (
    BudgetController,
    BudgetExhausted,
    ExperimentStore,
    HookSpec,
    JournalEvent,
    TrialExecutionOptions,
    TrialExecutor,
    TrialRequest,
    TrialState,
    WorkspaceMode,
    new_experiment_record,
)
from simtune.models import (
    NumaPolicy,
    Phase,
    Placement,
    Platform,
    PolicySpec,
    ResourceSnapshot,
    TrialStatus,
    ValidationState,
)
from simtune.validation import ValidationError
from simtune.workloads import load_workload, sha256_file

FIXTURE = Path(__file__).parent / "fixtures" / "experiments" / "workload.toml"
ROOT = Path(__file__).resolve().parents[1]
CAPABILITIES = {"platform": "linux", "tools": {}}


@pytest.fixture
def stage04_heat_binary(tmp_path):
    compiler = os.environ.get("SIMTUNE_CC") or shutil.which("cc") or shutil.which("gcc")
    if not compiler:
        pytest.skip("C compiler unavailable for the optional heat correctness smoke")
    binary = tmp_path / "heat2d"
    result = subprocess.run(
        [
            compiler,
            "-O1",
            "-std=c11",
            str(ROOT / "examples/heat2d.c"),
            "-lm",
            "-o",
            str(binary),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        pytest.skip(f"C compiler could not build heat2d: {result.stderr}")
    return binary


def snapshot():
    return ResourceSnapshot(
        id="synthetic-linux",
        platform=Platform.LINUX,
        is_wsl=False,
        online_cpu_ids=[0],
        cpus=None,
        numa_nodes=None,
        inherited_cpu_ids=[0],
        permitted_cpu_ids=[0],
        effective_cpu_ids=[0],
        allowed_memory_node_ids=None,
        memory_limits=[],
        sources={
            name: "synthetic test"
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
            "cpus": "inherited placement does not need topology in this test",
            "numa_nodes": "inherited NUMA policy does not need topology in this test",
            "allowed_memory_node_ids": "inherited NUMA policy does not need a node mask",
        },
    )


def policy(workload, policy_id="inherited"):
    return PolicySpec(
        id=policy_id,
        placement=Placement.INHERITED,
        numa_policy=NumaPolicy.INHERITED,
        numa_node_count=None,
        runtime=workload.spec.runtime,
        settings=[],
        budget=workload.spec.cases[0].resource_needs,
        rationale="Keep the fixture's declared resources unchanged.",
        unavailable={"numa_node_count": "inherited NUMA policy"},
    )


def make_executor(tmp_path, *, workload=None, strict_executor=None, command_runner=None):
    workload = workload or load_workload(FIXTURE)
    selected = policy(workload)
    budget = BudgetController(
        authorized_seconds=20,
        confirmation_reserved_seconds=0,
        cleanup_allowance_seconds=1,
        max_trials=10,
        max_launches=20,
    )
    record = new_experiment_record(
        experiment_id="experiment-stage04",
        workload=workload,
        snapshot=snapshot(),
        capabilities=CAPABILITIES,
        policies=[selected],
        budget=budget,
        seed=17,
    )
    store = ExperimentStore.create(tmp_path / "experiment", record)
    kwargs = {}
    if strict_executor is not None:
        kwargs["strict_executor"] = strict_executor
    if command_runner is not None:
        kwargs["command_runner"] = command_runner
    executor = TrialExecutor(
        store=store,
        workload=workload,
        capabilities=CAPABILITIES,
        budget=budget,
        snapshot_provider=snapshot,
        **kwargs,
    )
    return workload, selected, budget, store, executor


def request(selected, trial_id="trial-001", **overrides):
    values = dict(
        id=trial_id,
        case_id="tiny",
        policy=selected,
        phase=Phase.TRAINING,
        order=0,
        pair_id="pair-001",
    )
    values.update(overrides)
    return TrialRequest(**values)


def test_isolated_checked_trial_is_durable_and_explanatory(tmp_path):
    _, selected, budget, store, executor = make_executor(tmp_path)
    trial = executor.run(request(selected))
    assert trial.status == TrialStatus.SUCCEEDED
    assert trial.validation.state == ValidationState.PASSED
    assert trial.return_code == 0 and trial.timed_out is False
    assert Path(trial.cwd).parent.name == "trial-001"
    assert trial.argv[2] == trial.cwd
    assert trial.env_overrides == {"OMP_NUM_THREADS": "1", "SCIENCE_SEED": "7"}
    assert all(action.status.value == "applied" for action in trial.actions)
    assert all(action.observed_value is None for action in trial.actions)
    assert {Path(path).name for path in trial.raw_artifacts} >= {
        "plan.json",
        "launch.json",
        "launch.txt",
        "science-value.json",
    }
    stored = json.loads((store.root / "trials/trial-001/trial.json").read_text())
    assert stored["argv"] == trial.argv and stored["validation"]["state"] == "passed"
    assert store.recover().states == {"trial-001": TrialState.SUCCEEDED}
    assert store.record.training_trial_ids == ["trial-001"]
    assert budget.ledger().trials_started == 1 and budget.ledger().launches_started == 1


def test_no_checks_stays_unchecked_not_validated(tmp_path):
    loaded = load_workload(FIXTURE)
    unchecked_spec = replace(loaded.spec, output_checks=[])
    unchecked = replace(loaded, spec=unchecked_spec, workload_fingerprint="1" * 64)
    _, selected, _, _, executor = make_executor(tmp_path, workload=unchecked)
    trial = executor.run(request(selected))
    assert trial.status == TrialStatus.SUCCEEDED
    assert trial.validation.state == ValidationState.UNCHECKED
    assert trial.validation.checks == [] and trial.validation.evidence == []


def test_wrong_expected_science_is_retained_and_ineligible(tmp_path):
    fixture = tmp_path / "manifest"
    shutil.copytree(FIXTURE.parent, fixture)
    (fixture / "workload.py").chmod(0o755)
    manifest = fixture / "workload.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("expected = 6.0", "expected = 99.0"),
        encoding="utf-8",
    )
    loaded = load_workload(manifest)
    _, selected, _, store, executor = make_executor(tmp_path / "run", workload=loaded)
    trial = executor.run(request(selected))
    assert trial.status == TrialStatus.SUCCEEDED
    assert trial.validation.state == ValidationState.FAILED
    evidence = json.loads((store.root / trial.validation.evidence[0]).read_text())
    assert evidence["observed"] == 6.0 and evidence["expected"] == 99.0
    assert (Path(trial.cwd) / "result.json").is_file()


def test_setup_failure_launches_no_workload_and_preserves_workspace(tmp_path):
    workload_calls = []

    def no_workload(*args, **kwargs):
        workload_calls.append((args, kwargs))
        raise AssertionError("workload must not launch after setup failure")

    def failed_hook(argv, env, timeout, cwd):
        return {"return_code": 9, "timed_out": False, "wall_time_sec": 0.01}, "setup failed"

    _, selected, _, store, executor = make_executor(
        tmp_path, strict_executor=no_workload, command_runner=failed_hook
    )
    options = TrialExecutionOptions(setup=HookSpec(("setup", "{trial_dir}"), 1.0))
    trial = executor.run(request(selected, options=options))
    assert trial.status == TrialStatus.FAILED and trial.return_code == 9
    assert trial.validation.state == ValidationState.UNKNOWN
    assert workload_calls == []
    assert (store.root / "trials/trial-001/work").is_dir()
    assert store.recover().states[trial.id] == TrialState.FAILED


def test_disk_failure_before_intent_record_launches_nothing(tmp_path, monkeypatch):
    calls = []

    def workload(*args, **kwargs):
        calls.append(1)

    _, selected, _, store, executor = make_executor(tmp_path, strict_executor=workload)

    def fail_write(record):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(store, "write_trial", fail_write)
    with pytest.raises(OSError, match="disk failure"):
        executor.run(request(selected))
    assert calls == []
    assert (store.root / "trials/trial-001/work").is_dir()
    assert store.recover().states == {}


def test_declared_input_change_after_loading_prevents_trial(tmp_path):
    fixture = tmp_path / "manifest"
    shutil.copytree(FIXTURE.parent, fixture)
    (fixture / "workload.py").chmod(0o755)
    loaded = load_workload(fixture / "workload.toml")
    _, selected, _, store, executor = make_executor(tmp_path / "run", workload=loaded)
    (fixture / "input.txt").write_text("changed after load\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="SHA-256 changed"):
        executor.run(request(selected))
    assert not (store.root / "trials").exists()


def test_running_and_partial_journal_recover_as_uncertain(tmp_path):
    _, selected, _, store, _ = make_executor(tmp_path)
    store.allocate_trial("trial-crashed")
    store.append(
        JournalEvent(
            trial_id="trial-crashed",
            state=TrialState.NOT_STARTED,
            timestamp="2026-09-18T00:00:00Z",
            reason="intent stored",
            record="trials/trial-crashed/trial.json",
        )
    )
    store.append(
        JournalEvent(
            trial_id="trial-crashed",
            state=TrialState.RUNNING,
            timestamp="2026-09-18T00:00:01Z",
            reason="launch boundary crossed",
            record="trials/trial-crashed/trial.json",
        )
    )
    with (store.root / "trials.jsonl").open("ab") as stream:
        stream.write(b'{"partial":')
        stream.flush()
        os.fsync(stream.fileno())
    recovery = store.recover()
    assert recovery.states == {"trial-crashed": TrialState.UNCERTAIN}
    assert recovery.ignored_partial_line is True
    with pytest.raises(ValidationError, match="uncertain journal"):
        store.complete("must not fabricate completion")
    assert selected.id == "inherited"


def test_complete_corrupt_journal_line_is_not_ignored(tmp_path):
    _, _, _, store, _ = make_executor(tmp_path)
    with (store.root / "trials.jsonl").open("ab") as stream:
        stream.write(b'{"schema_version":99}\n')
        stream.flush()
        os.fsync(stream.fileno())
    with pytest.raises(ValidationError, match="journal line 1"):
        store.recover()


def test_interrupted_atomic_replace_preserves_previous_record(tmp_path, monkeypatch):
    _, _, _, store, _ = make_executor(tmp_path)
    before = (store.root / "experiment.json").read_bytes()

    def interrupted_replace(source, destination):
        raise OSError("synthetic interrupted rename")

    monkeypatch.setattr("simtune.experiments.os.replace", interrupted_replace)
    changed = replace(store.record, explanation="replacement must not become visible")
    with pytest.raises(OSError, match="interrupted rename"):
        store.update_record(changed)
    assert (store.root / "experiment.json").read_bytes() == before
    assert list(store.root.glob(".experiment.json.*.tmp")) == []


def test_uncertain_exception_is_recorded_and_never_retried(tmp_path):
    calls = []

    def uncertain(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("lost launcher response")

    _, selected, _, store, executor = make_executor(tmp_path, strict_executor=uncertain)
    with pytest.raises(RuntimeError, match="lost launcher"):
        executor.run(request(selected))
    assert calls == [1]
    assert store.recover().states["trial-001"] == TrialState.UNCERTAIN
    stored = json.loads((store.root / "trials/trial-001/trial.json").read_text())
    assert stored["status"] == "unknown" and "Uncertain" in stored["status_reason"]
    with pytest.raises(FileExistsError):
        executor.run(request(selected))
    assert calls == [1]


def test_completion_marker_follows_records_and_does_not_select(tmp_path):
    _, selected, _, store, executor = make_executor(tmp_path)
    executor.run(request(selected))
    completed = store.complete(
        "All scheduled stage-04 trials have terminal records; no winner selected."
    )
    assert completed.outcome.value == "inconclusive"
    assert completed.selected_policy_id is None
    marker = json.loads((store.root / "completion.json").read_text())
    assert marker["experiment_id"] == completed.id
    assert (
        marker["experiment_record_sha256"]
        == hashlib.sha256((store.root / "experiment.json").read_bytes()).hexdigest()
    )
    with pytest.raises(ValidationError, match="already"):
        store.complete("again")
    with pytest.raises(ValidationError, match="immutable"):
        store.update_record(completed)


def test_corrupt_completion_marker_is_not_accepted(tmp_path):
    _, selected, _, store, executor = make_executor(tmp_path)
    executor.run(request(selected))
    store.complete("terminal records verified")
    marker_path = store.root / "completion.json"
    marker = json.loads(marker_path.read_text())
    marker["experiment_record_sha256"] = "0" * 64
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(ValidationError, match="does not match"):
        store.is_complete()


def test_budget_caps_time_counts_and_reserves_confirmation():
    now = [0.0]
    budget = BudgetController(
        authorized_seconds=10,
        confirmation_reserved_seconds=3,
        cleanup_allowance_seconds=2,
        max_trials=1,
        max_launches=2,
        clock=lambda: now[0],
    )
    budget.begin_trial(Phase.TRAINING)
    assert budget.begin_launch(Phase.TRAINING, 99) == 7
    started = now[0]
    now[0] = 6
    budget.charge("workload", started)
    assert budget.available_seconds(Phase.TRAINING) == 1
    assert budget.available_seconds(Phase.CONFIRMATION) == 4
    assert budget.begin_launch(Phase.TRAINING, 5) == 1
    with pytest.raises(BudgetExhausted, match="trial count"):
        budget.begin_trial(Phase.TRAINING)
    with pytest.raises(BudgetExhausted, match="launch count"):
        budget.begin_launch(Phase.CONFIRMATION, 1)
    assert budget.ledger().launches_started == 2


def test_fixed_cwd_concurrent_lease_rejects_before_workload(tmp_path):
    calls = []

    def workload(*args, **kwargs):
        calls.append(1)

    _, selected, _, store, executor = make_executor(tmp_path, strict_executor=workload)
    fixed = tmp_path / "legacy-cwd"
    fixed.mkdir()
    digest = hashlib.sha256(str(fixed.resolve()).encode("utf-8")).hexdigest()
    lock_path = Path(tempfile.gettempdir()) / f"simtune-fixed-cwd-{digest}.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    options = TrialExecutionOptions(workspace_mode=WorkspaceMode.FIXED_CWD, fixed_cwd=fixed)
    try:
        with pytest.raises(ValidationError, match="already in use"):
            executor.run(request(selected, options=options))
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    assert calls == []
    assert store.recover().states["trial-001"] == TrialState.UNCERTAIN


def test_case_resource_change_is_rejected_before_trial_creation(tmp_path):
    workload, selected, _, store, executor = make_executor(tmp_path)
    changed_budget = replace(selected.budget, cpu_count=2, reference_cpu_ids=[0, 1])
    changed = replace(selected, budget=changed_budget)
    with pytest.raises(ValidationError, match="resource inputs"):
        executor.run(request(changed))
    assert not (store.root / "trials").exists()
    assert workload.spec.cases[0].arguments == {"input": "input.txt"}


def test_two_trials_cannot_collide_and_cleanup_is_skipped_on_bad_science(tmp_path):
    cleanup_calls = []

    def cleanup(argv, env, timeout, cwd):
        cleanup_calls.append(cwd)
        return {"return_code": 0, "timed_out": False}, ""

    _, selected, _, store, executor = make_executor(tmp_path, command_runner=cleanup)
    first = executor.run(request(selected, trial_id="trial-a"))
    second = executor.run(request(selected, trial_id="trial-b", order=1, pair_id="pair-002"))
    assert first.cwd != second.cwd
    assert Path(first.cwd, "result.json").is_file()
    assert Path(second.cwd, "result.json").is_file()

    loaded = load_workload(FIXTURE)
    bad_check = replace(loaded.spec.output_checks[0], expected=99.0)
    bad = replace(
        loaded,
        spec=replace(loaded.spec, output_checks=[bad_check]),
        workload_fingerprint="2" * 64,
    )
    _, selected_bad, _, _, executor_bad = make_executor(
        tmp_path / "bad", workload=bad, command_runner=cleanup
    )
    options = TrialExecutionOptions(cleanup=HookSpec(("cleanup", "{trial_dir}"), 1.0))
    failed_validation = executor_bad.run(request(selected_bad, options=options))
    assert failed_validation.validation.state == ValidationState.FAILED
    assert cleanup_calls == []


def test_store_rechecks_symlinks_before_artifact_write(tmp_path):
    _, _, _, store, _ = make_executor(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    trials = store.root / "trials"
    trials.mkdir()
    (trials / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValidationError, match="escapes"):
        store.write_artifact_text("trials/escape/deleted.txt", "must stay inside")
    assert list(outside.iterdir()) == []


def test_keyboard_interrupt_is_terminal_and_not_success(tmp_path):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    _, selected, _, store, executor = make_executor(tmp_path, strict_executor=interrupt)
    trial = executor.run(request(selected))
    assert trial.status == TrialStatus.INTERRUPTED
    assert trial.validation.state == ValidationState.UNKNOWN
    assert store.recover().states[trial.id] == TrialState.INTERRUPTED


def _heat_checksum(size, steps, wave):
    values = [
        [
            math.sin(math.pi * wave * i / (size - 1)) * math.sin(math.pi * wave * j / (size - 1))
            if 0 < i < size - 1 and 0 < j < size - 1
            else 0.0
            for j in range(size)
        ]
        for i in range(size)
    ]
    for _ in range(steps):
        updated = [[0.0] * size for _ in range(size)]
        for i in range(1, size - 1):
            for j in range(1, size - 1):
                updated[i][j] = values[i][j] + 0.2 * (
                    values[i - 1][j]
                    + values[i + 1][j]
                    + values[i][j - 1]
                    + values[i][j + 1]
                    - 4 * values[i][j]
                )
        values = updated
    return sum(map(sum, values))


def _heat_manifest(tmp_path, heat_binary, expected):
    tmp_path.mkdir(parents=True, exist_ok=True)
    wrapper = tmp_path / "heat_wrapper.py"
    wrapper.write_text(
        """#!/usr/bin/env python3
import json
import subprocess
import sys
from pathlib import Path

result = subprocess.run(
    [sys.argv[1], "--size", "7", "--steps", "3", "--wave", "1"],
    check=False,
    capture_output=True,
    text=True,
)
if result.returncode:
    print(result.stderr, file=sys.stderr)
    raise SystemExit(result.returncode)
record = json.loads(result.stdout)
Path(sys.argv[2], "result.json").write_text(json.dumps(record) + "\\n", encoding="utf-8")
""",
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    manifest = tmp_path / "heat.toml"
    manifest.write_text(
        f'''kind = "workload_spec"
schema_version = 1
id = "heat-tiny"
family = "heat2d"
argv_template = ["heat_wrapper.py", "{{case.binary}}", "{{trial_dir}}"]
resolved_executable = "heat_wrapper.py"
executable_sha256 = "{sha256_file(wrapper)}"
base_directory = "."
case_substitutions = ["binary"]
training_case_ids = ["heat-small"]
validation_case_ids = []
fixed_env = {{}}

[runtime]
name = "synthetic-openmp"
[runtime.unavailable]
version = "Serial correctness wrapper has no runtime version."
identity = "Wrapper and heat binary are hashed artifacts."

[limits]
trial_timeout_seconds = 5.0
max_trials = 4
max_launches = 4

[[artifacts]]
id = "wrapper"
role = "launcher"
path = "heat_wrapper.py"
sha256 = "{sha256_file(wrapper)}"

[[output_checks]]
id = "checksum"
method = "numeric_json"
output = "result.json"
json_field = ["checksum"]
expected = {expected!r}
absolute_tolerance = 1e-12
relative_tolerance = 0.0
rationale = "Compare the tiny fixed heat stencil with an independent calculation."
[output_checks.unavailable]
sha256 = "Numeric check."
argv = "Numeric check."
timeout_seconds = "Numeric check."

[[cases]]
kind = "case_spec"
schema_version = 1
id = "heat-small"
input_class = "tiny-correctness"
weight = 1.0
checks = []
regression_tolerance = 0.0
[cases.arguments]
binary = "{heat_binary}"
[[cases.input_artifacts]]
id = "heat-binary"
role = "executable-input"
path = "{heat_binary}"
sha256 = "{sha256_file(heat_binary)}"
[cases.resource_needs]
basis = "inherited"
cpu_count = 1
thread_count = 1
reference_cpu_ids = [0]
rationale = "Correctness only at one fixed CPU and thread."
[cases.resource_needs.unavailable]
memory_bytes = "No capacity claim."
''',
        encoding="utf-8",
    )
    return manifest


def test_tiny_heat_correctness_passes_and_wrong_reference_fails(stage04_heat_binary, tmp_path):
    heat_binary = stage04_heat_binary
    expected = _heat_checksum(7, 3, 1)
    manifest = _heat_manifest(tmp_path, heat_binary, expected)
    loaded = load_workload(manifest)
    selected = policy(loaded)
    budget = BudgetController(
        authorized_seconds=20,
        confirmation_reserved_seconds=0,
        cleanup_allowance_seconds=0,
        max_trials=2,
        max_launches=2,
    )
    record = new_experiment_record(
        experiment_id="heat-correctness",
        workload=loaded,
        snapshot=snapshot(),
        capabilities=CAPABILITIES,
        policies=[selected],
        budget=budget,
        seed=1,
    )
    store = ExperimentStore.create(tmp_path / "correct", record)
    executor = TrialExecutor(
        store=store,
        workload=loaded,
        capabilities=CAPABILITIES,
        budget=budget,
        snapshot_provider=snapshot,
    )
    good = executor.run(
        TrialRequest(
            id="heat-good",
            case_id="heat-small",
            policy=selected,
            phase=Phase.TRAINING,
            order=0,
            pair_id="heat-pair",
        )
    )
    assert good.validation.state == ValidationState.PASSED

    wrong_manifest = _heat_manifest(tmp_path / "wrong-manifest", heat_binary, expected + 1.0)
    wrong_loaded = load_workload(wrong_manifest)
    wrong_policy = policy(wrong_loaded)
    wrong_budget = BudgetController(
        authorized_seconds=20,
        confirmation_reserved_seconds=0,
        cleanup_allowance_seconds=0,
        max_trials=1,
        max_launches=1,
    )
    wrong_record = new_experiment_record(
        experiment_id="heat-wrong",
        workload=wrong_loaded,
        snapshot=snapshot(),
        capabilities=CAPABILITIES,
        policies=[wrong_policy],
        budget=wrong_budget,
        seed=1,
    )
    wrong_store = ExperimentStore.create(tmp_path / "wrong", wrong_record)
    wrong_executor = TrialExecutor(
        store=wrong_store,
        workload=wrong_loaded,
        capabilities=CAPABILITIES,
        budget=wrong_budget,
        snapshot_provider=snapshot,
    )
    bad = wrong_executor.run(
        TrialRequest(
            id="heat-bad",
            case_id="heat-small",
            policy=wrong_policy,
            phase=Phase.TRAINING,
            order=0,
            pair_id="heat-pair",
        )
    )
    assert bad.status == TrialStatus.SUCCEEDED
    assert bad.validation.state == ValidationState.FAILED
    assert wrong_store.record.selected_policy_id is None


@pytest.mark.parametrize("timed_out", [False, True])
def test_unestablished_teardown_is_durable_uncertain_and_order_stops(tmp_path, timed_out):
    from simtune.launch import StrictLaunchResult

    def uncertain(plan, argv, capabilities, timeout, **kwargs):
        return StrictLaunchResult(
            policy_id=plan.policy.id,
            snapshot_id=plan.snapshot_id,
            final_argv=tuple(argv),
            actions=plan.actions,
            unobserved=(),
            metrics={
                "return_code": 124 if timed_out else -15,
                "timed_out": timed_out,
                "wall_time_sec": 0.1,
                "cancellation": {
                    "parent_signal": None if timed_out else 15,
                    "workload_return_code": None,
                    "launch_attempted": True,
                    "termination_known": False,
                    "output_complete": False,
                },
            },
            raw_output="partial evidence with unestablished termination",
        )

    _, selected, budget, store, executor = make_executor(tmp_path, strict_executor=uncertain)
    records = executor.run_ordered([request(selected), request(selected, "would-repeat")])
    assert len(records) == budget.launches_started == 1
    trial = records[0]
    assert trial.status == TrialStatus.UNKNOWN
    if timed_out:
        assert trial.timed_out is None
        assert trial.extensions["simtune.cancellation"]["deadline_expired"] is True
    assert trial.validation.state == ValidationState.UNKNOWN
    assert trial.extensions["simtune.cancellation"]["termination_known"] is False
    assert store.recover().states[trial.id] == TrialState.UNCERTAIN
    assert "partial evidence" in (store.root / f"trials/{trial.id}/launch.txt").read_text()
    with pytest.raises(ValidationError, match="uncertain journal state"):
        store.complete("must not accept uncertainty")
