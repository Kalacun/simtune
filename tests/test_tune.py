"""Additive measured-search CLI and harmless dry-run tests."""

import json
import sys

import pytest

from simtune import cli
from simtune.models import Outcome
from simtune.search import SearchConfig, run_search
from simtune.tune import bare_workload
from simtune.workloads import expand_trial_argv, load_workload


def test_bare_command_is_literal_unchecked_and_loadable(tmp_path):
    argv = [
        sys.executable,
        "-c",
        'print({"science": 1})',
        "{trial_dir}",
        "{case.input}",
        "$HOME",
        "α\nβ",
    ]
    workload = bare_workload(argv, timeout=2, max_trials=10, max_launches=20)
    assert workload.spec.argv_template[1:] == argv[1:]
    assert not workload.spec.output_checks and not workload.spec.validation_case_ids
    expanded = expand_trial_argv(workload.spec, workload.spec.cases[0], tmp_path)
    assert expanded[1:] == argv[1:]
    manifest = tmp_path / "workload.json"
    manifest.write_text(workload.spec.to_json())
    assert load_workload(manifest).workload_fingerprint == workload.workload_fingerprint


def test_tune_dryrun_launches_nothing_and_creates_no_directory(tmp_path, monkeypatch, capsys):
    def forbidden(*a, **k):
        pytest.fail("dry run launched a subprocess")

    monkeypatch.setattr("simtune.profiler.execute_command", forbidden)
    monkeypatch.setattr("simtune.profiler.profile_command", forbidden)
    monkeypatch.setattr("simtune.util.run_command", forbidden)
    out = tmp_path / "absent"
    assert (
        cli.main(
            [
                "tune",
                "--budget",
                "1m",
                "--out",
                str(out),
                "--dry-run",
                "--",
                sys.executable,
                "-c",
                'print("hello")',
            ]
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    assert data["dry_run"] and not out.exists()
    assert data["state"]["label"].startswith("input-specific")
    assert all(item["exclusion_reason"] for item in data["candidates"][1:]) or data["plans"]


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--budget", "nan"],
        ["--budget", "0s"],
        ["--budget", "1s", "--repeat", "0"],
        ["--budget", "1s", "--confirmation-pairs", "4"],
        ["--budget", "1s", "--minimum-improvement", "1"],
        ["--budget", "1s", "--max-launches", "0"],
    ],
)
def test_invalid_tune_options(extra, tmp_path):
    argv = ["tune", "--out", str(tmp_path / "absent"), *extra, "--", sys.executable, "-c", "pass"]
    try:
        result = cli.main(argv)
    except SystemExit as exc:
        result = exc.code
    assert result == 2
    assert not (tmp_path / "absent").exists()


def test_search_native_unsupported_remains_honest(tmp_path):
    from simtune.models import Platform
    from simtune.search import search_snapshot

    snapshot = search_snapshot()
    if snapshot.platform == Platform.LINUX:
        pytest.skip("local unsupported-platform case applies to non-Linux hosts")
    workload = bare_workload(
        [sys.executable, "-c", "print(1)"], timeout=1, max_trials=10, max_launches=20
    )
    result = run_search(
        workload, tmp_path / "unsupported", SearchConfig(2), snapshot_provider=lambda: snapshot
    )
    assert result.outcome == Outcome.INCONCLUSIVE
    assert result.selected_policy_id is None
    assert result.budget.launches_started == 0
    assert all(candidate.exclusion_reason for candidate in result.candidates)
    assert (tmp_path / "unsupported/completion.json").is_file()


def test_resume_rejects_new_workload_or_dryrun(tmp_path):
    for extra in (
        ["--workload", "x.toml"],
        ["--dry-run"],
        ["--out", "x"],
        ["--repeat", "3"],
        ["--collectors", "timing"],
    ):
        assert cli.main(["tune", "--resume", str(tmp_path), *extra]) == 2


def test_bare_executable_name_with_literal_braces(tmp_path):
    program = tmp_path / "program {literal} α"
    program.write_text("#!/usr/bin/env python3\nprint(1)\n")
    program.chmod(0o755)
    workload = bare_workload([str(program)], timeout=1, max_trials=2, max_launches=2)
    assert workload.spec.argv_template == [str(program)]
