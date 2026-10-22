# Extension formats, version 1

Stage 01 implements the Python records, format validation and examples described
here. Later integrated stages add topology, strict launches, isolated trials and
[measured search](search.md) and [recipe replay](recipes.md). The five original
commands and [run schema 2](storage.md) retain their semantics. These record APIs
remain separate from execution, alongside the normative
[shared contracts](implementation/CONTRACTS.md).

## Reading and writing records

The standard-library-only API is `simtune.models`. Each public record offers
`from_dict(data, *, root=None)`, `from_json(text, *, root=None)`,
`to_dict(*, root=None)` and `to_json(*, root=None)`. `record_from_dict` and
`record_from_json` dispatch by explicit `kind`. `WorkloadSpec.from_toml(text)`
parses the normalized workload contract. Dataclass construction and serialization
both validate; use enum members in Python and their string values on the wire.
Invalid input raises `simtune.validation.ValidationError`, a `ValueError` with
field context. There is no mandatory schema library or runtime fixture loading.

```python
from pathlib import Path
from simtune.models import TrialRecord

root = Path("tests/fixtures/extension/valid")
trial = TrialRecord.from_json((root / "trial.json").read_text(), root=root)
for action in trial.actions:
    print(action.describe())
assert TrialRecord.from_json(trial.to_json()) == trial
```

No command is executed by these APIs. Validation establishes shape and local
consistency, **not** truth of identity hashes, compatibility, correctness,
performance, or the existence of referenced evidence. Later producers and
consumers must verify these claims. Frozen dataclasses prevent attribute
reassignment, but nested lists/maps are mutable; conversion copies containers
and serialization revalidates them. They are not security containers.

## Envelope, primitives and unknowns

Each of the eight record types, including nested records, has exactly
`kind`, `schema_version: 1`, and `id`, plus the required fields catalogued below.
Versions are independent of legacy run versions. No missing kind/version is
inferred, and no legacy run is reinterpreted as a recipe. Nested value objects
have no separate kind/version; their schema belongs to their containing record.

Every model/value object permits two optional maps:

- `unavailable`: field name → nonblank reason. It must name **exactly** the
  object's null fields, including unknown, not supplied, not applicable, or
  unlimited states. Null fields remain required in JSON. An empty list means
  known empty; it does not mean unavailable. `extensions` payload nulls are
  opaque data, not observations.
- `extensions`: nonblank namespace → arbitrary finite JSON data. This is the
  explicit escape hatch for annotations. Unknown normal keys, even in nested
  objects, are errors. Extension content never grants an execution capability.

Both maps default to `{}` and appear explicitly on output. All other fields are
required, even those whose value may be null. Empty/blank reasons are rejected.
JSON duplicate keys and NaN/Infinity (including overflow such as `1e999`) fail.
Integers/floats exclude booleans; float fields accept finite representable integers.
Zero remains a real measurement. Negative elapsed time is not implied by a
missing result; unavailable measurements use null and a reason.

| Type notation | Wire meaning |
| --- | --- |
| `ID`, `IDs` | Safe ID / unique array of IDs. `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`; case-sensitive, never empty, dot, slash or backslash |
| `Count`, `PositiveInt` | Integer ≥ 0 / > 0 |
| `float`, `Nonnegative`, `Positive`, `Fraction` | Finite number / ≥ 0 / > 0 / in [0, 1) |
| `CpuIDs` | Unique nonnegative integer IDs, not counts; may be sparse or empty; also used for memory-node ID sets |
| `Text`, `Arg` | Nonblank NUL-free string / NUL-free string (empty and newline arguments allowed) |
| `Digest` | Exactly 64 lowercase SHA-256 hex characters; validation does not calculate hashes |
| `Timestamp` | ISO 8601 timestamp with timezone; UTC `Z` recommended |
| `AbsolutePath` | Absolute NUL-free execution-context path |
| `ArtifactPath` | Confined relative evidence/output reference as described below |
| `JSONValue` | Finite JSON scalar, array or object, only where explicitly declared |
| `X \| None` | Required field accepting `X` or null, with a local `unavailable` reason |
| `list[X]`, `dict[K, V]` | Array / string-keyed object with typed values |

Sibling objects with `id` values must have unique IDs. Reusing a policy ID as a
reference in another record is expected. CPU/core/socket/node IDs describe an
OS namespace; a core identity is **(socket_id, core_id)**, not core ID alone.
IDs in trial partitions and case partitions are unique; training and held-out
partitions are disjoint. Cross-file referential integrity remains a consumer gate.

