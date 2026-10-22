"""Small, explicit project configuration for new orchestration commands."""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TUNE_KEYS = {
    "workload",
    "budget",
    "out",
    "save",
    "repeat",
    "warmups",
    "seed",
    "confirmation_pairs",
    "confirmation_fraction",
    "minimum_improvement",
    "max_trials",
    "max_launches",
    "timeout",
    "from_run",
    "label",
}
PATH_KEYS = {"workload", "out", "save", "from_run"}
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


@dataclass(frozen=True)
class ProjectConfig:
    path: Path | None
    tune: dict[str, Any]


def load_project_config(explicit: str | None) -> ProjectConfig:
    path = Path(explicit).resolve() if explicit else Path.cwd() / "simtune.toml"
    if not path.exists():
        if explicit:
            raise ValueError(f"config file does not exist: {path}")
        return ProjectConfig(None, {})
    if not path.is_file():
        raise ValueError(f"config is not a file: {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"invalid config {path}: {exc}") from exc
    if set(data) != {"tune"} or not isinstance(data["tune"], dict):
        raise ValueError("config requires only a [tune] table")
    values = data["tune"]
    unknown = set(values) - TUNE_KEYS
    if unknown:
        raise ValueError(f"unknown [tune] config key: {', '.join(sorted(unknown))}")
    for key, value in values.items():
        if key in PATH_KEYS:
            if not isinstance(value, str) or not value:
                raise ValueError(f"[tune].{key} must be a path string")
            values[key] = str((path.parent / value).resolve())
        elif key in {"budget", "timeout", "label"}:
            if not isinstance(value, str):
                raise ValueError(f"[tune].{key} must be a string")
            if key in {"budget", "timeout"}:
                token = value[:-1] if value[-1:] in {"s", "m", "h"} else value
                try:
                    number = float(token)
                except ValueError as exc:
                    raise ValueError(f"[tune].{key} must be a positive duration") from exc
                if not math.isfinite(number) or number <= 0:
                    raise ValueError(f"[tune].{key} must be a positive duration")
        elif key in {
            "repeat",
            "warmups",
            "seed",
            "confirmation_pairs",
            "max_trials",
            "max_launches",
        }:
            if type(value) is not int:
                raise ValueError(f"[tune].{key} must be an integer")
        elif type(value) not in {int, float}:
            raise ValueError(f"[tune].{key} must be a number")
    if "label" in values and not LABEL.fullmatch(values["label"]):
        raise ValueError("label must be a safe ASCII name of at most 64 characters")
    return ProjectConfig(path, values)
