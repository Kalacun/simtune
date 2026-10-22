# Shared implementation contracts

Current reading note: this document retains the original staged design language
and planned surface below. Manifests/trials, strict search, recipes/replay,
config/show/resume and collectors/imports are now implemented; Slurm/ensemble
commands remain deferred. Use the [current user guide](../user-guide.md) and
[release status](release-plan/STATUS.md) for actual commands and acceptance.
The design text alone is neither implementation nor native evidence.

This is the normative design for the extension. Stage 01 implements the typed
records, format validators and synthetic fixtures documented in [formats](../formats.md).
Execution, discovery, search and replay described here remain **planned** until
their stages pass. Later interface changes require an explicit rationale, updated
producers/consumers and compatibility tests in the same handoff.

## Product and compatibility

- Launch unchanged, trusted scientific programs. Scientific parameters, input,
  seeds, precision and build remain fixed across each candidate comparison.
- New optimizer initially supports one command/process tree on one Linux node.
  macOS/WSL retain manual functionality; unsupported search dimensions are
  reported explicitly. Do not turn unknown topology into a guessed CPU mask.
- Existing five commands, argv behavior, `auto`'s N+1 executions, manual fallback,
  exit codes and run-schema-2 readability remain compatible. Additive flags must
  retain existing defaults. Do not put new orchestration in the legacy heuristic.
- Core dependencies remain standard-library first (dataclasses, tomllib, JSON).
  Runtime adapters/tools remain optional. Do not require an LLM service.
- No sudo, host sysctls, governor changes, daemon, implicit shell, automatic
  workload retry, job submission or installation of system tools.

## Records and versioning

Use small typed records at new boundaries; keep the existing dictionaries where
rewriting them would add no value. All serialized records have an independent
`schema_version: 1`, explicit kind, units and stable IDs. Reject unsupported
versions clearly; never reinterpret legacy run schema as a recipe schema.

| Record | Required content |
| --- | --- |
| ResourceSnapshot | Platform; online CPU IDs; core/socket/SMT/NUMA relationships; inherited affinity; allowed memory nodes; permitted envelope when known; memory limits with source; unavailable fields/reasons |
| WorkloadSpec | argv template; resolved executable; declared code/input artifacts and hashes; base directory; declared runtime; fixed env allowlist; permitted case substitutions; training/validation case IDs; output checks; limits |
| CaseSpec | Stable ID; input class; explicit argument values/files; finite positive weight; scientific check/reference; resource needs when known |
| PolicySpec | Stable ID; logical CPU placement; NUMA policy; declared runtime settings; CPU/thread/memory budget; required versus optional settings; generation rationale |
| TrialRecord | Experiment/case/policy/pair IDs; phase and order; argv/cwd/env overrides; start/end; requested/resolved/observed actions; return code/timeout; metrics; validation; raw-artifact paths |
| ExperimentRecord | Workload/environment fingerprints; candidates and exclusion reasons; objective; seed; budget ledger; journal references; selection rule; training and held-out evidence; outcome |
| Recipe | Workload family and variable arguments; logical selected policy; resource constraints; compatibility rules; required capabilities; validation state; evidence references; performance outcome |
| Metric | Name; finite value or null; unit; collector/version; process/job scope; availability reason; raw source; scaling/multiplexing information where supplied |

Finite values only, with booleans excluded from numeric fields. Unknown is null
with a reason, never zero. Serialize argv as arrays; never reconstruct executable
commands from formatted display text. IDs must be safe path components. Relative
artifact references cannot escape their record's root; test symlinks as well as
`..`. Schemas may include a documented `extensions` mapping, not silent typo acceptance.

New experiments use atomic record replacement plus a flushed append-only trial
journal, with a separate completion marker. An interrupted trial is `interrupted`
or `unknown`, never implicitly successful. Resume does not repeat an uncertain
side-effecting trial automatically. Legacy run directories are not rewritten.

## User explanations and stage-01 format clarifications

