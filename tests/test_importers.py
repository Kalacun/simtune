"""Hand-written synthetic perf stat rows; no native counter claim."""

import json
import sys

import pytest

from simtune import cli, profiler
from simtune.candidates import _profile_order
from simtune.collectors import parse_live_perf, probe
from simtune.experiments import _rusage_metrics
from simtune.importers import import_profile, load_imported_profile, parse_perf_stat
from simtune.search import search_capabilities, search_snapshot
from simtune.tune import bare_workload
from simtune.validation import ValidationError
from simtune.workloads import environment_fingerprint


def context(**overrides):
    value = {
        "kind": "simtune_perf_stat_context",
        "schema_version": 1,
        "command": [sys.executable, "-c", "pass"],
        "input_identity": "synthetic-input",
        "host": "synthetic-host",
        "versions": {"perf": "perf version synthetic"},
        "scope": "process_tree",
        "fixed_env": {},
        "workload_fingerprint": None,
        "environment_fingerprint": None,
    }
    value.update(overrides)
    return value


def test_perf_rows_preserve_duplicates_availability_units_and_scaling():
    raw = (
        b"# synthetic perf stat\n1,25;msec;task-clock;1000000;50.00;;\n"
        b"<not counted>;;cycles;1000000;25.00\n"
        b"<not supported>;;cycles\n100;;instructions\n"
        b"2,5;seconds;seconds time elapsed\n"
    )
    rows = parse_perf_stat(raw, context())
    assert len(rows) == 5
    assert [row.name for row in rows].count("cycles") == 2
    assert rows[0].value == 1.25 and rows[0].unit == "msec"
    assert rows[0].multiplex_running_fraction == 0.5
    assert rows[0].scaling_factor == 2
    assert rows[1].value is None and rows[1].unavailable["value"] == "not counted"
    assert rows[2].value is None and rows[2].unavailable["value"] == "not supported"
    assert rows[0].extensions["simtune.perf_stat"]["value_is_perf_scaled"]
    assert rows[0].to_dict()["scope"] == "process_tree"
    assert rows[0].from_dict(rows[0].to_dict()) == rows[0]


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"# only comment\n",
        b"1;cycles\n",
        b"1;;cycles;bad\n",
        b"1;bananas;cycles\n",
        b"nan;;cycles\n",
        b"1e999;;cycles\n",
        b"1;;cycles;;0\n",
        b"1;;cycles;;101\n",
        b"1;;cycles;1;25;extra;too;many\n",
        b"1;;cycles\npartial\n",
        b"1;;cycles\xff\n",
    ],
)
def test_bad_perf_is_rejected(raw):
    with pytest.raises(ValidationError):
        parse_perf_stat(raw, context())


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 2},
        {"scope": "whole-cluster"},
        {"command": "python"},
        {"versions": {}},
        {"fixed_env": {"A": 2}},
        {"environment_fingerprint": "not-a-digest"},
    ],
)
def test_bad_context_is_rejected(change):
    with pytest.raises(ValidationError):
        parse_perf_stat(b"1;;cycles\n", context(**change))


