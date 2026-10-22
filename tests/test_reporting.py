"""Read-only R04 reports use synthetic timings, never performance claims."""

import io
import json
import math
from contextlib import redirect_stdout
from dataclasses import replace

import pytest
from test_recipes import build_recipe
from test_recipes import incomplete_snapshot as incomplete_snapshot
from test_recipes import recipe_snapshot as recipe_snapshot
from test_recipes import saved as saved
from test_search import CAPABILITIES, FakeClock, SyntheticExecution, execute, make_workload
from test_search_recovery import crash_after_terminal

from simtune import cli, tune
from simtune.lookup import show_run
from simtune.models import Phase
from simtune.reporting import (
    _measurement,
    decision_report,
    render_decision_report,
    time_reduction_percent,
)
from simtune.search import run_search
from simtune.statistics import PolicyScore


@pytest.mark.parametrize("ratio,expected", [(2, 50), (1, 0), (0.5, -100), (1.25, 20)])
def test_ratio_is_not_percent_ratio_increase(ratio, expected):
    assert time_reduction_percent(ratio) == pytest.approx(expected)


@pytest.mark.parametrize("ratio", [None, True, 0, -1, math.inf, math.nan])
def test_missing_invalid_ratios_remain_unknown(ratio):
    assert time_reduction_percent(ratio) is None


def test_missing_interval_stays_missing():
    score = PolicyScore("fixture", Phase.TRAINING, True, (), 2, None, None, (), 2, 1)
    result = _measurement(score)
    assert result["time_reduction_percent"] == 50
    assert result["ratio_interval_95"] is None
    assert result["time_reduction_interval_95_percent"] is None
    assert "ratio_interval_95" in result["unavailable"]


def test_report_gain_shared_show_explain_and_redirected_stdout(saved, monkeypatch):
    path, _, experiment = saved
    root = path.parent / "experiment"
    before = {p: p.read_bytes() for p in path.parent.rglob("*") if p.is_file()}
    monkeypatch.setattr(
        "simtune.profiler.execute_command", lambda *a, **k: pytest.fail("report launched")
    )
    monkeypatch.setattr("simtune.util.optional_import", lambda name: None)
    shown = show_run(root)["decision_report"]
    assert shown["outcome"] == experiment.outcome.value == "validated_improvement"
    row = next(
        r
        for r in shown["case_policy_rows"]
        if r["policy_id"] == "runtime-close" and r["phase"] == "confirmation"
    )
    assert row["complete_pair_count"] == 5
    assert row["checks"] == {"passed": 5}
    assert row["baseline_seconds"] == 2 and row["candidate_seconds"] == 1
    assert row["paired_geometric_ratio"] == 2
    assert row["time_reduction_percent"] == 50
    assert row["time_reduction_interval_95_percent"] == [50, 50]
    assert shown["cost"]["conditional_saved_seconds_per_run"] == 1
    assert shown["cost"]["break_even_runs"] == math.ceil(experiment.budget.spent_seconds)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert cli.main(["explain", str(path), "--json"]) == 0
    explained = json.loads(buffer.getvalue())["decision_report"]
    for key in ("case_policy_rows", "historical_validation", "cost", "settings"):
        assert explained[key] == shown[key]
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert cli.main(["show", str(root)]) == 0
        assert cli.main(["explain", str(path)]) == 0
    text = buffer.getvalue()
    for phrase in (
        "held-out confirmation",
        "training selection",
        "Time reduction %",
        "50",
        "Cost:",
        "Observed:",
        "untracked",
        "complete: True",
    ):
        assert phrase in text
    assert all(p.read_bytes() == data for p, data in before.items())


