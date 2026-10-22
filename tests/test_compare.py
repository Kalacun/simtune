import csv
import json

import pytest

from simtune import compare


def write_run(path, iterations, meta=None):
    path.mkdir()
    (path / "meta.json").write_text(json.dumps(meta or {"mode": "baseline"}))
    (path / "metrics.json").write_text(
        json.dumps({"iterations": iterations, "aggregate": {"wall_time_sec_mean": 0.01}})
    )
    return str(path)


def test_legacy_failures_are_not_misrepresented(tmp_path):
    baseline = write_run(tmp_path / "baseline", [{"wall_time_sec": 2, "return_code": 0}])
    failed = write_run(tmp_path / "failed", [{"wall_time_sec": 0.01, "return_code": 127}])
    table, rows = compare.compare_runs([baseline, failed], tmp_path / "exports")
    assert rows[0]["wall_time_sec_mean"] == 2
    assert rows[1]["wall_time_sec_mean"] is None
    assert rows[1]["status"] == "failed" and "failed" in table
    assert json.loads((tmp_path / "exports" / "compare.json").read_text()) == rows


def test_csv_formula_escaping(tmp_path):
    run = write_run(
        tmp_path / "r", [{"return_code": 0, "wall_time_sec": 1}], {"mode": "=FORMULA()"}
    )
    compare.compare_runs([run], tmp_path)
    with (tmp_path / "compare.csv").open() as handle:
        row = next(csv.DictReader(handle))
    assert row["mode"] == "'=FORMULA()"


def test_bad_schema_and_missing_status(tmp_path):
    future = write_run(tmp_path / "future", [], {"schema_version": 99})
    with pytest.raises(ValueError, match="schema version"):
        compare.compare_runs([future], tmp_path)
    unknown = write_run(tmp_path / "unknown", [{"wall_time_sec": 1}])
    with pytest.raises(ValueError, match="return_code"):
        compare.compare_runs([unknown], tmp_path)
    assert not (tmp_path / "compare.json").exists()


def test_partial_success_is_visible(tmp_path):
    run = write_run(
        tmp_path / "partial",
        [{"wall_time_sec": 1, "return_code": 0}, {"wall_time_sec": 0.01, "return_code": 1}],
    )
    _, rows = compare.compare_runs([run], tmp_path)
    assert rows[0]["wall_time_sec_mean"] == 1
    assert rows[0]["status"] == "failed" and rows[0]["failed_count"] == 1


def test_preserved_historical_failure_fixture(tmp_path):
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "legacy_taskset_failure"
    _, rows = compare.compare_runs([str(fixture)], tmp_path)
    assert rows[0]["failed_count"] == 1
    assert rows[0]["wall_time_sec_mean"] is None
