"""Real harmless replay processes; synthetic metadata makes no Linux claim."""

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from process_helpers import SIGNALS, cleanup, living, wait_for
from test_recipes import (  # noqa: F401
    build_recipe,
    remap,
)
from test_recipes import (
    incomplete_snapshot as incomplete_snapshot,
)
from test_recipes import (
    recipe_snapshot as recipe_snapshot,
)
from test_recipes import (
    saved as saved,
)
from test_search import CAPABILITIES

from simtune import cli, replay
from simtune.hardware import HARDWARE_KEY, compare_hardware, hardware_context
from simtune.recipes import load_recipe
from simtune.replay import exec_recipe
from simtune.validation import ValidationError
from simtune.workloads import sha256_file


@pytest.mark.parametrize("change", ["model", "missing", "old", "old-v2"])
def test_hardware_change_unknown_or_old_requires_explicit_acknowledgement(
    saved, recipe_snapshot, tmp_path, monkeypatch, capsys, change
):
    path, workload, _ = saved
    current = recipe_snapshot
    if change == "model":
        identity = current.extensions[HARDWARE_KEY]
        current = replace(
            current,
            extensions={
                HARDWARE_KEY: {
                    **identity,
                    "processors": [
                        {**p, "model": "Different model"} for p in identity["processors"]
                    ],
                }
            },
        )
    elif change == "missing":
        current = replace(current, extensions={})
    else:
        data = json.loads(path.read_text())
        details = data["extensions"]["simtune.stage06"]
        details["schema_version"] = 2
        del details["hardware"]
        del details["label_scope"]
        path.write_text(json.dumps(data))
        if change == "old":
            from test_recipes import legacy_recipe

            legacy_recipe(path)
    before = {p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}
    monkeypatch.setattr(replay, "execute_strict_launch", lambda *a, **k: pytest.fail("launched"))
    bundle = load_recipe(path)
    assert bundle.details["heldout_confirmed"] and bundle.details["replay_validated"]
    kwargs = dict(
        cwd=tmp_path, dry_run=True, snapshot_provider=lambda: current, capabilities=CAPABILITIES
    )
    with pytest.raises(ValidationError, match="hardware class"):
        exec_recipe(path, command(workload, tmp_path), **kwargs)
    result = exec_recipe(path, command(workload, tmp_path), allow_unvalidated=True, **kwargs)
    assert result.record["current_compatibility"] == "passed"
    assert result.record["current_output_validation"] == "unchecked"
    assert result.record["historical_protocol_valid"]
    assert result.record["label"] == "validated" and "historical" in result.record["label_scope"]
    assert not result.record["validated_reuse_authorized"]
    assert result.record["hardware_class"]["status"] == (
        "changed" if change == "model" else "unknown"
    )
    assert "current output unchecked" in capsys.readouterr().err
    assert all(p.read_bytes() == value for p, value in before.items())


def test_hardware_remapping_preserves_class_and_heterogeneity(recipe_snapshot):
    assert (
        compare_hardware(hardware_context(recipe_snapshot), remap(recipe_snapshot))["status"]
        == "matched"
    )
    identity = recipe_snapshot.extensions[HARDWARE_KEY]
    heterogeneous = replace(
        recipe_snapshot,
        extensions={
            HARDWARE_KEY: {
                **identity,
                "processors": [
                    identity["processors"][0],
                    {**identity["processors"][1], "model": "Other model"},
                ],
            }
        },
    )
    assert (
        hardware_context(heterogeneous)["class_id"] != hardware_context(recipe_snapshot)["class_id"]
    )
    assert (
        hardware_context(remap(heterogeneous))["class_id"]
        == hardware_context(heterogeneous)["class_id"]
    )


def test_explain_human_distinguishes_compatibility_and_class_authorization(
    saved, recipe_snapshot, monkeypatch, capsys
):
    path, workload, _ = saved
    current = replace(recipe_snapshot, extensions={})
    monkeypatch.setattr(replay, "search_snapshot", lambda: current)
    monkeypatch.setattr(replay, "search_capabilities", lambda _: CAPABILITIES)
    monkeypatch.setattr(replay, "execute_strict_launch", lambda *a, **k: pytest.fail("launched"))
    assert cli.main(["explain", str(path), "--workload", str(workload.manifest_path)]) == 0
    human = capsys.readouterr().out
    for phrase in (
        "historically validated",
        "Current mechanical compatibility: passed",
        "Hardware class: unknown",
        "Validated reuse authorized: False",
        "explicitly revalidate",
        "exec output is unchecked",
    ):
        assert phrase in human


