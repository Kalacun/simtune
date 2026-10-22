"""Version-1 extension contracts. These records do not execute or discover anything.

The eight record envelopes are independent of legacy run schema 2. Small nested
value objects share strict validation but are not independently versioned files.
See docs/formats.md for required fields, units, provenance and trust boundaries.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field, fields
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, ClassVar, Self

from .validation import (
    ValidationError,
    decode_model,
    encode_model,
    load_json,
    require,
    restore_toml_nulls,
    validate_model,
)

Text = Annotated[str, "text"]
Arg = Annotated[str, "arg"]
ID = Annotated[str, "id"]
Digest = Annotated[str, "sha256"]
PositiveInt = Annotated[int, "positive"]
Count = Annotated[int, "nonnegative"]
Positive = Annotated[float, "positive"]
Nonnegative = Annotated[float, "nonnegative"]
Fraction = Annotated[float, "fraction"]
IDs = Annotated[list[ID], "unique"]
CpuIDs = Annotated[list[Count], "unique"]
ArtifactPath = Annotated[str, "artifact"]
AbsolutePath = Annotated[str, "absolute"]
Timestamp = Annotated[str, "timestamp"]
JSONValue = Annotated[Any, "json"]


class Platform(StrEnum):
    LINUX = "linux"
    MACOS = "macos"
    OTHER = "other"


class Outcome(StrEnum):
    VALIDATED_IMPROVEMENT = "validated_improvement"
    NO_IMPROVEMENT = "no_improvement"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class ValidationState(StrEnum):
    UNCHECKED = "unchecked"
    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class TrialStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"


class Phase(StrEnum):
    WARMUP = "warmup"
    TRAINING = "training"
    CONFIRMATION = "confirmation"


class ActionStatus(StrEnum):
    PLANNED = "planned"
    APPLIED = "applied"
    SKIPPED = "skipped"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    UNKNOWN = "unknown"


class Target(StrEnum):
    CPU_PLACEMENT = "cpu_placement"
    NUMA_POLICY = "numa_policy"
    RUNTIME = "runtime"
    ENVIRONMENT = "environment"
    PROCESS_LIMIT = "process_limit"


class Placement(StrEnum):
    INHERITED = "inherited"
    COMPACT = "compact"
    SPREAD = "spread"
    ONE_THREAD_PER_CORE = "one_thread_per_core"


class NumaPolicy(StrEnum):
    INHERITED = "inherited"
    LOCAL = "local"
    BIND = "bind"
    INTERLEAVE = "interleave"


class BudgetBasis(StrEnum):
    INHERITED = "inherited"
    RESOURCE_CAPPED = "resource_capped"
    UNCONSTRAINED_INHERITED = "unconstrained_inherited"


class CheckKind(StrEnum):
    FILE_HASH = "file_hash"
    NUMERIC_JSON = "numeric_json"
    COMMAND = "command"


class Scope(StrEnum):
    PROCESS = "process"
    PROCESS_TREE = "process_tree"
    JOB_STEP = "job_step"
    JOB = "job"


class LimitState(StrEnum):
    LIMITED = "limited"
    UNLIMITED = "unlimited"
    UNKNOWN = "unknown"


@dataclass(frozen=True, kw_only=True)
class Model:
    # Only these two maps may be omitted on the wire. None is never a default.
    unavailable: dict[Text, Text] = field(default_factory=dict)
    extensions: dict[Text, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_model(self)

    def _validate(self, path: str) -> None:
        pass

    def to_dict(self, *, root: Path | None = None) -> dict[str, Any]:
        return encode_model(self, root=root)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, root: Path | None = None) -> Self:
        return decode_model(cls, data, root=root)


@dataclass(frozen=True, kw_only=True)
class Record(Model):
    id: ID
    KIND: ClassVar[str]
    SCHEMA_VERSION: ClassVar[int] = 1

    def to_json(self, *, root: Path | None = None) -> str:
        return (
            json.dumps(
                self.to_dict(root=root),
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    @classmethod
    def from_json(cls, text: str, *, root: Path | None = None) -> Self:
        return cls.from_dict(load_json(text), root=root)


@dataclass(frozen=True, kw_only=True)
class CpuTopology(Model):
    cpu_id: Count
    socket_id: Count | None
    core_id: Count | None
    smt_siblings: CpuIDs | None
    numa_node_id: Count | None

    def _validate(self, path: str) -> None:
        if self.smt_siblings is not None:
            require(self.cpu_id in self.smt_siblings, path, "SMT siblings must include this CPU")


@dataclass(frozen=True, kw_only=True)
class NumaNode(Model):
    node_id: Count
    cpu_ids: CpuIDs
    memory_bytes: Count | None
    # Distance keys are decimal Linux node IDs; values are relative distances, not time.
    distances: dict[str, Count] | None

    def _validate(self, path: str) -> None:
        if self.distances is not None:
            require(
                all(re.fullmatch(r"0|[1-9][0-9]*", key) for key in self.distances),
                path,
                "distance keys must be decimal node IDs",
            )


@dataclass(frozen=True, kw_only=True)
class MemoryLimit(Model):
    source: Text
    scope: Text
    state: LimitState
    bytes: Count | None

    def _validate(self, path: str) -> None:
        require(
            (self.state == LimitState.LIMITED) == (self.bytes is not None),
            path,
            "only a limited memory bound has a byte value",
        )


@dataclass(frozen=True, kw_only=True)
class ResourceSnapshot(Record):
    KIND: ClassVar[str] = "resource_snapshot"
    platform: Platform
    is_wsl: bool
    online_cpu_ids: CpuIDs | None
    cpus: list[CpuTopology] | None
    numa_nodes: list[NumaNode] | None
    inherited_cpu_ids: CpuIDs | None
    permitted_cpu_ids: CpuIDs | None
    effective_cpu_ids: CpuIDs | None
    allowed_memory_node_ids: CpuIDs | None
    memory_limits: list[MemoryLimit]
    # A field-name to source-description map; scheduler hints are never CPU masks.
    sources: dict[Text, Text]
    scheduler_hints: dict[Text, Arg]
    conflicts: list[Text]

    def _validate(self, path: str) -> None:
        require(not self.is_wsl or self.platform == Platform.LINUX, path, "WSL requires Linux")
        for name, items, key in (
            ("cpus", self.cpus, "cpu_id"),
            ("numa_nodes", self.numa_nodes, "node_id"),
        ):
            if items is not None:
                values = [getattr(item, key) for item in items]
                require(len(values) == len(set(values)), f"{path}.{name}", "duplicate IDs")
        known = [
            set(s)
            for s in (self.online_cpu_ids, self.inherited_cpu_ids, self.permitted_cpu_ids)
            if s is not None
        ]
        if self.effective_cpu_ids is not None:
            require(bool(known), path, "effective CPUs require a known boundary")
            require(
                set(self.effective_cpu_ids) == set.intersection(*known),
                path,
                "effective CPUs must equal the intersection of known boundaries",
            )
        names = {f.name for f in fields(self)}
        require(
            set(self.sources) <= names - {"sources", "extensions", "unavailable"},
            path,
            "source keys must name snapshot fields",
        )
        for name in (
            "online_cpu_ids",
            "cpus",
            "numa_nodes",
            "inherited_cpu_ids",
            "permitted_cpu_ids",
            "effective_cpu_ids",
            "allowed_memory_node_ids",
        ):
            if getattr(self, name) is not None:
                require(name in self.sources, f"{path}.sources", f"missing provenance for {name}")


@dataclass(frozen=True, kw_only=True)
class Artifact(Model):
    id: ID
    role: Text
    path: Text
    sha256: Digest


@dataclass(frozen=True, kw_only=True)
class RuntimeIdentity(Model):
    name: Text
    version: Text | None
    identity: Text | None


@dataclass(frozen=True, kw_only=True)
class ResourceBudget(Model):
    basis: BudgetBasis
    cpu_count: PositiveInt | None
    thread_count: PositiveInt | None
    memory_bytes: PositiveInt | None
    reference_cpu_ids: CpuIDs | None
    rationale: Text

    def _validate(self, path: str) -> None:
        if self.reference_cpu_ids is not None:
            require(bool(self.reference_cpu_ids), path, "reference CPU mask cannot be empty")
            if self.cpu_count is not None:
                require(
                    len(self.reference_cpu_ids) == self.cpu_count,
                    path,
                    "reference mask must match CPU budget",
                )
        if self.basis == BudgetBasis.RESOURCE_CAPPED:
            require(self.cpu_count is not None, path, "resource-capped budget requires CPU count")


@dataclass(frozen=True, kw_only=True)
class OutputCheck(Model):
    id: ID
    method: CheckKind
    output: ArtifactPath | None
    sha256: Digest | None
    json_field: list[Text] | None
    expected: float | None
    absolute_tolerance: Nonnegative | None
    relative_tolerance: Nonnegative | None
    argv: list[Arg] | None
    timeout_seconds: Positive | None
    rationale: Text

    def _validate(self, path: str) -> None:
        fields_by_method = {
            CheckKind.FILE_HASH: {"output", "sha256"},
            CheckKind.NUMERIC_JSON: {
                "output",
                "json_field",
                "expected",
                "absolute_tolerance",
                "relative_tolerance",
            },
            CheckKind.COMMAND: {"argv", "timeout_seconds"},
        }
        all_fields = set.union(*fields_by_method.values())
        require(
            {key for key in all_fields if getattr(self, key) is not None}
            == fields_by_method[self.method],
            path,
            "check fields do not match method",
        )
        if self.argv is not None:
            _argv(self.argv, path)
        if self.json_field is not None:
            require(bool(self.json_field), path, "JSON field path must be nonempty")


@dataclass(frozen=True, kw_only=True)
class CaseSpec(Record):
    KIND: ClassVar[str] = "case_spec"
    input_class: Text
    arguments: dict[ID, Arg]
    input_artifacts: list[Artifact]
    weight: Positive
    checks: list[OutputCheck]
    resource_needs: ResourceBudget
    regression_tolerance: Fraction


@dataclass(frozen=True, kw_only=True)
class WorkloadLimits(Model):
    trial_timeout_seconds: Positive
    max_trials: PositiveInt
    max_launches: PositiveInt


@dataclass(frozen=True, kw_only=True)
class WorkloadSpec(Record):
    KIND: ClassVar[str] = "workload_spec"
    family: ID
    argv_template: list[Arg]
    resolved_executable: AbsolutePath
    executable_sha256: Digest
    artifacts: list[Artifact]
    base_directory: AbsolutePath
    runtime: RuntimeIdentity
    fixed_env: dict[Text, Arg]
    case_substitutions: IDs
    cases: list[CaseSpec]
    training_case_ids: IDs
    validation_case_ids: IDs
    output_checks: list[OutputCheck]
    limits: WorkloadLimits

    @classmethod
    def from_toml(cls, text: str) -> Self:
        """Parse native input; absent nullable fields encode TOML's missing null.

        `unavailable` must still name and explain each absent/null field. Paths
        are declared identities here; no resolution, hashing, or execution.
        """
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ValidationError(f"TOML: {exc}") from exc
        return cls.from_dict(restore_toml_nulls(cls, data))

    def _validate(self, path: str) -> None:
        _argv(self.argv_template, path)
        literal_argv = self.extensions.get("simtune.literal_argv", False)
        require(type(literal_argv) is bool, path, "literal_argv must be boolean")
        require(
            literal_argv or ("{" not in self.argv_template[0] and "}" not in self.argv_template[0]),
            path,
            "executable cannot be substituted",
        )
        if literal_argv:
            require(not self.case_substitutions, path, "literal argv cannot declare substitutions")
        for arg in self.argv_template:
            if not literal_argv and ("{" in arg or "}" in arg):
                require(
                    arg
                    in {"{trial_dir}", *("{case." + key + "}" for key in self.case_substitutions)},
                    path,
                    "unknown or non-whole-argument placeholder",
                )
        case_ids = {case.id for case in self.cases}
        training, validation = set(self.training_case_ids), set(self.validation_case_ids)
        require(bool(training), path, "at least one training case is required")
        require(not training & validation, path, "training and validation cases must be disjoint")
        require(
            training | validation == case_ids,
            path,
            "case partition must cover exactly declared cases",
        )
        for case in self.cases:
            require(
                set(case.arguments) == set(self.case_substitutions),
                path,
                "case arguments must match declared substitutions",
            )
            ids = [check.id for check in self.output_checks + case.checks]
            require(len(ids) == len(set(ids)), path, "duplicate effective check IDs")
        _env(self.fixed_env, path)


@dataclass(frozen=True, kw_only=True)
class SettingSpec(Model):
    id: ID
    target: Target
    name: Text
    value: JSONValue
    unit: Text
    required: bool
    rationale: Text
    expected_effect: Text

    def _validate(self, path: str) -> None:
        require(self.value is not None, path, "requested setting must have a known value")


@dataclass(frozen=True, kw_only=True)
class PolicySpec(Record):
    KIND: ClassVar[str] = "policy_spec"
    placement: Placement
    numa_policy: NumaPolicy
    numa_node_count: PositiveInt | None
    runtime: RuntimeIdentity
    settings: list[SettingSpec]
    budget: ResourceBudget
    rationale: Text

    def _validate(self, path: str) -> None:
        binding = self.numa_policy in {NumaPolicy.BIND, NumaPolicy.INTERLEAVE}
        require(
            binding == (self.numa_node_count is not None),
            path,
            "binding/interleaving require a logical node count; inherited/local do not",
        )
        names = [(item.target, item.name) for item in self.settings]
        require(len(names) == len(set(names)), path, "duplicate target/setting names")


@dataclass(frozen=True, kw_only=True)
class ToolIdentity(Model):
    name: Text
    version: Text | None
    mechanism: Text


@dataclass(frozen=True, kw_only=True)
class ActionRecord(Model):
    """User-facing account of one setting; rationale is a hypothesis, not a speedup."""

    setting: SettingSpec
    tool: ToolIdentity
    status: ActionStatus
    resolved_value: JSONValue
    observed_value: JSONValue
    explanation: Text
    observation_scope: Text
    evidence: list[ArtifactPath]

    def _validate(self, path: str) -> None:
        if self.status in {ActionStatus.PLANNED, ActionStatus.APPLIED}:
            require(
                self.resolved_value is not None,
                path,
                "planned/applied action requires a resolved value",
            )
        if self.observed_value is not None:
            require(bool(self.evidence), path, "observed action requires evidence")
        if self.status == ActionStatus.APPLIED:
            require(bool(self.evidence), path, "applied mechanism requires evidence")

    def describe(self) -> str:
        """Plain explanation of this record, never executable command text."""
        validate_model(self)

        def display(value: Any, name: str) -> str:
            return (
                self.unavailable[name] if value is None else json.dumps(value, ensure_ascii=False)
            )

        return (
            f"{self.setting.name} ({self.setting.target.value}; {self.setting.unit}) — "
            f"{'required' if self.setting.required else 'optional'}; {self.status.value}\n"
            f"Tool/mechanism: {self.tool.name} / {self.tool.mechanism}\n"
            f"Tool version: {self.tool.version or self.tool.unavailable['version']}\n"
            f"Why: {self.setting.rationale}\nExpected effect: {self.setting.expected_effect}\n"
            f"Requested: {json.dumps(self.setting.value, ensure_ascii=False)}\n"
            f"Resolved: {display(self.resolved_value, 'resolved_value')}\n"
            f"Observed: {display(self.observed_value, 'observed_value')}\n"
            f"Observation scope: {self.observation_scope}\n"
            f"Evidence: {json.dumps(self.evidence, ensure_ascii=False)}\n{self.explanation}"
        )


@dataclass(frozen=True, kw_only=True)
class Metric(Record):
    KIND: ClassVar[str] = "metric"
    name: Text
    value: float | None
    unit: Text
    collector: ToolIdentity
    scope: Scope
    raw_source: ArtifactPath | None
    scaling_factor: Positive | None
    multiplex_running_fraction: float | None

    def _validate(self, path: str) -> None:
        if self.multiplex_running_fraction is not None:
            require(
                0 <= self.multiplex_running_fraction <= 1,
                path,
                "running fraction must be in [0, 1]",
            )


@dataclass(frozen=True, kw_only=True)
class ValidationResult(Model):
    state: ValidationState
    checks: IDs
    explanation: Text
    evidence: list[ArtifactPath]

    def _validate(self, path: str) -> None:
        if self.state == ValidationState.PASSED:
            require(
                bool(self.checks) and bool(self.evidence),
                path,
                "passed validation requires checks and evidence",
            )


@dataclass(frozen=True, kw_only=True)
class TrialRecord(Record):
    KIND: ClassVar[str] = "trial_record"
    experiment_id: ID
    case_id: ID
    policy_id: ID
    pair_id: ID | None
    phase: Phase
    order: Count
    argv: list[Arg]
    cwd: AbsolutePath
    env_overrides: dict[Text, Arg]
    started_at: Timestamp | None
    ended_at: Timestamp | None
    status: TrialStatus
    status_reason: Text
    return_code: int | None
    timed_out: bool | None
    actions: list[ActionRecord]
    metrics: list[Metric]
    validation: ValidationResult
    raw_artifacts: list[ArtifactPath]

    def _validate(self, path: str) -> None:
        _argv(self.argv, path)
        _env(self.env_overrides, path)
        if self.phase != Phase.WARMUP:
            require(self.pair_id is not None, path, "measurement trials require a pair ID")
        action_ids = [action.setting.id for action in self.actions]
        require(len(action_ids) == len(set(action_ids)), path, "duplicate action setting IDs")
        if self.started_at is not None and self.ended_at is not None:
            require(
                datetime.fromisoformat(self.ended_at.replace("Z", "+00:00"))
                >= datetime.fromisoformat(self.started_at.replace("Z", "+00:00")),
                path,
                "end precedes start",
            )
        if self.status == TrialStatus.SUCCEEDED:
            require(
                self.return_code == 0
                and self.timed_out is False
                and self.started_at is not None
                and self.ended_at is not None,
                path,
                "success requires zero exit, known times and no timeout",
            )
        if self.status == TrialStatus.TIMED_OUT:
            require(self.timed_out is True, path, "timeout status requires timed_out=true")
        if self.timed_out is True:
            require(
                self.status == TrialStatus.TIMED_OUT,
                path,
                "timeout cannot be reported as another status",
            )
        if self.status == TrialStatus.FAILED:
            require(
                self.return_code != 0, path, "failed launch requires nonzero or unknown return code"
            )


@dataclass(frozen=True, kw_only=True)
class Candidate(Model):
    policy_id: ID
    exclusion_reason: Text | None


@dataclass(frozen=True, kw_only=True)
class BudgetLedger(Model):
    authorized_seconds: Positive
    spent_seconds: Nonnegative
    confirmation_reserved_seconds: Nonnegative
    cleanup_allowance_seconds: Nonnegative
    max_trials: PositiveInt
    trials_started: Count
    max_launches: PositiveInt
    launches_started: Count
    additional_seconds: list[Positive]
    # Discovery/setup/warmup/workload/check/confirmation/cleanup, in seconds.
    charges_seconds: dict[Text, Nonnegative]

    def _validate(self, path: str) -> None:
        require(
            self.confirmation_reserved_seconds
            <= self.authorized_seconds + sum(self.additional_seconds),
            path,
            "confirmation reserve exceeds authorized budget",
        )
        # Overruns remain representable; this record is evidence, not a scheduler.
        require(
            abs(sum(self.charges_seconds.values()) - self.spent_seconds) <= 1e-6,
            path,
            "charges must sum to spent seconds",
        )


@dataclass(frozen=True, kw_only=True)
class SelectionRule(Model):
    minimum_time_reduction: Fraction
    confidence_level: float
    interval_method: Text
    bootstrap_seed: Count
    minimum_confirmation_pairs: PositiveInt
    aggregation: Text

    def _validate(self, path: str) -> None:
        require(self.confidence_level == 0.95, path, "version 1 requires 0.95 confidence")
        require(
            self.interval_method == "paired_bootstrap", path, "version 1 requires paired_bootstrap"
        )
        require(
            self.minimum_confirmation_pairs >= 5, path, "at least 5 confirmation pairs per case"
        )
        require(
            self.aggregation == "weighted_mean_log_paired_ratio", path, "unsupported aggregation"
        )


@dataclass(frozen=True, kw_only=True)
class ExperimentRecord(Record):
    KIND: ClassVar[str] = "experiment_record"
    workload_fingerprint: Digest
    environment_fingerprint: Digest
    candidates: list[Candidate]
    objective: Text
    seed: Count
    budget: BudgetLedger
    journal: ArtifactPath
    completion_marker: ArtifactPath | None
    selection_rule: SelectionRule
    training_trial_ids: IDs
    confirmation_trial_ids: IDs
    selected_policy_id: ID | None
    outcome: Outcome
    explanation: Text

    def _validate(self, path: str) -> None:
        require(self.objective == "elapsed_seconds", path, "version 1 objective is elapsed_seconds")
        ids = [item.policy_id for item in self.candidates]
        require(
            bool(ids) and len(ids) == len(set(ids)),
            path,
            "candidate policy IDs must be nonempty and unique",
        )
        require(
            not set(self.training_trial_ids) & set(self.confirmation_trial_ids),
            path,
            "training and confirmation evidence must be disjoint",
        )
        if self.selected_policy_id is not None:
            require(
                any(
                    c.policy_id == self.selected_policy_id and c.exclusion_reason is None
                    for c in self.candidates
                ),
                path,
                "selected policy must be included and not excluded",
            )
        if self.outcome == Outcome.VALIDATED_IMPROVEMENT:
            require(
                self.selected_policy_id is not None
                and bool(self.training_trial_ids)
                and bool(self.confirmation_trial_ids)
                and self.completion_marker is not None,
                path,
                "validated outcome requires selection, separate evidence and completion marker",
            )


@dataclass(frozen=True, kw_only=True)
class CompatibilityRules(Model):
    workload_fingerprint: Digest
    runtime: RuntimeIdentity
    topology_class: Text
    input_classes: list[Text]
    required_capabilities: IDs
    require_artifact_hashes: bool
    require_runtime_match: bool
    require_topology_match: bool
    rationale: Text

    def _validate(self, path: str) -> None:
        require(
            self.require_artifact_hashes
            and self.require_runtime_match
            and self.require_topology_match,
            path,
            "version 1 cannot relax identity/runtime/topology compatibility",
        )
        require(
            bool(self.input_classes) and len(self.input_classes) == len(set(self.input_classes)),
            path,
            "input classes must be nonempty and unique",
        )


@dataclass(frozen=True, kw_only=True)
class EvidenceResolution(Model):
    resource_snapshot_id: ID
    cpu_ids: CpuIDs
    memory_node_ids: CpuIDs | None
    trial_ids: IDs

    def _validate(self, path: str) -> None:
        require(
            bool(self.cpu_ids) and bool(self.trial_ids),
            path,
            "evidence resolution requires CPU mask and trial IDs",
        )


@dataclass(frozen=True, kw_only=True)
class Recipe(Record):
    KIND: ClassVar[str] = "recipe"
    workload_family: ID
    variable_arguments: IDs
    policy: PolicySpec
    resource_constraints: ResourceBudget
    compatibility: CompatibilityRules
    validation: ValidationResult
    evidence: list[ArtifactPath]
    evidence_resolutions: list[EvidenceResolution]
    performance_outcome: Outcome
    explanation: Text

    def _validate(self, path: str) -> None:
        if self.performance_outcome == Outcome.VALIDATED_IMPROVEMENT:
            require(
                self.validation.state == ValidationState.PASSED
                and bool(self.evidence)
                and bool(self.evidence_resolutions),
                path,
                "validated improvement requires passed checks and evidence",
            )


RECORD_TYPES = {
    cls.KIND: cls
    for cls in (
        ResourceSnapshot,
        WorkloadSpec,
        CaseSpec,
        PolicySpec,
        TrialRecord,
        ExperimentRecord,
        Recipe,
        Metric,
    )
}


def record_from_dict(data: dict[str, Any], *, root: Path | None = None) -> Record:
    """Dispatch by explicit extension kind; never infer kind from legacy version."""
    require(type(data) is dict, "record", "expected object")
    kind = data.get("kind")
    require(
        type(kind) is str and kind in RECORD_TYPES,
        "record.kind",
        "unknown or missing extension kind",
    )
    return RECORD_TYPES[kind].from_dict(data, root=root)


def record_from_json(text: str, *, root: Path | None = None) -> Record:
    return record_from_dict(load_json(text), root=root)


def _argv(argv: list[str], path: str) -> None:
    require(bool(argv) and bool(argv[0]), path, "argv requires a nonempty executable")


def _env(env: dict[str, str], path: str) -> None:
    require(
        all("=" not in key and "\0" not in key for key in env),
        path,
        "invalid environment variable name",
    )