## Paths, argv and native TOML

Artifact references reject absolute paths, drive prefixes, backslashes, control
characters, empty components, `.` and `..`. Parsing without `root` checks syntax
only. Passing an existing record `root` checks real paths, including symlink
ancestors, dangling links pointing outside, and loops. Use
`simtune.validation.artifact_path(root, reference)` immediately before file
access. Planned output leaves may not exist yet. This check is not protection
against another process swapping symlinks after validation. Future writers must
own their workspace and constrain file operations there.

Evidence paths, journal/completion references, validator outputs and raw metric
sources use this rule. They resolve under the root supplied for the containing
record, including its embedded objects. Declared workload/code/input artifact
paths are a different namespace: `Artifact.path` is an external declared file
path, absolute or relative to the workload base directory; it may be outside
the evidence root. `cwd` and `resolved_executable` also describe external context,
not permission to delete or write arbitrary paths. Stage 04 must resolve the
manifest base relative to the manifest, never the agent's current directory,
and hash the executable and declared files. This stage does no file hashing or
workload path resolution. The normalized TOML fixture uses synthetic absolute
identities; author-friendly manifest resolution is stage 04.

Argv is always an array. Never execute `describe()` text, `command.txt`, or any
shell-formatted display. Workload templates expand only declared whole arguments
`{case.NAME}` and `{trial_dir}`; the executable cannot be substituted. Unknown
placeholders, substrings and arbitrary attribute access fail. Literal `$HOME`,
Unicode, newlines and shell punctuation remain literal. Case values are inserted
once, with no recursive expansion. No shell/eval/environment expansion occurs.
`fixed_env` stores only explicitly allowlisted name/value pairs. Trial overrides
are separate and do not mutate the parent environment.

TOML has no null. In the normalized TOML contract, omit a nullable field and name
it under that object's `[unavailable]` table. The parser restores only typed
nullable fields, never opaque `extensions` values. JSON still requires the null
key. Other omissions, unknown fields and duplicate keys fail in either format.
See [valid JSON/TOML and intentionally invalid examples](../tests/fixtures/extension/README.md).
All those fixtures are synthetic and establish no benchmark result.

## Explain what happened to the program

Contracts serve both program interchange and the user inspecting a run.
`PolicySpec.rationale` explains why a policy was proposed. Each `SettingSpec`
names the affected launch/runtime setting, its value and unit, whether it is
required, its rationale and its **expected**, unproven effect.

`TrialRecord.actions` contains `ActionRecord` values. Each carries that requested
setting; the tool name, version (or reason unavailable) and mechanism; the resolved
value; the observed value (or reason unavailable); an explanation, observation
scope and evidence references. `ActionRecord.describe()` is an implemented plain
text rendering of those fields. `explain` uses these; `show` and other reporting producers must retain these
same records instead of producing an unrelated success story.

The synthetic example explains `OMP_PROC_BIND=spread`: an OpenMP runtime receives
a child environment override to test whether spreading workers reduces shared
cache contention. It also says locality may worsen, labels the action `planned`,
and explicitly says actual worker binding was not observed. It claims no speedup.

| Action status | Meaning |
| --- | --- |
| `planned` | Resolved setting prepared; no assertion that it ran |
| `applied` | Producer has evidence the named mechanism was applied; this alone does not prove all threads/pages obeyed it |
| `skipped`, `unsupported`, `failed` | Setting was not applied as requested; explanation states why |
| `unknown` | Application state could not be established |

Resolved values are required for planned/applied actions. Applied mechanisms
and non-null observations require evidence references; observations also carry
an explicit scope. A resolved value
or successful preflight must never be copied into observed data without evidence.
Required setting failures are evidence to retain, not grounds to erase the trial.
A required setting's loss fails the implemented strict execution path. The format can
represent that loss; it does not implement the strict launcher.

A tool being used is not evidence of acceleration. Collectors are separately
identified by `Metric.collector`, with process/tree/job scope and raw source.
Performance conclusions belong to the experiment/recipe outcome and its evidence.
User explanations must retain unchecked, unavailable, failed and inconclusive
labels, separate scientific checks from execution status, and avoid claiming
source-code or scientific-parameter optimization.

## Resource, evidence and replay semantics

