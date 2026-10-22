# Decision report and hardware provenance

R04 adds a read-only projection shared by `tune`, `show`, and `explain`. JSON
retains existing keys and adds `decision_report` with
`kind: "simtune_decision_report"`, `schema_version: 1`. Normal human output
renders the same projection as a case/policy table and explanations. Rendering
launches nothing, exports nothing, and changes no experiment or recipe bytes.
`show` does not require a recipe. Old manual run-schema-2 inspection is unchanged.

The report's fields are:

| Field | Meaning |
| --- | --- |
| `outcome`, `explanation`, selected/reference IDs | Domain decision and the saved reason; the projection chooses no winner |
| `complete`, `completion_marker`, `phase` | Actual completion check and recorded search phase |
| `case_policy_rows`, `aggregates` | Training selection and held-out confirmation, fixed weights, checks, reasons and eligible paired measurements |
| `historical_validation` | Recorded scientific-check counts and recomputed R01 protocol validity/reasons, separately from performance outcome |
| `settings` | Requested policies, distinct resolved plans, actual trial actions and declared resource-count equality |
| `trials`, `candidates` | Raw evidence references, unsuccessful/unchecked/unknown outcomes and exclusions retained |
| `cost` | Full recorded tuning seconds and budget ledger; conditional arithmetic seconds saved/run and break-even |
| `hardware` | Recorded processor observations, normalized class or explicit missing-data reasons |
| `applicability` | Historical-only scope; current compatibility is unchecked by this projection |
| `evidence`, `evidence_errors`, `limitations`, `unavailable` | Evidence locations, diagnostics and reasons for absent values |

`complete_pair_count` counts structurally complete reference/candidate pairs
with distinct trial orders; it does not certify science or eligibility. Check
states and `eligible` are separate. A candidate with failed or excluded evidence
has no numeric score in the report, even if some surviving pairs were fast.
Incomplete/uncertain journal evidence prevents scoring; intact partial trial
records remain visible. If older records lack the newer evidence files, `show`
retains their metadata and exposes an unavailable report with diagnostic reasons.

Elapsed values are arithmetic means in **seconds**. The paired geometric ratio
is dimensionless baseline/candidate (`B/C`), with the domain's seeded 95% paired
bootstrap interval. For ratio `R`, time reduction in **percent** is
`100 * (1 - 1/R)`; both interval endpoints use that same increasing transform.
A ratio of 2 is 50% time reduction, 1 is 0%, and 0.5 is -100%. Missing intervals
remain missing, with reasons. No geometric ratio is converted into seconds.

Break-even is `ceil(full_tuning_seconds / positive_validated_seconds_saved)`.
Savings use the existing normalized, declared held-out case weights and
arithmetic reference minus candidate seconds, only after complete validated
improvement evidence. No gain, invalid protocol, nonpositive savings or partial
evidence leaves break-even null. Comparable future executions under that case
mix are a condition, not a promise. Historical probe counts may be unknown;
the recorded time/count ledger is neither repaired nor reset.

Bare-command tuning warns on stderr before search starts that it is unchecked,
input-specific and cannot select an eligible winner. The warning points to
[the checked heat example](../examples/HEAT_EXPERIMENT.md) and
[workload checks](experiments.md). That heat document supplies the implemented
manifest generator, pinned validator, strict tune/validate/replay path and the
independent manual checked harness. No unchecked
candidate ranking or implicit scientific checks are added.

## Hardware identity extension

New discoveries add `ResourceSnapshot.extensions["simtune.hardware"]`, extension
`schema_version: 1`, `class_algorithm: "processor-class-v1"`. The outer resource
schema stays 1. Fields are `architecture`, `source`, `processors`, `complete`,
and `unavailable`. Each processor records `cpu_id`, `vendor`, `model`,
`attributes`, and local `unavailable` reasons. Only eligible CPUs are retained.

Linux reads `/proc/cpuinfo` through the existing bounded read-only provider
(1,048,576 characters); it launches no subprocess and needs no dependency.
Architecture comes from the process's machine observation. `amd64` normalizes
to `x86_64`, and `arm64` to `aarch64`. Linux x86 needs each CPU's `vendor_id` and
`model name`; available family/model/stepping identifiers enrich its identity.
Linux ARM needs each CPU's implementer, part, architecture, variant and revision.
Global board names and generic Processor strings cannot fill missing per-CPU
data. Duplicate processor IDs, incomplete rows, unsupported architectures and
unreadable/oversized sources leave class evidence unknown. No first-row fallback
is used for heterogeneous hosts. macOS has no supported processor provider here.

The normalized class hashes architecture and every eligible CPU's vendor/model/
attributes in sorted eligible-CPU order. It excludes absolute CPU IDs, host names
and source paths. This order aligns with the separate topology normalization;
some equivalent permutations conservatively require revalidation. Different
models at equal topology are different classes. Missing evidence is never a
class match. CPU identity does not replace topology relationships, and the class
does not track memory technology, frequencies, firmware, dynamic dependencies or
background load. Parsing tests provide no native observation or cross-machine
performance-transfer evidence; native CPU observations belong to R06.

## Strict replay migration

New recipes write stage-06 extension **version 3** with recorded `hardware` class
context and `label_scope`. Versions 1 and 2 remain inspectable through the
existing evidence-recomputation paths. Old resources and recipes gain no CPU
identity from the host reading them. Older readers that support only version 2
will reject version 3 and need updating.

New observations participate in the existing resource/environment fingerprint.
An older incomplete experiment can therefore require a new experiment when
current discovery adds processor evidence; resume cannot mix historical unknown
processor context with newly observed context. Inspection performs no backfill.

The retained JSON `label` and stage-06 `replay_validated` keys describe only
historical science/protocol/runtime/topology evidence. Human messages say
**historically validated** or **historically unvalidated**. `explain --workload`
separately reports `current_compatibility`, `hardware_class` status and
`validated_reuse_authorized`. A mechanical pass alone does not authorize
validated reuse. Current performance is always unproven.

`exec` requires historical validation **and** known matching recorded/current
hardware class for override-free reuse. A changed or unknown class, including
old recipes without version-3 hardware provenance, requires explicit fresh
`validate` to create a derived recipe, or `--allow-unvalidated` to acknowledge
absent performance evidence. The override does not bypass workload identity,
topology/resource/required-setting conflicts, evidence integrity or R01 protocol
consistency. A matched class cannot upgrade unchecked or reused-input evidence.
The rule is refreshed immediately before launch.

Replay stderr, dry-run JSON and `replay.json` distinguish historical validation
and performance outcome, current mechanical compatibility, observed class
match/changed/unknown, and `current_output_validation: "unchecked"`.
`validated_reuse_authorized` refers to this conservative authorization rule,
not validation of the current output or its speed. Every exec runs exactly once,
with no hidden reference, validators, retuning, migration or automatic retry.