def test_import_cli_retains_exact_bytes_and_detects_tampering(tmp_path, capsys):
    source = tmp_path / "source.perf"
    source.write_bytes(b"# synthetic\r\n1,5;msec;task-clock\r\n")
    ctx = tmp_path / "source.json"
    ctx.write_text(json.dumps(context()), encoding="utf-8")
    out = tmp_path / "imported"
    assert (
        cli.main(
            [
                "import-profile",
                "--format",
                "perf-stat",
                "--input",
                str(source),
                "--context",
                str(ctx),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    assert "Imported 1" in capsys.readouterr().out
    record = load_imported_profile(out)
    assert record["metrics"][0]["raw_source"] == "raw.perf"
    assert (out / "raw.perf").read_bytes() == source.read_bytes()
    assert (out / "context.json").read_bytes() == ctx.read_bytes()
    (out / "raw.perf").write_bytes(b"2;;cycles\n")
    with pytest.raises(ValidationError, match="changed"):
        load_imported_profile(out)


def test_bad_import_leaves_no_output(tmp_path):
    source = tmp_path / "source.perf"
    source.write_bytes(b"1;unknown-unit;cycles\n")
    ctx = tmp_path / "source.json"
    ctx.write_text(json.dumps(context()), encoding="utf-8")
    out = tmp_path / "imported"
    with pytest.raises(ValidationError, match="unit"):
        import_profile(source, ctx, out)
    assert not out.exists()


def test_wrong_import_version_rejected(tmp_path):
    source = tmp_path / "source.perf"
    source.write_bytes(b"1;;cycles\n")
    ctx = tmp_path / "source.json"
    ctx.write_text(json.dumps(context()), encoding="utf-8")
    out = tmp_path / "imported"
    import_profile(source, ctx, out)
    record = json.loads((out / "profile.json").read_text())
    record["schema_version"] = 2
    (out / "profile.json").write_text(json.dumps(record))
    with pytest.raises(ValidationError, match="unsupported"):
        load_imported_profile(out)


def test_import_record_cannot_redirect_raw_reference(tmp_path):
    source = tmp_path / "source.perf"
    source.write_bytes(b"1;;cycles\n")
    ctx = tmp_path / "source.json"
    ctx.write_text(json.dumps(context()), encoding="utf-8")
    out = tmp_path / "imported"
    import_profile(source, ctx, out)
    record = json.loads((out / "profile.json").read_text())
    record["raw_source"] = "../other"
    (out / "profile.json").write_text(json.dumps(record))
    with pytest.raises(ValidationError, match="source references"):
        load_imported_profile(out)


def test_imported_hint_only_orders_after_exact_fingerprints_and_unscaled_values():
    ctx = context(workload_fingerprint="a" * 64, environment_fingerprint="b" * 64)
    rows = parse_perf_stat(b"2000;msec;task-clock\n1;seconds;seconds time elapsed\n", ctx)
    hint = {
        "kind": "simtune_imported_profile",
        "workload_fingerprint": "a" * 64,
        "environment_fingerprint": "b" * 64,
        "metrics": [row.to_dict() for row in rows],
    }
    order, note = _profile_order(hint, "a" * 64, "b" * 64)
    assert order[0].value == "spread" and "fresh paired" in note
    assert "ignored" in _profile_order(hint, "a" * 64, "c" * 64)[1]
    hint["metrics"][0]["scaling_factor"] = 2
    assert "deterministic" in _profile_order(hint, "a" * 64, "b" * 64)[1]


def test_incompatible_import_rejected_before_search(tmp_path, capsys):
    source = tmp_path / "source.perf"
    source.write_bytes(b"1;;cycles\n")
    ctx = tmp_path / "source.json"
    ctx.write_text(json.dumps(context()), encoding="utf-8")
    out = tmp_path / "imported"
    import_profile(source, ctx, out)
    target = tmp_path / "search"
    status = cli.main(
        [
            "tune",
            "--budget",
            "1m",
            "--dry-run",
            "--out",
            str(target),
            "--from-run",
            str(out),
            "--",
            sys.executable,
            "-c",
            "pass",
        ]
    )
    assert status == 2 and not target.exists()
    assert "do not match" in capsys.readouterr().err


def test_compatible_import_only_orders_fresh_dry_run(tmp_path, capsys):
    argv = [sys.executable, "-c", "pass"]
    loaded = bare_workload(argv, timeout=60, max_trials=100, max_launches=300)
    snapshot = search_snapshot()
    fingerprint = environment_fingerprint(loaded, snapshot, search_capabilities(snapshot))
    ctx = context(
        command=loaded.spec.argv_template,
        input_identity=loaded.workload_fingerprint,
        workload_fingerprint=loaded.workload_fingerprint,
        environment_fingerprint=fingerprint,
    )
    source = tmp_path / "source.perf"
    source.write_bytes(b"2000;msec;task-clock\n1;seconds;seconds time elapsed\n")
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps(ctx), encoding="utf-8")
    imported = tmp_path / "imported"
    import_profile(source, context_path, imported)
    target = tmp_path / "fresh-search"
    assert (
        cli.main(
            [
                "tune",
                "--budget",
                "1m",
                "--dry-run",
                "--out",
                str(target),
                "--from-run",
                str(imported),
                "--",
                *argv,
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert not target.exists()
    assert "fresh paired" in " ".join(result["state"]["notes"])
    assert result["budget"]["trials_started"] == 0


def test_perf_probe_missing_is_optional_and_no_workload_retry(caps, monkeypatch):
    assert probe("timing", caps).name == "timing"
    with pytest.raises(ValidationError, match="unavailable"):
        probe("perf-stat", caps)
    usage = profiler._rusage_snapshot()
    calls = []

    def fail(command, *args):
        calls.append(command)
        return 1, "STDERR: perf denied", False, 0.1, usage, usage, None, {}

    monkeypatch.setattr(profiler, "_run_command", fail)
    result, raw = profiler.collect_perf_command(["workload"], {}, 1, "perf", "test")
    assert len(calls) == 1 and result["return_code"] == 1
    assert not result["perf_fallback"] and "perf denied" in raw


def test_successful_perf_with_absent_counter_keeps_fixed_collector_set():
    rows = parse_live_perf(
        b"1,5;msec;task-clock;100;50\n<not counted>;;cycles;100;0\n",
        version="synthetic perf",
    )
    assert [row["name"] for row in rows] == ["task-clock", "cycles", "instructions"]
    assert rows[1]["value"] is None and rows[1]["multiplex_running_fraction"] == 0
    assert rows[2]["value"] is None and "absent" in rows[2]["unavailable"]["value"]


def test_rusage_metrics_label_cpu_and_unavailable_rss():
    rows = _rusage_metrics(
        "trial", {"user_time_sec": 1.5, "sys_time_sec": 0.2, "max_rss_kb": None}, "launch.txt"
    )
    assert [row.name for row in rows] == ["user_cpu_time", "system_cpu_time", "max_rss"]
    assert rows[0].unit == "seconds" and rows[0].value == 1.5
    assert rows[2].value is None and "lifetime" in rows[2].unavailable["value"]
    assert all(row.scope.value == "process_tree" and row.raw_source == "launch.txt" for row in rows)
