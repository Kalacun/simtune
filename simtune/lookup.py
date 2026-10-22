"""Rebuildable lookup of local experiment and legacy run records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .experiments import ExperimentStore
from .validation import ValidationError


def find_run(value: str, root: str | Path = "runs") -> Path:
    direct = Path(value).expanduser()
    if direct.is_dir():
        return direct.resolve()
    if direct.exists():
        raise ValidationError(f"not a run directory: {value}")
    matches: list[Path] = []
    base = Path(root)
    if base.is_dir():
        for path in base.iterdir():
            if not path.is_dir() or path.is_symlink():
                continue
            if (path / "experiment.json").is_file():
                try:
                    record = ExperimentStore.open(path).record
                except (OSError, ValueError):
                    continue
                if path.name.startswith(value) or record.id.startswith(value):
                    matches.append(path)
            elif (path / "meta.json").is_file() and path.name.startswith(value):
                matches.append(path)
    if not matches:
        raise ValidationError(f"run ID or path not found: {value}")
    if len(matches) > 1:
        raise ValidationError(
            f"ambiguous run ID {value!r}: {', '.join(sorted(path.name for path in matches))}"
        )
    return matches[0].resolve()


def show_run(path: Path) -> dict[str, Any]:
    if (path / "experiment.json").is_file():
        store = ExperimentStore.open(path)
        record = store.record
        state = record.extensions.get("simtune.stage05", {})
        from .reporting import decision_report

        return {
            "kind": "simtune_show",
            "schema_version": 1,
            "record_kind": "experiment",
            "id": record.id,
            "path": str(path),
            "complete": store.is_complete(),
            "outcome": record.outcome.value,
            "explanation": record.explanation,
            "selected_policy_id": record.selected_policy_id,
            "label": state.get("label"),
            "phase": state.get("phase"),
            "next_index": state.get("next_index"),
            "scheduled_trials": len(state.get("schedule", [])),
            "budget": record.budget.to_dict(),
            "candidates": [item.to_dict() for item in record.candidates],
            "training_trial_ids": record.training_trial_ids,
            "confirmation_trial_ids": record.confirmation_trial_ids,
            "decision_report": decision_report(path),
        }
    try:
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        metrics = json.loads((path / "metrics.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValidationError(f"cannot read run {path}: {exc}") from exc
    return {
        "kind": "simtune_show",
        "schema_version": 1,
        "record_kind": "legacy_run",
        "id": path.name,
        "path": str(path),
        "meta": meta,
        "metrics": metrics,
    }