def test_matching_hardware_cannot_upgrade_unchecked_science(tmp_path, recipe_snapshot):
    path, workload, _ = build_recipe(tmp_path, recipe_snapshot, checked=False)
    kwargs = dict(
        cwd=tmp_path,
        dry_run=True,
        snapshot_provider=lambda: recipe_snapshot,
        capabilities=CAPABILITIES,
    )
    with pytest.raises(ValidationError, match="historical validation"):
        exec_recipe(path, command(workload, tmp_path), **kwargs)
    result = exec_recipe(path, command(workload, tmp_path), allow_unvalidated=True, **kwargs)
    assert result.record["hardware_class"]["status"] == "matched"
    assert not result.record["validated_reuse_authorized"]
    assert not result.record["historical_protocol_valid"]


def test_class_loss_immediately_before_launch_rejects_without_baseline(
    saved, recipe_snapshot, tmp_path, monkeypatch
):
    path, workload, _ = saved
    snapshots = iter([recipe_snapshot, replace(recipe_snapshot, extensions={})])
    with pytest.raises(ValidationError, match="before launch"):
        exec_recipe(
            path,
            command(workload, tmp_path),
            cwd=tmp_path,
            out=tmp_path / "replay",
            snapshot_provider=lambda: next(snapshots),
            capabilities=CAPABILITIES,
        )
    record = json.loads((tmp_path / "replay/replay.json").read_text())
    assert record["launches"] == 0 and record["status"] == "rejected"
    assert record["current_compatibility"] == "rejected"
    assert record["hardware_class"]["status"] == "unknown"
    assert not record["validated_reuse_authorized"]
    assert not (tmp_path / "launches").exists()


def command(workload, cwd):
    return [workload.spec.resolved_executable, workload.spec.cases[0].arguments["input"], str(cwd)]


def test_exactly_one_minimal_launch_and_streams(
    saved, recipe_snapshot, tmp_path, capfd, monkeypatch
):
    path, workload, _ = saved

    def forbidden(*a, **k):
        pytest.fail("replay inserted profiling")

    monkeypatch.setattr("simtune.profiler.profile_command", forbidden)
    result = exec_recipe(
        path,
        command(workload, tmp_path),
        cwd=tmp_path,
        out=tmp_path / "replay",
        snapshot_provider=lambda: recipe_snapshot,
        capabilities=CAPABILITIES,
    )
    captured = capfd.readouterr()
    assert captured.out == "out α\nSTDERR:\n"
    assert "err\nSTDOUT:\n" in captured.err and "validated recipe" in captured.err
    assert (tmp_path / "launches").read_text() == "launch\n"
    assert result.status == 0 and result.record["launches"] == 1
    assert not result.record["metrics"]["profiler"]["used_time"]
    assert not result.record["metrics"]["profiler"]["used_perf"]
    assert result.record["metrics"]["profiler"]["final_cmd"] == command(workload, tmp_path)
    assert "baseline" not in result.record["metrics"]["profiler"]["final_cmd"]
    assert result.record["science"].startswith("unchecked")
    assert json.loads((result.directory / "replay.json").read_text())["status"] == "succeeded"


@pytest.mark.parametrize(
    "mode,expected,raw", [("exit", 17, 17), ("signal", 143, -15), ("wait", 124, 124)]
)
def test_exit_signal_timeout_no_retry(
    saved, recipe_snapshot, tmp_path, mode, expected, raw, monkeypatch
):
    from simtune import profiler

    launches = []
    popen = profiler.subprocess.Popen

    def counted(*args, **kwargs):
        launches.append(args[0])
        return popen(*args, **kwargs)

    monkeypatch.setattr(profiler.subprocess, "Popen", counted)
    path, workload, _ = saved
    result = exec_recipe(
        path,
        command(workload, tmp_path),
        cwd=tmp_path,
        out=tmp_path / "replay",
        timeout=0.2 if mode == "wait" else 3,
        base_env={**os.environ, "REPLAY_TEST_MODE": mode},
        snapshot_provider=lambda: recipe_snapshot,
        capabilities=CAPABILITIES,
    )
    assert result.status == expected and result.record["return_code"] == raw
    assert result.record["metrics"]["cancellation"]["parent_signal"] is None
    assert launches == [command(workload, tmp_path)]
    marker = tmp_path / "launches"
    # A timeout may expire during interpreter startup under load. Popen is the
    # authoritative launch counter; a started sentinel still must occur once.
    if mode != "wait" or marker.exists():
        assert marker.read_text() == "launch\n"


