"""Compare run directories."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from . import metrics as metric_tools
from . import storage, util


def _load_run(run_dir: str) -> dict[str, Any]:
    path = Path(run_dir)
    meta = util.load_json(path / "meta.json")
    metrics = util.load_json(path / "metrics.json")
    if not isinstance(meta, dict):
        raise ValueError(f"{path}: meta.json must be an object")
    version = meta.get("schema_version", 1)
    if type(version) is not int or version not in {1, storage.SCHEMA_VERSION}:
        raise ValueError(f"{path}: unsupported schema version {version!r}")
    storage.validate_metrics(metrics)
    # Legacy aggregates averaged failed commands too; recompute from raw samples.
    iterations = metrics["iterations"]
    if any(
        type(item.get("return_code")) is not int
        or ("timed_out" in item and type(item["timed_out"]) is not bool)
        for item in iterations
    ):
        raise ValueError(
            f"{path}: every iteration requires an integer return_code and boolean timed_out"
        )
    aggregate = metric_tools.aggregate_metrics(iterations) if iterations else {}
    status = "unknown" if not iterations else ("failed" if aggregate["failed_count"] else "ok")
    row = {
        "run": str(path),
        "mode": meta.get("mode"),
        "status": status,
        "successful_count": aggregate.get("successful_count", 0),
        "failed_count": aggregate.get("failed_count", 0),
        "timed_out_count": aggregate.get("timed_out_count", 0),
        "wall_time_sec_mean": aggregate.get("wall_time_sec_mean"),
        "wall_time_sec_median": aggregate.get("wall_time_sec_median"),
        "user_time_sec_mean": aggregate.get("user_time_sec_mean"),
        "sys_time_sec_mean": aggregate.get("sys_time_sec_mean"),
        "max_rss_kb_mean": aggregate.get("max_rss_kb_mean"),
    }
    row.update({f"meta_{k}": v for k, v in meta.items() if k not in row})
    return row


def compare_runs(
    run_dirs: list[str], out_dir: str | Path = "."
) -> tuple[str, list[dict[str, Any]]]:
    if not run_dirs:
        raise ValueError("provide at least one run directory")
    rows = [_load_run(run_dir) for run_dir in run_dirs]

    table_rows = [
        {
            "run": row.get("run"),
            "mode": row.get("mode"),
            "status": row["status"],
            "failed": row["failed_count"],
            "wall_mean": row.get("wall_time_sec_mean"),
            "wall_median": row.get("wall_time_sec_median"),
        }
        for row in rows
    ]
    table = util.render_table(
        table_rows, ["run", "mode", "status", "failed", "wall_mean", "wall_median"]
    )

    output = util.ensure_dir(out_dir)
    json_path = output / "compare.json"
    csv_path = output / "compare.csv"
    util.write_json(json_path, rows)

    fieldnames = sorted({key for row in rows for key in row.keys()})
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_cell(value) for key, value in row.items()})

    return table, rows


def _csv_cell(value: Any) -> Any:
    """Prevent spreadsheet formula execution from untrusted metadata/paths."""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value
