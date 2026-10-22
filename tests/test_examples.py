from examples.benchmark_report import render_report


def test_report_refuses_failure_speedup():
    report = render_report(
        {"iterations": [{"return_code": 0, "wall_time_sec": 3}]},
        {"iterations": [{"return_code": 127, "wall_time_sec": 0.05}]},
        None,
        {},
        {},
    )
    assert "ratio unavailable" in report
    assert "60.000x" not in report


def test_report_shows_ratio_as_observation():
    report = render_report(
        {"iterations": [{"return_code": 0, "wall_time_sec": 2}]},
        {"iterations": [{"return_code": 0, "wall_time_sec": 1}]},
        None,
        {},
        {},
    )
    assert "Observed wall-time ratio" in report and "2.000x" in report
