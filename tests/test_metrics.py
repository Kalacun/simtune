import math

import pytest

from simtune import metrics


def test_normalization_precedence_zero_and_idempotence():
    raw = {
        "user_time_sec": 0,
        "time_tool": {"user_time_sec": 1, "sys_time_sec": 2},
        "rusage": {"user_time_sec": 3, "sys_time_sec": 4, "in_block": 5},
    }
    result = metrics.normalize_metrics(raw)
    assert result["user_time_sec"] == 0 and result["sys_time_sec"] == 2
    assert result["in_block"] == 5
    assert metrics.normalize_metrics(result) == result
    assert metrics.normalize_metrics({"in_block": 7})["in_block"] == 7


def test_aggregation_filters_failures_nonfinite_and_status_fields():
    agg = metrics.aggregate_metrics(
        [
            {"wall_time_sec": 2, "user_time_sec": float("inf"), "return_code": 0, "iteration": 1},
            {"wall_time_sec": 4, "user_time_sec": float("nan"), "return_code": 0, "iteration": 2},
            {"wall_time_sec": 0.01, "return_code": 127},
            {"wall_time_sec": 0.1, "return_code": 124, "timed_out": True},
        ]
    )
    assert agg["wall_time_sec_mean"] == 3
    assert agg["wall_time_sec_median"] == 3
    assert agg["wall_time_sec_min"] == 2
    assert agg["wall_time_sec_stdev"] == pytest.approx(math.sqrt(2))
    assert agg["wall_time_sec_count"] == 2
    assert agg["failed_count"] == 2 and agg["timed_out_count"] == 1
    assert not any(
        key.startswith(("return_code_", "iteration_", "user_time_sec_"))
        for key in agg
        if key != "iteration_count"
    )


def test_empty_singleton_and_all_failed():
    assert metrics.aggregate_metrics([]) == {}
    assert metrics.aggregate_metrics([{"wall_time_sec": 1}])["wall_time_sec_stdev"] == 0
    assert "wall_time_sec_mean" not in metrics.aggregate_metrics(
        [{"wall_time_sec": 0.01, "return_code": 2}]
    )


@pytest.mark.parametrize(
    "item,code",
    [
        ({"return_code": -15}, 143),
        ({"return_code": 3}, 3),
        ({"return_code": 0}, 0),
        ({"timed_out": True}, 124),
    ],
)
def test_exit_status(item, code):
    assert metrics.exit_status([item]) == code
