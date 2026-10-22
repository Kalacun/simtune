"""Utility helpers for simtune."""

from __future__ import annotations

import io
import json
import math
import os
import platform
import shutil
import signal
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Iterable, Optional


@dataclass
class Cancellation:
    # The handler only records the first request. Waiting and I/O belong to callers.
    signum: int | None = None

    def request(self, signum, _frame=None):
        if self.signum is None:
            self.signum = signum


_active_cancellation: Cancellation | None = None


@contextmanager
def cancellation_scope():
    """Temporary handlers for sequential main-thread execution, nested through persistence."""
    global _active_cancellation
    if threading.current_thread() is not threading.main_thread():
        raise ValueError("command execution requires the sequential main thread")
    if _active_cancellation is not None:
        yield _active_cancellation
        return
    state = Cancellation()
    previous = {}
    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    # Prevent delivery between individual handler registrations on POSIX.
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
    try:
        for signum in signals:
            previous[signum] = signal.signal(signum, state.request)
        _active_cancellation = state
    except BaseException:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        raise
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, mask)
    try:
        yield state
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        _active_cancellation = None


def cancellable(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with cancellation_scope():
            return function(*args, **kwargs)

    return wrapped


def cancellation_signal():
    return _active_cancellation.signum if _active_cancellation is not None else None


class CommandCancelled(KeyboardInterrupt):
    """A command check was cancelled; its evidence has already been persisted."""

    def __init__(self, metrics, evidence):
        super().__init__("parent cancellation during scientific validation")
        self.metrics = metrics
        self.evidence = evidence


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def optional_import(module_name: str) -> Optional[Any]:
    try:
        return __import__(module_name)
    except ImportError:
        return None


def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def run_command(
    args: list[str],
    timeout: Optional[float] = None,
    env: Optional[dict[str, str]] = None,
) -> CommandResult:
    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=env,
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(124, _decode(exc.stdout), _decode(exc.stderr) or "probe timed out")
    except OSError as exc:
        return CommandResult(127 if isinstance(exc, FileNotFoundError) else 126, "", str(exc))


def _decode(value: str | bytes | None) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else value or ""


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_json(path: str | Path, data: Any) -> None:
    p = Path(path)
    p.write_text(
        json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )


def write_text(path: str | Path, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def iso_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_first_line(path: str | Path) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.readline().strip()
    except OSError:
        return ""


def detect_platform() -> str:
    system = platform.system().lower()
    if system == "linux":
        return "linux"
    if system == "darwin":
        return "macos"
    return "other"


def is_wsl() -> bool:
    if os.environ.get("WSL_INTEROP") or os.environ.get("WSL_DISTRO_NAME"):
        return True
    version = read_first_line("/proc/version")
    return "microsoft" in version.lower() or "wsl" in version.lower()


def gather_kernel_info() -> dict[str, str]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
    }


def compact_text(text: str, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def render_table(rows: Iterable[dict[str, Any]], columns: list[str]) -> str:
    rich = optional_import("rich")
    if rich:
        from rich.console import Console
        from rich.table import Table
        from rich.text import Text

        table = Table(show_header=True, header_style="bold")
        for col in columns:
            table.add_column(col)
        for row in rows:
            table.add_row(*[Text(str(row.get(col, ""))) for col in columns])
        console = Console(record=True, file=io.StringIO(), markup=False)
        console.print(table)
        return console.export_text()

    lines = ["\t".join(columns)]
    for row in rows:
        lines.append("\t".join(str(row.get(col, "")) for col in columns))
    return "\n".join(lines)


def format_kv(data: dict[str, Any]) -> str:
    lines = []
    for key, value in data.items():
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def redact_env(env: dict[str, str], keys: Iterable[str]) -> dict[str, str]:
    redacted = dict(env)
    for key in keys:
        if key in redacted:
            redacted[key] = "***"
    return redacted


def write_command_file(path: str | Path, cmd: list[str]) -> None:
    Path(path).write_text("\n".join(cmd) + "\n")


def load_json(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path} at line {exc.lineno}: {exc.msg}") from exc


def in_wsl_or_linux(platform_name: str, wsl: bool) -> bool:
    return platform_name == "linux" and wsl


def format_bool(value: bool) -> str:
    return "yes" if value else "no"


def run_git_info() -> dict[str, Any]:
    git = which("git")
    if not git:
        return {}
    rev = run_command([git, "rev-parse", "HEAD"], timeout=2)
    if rev.returncode != 0:
        return {}
    branch = run_command([git, "rev-parse", "--abbrev-ref", "HEAD"], timeout=2)
    info: dict[str, Any] = {"commit": rev.stdout.strip()}
    status = run_command([git, "status", "--porcelain"], timeout=2)
    if status.returncode == 0:
        info["dirty"] = bool(status.stdout.strip())
    if branch.returncode == 0:
        info["branch"] = branch.stdout.strip()
    return info


def apply_env_overrides(base: dict[str, str], overrides: dict[str, str]) -> dict[str, str]:
    env = dict(base)
    env.update(overrides)
    return env


def has_command(path: Optional[str]) -> bool:
    return bool(path and Path(path).exists())


def is_root() -> bool:
    if hasattr(os, "geteuid"):
        return os.geteuid() == 0
    return False


def allowlist_resource_names() -> dict[str, int]:
    import resource

    names = ("as", "core", "cpu", "data", "fsize", "nofile", "nproc", "rss", "stack")
    mapping = {
        name: getattr(resource, "RLIMIT_" + name.upper())
        for name in names
        if hasattr(resource, "RLIMIT_" + name.upper())
    }
    return mapping


def parse_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sanitize_cmd(cmd: list[str]) -> list[str]:
    if not cmd or any(not isinstance(item, str) or "\0" in item for item in cmd) or not cmd[0]:
        raise ValueError("command must contain a nonempty executable and NUL-free string arguments")
    return list(cmd)


def is_executable(path: Optional[str]) -> bool:
    if not path:
        return False
    return os.access(path, os.X_OK)


def validate_profile_name(name: str) -> str:
    return name.strip().replace(" ", "_")


def is_timeout(exit_code: int) -> bool:
    return exit_code == 124


def ensure_ascii(text: str) -> str:
    return text.encode("ascii", "ignore").decode("ascii")


def default_env() -> dict[str, str]:
    return dict(os.environ)


def flatten_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def platform_hint() -> str:
    return detect_platform()


def safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def send_notification(summary: str, body: str) -> tuple[bool, str]:
    notify_send = which("notify-send")
    if notify_send and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        result = run_command([notify_send, summary, body], timeout=2)
        if result.returncode == 0:
            return True, ""
        return False, compact_text(result.stderr or result.stdout)

    dbus_send = which("dbus-send")
    if dbus_send and os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        args = [
            dbus_send,
            "--session",
            "--dest=org.freedesktop.Notifications",
            "--type=method_call",
            "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications.Notify",
            "string:simtune",
            "uint32:0",
            "string:",
            f"string:{summary}",
            f"string:{body}",
            "array:string:",
            "dict:string:variant:",
            "int32:5000",
        ]
        result = run_command(args, timeout=2)
        if result.returncode == 0:
            return True, ""
        return False, compact_text(result.stderr or result.stdout)

    if not notify_send and not dbus_send:
        return False, "notify-send/dbus-send not found"
    return False, "no notification transport available"


def validate_run_options(repeat: int, timeout: float | None) -> None:
    """Validate API calls as well as CLI input before any workload is launched."""
    if type(repeat) is not int or repeat < 1:
        raise ValueError("repeat must be a positive integer")
    if timeout is not None and (
        isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0
    ):
        raise ValueError("timeout must be a finite positive number")
