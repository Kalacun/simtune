"""Invocation-local tune export reporting, with real search stores and recipes.

Synthetic hosts and deterministic elapsed metrics establish no native speedup.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_recipes import recipe_snapshot as _recipe_snapshot
from test_search import CAPABILITIES, FakeClock, SyntheticExecution, make_workload
from test_search import snapshot as _snapshot
from test_search_budgets import PERF_CAPS

from simtune import cli, tune
from simtune.experiments import ExperimentStore, TrialExecutor
from simtune.models import Platform
from simtune.recipes import load_recipe
from simtune.search import resume_search, run_search

incomplete_snapshot = _snapshot
recipe_snapshot = _recipe_snapshot


@pytest.fixture
def search_cli(tmp_path, recipe_snapshot, monkeypatch):
    monkeypatch.chdir(tmp_path)
    loaded = make_workload(tmp_path / "manifest")
    data = json.loads(loaded.manifest_path.read_text())
    data["runtime"] = {
        "name": "synthetic-openmp",
        "version": "fixture-1",
        "identity": data["executable_sha256"],
    }
    loaded.manifest_path.write_text(json.dumps(data))
    clock = FakeClock()
    execution = SyntheticExecution(clock)

    def configure(*, host=recipe_snapshot, caps=CAPABILITIES, load_cost=0):
        def search(load, out, config, **kwargs):
            def timed_load():
                result = load()
                clock.advance(load_cost)
                return result

            return run_search(
                timed_load,
                out,
                replace(config, bootstrap_samples=100),
                snapshot_provider=lambda: host,
                capabilities=caps,
                clock=clock,
                executor_kwargs=execution.kwargs,
                **kwargs,
            )

        def resume(out, **kwargs):
            return resume_search(
                out,
                snapshot_provider=lambda: host,
                capabilities=caps,
                clock=clock,
                executor_kwargs=execution.kwargs,
                **kwargs,
            )

        monkeypatch.setattr(tune, "run_search", search)
        monkeypatch.setattr(tune, "resume_search", resume)

    configure()
    args = [
        "tune",
        "--workload",
        str(loaded.manifest_path),
        "--budget",
        "100s",
        "--out",
        str(tmp_path / "search"),
        "--json",
    ]
    return args, configure, clock, execution, recipe_snapshot


def metadata(report, *, status, reason=None, requested=None):
    assert report["recipe_export"] == {
        "status": status,
        "reason": reason,
        "requested_path": requested,
    }
    assert report["recipe_path"] == (requested if status == "saved" else None)


@pytest.mark.parametrize("requested", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_unsupported_export_is_invocation_local(tmp_path, search_cli, capsys, requested, existing):
    args, configure, _, execution, host = search_cli
    configure(host=replace(host, platform=Platform.MACOS), caps={"platform": "darwin", "tools": {}})
    path = tmp_path / "recipe.json"
    sentinel = b"prior unrelated artifact\x00\xff"
    if existing:
        path.write_bytes(sentinel)
    assert cli.main([*args, *(["--save", str(path)] if requested else [])]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["kind"] == "simtune_tune_result"
    assert report["outcome"] == "inconclusive" and report["selected_policy_id"] is None
    assert report["budget"]["launches_started"] == 0 and execution.calls == []
    metadata(
        report,
        status="skipped" if requested else "not_requested",
        reason="no_selected_policy" if requested else None,
        requested=str(path) if requested else None,
    )
    assert ("Recipe not saved" in captured.err) == requested
    assert path.read_bytes() == sentinel if existing else not path.exists()
    assert not path.with_name(path.name + ".evidence").exists()


@pytest.mark.parametrize("outcome", ["validated_improvement", "no_improvement", "inconclusive"])
@pytest.mark.parametrize("requested", [False, True])
def test_real_export_and_no_request(tmp_path, search_cli, capsys, outcome, requested):
    args, _, _, execution, _ = search_cli
    if outcome == "no_improvement":
        execution.times = dict.fromkeys(execution.times, 2.0)
    if outcome == "inconclusive":
        # Completed checked training without independent held-out evidence still
        # exports under the existing unvalidated contract.
        manifest = Path(args[2])
        data = json.loads(manifest.read_text())
        data["validation_case_ids"] = []
        data["cases"] = [data["cases"][0]]
        manifest.write_text(json.dumps(data))
    path = "recipe α.json"  # Preserve CLI path spelling; resolve only as a reader.
    assert cli.main([*args, *(["--save", path] if requested else [])]) == (
        1 if outcome == "inconclusive" else 0
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["outcome"] == outcome and execution.calls
    metadata(
        report,
        status="saved" if requested else "not_requested",
        requested=path if requested else None,
    )
    if requested:
        bundle = load_recipe(report["recipe_path"])
        assert bundle.recipe.performance_outcome.value == outcome
        assert bundle.recipe.policy.id == report["selected_policy_id"]
        assert bundle.details["replay_validated"] == (outcome == "validated_improvement")
        assert "Recipe saved:" in captured.err
        assert (tmp_path / path).is_file()
    else:
        assert not (tmp_path / path).exists()


@pytest.mark.parametrize("stop", ["discovery", "training", "interrupted"])
def test_incomplete_exports_and_resume(tmp_path, search_cli, monkeypatch, capsys, stop):
    args, configure, clock, execution, _ = search_cli
    path = tmp_path / "recipe.json"
    path.write_bytes(b"sentinel")
    if stop == "discovery":
        configure(load_cost=101)
    elif stop == "training":

        def progress(*_):
            clock.advance(101)

        monkeypatch.setattr(tune, "_progress", progress)
    else:

        def interrupted(*_):
            raise KeyboardInterrupt

        monkeypatch.setattr(TrialExecutor, "run", interrupted)
    assert cli.main([*args, "--save", str(path)]) == (130 if stop == "interrupted" else 1)
    report = json.loads(capsys.readouterr().out)
    assert report["outcome"] == ("interrupted" if stop == "interrupted" else "inconclusive")
    assert report["kind"] == "simtune_tune_result"
    metadata(report, status="skipped", reason="incomplete_experiment", requested=str(path))
    assert path.read_bytes() == b"sentinel"
    assert not ExperimentStore.open(tmp_path / "search").is_complete()
    assert len(execution.calls) == (1 if stop == "training" else 0)
    if stop == "discovery":
        assert (
            cli.main(
                [
                    "tune",
                    "--resume",
                    str(tmp_path / "search"),
                    "--save",
                    str(path),
                    "--json",
                ]
            )
            == 1
        )
        resumed = json.loads(capsys.readouterr().out)
        metadata(resumed, status="skipped", reason="incomplete_experiment", requested=str(path))
        assert execution.calls == [] and path.read_bytes() == b"sentinel"
        # A later invocation can complete the same experiment without exporting;
        # the prior requested path/status must not carry over.
        configure()
        assert (
            cli.main(["tune", "--resume", str(tmp_path / "search"), "--budget", "100s", "--json"])
            == 0
        )
        report = json.loads(capsys.readouterr().out)
        assert report["outcome"] == "validated_improvement"
        metadata(report, status="not_requested")
        assert path.read_bytes() == b"sentinel"


@pytest.mark.parametrize("requested", [False, True])
def test_startup_budget_stop_keeps_kind_and_has_no_artifact(
    tmp_path, search_cli, capsys, requested
):
    args, configure, _, execution, _ = search_cli
    configure(caps=PERF_CAPS, load_cost=101)
    path = str(tmp_path / "recipe.json")
    assert (
        cli.main([*args, "--collectors", "perf-stat", *(["--save", path] if requested else [])])
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["kind"] == "simtune_tune_startup_result"
    assert report["outcome"] == "inconclusive" and report["completion_marker"] is None
    assert report["budget"]["launches_started"] == 0 and execution.calls == []
    metadata(
        report,
        status="skipped" if requested else "not_requested",
        reason="experiment_not_started" if requested else None,
        requested=path if requested else None,
    )
    assert not (tmp_path / "search").exists() and not Path(path).exists()


@pytest.mark.parametrize("requested", [False, True])
def test_dry_run_reports_no_export_and_writes_nothing(tmp_path, search_cli, capsys, requested):
    args, _, _, execution, _ = search_cli
    path = str(tmp_path / "recipe.json")
    assert cli.main([*args, "--dry-run", *(["--save", path] if requested else [])]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["kind"] == "simtune_tune_dry_run" and report["dry_run"]
    metadata(
        report,
        status="skipped" if requested else "not_requested",
        reason="dry_run" if requested else None,
        requested=path if requested else None,
    )
    assert not (tmp_path / "search").exists() and not Path(path).exists()
    assert execution.calls == []


def test_real_save_failure_emits_no_success(tmp_path, search_cli, capsys):
    args, _, _, _, _ = search_cli
    path = tmp_path / "recipe.json"
    path.write_bytes(b"existing")
    assert cli.main([*args, "--save", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "destination already exists" in captured.err
    assert "Recipe saved:" not in captured.err
    assert path.read_bytes() == b"existing"


def test_completed_saved_search_resume_refuses_stale_success(tmp_path, search_cli, capsys):
    args, _, _, _, _ = search_cli
    path = str(tmp_path / "recipe.json")
    assert cli.main([*args, "--save", path]) == 0
    metadata(json.loads(capsys.readouterr().out), status="saved", requested=path)
    before = Path(path).read_bytes()
    assert cli.main(["tune", "--resume", str(tmp_path / "search"), "--save", path, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "completed search is immutable" in captured.err
    assert Path(path).read_bytes() == before


def test_human_skip_keeps_explanation(tmp_path, search_cli, capsys):
    args, configure, _, _, host = search_cli
    configure(host=replace(host, platform=Platform.MACOS), caps={"platform": "darwin", "tools": {}})
    path = tmp_path / "recipe.json"
    assert cli.main([*args[:-1], "--save", str(path)]) == 1
    captured = capsys.readouterr()
    assert "Recipe not saved:" in captured.err
    assert "Reference retained: unsupported" in captured.out
    assert "Recipe saved:" not in captured.out and not path.exists()