@pytest.mark.parametrize(
    "what", ["executable", "source", "topology", "argument", "runtime", "class"]
)
def test_incompatible_launches_zero_even_with_override(
    saved, recipe_snapshot, tmp_path, monkeypatch, what
):
    path, workload, _ = saved
    argv = command(workload, tmp_path)
    kwargs = {}
    s = recipe_snapshot
    if what in {"source", "executable"}:
        with Path(workload.spec.resolved_executable).open("a") as stream:
            stream.write("\n# modified source\n")
    elif what == "topology":
        s = replace(s, cpus=[replace(c, core_id=0, smt_siblings=[0, 1]) for c in s.cpus])
    elif what == "argument":
        argv[1] = "not-declared"
    else:
        spec = (
            replace(workload.spec, runtime=replace(workload.spec.runtime, version="changed"))
            if what == "runtime"
            else replace(
                workload.spec, cases=[replace(c, input_class="new") for c in workload.spec.cases]
            )
        )
        changed = tmp_path / "changed.json"
        changed.write_text(spec.to_json())
        kwargs["workload_path"] = changed
    monkeypatch.setattr(replay, "execute_strict_launch", lambda *a, **k: pytest.fail("launched"))
    with pytest.raises(ValidationError):
        exec_recipe(
            path,
            argv,
            cwd=tmp_path,
            out=tmp_path / "absent",
            allow_unvalidated=True,
            snapshot_provider=lambda: s,
            capabilities=CAPABILITIES,
            **kwargs,
        )
    assert not (tmp_path / "launches").exists()


def test_explicit_unvalidated_override_is_retained(tmp_path, recipe_snapshot, capfd):
    path, workload, _ = build_recipe(tmp_path, recipe_snapshot, checked=False)
    kwargs = dict(
        cwd=tmp_path,
        out=tmp_path / "replay",
        snapshot_provider=lambda: recipe_snapshot,
        capabilities=CAPABILITIES,
    )
    with pytest.raises(ValidationError, match="allow-unvalidated"):
        exec_recipe(path, command(workload, tmp_path), **kwargs)
    assert not (tmp_path / "launches").exists()
    result = exec_recipe(path, command(workload, tmp_path), allow_unvalidated=True, **kwargs)
    assert result.record["label"] == "unvalidated" and result.record["allow_unvalidated"]
    assert "unvalidated recipe" in capfd.readouterr().err


def test_new_declared_case_values_preserve_class_and_literals(saved, recipe_snapshot, tmp_path):
    path, workload, _ = saved
    literal = "$HOME α\n --flag"
    spec = replace(
        workload.spec, cases=[replace(c, arguments={"input": literal}) for c in workload.spec.cases]
    )
    manifest = tmp_path / "new-case.json"
    manifest.write_text(spec.to_json())
    argv = [spec.resolved_executable, literal, str(tmp_path)]
    result = exec_recipe(
        path,
        argv,
        workload_path=manifest,
        cwd=tmp_path,
        out=tmp_path / "replay",
        snapshot_provider=lambda: remap(recipe_snapshot),
        capabilities=CAPABILITIES,
    )
    assert result.status == 0 and result.record["argv"] == argv
    assert result.record["plan"]["cpu_ids"] == [12, 30]
    assert (tmp_path / "launches").read_text() == "launch\n"


