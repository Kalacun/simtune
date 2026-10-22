"""Public command gates, also copied outside checkout for installed-wheel CI.

Recipe hosts/timings in this module are synthetic; no native acceptance claim.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from test_recipes import build_recipe
from test_recipes import recipe_snapshot as _recipe_snapshot
from test_search import CAPABILITIES, snapshot  # noqa: F401

import simtune
from simtune import cli
from simtune.replay import exec_recipe

incomplete_snapshot = snapshot
recipe_snapshot = _recipe_snapshot


def test_required_installed_import():
    if os.environ.get("SIMTUNE_REQUIRE_INSTALLED"):
        assert "site-packages" in Path(simtune.__file__).parts
        assert Path.cwd() not in Path(simtune.__file__).parents


def public_command(*argv, cwd):
    # This executes the console entry point in the same interpreter's bin directory.
    executable = Path(sys.executable).parent / "simtune"
    return subprocess.run(
        [str(executable), *argv],
        cwd=cwd,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_public_tune_dry_json_show(tmp_path):
    command = [sys.executable, "-c", "print('harmless')", "$HOME", "a literal argument"]
    root = tmp_path / "experiment"
    args = ["tune", "--budget", "3s", "--out", str(root), "--json"]
    dry = public_command(*args, "--dry-run", "--", *command, cwd=tmp_path)
    assert dry.returncode == 0, dry.stderr
    assert isinstance(json.loads(dry.stdout), dict) and not root.exists()
    result = public_command(*args, "--", *command, cwd=tmp_path)
    # Bare inputs are unchecked: Linux may run; unsupported hosts launch nothing.
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report["kind"] == "simtune_tune_result"
    assert report["outcome"] in ("inconclusive", "failed")
    assert report["recipe_path"] is None
    assert report["recipe_export"]["status"] == "not_requested"
    shown = public_command("show", str(root), "--json", cwd=tmp_path)
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["kind"] == "simtune_show"


def test_synthetic_recipe_export_load_replay_commands(
    tmp_path, recipe_snapshot, monkeypatch, capsys
):
    path, workload, _ = build_recipe(
        tmp_path,
        recipe_snapshot,
        times={"baseline": 2.0, "runtime-close": 2.0, "runtime-spread": 2.0},
    )
    # Training no-improvement exports an unvalidated reference, with no invented gain.
    assert cli.main(["explain", str(path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["label"] == "unvalidated"
    from simtune import replay

    monkeypatch.setattr(replay, "search_snapshot", lambda: recipe_snapshot)
    monkeypatch.setattr(replay, "search_capabilities", lambda _: CAPABILITIES)
    argv = [*workload.spec.argv_template]
    argv[1:] = ["training-data", str(tmp_path)]
    args = ["exec", "--recipe", str(path), "--cwd", str(tmp_path), "--dry-run"]
    assert cli.main([*args, "--", *argv]) == 2
    assert "allow-unvalidated" in capsys.readouterr().err
    assert cli.main([*args, "--allow-unvalidated", "--", *argv]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["launches"] == 0 and dry["current_output_validation"] == "unchecked"
    # Acknowledgment retains mechanical cwd and artifact boundaries.
    assert cli.main([*args, "--allow-unvalidated", "--", *argv[:-1], str(tmp_path / "wrong")]) == 2
    capsys.readouterr()
    result = exec_recipe(
        path,
        argv,
        allow_unvalidated=True,
        cwd=tmp_path,
        snapshot_provider=lambda: recipe_snapshot,
        capabilities=CAPABILITIES,
        out=tmp_path / "replay",
    )
    assert result.status == 0 and result.record["launches"] == 1
    assert result.record["current_output_validation"] == "unchecked"
    assert (tmp_path / "launches").read_text().splitlines() == ["launch"]