`ResourceSnapshot` is input to the implemented strict planner. Known effective CPUs must equal
the intersection of known online, permitted and inherited masks; a completely
unknown boundary cannot yield a fabricated effective mask. A known empty
intersection remains empty. Known boundary/topology fields require `sources`.
This validator checks a supplied snapshot; it does not discover one. `conflicts`
retains source disagreements. CPU topology may be partial/offline, and inconsistent
raw observations may be retained for later conservative resolution. Memory-node
permissions are independent. Each `MemoryLimit` has a source, scope, limited /
unlimited / unknown state and bytes or an explained null. An empty memory-limit
list means no observations, not guaranteed capacity. Scheduler hints are metadata.

The reference inherits the conservative resource envelope by default. CPU/thread
search is opt-in. A smaller user CPU budget requires a deterministic reference
mask before any comparison. `ResourceBudget.reference_cpu_ids` may be null in
unresolved intent; before execution, the consumer must resolve and record it.
Known masks must be nonempty and match a declared count. `resource_capped`
requires a CPU count; `unconstrained_inherited` labels a separate comparison.
Do not present different-budget results as equal-resource placement gains.

`PolicySpec.placement`, `numa_policy` and declared budgets are required logical
intent. Per-setting `required` governs additional declared settings; it cannot
make the policy's top-level intent optional. Concrete launch masks belong in
`EvidenceResolution`, not the logical placement mode. A saved
`ResourceBudget.reference_cpu_ids` documents the comparison anchor only; it is
never a mask to copy onto a new allocation during replay. Binding/interleaving use
a logical node count and require capacity/eligibility checks in the strict planner.
Settings' names and JSON values are data, not arbitrary executable hooks;
runtime adapters validate supported names/value shapes and fail closed for
unknown required settings.

`TrialRecord.status=succeeded` requires start/end, zero return code, and no
timeout. This is only launch/process success; validation may still be unchecked
or failed. `interrupted`, `unknown`, `unsupported`, `failed`, `timed_out` are
retained explicitly. A pending/uncertain trial may have null timestamps/status
values with reasons. Non-warmup trials have pair IDs; order is zero-based within
the experiment. Failed/timeout elapsed measurements may be recorded, never scored
as fast successes. Metric units are explicit (`seconds`, `bytes`, `count`, or an
identified collector unit); unavailable/multiplexed counters cannot be inferred.
Scaling factor and running fraction are supplied metadata, not fabricated values.

Scientific checks describe exact output SHA-256, numeric JSON object-key paths
with explicit absolute/relative tolerances and finite reference values, or a
bounded argv validator. This is check specification, not check execution.
`ValidationResult.state=passed` needs named checks and evidence. Exit zero alone
is `unchecked`. Recipe science validation and performance outcome are separate.
Unchecked or unconfirmed evidence cannot become a validated improvement.

The initial objective is `elapsed_seconds`. Case weights are predeclared positive
numbers. Aggregate **weighted mean log paired baseline/candidate ratios**, then
report the geometric mean ratio. Ratios are dimensionless and are not seconds
saved or fractional time reduction. The version-1 selection rule uses a seeded
95% paired bootstrap, at least **5 complete fresh confirmation pairs per held-out
case**, minimum time reduction `d=0.02` by default, and case regression tolerance
`r=0` by default. Require aggregate lower bound `> 1/(1-d)` and each held-out
case lower bound `>= 1/(1+r)`, correct outputs, and no selected-policy failures.
Selection samples and confirmation samples must be disjoint. No held-out cases
means input-specific/provisional evidence requiring explicit unvalidated replay.
The record validator checks declared fields, not statistical claims; stage 05
verifies the complete protocol and linked trial evidence.

`BudgetLedger` is seconds of this tuning job, including discovery, setup, warmup,
workloads, checks and confirmation, with separately bounded cleanup. It retains
initial authorization, additional authorizations on resume, the confirmation
reserve, launch/trial counts and categorized charges. Charges sum to spent time;
overruns and counts exceeding a limit remain representable as actual evidence.
The execution gate must stop new work before exhaustion. Resume's `--budget`
authorizes **additional** time, never resets the prior ledger. Without it, only
remaining prior authorization is available; uncertain side effects are not
repeated automatically. Stage 04 implements atomic replacement, flushed journal
append and separate completion markers; **none of those I/O operations exist in
these format APIs**. A marker path in a valid record does not prove completion.

Recipe compatibility requires workload/artifact identity, runtime, topology
class, resource constraints, input class and required capabilities. Equal CPU
counts are insufficient. Version 1 cannot disable identity/runtime/topology
checks. Re-resolve logical intent inside the new envelope; changed conditions
require explicit revalidation and a derived record, preserving the original.
Strict replay of unchecked/unconfirmed evidence needs `--allow-unvalidated` and
retains that label. No silent retuning or replacement profile. Stages 05–06 implement statistical, compatibility and replay validation;
see [recipes](recipes.md) for evidence and applicability requirements.

