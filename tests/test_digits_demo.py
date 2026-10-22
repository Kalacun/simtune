"""Synthetic checks for the manual demo; no scientific dependencies or speed claims."""

from copy import deepcopy

from examples.digits_benchmark import equivalent, summarize


def output():
    return {
        "dataset_sha256": "synthetic-dataset",
        "shape": [1797, 64],
        "sklearn_version": "test",
        "checks": "passed",
        "results": [
            {"seed": i, "inertia": 100.0, "labels_sha256": f"synthetic-labels-{i}"}
            for i in range(5)
        ],
    }


def test_equivalence_rejects_changed_science_and_accepts_roundoff():
    reference = output()
    candidate = deepcopy(reference)
    candidate["results"][0]["inertia"] += 1e-8
    assert equivalent(reference, candidate)
    candidate["results"][0]["labels_sha256"] = "wrong-labels"
    assert not equivalent(reference, candidate)
    candidate = deepcopy(reference)
    candidate["results"][0]["inertia"] = float("nan")
    assert not equivalent(reference, candidate)
    candidate = deepcopy(reference)
    candidate["dataset_sha256"] = "different-data"
    assert not equivalent(reference, candidate)
    candidate = deepcopy(reference)
    candidate["checks"] = "failed"
    assert not equivalent(reference, candidate)


def test_summary_never_drops_failed_or_missing_trials():
    rows = [
        {"phase": phase, "pair": pair, "mode": mode, "status": "passed", "wall_seconds": seconds}
        for phase, pair in [("warmup", -1), *[("measurement", i) for i in range(10)]]
        for mode, seconds in [("inherited", 2.0), ("one_thread", 1.0)]
    ]
    assert summarize(rows, 10)["baseline_over_candidate"] == 2.0
    assert summarize(rows[1:], 10)["status"] == "inconclusive"
    assert summarize(rows[:-1], 10)["status"] == "inconclusive"
    rows[2]["status"] = "failed"
    assert "baseline_over_candidate" not in summarize(rows, 10)
    rows[2]["status"] = "passed"
    rows[3]["mode"] = "inherited"
    assert summarize(rows, 10)["status"] == "inconclusive"
