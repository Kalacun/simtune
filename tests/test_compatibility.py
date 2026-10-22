"""Frozen compatibility baseline for the five existing commands and run schemas."""

import argparse
import json
import sys
from pathlib import Path

import pytest
from process_helpers import SIGNALS, signal_parent, worker_argv

from simtune import cli, compare, metrics, profiler, profiles, runner, util

FIXTURES = Path(__file__).parent / "fixtures"


def test_current_cli_help_is_frozen(monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    parser = cli.build_parser()
    actual = {"simtune": parser.format_help()}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            actual.update({name: sub.format_help() for name, sub in action.choices.items()})
    expected = json.loads((FIXTURES / "compatibility/cli-help.json").read_text())
    # Python 3.11–3.12 wrap the REMAINDER ellipsis onto its own line when the
    # additive command list reaches this width. Preserve all actual help text.
    actual["simtune"] = actual["simtune"].replace("}\n               ...", "} ...")
    assert actual == expected
    for command in (
        "slurm-plan",
        "ensemble",
    ):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args([command])
        assert exc.value.code == 2


def test_defaults_and_literal_argv():
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "run",
            "--mode",
            "auto",
            "--out",
            "out",
            "--",
            "prog",
            "α",
            "$HOME",
            "line\nbreak",
            "--repeat",
            "99",
        ]
    )
    assert args.repeat == 1 and args.timeout is None
    assert not args.dry_run and not args.notify
    assert args.profiles is None
    assert cli._parse_cmd(args.cmd) == ["prog", "α", "$HOME", "line\nbreak", "--repeat", "99"]
    assert parser.parse_args(["compare", "a"]).out == "."


def test_auto_n_plus_one_and_seven_file_schema(cli_host, tmp_path):
    marker = tmp_path / "launch-count"
    code = "from pathlib import Path; import sys; p=Path(sys.argv[1]); p.open('a').write('one\\n')"
    out = tmp_path / "runs"
    argv = [sys.executable, "-c", code, str(marker), "literal $HOME", "α\nβ"]
    assert cli.main(["run", "--mode", "auto", "--out", str(out), "--repeat", "3", "--", *argv]) == 0
    assert marker.read_text().splitlines() == ["one"] * 4
    runs = list(out.iterdir())
    assert len(runs) == 2
    for folder in runs:
        assert {p.name for p in folder.iterdir()} == {
            "meta.json",
            "capabilities.json",
            "applied_actions.json",
            "metrics.json",
            "raw_profiler.txt",
            "command.txt",
            "rationale.json",
        }
        meta = json.loads((folder / "meta.json").read_text())
        assert meta["schema_version"] == 2 and meta["cmd"] == argv
        assert "kind" not in meta
        data = json.loads((folder / "metrics.json").read_text())
        assert len(data["iterations"]) == (1 if meta["mode"] == "baseline" else 3)
        actions = json.loads((folder / "applied_actions.json").read_text())
        assert "not independently verified" in actions["note"]


def test_manual_required_settings_remain_best_effort(caps):
    data = profiles.load_profiles()
    mode, profile, reasons = runner.select_profile(data, "throughput", caps)
    assert mode == "desktop_friendly" and reasons
    assert runner.build_action_plan(profile, caps, False)["skipped"]
    caps["tools"]["taskset"] = {"path": "taskset", "available": True}
    plan = runner.build_action_plan({"actions": [{"type": "taskset", "cpus": "0-1"}]}, caps, False)
    assert not plan["applied"] and not plan["wrappers"]
    assert plan["skipped"][0]["reason"] == "no requested CPUs are known to be available"


@pytest.mark.parametrize(
    "sample,expected",
    [
        ({"return_code": 0}, 0),
        ({"return_code": 7}, 7),
        ({"return_code": -15}, 143),
        ({"return_code": -2}, 130),
        ({"return_code": 1, "timed_out": True}, 124),
    ],
)
def test_existing_exit_mapping(sample, expected):
    assert metrics.exit_status([sample]) == expected


def test_cli_timeout_and_interrupt_status(cli_host, tmp_path, monkeypatch):
    monkeypatch.setattr(
        profiler,
        "profile_command",
        lambda *a, **kw: ({"return_code": 124, "timed_out": True}, "synthetic timeout"),
    )
    assert cli.main(["profile", "--out", str(tmp_path / "timeout"), "--", "unused"]) == 124

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(profiler, "profile_command", interrupt)
    assert cli.main(["profile", "--out", str(tmp_path / "interrupted"), "--", "unused"]) == 130
    assert not list((tmp_path / "interrupted").glob("*/meta.json"))


def test_schema2_and_legacy_read_without_migration(tmp_path):
    folders = [FIXTURES / "compatibility/schema2", FIXTURES / "legacy_taskset_failure"]
    before = {p: p.read_bytes() for folder in folders for p in folder.iterdir()}
    _, rows = compare.compare_runs([str(p) for p in folders], tmp_path)
    assert rows[0]["status"] == "failed" and rows[0]["wall_time_sec_mean"] == 2
    assert rows[1]["status"] == "failed" and rows[1]["wall_time_sec_mean"] is None
    assert {p: p.read_bytes() for p in before} == before


def test_corrupt_or_incomplete_legacy_directory_is_not_success(tmp_path):
    util.write_json(tmp_path / "metrics.json", {"iterations": [], "aggregate": {}})
    with pytest.raises(FileNotFoundError):
        compare._load_run(str(tmp_path))
    (tmp_path / "meta.json").write_text('{"schema_version":')
    with pytest.raises(ValueError, match="invalid JSON"):
        compare._load_run(str(tmp_path))


@pytest.mark.parametrize("sig", SIGNALS)
def test_actual_captured_cli_parent_signal_status_and_repeat_stop(tmp_path, sig):
    driver = f"""from simtune import cli,capabilities
capabilities.detect_capabilities=lambda: {{'platform':'macos','is_wsl':False,'tools':{{}}}}
raise SystemExit(cli.main(['profile','--repeat','3','--out',{str(tmp_path / "runs")!r},'--',
    *{worker_argv(tmp_path, "ignoring")!r}]))
"""
    status, out, err, _ = signal_parent(tmp_path, driver, sig)
    assert status == 128 + sig, (out, err)
    assert (tmp_path / "launches").read_text() == "launch\n"
    paths = list((tmp_path / "runs").glob("*/metrics.json"))
    assert len(paths) == 1
    result = json.loads(paths[0].read_text())
    assert len(result["iterations"]) == 1
    assert result["iterations"][0]["cancellation"]["parent_signal"] == sig
    assert "partial-output" in (paths[0].parent / "raw_profiler.txt").read_text()
