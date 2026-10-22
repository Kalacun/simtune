from simtune import heuristics


def test_heuristics_cpu_bound():
    metrics = {"wall_time_sec": 10, "user_time_sec": 8, "sys_time_sec": 1}
    caps = {"platform": "linux", "is_wsl": False, "tools": {}, "systemd": {"functional": False}}
    decision = heuristics.select_mode(metrics, caps, False, False)
    assert decision["mode"] == "throughput"


def test_heuristics_io_bound_with_cgroup():
    metrics = {"wall_time_sec": 10, "user_time_sec": 1, "sys_time_sec": 1, "in_block": 2000}
    caps = {"platform": "linux", "is_wsl": False, "tools": {}, "systemd": {"functional": True}}
    decision = heuristics.select_mode(metrics, caps, False, False)
    assert decision["mode"] == "isolated"


def test_heuristics_latency_flag():
    metrics = {"wall_time_sec": 5, "user_time_sec": 2, "sys_time_sec": 1}
    caps = {"platform": "linux", "is_wsl": False, "tools": {}, "systemd": {"functional": True}}
    decision = heuristics.select_mode(metrics, caps, True, False)
    assert decision["mode"] == "latency"


def test_empty_baseline_defaults_conservatively(caps):
    result = heuristics.select_mode({}, caps, False, False)
    assert result["mode"] == "desktop_friendly"
    assert result["inputs"]["cpu_ratio"] == 0


def test_parallel_cpu_ratio_can_exceed_one(caps):
    result = heuristics.select_mode({"wall_time_sec": 1, "user_time_sec": 2}, caps, False, False)
    assert result["mode"] == "throughput" and result["inputs"]["cpu_ratio"] == 2


def test_flags_have_documented_precedence(caps):
    assert heuristics.select_mode({}, caps, True, True)["mode"] == "latency"
    assert heuristics.select_mode({}, caps, False, True)["mode"] == "desktop_friendly"
