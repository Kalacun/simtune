import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from process_helpers import SIGNALS, cleanup, living, signal_parent, wait_for, worker_argv

from simtune import metrics, profiler, util


def test_successful_subprocess_and_binary_output(caps):
    result, raw = profiler.profile_command(
        [sys.executable, "-c", "import os; os.write(1,b'hi\\xff'); os.write(2,b'err\\xfe')"],
        util.default_env(),
        5,
        caps,
    )
    assert result["return_code"] == 0
    assert "hi\ufffd" in raw and "err\ufffd" in raw
    assert metrics.normalize_metrics(result)["max_rss_kb"] is None


def test_minimal_execution_has_no_optional_profiler_wrappers(caps):
    caps["perf"]["functional"] = True
    caps["tools"]["perf"] = {"path": "definitely-not-run"}
    caps["tools"]["time"] = {"path": "definitely-not-run", "functional": True}
    result, raw = profiler.execute_command(
        [sys.executable, "-c", "print('minimal')"], util.default_env(), 5
    )
    assert result["return_code"] == 0 and "minimal" in raw
    assert not result["profiler"]["used_time"] and not result["profiler"]["used_perf"]


def test_missing_executable_is_recorded(caps, tmp_path):
    result, raw = profiler.profile_command([str(tmp_path / "missing")], util.default_env(), 5, caps)
    assert result["return_code"] == 127
    assert "could not launch" in raw


def test_permission_and_preexec_failures_are_recorded(caps, monkeypatch):
    def fail(*a, **kw):
        raise subprocess.SubprocessError("Exception occurred in preexec_fn")

    monkeypatch.setattr(subprocess, "Popen", fail)
    result, raw = profiler.profile_command(["command"], util.default_env(), None, caps)
    assert result["return_code"] == 126 and "preexec_fn" in raw


def test_timeout_kills_inherited_process_group(caps, tmp_path):
    # Descendant holds stdout open after the parent exits. Killing only the
    # direct child would leave communicate hanging for the descendant's lifetime.
    marker = tmp_path / "escaped-work"
    child = f"import time; from pathlib import Path; time.sleep(2); Path({str(marker)!r}).touch()"
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child!r}]); print('started', flush=True)"
    start = time.monotonic()
    result, raw = profiler.profile_command(
        [sys.executable, "-c", parent], util.default_env(), 0.5, caps
    )
    assert result["timed_out"] and result["return_code"] == 124
    assert "started" in raw
    assert time.monotonic() - start < 4
    assert not marker.exists()


def test_failed_perf_never_reexecutes_workload(caps, monkeypatch):
    caps["perf"]["functional"] = True
    caps["tools"]["perf"] = {"path": "perf"}
    calls = []
    usage = profiler._rusage_snapshot()

    def fail(command, *args):
        calls.append(command)
        return 1, "STDERR: permission denied", False, 0.1, usage, usage, None, {}

    monkeypatch.setattr(profiler, "_run_command", fail)
    result, raw = profiler.profile_command(["workload"], {}, 5, caps)
    assert len(calls) == 1
    assert result["return_code"] == 1 and not result["perf_fallback"]
    assert "permission denied" in raw


def test_workload_stderr_cannot_spoof_time_statistics(caps, monkeypatch):
    caps["tools"]["time"] = {"path": "time", "functional": True}
    usage = profiler._rusage_snapshot()

    def run(command, *args):
        Path(command[command.index("-o") + 1]).write_text("simtune_user=1.5\nsimtune_rss=2048\n")
        return 0, "STDERR: simtune_rss=999999", False, 2, usage, usage, None, {}

    monkeypatch.setattr(profiler, "_run_command", run)
    result, _ = profiler.profile_command(["workload"], {}, 5, caps)
    assert result["time_tool"]["max_rss_kb"] == 2048


def test_time_parsers_handle_units_and_malformed_values():
    assert (
        profiler._parse_time_macos("2.0 real 1.5 user 0.1 sys\n 1048576 maximum resident set size")[
            "max_rss_kb"
        ]
        == 1024
    )
    assert profiler._parse_time_linux(
        "simtune_user=1,25\nsimtune_rss=invalid\nsimtune_sys=nan"
    ) == {"user_time_sec": 1.25}
    assert profiler._parse_time_macos("bad real bad user bad sys") == {}


def test_perf_unsupported_and_localized_values():
    parsed = profiler._parse_perf_csv(
        "<not supported>;;cycles\n1,25;msec;task-clock\n100,,instructions\nnan;;cycles"
    )
    assert parsed == {
        "task-clock": {"value": 1.25, "unit": "msec"},
        "instructions": {"value": 100, "unit": ""},
    }


def test_rusage_does_not_report_lifetime_peak_as_run_peak():
    names = [
        "utime",
        "stime",
        "maxrss",
        "minflt",
        "majflt",
        "inblock",
        "oublock",
        "nvcsw",
        "nivcsw",
    ]
    start = SimpleNamespace(**{f"ru_{name}": 10 for name in names})
    end = SimpleNamespace(**{f"ru_{name}": 20 for name in names})
    delta = profiler._rusage_delta(start, end)
    assert delta["max_rss_kb"] is None
    assert delta["user_time_sec"] == 10


