"""Metrics normalization and aggregation."""

from __future__ import annotations

import math
from statistics import mean, median, stdev
from typing import Any


def normalize_metrics(raw: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(raw)
    rusage = raw.get("rusage") or {}
    time_tool = raw.get("time_tool") or {}

    def pick_value(raw_value: Any, tool_value: Any, fallback_value: Any) -> Any:
        if raw_value is not None:
            return raw_value
        if tool_value is not None:
            return tool_value
        return fallback_value

    normalized["user_time_sec"] = pick_value(
        raw.get("user_time_sec"), time_tool.get("user_time_sec"), rusage.get("user_time_sec")
    )
    normalized["sys_time_sec"] = pick_value(
        raw.get("sys_time_sec"), time_tool.get("sys_time_sec"), rusage.get("sys_time_sec")
    )
    normalized["max_rss_kb"] = pick_value(
        raw.get("max_rss_kb"), time_tool.get("max_rss_kb"), rusage.get("max_rss_kb")
    )

    normalized["minor_faults"] = pick_value(
        raw.get("minor_faults"), None, rusage.get("minor_faults")
    )
    normalized["major_faults"] = pick_value(
        raw.get("major_faults"), None, rusage.get("major_faults")
    )
    normalized["in_block"] = pick_value(raw.get("in_block"), None, rusage.get("in_block"))
    normalized["out_block"] = pick_value(raw.get("out_block"), None, rusage.get("out_block"))
    normalized["voluntary_ctx"] = pick_value(
        raw.get("voluntary_ctx"), None, rusage.get("voluntary_ctx")
    )
    normalized["involuntary_ctx"] = pick_value(
        raw.get("involuntary_ctx"), None, rusage.get("involuntary_ctx")
    )

    return normalized


def _numeric_fields(metrics: dict[str, Any]) -> dict[str, float]:
    numeric: dict[str, float] = {}
    for key, value in metrics.items():
        if key in {"return_code", "iteration"} or isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)) and math.isfinite(value):
            numeric[key] = float(value)
    return numeric


def iteration_succeeded(item: dict[str, Any]) -> bool:
    """Absent status is accepted for legacy metric-only API input."""
    return item.get("return_code", 0) == 0 and not item.get("timed_out", False)


def aggregate_metrics(metrics_list: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate finite measurements from successful iterations only.

    Counts reveal missing data. Sample standard deviation is zero for a single
    sample for compatibility; it does not establish measurement certainty.
    """
    if not metrics_list:
        return {}
    successful = [item for item in metrics_list if iteration_succeeded(item)]
    aggregate: dict[str, Any] = {
        "iteration_count": len(metrics_list),
        "successful_count": len(successful),
        "failed_count": len(metrics_list) - len(successful),
        "timed_out_count": sum(bool(item.get("timed_out")) for item in metrics_list),
    }
    numeric_fields: dict[str, list[float]] = {}
    for item in successful:
        for key, value in _numeric_fields(item).items():
            numeric_fields.setdefault(key, []).append(value)
    for key, values in numeric_fields.items():
        aggregate[f"{key}_count"] = len(values)
        aggregate[f"{key}_min"] = min(values)
        aggregate[f"{key}_median"] = median(values)
        aggregate[f"{key}_mean"] = mean(values)
        aggregate[f"{key}_stdev"] = stdev(values) if len(values) > 1 else 0.0
    return aggregate


def exit_status(iterations: list[dict[str, Any]]) -> int:
    """Return the first failure, using shell conventions for signals/timeouts."""
    for item in iterations:
        if item.get("timed_out"):
            return 124
        code = item.get("return_code", 0)
        if code:
            return 128 - code if code < 0 else code
    return 0
