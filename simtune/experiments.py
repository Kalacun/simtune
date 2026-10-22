"""Durable, isolated trial execution on top of the strict launcher.

This module deliberately stops before candidate scoring or winner selection.
It records exact trial order, failures and scientific validation for stage 05.
"""

from __future__ import annotations

import fcntl
import hashlib
import inspect
import json
import math
import os
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import profiler, util
from .checks import CommandRunner, run_checks
from .launch import (
    StrictLaunchResult,
    StrictPlanningError,
    execute_strict_launch,
    plan_strict_launch,
)
from .models import (
    ActionRecord,
    ActionStatus,
    BudgetBasis,
    BudgetLedger,
    Candidate,
    ExperimentRecord,
    Metric,
    NumaPolicy,
    Outcome,
    Phase,
    Placement,
    PolicySpec,
    Scope,
    SelectionRule,
    ToolIdentity,
    TrialRecord,
    TrialStatus,
    ValidationResult,
    ValidationState,
)
from .topology import discover_resource_snapshot
from .validation import ValidationError, artifact_path, load_json
from .workloads import (
    LoadedWorkload,
    environment_fingerprint,
    expand_trial_argv,
    verify_workload_identity,
)

Clock = Callable[[], float]
TimestampProvider = Callable[[], str]
SnapshotProvider = Callable[[], Any]
StrictExecutor = Callable[..., StrictLaunchResult]


class TrialState(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"
    UNSUPPORTED = "unsupported"
    UNCERTAIN = "uncertain"


_TERMINAL_STATES = {
    TrialState.SUCCEEDED,
    TrialState.FAILED,
    TrialState.TIMED_OUT,
    TrialState.INTERRUPTED,
    TrialState.UNSUPPORTED,
    TrialState.UNCERTAIN,
}


class WorkspaceMode(StrEnum):
    ISOLATED = "isolated"
    FIXED_CWD = "fixed_cwd"


class BudgetExhausted(RuntimeError):
    """No new trial or subprocess is authorized by the experiment ledger."""


@dataclass(frozen=True)
class HookSpec:
    argv: tuple[str, ...]
    timeout_seconds: float

    def __post_init__(self) -> None:
        if (
            not self.argv
            or not self.argv[0]
            or any(not isinstance(arg, str) or "\0" in arg for arg in self.argv)
        ):
            raise ValidationError("hook argv requires a nonempty executable and NUL-free arguments")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValidationError("hook timeout must be finite and positive")
        for argument in self.argv:
            if ("{" in argument or "}" in argument) and argument != "{trial_dir}":
                raise ValidationError(
                    "hook argv permits only the whole-argument {trial_dir} placeholder"
                )


@dataclass(frozen=True)
class TrialExecutionOptions:
    workspace_mode: WorkspaceMode = WorkspaceMode.ISOLATED
    fixed_cwd: Path | None = None
    setup: HookSpec | None = None
    cleanup: HookSpec | None = None
    collectors: str = "timing"

    def __post_init__(self) -> None:
        if self.collectors not in {"timing", "perf-stat"}:
            raise ValidationError("collectors must be timing or perf-stat")
        if self.workspace_mode == WorkspaceMode.ISOLATED and self.fixed_cwd is not None:
            raise ValidationError("isolated workspace mode cannot declare fixed_cwd")
        if self.workspace_mode == WorkspaceMode.FIXED_CWD and self.fixed_cwd is None:
            raise ValidationError("fixed_cwd mode requires an explicit working directory")


@dataclass(frozen=True)
class TrialRequest:
    id: str
    case_id: str
    policy: PolicySpec
    phase: Phase
    order: int
    pair_id: str | None
    options: TrialExecutionOptions = field(default_factory=TrialExecutionOptions)


@dataclass(frozen=True)
class JournalEvent:
    trial_id: str
    state: TrialState
    timestamp: str
    reason: str
    record: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "trial_state",
            "schema_version": 1,
            "trial_id": self.trial_id,
            "state": self.state.value,
            "timestamp": self.timestamp,
            "reason": self.reason,
            "record": self.record,
        }

    @classmethod
    def from_dict(cls, value: Any) -> JournalEvent:
        if type(value) is not dict:
            raise ValidationError("journal event must be an object")
        expected = {"kind", "schema_version", "trial_id", "state", "timestamp", "reason", "record"}
        if set(value) != expected or value.get("kind") != "trial_state":
            raise ValidationError("journal event has unknown, missing, or invalid fields")
        if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
            raise ValidationError("journal event has an unsupported schema version")
        if not isinstance(value.get("trial_id"), str) or not value["trial_id"]:
            raise ValidationError("journal event requires a trial ID")
        if not isinstance(value.get("reason"), str) or not value["reason"].strip():
            raise ValidationError("journal event requires a reason")
        if value["record"] is not None and not isinstance(value["record"], str):
            raise ValidationError("journal record reference must be a string or null")
        try:
            state = TrialState(value["state"])
            stamp = datetime.fromisoformat(value["timestamp"].replace("Z", "+00:00"))
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"invalid journal event state or timestamp: {exc}") from exc
        if stamp.tzinfo is None:
            raise ValidationError("journal event timestamp requires a timezone")
        return cls(
            trial_id=value["trial_id"],
            state=state,
            timestamp=value["timestamp"],
            reason=value["reason"],
            record=value["record"],
        )


@dataclass(frozen=True)
class JournalRecovery:
    events: tuple[JournalEvent, ...]
    states: dict[str, TrialState]
    ignored_partial_line: bool


