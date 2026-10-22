"""Checked example integration; synthetic timings/hosts establish no speed gain."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_recipes import recipe_snapshot as _recipe_snapshot
from test_search import CAPABILITIES, FakeClock, snapshot  # noqa: F401

from examples.check_heat_result import check_result
from simtune.checks import run_checks
from simtune.launch import StrictLaunchResult, plan_strict_launch
from simtune.models import Outcome, TrialRecord, ValidationState
from simtune.recipes import load_recipe, save_recipe
from simtune.replay import exec_recipe, validate_recipe
from simtune.search import SearchConfig, run_search
from simtune.validation import ValidationError
from simtune.workloads import expand_trial_argv, load_workload

incomplete_snapshot = snapshot

recipe_snapshot = _recipe_snapshot

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def openmp_heat(tmp_path_factory):
    requested = os.environ.get("SIMTUNE_OPENMP_CC")
    compiler = requested or shutil.which("gcc") or shutil.which("cc")
    if not compiler:
        pytest.skip("optional local OpenMP compiler unavailable; CI supplies SIMTUNE_OPENMP_CC")
    binary = tmp_path_factory.mktemp("checked heat $ literal") / "heat2d"
    run = subprocess.run(
        [
            compiler,
            "-O3",
            "-std=c11",
            "-fopenmp",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(ROOT / "examples/heat2d.c"),
            "-lm",
            "-o",
            str(binary),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if run.returncode and not requested:
        pytest.skip("default compiler lacks OpenMP; set SIMTUNE_OPENMP_CC to require this gate")
    assert run.returncode == 0, run.stderr
    return binary


def helper_command(binary, library, output):
    return [
        sys.executable,
        str(ROOT / "examples/make_heat_workload.py"),
        "--binary",
        str(binary),
        "--cpus",
        "2",
        "--threads",
        "2",
        "--training-size",
        "64",
        "--validation-size",
        "80",
        "--steps",
        "3",
        "--wave",
        "1",
        "--first-touch",
        "parallel",
        "--runtime-name",
        "libgomp",
        "--runtime-version",
        "synthetic declared library v1",
        "--runtime-library",
        str(library),
        "--out",
        str(output),
    ]


def generate(tmp_path, binary):
    # Library metadata here is deliberately synthetic, never a native linkage claim.
    library = tmp_path / "declared runtime $ literal library"
    library.write_bytes(b"synthetic declared artifact")
    path = tmp_path / "heat workload.toml"
    result = subprocess.run(
        helper_command(binary, library, path), capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    return load_workload(path), library


def run_heat(binary, directory):
    return subprocess.run(
        [
            str(binary),
            "--size",
            "64",
            "--steps",
            "3",
            "--wave",
            "1",
            "--first-touch",
            "parallel",
            "--output-dir",
            str(directory),
        ],
        env={**os.environ, "OMP_NUM_THREADS": "2", "OMP_DYNAMIC": "FALSE"},
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_output_file_stdout_and_original_behavior(openmp_heat, tmp_path):
    run = run_heat(openmp_heat, tmp_path)
    assert run.returncode == 0, run.stderr
    assert (tmp_path / "heat-result.json").read_text() == run.stdout
    assert check_result(tmp_path, size=64, steps=3, wave=1, first_touch="parallel", threads=2)
    legacy = subprocess.run(
        [str(openmp_heat), "--size", "64", "--steps", "3"],
        cwd=tmp_path,
        env={**os.environ, "OMP_NUM_THREADS": "2"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert legacy.returncode == 0
    assert json.loads(legacy.stdout)["checksum"] == json.loads(run.stdout)["checksum"]
    failure = run_heat(openmp_heat, tmp_path / "missing")
    assert failure.returncode == 1 and "heat-result.json" in failure.stderr
    # A directory in place of the result file is an explicit write failure.
    (tmp_path / "heat-result.json").unlink()
    (tmp_path / "heat-result.json").mkdir()
    assert run_heat(openmp_heat, tmp_path).returncode == 1


def test_generated_manifest_paths_resources_and_overwrite(openmp_heat, tmp_path, monkeypatch):
    workload, library = generate(tmp_path, openmp_heat)
    spec = workload.spec
    assert spec.runtime.name == "libgomp" and spec.runtime.identity
    assert spec.cases[0].resource_needs == spec.cases[1].resource_needs
    assert spec.cases[0].resource_needs.reference_cpu_ids is None
    assert spec.cases[0].resource_needs.memory_bytes is None
    assert {a.id for a in spec.artifacts} == {
        "heat-source",
        "heat-validator",
        "openmp-library",
        "validator-python",
    }
    assert all(Path(a.path).is_absolute() for a in spec.artifacts)
    assert spec.cases[0].arguments != spec.cases[1].arguments
    workspace = tmp_path / "trial $ literal"
    workspace.mkdir()
    argv = expand_trial_argv(spec, spec.cases[0], workspace)
    assert argv[-1] == str(workspace) and argv[2] == "64"
    assert spec.cases[0].checks[0].argv[-9:] == [
        "64",
        "--steps",
        "3",
        "--wave",
        "1",
        "--first-touch",
        "parallel",
        "--threads",
        "2",
    ]
    before = workload.manifest_path.read_bytes()
    result = subprocess.run(
        helper_command(openmp_heat, library, workload.manifest_path),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2 and "overwrite" in result.stderr
    assert workload.manifest_path.read_bytes() == before
    monkeypatch.chdir(tmp_path.parent)
    assert load_workload(workload.manifest_path).spec == spec
    library.write_bytes(b"changed")
    with pytest.raises(ValidationError, match="SHA-256 changed"):
        load_workload(workload.manifest_path)


@pytest.mark.parametrize(
    "option,value",
    [
        ("--cpus", "0"),
        ("--threads", "3"),
        ("--steps", "1000001"),
        ("--steps", "nan"),
        ("--training-size", "2"),
        ("--validation-size", "64"),
        ("--wave", "63"),
        ("--runtime-version", ""),
        ("--runtime-library", "/nonexistent/runtime/library"),
    ],
)
def test_helper_rejects_incomplete_or_invalid_declarations(tmp_path, option, value):
    library = tmp_path / "runtime"
    library.write_bytes(b"declared")
    command = helper_command(Path(sys.executable), library, tmp_path / "bad.toml")
    command[command.index(option) + 1] = value
    run = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert run.returncode == 2 and run.stderr
    assert not (tmp_path / "bad.toml").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("valid", False),
        ("valid", 1),
        ("size", 80),
        ("steps", 4),
        ("wave", 2),
        ("first_touch", "serial"),
        ("threads", 1),
        ("threads", True),
        ("openmp", 0),
        ("checksum", float("nan")),
        ("checksum", float("inf")),
        ("checksum", True),
        ("max_abs_error", 1e-8),
        ("max_abs_error", -1.0),
        ("max_abs_error", None),
        ("tolerance", 1.0),
        ("schema_version", 2),
        ("grid_bytes", 1),
        ("updates", 2),
    ],
)
def test_pinned_check_rejects_bad_result(openmp_heat, tmp_path, field, value):
    workload, _ = generate(tmp_path, openmp_heat)
    workspace = tmp_path / "trial"
    workspace.mkdir()
    data = json.loads(run_heat(openmp_heat, workspace).stdout)
    data[field] = value
    (workspace / "heat-result.json").write_text(json.dumps(data))
    report = run_checks(
        workload.spec.cases[0].checks,
        workspace,
        tmp_path / "checks",
        evidence_prefix="checks",
        base_env=os.environ,
        deadline=None,
        clock=time.monotonic,
    )
    assert report.result.state == ValidationState.FAILED and report.launches_started == 1
    assert report.spent_seconds > 0
    assert json.loads((tmp_path / "checks/heat-result.json").read_text())["return_code"] == 1


@pytest.mark.parametrize("payload", [None, "{}", "{", '{"size":64,"size":80}', "[]"])
def test_validator_missing_malformed_duplicate_file(tmp_path, payload):
    if payload is not None:
        (tmp_path / "heat-result.json").write_text(payload)
    with pytest.raises((ValueError, OSError)):
        check_result(tmp_path, size=64, steps=3, wave=1, first_touch="parallel", threads=2)


class HeatExecution:
    """Real tiny solver/checks under synthetic planner/elapsed evidence only."""

    def __init__(self, clock, *, fail_check=False):
        self.clock = clock
        self.fail_check = fail_check

    def strict(
        self, plan, argv, capabilities, timeout, *, base_env, snapshot_provider, cwd, before_launch
    ):
        plan_strict_launch(plan.policy, snapshot_provider(), capabilities)
        timeout = before_launch(timeout)
        env = {**base_env, **dict(plan.env_overrides)}
        run = subprocess.run(
            argv, cwd=cwd, env=env, timeout=timeout, capture_output=True, text=True
        )
        if self.fail_check:
            data = json.loads((cwd / "heat-result.json").read_text())
            data["max_abs_error"] = 1.0
            data["tolerance"] = 2.0  # An apparently successful process cannot relax the check.
            (cwd / "heat-result.json").write_text(json.dumps(data))
        self.clock.advance(0.05)
        return StrictLaunchResult(
            final_argv=tuple(argv),
            metrics={"return_code": run.returncode, "timed_out": False, "wall_time_sec": 2.0},
            raw_output=run.stdout + run.stderr + "\nSYNTHETIC elapsed evidence, no speed claim\n",
            policy_id=plan.policy.id,
            snapshot_id=plan.snapshot_id,
            actions=plan.actions,
            unobserved=plan.unobserved,
        )


@pytest.mark.parametrize("fail_check", [False, True])
def test_checked_search_negative_and_failed_check(
    openmp_heat, tmp_path, recipe_snapshot, fail_check
):
    workload, _ = generate(tmp_path, openmp_heat)
    clock = FakeClock()
    executor = HeatExecution(clock, fail_check=fail_check)
    root = tmp_path / "search"
    result = run_search(
        workload,
        root,
        SearchConfig(120, repeat=1, warmups=0, bootstrap_samples=100),
        clock=clock,
        snapshot_provider=lambda: recipe_snapshot,
        capabilities=CAPABILITIES,
        executor_kwargs={"strict_executor": executor.strict},
    )
    records = [
        TrialRecord.from_json(p.read_text(), root=root)
        for p in (root / "trials").glob("*/trial.json")
    ]
    assert records
    assert all(
        r.validation.state == (ValidationState.FAILED if fail_check else ValidationState.PASSED)
        for r in records
    )
    assert result.outcome != Outcome.VALIDATED_IMPROVEMENT
    if fail_check:
        assert result.outcome in (Outcome.FAILED, Outcome.INCONCLUSIVE)
        return
    assert result.outcome == Outcome.NO_IMPROVEMENT
    recipe = tmp_path / "negative.recipe.json"
    save_recipe(root, recipe)
    bundle = load_recipe(recipe)
    before = recipe.read_bytes()
    assert not bundle.details["heldout_confirmed"]
    replay = tmp_path / "replay cwd"
    replay.mkdir()
    argv = expand_trial_argv(workload.spec, workload.spec.cases[0], replay)
    with pytest.raises(ValidationError, match="unvalidated|revalidation"):
        exec_recipe(
            recipe,
            argv,
            snapshot_provider=lambda: recipe_snapshot,
            capabilities=CAPABILITIES,
            cwd=replay,
            workload_path=workload.manifest_path,
            dry_run=True,
        )
    # Matching cwd is mandatory even for explicitly acknowledged replay.
    with pytest.raises(ValidationError, match="match|case|argv"):
        exec_recipe(
            recipe,
            argv[:-1] + [str(tmp_path)],
            snapshot_provider=lambda: recipe_snapshot,
            capabilities=CAPABILITIES,
            cwd=replay,
            workload_path=workload.manifest_path,
            allow_unvalidated=True,
            dry_run=True,
        )
    derived, derived_path = validate_recipe(
        recipe,
        workload.manifest_path,
        tmp_path / "validation",
        config=SearchConfig(120, repeat=1, warmups=0, bootstrap_samples=100),
        clock=clock,
        snapshot_provider=lambda: recipe_snapshot,
        capabilities=CAPABILITIES,
        executor_kwargs={"strict_executor": executor.strict},
    )
    assert derived.outcome == Outcome.NO_IMPROVEMENT
    validated = load_recipe(derived_path)
    assert validated.details["heldout_confirmed"]
    assert recipe.read_bytes() == before


@pytest.mark.parametrize(
    "artifact_id", ["heat-source", "heat-validator", "openmp-library", "validator-python"]
)
def test_each_pinned_artifact_is_rechecked(openmp_heat, tmp_path, artifact_id):
    from dataclasses import replace

    workload, _ = generate(tmp_path, openmp_heat)
    artifact = next(a for a in workload.spec.artifacts if a.id == artifact_id)
    copy = tmp_path / "pinned artifact copy"
    copy.write_bytes(Path(artifact.path).read_bytes())
    copied = replace(artifact, path=str(copy))
    spec = replace(
        workload.spec,
        artifacts=[copied if a.id == artifact_id else a for a in workload.spec.artifacts],
    )
    manifest = tmp_path / "copied.json"
    manifest.write_text(spec.to_json())
    load_workload(manifest)
    copy.write_bytes(copy.read_bytes() + b"changed")
    with pytest.raises(ValidationError, match="SHA-256 changed"):
        load_workload(manifest)


def test_validator_confines_symlink_and_bounds_file(tmp_path):
    workspace = tmp_path / "trial"
    workspace.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    output = workspace / "heat-result.json"
    output.symlink_to(outside)
    with pytest.raises(ValueError, match="inside"):
        check_result(workspace, size=64, steps=3, wave=1, first_touch="parallel", threads=2)
    output.unlink()
    output.write_bytes(b" " * 65537)
    with pytest.raises(ValueError, match="64 KiB"):
        check_result(workspace, size=64, steps=3, wave=1, first_touch="parallel", threads=2)