The records also explain to a user what was requested/applied to their unchanged
program, which tools or runtime mechanisms were involved, and why. This is a
required purpose alongside interchange and configuration reuse. `SettingSpec`
records the target, name, value, unit, required flag, rationale and expected
(unproven) effect. `ActionRecord` records tool/version/mechanism, resolution,
application status, observed value, observation scope, explanation and evidence.
`ActionRecord.describe()` renders these fields without executing anything.
Future explain/show producers must retain this provenance in both human and
machine output. A tool's presence, planned use or successful invocation is not
proof of acceleration or every thread/page's placement. Report that distinction,
including missing observations, skipped/unsupported/failed settings and unchecked
science. Collectors name measurement tools separately from tuning mechanisms.

Version 1 has eight record kinds (CaseSpec is independently versioned), listed
with exact fields in [formats](../formats.md). Nested value objects belong to
the enclosing schema. All fields except `unavailable` and `extensions` are
required on JSON input; nulls need local field-name reasons in `unavailable`.
TOML omits nulls and retains those reasons. Only typed nullable fields are
restored; extensions are opaque. ID syntax is ASCII alphanumeric followed by
alphanumeric/underscore/hyphen, maximum 128 characters. Record parsing validates
shape/local consistency, not host compatibility or truth of referenced evidence.

Evidence/output paths are relative to the record root and checked through
symlinks when a root is supplied. Workload external artifact identities are a
separate namespace, resolved against the manifest/base by stage 04. No workload
path resolution, hashing, journaling or launch is part of stage 01. Recheck path
confinement at actual file access; a prior check is not a race-proof sandbox.

The default reference budget is inherited; smaller CPU budgets require a
resolved deterministic reference mask before execution. An unresolved intent
may carry an explained null mask. An unconstrained inherited comparison is
separately labeled. Top-level placement/NUMA intent and declared budgets in
PolicySpec are required; optional per-setting flags cannot downgrade them.
Recipe compatibility cannot disable identity/runtime/topology checks; a count
match alone is insufficient. Unchecked science or absent held-out confirmation
requires explicit unvalidated replay and remains labeled. The statistical rule
below remains unchanged: dimensionless paired ratios, default d=0.02 and r=0,
95% seeded paired bootstrap and at least five confirmation pairs per case.
Resume without additional budget can use only unspent prior authorization;
`--budget` adds time to the existing ledger. Overruns remain recordable evidence,
not authorization for further work. Unknown/interrupted work is never retried
automatically. Later stages enforce these execution and statistical gates.

## Resource boundaries and policy resolution

The execution ceiling is the intersection of known online, permitted and
inherited CPU sets. Preserve a narrower inherited task mask by default even if
the allocation/cgroup is larger. Scheduler counts are not CPU IDs. If any source
conflicts, retain the conservative intersection and record the conflict.

R03 clarification: `effective_cpu_ids` is the intersection of **available**
observations, useful even for partial discovery/doctor. It is not by itself
authorization to rebind. Strict concrete CPU-budget resolution requires an
observed inherited task mask (`sched_getaffinity(0)` and/or the kernel's
`/proc/self/status` `Cpus_allowed_list`), including policies applying OpenMP
placement controls. A cgroup/allocation envelope cannot establish a narrower
voluntary mask. Missing inherited affinity excludes strict plans and survives
the immediate pre-launch refresh; missing optional cgroup data alone does not
exclude a known inherited mask. Manual inherited/best-effort behavior is retained.

Read memory-node restrictions separately from CPU affinity. cgroup limits can
be tighter than host RAM and ancestor limits can be tighter than a leaf. Unknown
capacity must not become a guarantee that a strict memory binding will fit.

R03 clarification: every strict policy, including inherited NUMA, rejects a
declared memory need above any known allocation cap (including ancestors).
Changing memory policy additionally checks selected-node capacity. Search and
replay use the same strict planner for these checks. Unknown/unlimited allocation
caps supply no finite guarantee; inherited NUMA does not require invented node
capacity. A declared need is neither enforced RSS nor reserved memory, and node
totals do not establish free/reserved capacity.

Default to the inherited resource budget. CPU/thread-count search is opt-in.
If the user declares a smaller CPU budget, define and record a deterministic
resource-capped reference mask before comparing equal-budget candidates. Label
an unconstrained inherited comparison separately; do not claim a same-resource
placement improvement from reducing or increasing allocated resources.

