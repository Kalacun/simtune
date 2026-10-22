import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from examples.heat_benchmark import execute, paired_summary, read_workload_output, run_trial

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def heat_binary(tmp_path_factory):
    compiler = shutil.which("cc")
    if not compiler:
        pytest.skip("C compiler unavailable; only the optional example requires it")
    binary = tmp_path_factory.mktemp("heat-serial") / "heat2d"
    subprocess.run(
        [
            compiler,
            "-O2",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(ROOT / "examples/heat2d.c"),
            "-lm",
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return binary


def execute_heat(binary, *args, threads=1):
    return subprocess.run(
        [str(binary), *args],
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "OMP_NUM_THREADS": str(threads), "OMP_DYNAMIC": "FALSE"},
    )


@pytest.mark.parametrize("wave", [1, 2])
def test_heat_matches_independent_small_stencil(heat_binary, wave):
    n, steps = 7, 3
    values = [
        [
            math.sin(math.pi * wave * i / (n - 1)) * math.sin(math.pi * wave * j / (n - 1))
            if 0 < i < n - 1 and 0 < j < n - 1
            else 0.0
            for j in range(n)
        ]
        for i in range(n)
    ]
    for _ in range(steps):
        updated = [[0.0] * n for _ in range(n)]
        for i in range(1, n - 1):
            for j in range(1, n - 1):
                updated[i][j] = values[i][j] + 0.2 * (
                    values[i - 1][j]
                    + values[i + 1][j]
                    + values[i][j - 1]
                    + values[i][j + 1]
                    - 4 * values[i][j]
                )
        values = updated
    result = execute_heat(heat_binary, "--size", str(n), "--steps", str(steps), "--wave", str(wave))
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["valid"] is True and data["max_abs_error"] < 1e-12
    assert data["checksum"] == pytest.approx(sum(map(sum, values)), abs=1e-12)
    assert data["updates"] == (n - 2) ** 2 * steps
    assert data["grid_bytes"] == 16 * n * n
    assert data["threads"] == 1 and data["openmp"] == 0


@pytest.mark.parametrize(
    "args",
    [
        ["--size", "2"],
        ["--size", "999999999999999999999999"],
        ["--steps", "0"],
        ["--steps", "-2"],
        ["--wave", "5", "--size", "6"],
        ["--size"],
        ["--first-touch", "unknown"],
        ["--unknown", "1"],
        ["--size", "3.0"],
    ],
)
def test_heat_rejects_invalid_options(heat_binary, args):
    result = execute_heat(heat_binary, *args)
    assert result.returncode == 2 and result.stderr


def test_heat_openmp_thread_and_initialization_invariance(tmp_path):
    requested = os.environ.get("SIMTUNE_OPENMP_CC")
    compiler = requested or shutil.which("gcc") or shutil.which("cc")
    if not compiler:
        pytest.skip("OpenMP compiler unavailable")
    binary = tmp_path / "heat2d"
    result = subprocess.run(
        [
            compiler,
            "-O2",
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
    if result.returncode and not requested:
        pytest.skip("default compiler lacks OpenMP; set SIMTUNE_OPENMP_CC to require it")
    assert result.returncode == 0, result.stderr
    checksums = []
    for threads in (1, 2):
        for touch in ("serial", "parallel"):
            run = execute_heat(
                binary, "--size", "64", "--steps", "9", "--first-touch", touch, threads=threads
            )
            assert run.returncode == 0, run.stderr
            data = json.loads(run.stdout)
            assert data["valid"] is True and data["threads"] == threads and data["openmp"] > 0
            checksums.append(data["checksum"])
    assert all(value == pytest.approx(checksums[0], abs=1e-12) for value in checksums)


def trial(arm, pair=1, **overrides):
    return {
        "arm": arm,
        "pair": pair,
        "phase": "measurement",
        "eligible": True,
        "collectors": {"used_time": True, "used_perf": False},
        "wall_time_sec": 2 if arm == "baseline" else 1,
        **overrides,
    }


def test_summary_uses_paired_ratio_and_seeded_interval():
    trials = [trial(arm, pair=pair) for pair in range(1, 7) for arm in ("baseline", "candidate")]
    result = paired_summary(trials, 6, 123)
    assert result["status"] == "complete"
    assert result["observed_wall_ratio"] == pytest.approx(2)
    assert result["paired_bootstrap_95pct"] == pytest.approx([2, 2])
    assert result == paired_summary(trials, 6, 123)


@pytest.mark.parametrize(
    "replacement",
    [
        {"eligible": False},
        {"collectors": {"used_time": False}},
        {"wall_time_sec": float("nan")},
    ],
)
def test_summary_refuses_invalid_pair(replacement):
    result = paired_summary([trial("baseline"), trial("candidate", **replacement)], 1, 0)
    assert result["status"] == "invalid"
    assert result["observed_wall_ratio"] is None


def test_summary_refuses_missing_pairs_and_failed_warmup():
    assert paired_summary([trial("baseline")], 1, 0)["status"] == "invalid"
    result = paired_summary(
        [trial("baseline"), trial("candidate"), trial("candidate", phase="warmup", eligible=False)],
        1,
        0,
    )
    assert result["observed_wall_ratio"] is None


def test_output_validation_checks_actual_threads_and_finite_values(heat_binary):
    output = execute_heat(heat_binary, "--size", "8", "--steps", "2").stdout
    assert read_workload_output(output, {"threads": 1})["valid"]
    with pytest.raises(ValueError, match="threads"):
        read_workload_output(output, {"threads": 2})
    with pytest.raises(ValueError, match="exactly one"):
        read_workload_output(output + output, {})
    data = json.loads(output)
    data["checksum"] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        read_workload_output(json.dumps(data, separators=(",", ":")), {})


def test_paired_cli_workflow_and_no_overwrite(heat_binary, tmp_path):
    pytest.importorskip("yaml")
    destination = tmp_path / "evidence"
    command = [
        sys.executable,
        str(ROOT / "examples/heat_benchmark.py"),
        "--binary",
        str(heat_binary),
        "--profiles",
        str(ROOT / "profiles/heat-experiment.yaml"),
        "--mode",
        "inherited",
        "--size",
        "16",
        "--steps",
        "2",
        "--threads",
        "1",
        "--pairs",
        "1",
        "--warmups",
        "1",
        "--include-direct",
        "--out",
        str(destination),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads((destination / "summary.json").read_text())
    assert summary["status"] == "complete" and summary["eligible_pairs"] == 1
    assert summary["paired_bootstrap_95pct"] is None
    records = [json.loads(line) for line in (destination / "trials.jsonl").read_text().splitlines()]
    assert len(records) == 6 and all(record["eligible"] for record in records)
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["status"] == "complete" and len(manifest["binary_sha256"]) == 64
    before = (destination / "manifest.json").read_bytes()
    again = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert again.returncode == 2 and "new directory" in again.stderr
    assert (destination / "manifest.json").read_bytes() == before


def test_example_timeout_retains_output_and_reaps_child(monkeypatch):
    original_popen = subprocess.Popen
    started = []

    def ready_process(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        # Handshake before the timeout starts: no assumption about Python startup speed.
        # Avoid TextIOWrapper read-ahead: communicate() may read the descriptor
        # directly on Python 3.11/3.12 and lose a buffered second output line.
        handshake = bytearray()
        while not handshake.endswith(b"\n"):
            byte = os.read(process.stdout.fileno(), 1)
            assert byte, "child exited before the handshake"
            handshake.extend(byte)
        started.append(int(handshake))
        return process

    monkeypatch.setattr(subprocess, "Popen", ready_process)
    code, output, _ = execute(
        [
            sys.executable,
            "-c",
            "import os,time; print(os.getpid(), flush=True); print('started', flush=True); time.sleep(30)",
        ],
        os.environ.copy(),
        0.2,
    )
    assert code == 124 and "started" in output
    with pytest.raises(ProcessLookupError):
        os.kill(started[0], 0)


@pytest.mark.parametrize("problem", ["skipped", "adjusted", "fallback", "failed"])
def test_trial_rejects_policy_loss_and_failure(tmp_path, monkeypatch, problem):
    from argparse import Namespace

    from examples import heat_benchmark

    def fake_execute(command, env, timeout):
        folder = Path(command[command.index("--out") + 1]) / "run_fake"
        folder.mkdir(parents=True)
        (folder / "metrics.json").write_text(json.dumps({"iterations": [{"return_code": 0}]}))
        (folder / "meta.json").write_text(
            json.dumps({"mode": "wrong" if problem == "fallback" else "inherited"})
        )
        (folder / "applied_actions.json").write_text(
            json.dumps(
                {
                    "skipped": ["unavailable"] if problem == "skipped" else [],
                    "adjusted": ["different"] if problem == "adjusted" else [],
                }
            )
        )
        return (127 if problem == "failed" else 0), "diagnostic output", 0.1

    monkeypatch.setattr(heat_benchmark, "execute", fake_execute)
    args = Namespace(out=tmp_path, timeout=1, profiles=Path("unused.yaml"), mode="inherited")
    record = run_trial(args, "candidate", "measurement", 1, 1, ["unused"], {}, {})
    assert not record["eligible"] and record["error"]
    assert (tmp_path / record["path"] / "trial.json").is_file()


def test_invalid_candidate_stops_before_creating_evidence(heat_binary, tmp_path):
    pytest.importorskip("yaml")
    destination = tmp_path / "should-not-exist"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "examples/heat_benchmark.py"),
            "--binary",
            str(heat_binary),
            "--profiles",
            str(ROOT / "profiles/heat-experiment.yaml"),
            "--mode",
            "typo",
            "--out",
            str(destination),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2 and "unknown candidate" in result.stderr
    assert not destination.exists()
