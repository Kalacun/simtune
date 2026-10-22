"""Profile one command exactly once, retaining failures and profiler output."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    import resource
except ImportError:
    resource = None

from . import util


def _rusage_snapshot() -> Any:
    if resource is None:
        raise ValueError("profiling requires the POSIX resource module")
    return resource.getrusage(resource.RUSAGE_CHILDREN)


def _rusage_delta(start: Any, end: Any) -> dict[str, float | None]:
    # ru_maxrss is a lifetime high-water mark, not an additive counter. Taking
    # a delta (or the ending value) cannot recover a single run's peak memory.
    return {
        "user_time_sec": end.ru_utime - start.ru_utime,
        "sys_time_sec": end.ru_stime - start.ru_stime,
        "max_rss_kb": None,
        "minor_faults": end.ru_minflt - start.ru_minflt,
        "major_faults": end.ru_majflt - start.ru_majflt,
        "in_block": end.ru_inblock - start.ru_inblock,
        "out_block": end.ru_oublock - start.ru_oublock,
        "voluntary_ctx": end.ru_nvcsw - start.ru_nvcsw,
        "involuntary_ctx": end.ru_nivcsw - start.ru_nivcsw,
    }


def _number(text: str) -> float | None:
    try:
        value = float(text.strip().replace(",", "."))
        return value if math.isfinite(value) else None
    except ValueError:
        return None


def _parse_time_linux(text: str) -> dict[str, float]:
    metrics = {}
    labels = {
        "simtune_user=": "user_time_sec",
        "simtune_sys=": "sys_time_sec",
        "simtune_rss=": "max_rss_kb",
        # Retain support for legacy raw GNU time output.
        "User time (seconds):": "user_time_sec",
        "System time (seconds):": "sys_time_sec",
        "Maximum resident set size (kbytes):": "max_rss_kb",
    }
    for line in text.splitlines():
        for label, key in labels.items():
            if line.strip().startswith(label):
                value = _number(line.strip()[len(label) :])
                if value is not None:
                    metrics[key] = value
    return metrics


def _parse_time_macos(text: str) -> dict[str, float]:
    metrics = {}
    for line in text.splitlines():
        parts = line.split()
        if line.strip().endswith("maximum resident set size") and parts:
            value = _number(parts[0])
            if value is not None:
                metrics["max_rss_kb"] = value / 1024.0
        if len(parts) == 6 and parts[1::2] == ["real", "user", "sys"]:
            for key, token in (("user_time_sec", parts[2]), ("sys_time_sec", parts[4])):
                value = _number(token)
                if value is not None:
                    metrics[key] = value
    return metrics


def _parse_perf_csv(text: str) -> dict[str, Any]:
    metrics = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [item.strip() for item in line.split(";" if ";" in line else ",")]
        if len(parts) < 3:
            continue
        value = _number(parts[0])
        if value is not None and parts[2]:
            metrics[parts[2]] = {"value": value, "unit": parts[1]}
    return metrics


def _build_profiler_command(
    cmd: list[str],
    capabilities: dict[str, Any],
    time_output: str,
    perf_output: str,
) -> tuple[list[str], dict[str, Any]]:
    platform_name = capabilities.get("platform")
    tools = capabilities.get("tools", {})
    command = list(cmd)
    meta: dict[str, Any] = {"used_time": False, "used_perf": False, "platform": platform_name}
    perf_path = tools.get("perf", {}).get("path")
    if platform_name == "linux" and capabilities.get("perf", {}).get("functional") and perf_path:
        command = [perf_path, "stat", "-x;", "-o", perf_output, "--", *command]
        meta["used_perf"] = True
    time_tool = tools.get("time", {})
    time_path = time_tool.get("path")
    if time_path and time_tool.get("functional", True) and platform_name in {"linux", "macos"}:
        flags = (
            ["-f", "simtune_user=%U\nsimtune_sys=%S\nsimtune_rss=%M"]
            if platform_name == "linux"
            else ["-l"]
        )
        command = [time_path, *flags, "-o", time_output, "--", *command]
        meta["used_time"] = True
    meta["final_cmd"] = command
    meta["rusage_scope"] = "delta of reaped children, including wrappers; sequential use only"
    meta["rss_source"] = "time tool (KiB)" if meta["used_time"] else "unavailable"
    return command, meta


# Cancellation may overrun the workload deadline by these separate cleanup bounds.
_CANCEL_GRACE_SECONDS = 0.2
_DRAIN_SECONDS = 1.0
_REAP_SECONDS = 1.0
_POLL_SECONDS = 0.05


def _signal_group(proc, signum):
    try:
        os.killpg(proc.pid, signum)
        return True
    except ProcessLookupError:
        return True  # No owned group remains.
    except OSError:
        return False


def _teardown(proc, signum):
    """Forward once, then kill the group even if its leader has already exited."""
    known = _signal_group(proc, signum)
    if signum != signal.SIGKILL:
        deadline = time.monotonic() + _CANCEL_GRACE_SECONDS
        while time.monotonic() < deadline:
            proc.poll()
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                break
            except OSError:
                # Some POSIX hosts transiently report EPERM for an exiting group.
                # The final kill/reap decides whether teardown is established.
                pass
            time.sleep(min(_POLL_SECONDS, max(0, deadline - time.monotonic())))
        known = _signal_group(proc, signal.SIGKILL)
    return known


def _drain_and_reap(proc):
    complete = True
    stdout, stderr = "", ""
    try:
        stdout, stderr = proc.communicate(timeout=_DRAIN_SECONDS)
    except subprocess.TimeoutExpired as exc:
        # An escaped session may keep a descriptor open. It is outside ownership.
        complete = False
        stdout, stderr = util._decode(exc.stdout), util._decode(exc.stderr)
    finally:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
    try:
        proc.wait(timeout=_REAP_SECONDS)
    except subprocess.TimeoutExpired:
        return stdout, stderr, complete, False
    return stdout or "", stderr or "", complete, True


@util.cancellable
def _run_command(
    command: list[str],
    env: dict[str, str],
    timeout: float | None,
    preexec_fn: Callable[[], None] | None,
    cwd: str | Path | None = None,
    *,
    passthrough: bool = False,
) -> tuple[int, str, bool, float, Any, Any, str | None, dict[str, Any]]:
    start_usage = _rusage_snapshot()
    start_time = time.perf_counter()
    proc = None
    error = None
    code = 0
    stdout, stderr = "", ""
    timed_out = False
    output_complete = True
    termination_known = True
    with util.cancellation_scope() as cancellation:
        try:
            # Handlers are installed before Popen, including its exec-error handshake.
            if cancellation.signum is None:
                proc = subprocess.Popen(
                    command,
                    stdout=None if passthrough else subprocess.PIPE,
                    stderr=None if passthrough else subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    preexec_fn=preexec_fn,
                    start_new_session=True,
                    cwd=cwd,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            code = 127 if isinstance(exc, FileNotFoundError) else 126
            error = f"could not launch command or apply process settings: {exc}"
            stderr = error
        try:
            if proc is not None:
                deadline = None if timeout is None else time.monotonic() + timeout
                while cancellation.signum is None:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        timed_out = True
                        break
                    try:
                        stdout, stderr = proc.communicate(
                            timeout=min(_POLL_SECONDS, remaining)
                            if remaining is not None
                            else _POLL_SECONDS
                        )
                        break
                    except subprocess.TimeoutExpired:
                        continue
                    except KeyboardInterrupt:
                        cancellation.request(signal.SIGINT)
                if cancellation.signum is not None or timed_out:
                    termination_known = _teardown(
                        proc,
                        cancellation.signum if cancellation.signum is not None else signal.SIGKILL,
                    )
                    stdout, stderr, output_complete, reaped = _drain_and_reap(proc)
                    termination_known = termination_known and reaped
                code = proc.returncode
        except BaseException:
            if proc is not None:
                _teardown(proc, signal.SIGKILL)
                _drain_and_reap(proc)
            raise
        signum = cancellation.signum
        # A signal racing with child exit is still a parent cancellation request.
        if signum is not None:
            code = -signum
        elif timed_out:
            code = 124
        details = {
            "parent_signal": signum,
            "workload_return_code": proc.returncode if proc is not None else None,
            "launch_attempted": proc is not None,
            "termination_known": termination_known,
            "output_complete": output_complete,
        }
    raw_output = (
        "stdout/stderr inherited by the workload; not captured"
        if passthrough
        else f"STDOUT:\n{stdout or ''}\nSTDERR:\n{stderr or ''}"
    )
    return (
        code,
        raw_output,
        timed_out,
        time.perf_counter() - start_time,
        start_usage,
        _rusage_snapshot(),
        error,
        details,
    )


@util.cancellable
def execute_command(
    cmd: list[str],
    env: dict[str, str],
    timeout: float | None,
    preexec_fn: Callable[[], None] | None = None,
    *,
    cwd: str | Path | None = None,
    passthrough: bool = False,
) -> tuple[dict[str, Any], str]:
    """Execute once without time/perf wrappers, retaining diagnostics.

    This is the minimal path used by strict recipe execution.  It deliberately
    shares the same bounded process-group cleanup as profiling and never retries
    a command whose execution may have started.
    """
    command = util.sanitize_cmd(cmd)
    util.validate_run_options(1, timeout)
    kwargs = {"passthrough": True} if passthrough else {}
    code, raw, timed_out, wall, start_usage, end_usage, error, cancellation = _run_command(
        command, env, timeout, preexec_fn, cwd, **kwargs
    )
    result: dict[str, Any] = {
        "wall_time_sec": wall,
        "cancellation": cancellation,
        "return_code": code,
        "timed_out": timed_out,
        "rusage": _rusage_delta(start_usage, end_usage),
        "profiler": {
            "used_time": False,
            "used_perf": False,
            "final_cmd": command,
            "rusage_scope": "delta of reaped children, including wrappers; sequential use only",
            "rss_source": "unavailable",
        },
        "perf_fallback": False,
    }
    if error:
        result["error"] = error
    return result, raw


@util.cancellable
def collect_perf_command(
    cmd: list[str],
    env: dict[str, str],
    timeout: float | None,
    perf_path: str,
    perf_version: str,
    preexec_fn: Callable[[], None] | None = None,
    *,
    cwd: str | Path | None = None,
) -> tuple[dict[str, Any], str]:
    """Run one perf-wrapped workload. Wrapper or parser failure is a failed trial."""
    from .collectors import parse_live_perf, read_launcher_affinity

    command = util.sanitize_cmd(cmd)
    util.validate_run_options(1, timeout)
    launcher_affinity = read_launcher_affinity()
    with tempfile.TemporaryDirectory(prefix="simtune-perf-") as tmp:
        output = Path(tmp) / "perf.txt"
        wrapped = [
            perf_path,
            "stat",
            "-x;",
            "--no-big-num",
            "-e",
            "task-clock,cycles,instructions",
            "-o",
            str(output),
            "--",
            *command,
        ]
        code, raw, timed_out, wall, start_usage, end_usage, error, cancellation = _run_command(
            wrapped, env, timeout, preexec_fn, cwd
        )
        perf_raw = output.read_bytes() if output.exists() else b""
    result: dict[str, Any] = {
        "wall_time_sec": wall,
        "cancellation": cancellation,
        "return_code": code,
        "timed_out": timed_out,
        "rusage": _rusage_delta(start_usage, end_usage),
        "profiler": {
            "used_perf": True,
            "used_time": False,
            "final_cmd": wrapped,
            "perf_version": perf_version,
            "rss_source": "unavailable",
        },
        "perf_fallback": False,
        "collector_observations": {"launcher_affinity": launcher_affinity},
    }
    if error:
        result["error"] = error
    if code == 0 and not timed_out:
        try:
            if not perf_raw:
                raise ValueError("perf produced no statistics")
            result["collector_metrics"] = parse_live_perf(perf_raw, version=perf_version)
        except ValueError as exc:
            result["return_code"] = 1
            result["error"] = f"perf collector output invalid: {exc}"
    return result, raw + "\nPERF:\n" + perf_raw.decode("utf-8", errors="replace")


@util.cancellable
def profile_command(
    cmd: list[str],
    env: dict[str, str],
    timeout: float | None,
    capabilities: dict[str, Any],
    preexec_fn: Callable[[], None] | None = None,
    *,
    cwd: str | Path | None = None,
) -> tuple[dict[str, Any], str]:
    """Execute once. A wrapper failure is recorded, never retried automatically.

    stdout/stderr are captured as UTF-8 with replacement for invalid bytes.
    Resource deltas require sequential use in a process that reaps no other jobs.
    Process settings use preexec_fn, so tuned execution is not thread-safe.
    """
    cmd = util.sanitize_cmd(cmd)
    util.validate_run_options(1, timeout)
    if capabilities.get("platform") not in {"linux", "macos"}:
        raise ValueError("profiling requires Linux (including WSL) or macOS")
    with tempfile.TemporaryDirectory(prefix="simtune-profiler-") as tmp:
        time_path, perf_path = Path(tmp) / "time.txt", Path(tmp) / "perf.txt"
        command, meta = _build_profiler_command(cmd, capabilities, str(time_path), str(perf_path))
        code, raw, timed_out, wall, start_usage, end_usage, error, cancellation = _run_command(
            command, env, timeout, preexec_fn, cwd
        )
        time_text = (
            time_path.read_text(encoding="utf-8", errors="replace") if time_path.exists() else ""
        )
        perf_text = (
            perf_path.read_text(encoding="utf-8", errors="replace") if perf_path.exists() else ""
        )
    result: dict[str, Any] = {
        "wall_time_sec": wall,
        "cancellation": cancellation,
        "return_code": code,
        "timed_out": timed_out,
        "rusage": _rusage_delta(start_usage, end_usage),
        "profiler": meta,
        "perf_fallback": False,
    }
    if error:
        result["error"] = error
    if meta["used_time"]:
        parser = _parse_time_macos if capabilities["platform"] == "macos" else _parse_time_linux
        result["time_tool"] = parser(time_text)
    if meta["used_perf"]:
        result["perf"] = _parse_perf_csv(perf_text)
    return result, raw + f"\nTIME:\n{time_text}\nPERF:\n{perf_text}"
