from pathlib import Path

import pytest

from simtune import profiles, util


def test_load_profiles_has_defaults():
    data = profiles.load_profiles()
    assert "desktop_friendly" in data
    assert "throughput" in data
    assert "isolated" in data
    assert "latency" in data


def test_list_modes_required_caps():
    data = profiles.load_profiles()
    rows = profiles.list_modes(data)
    names = {row["name"] for row in rows}
    assert "throughput" in names
    row = next(row for row in rows if row["name"] == "throughput")
    assert "taskset" in row["required"]


@pytest.mark.parametrize(
    "action",
    [
        None,
        {},
        {"type": []},
        {"type": "typo"},
        {"type": "nice", "level": True},
        {"type": "nice", "level": 20},
        {"type": "nice", "level": -21},
        {"type": "nice", "level": 1.5},
        {"type": "nice", "level": 1, "optional": "false"},
        {"type": "nice", "level": 1, "levle": 2},
        {"type": "ionice", "class": "bogus"},
        {"type": "ionice", "level": 8},
        {"type": "taskset", "cpus": "3-1"},
        {"type": "taskset", "cpus": []},
        {"type": "taskset", "cpus": "0-999999999"},
        {"type": "taskset", "cpus": [False]},
        {"type": "env", "vars": {"A=B": "x"}},
        {"type": "env", "vars": {"A": []}},
        {"type": "env", "vars": {"A": "x\0"}},
        {"type": "ulimit", "limits": {"unknown": 1}},
        {"type": "ulimit", "limits": {"nofile": [5, 4]}},
        {"type": "ulimit", "limits": {"nofile": [-1, 4]}},
        {"type": "ulimit", "limits": {"nofile": -2}},
        {"type": "cgroup_systemd", "properties": []},
        {"type": "numactl", "args": "--interleave=all"},
        {"type": "mac_taskpolicy", "policy": "invalid"},
    ],
)
def test_action_validation(action):
    assert profiles.validate_action(action)


@pytest.mark.parametrize(
    "data", [None, [], {}, {"x": None}, {"../escape": {}}, {"x": {"actions": "wrong"}}]
)
def test_profile_shapes(data):
    assert profiles.validate_profiles(data)


def test_unknown_fallback():
    data = profiles.load_profiles()
    data["latency"]["fallback_mode"] = "missing"
    assert any("unknown fallback" in e for e in profiles.validate_profiles(data))


def test_defaults_are_independent_and_valid():
    data = profiles.load_profiles()
    assert not profiles.validate_profiles(data)
    data["throughput"]["actions"].clear()
    assert profiles.load_profiles()["throughput"]["actions"]


def test_explicit_profiles_need_yaml(monkeypatch):
    monkeypatch.setattr(util, "optional_import", lambda name: None)
    with pytest.raises(ValueError, match="PyYAML"):
        profiles.load_profiles(["missing.yaml"])
    assert profiles.load_profiles()


def test_yaml_errors_and_missing_file(tmp_path):
    pytest.importorskip("yaml")
    with pytest.raises(FileNotFoundError):
        profiles.load_profiles([str(tmp_path / "missing.yaml")])
    for text in ("[]", "", "[invalid"):
        path = tmp_path / "bad.yaml"
        path.write_text(text)
        with pytest.raises(ValueError):
            profiles.load_profiles([str(path)])


def test_documented_default_yaml_matches_builtins():
    pytest.importorskip("yaml")
    path = Path(__file__).resolve().parents[1] / "profiles" / "default.yaml"
    assert profiles.load_profiles([str(path)]) == profiles.load_profiles()


def test_valid_limits_and_cpus():
    assert not profiles.validate_action({"type": "ulimit", "limits": {"nofile": [1024, -1]}})
    assert profiles.parse_cpu_list("0-2,5,2") == {0, 1, 2, 5}
    assert profiles.parse_cpu_list([3, 5]) == {3, 5}


def test_numa_node_zero_is_a_valid_binding():
    assert not profiles.validate_action({"type": "numactl", "membind": 0})
