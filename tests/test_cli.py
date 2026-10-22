import json
import sys
from pathlib import Path

import pytest

from simtune import cli, profiler


@pytest.mark.parametrize(
    "option,value",
    [
        ("--repeat", "0"),
        ("--repeat", "-2"),
        ("--repeat", "1.5"),
        ("--timeout", "0"),
        ("--timeout", "-1"),
        ("--timeout", "nan"),
        ("--timeout", "inf"),
        ("--timeout", "abc"),
    ],
)
def test_invalid_options(option, value):
    with pytest.raises(SystemExit) as exc:
        cli.main(["profile", "--out", "unused", option, value, "--", "echo"])
    assert exc.value.code == 2


def test_command_arguments_are_preserved():
    parsed = cli.build_parser().parse_args(
        [
            "profile",
            "--out",
            "runs",
            "--",
            "python",
            "-c",
            "print('a b')",
            "--repeat",
            "0",
        ]
    )
    assert cli._parse_cmd(parsed.cmd) == ["python", "-c", "print('a b')", "--repeat", "0"]


@pytest.mark.parametrize(
    "args",
    [
        ["profile", "--out", "unused", "--"],
        ["run", "--out", "unused", "--mode", "throughput"],
    ],
)
def test_missing_command(args, capsys):
    assert cli.main(args) == 2
    assert "No command" in capsys.readouterr().err


def test_unknown_mode_is_friendly(cli_host, capsys):
    assert cli.main(["run", "--out", "unused", "--mode", "typo", "--", "echo"]) == 2
    assert "unknown mode" in capsys.readouterr().err


def test_dry_auto_has_no_workload_or_output_directory(cli_host, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        profiler, "profile_command", lambda *a, **k: pytest.fail("executed workload")
    )
    out = tmp_path / "not-created"
    assert (
        cli.main(["run", "--out", str(out), "--mode", "auto", "--dry-run", "--", "echo", "a b"])
        == 0
    )
    assert not out.exists()
    output = capsys.readouterr().out
    assert "Provisional" in output
    assert "'a b'" in output


def test_baseline_failure_is_stored_and_propagated(cli_host, tmp_path):
    assert (
        cli.main(
            [
                "profile",
                "--out",
                str(tmp_path),
                "--repeat",
                "2",
                "--",
                sys.executable,
                "-c",
                "raise SystemExit(7)",
            ]
        )
        == 7
    )
    data = json.loads(next(tmp_path.glob("*/metrics.json")).read_text())
    assert data["aggregate"]["failed_count"] == 2
    assert "wall_time_sec_mean" not in data["aggregate"]


def test_auto_failed_baseline_does_not_tune(cli_host, tmp_path):
    assert (
        cli.main(
            [
                "run",
                "--out",
                str(tmp_path),
                "--mode",
                "auto",
                "--",
                sys.executable,
                "-c",
                "raise SystemExit(9)",
            ]
        )
        == 9
    )
    assert len(list(tmp_path.iterdir())) == 1
    assert next(tmp_path.iterdir()).name.endswith("baseline")


def test_auto_retains_successful_baseline(cli_host, tmp_path):
    assert (
        cli.main(
            [
                "run",
                "--out",
                str(tmp_path),
                "--mode",
                "auto",
                "--",
                sys.executable,
                "-c",
                "print('ok')",
            ]
        )
        == 0
    )
    paths = list(tmp_path.iterdir())
    assert len(paths) == 2
    tuned = next(p for p in paths if not p.name.endswith("baseline"))
    rationale = json.loads((tuned / "rationale.json").read_text())
    assert Path(rationale["baseline_run_dir"]).is_dir()
    assert rationale["baseline_metrics"]["return_code"] == 0


def test_invalid_compare_path_is_friendly(tmp_path, capsys):
    assert cli.main(["compare", str(tmp_path / "missing"), "--out", str(tmp_path)]) == 2
    assert "Traceback" not in capsys.readouterr().err
    assert not (tmp_path / "compare.json").exists()


def test_flags_require_auto(cli_host):
    assert (
        cli.main(["run", "--out", "unused", "--mode", "latency", "--respect-desktop", "--", "echo"])
        == 2
    )


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "simtune 0.1.0" in capsys.readouterr().out


def test_compare_invalid_json_identifies_file(tmp_path, capsys):
    (tmp_path / "meta.json").write_text("not json")
    assert cli.main(["compare", str(tmp_path)]) == 2
    error = capsys.readouterr().err
    assert str(tmp_path / "meta.json") in error and "invalid JSON" in error