## Frozen CLI surface and implementation state

`doctor`, `list-modes`, `profile`, `run`, `compare`, `tune`, `explain`, `exec` and
`validate` are implemented.
Their help/defaults/status/fallback/storage regression fixtures live in
`tests/fixtures/compatibility/`. In the reserved surface below, tune workload,
bare-command, hint, minimal resume, `--save`, explain, exec and validate forms
are available; show/import/Slurm/ensemble commands remain later-stage work. All workload argv follows `--` unchanged.

```text
simtune tune --workload experiment.toml --budget 10m --out DIR [--save recipe.json]
simtune tune --budget 10m --repeat 3 --out DIR -- COMMAND ...
simtune tune --from-run RUN_DIR --workload experiment.toml --budget 10m --out DIR
simtune explain recipe.json [--json]
simtune exec --recipe recipe.json [--allow-unvalidated] -- COMMAND ...
simtune validate --recipe recipe.json --workload experiment.toml --out DIR
simtune show RUN_ID_OR_PATH [--json]
simtune tune --resume EXPERIMENT_DIR [--budget ADDITIONAL_TIME]
simtune import-profile --format perf-stat --input FILE --context CONTEXT_JSON --out DIR
simtune slurm-plan --workload experiment.toml --recipe recipe.json --out DIR
simtune ensemble --workload ensemble.toml --budget 10m --out DIR
```

The tune input variants are workload, bare command, or resume; `--from-run` is a
hint alongside a workload, not substitute fresh evidence. Bare-command tuning
is input-specific/unchecked. `--repeat` does not multiply wall-time authorization
or lower confirmation requirements. Dry-run variants launch no workload/setup/
validator and create no evidence directory. Planned orchestration statuses are
0 success/no-improvement, 1 inconclusive/check failure, 2 invalid/incompatible,
124 timeout, 130 interrupt. `exec` propagates workload status and adds no baseline.
Legacy command semantics remain those in [storage](storage.md) and current help.

## Exact field catalogue

All fields below are required; `unavailable` and `extensions` are the only
omittable maps. Each record additionally has the common envelope and `id`.
Type/enum spellings match `simtune.models`; this catalogue is checked against the
Python declarations in tests to keep future interface changes explicit.

`Platform`: `linux`, `macos`, `other`.

`Outcome`: `validated_improvement`, `no_improvement`, `inconclusive`, `failed`, `interrupted`.

`ValidationState`: `unchecked`, `passed`, `failed`, `unknown`.

`TrialStatus`: `succeeded`, `failed`, `timed_out`, `interrupted`, `unknown`, `unsupported`.

`Phase`: `warmup`, `training`, `confirmation`.

`ActionStatus`: `planned`, `applied`, `skipped`, `unsupported`, `failed`, `unknown`.

`Target`: `cpu_placement`, `numa_policy`, `runtime`, `environment`, `process_limit`.

`Placement`: `inherited`, `compact`, `spread`, `one_thread_per_core`.

`NumaPolicy`: `inherited`, `local`, `bind`, `interleave`.

`BudgetBasis`: `inherited`, `resource_capped`, `unconstrained_inherited`.

`CheckKind`: `file_hash`, `numeric_json`, `command`.

`Scope`: `process`, `process_tree`, `job_step`, `job`.

`LimitState`: `limited`, `unlimited`, `unknown`.

### CpuTopology (nested value object)

| Field | Type |
| --- | --- |
| `cpu_id` | `Count` |
| `socket_id` | `Count \| None` |
| `core_id` | `Count \| None` |
| `smt_siblings` | `CpuIDs \| None` |
| `numa_node_id` | `Count \| None` |

### NumaNode (nested value object)

| Field | Type |
| --- | --- |
| `node_id` | `Count` |
| `cpu_ids` | `CpuIDs` |
| `memory_bytes` | `Count \| None` |
| `distances` | `dict[str, Count] \| None` |

### MemoryLimit (nested value object)

| Field | Type |
| --- | --- |
| `source` | `Text` |
| `scope` | `Text` |
| `state` | `LimitState` |
| `bytes` | `Count \| None` |

### ResourceSnapshot — `resource_snapshot`