def test_dry_run_and_explain_check_launch_nothing(
    saved, recipe_snapshot, tmp_path, monkeypatch, capsys
):
    path, workload, _ = saved
    monkeypatch.setattr(replay, "search_snapshot", lambda: recipe_snapshot)
    monkeypatch.setattr(replay, "search_capabilities", lambda _: CAPABILITIES)
    monkeypatch.setattr(replay, "execute_strict_launch", lambda *a, **k: pytest.fail("launched"))
    result = exec_recipe(
        path, command(workload, tmp_path), cwd=tmp_path, out=tmp_path / "absent", dry_run=True
    )
    assert result.directory is None and not (tmp_path / "absent").exists()
    assert (
        cli.main(["explain", str(path), "--json", "--workload", str(workload.manifest_path)]) == 0
    )
    data = json.loads(capsys.readouterr().out)
    assert data["applicability"]["checked_on_current_host"]
    assert not data["applicability"]["failures"]
    assert not (tmp_path / "launches").exists()


def test_resource_change_immediately_before_launch_is_recorded_rejected(
    saved, recipe_snapshot, tmp_path
):
    path, workload, _ = saved
    narrow = replace(recipe_snapshot, inherited_cpu_ids=[0], effective_cpu_ids=[0])
    snapshots = iter([recipe_snapshot, narrow])
    with pytest.raises(ValidationError, match="topology"):
        exec_recipe(
            path,
            command(workload, tmp_path),
            cwd=tmp_path,
            out=tmp_path / "replay",
            snapshot_provider=lambda: next(snapshots),
            capabilities=CAPABILITIES,
        )
    data = json.loads((tmp_path / "replay/replay.json").read_text())
    assert data["status"] == "rejected" and data["launches"] == 0
    assert not data["compatibility_passed"] and not data["required_settings_passed"]
    assert not (tmp_path / "launches").exists()


def _driver(tmp_path, path, workload, snapshot, *, mode="normal"):
    driver = tmp_path / "driver.py"
    driver.write_text("""import json, os, sys
from pathlib import Path
from simtune.models import ResourceSnapshot
from simtune.replay import exec_recipe
snapshot=ResourceSnapshot.from_dict(json.loads(sys.argv[4]))
result=exec_recipe(sys.argv[1], json.loads(sys.argv[2]), cwd=sys.argv[3], out=Path(sys.argv[3])/"replay",
    snapshot_provider=lambda: snapshot, capabilities={"platform":"linux","tools":{}},
    base_env={**os.environ,"REPLAY_TEST_MODE":sys.argv[5]})
sys.exit(result.status)
""")
    return [
        sys.executable,
        str(driver),
        str(path),
        json.dumps(command(workload, tmp_path)),
        str(tmp_path),
        snapshot.to_json(),
        mode,
    ]


def test_binary_stdin_and_separate_stdout_stderr(saved, recipe_snapshot, tmp_path):
    path, workload, _ = saved
    argv = _driver(tmp_path, path, workload, recipe_snapshot, mode="input")
    result = subprocess.run(argv, input=b"literal\x00\xff\n", capture_output=True, timeout=15)
    assert result.returncode == 0
    assert result.stdout == b"literal\x00\xff\n"
    assert b"err\nSTDOUT:\n" in result.stderr
    assert (tmp_path / "launches").read_text() == "launch\n"


@pytest.mark.parametrize("sig", SIGNALS)
@pytest.mark.parametrize("mode", ["wait", "ignore"])
def test_parent_signals_forwarded_to_single_workload(saved, recipe_snapshot, tmp_path, sig, mode):
    path, workload, _ = saved
    argv = _driver(tmp_path, path, workload, recipe_snapshot, mode=mode)
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_for(lambda: (tmp_path / "ready.json").exists(), proc)
        ready = json.loads((tmp_path / "ready.json").read_text())
        proc.send_signal(sig)
        out, err = proc.communicate(timeout=5)
        assert proc.returncode == 128 + sig, (out, err)
        assert not living(ready["pid"])
    finally:
        cleanup(proc, tmp_path)
    data = json.loads((tmp_path / "replay/replay.json").read_text())
    assert data["status"] == "interrupted" and data["return_code"] == -sig
    assert data["metrics"]["cancellation"]["parent_signal"] == sig
    assert data["metrics"]["cancellation"]["termination_known"] is True
    assert (tmp_path / "launches").read_text() == "launch\n"


