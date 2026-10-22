"""Capability detection for simtune."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from . import topology, util

# The running interpreter exists even on hosts without /usr/bin/true. Isolate
# probes from user site packages and Python environment startup configuration.
_PROBE_COMMAND = [os.sys.executable, "-I", "-S", "-c", "pass"]

ACTION_TYPES = [
    "nice",
    "ionice",
    "taskset",
    "numactl",
    "cgroup_systemd",
    "env",
    "ulimit",
    "mac_taskpolicy",
]


def _tool_version(path: str, args: list[str]) -> str:
    result = util.run_command([path] + args, timeout=2)
    if result.returncode == 0:
        return result.stdout.strip() or result.stderr.strip()
    return result.stderr.strip() or result.stdout.strip()


def _detect_tool(name: str, version_args: list[str] | None = None) -> dict[str, Any]:
    path = util.which(name)
    data: dict[str, Any] = {"path": path, "available": bool(path), "version": ""}
    if path and version_args:
        data["version"] = util.compact_text(_tool_version(path, version_args))
    return data


def _detect_systemd(systemd_run: dict[str, Any]) -> dict[str, Any]:
    if not systemd_run.get("available"):
        return {"available": False, "functional": False, "reason": "systemd-run not found"}
    result = util.run_command(
        [
            systemd_run["path"],
            "--user",
            "--scope",
            "--quiet",
            "--expand-environment=no",
            "--",
            *_PROBE_COMMAND,
        ],
        timeout=3,
    )
    if result.returncode == 0:
        return {"available": True, "functional": True, "reason": ""}
    reason = util.compact_text(result.stderr or result.stdout)
    return {"available": True, "functional": False, "reason": reason or "systemd-run failed"}


def _detect_perf(perf: dict[str, Any]) -> dict[str, Any]:
    if not perf.get("available"):
        return {"available": False, "functional": False, "reason": "perf not found"}
    result = util.run_command([perf["path"], "stat", "-x;", "--", *_PROBE_COMMAND], timeout=3)
    if result.returncode == 0:
        return {"available": True, "functional": True, "reason": ""}
    reason = util.compact_text(result.stderr or result.stdout)
    return {"available": True, "functional": False, "reason": reason or "perf stat failed"}


def _detect_cgroups() -> dict[str, Any]:
    cgroup_root = "/sys/fs/cgroup"
    if not os.path.exists(cgroup_root):
        return {"mounted": False, "mode": ""}
    mode = "v1"
    if os.path.exists(os.path.join(cgroup_root, "cgroup.controllers")):
        mode = "v2"
    return {"mounted": True, "mode": mode}


def detect_capabilities() -> dict[str, Any]:
    platform_name = util.detect_platform()
    wsl = util.is_wsl() if platform_name == "linux" else False

    linux_tools = {"perf", "taskset", "numactl", "ionice", "systemd-run", "systemctl"}
    tools = {}
    for name in (
        "time",
        "perf",
        "taskset",
        "numactl",
        "ionice",
        "systemd-run",
        "systemctl",
        "taskpolicy",
    ):
        if (name in linux_tools and platform_name != "linux") or (
            name == "taskpolicy" and platform_name != "macos"
        ):
            tools[name] = {"path": None, "available": False, "version": ""}
        else:
            args = ["--hardware"] if name == "numactl" else ["--version"]
            if name == "taskpolicy" or (name == "time" and platform_name == "macos"):
                args = None
            tools[name] = _detect_tool(name, args)
    # PATH may provide an incompatible `time` implementation (e.g. GNU on macOS).
    # Verify precisely the invocation we use, against a harmless command.
    time_tool = tools["time"]
    time_tool["functional"] = False
    if time_tool["path"] and platform_name in {"linux", "macos"}:
        with tempfile.TemporaryDirectory(prefix="simtune-probe-") as tmp:
            output = str(Path(tmp) / "time.txt")
            flags = (
                ["-f", "simtune_user=%U\nsimtune_sys=%S\nsimtune_rss=%M"]
                if platform_name == "linux"
                else ["-l"]
            )
            result = util.run_command(
                [time_tool["path"], *flags, "-o", output, "--", *_PROBE_COMMAND], timeout=2
            )
            time_tool["functional"] = result.returncode == 0
            time_tool["reason"] = "" if result.returncode == 0 else util.compact_text(result.stderr)

    systemd = _detect_systemd(tools["systemd-run"])
    perf = _detect_perf(tools["perf"])
    affinity = None
    if platform_name == "linux" and hasattr(os, "sched_getaffinity"):
        try:
            affinity = sorted(os.sched_getaffinity(0))
        except OSError:
            pass
    resource_snapshot = topology.discover_resource_snapshot(
        platform_name=platform_name,
        is_wsl=wsl,
        affinity_reader=(lambda: affinity) if affinity is not None else None,
    )

    capabilities = {
        "platform": platform_name,
        "cpu": {"logical_count": os.cpu_count(), "affinity": affinity},
        "resource_snapshot": resource_snapshot.to_dict(),
        "is_wsl": wsl,
        "kernel": util.gather_kernel_info(),
        "tools": tools,
        "systemd": systemd,
        "perf": perf,
        "cgroups": _detect_cgroups(),
        "permissions": {
            "is_root": util.is_root(),
            "euid": os.geteuid() if hasattr(os, "geteuid") else None,
        },
        "python": {
            "version": os.sys.version,
            "executable": os.path.abspath(os.sys.executable),
        },
    }
    return capabilities


def action_capabilities(capabilities: dict[str, Any]) -> list[dict[str, Any]]:
    platform_name = capabilities.get("platform", "other")
    wsl = capabilities.get("is_wsl", False)
    tools = capabilities.get("tools", {})
    systemd = capabilities.get("systemd", {})

    rows: list[dict[str, Any]] = []

    def add(action: str, supported: bool, reason: str = "") -> None:
        rows.append({"action": action, "supported": util.format_bool(supported), "reason": reason})

    add("env", True, "always available")
    posix = platform_name in {"linux", "macos"}
    add("ulimit", posix, "resource module" if posix else "unsupported platform")
    add(
        "nice",
        posix,
        "priority increases may require privileges" if posix else "unsupported platform",
    )

    add(
        "ionice",
        platform_name == "linux" and bool(tools.get("ionice", {}).get("available")),
        "requires ionice" if platform_name == "linux" else "linux-only",
    )
    add(
        "taskset",
        platform_name == "linux" and bool(tools.get("taskset", {}).get("available")),
        "requires taskset" if platform_name == "linux" else "linux-only",
    )

    numactl_supported = platform_name == "linux" and bool(tools.get("numactl", {}).get("available"))
    numactl_reason = "requires numactl" if platform_name == "linux" else "linux-only"
    if wsl and numactl_supported:
        numactl_reason = "WSL2: NUMA binding is typically ineffective"
    add("numactl", numactl_supported, numactl_reason)

    cgroup_supported = platform_name == "linux" and bool(systemd.get("functional"))
    cgroup_reason = (
        "systemd-run functional" if cgroup_supported else systemd.get("reason", "") or "linux-only"
    )
    add("cgroup_systemd", cgroup_supported, cgroup_reason)

    add(
        "mac_taskpolicy",
        platform_name == "macos" and bool(tools.get("taskpolicy", {}).get("available")),
        "requires taskpolicy" if platform_name == "macos" else "macos-only",
    )

    return rows


def supported_action_map(capabilities: dict[str, Any]) -> dict[str, bool]:
    support: dict[str, bool] = {}
    for row in action_capabilities(capabilities):
        support[row["action"]] = row["supported"] == "yes"
    return support


def doctor_report(capabilities: dict[str, Any]) -> str:
    lines = []
    lines.append("simtune doctor")
    lines.append("")
    lines.append(f"Platform: {capabilities['platform']}")
    lines.append(f"WSL detected: {util.format_bool(capabilities['is_wsl'])}")
    kernel = capabilities.get("kernel", {})
    lines.append(f"Kernel: {kernel.get('release', '')} {kernel.get('version', '')}")
    lines.append("")
    lines.append(f"CPU: {capabilities.get('cpu', {})}")
    snapshot = capabilities.get("resource_snapshot")
    if isinstance(snapshot, dict):
        lines.append("Resource snapshot:")
        lines.append(f"  Online CPUs: {snapshot.get('online_cpu_ids')}")
        lines.append(f"  Inherited CPUs: {snapshot.get('inherited_cpu_ids')}")
        lines.append(f"  Permitted CPUs: {snapshot.get('permitted_cpu_ids')}")
        lines.append(f"  Effective CPUs: {snapshot.get('effective_cpu_ids')}")
        lines.append(f"  Allowed memory nodes: {snapshot.get('allowed_memory_node_ids')}")
        limits = snapshot.get("memory_limits", [])
        lines.append(f"  Memory limits: {limits}")
        conflicts = snapshot.get("conflicts", [])
        if conflicts:
            lines.append("  Conflicts/limitations:")
            lines.extend(f"    - {item}" for item in conflicts)
        unavailable = snapshot.get("unavailable", {})
        if unavailable:
            lines.append("  Unavailable:")
            lines.extend(f"    {name}: {reason}" for name, reason in unavailable.items())
    lines.append("Tools:")
    tools = capabilities.get("tools", {})
    for name, info in tools.items():
        status = "found" if info.get("available") else "missing"
        path = info.get("path") or ""
        lines.append(f"  {name}: {status} {path}")
        if info.get("reason"):
            lines.append(f"    {info['reason']}")
    lines.append("")
    systemd = capabilities.get("systemd", {})
    lines.append(
        f"systemd-run: {util.format_bool(systemd.get('functional'))} {systemd.get('reason', '')}"
    )
    perf = capabilities.get("perf", {})
    lines.append(f"perf stat: {util.format_bool(perf.get('functional'))} {perf.get('reason', '')}")
    lines.append("")

    rows = action_capabilities(capabilities)
    table = util.render_table(rows, ["action", "supported", "reason"])
    lines.append("Capabilities:")
    lines.append(table)
    return "\n".join(lines)