| Field | Type |
| --- | --- |
| `platform` | `Platform` |
| `is_wsl` | `bool` |
| `online_cpu_ids` | `CpuIDs \| None` |
| `cpus` | `list[CpuTopology] \| None` |
| `numa_nodes` | `list[NumaNode] \| None` |
| `inherited_cpu_ids` | `CpuIDs \| None` |
| `permitted_cpu_ids` | `CpuIDs \| None` |
| `effective_cpu_ids` | `CpuIDs \| None` |
| `allowed_memory_node_ids` | `CpuIDs \| None` |
| `memory_limits` | `list[MemoryLimit]` |
| `sources` | `dict[Text, Text]` |
| `scheduler_hints` | `dict[Text, Arg]` |
| `conflicts` | `list[Text]` |

### Artifact (nested value object)

| Field | Type |
| --- | --- |
| `id` | `ID` |
| `role` | `Text` |
| `path` | `Text` |
| `sha256` | `Digest` |

### RuntimeIdentity (nested value object)

| Field | Type |
| --- | --- |
| `name` | `Text` |
| `version` | `Text \| None` |
| `identity` | `Text \| None` |

### ResourceBudget (nested value object)

| Field | Type |
| --- | --- |
| `basis` | `BudgetBasis` |
| `cpu_count` | `PositiveInt \| None` |
| `thread_count` | `PositiveInt \| None` |
| `memory_bytes` | `PositiveInt \| None` |
| `reference_cpu_ids` | `CpuIDs \| None` |
| `rationale` | `Text` |

### OutputCheck (nested value object)

| Field | Type |
| --- | --- |
| `id` | `ID` |
| `method` | `CheckKind` |
| `output` | `ArtifactPath \| None` |
| `sha256` | `Digest \| None` |
| `json_field` | `list[Text] \| None` |
| `expected` | `float \| None` |
| `absolute_tolerance` | `Nonnegative \| None` |
| `relative_tolerance` | `Nonnegative \| None` |
| `argv` | `list[Arg] \| None` |
| `timeout_seconds` | `Positive \| None` |
| `rationale` | `Text` |

### CaseSpec — `case_spec`

| Field | Type |
| --- | --- |
| `input_class` | `Text` |
| `arguments` | `dict[ID, Arg]` |
| `input_artifacts` | `list[Artifact]` |
| `weight` | `Positive` |
| `checks` | `list[OutputCheck]` |
| `resource_needs` | `ResourceBudget` |
| `regression_tolerance` | `Fraction` |

### WorkloadLimits (nested value object)

| Field | Type |
| --- | --- |
| `trial_timeout_seconds` | `Positive` |
| `max_trials` | `PositiveInt` |
| `max_launches` | `PositiveInt` |

### WorkloadSpec — `workload_spec`

| Field | Type |
| --- | --- |
| `family` | `ID` |
| `argv_template` | `list[Arg]` |
| `resolved_executable` | `AbsolutePath` |
| `executable_sha256` | `Digest` |
| `artifacts` | `list[Artifact]` |
| `base_directory` | `AbsolutePath` |
| `runtime` | `RuntimeIdentity` |
| `fixed_env` | `dict[Text, Arg]` |
| `case_substitutions` | `IDs` |
| `cases` | `list[CaseSpec]` |
| `training_case_ids` | `IDs` |
| `validation_case_ids` | `IDs` |
| `output_checks` | `list[OutputCheck]` |
| `limits` | `WorkloadLimits` |

### SettingSpec (nested value object)

| Field | Type |
| --- | --- |
| `id` | `ID` |
| `target` | `Target` |
| `name` | `Text` |
| `value` | `JSONValue` |
| `unit` | `Text` |
| `required` | `bool` |
| `rationale` | `Text` |
| `expected_effect` | `Text` |

### PolicySpec — `policy_spec`

| Field | Type |
| --- | --- |
| `placement` | `Placement` |
| `numa_policy` | `NumaPolicy` |
| `numa_node_count` | `PositiveInt \| None` |
| `runtime` | `RuntimeIdentity` |
| `settings` | `list[SettingSpec]` |
| `budget` | `ResourceBudget` |
| `rationale` | `Text` |

### ToolIdentity (nested value object)

| Field | Type |
| --- | --- |
| `name` | `Text` |
| `version` | `Text \| None` |
| `mechanism` | `Text` |

### ActionRecord (nested value object)

| Field | Type |
| --- | --- |
| `setting` | `SettingSpec` |
| `tool` | `ToolIdentity` |
| `status` | `ActionStatus` |
| `resolved_value` | `JSONValue` |
| `observed_value` | `JSONValue` |
| `explanation` | `Text` |
| `observation_scope` | `Text` |
| `evidence` | `list[ArtifactPath]` |