def test_disabled_tools_are_not_wrapped(caps):
    caps["tools"]["time"] = {"path": "time", "functional": False}
    command, meta = profiler._build_profiler_command(["workload"], caps, "time.txt", "perf.txt")
    assert command == ["workload"] and not meta["used_time"]


# Deterministic exit races complement the actual POSIX process tests below.


@pytest.mark.parametrize("boundary", ["forward", "probe"])
def test_absent_group_observation_is_not_overwritten_by_another_signal(monkeypatch, boundary):
    calls = []
    absent_signal = signal.SIGINT if boundary == "forward" else 0

    def killpg(pid, signum):
        calls.append(signum)
        if signum == absent_signal:
            raise ProcessLookupError("owned group has disappeared")
        if boundary == "forward" or signum == signal.SIGKILL:
            raise PermissionError("a redundant signal cannot invalidate observed absence")

    monkeypatch.setattr(os, "killpg", killpg)
    assert profiler._teardown(SimpleNamespace(pid=123, poll=lambda: 0), signal.SIGINT)
    assert calls == ([signal.SIGINT] if boundary == "forward" else [signal.SIGINT, 0])


@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGKILL])
def test_final_group_signal_error_is_rechecked_with_a_bound(monkeypatch, persistent, signum):
    now = [0.0]
    attempts = []

    def killpg(pid, sig):
        if sig == signal.SIGKILL:
            attempts.append(now[0])
            if persistent or len(attempts) == 1:
                raise PermissionError("group signal not established")
            raise ProcessLookupError("group no longer exists")

    monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setattr(profiler.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        profiler.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )
    # A reaped leader alone is insufficient: persistent group errors stay unknown.
    assert profiler._teardown(SimpleNamespace(pid=123, poll=lambda: 0), signum) is (not persistent)
    assert len(attempts) >= 2
    assert now[0] <= profiler._CANCEL_GRACE_SECONDS + 1.01


@pytest.mark.parametrize("sig", SIGNALS)
@pytest.mark.parametrize("mode", ["cooperative", "ignoring", "grandchild"])
def test_captured_parent_cancellation_reaps_owned_group(tmp_path, sig, mode):
    argv = worker_argv(tmp_path, mode)
    driver = f"""import json,os,signal,sys
from pathlib import Path
from simtune import profiler,util
previous={{sig:signal.getsignal(sig) for sig in ({int(signal.SIGINT)},15,1)}}
result,raw=profiler.execute_command({argv!r},dict(os.environ),20)
assert all(signal.getsignal(sig)==handler for sig,handler in previous.items())
Path({str(tmp_path / "result.json")!r}).write_text(json.dumps(result))
Path({str(tmp_path / "raw.txt")!r}).write_text(raw)
code=result['return_code']; sys.exit(128-code if code<0 else code)
"""
    status, out, err, ready = signal_parent(tmp_path, driver, sig, repeated=True)
    assert status == 128 + sig, (out, err)
    assert not living(ready["pid"])
    descendant = tmp_path / "descendant.json"
    if descendant.exists():
        assert not living(json.loads(descendant.read_text())["pid"])
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["return_code"] == -sig
    assert result["cancellation"]["parent_signal"] == sig
    assert result["cancellation"]["termination_known"] is True
    assert result["cancellation"]["workload_return_code"] == (0 if mode == "cooperative" else -9)
    assert "partial-output" in (tmp_path / "raw.txt").read_text()
    assert "partial-error" in (tmp_path / "raw.txt").read_text()
    assert not (tmp_path / "completion").exists()
    assert not (tmp_path / "descendant-completion").exists()
    assert (tmp_path / "launches").read_text().splitlines().count("launch") == (
        2 if mode == "grandchild" else 1
    )


def test_escaped_session_pipe_drain_is_bounded_and_explicit(tmp_path):
    driver = f"""import json,os,sys
from pathlib import Path
from simtune.profiler import execute_command
result,raw=execute_command({worker_argv(tmp_path, "escaped")!r},dict(os.environ),20)
Path({str(tmp_path / "result.json")!r}).write_text(json.dumps(result))
Path({str(tmp_path / "raw.txt")!r}).write_text(raw)
sys.exit(128-result['return_code'])
"""
    status, out, err, ready = signal_parent(tmp_path, driver, signal.SIGTERM)
    assert ready["escaped_alive_before_cleanup"] is True
    assert status == 143, (out, err)
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["cancellation"]["output_complete"] is False
    assert "partial-output" in (tmp_path / "raw.txt").read_text()
    # signal_parent finally explicitly kills the escaped test session too.
    assert not living(json.loads((tmp_path / "descendant.json").read_text())["pid"])


