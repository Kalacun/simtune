"""Explicit scientific-output checks for isolated trial workspaces."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import profiler, util
from .models import CheckKind, OutputCheck, ValidationResult, ValidationState
from .validation import ValidationError, artifact_path, load_json
from .workloads import sha256_file

Clock = Callable[[], float]
CommandRunner = Callable[
    [list[str], dict[str, str], float | None, Path], tuple[dict[str, Any], str]
]


@dataclass(frozen=True)
class CheckOutcome:
    check_id: str
    passed: bool
    explanation: str
    evidence: str
    launched: bool = False
    elapsed_seconds: float = 0.0


@dataclass(frozen=True)
class ValidationReport:
    result: ValidationResult
    outcomes: tuple[CheckOutcome, ...]
    launches_started: int
    spent_seconds: float


def _atomic_json(path: Path, value: Any) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _evidence_path(directory: Path, check_id: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{check_id}.json"
    # Reject a pre-created symlink before writing check evidence.
    if path.is_symlink():
        raise ValidationError(f"check evidence path is a symlink: {path}")
    return path


def _json_number(document: Any, fields: Sequence[str]) -> float:
    value = document
    for field in fields:
        if type(value) is not dict or field not in value:
            raise ValidationError(f"numeric JSON field {list(fields)!r} is missing at {field!r}")
        value = value[field]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"numeric JSON field {list(fields)!r} is not a number")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValidationError(f"numeric JSON field {list(fields)!r} is nonfinite")
    return float(value)


def _command_argv(argv: list[str], workspace: Path) -> list[str]:
    resolved = []
    for argument in argv:
        if argument == "{trial_dir}":
            resolved.append(str(workspace))
        elif "{" in argument or "}" in argument:
            raise ValidationError(
                "validator argv permits only the whole-argument {trial_dir} placeholder"
            )
        else:
            resolved.append(argument)
    return resolved


def _default_runner(
    argv: list[str], env: dict[str, str], timeout: float | None, cwd: Path
) -> tuple[dict[str, Any], str]:
    return profiler.execute_command(argv, env, timeout, cwd=cwd)


@util.cancellable
def run_checks(
    checks: Sequence[OutputCheck],
    workspace: str | Path,
    evidence_directory: str | Path,
    *,
    evidence_prefix: str,
    base_env: Mapping[str, str],
    deadline: float | None,
    clock: Clock,
    command_runner: CommandRunner = _default_runner,
) -> ValidationReport:
    """Run every declared check once and retain a result for each.

    File access is re-confined at use time. Command validators run without a
    shell, in a caller-owned trial workspace (communicated by cwd through the
    runner wrapper and ``SIMTUNE_TRIAL_DIR``), and are capped by both their own
    timeout and the experiment deadline.
    """
    root = Path(workspace).resolve(strict=True)
    evidence_root = Path(evidence_directory)
    if not checks:
        return ValidationReport(
            result=ValidationResult(
                state=ValidationState.UNCHECKED,
                checks=[],
                explanation="No scientific output checks were declared or run.",
                evidence=[],
            ),
            outcomes=(),
            launches_started=0,
            spent_seconds=0.0,
        )

    outcomes: list[CheckOutcome] = []
    total_spent = 0.0
    launches = 0
    for check in checks:
        evidence_path = _evidence_path(evidence_root, check.id)
        evidence_ref = f"{evidence_prefix}/{check.id}.json"
        started = clock()
        observed: dict[str, Any] = {
            "schema_version": 1,
            "kind": "output_check_result",
            "check_id": check.id,
            "method": check.method.value,
            "passed": False,
            "explanation": "check did not complete",
        }
        launched = False
        cancelled = None
        try:
            if deadline is not None and clock() >= deadline:
                raise ValidationError("experiment budget exhausted before scientific check")
            if check.method == CheckKind.FILE_HASH:
                output = artifact_path(root, check.output)
                if not output.is_file():
                    raise ValidationError(f"required output is missing: {check.output}")
                actual = sha256_file(output)
                observed.update(expected_sha256=check.sha256, observed_sha256=actual)
                if actual != check.sha256:
                    raise ValidationError(
                        f"SHA-256 mismatch: expected {check.sha256}, observed {actual}"
                    )
                explanation = "Output SHA-256 exactly matches the declared reference."
            elif check.method == CheckKind.NUMERIC_JSON:
                output = artifact_path(root, check.output)
                if not output.is_file():
                    raise ValidationError(f"required JSON output is missing: {check.output}")
                document = load_json(output.read_text(encoding="utf-8"))
                actual = _json_number(document, check.json_field)
                tolerance = max(
                    check.absolute_tolerance,
                    check.relative_tolerance * max(abs(actual), abs(check.expected)),
                )
                difference = abs(actual - check.expected)
                observed.update(
                    json_field=check.json_field,
                    expected=check.expected,
                    observed=actual,
                    absolute_tolerance=check.absolute_tolerance,
                    relative_tolerance=check.relative_tolerance,
                    effective_tolerance=tolerance,
                    absolute_difference=difference,
                )
                if difference > tolerance:
                    raise ValidationError(
                        f"numeric mismatch: difference {difference} exceeds tolerance {tolerance}"
                    )
                explanation = "Finite JSON number is within the declared tolerance."
            else:
                remaining = None if deadline is None else deadline - clock()
                if remaining is not None and remaining <= 0:
                    raise ValidationError("experiment budget exhausted before validator launch")
                timeout = check.timeout_seconds
                if remaining is not None:
                    timeout = min(timeout, remaining)
                argv = _command_argv(check.argv, root)
                env = dict(base_env)
                env["SIMTUNE_TRIAL_DIR"] = str(root)
                launched = True
                launches += 1
                command_started = clock()
                # If the parent cannot run handlers (SIGKILL/power loss), running is uncertain.
                observed.update(status="running", argv=argv)
                _atomic_json(evidence_path, observed)
                if deadline is not None:
                    timeout = min(timeout, deadline - clock())
                    if timeout <= 0:
                        raise ValidationError("budget exhausted while persisting validator intent")
                metrics, raw = command_runner(argv, env, timeout, root)
                command_elapsed = clock() - command_started
                observed.update(
                    argv=argv,
                    return_code=metrics.get("return_code"),
                    timed_out=metrics.get("timed_out"),
                    raw_output=raw,
                    cancellation=metrics.get("cancellation"),
                )
                if (metrics.get("cancellation") or {}).get("parent_signal") is not None or not (
                    metrics.get("cancellation") or {}
                ).get("termination_known", True):
                    cancelled = metrics
                    raise ValidationError("validator interrupted or termination is uncertain")
                if metrics.get("timed_out"):
                    raise ValidationError("validator timed out")
                if metrics.get("return_code") != 0:
                    raise ValidationError(
                        f"validator exited with status {metrics.get('return_code')!r}"
                    )
                if command_elapsed > timeout:
                    raise ValidationError("validator completed after its authorized timeout")
                explanation = "Explicit validator command completed successfully."
            if deadline is not None and clock() >= deadline:
                raise ValidationError("scientific check completed after the experiment deadline")
            observed.update(passed=True, status="succeeded", explanation=explanation)
            passed = True
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            explanation = str(exc)
            observed.update(explanation=explanation, status="failed")
            passed = False
        elapsed = max(0.0, clock() - started)
        total_spent += elapsed
        observed["elapsed_seconds"] = elapsed
        _atomic_json(evidence_path, observed)
        if passed and deadline is not None and clock() >= deadline:
            passed = False
            explanation = "scientific check evidence completed after the experiment deadline"
            observed.update(passed=False, explanation=explanation)
            _atomic_json(evidence_path, observed)
        if cancelled is not None:
            observed["status"] = (
                "interrupted" if cancelled["cancellation"]["termination_known"] else "unknown"
            )
            _atomic_json(evidence_path, observed)
            raise util.CommandCancelled(
                cancelled, [outcome.evidence for outcome in outcomes] + [evidence_ref]
            )
        outcomes.append(
            CheckOutcome(
                check_id=check.id,
                passed=passed,
                explanation=explanation,
                evidence=evidence_ref,
                launched=launched,
                elapsed_seconds=elapsed,
            )
        )

    passed = all(outcome.passed for outcome in outcomes)
    failed_ids = [outcome.check_id for outcome in outcomes if not outcome.passed]
    result = ValidationResult(
        state=ValidationState.PASSED if passed else ValidationState.FAILED,
        checks=[outcome.check_id for outcome in outcomes],
        explanation=(
            "All declared scientific output checks passed."
            if passed
            else f"Scientific output checks failed: {', '.join(failed_ids)}."
        ),
        evidence=[outcome.evidence for outcome in outcomes],
    )
    return ValidationReport(
        result=result,
        outcomes=tuple(outcomes),
        launches_started=launches,
        spent_seconds=total_spent,
    )
