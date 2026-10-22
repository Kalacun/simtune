import subprocess

import pytest

from simtune import capabilities, util
from simtune.models import ResourceSnapshot


def test_action_capabilities_linux_tools():
    fake = {
        "platform": "linux",
        "is_wsl": False,
        "tools": {
            "ionice": {"available": True},
            "taskset": {"available": True},
            "numactl": {"available": True},
            "systemd-run": {"available": True},
        },
        "systemd": {"functional": True, "reason": ""},
    }
    rows = capabilities.action_capabilities(fake)
    support = {row["action"]: row["supported"] for row in rows}
    assert support["ionice"] == "yes"
    assert support["taskset"] == "yes"
    assert support["numactl"] == "yes"
    assert support["cgroup_systemd"] == "yes"


@pytest.mark.parametrize("platform_name", ["linux", "macos", "other"])
def test_platform_gating(platform_name, caps):
    caps["platform"] = platform_name
    caps["tools"] = {
        name: {"available": True} for name in ["ionice", "taskset", "numactl", "taskpolicy"]
    }
    support = capabilities.supported_action_map(caps)
    assert support["taskset"] == (platform_name == "linux")
    assert support["mac_taskpolicy"] == (platform_name == "macos")
    assert support["nice"] == (platform_name in {"linux", "macos"})


def test_absent_tools_are_not_probed(monkeypatch):
    monkeypatch.setattr(util, "which", lambda name: None)
    monkeypatch.setattr(util, "run_command", lambda *a, **kw: pytest.fail("probe"))
    caps = capabilities.detect_capabilities()
    assert not caps["perf"]["functional"]
    assert not caps["systemd"]["functional"]


def test_failed_functional_probe():
    for func in (capabilities._detect_systemd, capabilities._detect_perf):
        assert not func({"available": False})["functional"]


def test_probe_failures_are_readable(monkeypatch):
    monkeypatch.setattr(
        util.subprocess,
        "run",
        lambda *a, **kw: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("tool", 1, output=b"partial", stderr=b"late")
        ),
    )
    result = util.run_command(["tool"], timeout=1)
    assert result.returncode == 124 and result.stdout == "partial"
    result = capabilities._detect_perf({"available": True, "path": "tool"})
    assert not result["functional"] and result["reason"] == "late"


@pytest.mark.parametrize(
    "error,code", [(FileNotFoundError("gone"), 127), (PermissionError("denied"), 126)]
)
def test_disappearing_probe_tools(monkeypatch, error, code):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(util.subprocess, "run", fail)
    result = util.run_command(["tool"])
    assert result.returncode == code
    assert result.stderr


def test_wsl_environment_and_kernel_detection(monkeypatch):
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.setattr(util, "read_first_line", lambda path: "Linux MICROSOFT-standard-WSL2")
    assert util.is_wsl()
    monkeypatch.setattr(util, "read_first_line", lambda path: "native Linux")
    assert not util.is_wsl()
    monkeypatch.setenv("WSL_DISTRO_NAME", "test")
    assert util.is_wsl()


def test_macos_does_not_probe_linux_tools(monkeypatch):
    monkeypatch.setattr(util, "detect_platform", lambda: "macos")
    seen = []
    monkeypatch.setattr(util, "which", lambda name: seen.append(name))
    capabilities.detect_capabilities()
    assert seen == ["time", "taskpolicy"]


def test_time_probe_failure_disables_wrapper(monkeypatch):
    monkeypatch.setattr(util, "detect_platform", lambda: "macos")
    monkeypatch.setattr(util, "which", lambda name: "/tool/time" if name == "time" else None)
    monkeypatch.setattr(
        util, "run_command", lambda *a, **kw: util.CommandResult(1, "", "unsupported flags")
    )
    snapshot = capabilities.detect_capabilities()
    assert snapshot["tools"]["time"]["available"]
    assert not snapshot["tools"]["time"]["functional"]
    assert snapshot["tools"]["time"]["reason"] == "unsupported flags"


def test_resource_snapshot_is_additive_and_visible_in_doctor(monkeypatch):
    monkeypatch.setattr(util, "detect_platform", lambda: "macos")
    monkeypatch.setattr(util, "which", lambda _name: None)
    detected = capabilities.detect_capabilities()
    # Existing consumers retain the original lightweight fields.
    assert set(detected["cpu"]) == {"logical_count", "affinity"}
    resource = ResourceSnapshot.from_dict(detected["resource_snapshot"])
    assert resource.platform.value == "macos"
    assert resource.effective_cpu_ids is None
    report = capabilities.doctor_report(detected)
    assert "CPU:" in report
    assert "Resource snapshot:" in report
    assert "Effective CPUs: None" in report