### Metric — `metric`

| Field | Type |
| --- | --- |
| `name` | `Text` |
| `value` | `float \| None` |
| `unit` | `Text` |
| `collector` | `ToolIdentity` |
| `scope` | `Scope` |
| `raw_source` | `ArtifactPath \| None` |
| `scaling_factor` | `Positive \| None` |
| `multiplex_running_fraction` | `float \| None` |

### ValidationResult (nested value object)

| Field | Type |
| --- | --- |
| `state` | `ValidationState` |
| `checks` | `IDs` |
| `explanation` | `Text` |
| `evidence` | `list[ArtifactPath]` |

### TrialRecord — `trial_record`

| Field | Type |
| --- | --- |
| `experiment_id` | `ID` |
| `case_id` | `ID` |
| `policy_id` | `ID` |
| `pair_id` | `ID \| None` |
| `phase` | `Phase` |
| `order` | `Count` |
| `argv` | `list[Arg]` |
| `cwd` | `AbsolutePath` |
| `env_overrides` | `dict[Text, Arg]` |
| `started_at` | `Timestamp \| None` |
| `ended_at` | `Timestamp \| None` |
| `status` | `TrialStatus` |
| `status_reason` | `Text` |
| `return_code` | `int \| None` |
| `timed_out` | `bool \| None` |
| `actions` | `list[ActionRecord]` |
| `metrics` | `list[Metric]` |
| `validation` | `ValidationResult` |
| `raw_artifacts` | `list[ArtifactPath]` |

### Candidate (nested value object)

| Field | Type |
| --- | --- |
| `policy_id` | `ID` |
| `exclusion_reason` | `Text \| None` |

### BudgetLedger (nested value object)

| Field | Type |
| --- | --- |
| `authorized_seconds` | `Positive` |
| `spent_seconds` | `Nonnegative` |
| `confirmation_reserved_seconds` | `Nonnegative` |
| `cleanup_allowance_seconds` | `Nonnegative` |
| `max_trials` | `PositiveInt` |
| `trials_started` | `Count` |
| `max_launches` | `PositiveInt` |
| `launches_started` | `Count` |
| `additional_seconds` | `list[Positive]` |
| `charges_seconds` | `dict[Text, Nonnegative]` |

### SelectionRule (nested value object)

| Field | Type |
| --- | --- |
| `minimum_time_reduction` | `Fraction` |
| `confidence_level` | `float` |
| `interval_method` | `Text` |
| `bootstrap_seed` | `Count` |
| `minimum_confirmation_pairs` | `PositiveInt` |
| `aggregation` | `Text` |

### ExperimentRecord — `experiment_record`

| Field | Type |
| --- | --- |
| `workload_fingerprint` | `Digest` |
| `environment_fingerprint` | `Digest` |
| `candidates` | `list[Candidate]` |
| `objective` | `Text` |
| `seed` | `Count` |
| `budget` | `BudgetLedger` |
| `journal` | `ArtifactPath` |
| `completion_marker` | `ArtifactPath \| None` |
| `selection_rule` | `SelectionRule` |
| `training_trial_ids` | `IDs` |
| `confirmation_trial_ids` | `IDs` |
| `selected_policy_id` | `ID \| None` |
| `outcome` | `Outcome` |
| `explanation` | `Text` |

### CompatibilityRules (nested value object)

| Field | Type |
| --- | --- |
| `workload_fingerprint` | `Digest` |
| `runtime` | `RuntimeIdentity` |
| `topology_class` | `Text` |
| `input_classes` | `list[Text]` |
| `required_capabilities` | `IDs` |
| `require_artifact_hashes` | `bool` |
| `require_runtime_match` | `bool` |
| `require_topology_match` | `bool` |
| `rationale` | `Text` |

### EvidenceResolution (nested value object)

| Field | Type |
| --- | --- |
| `resource_snapshot_id` | `ID` |
| `cpu_ids` | `CpuIDs` |
| `memory_node_ids` | `CpuIDs \| None` |
| `trial_ids` | `IDs` |

### Recipe — `recipe`

| Field | Type |
| --- | --- |
| `workload_family` | `ID` |
| `variable_arguments` | `IDs` |
| `policy` | `PolicySpec` |
| `resource_constraints` | `ResourceBudget` |
| `compatibility` | `CompatibilityRules` |
| `validation` | `ValidationResult` |
| `evidence` | `list[ArtifactPath]` |
| `evidence_resolutions` | `list[EvidenceResolution]` |
| `performance_outcome` | `Outcome` |
| `explanation` | `Text` |