CPU placement candidates must actually differ in resolved behavior. Reordering
the same taskset mask is not a new placement. Deduplicate equivalent plans.
NUMA options initially: inherited, local allocation, binding to eligible nodes
with declared capacity assumptions, and interleaving eligible nodes. No guessed
absolute IDs or widening through raw numactl CPU options in the new strict path.

The new `strict` path refuses missing required capabilities, fallback, semantic
adjustment, empty masks, unsupported requested limits and known conflicts before
workload launch. Resolving a logical policy to a concrete eligible mask is not a
semantic downgrade. Runtime tool failures propagate without rerunning the workload.
Record requested/planned/observed states separately: successful preflight is not
proof of every page's residency or every application thread's binding. Observe
initial affinity/policy where feasible and label unverifiable effects honestly.

## Workload identity and scientific checks

TOML is the native project/workload input; JSON is the exchange format. No YAML
dependency for new core records. Existing YAML profiles remain supported.
Resolve executable and declared file paths relative to the manifest, not an
agent's working directory. Hash the executable and declared source/input files;
record versions/identity of declared runtimes and relevant environment. Do not
claim to fingerprint all dynamic dependencies, containers or network state.

Only explicitly declared whole-argument placeholders such as `{case.input}` and
`{trial_dir}` are expanded. No eval, shell, arbitrary attribute access, implicit
environment expansion or substring substitution. Validate unknown placeholders.
Replay may vary only declared case arguments; different scientific inputs are
allowed across experiments, never across policies within one comparison case.

Default to a separate working/output directory per trial. Stage 04 must define
an explicit compatibility path for programs needing a fixed working directory.
Setup, cleanup and validator commands are argv arrays, opt-in, timed, recorded
and given only their trial's workspace. Do not delete paths outside that root.

Checks support exact file hashes, finite numeric JSON fields with explicit
absolute/relative tolerances, and a user-supplied validator command. A successful
exit alone is `unchecked`. Unchecked results may be explored, but cannot become
scientifically validated recipes. Strict replay of unchecked evidence requires
an explicit `--allow-unvalidated` override and retains that label.

## Experiments, budgets and selection

Initial objective: elapsed seconds for fixed work and resources. Case weights
are declared before execution; aggregate using weighted mean log paired ratios
(baseline/candidate), reported also as a geometric mean ratio. Keep each case's
results and failures visible. Do not mix units or pool different hardware cases.

Use a small deterministic candidate generator informed by profiling. Old/imported
profiles propose hypotheses, never replace fresh paired evidence. Use seeded
balanced AB/BA order per case, with warmups recorded separately and no outlier
removal. Baseline/candidate in a pair use the same collectors and case inputs.

Budget is wall time spent on this tuning job: discovery, warmups, setup, workload,
checks and confirmation. Track it with a monotonic clock. Before every subprocess,
cap timeout to remaining budget; no new trial starts after exhaustion. Bound
cleanup separately and document its possible overrun. Also enforce a maximum
launch/trial count; do not multiply budget silently by `--repeat`. Reserve a
declared validation budget before exploratory trials consume it.

R03 clarification: the subprocess cap includes collector discovery probes.
Both perf preparation commands require separate exploratory launch reservations
and timeouts capped to `min(2 seconds, remaining primary exploratory time)` at
startup and resume. Declare confirmation time/count reserves before probing;
probes cannot borrow them or the cleanup allowance. Charge actual elapsed time,
including failure, persistence and teardown, once; bounded teardown may overrun
the primary deadline but grants no later launch. Timing and dry-run start no
collector probes. Exhaustion is inconclusive, distinct from a failed/unavailable
tool, with no fallback or workload retry. Before a store exists, expose a
structured startup result; during resume, persist reservations/failure evidence
without a completion marker. Uncertain persisted probe intent blocks resume.
Old ledgers retain their spent time/counts exactly: absent historical probe
counts remain explicitly unknown, never reconstructed or reset. Explicit
additional time does not extend launch-count authorization.

Training trials propose one winner. Require independent held-out cases and fresh
paired confirmation before marking a performance improvement validated. Without
held-out cases, report input-specific/provisional results and require explicit
unvalidated replay. Do not reuse selection samples as confirmation samples.

