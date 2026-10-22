"""Auto mode heuristics."""

from __future__ import annotations

from typing import Any

from . import capabilities as cap


def select_mode(
    metrics: dict[str, Any],
    capabilities: dict[str, Any],
    latency_important: bool,
    respect_desktop: bool,
) -> dict[str, Any]:
    wall = float(metrics.get("wall_time_sec") or 0.0)
    user = float(metrics.get("user_time_sec") or 0.0)
    sys = float(metrics.get("sys_time_sec") or 0.0)
    cpu_ratio = (user + sys) / wall if wall > 0 else 0.0

    in_block = float(metrics.get("in_block") or 0.0)
    out_block = float(metrics.get("out_block") or 0.0)
    io_per_sec = (in_block + out_block) / wall if wall > 0 else 0.0

    blocked_ratio = max(0.0, wall - (user + sys)) / wall if wall > 0 else 0.0

    rules = []

    if latency_important:
        rules.append("latency_important flag forces latency mode")
        return {
            "mode": "latency",
            "reason": "latency-important flag",
            "rules": rules,
            "inputs": {
                "cpu_ratio": cpu_ratio,
                "io_per_sec": io_per_sec,
                "blocked_ratio": blocked_ratio,
            },
        }

    if respect_desktop:
        rules.append("respect_desktop flag forces desktop_friendly mode")
        return {
            "mode": "desktop_friendly",
            "reason": "respect-desktop flag",
            "rules": rules,
            "inputs": {
                "cpu_ratio": cpu_ratio,
                "io_per_sec": io_per_sec,
                "blocked_ratio": blocked_ratio,
            },
        }

    high_cpu = cpu_ratio >= 0.7
    high_io = io_per_sec >= 100 or blocked_ratio >= 0.5

    if high_io:
        rules.append("high IO activity or blocked time")
        support_map = cap.supported_action_map(capabilities)
        if support_map.get("cgroup_systemd", False):
            return {
                "mode": "isolated",
                "reason": "IO-bound workload with cgroup support",
                "rules": rules,
                "inputs": {
                    "cpu_ratio": cpu_ratio,
                    "io_per_sec": io_per_sec,
                    "blocked_ratio": blocked_ratio,
                },
            }
        return {
            "mode": "desktop_friendly",
            "reason": "IO-bound workload without cgroup support",
            "rules": rules,
            "inputs": {
                "cpu_ratio": cpu_ratio,
                "io_per_sec": io_per_sec,
                "blocked_ratio": blocked_ratio,
            },
        }

    if high_cpu and not high_io:
        rules.append("high CPU utilization with low IO")
        return {
            "mode": "throughput",
            "reason": "CPU-bound workload",
            "rules": rules,
            "inputs": {
                "cpu_ratio": cpu_ratio,
                "io_per_sec": io_per_sec,
                "blocked_ratio": blocked_ratio,
            },
        }

    rules.append("defaulting to desktop_friendly")
    return {
        "mode": "desktop_friendly",
        "reason": "mixed workload default",
        "rules": rules,
        "inputs": {
            "cpu_ratio": cpu_ratio,
            "io_per_sec": io_per_sec,
            "blocked_ratio": blocked_ratio,
        },
    }
