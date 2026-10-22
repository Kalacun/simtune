"""Project configuration, run lookup and new command output contracts."""

import json
import sys

import pytest

from simtune import cli
from simtune.lookup import find_run


def test_config_precedence_paths_and_effective_output(tmp_path, monkeypatch, capsys):
    project = tmp_path / "project with spaces"
    project.mkdir()
    (project / "simtune.toml").write_text(
        '[tune]\nworkload = "input workload.toml"\nbudget = "2m"\n'
        'out = "results/one"\nrepeat = 2\nlabel = "example"\n'
    )
    monkeypatch.chdir(project)
    assert cli.main(["tune", "--show-config", "--repeat", "4"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["tune"]["repeat"] == {"value": 4, "source": "CLI"}
    assert report["tune"]["budget"]["value"] == 120
    assert report["tune"]["workload"]["value"] == str(project / "input workload.toml")
    assert report["tune"]["out"]["value"] == str(project / "results/one")


def test_no_parent_config_discovery(tmp_path, monkeypatch, capsys):
    (tmp_path / "simtune.toml").write_text('[tune]\nbudget = "9m"\n')
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    assert cli.main(["tune", "--show-config"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["config_path"] is None
    assert report["tune"]["budget"]["value"] is None


@pytest.mark.parametrize(
    ("options", "value", "source"),
    [
        ([], "timing", "default"),
        (["--collectors", "timing"], "timing", "CLI"),
        (["--collectors", "perf-stat"], "perf-stat", "CLI"),
    ],
)
def test_show_config_collectors_without_discovery_or_launch(
    tmp_path, monkeypatch, capsys, options, value, source
):
    import subprocess

    from simtune import tune

    def forbidden(*args, **kwargs):
        pytest.fail("show-config must not discover capabilities or launch subprocesses")

    monkeypatch.chdir(tmp_path)
    (tmp_path / "simtune.toml").write_text("[tune]\nrepeat = 2\n")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(tune, "run_search", forbidden)
    monkeypatch.setattr(cli.capabilities, "detect_capabilities", forbidden)
    assert cli.main(["tune", *options, "--show-config"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["tune"]["collectors"] == {"value": value, "source": source}
    assert report["tune"]["repeat"]["source"] == str(tmp_path / "simtune.toml")
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize(
    "contents",
    [
        "[tune]\ntypo = 1\n",
        '[other]\nbudget = "1m"\n',
        "[tune]\nrepeat = true\n",
        '[tune]\nlabel = "../bad"\n',
        '[tune]\nbudget = "nonsense"\n',
        '[tune]\ncollectors = "perf-stat"\n',
    ],
)
def test_invalid_config_fails_before_launch(tmp_path, monkeypatch, contents, capsys):
    (tmp_path / "simtune.toml").write_text(contents)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["tune", "--budget", "1s", "--", sys.executable, "-c", "pass"]) == 2
    assert "simtune:" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()


def test_json_result_and_default_id(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    status = cli.main(
        [
            "tune",
            "--budget",
            "2s",
            "--json",
            "--label",
            "tiny",
            "--",
            sys.executable,
            "-c",
            "print('safe')",
        ]
    )
    assert status == 1  # local macOS search cannot claim Linux strict policies
    out = capsys.readouterr()
    result = json.loads(out.out)
    assert result["kind"] == "simtune_tune_result"
    assert result["id"].startswith("search-") and "-tiny-" in result["id"]
    assert result["path"].startswith(str(tmp_path / "runs"))
    assert result["budget"]["launches_started"] == 0
    assert result["recipe_path"] is None
    assert result["recipe_export"] == {
        "status": "not_requested",
        "reason": None,
        "requested_path": None,
    }
    assert "safe" not in out.out
    assert cli.main(["show", result["id"], "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["id"] == result["id"] and shown["complete"]


def test_ambiguous_and_exact_lookup(tmp_path):
    root = tmp_path / "runs"
    root.mkdir()
    for name in ("run_alpha", "run_alpine"):
        path = root / name
        path.mkdir()
        (path / "meta.json").write_text("{}")
    with pytest.raises(ValueError, match="ambiguous.*run_alpha.*run_alpine"):
        find_run("run_al", root)
    assert find_run(str(root / "run_alpha"), root) == (root / "run_alpha").resolve()


def test_dry_run_counts_and_literal_tail(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "never"
    assert (
        cli.main(
            [
                "tune",
                "--budget",
                "2s",
                "--out",
                str(out),
                "--dry-run",
                "--",
                sys.executable,
                "-c",
                "print(1)",
                "--json",
                "$HOME",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["launches_per_trial_by_case"]["input"]["workload"] == 1
    assert report["launches_per_trial_by_case"]["input"]["validator"] == 0
    assert not out.exists()
