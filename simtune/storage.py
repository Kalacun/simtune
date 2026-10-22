"""Run storage utilities."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__, util
from . import capabilities as cap

SCHEMA_VERSION = 2

METRICS_SCHEMA = {
    "type": "object",
    "properties": {
        "iterations": {"type": "array"},
        "aggregate": {"type": "object"},
    },
    "required": ["iterations", "aggregate"],
}

APPLIED_ACTIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "requested": {"type": "array"},
        "applied": {"type": "array"},
        "skipped": {"type": "array"},
        "adjusted": {"type": "array"},
        "final_command": {"type": "array"},
        "notifications": {"type": "object"},
    },
    "required": ["requested", "applied", "skipped", "adjusted", "final_command"],
}


def _validate_with_schema(data: dict[str, Any], schema: dict[str, Any], name: str) -> None:
    # Core validation must not depend on an optional extra being installed.
    if not isinstance(data, dict):
        raise ValueError(f"{name} must be an object")
    types = {"array": list, "object": dict}
    for key in schema["required"]:
        if key not in data:
            raise ValueError(f"{name}: missing {key}")
    for key, spec in schema["properties"].items():
        if key in data and not isinstance(data[key], types[spec["type"]]):
            raise ValueError(f"{name}: invalid type for {key}")
    if "iterations" in data and any(not isinstance(item, dict) for item in data["iterations"]):
        raise ValueError(f"{name}: iterations must contain objects")
    if "final_command" in data and any(not isinstance(arg, str) for arg in data["final_command"]):
        raise ValueError(f"{name}: final_command must contain strings")
    try:
        json.dumps(data, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{name}: invalid JSON data: {exc}") from exc


def validate_metrics(data: dict[str, Any]) -> None:
    _validate_with_schema(data, METRICS_SCHEMA, "metrics.json")


def create_run_dir(base_dir: str, mode: str) -> Path:
    from .profiles import PROFILE_NAME

    if not isinstance(mode, str) or not PROFILE_NAME.fullmatch(mode):
        raise ValueError("run mode must contain only letters, digits, underscores, or hyphens")
    root = util.ensure_dir(base_dir)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    name = f"run_{timestamp}_{mode}"
    run_dir = root / name
    counter = 1
    while True:
        try:
            run_dir.mkdir(exist_ok=False)
            break
        except FileExistsError:
            run_dir = root / f"{name}_{counter}"
            counter += 1
    return run_dir


def write_run(
    run_dir: Path,
    meta: dict[str, Any],
    capabilities: dict[str, Any],
    applied_actions: dict[str, Any],
    metrics: dict[str, Any],
    raw_profiler: str,
    cmd: list[str],
    rationale: dict[str, Any],
) -> None:
    meta = dict(meta)
    meta["git"] = util.run_git_info()
    meta["schema_version"] = SCHEMA_VERSION
    meta["simtune_version"] = __version__
    meta["cwd"] = os.getcwd()

    _validate_with_schema(metrics, METRICS_SCHEMA, "metrics.json")
    _validate_with_schema(applied_actions, APPLIED_ACTIONS_SCHEMA, "applied_actions.json")

    util.write_json(
        run_dir / "capabilities.json",
        {"capabilities": capabilities, "doctor": cap.doctor_report(capabilities)},
    )
    util.write_json(run_dir / "applied_actions.json", applied_actions)
    util.write_json(run_dir / "metrics.json", metrics)
    util.write_text(run_dir / "raw_profiler.txt", raw_profiler)
    util.write_text(run_dir / "command.txt", "\n".join(cmd) + "\n")
    util.write_json(run_dir / "rationale.json", rationale)
    # Metadata is the completion marker: interrupted writes remain visibly incomplete.
    util.write_json(run_dir / "meta.json", meta)
