"""Profiles loading and validation."""

from __future__ import annotations

import copy
import math
import re
from pathlib import Path
from typing import Any

from . import util

BUILTIN_PROFILES: dict[str, Any] = {
    "desktop_friendly": {
        "description": "Low interference with desktop workloads; gentler CPU/IO priority.",
        "fallback_mode": "throughput",
        "safety_notes": "Avoids aggressive CPU pinning; may reduce throughput.",
        "actions": [
            {"type": "nice", "level": 5},
            {"type": "ionice", "class": "idle", "optional": True},
            {"type": "cgroup_systemd", "optional": True, "properties": {"CPUQuota": "80%"}},
        ],
    },
    "throughput": {
        "description": "CPU-oriented policy with explicit affinity and optional NUMA interleaving.",
        "fallback_mode": "desktop_friendly",
        "safety_notes": "May reduce desktop responsiveness on shared machines.",
        "actions": [
            {"type": "nice", "level": 0},
            {"type": "taskset", "cpus": "available"},
            {"type": "numactl", "optional": True, "interleave": "all"},
        ],
    },
    "isolated": {
        "description": "Constrain CPU and memory use through a systemd user scope.",
        "fallback_mode": "desktop_friendly",
        "safety_notes": "Requires a functional systemd user manager; this is not a security sandbox.",
        "actions": [
            {
                "type": "cgroup_systemd",
                "properties": {"CPUQuota": "70%", "MemoryMax": "4G", "IOWeight": "100"},
            }
        ],
    },
    "latency": {
        "description": "Prefer latency-sensitive scheduling where allowed.",
        "fallback_mode": "desktop_friendly",
        "safety_notes": "Negative nice may require elevated privileges.",
        "actions": [
            {"type": "nice", "level": -5},
            {"type": "taskset", "optional": True, "cpus": "0-1"},
        ],
    },
}

ACTION_REQUIREMENTS = {
    "nice": ["nice"],
    "ionice": ["ionice"],
    "taskset": ["taskset"],
    "numactl": ["numactl"],
    "cgroup_systemd": ["cgroup_systemd"],
    "env": [],
    "ulimit": ["ulimit"],
    "mac_taskpolicy": ["mac_taskpolicy"],
}


PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
RESOURCE_NAMES = {"as", "core", "cpu", "data", "fsize", "nofile", "nproc", "rss", "stack"}
ACTION_FIELDS = {
    "nice": {"level"},
    "ionice": {"class", "level"},
    "taskset": {"cpus"},
    "numactl": {"interleave", "membind", "physcpubind", "args"},
    "cgroup_systemd": {"properties"},
    "env": {"vars"},
    "ulimit": {"limits"},
    "mac_taskpolicy": {"policy", "args"},
}


def parse_cpu_list(value: Any) -> set[int]:
    """Parse explicit CPU IDs and inclusive ranges; reject invalid/huge ranges."""
    if isinstance(value, list):
        if not value or any(type(cpu) is not int or cpu < 0 for cpu in value):
            raise ValueError("cpus must be a nonempty list of nonnegative integers")
        return set(value)
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*", value):
        raise ValueError("cpus must be 'available', a CPU list, or ranges such as '0-3,6'")
    cpus: set[int] = set()
    for part in value.split(","):
        bounds = part.split("-")
        start, end = int(bounds[0]), int(bounds[-1])
        if end < start or end - start > 65535:
            raise ValueError("invalid or excessively large CPU range")
        cpus.update(range(start, end + 1))
    return cpus