@pytest.mark.parametrize("scenario", ["no_gain", "unchecked", "failed", "partial", "unsupported"])
def test_nonwinning_reports_keep_reasons_and_missing_savings(
    tmp_path, recipe_snapshot, monkeypatch, scenario
):
    if scenario == "partial":
        root, _, execution, _ = crash_after_terminal(tmp_path, recipe_snapshot, monkeypatch)
        expected = "inconclusive"
    else:
        snapshot = recipe_snapshot
        if scenario == "unsupported":
            snapshot = replace(
                snapshot,
                inherited_cpu_ids=None,
                unavailable={"inherited_cpu_ids": "fixture observation absent"},
            )
        result, execution, _ = execute(
            tmp_path,
            snapshot,
            checked=scenario != "unchecked",
            times={"baseline": 2, "runtime-close": 2, "runtime-spread": 2}
            if scenario == "no_gain"
            else None,
            failed=("baseline",) if scenario == "failed" else (),
        )
        root = tmp_path / "search"
        expected = result.outcome.value
    calls = len(execution.calls)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    report = decision_report(root)
    assert report["outcome"] == expected
    assert report["cost"]["break_even_runs"] is None
    assert report["cost"]["conditional_saved_seconds_per_run"] is None
    assert report["cost"]["unavailable"]
    text = render_decision_report(report)
    assert "Reason:" in text
    if scenario == "failed":
        assert any(t["status"] == "failed" for t in report["trials"])
    if scenario in {"unchecked", "failed", "unsupported"}:
        assert all(
            r["paired_geometric_ratio"] is None and r["unavailable"]
            for r in report["case_policy_rows"]
        )
    if scenario == "partial":
        assert not report["complete"]
    assert len(execution.calls) == calls
    assert all(p.read_bytes() == data for p, data in before.items())
    assert not list(root.glob("*.recipe.json"))


def test_break_even_uses_weighted_arithmetic_evidence(tmp_path, recipe_snapshot):
    def change(spec):
        first = replace(spec.cases[1], weight=3)
        second = replace(first, id="second-heldout", weight=1, arguments={"input": "other-heldout"})
        return replace(
            spec, cases=[spec.cases[0], first, second], validation_case_ids=[first.id, second.id]
        )

    def timing(policy, case):
        baseline = 100 if case == "other-heldout" else 10
        ratio = 2 if case == "other-heldout" else 10
        return baseline / ratio if policy == "runtime-close" else baseline

    path, _, experiment = build_recipe(
        tmp_path, recipe_snapshot, times=timing, spec_transform=change
    )
    report = decision_report(path.parent / "experiment")
    aggregate = next(
        r
        for r in report["aggregates"]
        if r["policy_id"] == "runtime-close" and r["phase"] == "confirmation"
    )
    assert aggregate["baseline_seconds"] == 32.5
    assert aggregate["candidate_seconds"] == 13.25
    assert aggregate["paired_geometric_ratio"] == pytest.approx(10**0.75 * 2**0.25)
    assert report["cost"]["conditional_saved_seconds_per_run"] == 19.25
    assert report["cost"]["break_even_runs"] == math.ceil(experiment.budget.spent_seconds / 19.25)


def test_tune_json_uses_shared_report_and_warning_precedes_search(
    tmp_path, recipe_snapshot, monkeypatch, capsys
):
    root = tmp_path / "search"
    original = run_search
    warning = []

    def search(load, out, config, **kwargs):
        warning.append(capsys.readouterr().err)
        loaded = make_workload(tmp_path / "manifest", checked=False)
        clock = FakeClock()
        synthetic = SyntheticExecution(clock)
        return original(
            loaded,
            out,
            config,
            snapshot_provider=lambda: recipe_snapshot,
            capabilities=CAPABILITIES,
            clock=clock,
            executor_kwargs=synthetic.kwargs,
        )

    monkeypatch.setattr(tune, "run_search", search)
    import sys

    assert (
        cli.main(
            [
                "tune",
                "--json",
                "--budget",
                "10s",
                "--out",
                str(root),
                "--",
                sys.executable,
                "-c",
                "pass",
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert "unchecked" in warning[0] and "cannot select" in warning[0]
    assert "examples/HEAT_EXPERIMENT.md" in warning[0]
    assert report["decision_report"] == show_run(root)["decision_report"]
    assert all(
        row["paired_geometric_ratio"] is None
        for row in report["decision_report"]["case_policy_rows"]
    )


def test_legacy_run_and_absent_new_evidence_remain_inspectable(saved, tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "meta.json").write_text('{"schema_version":2}')
    (legacy / "metrics.json").write_text('{"aggregate":{"wall_time_sec":3}}')
    assert show_run(legacy)["metrics"]["aggregate"]["wall_time_sec"] == 3
    root = saved[0].parent / "experiment"
    (root / "resources.json").unlink()  # Missing evidence is diagnostic, never fabricated.
    report = show_run(root)["decision_report"]
    assert report["evidence_errors"] and not report["case_policy_rows"]
    assert report["hardware"]["class_id"] is None