@pytest.mark.parametrize("boundary", ["before_spawn", "after_spawn", "after_exit", "failed_spawn"])
def test_cancellation_spawn_exit_races_restore_handlers(tmp_path, monkeypatch, boundary):
    previous = {sig: signal.getsignal(sig) for sig in SIGNALS}
    original = subprocess.Popen
    spawned = []

    def popen(*args, **kwargs):
        if boundary in {"before_spawn", "failed_spawn"}:
            os.kill(os.getpid(), signal.SIGTERM)
        if boundary == "failed_spawn":
            raise FileNotFoundError("synthetic failed spawn")
        proc = original(*args, **kwargs)
        spawned.append(proc)
        if boundary == "after_spawn":
            wait_for(lambda: (tmp_path / "ready.json").exists(), proc)
            os.kill(os.getpid(), signal.SIGTERM)
        if boundary == "after_exit":
            communicate = proc.communicate

            def completed(*a, **kw):
                result = communicate(*a, **kw)
                os.kill(os.getpid(), signal.SIGTERM)
                return result

            proc.communicate = completed
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen)
    argv = (
        [sys.executable, "-c", "print('finished',flush=True)"]
        if boundary == "after_exit"
        else worker_argv(tmp_path)
    )
    try:
        result, raw = profiler.execute_command(argv, util.default_env(), 5)
        assert result["return_code"] == -signal.SIGTERM
        assert result["cancellation"]["parent_signal"] == signal.SIGTERM
        if boundary == "after_exit":
            assert result["cancellation"]["workload_return_code"] == 0
            assert "finished" in raw
        assert all(signal.getsignal(sig) == handler for sig, handler in previous.items())
    finally:
        monkeypatch.setattr(subprocess, "Popen", original)
        for proc in spawned:
            cleanup(proc, tmp_path)


def test_handlers_restored_on_unexpected_wait_exception(tmp_path, monkeypatch):
    previous = {sig: signal.getsignal(sig) for sig in SIGNALS}
    original = subprocess.Popen
    spawned = []

    def popen(*a, **kw):
        proc = original(*a, **kw)
        spawned.append(proc)
        communicate = proc.communicate
        calls = 0

        def fail_once(*a, **kw):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("unexpected wait error")
            return communicate(*a, **kw)

        proc.communicate = fail_once
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen)
    try:
        with pytest.raises(RuntimeError, match="unexpected wait error"):
            profiler.execute_command(worker_argv(tmp_path), util.default_env(), 5)
        assert all(signal.getsignal(sig) == handler for sig, handler in previous.items())
    finally:
        monkeypatch.setattr(subprocess, "Popen", original)
        for proc in spawned:
            cleanup(proc, tmp_path)


def test_preexisting_cancellation_launches_nothing_and_restores_handlers(monkeypatch):
    previous = {sig: signal.getsignal(sig) for sig in SIGNALS}

    def forbidden(*a, **kw):
        pytest.fail("cancelled scope launched another command")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with util.cancellation_scope():
        os.kill(os.getpid(), signal.SIGHUP)
        result, _ = profiler.execute_command(["unused"], util.default_env(), 5)
        assert result["return_code"] == -signal.SIGHUP
        assert result["cancellation"]["launch_attempted"] is False
        assert result["cancellation"]["workload_return_code"] is None
    assert all(signal.getsignal(sig) == handler for sig, handler in previous.items())


def test_partial_handler_registration_failure_restores_prior_handlers(monkeypatch):
    previous = {sig: signal.getsignal(sig) for sig in SIGNALS}
    original = signal.signal

    def fail(signum, handler):
        if signum == signal.SIGTERM and handler not in previous.values():
            raise RuntimeError("handler registration failed")
        return original(signum, handler)

    monkeypatch.setattr(signal, "signal", fail)
    with pytest.raises(RuntimeError, match="registration failed"):
        profiler.execute_command(["unused"], {}, 5)
    assert all(signal.getsignal(sig) == handler for sig, handler in previous.items())
    assert util._active_cancellation is None


def test_ready_timeout_reaps_ignoring_grandchild_and_preserves_output(tmp_path, monkeypatch):
    previous = {sig: signal.getsignal(sig) for sig in SIGNALS}
    original = subprocess.Popen
    spawned = []

    def ready_popen(*a, **kw):
        proc = original(*a, **kw)
        spawned.append(proc)
        wait_for(lambda: (tmp_path / "ready.json").exists(), proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", ready_popen)
    try:
        result, raw = profiler.execute_command(
            worker_argv(tmp_path, "grandchild"), util.default_env(), 0.1
        )
        assert result["return_code"] == 124 and result["timed_out"] is True
        assert result["cancellation"]["parent_signal"] is None
        assert result["cancellation"]["termination_known"] is True
        assert "partial-output" in raw and "partial-error" in raw
        monkeypatch.setattr(subprocess, "Popen", original)
        assert not living(json.loads((tmp_path / "ready.json").read_text())["pid"])
        assert not living(json.loads((tmp_path / "descendant.json").read_text())["pid"])
        assert all(signal.getsignal(sig) == handler for sig, handler in previous.items())
    finally:
        monkeypatch.setattr(subprocess, "Popen", original)
        for proc in spawned:
            cleanup(proc, tmp_path)
    assert not living(json.loads((tmp_path / "descendant.json").read_text())["pid"])