def test_cli_actual_exit_status_and_no_baseline(saved, recipe_snapshot, tmp_path, monkeypatch):
    path, workload, _ = saved
    monkeypatch.setattr(replay, "search_snapshot", lambda: recipe_snapshot)
    monkeypatch.setattr(replay, "search_capabilities", lambda _: CAPABILITIES)
    monkeypatch.setenv("REPLAY_TEST_MODE", "exit")
    assert (
        cli.main(
            [
                "exec",
                "--recipe",
                str(path),
                "--cwd",
                str(tmp_path),
                "--out",
                str(tmp_path / "replay"),
                "--",
                *command(workload, tmp_path),
            ]
        )
        == 17
    )
    assert (tmp_path / "launches").read_text() == "launch\n"


def test_failed_science_revalidation_does_not_relabel_source(saved, recipe_snapshot, tmp_path):
    from test_search import FakeClock, SyntheticExecution

    from simtune.replay import validate_recipe
    from simtune.search import SearchConfig

    path, workload, _ = saved
    before = sha256_file(path)
    bad = replace(
        workload.spec, output_checks=[replace(workload.spec.output_checks[0], expected=999)]
    )
    manifest = tmp_path / "bad-science.json"
    manifest.write_text(bad.to_json())
    clock = FakeClock()
    execution = SyntheticExecution(clock, times={"baseline": 2, "recipe-policy": 1})
    result, derived = validate_recipe(
        path,
        manifest,
        tmp_path / "bad-validation",
        config=SearchConfig(100, bootstrap_samples=100),
        snapshot_provider=lambda: recipe_snapshot,
        capabilities=CAPABILITIES,
        clock=clock,
        executor_kwargs=execution.kwargs,
    )
    assert result.outcome.value in {"failed", "inconclusive"}
    assert sha256_file(path) == before and load_recipe(path).details["replay_validated"]
    assert not load_recipe(derived).details["replay_validated"]


@pytest.mark.parametrize(
    "name,value",
    [
        ("GOMP_CPU_AFFINITY", "0 1"),
        ("OMP_PLACES", "{0},{1}"),
        ("KMP_AFFINITY", "explicit,proclist=[0,1]"),
    ],
)
def test_absolute_runtime_affinity_is_not_logical_reuse(
    saved, recipe_snapshot, tmp_path, monkeypatch, name, value
):
    path, workload, _ = saved
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(replay, "execute_strict_launch", lambda *a, **k: pytest.fail("launched"))
    with pytest.raises(ValidationError, match="runtime affinity"):
        exec_recipe(
            path,
            command(workload, tmp_path),
            cwd=tmp_path,
            allow_unvalidated=True,
            snapshot_provider=lambda: recipe_snapshot,
            capabilities=CAPABILITIES,
        )


def test_relative_input_must_resolve_to_the_hashed_declared_file(saved, recipe_snapshot, tmp_path):
    from simtune.models import Artifact

    path, workload, _ = saved
    source = Path(workload.spec.base_directory) / "input.txt"
    source.write_text("declared scientific input")
    alternate = tmp_path / "input.txt"
    alternate.write_text("different scientific input")
    spec = replace(
        workload.spec,
        cases=[
            replace(
                c,
                arguments={"input": "input.txt"},
                input_artifacts=[
                    Artifact(id="input", role="input", path="input.txt", sha256=sha256_file(source))
                ],
            )
            for c in workload.spec.cases
        ],
    )
    manifest = tmp_path / "relative.json"
    manifest.write_text(spec.to_json())
    with pytest.raises(ValidationError, match="relative file argument"):
        exec_recipe(
            path,
            [spec.resolved_executable, "input.txt", str(tmp_path)],
            cwd=tmp_path,
            workload_path=manifest,
            snapshot_provider=lambda: recipe_snapshot,
            capabilities=CAPABILITIES,
        )
    assert not (tmp_path / "launches").exists()