def validate_action(action: Any) -> list[str]:
    """Return actionable errors without assuming the input has a valid shape."""
    if not isinstance(action, dict) or not isinstance(action.get("type"), str):
        return ["action must be a mapping with a string type"]
    kind = action["type"]
    if kind not in ACTION_FIELDS:
        return [f"unknown action type {kind!r}"]
    errors = []
    unknown = set(action) - ACTION_FIELDS[kind] - {"type", "optional"}
    if unknown:
        errors.append(f"unknown fields: {sorted(unknown, key=str)}")
    if "optional" in action and type(action["optional"]) is not bool:
        errors.append("optional must be a boolean")
    if kind == "nice" and (
        type(action.get("level")) is not int or not -20 <= action["level"] <= 19
    ):
        errors.append("nice level must be an integer from -20 to 19")
    if kind == "ionice":
        if action.get("class", "best-effort") not in (
            "idle",
            "best-effort",
            "realtime",
            "real-time",
        ):
            errors.append("ionice class must be idle, best-effort, or realtime")
        if "level" in action and (
            type(action["level"]) is not int or not 0 <= action["level"] <= 7
        ):
            errors.append("ionice level must be an integer from 0 to 7")
    if kind == "taskset" and action.get("cpus") != "available":
        try:
            parse_cpu_list(action.get("cpus"))
        except ValueError as exc:
            errors.append(str(exc))
    if "args" in action and (
        not isinstance(action["args"], list)
        or any(not isinstance(arg, str) or "\0" in arg for arg in action["args"])
    ):
        errors.append("args must be a list of NUL-free strings")
    if kind == "mac_taskpolicy":
        if action.get("policy") not in (None, "background"):
            errors.append("taskpolicy policy must be background; use args for explicit flags")
        if not action.get("policy") and not action.get("args"):
            errors.append("taskpolicy requires policy or args")
    if kind == "numactl":
        if not any(
            key in action for key in ("interleave", "membind", "physcpubind")
        ) and not action.get("args"):
            errors.append("numactl requires a binding option or args")
        for key in ("interleave", "membind", "physcpubind"):
            if key in action and (
                not isinstance(action[key], (str, int))
                or isinstance(action[key], bool)
                or not str(action[key])
                or "\0" in str(action[key])
            ):
                errors.append(f"{key} must be a nonempty string or integer")
    for field in ("vars", "properties", "limits"):
        if field not in ACTION_FIELDS[kind]:
            continue
        values = action.get(field)
        if not isinstance(values, dict) or not values:
            errors.append(f"{field} must be a nonempty mapping")
            continue
        for key, value in values.items():
            if not isinstance(key, str) or not key or "\0" in key or "=" in key:
                errors.append(f"invalid {field} key {key!r}")
            if field != "limits":
                if (
                    not isinstance(value, (str, int, float, bool))
                    or "\0" in str(value)
                    or (isinstance(value, float) and not math.isfinite(value))
                ):
                    errors.append(f"{field} values must be scalar and NUL-free")
            else:
                if key not in RESOURCE_NAMES:
                    errors.append(f"unknown resource {key!r}")
                pair = value if isinstance(value, (list, tuple)) else [value, value]
                if len(pair) != 2 or any(type(n) is not int or n < -1 for n in pair):
                    errors.append(
                        f"limit {key} must be an integer or [soft, hard]; -1 means unlimited"
                    )
                elif pair[1] != -1 and (pair[0] == -1 or pair[0] > pair[1]):
                    errors.append(f"limit {key}: soft cannot exceed hard")
    return errors


def _validate_profile(name: str, profile: Any) -> list[str]:
    if not isinstance(profile, dict):
        return [f"{name}: profile must be a mapping"]
    errors = []
    for key in ("description", "safety_notes"):
        if not isinstance(profile.get(key), str):
            errors.append(f"{name}: {key} must be a string")
    if "fallback_mode" not in profile or not isinstance(
        profile["fallback_mode"], (str, type(None))
    ):
        errors.append(f"{name}: fallback_mode must be a profile name or null")
    if not isinstance(profile.get("actions"), list):
        return errors + [f"{name}: actions must be a list"]
    for idx, action in enumerate(profile["actions"]):
        errors.extend(f"{name}: action {idx}: {error}" for error in validate_action(action))
    return errors


def validate_profiles(profiles: Any) -> list[str]:
    if not isinstance(profiles, dict) or not profiles:
        return ["profiles must be a nonempty mapping"]
    errors = []
    for name, profile in profiles.items():
        if not isinstance(name, str) or not PROFILE_NAME.fullmatch(name):
            errors.append(f"invalid profile name {name!r}")
        errors.extend(_validate_profile(str(name), profile))
        if isinstance(profile, dict):
            fallback = profile.get("fallback_mode")
            if isinstance(fallback, str) and fallback not in profiles:
                errors.append(f"{name}: unknown fallback profile {fallback!r}")
    return errors


def load_profiles(paths: list[str] | None = None) -> dict[str, Any]:
    """Return independent built-ins, or load and validate explicit YAML files.

    Explicit files replace the built-in collection; later files override earlier
    profiles. Missing files or PyYAML are errors, never silent substitutions.
    """
    if paths is None:
        return copy.deepcopy(BUILTIN_PROFILES)
    yaml = util.optional_import("yaml")
    if yaml is None:
        raise ValueError("custom profiles require PyYAML; install simtune[yaml]")
    data: dict[str, Any] = {}
    for filename in paths:
        path = Path(filename)
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid YAML in {path}: {exc}") from exc
        if not isinstance(loaded, dict) or not loaded:
            raise ValueError(f"{path}: profiles must be a nonempty mapping")
        data.update(loaded)
    errors = validate_profiles(data)
    if errors:
        raise ValueError("Invalid profiles: " + "; ".join(errors))
    return data


def profile_required_capabilities(profile: dict[str, Any]) -> list[str]:
    required: list[str] = []
    for action in profile.get("actions", []):
        if action.get("optional"):
            continue
        action_type = action.get("type")
        for req in ACTION_REQUIREMENTS.get(action_type, []):
            if req not in required:
                required.append(req)
    return required


def list_modes(profiles: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for name, profile in profiles.items():
        rows.append(
            {
                "name": name,
                "description": profile.get("description", ""),
                "required": ",".join(profile_required_capabilities(profile)),
            }
        )
    return rows


def get_profile(profiles: dict[str, Any], name: str) -> dict[str, Any]:
    if name in profiles:
        return profiles[name]
    raise KeyError(f"Unknown profile: {name}")
