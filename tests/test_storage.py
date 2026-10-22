import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from simtune import storage, util


def test_storage_writes_files(tmp_path: Path):
    run_dir = storage.create_run_dir(str(tmp_path), "test")
    storage.write_run(
        run_dir,
        meta={"mode": "test", "cmd": ["echo", "hello"], "timestamp": "now"},
        capabilities={"platform": "linux", "is_wsl": False, "tools": {}, "systemd": {}, "perf": {}},
        applied_actions={
            "requested": [],
            "applied": [],
            "skipped": [],
            "adjusted": [],
            "final_command": [],
        },
        metrics={"iterations": [], "aggregate": {}},
        raw_profiler="",
        cmd=["echo", "hello"],
        rationale={},
    )
    assert (run_dir / "meta.json").exists()
    assert (run_dir / "capabilities.json").exists()
    assert (run_dir / "applied_actions.json").exists()
    assert (run_dir / "metrics.json").exists()
    assert (run_dir / "raw_profiler.txt").exists()
    assert (run_dir / "command.txt").exists()
    assert (run_dir / "rationale.json").exists()


def test_concurrent_directory_allocation(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda _: storage.create_run_dir(str(tmp_path), "test"), range(32)))
    assert len(set(paths)) == 32


@pytest.mark.parametrize("mode", ["../escape", "/absolute", "", "a/b"])
def test_run_mode_cannot_escape_storage(tmp_path, mode):
    with pytest.raises(ValueError):
        storage.create_run_dir(str(tmp_path), mode)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "data",
    [
        [],
        {},
        {"iterations": {}, "aggregate": {}},
        {"iterations": [1], "aggregate": {}},
        {"iterations": [], "aggregate": {"x": float("nan")}},
    ],
)
def test_core_schema_validation_is_always_enabled(data, monkeypatch):
    monkeypatch.setattr(util, "optional_import", lambda name: None)
    with pytest.raises(ValueError):
        storage.validate_metrics(data)


def test_storage_metadata_and_exact_argv(tmp_path, caps, monkeypatch):
    monkeypatch.setattr(util, "run_git_info", lambda: {})
    path = storage.create_run_dir(str(tmp_path), "test")
    cmd = ["echo", "space here", "line\nbreak"]
    storage.write_run(
        path,
        {"mode": "test", "cmd": cmd},
        caps,
        {"requested": [], "applied": [], "skipped": [], "adjusted": [], "final_command": cmd},
        {"iterations": [], "aggregate": {}},
        "raw",
        cmd,
        {},
    )
    meta = json.loads((path / "meta.json").read_text())
    assert meta["schema_version"] == 2
    assert meta["cmd"] == cmd
    assert meta["cwd"] and meta["simtune_version"]


def test_incomplete_write_has_no_completion_metadata(tmp_path, caps, monkeypatch):
    path = storage.create_run_dir(str(tmp_path), "test")
    monkeypatch.setattr(util, "run_git_info", lambda: {})
    monkeypatch.setattr(util, "write_text", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        storage.write_run(
            path,
            {},
            caps,
            {"requested": [], "applied": [], "skipped": [], "adjusted": [], "final_command": []},
            {"iterations": [], "aggregate": {}},
            "",
            [],
            {},
        )
    assert not (path / "meta.json").exists()
