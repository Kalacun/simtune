"""Synthetic hosts keep tests independent of installed OS tools and privileges."""

import pytest


@pytest.fixture
def caps():
    return {
        "platform": "linux",
        "is_wsl": False,
        "tools": {},
        "systemd": {"functional": False},
        "perf": {"functional": False},
        "cpu": {"logical_count": 4, "affinity": [2, 3]},
    }


@pytest.fixture
def cli_host(monkeypatch, caps):
    from simtune import capabilities, util

    monkeypatch.setattr(capabilities, "detect_capabilities", lambda: caps)
    monkeypatch.setattr(util, "run_git_info", lambda: {})
    return caps