## R02 cancellation metadata

Trial/experiment envelope versions remain 1. Interrupted trials add
`TrialRecord.extensions["simtune.cancellation"]` with `version: 1` and the shared
execution metadata: `parent_signal` (raw positive signal or null),
`workload_return_code` (actual interrupted subprocess status or null),
`launch_attempted` (whether Popen returned a child), `termination_known` (owned
group received SIGKILL or was absent, and the direct child was reaped), and
`output_complete` (capture reached EOF within the drain bound). This applies to
the interrupted workload, hook or validator; its argv and role remain in the
referenced launch/hook/check artifact. It proves nothing about escaped groups.
When the parent cancels, execution `return_code` uses `-parent_signal`; the actual
subprocess exit remains separate. Cancellation after subprocess completion can
retain that completed return code in the trial while labeling the lifecycle
interrupted. `simtune.stage05` search state adds nullable `parent_signal` and
`interruption_exit_status` (the CLI status; uncertain timeout retains 124). To preserve the version-1
status/timeout invariant, uncertain/interrupted deadline expiry leaves trial
`timed_out` null with a reason and adds `deadline_expired: true` to the
cancellation extension; subprocess metrics retain the actual `timed_out: true`.

Execution metrics and replay metrics add the same `cancellation` mapping, without
`version` (inside their existing result envelope). Successful/self-signalling
commands have null `parent_signal`; readers must not infer parent cancellation
from a negative workload code alone. Hook/check artifacts add `status`; persisted
`running` without a terminal write is uncertain, never success. Interrupted
command checks remain `passed: false` and retain partial raw output. Old records
without this metadata remain readable; absence supplies no assumed termination
observation. No source record is migrated or rewritten on load.

## Stage-05 extension keys

`WorkloadSpec.extensions["simtune.literal_argv"] = true` is the explicit bare-command
mode: case substitutions must be empty and argv, including braces, is entirely
literal. Ordinary manifests retain strict whole-argument expansion.
`ExperimentRecord.extensions["simtune.stage05"]` has `schema_version: 1` and stores
the prespecified protocol, logical policies, schedule, cursor/pending intent,
exclusion/score details, labels and conditional costs.
`BudgetLedger.extensions["simtune.stage05"]` stores confirmation trial/launch
reservations and consumed confirmation counts. These additive extensions do not
change the frozen record fields or reinterpret legacy schemas.

R03 adds `BudgetLedger.extensions["simtune.release03"]` with `schema_version: 1`
and `collector_probe_counts: "included"` for newly accounted jobs. Resuming a
ledger without this extension retains its totals and labels the accounting
`"historical_unknown"`; missing historical probe launches are not invented.
This label persists through subsequent sessions and copied recipe evidence.

Search state adds `collector_probes` (ordered reservation/terminal events with
step, argv, status, timeout when authorized, actual elapsed time, and available
metrics/raw output/error) and `pending_collector_probe`. Persisted pending intent
without a terminal event is uncertain and blocks automatic resume. Probe launch
reservations consume the total subprocess cap; actual time is classified under
`collector_probe`. Resumed preparation failures retain the ledger and evidence
without a completion marker.

If collector preparation stops before creating an experiment, `run_search`
returns a JSON-compatible `simtune_tune_startup_result` (`schema_version: 1`),
containing outcome, explanation, collector, budget, collector_probes, parent_signal,
interruption_exit_status,
remedy and a null completion_marker. No partial evidence directory is created.
`tune --json` emits this result; human output provides its reason/remedy. Budget
exhaustion is inconclusive, tool failure is failed, parent cancellation is
interrupted. The first two return 1; parent cancellation returns 128+signal.
Unknown probe termination is retained as uncertain and blocks resume even if
pending intent is missing but the saved metrics establish uncertainty; its
interrupted startup result retains 124 for a known deadline expiry, or 130
otherwise, without inventing a parent signal.

### Tune CLI recipe export metadata

The CLI projections `simtune_tune_result`, `simtune_tune_startup_result` and
`simtune_tune_dry_run` (each schema version 1) include `recipe_path` and the
additive `recipe_export` object. This is invocation-local output, not a field in
ExperimentRecord, Recipe, persisted search state, `show` or `decision_report`.
Direct `run_search`/`resume_search` callers do not receive CLI export metadata.
`simtune_config` only describes configuration; CLI errors retain stderr/exit
behavior and emit no successful result. Existing records need no migration.

