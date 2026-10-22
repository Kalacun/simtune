"""Explicit, provenance-preserving imports of perf stat delimited output."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from .models import Metric, Scope, ToolIdentity
from .validation import ValidationError

_NUMBER = re.compile(r"^[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][+-]?\d+)?$")
_EVENT = re.compile(r"^[A-Za-z0-9_.:/-]+$")
_UNITS = {"", "msec", "usec", "nsec", "seconds", "Joules", "Watts", "GHz"}
_MISSING = {"<not supported>": "not supported", "<not counted>": "not counted"}
_CONTEXT = {
    "kind",
    "schema_version",
    "command",
    "input_identity",
    "host",
    "versions",
    "scope",
    "fixed_env",
    "workload_fingerprint",
    "environment_fingerprint",
}


def _decimal(token: str, field: str) -> float:
    if not _NUMBER.fullmatch(token):
        raise ValidationError(f"{field}: expected one finite ungrouped decimal (dot or comma)")
    value = float(token.replace(",", "."))
    if not math.isfinite(value):
        raise ValidationError(f"{field}: value is not finite")
    return value


def validate_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("perf context must be a JSON object")
    if (
        value.get("kind") != "simtune_perf_stat_context"
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
    ):
        raise ValidationError(
            "perf context requires kind simtune_perf_stat_context and schema_version 1"
        )
    if set(value) != _CONTEXT:
        raise ValidationError(f"perf context fields must be exactly {sorted(_CONTEXT)}")
    if (
        not isinstance(value["command"], list)
        or not value["command"]
        or any(not isinstance(item, str) or not item or "\0" in item for item in value["command"])
    ):
        raise ValidationError("perf context command must be a nonempty argv list")
    for name in ("input_identity", "host"):
        if not isinstance(value[name], str) or not value[name].strip():
            raise ValidationError(f"perf context {name} must be nonempty text")
    if (
        not isinstance(value["versions"], dict)
        or set(value["versions"]) != {"perf"}
        or not isinstance(value["versions"]["perf"], str)
        or not value["versions"]["perf"].strip()
    ):
        raise ValidationError("perf context versions must contain a nonempty perf version")
    try:
        Scope(value["scope"])
    except (ValueError, TypeError) as exc:
        raise ValidationError("perf context scope must be a supported process/job scope") from exc
    if not isinstance(value["fixed_env"], dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value["fixed_env"].items()
    ):
        raise ValidationError("perf context fixed_env must map strings to strings")
    for name in ("workload_fingerprint", "environment_fingerprint"):
        item = value[name]
        if item is not None and (
            not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item)
        ):
            raise ValidationError(f"perf context {name} must be a SHA-256 hex digest or null")
    return value


def parse_perf_stat(raw: bytes, context: dict[str, Any]) -> list[Metric]:
    """Parse perf stat `-x ';' --no-big-num` version 1; preserve every event row."""
    validate_context(context)
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError("perf stat input must be UTF-8") from exc
    tool = ToolIdentity(
        name="perf stat",
        version=context["versions"]["perf"],
        mechanism="external delimited statistics",
    )
    scope = Scope(context["scope"])
    result: list[Metric] = []
    for line_number, line in enumerate(decoded.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = [part.strip() for part in line.split(";")]
        if len(parts) not in {3, 4, 5, 6, 7}:
            raise ValidationError(f"perf stat line {line_number}: expected 3–7 semicolon fields")
        value_token, unit, event = parts[:3]
        if not event or (
            not _EVENT.fullmatch(event)
            and event not in {"seconds time elapsed", "seconds user", "seconds sys"}
        ):
            raise ValidationError(f"perf stat line {line_number}: invalid event name")
        if unit not in _UNITS:
            raise ValidationError(f"perf stat line {line_number}: unsupported unit {unit!r}")
        if len(parts) >= 4 and parts[3]:
            runtime = _decimal(parts[3], f"perf stat line {line_number} runtime")
            if runtime < 0:
                raise ValidationError(f"perf stat line {line_number}: negative runtime")
        running_fraction = None
        scaling = None
        if len(parts) >= 5 and parts[4]:
            percentage = _decimal(
                parts[4].removesuffix("%"), f"perf stat line {line_number} running percentage"
            )
            if not 0 <= percentage <= 100 or (percentage == 0 and value_token not in _MISSING):
                raise ValidationError(f"perf stat line {line_number}: invalid running percentage")
            running_fraction = percentage / 100
            if 0 < percentage < 100:
                scaling = 100 / percentage
        unavailable = {}
        if value_token in _MISSING:
            value = None
            unavailable["value"] = _MISSING[value_token]
        else:
            value = _decimal(value_token, f"perf stat line {line_number} value")
            if value < 0:
                raise ValidationError(f"perf stat line {line_number}: negative counter")
        if scaling is None:
            unavailable["scaling_factor"] = "no multiplex scaling indicated"
        if running_fraction is None:
            unavailable["multiplex_running_fraction"] = "running percentage not supplied"
        result.append(
            Metric(
                id=f"perf-{line_number}",
                name=event,
                value=value,
                unit=unit or "count",
                collector=tool,
                scope=scope,
                raw_source="raw.perf",
                scaling_factor=scaling,
                multiplex_running_fraction=running_fraction,
                unavailable=unavailable,
                extensions={
                    "simtune.perf_stat": {
                        "line": line_number,
                        "fields": parts,
                        "value_is_perf_scaled": scaling is not None,
                    }
                },
            )
        )
    if not result:
        raise ValidationError("perf stat input has no event rows")
    return result


def import_profile(
    input_path: str | Path, context_path: str | Path, out: str | Path
) -> dict[str, Any]:
    raw = Path(input_path).read_bytes()
    context_raw = Path(context_path).read_bytes()
    try:
        context = validate_context(json.loads(context_raw))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"perf context is not valid UTF-8 JSON: {exc}") from exc
    metrics = parse_perf_stat(raw, context)
    record = {
        "kind": "simtune_imported_profile",
        "schema_version": 1,
        "format": "perf-stat-v1",
        "context": context,
        "metrics": [metric.to_dict() for metric in metrics],
        "raw_source": "raw.perf",
        "context_source": "context.json",
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "context_sha256": hashlib.sha256(context_raw).hexdigest(),
        "limitations": [
            "External context is declared by the importer and not independently verified.",
            "Counters do not establish placement or a speedup; fresh paired trials are required.",
        ],
    }
    root = Path(out)
    root.mkdir(parents=True, exist_ok=False)
    (root / "raw.perf").write_bytes(raw)
    (root / "context.json").write_bytes(context_raw)
    (root / "profile.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    return record


def load_imported_profile(root: Path) -> dict[str, Any]:
    record = json.loads((root / "profile.json").read_text(encoding="utf-8"))
    if (
        not isinstance(record, dict)
        or record.get("kind") != "simtune_imported_profile"
        or type(record.get("schema_version")) is not int
        or record["schema_version"] != 1
        or record.get("format") != "perf-stat-v1"
    ):
        raise ValidationError("unsupported imported profile kind, schema version or format")
    if (
        set(record)
        != {
            "kind",
            "schema_version",
            "format",
            "context",
            "metrics",
            "raw_source",
            "context_source",
            "raw_sha256",
            "context_sha256",
            "limitations",
        }
        or record["raw_source"] != "raw.perf"
        or record["context_source"] != "context.json"
        or not isinstance(record["limitations"], list)
        or any(not isinstance(item, str) for item in record["limitations"])
    ):
        raise ValidationError("imported profile has unsupported fields or source references")
    context = validate_context(record.get("context"))
    raw = (root / "raw.perf").read_bytes()
    context_raw = (root / "context.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != record.get("raw_sha256") or hashlib.sha256(
        context_raw
    ).hexdigest() != record.get("context_sha256"):
        raise ValidationError("imported profile raw input or context has changed")
    if json.loads(context_raw) != context or [
        m.to_dict() for m in parse_perf_stat(raw, context)
    ] != record.get("metrics"):
        raise ValidationError("imported profile disagrees with its raw input or context")
    return record
