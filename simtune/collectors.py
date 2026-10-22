"""Optional, single-attempt diagnostics for new strict experiments.

The default timing path requires no external tool. A perf probe runs only when
explicitly requested and does not run the user's workload.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from . import profiler
from .importers import parse_perf_stat
from .models import Metric, Phase, Scope, ToolIdentity
from .validation import ValidationError


@dataclass(frozen=True)
class CollectorPlan:
    name: str
    perf_path: str | None = None
    perf_version: str | None = None

    def __post_init__(self) -> None:
        if self.name not in {"timing", "perf-stat"}:
            raise ValidationError("collector must be timing or perf-stat")
        if self.name == "perf-stat" and (not self.perf_path or not self.perf_version):
            raise ValidationError("perf-stat collector needs a probed path and version")


class ProbeAuthorization(Protocol):
    """Primary exploratory authorization; probes never spend confirmation reserves."""

    clock: Callable[[], float]

    def begin_launch(self, phase: Phase, requested_timeout: float) -> float: ...

    def available_seconds(self, phase: Phase) -> float: ...

    def charge(self, category: str, started: float) -> float: ...


class CollectorProbeFailure(ValidationError):
    def __init__(
        self,
        message: str,
        *,
        parent_signal: int | None = None,
        termination_known: bool = True,
        interruption_exit_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.parent_signal = parent_signal
        self.termination_known = termination_known
        self.interruption_exit_status = interruption_exit_status


def probe(
    name: str,
    capabilities: dict[str, Any],
    *,
    authorization: ProbeAuthorization | None = None,
    record_probe: Callable[[dict[str, Any]], None] | None = None,
) -> CollectorPlan:
    """Probe once per command through the shared bounded process execution path.

    Each tool timeout is at most two seconds and the remaining primary time.
    The optional recorder persists intent before spawn and results after cleanup.
    Exhaustion propagates separately from tool failure; no fallback or retry.
    """
    if name == "timing":
        return CollectorPlan("timing")
    if name != "perf-stat":
        raise ValidationError("collector must be timing or perf-stat")
    if capabilities.get("platform") != "linux":
        raise ValidationError("perf-stat collector requires Linux; use --collectors timing")
    info = capabilities.get("tools", {}).get("perf", {})
    path = info.get("path")
    if not path:
        raise ValidationError("perf executable unavailable; use --collectors timing")
    if authorization is None:
        raise ValidationError("perf-stat probes require remaining time and launch authorization")

    def run(argv: list[str], step: str) -> str:
        # Import here to avoid the experiments -> launch -> collectors cycle.
        from .experiments import BudgetExhausted

        started = authorization.clock()
        event: dict[str, Any] = {"step": step, "argv": argv, "status": "reserved"}
        try:
            timeout = authorization.begin_launch(Phase.TRAINING, 2)
            event["timeout_seconds"] = timeout
            if record_probe is not None:
                record_probe(dict(event))
            timeout = min(timeout, authorization.available_seconds(Phase.TRAINING))
            if timeout <= 0:
                raise BudgetExhausted("budget exhausted while persisting collector authorization")
            launched_at = authorization.clock()
            metrics, raw = profiler.execute_command(argv, dict(os.environ), timeout)
            event.update(metrics=metrics, raw_output=raw, timeout_seconds=timeout)
            cancellation = metrics.get("cancellation") or {}
            parent_signal = cancellation.get("parent_signal")
            if not cancellation.get("termination_known", True):
                raise CollectorProbeFailure(
                    "collector probe termination is uncertain; inspect owned work before resuming",
                    parent_signal=parent_signal,
                    termination_known=False,
                    interruption_exit_status=128 + parent_signal
                    if parent_signal is not None
                    else 124
                    if metrics["timed_out"]
                    else 130,
                )
            overran = authorization.clock() - launched_at > timeout
            if (
                parent_signal is None
                and (metrics["timed_out"] or metrics["return_code"] == 0 and overran)
                and authorization.available_seconds(Phase.TRAINING) <= 0
            ):
                raise BudgetExhausted("collector probe exhausted its remaining primary time")
            if metrics["return_code"] != 0 or metrics["timed_out"] or overran:
                event["status"] = "failed"
                raise CollectorProbeFailure(
                    f"perf-stat {step} probe failed, timed out or overran authorization; "
                    "inspect probe evidence or explicitly use --collectors timing",
                    parent_signal=parent_signal,
                )
            event["status"] = "succeeded"
            return raw
        except BudgetExhausted:
            event["status"] = "budget_exhausted"
            raise
        except BaseException as exc:
            event.update(
                status="uncertain"
                if not getattr(exc, "termination_known", True)
                else "interrupted"
                if getattr(exc, "parent_signal", None) is not None
                else "failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            event["elapsed_seconds"] = authorization.charge("collector_probe", started)
            if record_probe is not None:
                record_probe(dict(event))

    version = run([path, "--version"], "version")
    version_text = version.removeprefix("STDOUT:\n").partition("\nSTDERR:\n")[0].strip()
    if not version_text:
        raise ValidationError("perf-stat version unavailable; use --collectors timing")
    run(
        [path, "stat", "-x;", "--no-big-num", "-e", "task-clock,cycles,instructions", "--", "true"],
        "permission",
    )
    return CollectorPlan("perf-stat", path, version_text)


def parse_live_perf(raw: bytes, *, version: str) -> list[dict[str, Any]]:
    context = {
        "kind": "simtune_perf_stat_context",
        "schema_version": 1,
        "command": ["live strict trial"],
        "input_identity": "live strict trial",
        "host": "current host",
        "versions": {"perf": version},
        "scope": "process_tree",
        "fixed_env": {},
        "workload_fingerprint": None,
        "environment_fingerprint": None,
    }
    parsed = parse_perf_stat(raw, context)
    requested = {"task-clock": "msec", "cycles": "count", "instructions": "count"}
    result = []
    for event, unit in requested.items():
        found = [row for row in parsed if row.name == event]
        if len(found) > 1:
            raise ValidationError(f"perf collector repeated requested event {event}")
        if found:
            if found[0].unit != unit:
                raise ValidationError(f"perf collector changed unit for {event}")
            result.append(found[0].to_dict())
        else:
            result.append(
                Metric(
                    id=f"perf-missing-{event}",
                    name=event,
                    value=None,
                    unit=unit,
                    collector=ToolIdentity(
                        name="perf stat", version=version, mechanism="external delimited statistics"
                    ),
                    scope=Scope.PROCESS_TREE,
                    raw_source="raw.perf",
                    scaling_factor=None,
                    multiplex_running_fraction=None,
                    unavailable={
                        "value": "requested counter absent from perf output",
                        "scaling_factor": "not supplied",
                        "multiplex_running_fraction": "not supplied",
                    },
                ).to_dict()
            )
    return result


def read_launcher_affinity() -> dict[str, Any]:
    """Read-only launcher's mask; never a claim about worker or page placement."""
    if not hasattr(os, "sched_getaffinity"):
        return {
            "scope": "launcher_process",
            "cpu_ids": None,
            "unavailable": "sched_getaffinity is not available on this platform",
        }
    try:
        return {
            "scope": "launcher_process",
            "cpu_ids": sorted(os.sched_getaffinity(0)),
            "unavailable": None,
        }
    except OSError as exc:
        return {
            "scope": "launcher_process",
            "cpu_ids": None,
            "unavailable": f"read-only affinity observation failed: {exc}",
        }
