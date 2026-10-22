import os
import resource
import sys

import pytest

from simtune import profiles, runner, util


def test_build_action_plan_wrappers():
    profile = {
        "actions": [
            {"type": "ionice", "class": "idle"},
            {"type": "taskset", "cpus": "0-1"},
        ]
    }
    caps = {
        "platform": "linux",
        "is_wsl": False,
        "tools": {
            "ionice": {"path": "/usr/bin/ionice", "available": True},
            "taskset": {"path": "/usr/bin/taskset", "available": True},
            "numactl": {"path": "/usr/bin/numactl", "available": True},
            "systemd-run": {"path": "/usr/bin/systemd-run", "available": True},
        },
        "systemd": {"functional": True, "reason": ""},
    }
    plan = runner.build_action_plan(profile, caps, auto_mode=False)
    wrappers = plan["wrappers"]
    assert wrappers
    assert any("ionice" in wrapper[0] for wrapper in wrappers)
    assert any("taskset" in wrapper[0] for wrapper in wrappers)


def test_taskset_separator_and_restricted_affinity(caps):
    caps["tools"]["taskset"] = {"path": "/usr/bin/taskset", "available": True}
    plan = runner.build_action_plan({"actions": [{"type": "taskset", "cpus": "0-3"}]}, caps, False)
    assert plan["wrappers"] == [["/usr/bin/taskset", "-c", "--", "2,3"]]
    assert plan["adjusted"][0]["applied_as"]["cpus"] == "2,3"
    assert plan["applied"][0]["action"]["cpus"] == "2,3"


def test_taskset_available_and_empty_intersection(caps):
    caps["tools"]["taskset"] = {"path": "taskset", "available": True}
    plan = runner.build_action_plan(
        {"actions": [{"type": "taskset", "cpus": "available"}]}, caps, False
    )
    assert plan["wrappers"][0][-1] == "2,3"
    plan = runner.build_action_plan({"actions": [{"type": "taskset", "cpus": "0-1"}]}, caps, False)
    assert not plan["wrappers"] and plan["skipped"]


def test_wsl_auto_avoids_numa(caps):
    caps["is_wsl"] = True
    caps["tools"]["numactl"] = {"path": "numactl", "available": True}
    profile = {"actions": [{"type": "numactl", "interleave": "all"}]}
    assert runner.build_action_plan(profile, caps, True)["skipped"]
    assert runner.build_action_plan(profile, caps, False)["wrappers"]


def test_unsupported_optional_action_does_not_trigger_fallback(caps):
    data = profiles.load_profiles()
    selected, _, reasons = runner.select_profile(data, "desktop_friendly", caps)
    assert selected == "desktop_friendly" and not reasons
    plan = runner.build_action_plan(data[selected], caps, False)
    assert any(item["action"]["type"] == "ionice" for item in plan["skipped"])


def test_fallback_cycle_is_explicit(caps):
    data = {
        "a": {"actions": [{"type": "taskset", "cpus": "0"}], "fallback_mode": "b"},
        "b": {"actions": [{"type": "ionice"}], "fallback_mode": "a"},
    }
    mode, _, reasons = runner.select_profile(data, "a", caps)
    assert mode == "a" and "cycle" in reasons[-1]


def test_nice_respects_inherited_priority(caps, monkeypatch):
    monkeypatch.setattr(util, "is_root", lambda: False)
    monkeypatch.setattr(os, "getpriority", lambda *a: 10)
    plan = runner.build_action_plan({"actions": [{"type": "nice", "level": -5}]}, caps, False)
    assert plan["adjusted"][0]["applied_as"]["level"] == 10
    assert plan["applied"][0]["action"]["level"] == 10


def test_preexec_does_not_swallow_failures(monkeypatch):
    def denied(*args):
        raise PermissionError("priority denied")

    monkeypatch.setattr(os, "setpriority", denied)
    preexec = runner._build_preexec(0, [])
    with pytest.raises(PermissionError, match="denied"):
        preexec()


def test_unlimited_hard_cap_is_not_negative_cap(monkeypatch):
    monkeypatch.setattr(util, "is_root", lambda: False)
    monkeypatch.setattr(
        resource, "getrlimit", lambda res: (resource.RLIM_INFINITY, resource.RLIM_INFINITY)
    )
    applied, skipped = [], []
    limits = runner._prepare_rlimits(
        {"type": "ulimit", "limits": {"nofile": [1024, 2048]}}, applied, skipped
    )
    assert limits == [(resource.RLIMIT_NOFILE, 1024, 2048)] and not skipped


def test_cannot_raise_finite_hard_cap(monkeypatch):
    monkeypatch.setattr(util, "is_root", lambda: False)
    monkeypatch.setattr(resource, "getrlimit", lambda res: (100, 200))
    applied, skipped = [], []
    assert not runner._prepare_rlimits({"limits": {"nofile": -1}}, applied, skipped)
    assert "hard cap" in skipped[0]["reason"]


def test_systemd_scope_inherits_preexec_settings(caps, monkeypatch):
    caps["tools"]["systemd-run"] = {"path": "systemd-run", "available": True}
    caps["systemd"]["functional"] = True
    monkeypatch.setattr(resource, "getrlimit", lambda res: (1024, resource.RLIM_INFINITY))
    plan = runner.build_action_plan(
        {
            "actions": [
                {"type": "nice", "level": 5},
                {"type": "ulimit", "limits": {"nofile": 512}},
                {"type": "cgroup_systemd", "properties": {"CPUQuota": "80%"}},
            ]
        },
        caps,
        False,
    )
    assert plan["preexec"] is not None
    assert not plan["skipped"]
    assert not any("Nice=" in arg for arg in plan["wrappers"][0])


def test_macos_background_flag(caps):
    caps["platform"] = "macos"
    caps["tools"]["taskpolicy"] = {"path": "taskpolicy", "available": True}
    plan = runner.build_action_plan(
        {"actions": [{"type": "mac_taskpolicy", "policy": "background"}]}, caps, False
    )
    assert plan["wrappers"] == [["taskpolicy", "-b", "--"]]


def test_env_does_not_mutate_parent(caps, monkeypatch):
    monkeypatch.setenv("SIMTUNE_TEST", "parent")
    plan = runner.build_action_plan(
        {"actions": [{"type": "env", "vars": {"SIMTUNE_TEST": "child"}}]}, caps, False
    )
    assert plan["env_overrides"] == {"SIMTUNE_TEST": "child"}
    assert os.environ["SIMTUNE_TEST"] == "parent"


@pytest.mark.parametrize("repeat,timeout", [(0, None), (1, -1), (1, float("nan"))])
def test_runner_validates_api_arguments(caps, tmp_path, repeat, timeout):
    with pytest.raises(ValueError):
        runner.run_command(
            [sys.executable],
            str(tmp_path),
            "throughput",
            profiles.load_profiles(),
            caps,
            timeout,
            repeat,
            True,
            False,
            False,
            False,
        )
    assert not list(tmp_path.iterdir())


def test_scope_disables_argument_expansion(caps):
    caps["systemd"]["functional"] = True
    caps["tools"]["systemd-run"] = {"path": "systemd-run", "available": True}
    plan = runner.build_action_plan(
        {"actions": [{"type": "cgroup_systemd", "properties": {"CPUQuota": "80%"}}]}, caps, False
    )
    assert "--expand-environment=no" in plan["wrappers"][0]
    assert runner._combine_command(plan["wrappers"], ["echo", "$HOME"])[-1] == "$HOME"
