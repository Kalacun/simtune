"""Scientific validators distinguish checked, failed, and unchecked output."""

import hashlib
import json
import sys
import time
from dataclasses import replace

import pytest

from simtune.checks import run_checks
from simtune.models import CheckKind, OutputCheck, ValidationState


def numeric_check(**overrides):
    values = dict(
        id="numeric",
        method=CheckKind.NUMERIC_JSON,
        output="result.json",
        sha256=None,
        json_field=["science", "value"],
        expected=10.0,
        absolute_tolerance=0.5,
        relative_tolerance=0.0,
        argv=None,
        timeout_seconds=None,
        rationale="Check fixed scientific output.",
        unavailable={
            "sha256": "not used by numeric check",
            "argv": "not used by numeric check",
            "timeout_seconds": "not used by numeric check",
        },
    )
    values.update(overrides)
    return OutputCheck(**values)


def file_check(digest):
    return OutputCheck(
        id="hash",
        method=CheckKind.FILE_HASH,
        output="answer.bin",
        sha256=digest,
        json_field=None,
        expected=None,
        absolute_tolerance=None,
        relative_tolerance=None,
        argv=None,
        timeout_seconds=None,
        rationale="Match the complete output.",
        unavailable={
            name: "not used by hash check"
            for name in (
                "json_field",
                "expected",
                "absolute_tolerance",
                "relative_tolerance",
                "argv",
                "timeout_seconds",
            )
        },
    )


def command_check(argv, timeout=1.0):
    return OutputCheck(
        id="validator",
        method=CheckKind.COMMAND,
        output=None,
        sha256=None,
        json_field=None,
        expected=None,
        absolute_tolerance=None,
        relative_tolerance=None,
        argv=argv,
        timeout_seconds=timeout,
        rationale="Run an explicit validator.",
        unavailable={
            name: "not used by command check"
            for name in (
                "output",
                "sha256",
                "json_field",
                "expected",
                "absolute_tolerance",
                "relative_tolerance",
            )
        },
    )


def run(checks, workspace, evidence, **kwargs):
    return run_checks(
        checks,
        workspace,
        evidence,
        evidence_prefix="trials/t/checks",
        base_env={},
        deadline=None,
        clock=time.monotonic,
        **kwargs,
    )


def test_no_checks_is_explicitly_unchecked(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    report = run([], workspace, tmp_path / "checks")
    assert report.result.state == ValidationState.UNCHECKED
    assert report.result.checks == [] and report.result.evidence == []


def test_hash_pass_mismatch_missing_and_symlink_escape(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    payload = b"fixed output\n"
    (workspace / "answer.bin").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    assert (
        run([file_check(digest)], workspace, tmp_path / "ok").result.state == ValidationState.PASSED
    )
    assert (
        run([file_check("0" * 64)], workspace, tmp_path / "bad").result.state
        == ValidationState.FAILED
    )
    (workspace / "answer.bin").unlink()
    assert (
        run([file_check(digest)], workspace, tmp_path / "missing").result.state
        == ValidationState.FAILED
    )
    outside = tmp_path / "outside.bin"
    outside.write_bytes(payload)
    (workspace / "answer.bin").symlink_to(outside)
    assert (
        run([file_check(digest)], workspace, tmp_path / "escape").result.state
        == ValidationState.FAILED
    )


def test_numeric_tolerance_boundary_and_nonfinite_rejection(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    output = workspace / "result.json"
    output.write_text(json.dumps({"science": {"value": 10.5}}), encoding="utf-8")
    assert (
        run([numeric_check()], workspace, tmp_path / "boundary").result.state
        == ValidationState.PASSED
    )
    output.write_text(json.dumps({"science": {"value": 10.5001}}), encoding="utf-8")
    assert (
        run([numeric_check()], workspace, tmp_path / "outside").result.state
        == ValidationState.FAILED
    )
    output.write_text('{"science":{"value":NaN}}', encoding="utf-8")
    assert (
        run([numeric_check()], workspace, tmp_path / "nan").result.state == ValidationState.FAILED
    )
    output.write_text('{"science":{}}', encoding="utf-8")
    assert (
        run([numeric_check()], workspace, tmp_path / "missing").result.state
        == ValidationState.FAILED
    )


def test_relative_tolerance_and_boolean_are_not_numbers(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    check = replace(numeric_check(), absolute_tolerance=0.0, relative_tolerance=0.1)
    (workspace / "result.json").write_text('{"science":{"value":11}}', encoding="utf-8")
    assert run([check], workspace, tmp_path / "relative").result.state == ValidationState.PASSED
    (workspace / "result.json").write_text('{"science":{"value":true}}', encoding="utf-8")
    assert run([check], workspace, tmp_path / "boolean").result.state == ValidationState.FAILED


def test_validator_runs_once_in_workspace_and_retains_failure(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    calls = []

    def runner(argv, env, timeout, cwd):
        calls.append((argv, env, timeout, cwd))
        return {"return_code": 7, "timed_out": False}, "validator output"

    report = run(
        [command_check(["validator", "{trial_dir}"])],
        workspace,
        tmp_path / "checks",
        command_runner=runner,
    )
    assert report.result.state == ValidationState.FAILED
    assert report.launches_started == 1 and len(calls) == 1
    assert calls[0][0] == ["validator", str(workspace.resolve())]
    assert calls[0][1]["SIMTUNE_TRIAL_DIR"] == str(workspace.resolve())
    evidence = json.loads((tmp_path / "checks" / "validator.json").read_text())
    assert evidence["return_code"] == 7 and evidence["passed"] is False


def test_validator_timeout_and_unknown_placeholder(tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    timeout_check = command_check(
        [sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.02
    )
    report = run([timeout_check], workspace, tmp_path / "timeout")
    assert report.result.state == ValidationState.FAILED
    assert "timed out" in report.outcomes[0].explanation

    placeholder = command_check(["validator", "prefix-{trial_dir}"])
    report = run([placeholder], workspace, tmp_path / "placeholder")
    assert report.result.state == ValidationState.FAILED
    assert report.launches_started == 0


def test_validator_success_uses_trial_cwd_and_explicit_directory(tmp_path):
    workspace = tmp_path / "work space"
    workspace.mkdir()
    check = command_check(
        [
            sys.executable,
            "-c",
            (
                "import os,sys; "
                "raise SystemExit(0 if os.getcwd() == sys.argv[1] "
                "and os.environ['SIMTUNE_TRIAL_DIR'] == sys.argv[1] else 3)"
            ),
            "{trial_dir}",
        ]
    )
    report = run([check], workspace, tmp_path / "checks")
    assert report.result.state == ValidationState.PASSED
    assert report.launches_started == 1


def test_malformed_json_and_evidence_write_failure_never_pass(tmp_path, monkeypatch):
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "result.json").write_text("{", encoding="utf-8")
    assert (
        run([numeric_check()], workspace, tmp_path / "malformed").result.state
        == ValidationState.FAILED
    )

    from simtune import checks as checks_module

    (workspace / "result.json").write_text('{"science":{"value":10}}', encoding="utf-8")
    monkeypatch.setattr(
        checks_module,
        "_atomic_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read-only evidence")),
    )
    with pytest.raises(OSError, match="read-only"):
        run([numeric_check()], workspace, tmp_path / "readonly")