Prespecify a minimum relative wall-time reduction `d` (default 0.02, configurable),
a 95% paired-bootstrap interval method/seed, and at least 5 complete confirmation
pairs per case. A validated improvement requires the aggregate ratio interval's
lower bound to exceed `1 / (1-d)`, correct outputs, and no failures of the selected
policy in its training/confirmation trials. Every held-out case's ratio interval
must also rule out regression beyond its declared relative wall-time tolerance
`r` (default 0): lower bound at least `1 / (1+r)`. Other failed candidates remain
visible but cannot win. Ratio increase and time reduction are different units.
This is a deliberately conservative decision rule, not a proof
of optimality; document small-sample, correlation and selection limits. If budget
cannot fund the declared protocol, return `inconclusive` and retain the reference.
Stage 05 may refine the statistical method only with an explicit design record,
tests and unchanged separation of selection and confirmation.

Outcomes: `validated_improvement`, `no_improvement`, `inconclusive`, `failed`,
`interrupted`. Report every excluded/failed candidate and reason. Never score
timeouts or failed commands as fast runs. No-improvement is a useful result.

Tuning cost and expected remaining runs are separate from scientific timings.
Report break-even `ceil(additional_tuning_seconds / validated_seconds_saved)`
only for positive savings, using a declared expected case mix and savings in
seconds rather than converting a geometric ratio directly to seconds. No finite
break-even otherwise. Allocated CPU-hours,
consumed CPU seconds and scheduler billing are different quantities.

## Recipes and new command surfaces

Stage 01 freezes exact parser names and fixtures based on this surface:

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

These are **planned commands**. Bare-command tuning is input-specific/unchecked
unless checks are supplied through a manifest; it must say so. Resume budget is
additional authorized time with the prior ledger retained. Dry-run variants
launch no workload, validator or setup hook and create no evidence directory.

Recipes save logical intent and the concrete masks used for evidence. Resolve
against the new allocation; matching CPU count alone is insufficient. Validate
workload family, runtime, topology class, resources, required settings and input
class. Fail on incompatibility; do not silently retune or substitute a profile.
Changed conditions require explicit revalidation. Write a derived validation
record; preserve the original recipe and evidence. Unvalidated overrides remain
clearly labeled in both terminal and machine-readable output.

R04 strict-path clarification: preserve historical science/protocol/performance
facts separately from current mechanical compatibility and unchecked exec output.
New discoveries/recipes carry versioned processor-class provenance, independent
of topology. Override-free reuse additionally requires known matching recorded
and current classes. Changed/unknown class evidence, including older recipes
without it, requires explicit derived revalidation or an acknowledged
`--allow-unvalidated` replay; the latter bypasses no mechanical/evidence gate.
A class match supplies context, never a speed/output guarantee. Old records stay
readable and are not enriched from the reading host. See [R04's report contract](../decision-report.md).

`exec` uses minimal instrumentation by default, preserves signals/exit codes and
does not insert a fresh baseline. Its logs identify the recipe/resolution and
whether compatibility/required-setting checks passed. Full profiling is opt-in.
New orchestration commands distinguish successful no-improvement (exit 0),
inconclusive/validation failure (exit 1), invalid config/incompatibility (exit 2),
timeout (124), interrupt (130); retain actual workload statuses in records.
`exec` propagates the workload's status using existing shell conventions.

## Slurm, ensembles and scope

Slurm support is one-node/one-task initially. Discover within the actual step,
honor inherited masks and memory restrictions, and treat env counts as metadata.
Never interpret scheduler CPU numbering as Linux IDs. Produce reviewable snippets;
do not submit jobs or claim whole-job/rank metrics from a launcher process.

Ensembles run independent experiments on one node with explicit task and combined
memory budgets. Worker CPU sets are disjoint by default. Each worker runs in a
separate process; do not use threaded calls to existing preexec/rusage code.
Choose layouts using completed valid experiments per makespan at equal allocated
resources; account for failures and startup overhead. Integrate with scripts and
existing workflow engines rather than implementing their dependency scheduler.

MPI, multi-node, GPU and energy optimization remain out of scope for all stages.