@pytest.mark.parametrize(
    "problem",
    [
        "reused_inputs",
        "renamed_artifacts",
        "early_confirmation",
        "insufficient_pairs",
        "unchecked",
        "failed_checks",
    ],
)
def test_full_protocol_rejection_survives_export_explain_and_replay(
    tmp_path, recipe_snapshot, monkeypatch, capsys, problem
):
    from test_recipes import build_protocol_recipe

    from simtune.search import STATE_KEY

    path, workload, experiment = build_protocol_recipe(
        tmp_path, recipe_snapshot, monkeypatch, problem
    )
    assert experiment.outcome.value == ("failed" if problem == "failed_checks" else "inconclusive")
    assert experiment.selected_policy_id == "baseline"
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    bundle = load_recipe(path)
    confirmation = bundle.details["confirmation"]
    assert not confirmation["protocol_valid"] and confirmation["protocol_reasons"]
    if problem in {"reused_inputs", "renamed_artifacts", "early_confirmation"}:
        assert confirmation["training"]["eligible"] and confirmation["confirmation"]["eligible"]
    assert not bundle.details["heldout_confirmed"] and not bundle.details["replay_validated"]
    if problem != "failed_checks":
        assert experiment.extensions[STATE_KEY]["confirmation_result"] == confirmation
    monkeypatch.setattr(replay, "search_snapshot", lambda: recipe_snapshot)
    monkeypatch.setattr(replay, "search_capabilities", lambda _: CAPABILITIES)
    launches = []
    monkeypatch.setattr(replay, "execute_strict_launch", lambda *a, **k: launches.append(a))
    assert cli.main(["explain", str(path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["label"] == "unvalidated"
    assert not report["execution_errors"]
    assert report["measured_evidence"]["confirmation"] == confirmation
    assert cli.main(["explain", str(path)]) == 0
    human = capsys.readouterr().out
    assert f"unvalidated; {experiment.outcome.value}" in human
    assert "Held-out confirmation: False" in human
    assert all(reason in human for reason in confirmation["protocol_reasons"])
    args = [
        "exec",
        "--recipe",
        str(path),
        "--cwd",
        str(tmp_path),
        "--out",
        str(tmp_path / "absent"),
    ]
    assert cli.main([*args, "--dry-run", "--", *command(workload, tmp_path)]) == 2
    assert "allow-unvalidated" in capsys.readouterr().err
    assert cli.main([*args, "--", *command(workload, tmp_path)]) == 2
    capsys.readouterr()
    assert (
        cli.main([*args, "--dry-run", "--allow-unvalidated", "--", *command(workload, tmp_path)])
        == 0
    )
    dry = json.loads(capsys.readouterr().out)
    assert dry["label"] == "unvalidated" and dry["compatibility_passed"]
    assert dry["science"].startswith("unchecked") and dry["launches"] == 0
    incompatible = command(workload, tmp_path)
    incompatible[1] = "changed-scientific-input"
    assert cli.main([*args, "--dry-run", "--allow-unvalidated", "--", *incompatible]) == 2
    assert launches == [] and not (tmp_path / "absent").exists()
    assert all(p.read_bytes() == value for p, value in before.items())


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("problem", ["reused_inputs", "early_confirmation"])
def test_inconsistent_confirmation_claims_are_diagnostic_only(
    tmp_path, recipe_snapshot, monkeypatch, capsys, legacy, problem
):
    from test_recipes import build_protocol_recipe, legacy_recipe

    from simtune.recipes import RECIPE_KEY

    path, workload, _ = build_protocol_recipe(tmp_path, recipe_snapshot, monkeypatch, problem)
    if legacy:
        legacy_recipe(path)
    data = json.loads(path.read_text())
    details = data["extensions"][RECIPE_KEY]
    details["heldout_confirmed"] = details["replay_validated"] = True
    details["limitations"].remove("Independent held-out confirmation is absent or ineligible.")
    if not legacy:
        details["confirmation"]["protocol_valid"] = True
        details["confirmation"]["protocol_reasons"] = []
    path.write_text(json.dumps(data))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ValidationError, match="execution refused.*simtune explain"):
        load_recipe(path)
    assert cli.main(["explain", str(path), "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["label"] == "rejected" and report["execution_errors"]
    assert report["stored_recipe"]["extensions"][RECIPE_KEY]["heldout_confirmed"]
    assert not report["measured_evidence"]["heldout_confirmed"]
    assert not report["measured_evidence"]["confirmation"]["protocol_valid"]
    assert cli.main(["explain", str(path)]) == 2
    human = capsys.readouterr().out
    assert "rejected; inconclusive" in human and "Held-out confirmation: False" in human
    assert "Execution rejected:" in human and "Source files were not changed" in human
    monkeypatch.setattr(replay, "execute_strict_launch", lambda *a, **k: pytest.fail("launched"))
    for override in (False, True):
        with pytest.raises(ValidationError, match="execution refused"):
            exec_recipe(
                path,
                command(workload, tmp_path),
                cwd=tmp_path,
                out=tmp_path / "absent",
                allow_unvalidated=override,
                dry_run=True,
                snapshot_provider=lambda: recipe_snapshot,
                capabilities=CAPABILITIES,
            )
    diagnostic = load_recipe(path, diagnostic=True)
    with pytest.raises(ValidationError, match="execution refused"):
        replay.resolve_recipe(diagnostic, recipe_snapshot, CAPABILITIES)
    assert not (tmp_path / "absent").exists()
    assert all(p.read_bytes() == value for p, value in before.items())


@pytest.mark.parametrize("legacy", [False, True])
def test_complete_no_gain_confirmation_allows_reference_replay(
    tmp_path, recipe_snapshot, monkeypatch, capsys, legacy
):
    from test_recipes import legacy_recipe

    def timing(policy, case):
        return 1 if policy == "runtime-close" and case == "training-data" else 2

    path, workload, experiment = build_recipe(tmp_path, recipe_snapshot, times=timing)
    assert (
        experiment.outcome.value == "no_improvement" and experiment.selected_policy_id == "baseline"
    )
    if legacy:
        legacy_recipe(path)
    bundle = load_recipe(path)
    assert bundle.details["confirmation"]["protocol_valid"]
    assert bundle.details["confirmation"]["protocol_reasons"] == []
    assert bundle.details["validated_saved_seconds"] is None
    assert bundle.details["break_even_runs"] is None
    monkeypatch.setattr(replay, "search_snapshot", lambda: recipe_snapshot)
    monkeypatch.setattr(replay, "search_capabilities", lambda _: CAPABILITIES)
    monkeypatch.setattr(replay, "execute_strict_launch", lambda *a, **k: pytest.fail("launched"))
    assert cli.main(["explain", str(path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (
        report["label"] == "validated"
        and report["recipe"]["performance_outcome"] == "no_improvement"
    )
    assert cli.main(["explain", str(path)]) == 0
    assert "validated; no_improvement" in capsys.readouterr().out
    if legacy:
        with pytest.raises(ValidationError, match="hardware class"):
            exec_recipe(path, command(workload, tmp_path), cwd=tmp_path, dry_run=True)
    result = exec_recipe(
        path, command(workload, tmp_path), cwd=tmp_path, dry_run=True, allow_unvalidated=legacy
    )
    assert result.record["label"] == "validated"
    assert result.record["plan"]["policy"]["id"] == "baseline"
    assert result.record["launches"] == 0 and result.record["science"].startswith("unchecked")


def test_replay_inherited_numa_memory_contradiction_launches_zero(
    recipe_snapshot, tmp_path, monkeypatch
):
    from simtune.launch import StrictPlanningError
    from simtune.models import LimitState, MemoryLimit

    def needs_memory(spec):
        return replace(
            spec,
            cases=[
                replace(
                    case,
                    resource_needs=replace(case.resource_needs, memory_bytes=4096, unavailable={}),
                )
                for case in spec.cases
            ],
        )

    path, workload, _ = build_recipe(tmp_path, recipe_snapshot, spec_transform=needs_memory)
    current = replace(
        recipe_snapshot,
        memory_limits=[
            MemoryLimit(
                source="current ancestor", scope="allocation", state=LimitState.LIMITED, bytes=1024
            )
        ],
    )
    monkeypatch.setattr(replay, "execute_strict_launch", lambda *a, **k: pytest.fail("launched"))
    with pytest.raises(StrictPlanningError, match="1024 bytes"):
        exec_recipe(
            path,
            command(workload, tmp_path),
            cwd=tmp_path,
            out=tmp_path / "absent",
            snapshot_provider=lambda: current,
            capabilities=CAPABILITIES,
        )
    assert not (tmp_path / "launches").exists()
    assert not (tmp_path / "absent").exists()