| Field | Contract |
| --- | --- |
| `recipe_path` | Null unless `save_recipe` returned successfully in this invocation; on success retains the effective `--save` string, including existing CLI/config path normalization |
| `recipe_export.status` | `saved`, `skipped`, or `not_requested` |
| `recipe_export.requested_path` | Effective `--save` string, or null without a request |
| `recipe_export.reason` | Null for saved/not-requested; skip code below otherwise |

Requested exports skip with `dry_run` for dry-run output,
`experiment_not_started` for pre-store preparation stops,
`incomplete_experiment` for records without a completion marker (including
interruptions), or `no_selected_policy` for complete records with no selected
policy. Incompleteness takes precedence over no selection. These reasons do not
change outcomes, exit codes or export eligibility. Existing destination bytes
are neither proof of export nor modified on a skip. Export exceptions propagate
as command failures before any saved-success JSON; partial exporter artifacts
remain governed by the existing recipe failure contract. Inconclusive alone
does not prohibit export of a complete supported selection; its existing
evidence/validation gates still apply. Readers should treat `recipe_path` as an
artifact pointer and `requested_path` as intent. Old CLI output lacks this
object and may contain the pre-fix misleading path; no historical output is
rewritten or inferred to be a successful export.


## Stage-06 recipe extension

`Recipe.extensions["simtune.stage06"]` now writes `schema_version: 3` and contains the
confined evidence inventory/digests, source experiment and environment identity,
replay/scientific/held-out/runtime labels, topology description and selected-mask
classes, known inherited runtime controls, recomputed training/confirmation facts,
selection rule, budget/costs, limitations and optional source-recipe linkage. A
version-1 Recipe shape alone does not authorize execution. The loader verifies
these facts against the copied experiment and trial evidence.

Version 2 adds `protocol_valid` (boolean) and `protocol_reasons` (array of strings)
to the nested `confirmation` result, when confirmation was evaluated. The same
additive fields appear in `simtune.stage05.confirmation_result` in search state.
They are derived by confirmation evaluation from the complete trial ledger:
eligible phases with the declared pair counts, independent scientific inputs,
fresh confirmation order, passed science and no reused trial evidence. A valid
comparison can yield `no_improvement`; validity does not require a performance
gain. `heldout_confirmed` uses this complete fact, not just phase eligibility.
The bootstrap, thresholds, weights and minimum confirmation count are unchanged.

The Recipe envelope remains version 1. The loader explicitly supports stage-06
extension versions 1, 2 and 3: it recomputes all protocol facts for each version,
then compares with that version's wire representation. Version 1 omits the two
new confirmation fields; their absence never supplies an assumed validity flag.
Valid older bundles, including checked no-improvement evidence, remain usable.
Inconsistent saved claims fail closed for execution even with an unvalidated
override. `explain` can diagnose summary disagreements after evidence integrity
checks: its label is `rejected`, exit status is 2, `execution_errors` explains the
refusal, and JSON separates the original `stored_recipe` from the recomputed
`recipe` and `measured_evidence`. Corrupt/missing evidence still raises an error.
Loading/explaining performs no on-disk migration, export or rerun.

Version 3 adds recorded `hardware` context (processor-class algorithm version 1)
and a historical `label_scope`. ResourceSnapshot's outer schema remains 1;
`extensions["simtune.hardware"]` version 1 adds architecture, per-eligible-CPU
vendor/model provenance, source, completeness and missing-data reasons. Old
records gain no observations on load. `decision_report` is an additive,
independently versioned human/JSON projection. Replay retains generic historical
label keys and adds current compatibility, class match, authorization and
unchecked-output fields. See [the full report contract and replay migration](decision-report.md).

New search state additionally records `runtime_environment` (a limited set of
known runtime launch controls), `revalidation`, and optional `source_recipe`
provenance. Old state remains readable; missing runtime context prevents a
validated replay label. Explicit validation uses only the source logical policy
and reference and requests fresh held-out confirmation even without training gain.

`recipe-identity.json` is a versioned `recipe_workload_identity` envelope containing
the original workload fingerprint payload. `replay.json` is a versioned
`recipe_replay` log with a stable ID, source digest, validation/compatibility labels,
case/argv/cwd, current strict resolution, attempt count, actual workload status,
minimal metrics and unobserved effects. The recipe and source experiment are
immutable; only this new replay log changes from not_started/running to its
actual terminal result. A crash can leave running/unknown, never implied success.
See [recipes](recipes.md) for the supported command and storage semantics.