class BudgetController:
    """A monotonic session deadline plus durable, conservative authorization.

    The entire active session counts, including time outside explicitly charged
    spans. Named spans classify this elapsed time; they never add a second copy.
    Resume starts a new session against the old ledger's unspent authorization.
    Cleanup subprocess time has a separate aggregate allowance and cannot spend
    the confirmation time reserve. The ledger includes its elapsed wall time;
    total wall authorization is primary time plus that separate allowance.
    Cleanup persistence and other overhead conservatively consume primary time.
    """

    def __init__(
        self,
        *,
        authorized_seconds: float,
        confirmation_reserved_seconds: float,
        cleanup_allowance_seconds: float,
        max_trials: int,
        max_launches: int,
        confirmation_reserved_trials: int = 0,
        confirmation_reserved_launches: int = 0,
        clock: Clock = time.monotonic,
    ) -> None:
        for name, value, positive in (
            ("authorized_seconds", authorized_seconds, True),
            ("confirmation_reserved_seconds", confirmation_reserved_seconds, False),
            ("cleanup_allowance_seconds", cleanup_allowance_seconds, False),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                or (positive and value == 0)
            ):
                raise ValidationError(f"{name} has an invalid value")
        if confirmation_reserved_seconds > authorized_seconds:
            raise ValidationError("confirmation reserve exceeds authorized time")
        if type(max_trials) is not int or max_trials < 1:
            raise ValidationError("max_trials must be a positive integer")
        if type(max_launches) is not int or max_launches < 1:
            raise ValidationError("max_launches must be a positive integer")
        self.authorized_seconds = float(authorized_seconds)
        self.confirmation_reserved_seconds = float(confirmation_reserved_seconds)
        self.cleanup_allowance_seconds = float(cleanup_allowance_seconds)
        self.max_trials = max_trials
        self.max_launches = max_launches
        self.clock = clock
        self._session_started = self.clock()
        self._last_now = self._session_started
        self._prior_spent = 0.0
        self._session_charged = 0.0
        self._charged_spans: list[tuple[float, float]] = []
        self.trials_started = 0
        self.launches_started = 0
        self.confirmation_trials_started = 0
        self.confirmation_launches_started = 0
        self.additional_seconds: list[float] = []
        self.charges: dict[str, float] = {}
        self._ledger_extensions: dict[str, Any] = {
            "simtune.release03": {"schema_version": 1, "collector_probe_counts": "included"}
        }
        self.reserve_counts(
            trials=confirmation_reserved_trials, launches=confirmation_reserved_launches
        )

    def _now(self) -> float:
        self._last_now = max(self._last_now, self.clock())
        return self._last_now

    def reserve_counts(self, *, trials: int, launches: int) -> None:
        """Declare the confirmation protocol before exploration spends counts.

        A reserve may exceed the authorization: that deliberately leaves no
        exploratory authorization and lets search report an unfunded protocol.
        """
        if any(type(value) is not int or value < 0 for value in (trials, launches)):
            raise ValidationError("confirmation reserved counts must be nonnegative integers")
        self.confirmation_reserved_trials = trials
        self.confirmation_reserved_launches = launches

    @classmethod
    def from_ledger(
        cls,
        ledger: BudgetLedger,
        *,
        additional_seconds: float = 0,
        clock: Clock = time.monotonic,
        session_started: float | None = None,
    ) -> BudgetController:
        """Restore spent authorization without charging time between sessions."""
        extension = ledger.extensions.get("simtune.stage05", {})
        names = (
            "confirmation_reserved_trials",
            "confirmation_reserved_launches",
            "confirmation_trials_started",
            "confirmation_launches_started",
        )
        if not isinstance(extension, dict) or any(
            type(extension.get(name, 0)) is not int or extension.get(name, 0) < 0 for name in names
        ):
            raise ValidationError("invalid stage-05 budget count ledger")
        result = cls(
            authorized_seconds=ledger.authorized_seconds,
            confirmation_reserved_seconds=min(
                ledger.confirmation_reserved_seconds, ledger.authorized_seconds
            ),
            cleanup_allowance_seconds=ledger.cleanup_allowance_seconds,
            max_trials=ledger.max_trials,
            max_launches=ledger.max_launches,
            confirmation_reserved_trials=extension.get("confirmation_reserved_trials", 0),
            confirmation_reserved_launches=extension.get("confirmation_reserved_launches", 0),
            clock=clock,
        )
        if session_started is not None:
            if (
                isinstance(session_started, bool)
                or not isinstance(session_started, (int, float))
                or not math.isfinite(session_started)
                or session_started > result._last_now
            ):
                raise ValidationError("resume session start must be finite and no later than now")
            result._session_started = float(session_started)
        result._ledger_extensions = dict(ledger.extensions)
        # Preserve old totals exactly. Historical perf probes were not counted;
        # their missing counts cannot be reconstructed or silently invented.
        result._ledger_extensions.setdefault(
            "simtune.release03",
            {"schema_version": 1, "collector_probe_counts": "historical_unknown"},
        )
        result.confirmation_reserved_seconds = ledger.confirmation_reserved_seconds
        result._prior_spent = ledger.spent_seconds
        result.charges = dict(ledger.charges_seconds)
        result.trials_started = ledger.trials_started
        result.launches_started = ledger.launches_started
        result.confirmation_trials_started = extension.get("confirmation_trials_started", 0)
        result.confirmation_launches_started = extension.get("confirmation_launches_started", 0)
        if (
            result.trials_started > result.max_trials
            or result.launches_started > result.max_launches
            or result.confirmation_trials_started > result.trials_started
            or result.confirmation_launches_started > result.launches_started
        ):
            raise ValidationError("budget counts exceed their recorded authorization")
        result.additional_seconds = list(ledger.additional_seconds)
        if additional_seconds != 0:
            result.authorize_additional(additional_seconds)
        elif isinstance(additional_seconds, bool):
            raise ValidationError("additional budget must be numeric")
        return result

    @property
    def spent_seconds(self) -> float:
        return self._prior_spent + self._now() - self._session_started

    @property
    def total_authorized_seconds(self) -> float:
        return self.authorized_seconds + sum(self.additional_seconds)

    def authorize_additional(self, seconds: float) -> None:
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or seconds <= 0
        ):
            raise ValidationError("additional budget must be finite and positive")
        self.additional_seconds.append(float(seconds))

    def available_seconds(self, phase: Phase, *, cleanup: bool = False) -> float:
        if cleanup:
            spent = self.charges.get("cleanup", 0.0)
            return max(0.0, self.cleanup_allowance_seconds - spent)
        cleanup_authorized = min(self.charges.get("cleanup", 0.0), self.cleanup_allowance_seconds)
        primary_spent = self.spent_seconds - cleanup_authorized
        remaining = self.total_authorized_seconds - primary_spent
        if phase != Phase.CONFIRMATION:
            remaining -= self.confirmation_reserved_seconds
        return max(0.0, remaining)

    def begin_trial(self, phase: Phase) -> None:
        reserve = (
            max(0, self.confirmation_reserved_trials - self.confirmation_trials_started)
            if phase != Phase.CONFIRMATION
            else 0
        )
        if self.trials_started >= self.max_trials - reserve:
            raise BudgetExhausted("maximum trial count reached or reserved for confirmation")
        if self.available_seconds(phase) <= 0:
            raise BudgetExhausted("experiment time budget is exhausted")
        self.trials_started += 1
        if phase == Phase.CONFIRMATION:
            self.confirmation_trials_started += 1

    def begin_launch(
        self, phase: Phase, requested_timeout: float, *, cleanup: bool = False
    ) -> float:
        reserve = (
            max(0, self.confirmation_reserved_launches - self.confirmation_launches_started)
            if phase != Phase.CONFIRMATION
            else 0
        )
        if self.launches_started >= self.max_launches - reserve:
            raise BudgetExhausted(
                "maximum subprocess launch count reached or reserved for confirmation"
            )
        remaining = self.available_seconds(phase, cleanup=cleanup)
        timeout = min(float(requested_timeout), remaining)
        if not math.isfinite(timeout) or timeout <= 0:
            raise BudgetExhausted("no time remains for this subprocess")
        self.launches_started += 1
        if phase == Phase.CONFIRMATION:
            self.confirmation_launches_started += 1
        return timeout

    def charge(self, category: str, started: float) -> float:
        """Classify an elapsed span, excluding any already-classified overlap."""
        ended = self._now()
        start = min(ended, max(self._session_started, started))
        remaining = [(start, ended)]
        for previous_start, previous_end in self._charged_spans:
            parts = []
            for left, right in remaining:
                if previous_end <= left or previous_start >= right:
                    parts.append((left, right))
                else:
                    if left < previous_start:
                        parts.append((left, previous_start))
                    if previous_end < right:
                        parts.append((previous_end, right))
            remaining = parts
        elapsed = sum(right - left for left, right in remaining)
        self._charged_spans.extend(remaining)
        self._session_charged += elapsed
        self.charges[category] = self.charges.get(category, 0.0) + elapsed
        return ended - start

    def ledger(self) -> BudgetLedger:
        elapsed = self._now() - self._session_started
        charges = dict(self.charges)
        charges["overhead"] = charges.get("overhead", 0.0) + max(
            0.0, elapsed - self._session_charged
        )
        return BudgetLedger(
            authorized_seconds=self.authorized_seconds,
            spent_seconds=sum(charges.values()),
            confirmation_reserved_seconds=self.confirmation_reserved_seconds,
            cleanup_allowance_seconds=self.cleanup_allowance_seconds,
            max_trials=self.max_trials,
            trials_started=self.trials_started,
            max_launches=self.max_launches,
            launches_started=self.launches_started,
            additional_seconds=list(self.additional_seconds),
            charges_seconds=charges,
            extensions={
                **self._ledger_extensions,
                "simtune.stage05": {
                    **self._ledger_extensions.get("simtune.stage05", {}),
                    "confirmation_reserved_trials": self.confirmation_reserved_trials,
                    "confirmation_reserved_launches": self.confirmation_reserved_launches,
                    "confirmation_trials_started": self.confirmation_trials_started,
                    "confirmation_launches_started": self.confirmation_launches_started,
                },
            },
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValidationError(f"refusing to replace symlink artifact {path}")
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


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_bytes(
        path,
        (
            json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8"),
    )


class ExperimentStore:
    """Atomic record replacement plus a flushed append-only state journal."""

    def __init__(self, root: Path, record: ExperimentRecord) -> None:
        self.root = root
        self.record = record

    @classmethod
    def create(cls, root: str | Path, record: ExperimentRecord) -> ExperimentStore:
        destination = Path(root).resolve()
        destination.mkdir(parents=True, exist_ok=False)
        try:
            journal = artifact_path(destination, record.journal)
            journal.parent.mkdir(parents=True, exist_ok=True)
            with journal.open("xb") as stream:
                stream.flush()
                os.fsync(stream.fileno())
            store = cls(destination, record)
            store._write_record(record)
            return store
        except BaseException:
            # Keep the directory for diagnosis; never erase a possibly useful partial record.
            raise

    @classmethod
    def open(cls, root: str | Path) -> ExperimentStore:
        directory = Path(root).resolve(strict=True)
        path = directory / "experiment.json"
        record = ExperimentRecord.from_json(path.read_text(encoding="utf-8"), root=directory)
        return cls(directory, record)

    def _write_record(self, record: ExperimentRecord) -> None:
        _atomic_bytes(self.root / "experiment.json", record.to_json(root=self.root).encode("utf-8"))
        self.record = record

    def update_record(self, record: ExperimentRecord) -> None:
        if record.id != self.record.id:
            raise ValidationError("cannot replace an experiment with a different ID")
        if self.is_complete():
            raise ValidationError("completed experiment records are immutable")
        self._write_record(record)

    def is_complete(self) -> bool:
        if self.record.completion_marker is None:
            return False
        marker_path = artifact_path(self.root, self.record.completion_marker)
        if not marker_path.is_file():
            return False
        try:
            marker = load_json(marker_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValidationError(f"cannot read experiment completion marker: {exc}") from exc
        expected_fields = {
            "kind",
            "schema_version",
            "experiment_id",
            "experiment_record_sha256",
            "completed_at",
        }
        if (
            type(marker) is not dict
            or set(marker) != expected_fields
            or marker.get("kind") != "experiment_completion"
            or type(marker.get("schema_version")) is not int
            or marker["schema_version"] != 1
            or marker.get("experiment_id") != self.record.id
        ):
            raise ValidationError("invalid experiment completion marker")
        try:
            completed_at = datetime.fromisoformat(marker["completed_at"].replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ValidationError("invalid experiment completion timestamp") from exc
        if completed_at.tzinfo is None:
            raise ValidationError("experiment completion timestamp requires a timezone")
        record_digest = hashlib.sha256((self.root / "experiment.json").read_bytes()).hexdigest()
        if marker.get("experiment_record_sha256") != record_digest:
            raise ValidationError("completion marker does not match experiment record")
        return True

    def append(self, event: JournalEvent) -> None:
        if self.is_complete():
            raise ValidationError("cannot append to a completed experiment")
        payload = (
            json.dumps(event.to_dict(), ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        journal = artifact_path(self.root, self.record.journal)
        descriptor = os.open(journal, os.O_WRONLY | os.O_APPEND)
        try:
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise OSError(f"partial journal append: {written} of {len(payload)} bytes")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def allocate_trial(self, trial_id: str) -> tuple[Path, Path]:
        # TrialRecord validation will also check ID syntax; reject separators before mkdir.
        if (
            not trial_id
            or len(trial_id) > 128
            or not trial_id[0].isalnum()
            or not trial_id.isascii()
            or any(not (char.isalnum() or char in "_-") for char in trial_id)
        ):
            raise ValidationError("unsafe trial ID")
        trial_root = self.root / "trials" / trial_id
        trial_root.mkdir(parents=True, exist_ok=False)
        workspace = trial_root / "work"
        workspace.mkdir()
        return trial_root, workspace

    def write_trial(self, record: TrialRecord) -> None:
        path = artifact_path(self.root, f"trials/{record.id}/trial.json")
        _atomic_bytes(path, record.to_json(root=self.root).encode("utf-8"))

    def write_artifact_text(self, reference: str, text: str) -> None:
        path = artifact_path(self.root, reference)
        _atomic_bytes(path, text.encode("utf-8"))

    def write_artifact_json(self, reference: str, value: Any) -> None:
        path = artifact_path(self.root, reference)
        _atomic_json(path, value)

    def recover(self) -> JournalRecovery:
        journal = artifact_path(self.root, self.record.journal)
        payload = journal.read_bytes()
        ignored_partial = bool(payload and not payload.endswith(b"\n"))
        lines = payload.splitlines()
        if ignored_partial:
            lines = lines[:-1]
        events: list[JournalEvent] = []
        states: dict[str, TrialState] = {}
        for number, line in enumerate(lines, 1):
            try:
                event = JournalEvent.from_dict(load_json(line.decode("utf-8")))
            except (UnicodeDecodeError, ValidationError) as exc:
                raise ValidationError(f"journal line {number}: {exc}") from exc
            previous = states.get(event.trial_id)
            if previous is None and event.state != TrialState.NOT_STARTED:
                raise ValidationError(f"journal line {number}: first state is not not_started")
            if previous == TrialState.NOT_STARTED and event.state not in {
                TrialState.RUNNING,
                TrialState.UNSUPPORTED,
                TrialState.FAILED,
                TrialState.INTERRUPTED,
                TrialState.UNCERTAIN,
            }:
                raise ValidationError(f"journal line {number}: invalid transition")
            if previous == TrialState.RUNNING and event.state not in _TERMINAL_STATES:
                raise ValidationError(f"journal line {number}: invalid transition")
            if previous in _TERMINAL_STATES:
                raise ValidationError(f"journal line {number}: transition after terminal state")
            events.append(event)
            states[event.trial_id] = event.state
        recovered = {
            trial_id: TrialState.UNCERTAIN if state == TrialState.RUNNING else state
            for trial_id, state in states.items()
        }
        return JournalRecovery(tuple(events), recovered, ignored_partial)

    def complete(self, explanation: str) -> ExperimentRecord:
        if self.is_complete():
            raise ValidationError("experiment already has a durable completion marker")
        recovery = self.recover()
        incomplete = {
            trial_id: state.value
            for trial_id, state in recovery.states.items()
            if state in {TrialState.NOT_STARTED, TrialState.RUNNING, TrialState.UNCERTAIN}
        }
        if incomplete or recovery.ignored_partial_line:
            raise ValidationError(
                f"cannot complete experiment with uncertain journal state: {incomplete or 'partial line'}"
            )
        for trial_id in recovery.states:
            reference = f"trials/{trial_id}/trial.json"
            path = artifact_path(self.root, reference)
            if not path.is_file():
                raise ValidationError(f"cannot complete experiment: missing {reference}")
            trial = TrialRecord.from_json(path.read_text(encoding="utf-8"), root=self.root)
            expected = {
                TrialState.SUCCEEDED: TrialStatus.SUCCEEDED,
                TrialState.FAILED: TrialStatus.FAILED,
                TrialState.TIMED_OUT: TrialStatus.TIMED_OUT,
                TrialState.INTERRUPTED: TrialStatus.INTERRUPTED,
                TrialState.UNSUPPORTED: TrialStatus.UNSUPPORTED,
            }.get(recovery.states[trial_id])
            if expected is None or trial.status != expected:
                raise ValidationError(
                    f"cannot complete experiment: journal/record status mismatch for {trial_id}"
                )

        marker_ref = "completion.json"
        data = self.record.to_dict(root=self.root)
        data["completion_marker"] = marker_ref
        data["explanation"] = explanation
        data["unavailable"].pop("completion_marker", None)
        completed = ExperimentRecord.from_dict(data, root=self.root)
        self._write_record(completed)
        digest = hashlib.sha256((self.root / "experiment.json").read_bytes()).hexdigest()
        _atomic_json(
            artifact_path(self.root, marker_ref),
            {
                "kind": "experiment_completion",
                "schema_version": 1,
                "experiment_id": completed.id,
                "experiment_record_sha256": digest,
                "completed_at": _utc_now(),
            },
        )
        return completed


def new_experiment_record(
    *,
    experiment_id: str,
    workload: LoadedWorkload,
    snapshot: Any,
    capabilities: Mapping[str, Any],
    policies: Sequence[PolicySpec],
    budget: BudgetController,
    seed: int,
) -> ExperimentRecord:
    if not policies:
        raise ValidationError("an experiment requires at least one declared policy")
    ids = [policy.id for policy in policies]
    if len(ids) != len(set(ids)):
        raise ValidationError("experiment policy IDs must be unique")
    return ExperimentRecord(
        id=experiment_id,
        workload_fingerprint=workload.workload_fingerprint,
        environment_fingerprint=environment_fingerprint(workload, snapshot, capabilities),
        candidates=[
            Candidate(
                policy_id=policy.id,
                exclusion_reason=None,
                unavailable={"exclusion_reason": "policy has not been excluded"},
            )
            for policy in policies
        ],
        objective="elapsed_seconds",
        seed=seed,
        budget=budget.ledger(),
        journal="trials.jsonl",
        completion_marker=None,
        selection_rule=SelectionRule(
            minimum_time_reduction=0.02,
            confidence_level=0.95,
            interval_method="paired_bootstrap",
            bootstrap_seed=seed,
            minimum_confirmation_pairs=5,
            aggregation="weighted_mean_log_paired_ratio",
        ),
        training_trial_ids=[],
        confirmation_trial_ids=[],
        selected_policy_id=None,
        outcome=Outcome.INCONCLUSIVE,
        explanation=(
            "Trial substrate initialized. Stage 04 does not select a candidate or claim a speedup."
        ),
        unavailable={
            "completion_marker": "experiment is not complete",
            "selected_policy_id": "candidate selection belongs to stage 05",
        },
        extensions={
            "simtune.stage04": {
                "manifest": str(workload.manifest_path),
                "untracked_external_dependencies": list(workload.untracked_external_dependencies),
            }
        },
    )


def _validation_unknown(reason: str) -> ValidationResult:
    return ValidationResult(
        state=ValidationState.UNKNOWN,
        checks=[],
        explanation=reason,
        evidence=[],
    )


def _trial_unavailable(
    *, started_at: str | None, ended_at: str | None, return_code: int | None, timed_out: bool | None
) -> dict[str, str]:
    values = {
        "started_at": started_at,
        "ended_at": ended_at,
        "return_code": return_code,
        "timed_out": timed_out,
    }
    return {
        name: "value is not available for this trial state"
        for name, value in values.items()
        if value is None
    }


def _metric(trial_id: str, elapsed: Any, raw_source: str) -> Metric:
    value = (
        float(elapsed)
        if not isinstance(elapsed, bool)
        and isinstance(elapsed, (int, float))
        and math.isfinite(elapsed)
        and elapsed >= 0
        else None
    )
    unavailable = {
        "scaling_factor": "elapsed time is not a scaled counter",
        "multiplex_running_fraction": "elapsed time is not multiplexed",
    }
    if value is None:
        unavailable["value"] = "launcher did not return a finite nonnegative elapsed time"
    return Metric(
        id=f"elapsed-{trial_id}"[:128],
        name="elapsed",
        value=value,
        unit="seconds",
        collector=ToolIdentity(
            name="monotonic-clock",
            version=None,
            mechanism="strict process-tree elapsed clock",
            unavailable={"version": "clock implementation version is not recorded"},
        ),
        scope=Scope.PROCESS_TREE,
        raw_source=raw_source,
        scaling_factor=None,
        multiplex_running_fraction=None,
        unavailable=unavailable,
    )


def _rusage_metrics(trial_id: str, values: Any, raw_source: str) -> list[Metric]:
    """Label child CPU time and unavailable per-trial RSS without inference."""
    if not isinstance(values, dict):
        return []
    collector = ToolIdentity(
        name="posix-rusage",
        version=None,
        mechanism="sequential reaped-child delta, including measurement wrappers",
        unavailable={"version": "POSIX resource interface has no reported tool version"},
    )
    result = []
    for name, key, unit in (
        ("user_cpu_time", "user_time_sec", "seconds"),
        ("system_cpu_time", "sys_time_sec", "seconds"),
        ("max_rss", "max_rss_kb", "kilobytes"),
    ):
        raw = values.get(key)
        value = (
            float(raw) if type(raw) in {int, float} and math.isfinite(raw) and raw >= 0 else None
        )
        unavailable = {
            "scaling_factor": "not a scaled hardware counter",
            "multiplex_running_fraction": "not multiplexed",
        }
        if value is None:
            unavailable["value"] = (
                "rusage maximum RSS is a process-lifetime high-water mark, not a per-trial value"
                if name == "max_rss"
                else "rusage value unavailable or invalid"
            )
        result.append(
            Metric(
                id=f"{name}-{trial_id}"[:128],
                name=name,
                value=value,
                unit=unit,
                collector=collector,
                scope=Scope.PROCESS_TREE,
                raw_source=raw_source,
                scaling_factor=None,
                multiplex_running_fraction=None,
                unavailable=unavailable,
            )
        )
    return result


def _actions_after_launch(
    actions: Sequence[ActionRecord], *, success: bool, evidence: str
) -> list[ActionRecord]:
    result = []
    for action in actions:
        if action.status != ActionStatus.PLANNED:
            result.append(action)
            continue
        result.append(
            replace(
                action,
                status=ActionStatus.APPLIED if success else ActionStatus.UNKNOWN,
                evidence=[evidence],
                explanation=(
                    action.explanation
                    + (
                        " The recorded process path completed; this is mechanism evidence, not "
                        "proof of every thread or page's placement."
                        if success
                        else " The process path failed or timed out, so application is uncertain."
                    )
                ),
            )
        )
    return result


class _FixedCwdLease:
    def __init__(self, path: Path) -> None:
        digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        self.path = Path(tempfile.gettempdir()) / f"simtune-fixed-cwd-{digest}.lock"
        self.descriptor: int | None = None

    def __enter__(self) -> _FixedCwdLease:
        self.descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.descriptor)
            self.descriptor = None
            raise ValidationError(
                "fixed working directory is already in use; concurrent trials are rejected"
            ) from exc
        return self

    def __exit__(self, *_: Any) -> None:
        if self.descriptor is not None:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            os.close(self.descriptor)
            self.descriptor = None


class TrialExecutor:
    """Execute one trial at a time; use separate processes for future concurrency."""

    def __init__(
        self,
        *,
        store: ExperimentStore,
        workload: LoadedWorkload,
        capabilities: Mapping[str, Any],
        budget: BudgetController,
        snapshot_provider: SnapshotProvider = discover_resource_snapshot,
        base_env: Mapping[str, str] | None = None,
        clock: Clock = time.monotonic,
        timestamp: TimestampProvider = _utc_now,
        strict_executor: StrictExecutor = execute_strict_launch,
        command_runner: CommandRunner | None = None,
    ) -> None:
        if store.record.workload_fingerprint != workload.workload_fingerprint:
            raise ValidationError("experiment and workload fingerprints differ")
        self.store = store
        self.workload = workload
        self.capabilities = dict(capabilities)
        self.budget = budget
        self.snapshot_provider = snapshot_provider
        self.base_env = dict(os.environ if base_env is None else base_env)
        self.clock = clock
        self.timestamp = timestamp
        self.strict_executor = strict_executor
        self.command_runner = command_runner or self._run_command

    @staticmethod
    def _run_command(
        argv: list[str], env: dict[str, str], timeout: float | None, cwd: Path
    ) -> tuple[dict[str, Any], str]:
        return profiler.execute_command(argv, env, timeout, cwd=cwd)

    def _append(self, trial_id: str, state: TrialState, reason: str, record: str | None) -> None:
        self.store.append(
            JournalEvent(
                trial_id=trial_id,
                state=state,
                timestamp=self.timestamp(),
                reason=reason,
                record=record,
            )
        )

    def _sync_experiment(self, trial: TrialRecord) -> None:
        training = list(self.store.record.training_trial_ids)
        confirmation = list(self.store.record.confirmation_trial_ids)
        if trial.phase == Phase.TRAINING and trial.id not in training:
            training.append(trial.id)
        if trial.phase == Phase.CONFIRMATION and trial.id not in confirmation:
            confirmation.append(trial.id)
        updated = replace(
            self.store.record,
            budget=self.budget.ledger(),
            training_trial_ids=training,
            confirmation_trial_ids=confirmation,
        )
        self.store.update_record(updated)

    def _sync_budget(self) -> None:
        self.store.update_record(replace(self.store.record, budget=self.budget.ledger()))

    def _record(
        self,
        request: TrialRequest,
        *,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        started_at: str | None,
        ended_at: str | None,
        status: TrialStatus,
        reason: str,
        return_code: int | None,
        timed_out: bool | None,
        actions: Sequence[ActionRecord],
        metrics: Sequence[Metric],
        validation: ValidationResult,
        raw_artifacts: Sequence[str],
    ) -> TrialRecord:
        warning = None
        if request.options.workspace_mode == WorkspaceMode.FIXED_CWD:
            warning = (
                "Compatibility mode used a fixed workload cwd. Metadata/check artifacts remain "
                "isolated, but undeclared workload outputs may collide; concurrent use was locked."
            )
        return TrialRecord(
            id=request.id,
            experiment_id=self.store.record.id,
            case_id=request.case_id,
            policy_id=request.policy.id,
            pair_id=request.pair_id,
            phase=request.phase,
            order=request.order,
            argv=argv,
            cwd=str(cwd),
            env_overrides=env,
            started_at=started_at,
            ended_at=ended_at,
            status=status,
            status_reason=reason,
            return_code=return_code,
            timed_out=timed_out,
            actions=list(actions),
            metrics=list(metrics),
            validation=validation,
            raw_artifacts=list(raw_artifacts),
            unavailable={
                **_trial_unavailable(
                    started_at=started_at,
                    ended_at=ended_at,
                    return_code=return_code,
                    timed_out=timed_out,
                ),
                **({"pair_id": "warmup or unpaired trial"} if request.pair_id is None else {}),
            },
            extensions={
                "simtune.stage04": {
                    "workspace_mode": request.options.workspace_mode.value,
                    "isolated_trial_directory": f"trials/{request.id}/work",
                    "warning": warning,
                }
            },
        )

    @staticmethod
    def _same_resource_budget(request: TrialRequest, case: Any) -> None:
        policy = request.policy.budget
        required = case.resource_needs
        fields = ("basis", "cpu_count", "thread_count", "memory_bytes", "reference_cpu_ids")
        changed = [name for name in fields if getattr(policy, name) != getattr(required, name)]
        # Stage 05 resolves an explicitly resource-capped reference intent. The
        # count, memory and runtime stay fixed; only its formerly unknown mask
        # gains the deterministic concrete reference checked against discovery.
        if (
            required.basis == BudgetBasis.RESOURCE_CAPPED
            and required.reference_cpu_ids is None
            and policy.reference_cpu_ids is not None
            and "reference_cpu_ids" in changed
        ):
            changed.remove("reference_cpu_ids")
        if changed:
            raise ValidationError(
                f"policy {request.policy.id!r} changes case {case.id!r} resource inputs: {changed}"
            )

    def _verify_resolved_reference(self, request: TrialRequest, case: Any, snapshot: Any) -> None:
        required = case.resource_needs
        if required.basis != BudgetBasis.RESOURCE_CAPPED:
            return
        if required.reference_cpu_ids is not None:
            return
        placement = (
            Placement.INHERITED
            if snapshot.effective_cpu_ids is not None
            and required.cpu_count == len(snapshot.effective_cpu_ids)
            else Placement.COMPACT
        )
        reference = replace(
            request.policy,
            placement=placement,
            numa_policy=NumaPolicy.INHERITED,
            numa_node_count=None,
            settings=[],
            unavailable={**request.policy.unavailable, "numa_node_count": "inherited reference"},
        )
        resolved = plan_strict_launch(reference, snapshot, self.capabilities)
        if set(request.policy.budget.reference_cpu_ids or []) != set(resolved.cpu_ids or []):
            raise StrictPlanningError(
                code="reference_changed",
                setting="budget.reference_cpu_ids",
                requested=request.policy.budget.reference_cpu_ids,
                restriction=f"deterministic capped reference resolves to {list(resolved.cpu_ids or [])}",
                remedy="rebuild candidates against the current resource snapshot",
            )

    def _authorize_launch(
        self, phase: Phase, requested_timeout: float, *, cleanup: bool = False
    ) -> float:
        signum = util.cancellation_signal()
        if signum is not None:
            raise util.CommandCancelled(
                {
                    "return_code": -signum,
                    "timed_out": False,
                    "cancellation": {
                        "parent_signal": signum,
                        "workload_return_code": None,
                        "launch_attempted": False,
                        "termination_known": True,
                        "output_complete": True,
                    },
                },
                [],
            )
        timeout = self.budget.begin_launch(phase, requested_timeout, cleanup=cleanup)
        self._sync_budget()
        # Durable writes count too. Do not hand a subprocess a timeout that was
        # computed before an arbitrarily slow fsync.
        timeout = min(timeout, self.budget.available_seconds(phase, cleanup=cleanup))
        if timeout <= 0:
            raise BudgetExhausted("budget exhausted while persisting launch authorization")
        return timeout

    def _run_hook(
        self,
        name: str,
        hook: HookSpec,
        workspace: Path,
        phase: Phase,
        env: dict[str, str],
        trial_id: str,
        *,
        cleanup: bool = False,
    ) -> tuple[dict[str, Any], str]:
        argv = [str(workspace) if arg == "{trial_dir}" else arg for arg in hook.argv]
        timeout = self._authorize_launch(phase, hook.timeout_seconds, cleanup=cleanup)
        reference = f"trials/{trial_id}/hooks/{name}.json"
        self.store.write_artifact_json(
            reference,
            {
                "argv": argv,
                "workspace": str(workspace),
                "status": "running",
                "metrics": None,
                "raw_output": "",
            },
        )
        timeout = min(timeout, self.budget.available_seconds(phase, cleanup=cleanup))
        if timeout <= 0:
            raise BudgetExhausted("budget exhausted while persisting hook intent")
        started = self.clock()
        try:
            metrics, raw = self.command_runner(argv, env, timeout, workspace)
            if self.clock() - started > timeout:
                metrics = {**metrics, "simtune_budget_overrun": True}
        finally:
            self.budget.charge("cleanup" if cleanup else "setup", started)
            self._sync_budget()
        cancellation = metrics.get("cancellation") or {}
        hook_status = (
            "unknown"
            if not cancellation.get("termination_known", True)
            else "interrupted"
            if cancellation.get("parent_signal") is not None
            else "timed_out"
            if metrics.get("timed_out")
            else "succeeded"
            if metrics.get("return_code") == 0
            else "failed"
        )
        self.store.write_artifact_json(
            reference,
            {
                "argv": argv,
                "metrics": metrics,
                "raw_output": raw,
                "workspace": str(workspace),
                "status": hook_status,
            },
        )
        return metrics, reference

    @util.cancellable
    def run(self, request: TrialRequest) -> TrialRecord:
        case = self.workload.case(request.case_id)
        self._same_resource_budget(request, case)
        if request.policy.runtime != self.workload.spec.runtime:
            raise ValidationError("policy runtime differs from the fixed workload runtime identity")
        verify_workload_identity(self.workload)
        self.budget.begin_trial(request.phase)
        self._sync_budget()
        trial_root, workspace = self.store.allocate_trial(request.id)
        del trial_root
        argv = expand_trial_argv(self.workload.spec, case, workspace)
        if request.options.workspace_mode == WorkspaceMode.FIXED_CWD:
            try:
                cwd = request.options.fixed_cwd.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise ValidationError(f"cannot resolve fixed working directory: {exc}") from exc
            if not cwd.is_dir():
                raise ValidationError("fixed working directory is not a directory")
            lease: Any = _FixedCwdLease(cwd)
        else:
            cwd = workspace
            lease = _NullLease()

        record_ref = f"trials/{request.id}/trial.json"
        initial = self._record(
            request,
            argv=argv,
            cwd=cwd,
            env=dict(self.workload.relevant_environment),
            started_at=None,
            ended_at=None,
            status=TrialStatus.UNKNOWN,
            reason="Trial workspace allocated; no subprocess has started.",
            return_code=None,
            timed_out=None,
            actions=[],
            metrics=[],
            validation=_validation_unknown("Scientific validation has not started."),
            raw_artifacts=[],
        )
        # Both writes must be durable before any setup/workload side effect.
        self.store.write_trial(initial)
        self._append(request.id, TrialState.NOT_STARTED, initial.status_reason, record_ref)

        def checked_snapshot_provider() -> Any:
            snapshot = self.snapshot_provider()
            self._verify_resolved_reference(request, case, snapshot)
            return snapshot

        try:
            discovery_started = self.clock()
            try:
                snapshot = checked_snapshot_provider()
                plan = plan_strict_launch(request.policy, snapshot, self.capabilities)
            finally:
                self.budget.charge("discovery", discovery_started)
                self._sync_budget()
        except StrictPlanningError as exc:
            final = replace(
                initial,
                status=TrialStatus.UNSUPPORTED,
                status_reason=str(exc),
            )
            self.store.write_trial(final)
            self._append(request.id, TrialState.UNSUPPORTED, str(exc), record_ref)
            self._sync_experiment(final)
            return final

        plan_env = dict(plan.env_overrides)
        fixed_env = dict(self.workload.relevant_environment)
        conflicts = {
            name for name in fixed_env.keys() & plan_env.keys() if fixed_env[name] != plan_env[name]
        }
        if conflicts:
            reason = f"strict policy changes fixed workload environment: {sorted(conflicts)}"
            final = replace(initial, status=TrialStatus.UNSUPPORTED, status_reason=reason)
            self.store.write_trial(final)
            self._append(request.id, TrialState.UNSUPPORTED, reason, record_ref)
            self._sync_experiment(final)
            return final
        env_overrides = {**fixed_env, **plan_env}
        plan_ref = f"trials/{request.id}/plan.json"
        self.store.write_artifact_json(plan_ref, plan.to_dict())
        prepared = replace(
            initial,
            env_overrides=env_overrides,
            actions=list(plan.actions),
            raw_artifacts=[plan_ref],
        )
        self.store.write_trial(prepared)

        started_at = self.timestamp()
        self._append(request.id, TrialState.RUNNING, "Trial lifecycle started.", record_ref)
        raw_artifacts: list[str] = [plan_ref]
        validation = _validation_unknown("Workload did not reach scientific validation.")
        actions: list[ActionRecord] = list(plan.actions)
        metrics: list[Metric] = []
        return_code: int | None = None
        timed_out: bool | None = None
        status = TrialStatus.UNKNOWN
        reason = "Trial outcome is uncertain."
        terminal = TrialState.UNCERTAIN
        cancellation = None

        with lease:
            try:
                hook_env = {**self.base_env, **env_overrides, "SIMTUNE_TRIAL_DIR": str(workspace)}
                if request.options.setup is not None:
                    setup_metrics, reference = self._run_hook(
                        "setup",
                        request.options.setup,
                        workspace,
                        request.phase,
                        hook_env,
                        request.id,
                    )
                    raw_artifacts.append(reference)
                    cancellation = setup_metrics.get("cancellation")
                    if (cancellation or {}).get("parent_signal") is not None or not (
                        cancellation or {}
                    ).get("termination_known", True):
                        return_code = setup_metrics.get("return_code")
                        timed_out = False
                        raise util.CommandCancelled(setup_metrics, [reference])
                    if setup_metrics.get("simtune_budget_overrun") and not setup_metrics.get(
                        "timed_out"
                    ):
                        raise BudgetExhausted("setup hook exceeded its authorized timeout")
                    if setup_metrics.get("timed_out"):
                        status, terminal, timed_out = (
                            TrialStatus.TIMED_OUT,
                            TrialState.TIMED_OUT,
                            True,
                        )
                        return_code = 124
                        reason = "Setup hook timed out; workload was not launched."
                    elif setup_metrics.get("return_code") != 0:
                        status, terminal, timed_out = TrialStatus.FAILED, TrialState.FAILED, False
                        return_code = setup_metrics.get("return_code")
                        reason = "Setup hook failed; workload was not launched."
                    if status != TrialStatus.UNKNOWN:
                        raise _StopTrial

                # A setup hook may have changed a declared input. Recheck at the
                # last safe boundary before the strict workload launch.
                verify_workload_identity(self.workload)

                if self.budget.available_seconds(request.phase) <= 0:
                    raise BudgetExhausted("budget exhausted before strict launch revalidation")
                requested_timeout = self.workload.spec.limits.trial_timeout_seconds
                launch_authorized = False
                workload_deadline: float | None = None

                def before_launch(timeout: float | None) -> float:
                    nonlocal launch_authorized, workload_deadline
                    if launch_authorized:
                        remaining = self.budget.available_seconds(request.phase)
                        if remaining <= 0:
                            raise BudgetExhausted("budget exhausted before workload launch")
                        return min(timeout or requested_timeout, remaining)
                    capped = self._authorize_launch(request.phase, timeout or requested_timeout)
                    launch_authorized = True
                    workload_deadline = self.clock() + capped
                    return capped

                timeout = requested_timeout
                executor_parameters = inspect.signature(self.strict_executor).parameters
                if "before_launch" not in executor_parameters:
                    # Older injected executor test seams have no callback. Gate
                    # their single invocation; production strict execution uses
                    # the callback after its immediate strict re-resolution.
                    timeout = before_launch(timeout)
                launch_started = self.clock()
                category = (
                    "warmup"
                    if request.phase == Phase.WARMUP
                    else "confirmation"
                    if request.phase == Phase.CONFIRMATION
                    else "workload"
                )
                try:
                    result = self.strict_executor(
                        plan,
                        argv,
                        self.capabilities,
                        timeout,
                        base_env={**self.base_env, **fixed_env},
                        snapshot_provider=checked_snapshot_provider,
                        cwd=cwd,
                        **(
                            {"before_launch": before_launch}
                            if "before_launch" in executor_parameters
                            else {}
                        ),
                        **(
                            {"collector_mode": request.options.collectors}
                            if request.options.collectors != "timing"
                            else {}
                        ),
                    )
                    workload_ended = self.clock()
                    if not launch_authorized:
                        raise RuntimeError("strict executor returned without launch authorization")
                finally:
                    self.budget.charge(category, launch_started)
                    self._sync_budget()
                launch_ref = f"trials/{request.id}/launch.txt"
                self.store.write_artifact_text(launch_ref, result.raw_output)
                launch_record_ref = f"trials/{request.id}/launch.json"
                self.store.write_artifact_json(
                    launch_record_ref,
                    {
                        "kind": "strict_launch_result",
                        "schema_version": 1,
                        "policy_id": result.policy_id,
                        "snapshot_id": result.snapshot_id,
                        "final_argv": list(result.final_argv),
                        "metrics": result.metrics,
                        "unobserved": list(result.unobserved),
                        "raw_output": launch_ref,
                    },
                )
                raw_artifacts.extend([launch_ref, launch_record_ref])
                cancellation = result.metrics.get("cancellation")
                return_code = result.metrics.get("return_code")
                timed_out = result.metrics.get("timed_out")
                metrics = [_metric(request.id, result.metrics.get("wall_time_sec"), launch_ref)]
                metrics.extend(
                    _rusage_metrics(request.id, result.metrics.get("rusage"), launch_ref)
                )
                for index, datum in enumerate(result.metrics.get("collector_metrics", []), 1):
                    imported = Metric.from_dict(datum)
                    metrics.append(
                        replace(
                            imported, id=f"perf-{request.id}-{index}"[:128], raw_source=launch_ref
                        )
                    )
                actions = _actions_after_launch(
                    result.actions,
                    success=return_code == 0 and timed_out is False,
                    evidence=launch_record_ref,
                )
                if (cancellation or {}).get("parent_signal") is not None or not (
                    cancellation or {}
                ).get("termination_known", True):
                    raise util.CommandCancelled(result.metrics, [])
                if timed_out:
                    status, terminal = TrialStatus.TIMED_OUT, TrialState.TIMED_OUT
                    reason = "Strict workload launch timed out; scientific output was not accepted."
                    raise _StopTrial
                if return_code != 0:
                    status, terminal = TrialStatus.FAILED, TrialState.FAILED
                    reason = f"Strict workload launch exited with status {return_code!r}."
                    raise _StopTrial

                if (
                    self.budget.available_seconds(request.phase) <= 0
                    or workload_deadline is not None
                    and workload_ended > workload_deadline
                ):
                    raise BudgetExhausted("workload completed after its authorized deadline")
                check_started = self.clock()

                def budgeted_validator(
                    check_argv: list[str],
                    check_env: dict[str, str],
                    check_timeout: float | None,
                    check_cwd: Path,
                ) -> tuple[dict[str, Any], str]:
                    timeout_value = self._authorize_launch(
                        request.phase,
                        check_timeout or self.workload.spec.limits.trial_timeout_seconds,
                    )
                    return self.command_runner(check_argv, check_env, timeout_value, check_cwd)

                # Recheck workspace confinement after the trusted workload exits;
                # it may not redirect simtune's subsequent file access via a symlink.
                artifact_path(self.store.root, f"trials/{request.id}/work")
                checks_directory = artifact_path(self.store.root, f"trials/{request.id}/checks")
                checks = [*self.workload.spec.output_checks, *case.checks]
                try:
                    check_report = run_checks(
                        checks,
                        workspace,
                        checks_directory,
                        evidence_prefix=f"trials/{request.id}/checks",
                        base_env=hook_env,
                        deadline=self.clock() + self.budget.available_seconds(request.phase),
                        clock=self.clock,
                        command_runner=budgeted_validator,
                    )
                finally:
                    self.budget.charge("check", check_started)
                    self._sync_budget()
                validation = check_report.result
                raw_artifacts.extend(validation.evidence)
                if self.budget.available_seconds(request.phase) <= 0:
                    raise BudgetExhausted("scientific checks exhausted the trial deadline")
                status, terminal, reason = (
                    TrialStatus.SUCCEEDED,
                    TrialState.SUCCEEDED,
                    f"Workload process succeeded; scientific output is {validation.state.value}.",
                )

                # Failed scientific output is preserved in place for diagnosis.
                if (
                    request.options.cleanup is not None
                    and validation.state != ValidationState.FAILED
                ):
                    cleanup_metrics, reference = self._run_hook(
                        "cleanup",
                        request.options.cleanup,
                        workspace,
                        request.phase,
                        hook_env,
                        request.id,
                        cleanup=True,
                    )
                    raw_artifacts.append(reference)
                    cancellation = cleanup_metrics.get("cancellation")
                    if (cancellation or {}).get("parent_signal") is not None or not (
                        cancellation or {}
                    ).get("termination_known", True):
                        raise util.CommandCancelled(cleanup_metrics, [reference])
                    if cleanup_metrics.get("simtune_budget_overrun") and not cleanup_metrics.get(
                        "timed_out"
                    ):
                        raise BudgetExhausted(
                            "cleanup hook exceeded its separate authorized allowance"
                        )
                    if cleanup_metrics.get("timed_out"):
                        status, terminal, timed_out, return_code = (
                            TrialStatus.TIMED_OUT,
                            TrialState.TIMED_OUT,
                            True,
                            124,
                        )
                        reason = "Cleanup hook timed out after workload completion."
                    elif cleanup_metrics.get("return_code") != 0:
                        status, terminal, timed_out = TrialStatus.FAILED, TrialState.FAILED, False
                        return_code = cleanup_metrics.get("return_code")
                        reason = "Cleanup hook failed after workload completion."
            except _StopTrial:
                pass
            except util.CommandCancelled as exc:
                cancellation = exc.metrics["cancellation"]
                raw_artifacts.extend(ref for ref in exc.evidence if ref not in raw_artifacts)
                return_code = exc.metrics.get("return_code")
                timed_out = exc.metrics.get("timed_out")
                status, terminal = (
                    (TrialStatus.INTERRUPTED, TrialState.INTERRUPTED)
                    if cancellation["termination_known"]
                    else (TrialStatus.UNKNOWN, TrialState.UNCERTAIN)
                )
                reason = (
                    "Parent cancelled the trial; it will not be retried automatically."
                    if cancellation.get("parent_signal") is not None
                    else "Owned subprocess termination is uncertain; automatic retry refused."
                )
                if validation.state == ValidationState.UNKNOWN:
                    validation = _validation_unknown(
                        "Trial lifecycle interrupted by parent cancellation."
                    )
            except KeyboardInterrupt:
                status, terminal = TrialStatus.INTERRUPTED, TrialState.INTERRUPTED
                reason = "Trial was interrupted; it will not be retried automatically."
            except BudgetExhausted as exc:
                # Preserve the actual workload exit status. Scientific evidence
                # is incomplete even if that process itself returned success.
                if return_code == 0 and timed_out is False:
                    status, terminal = TrialStatus.SUCCEEDED, TrialState.SUCCEEDED
                else:
                    status, terminal = TrialStatus.FAILED, TrialState.FAILED
                validation = _validation_unknown(f"Budget prevented complete validation: {exc}")
                reason = f"Budget stopped the trial before another subprocess: {exc}"
            except StrictPlanningError as exc:
                status, terminal = TrialStatus.UNSUPPORTED, TrialState.UNSUPPORTED
                reason = f"Immediate strict launch revalidation rejected the trial: {exc}"
                validation = _validation_unknown("Required launch settings could not be preserved.")
            except BaseException as exc:
                # Persist uncertainty before propagating programmer/system failures.
                ended_at = self.timestamp()
                uncertain = self._record(
                    request,
                    argv=argv,
                    cwd=cwd,
                    env=env_overrides,
                    started_at=started_at,
                    ended_at=ended_at,
                    status=TrialStatus.UNKNOWN,
                    reason=f"Uncertain after {type(exc).__name__}: {exc}",
                    return_code=return_code,
                    timed_out=timed_out,
                    actions=actions,
                    metrics=metrics,
                    validation=validation,
                    raw_artifacts=raw_artifacts,
                )
                self.store.write_trial(uncertain)
                self._append(request.id, TrialState.UNCERTAIN, uncertain.status_reason, record_ref)
                self._sync_experiment(uncertain)
                raise

        if util.cancellation_signal() is not None and not (cancellation or {}).get("parent_signal"):
            cancellation = {
                "parent_signal": util.cancellation_signal(),
                "workload_return_code": return_code,
                "launch_attempted": return_code is not None,
                "termination_known": True,
                "output_complete": True,
            }
            status, terminal = TrialStatus.INTERRUPTED, TrialState.INTERRUPTED
            reason = "Parent cancelled the trial lifecycle after subprocess completion."
            if validation.state == ValidationState.UNKNOWN:
                validation = _validation_unknown(reason)
        if timed_out is True and status != TrialStatus.TIMED_OUT:
            # Keep the version-1 status/timeout invariant. The deadline event is
            # retained in launch metrics and the versioned cancellation extension.
            cancellation = {**(cancellation or {}), "deadline_expired": True}
            timed_out = None
        ended_at = self.timestamp()
        final = self._record(
            request,
            argv=argv,
            cwd=cwd,
            env=env_overrides,
            started_at=started_at,
            ended_at=ended_at,
            status=status,
            reason=reason,
            return_code=return_code,
            timed_out=timed_out,
            actions=actions,
            metrics=metrics,
            validation=validation,
            raw_artifacts=raw_artifacts,
        )
        if cancellation and (
            cancellation.get("parent_signal") is not None
            or not cancellation.get("termination_known", True)
        ):
            final = replace(
                final,
                unavailable={
                    **final.unavailable,
                    **(
                        {
                            "timed_out": "deadline expired but lifecycle termination is not established; see cancellation and subprocess metrics"
                        }
                        if cancellation.get("deadline_expired")
                        else {}
                    ),
                },
                extensions={
                    **final.extensions,
                    "simtune.cancellation": {"version": 1, **cancellation},
                },
            )
        self.store.write_trial(final)
        self._append(request.id, terminal, reason, record_ref)
        self._sync_experiment(final)
        return final

    def run_ordered(self, requests: Sequence[TrialRequest]) -> list[TrialRecord]:
        """Run an explicit caller-supplied order; never generate or rank candidates."""
        ids = [request.id for request in requests]
        if len(ids) != len(set(ids)):
            raise ValidationError("ordered trial IDs must be unique")
        records = []
        for request in requests:
            try:
                record = self.run(request)
                records.append(record)
                if record.status in {TrialStatus.INTERRUPTED, TrialStatus.UNKNOWN}:
                    break
            except BudgetExhausted:
                break
        return records


class _NullLease:
    def __enter__(self) -> _NullLease:
        return self

    def __exit__(self, *_: Any) -> None:
        return None


class _StopTrial(Exception):
    pass
