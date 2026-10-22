# Measured candidate search

Stage 05 adds `simtune tune` to the strict launcher and isolated-trial APIs.
The existing `run --mode auto` still executes its one heuristic baseline plus
N tuned runs. It does not call this search. Stage 06 adds [recipes and replay](recipes.md);
project configuration, JSON/progress, IDs, show and conservative resume are
implemented. Start with the [checked heat workflow](../examples/HEAT_EXPERIMENT.md).

```bash
simtune tune --workload experiment.toml --budget 10m --out results/search-01
simtune tune --workload experiment.toml --budget 10m --out results/search-01 --dry-run
simtune tune --budget 30s --repeat 3 --out results/unchecked -- python -c 'print(42)'
simtune tune --resume results/search-01 --budget 2m
```

## Project CLI workflow

Place `simtune.toml` in the current working directory, or pass `--config FILE`
to `tune`. There is no parent/home search. Relative paths in the file use the
config file's directory. Explicit CLI options take precedence. Unknown tables
and keys fail before any workload launch.

```toml
[tune]
workload = "experiment.toml"
budget = "10m"
label = "heat-validation"
repeat = 3
save = "heat.recipe.json"
```

Run `simtune tune --show-config` to see values and their sources, then
`simtune tune` to create `runs/search-DATE-LABEL-ID/`. Existing explicit
`--out DIR` still works. `--json` emits one finite JSON result on stdout;
phase/trial progress goes to stderr. The JSON result and `show --json` have
`kind` and `schema_version` fields. `simtune show ID_PREFIX` searches direct
children of `runs/` and rejects ambiguous prefixes; exact directory paths
also work. `simtune explain recipe.json` presents scientific evidence, requested
actions and current applicability. `simtune tune --resume ID_OR_PATH` uses the
saved protocol and prior unused authorization; `--budget` adds explicit time.
Project config is ignored on resume, while explicit `--config` is rejected.

`--collectors timing|perf-stat` is **CLI-only**, defaults to `timing`, and is
included in `--show-config` with `CLI` or `default` provenance. A `collectors`
key in `[tune]` is rejected; no new config key is introduced. Resume uses its
saved collector and rejects an explicit collector override. See
[collector limits](measurements.md).

Tune JSON reports `recipe_path` only after this invocation successfully exports
the recipe. Without `--save`, or when export is skipped, it is null even if an
older file exists at the requested destination. `recipe_export` separates the
`requested_path` from the artifact: its `status` is `saved`, `skipped`, or
`not_requested`, with a machine-readable `reason` for skips. Reasons are
`incomplete_experiment`, `no_selected_policy`, `experiment_not_started` (a
pre-store preparation stop), and `dry_run`. Other statuses have a null reason.
Normal results, startup results and dry-run output include these fields, keeping
their existing kinds and exit conventions. Save errors still fail the command;
they do not produce a successful JSON result. Resume reports only its own export
request. A checked no-improvement result can export its retained reference;
export alone supplies no additional replay or scientific authorization.

Dry run reports scheduled training trials, reserved confirmation trials and
workload/setup/cleanup/validator launches per case. It creates no evidence
directory. Capability and artifact discovery still occurs. Counts are a
declared plan; candidate failures or budget exhaustion may stop execution
early. Progress reports completed scheduled trials and available phase budget,
not a timing prediction.

Use a new output directory and a strict [workload manifest](experiments.md).
A bare command has literal argv, isolated working directories, no scientific
checks and no held-out cases: its results remain **unchecked/input-specific**.
Use absolute input paths or a manifest for programs relying on relative files.
Strict CPU/NUMA execution requires Linux observations. Unsupported hosts keep a
complete exclusion record and launch nothing; macOS manual commands still work.
Dry-run launches no processes (including discovery probes, hooks and validators)
and creates no evidence directory.

## Candidate set and fixed work

The generator declares at most nine hypotheses: the inherited or deterministically
capped reference, compact/spread/one-thread-per-core CPU placement, fixed-count
OpenMP close/spread binding, and local/bind/interleave NUMA policy. It changes one
dimension at a time. It does not change source, inputs, precision, algorithms,
CPU counts or thread counts. All cases in a search currently need the same
resource declaration. Thread-count search is not implemented.

The strict planner resolves every proposal. Equivalent CPU masks are deduplicated
regardless of ordering or redundant taskset invocation. Unknown CPU/NUMA IDs are
never guessed. NUMA binding requires declared memory assumptions and known
capacity. Required-setting failures, unsupported platforms, unavailable tools,
fixed-environment conflicts and equivalent plans have explicit exclusion reasons.
A failed candidate stays in the ledger; remaining scheduled trials for it are
marked skipped with that reason. It cannot win from its surviving timings.

`--from-run DIR` reads existing simtune metrics or an [explicitly imported perf
profile](measurements.md) as hints only. Both exact workload and environment
fingerprints must match before hints may order CPU hypotheses. Imported profiles
also require a matching declared command, input identity and fixed environment.
Legacy runs lacking these fingerprints are explicitly ignored. CPU/wall ratios,
perf counters and RSS do not prove NUMA behavior.
No historical timing sample enters selection or confirmation.

## Protocol and decision rule

The protocol records its seed, case weights, minimum reduction, bootstrap method,
confirmation count, candidate ledger and balanced AB/BA schedule before execution.
`--repeat` is exploration pairs per candidate/case (default 3); `--warmups`
(default 1 pair) are retained separately and never averaged into measurements.
Each case/candidate has equally many AB and BA pairs, or a difference of one
for an odd count. The seed reproduces ordering, not physical performance.

Only successful trials with all declared scientific checks passed and compatible
argv/environment/collectors yield scores. Elapsed seconds must be positive and
finite. Failed, timed-out, unsupported, interrupted, unknown, unchecked or
incompatible evidence cannot win. Trial/pair/artifact identity reuse is rejected.
All samples, including slow samples, remain visible.

Training ranks fixed-weight mean log paired ratios `log(baseline/candidate)` and
proposes at most one challenger whose geometric ratio exceeds `1/(1-d)`.
The default required relative wall-time reduction `d` is 0.02. With no qualifying
challenger, the result is `no_improvement`; incomplete/invalid evidence gives
`inconclusive`. Neither outcome changes the reference. Training-only no-improvement can leave
confirmation absent and a saved recipe unvalidated; explicit fresh `validate`
can produce checked negative confirmation without requiring a gain.

Confirmation uses distinct held-out scientific inputs and fresh pairs, with at
least five complete pairs per held-out case, regardless of exploration repeat.
Renaming training inputs is not a holdout. Confirmation trial order must follow
training; selection and confirmation trial/pair/artifact evidence must be disjoint.
The default 2,000 seeded bootstrap replicates independently resample complete
paired log ratios within each case, then compute the weighted aggregate mean.
The two-sided 95% percentile interval uses linear interpolation of the 2.5th and
97.5th percentiles in log space, exponentiated to dimensionless ratios.

A validated improvement requires aggregate interval lower bound **strictly
greater than `1/(1-d)`**, plus every held-out case's lower bound **at least
`1/(1+r)`**, where its predeclared permitted time regression `r` defaults to 0.
A 1e-12 relative equality guard prevents floating-point roundoff from turning
threshold equality into a win. All selected-policy trials, including warmups,
must be successful and checked. A complete valid confirmation that misses a
threshold is `no_improvement`; failed/insufficient confirmation is `inconclusive`.
The reference is selected for every non-win when it is supported.

This is a conservative decision rule, not an optimum or universal speedup claim.
Five pairs give limited resolution; serial correlation, drift, selection bias,
small samples, the chosen case mix and untracked dynamic dependencies limit
inference. AB/BA balancing and independent heldouts reduce some biases, not all.

## Budgets and recovery

`--budget` is total active-job monotonic wall time, including workload loading,
discovery, hashing, persistence, warmups, setup, measurement, checks, confirmation
and scoring. Named charges classify spans; overhead captures the rest without
double counting. Discovery uses filesystem observations and tool paths, without
unbudgeted version/hwloc probes. Native I/O cannot always be interrupted; its
actual overrun is recorded and cannot authorize another launch.

Half the initial time is reserved for confirmation by default (configurable via
`--confirmation-fraction`). Exact worst-case subprocess and trial counts for the
confirmation protocol are also reserved before exploration. Counts include
setup, workload, external validators and cleanup. Manifest limits remain hard
ceilings; CLI caps may only reduce them. Repeat never multiplies authorization.
The entire declared exploration/confirmation count protocol must fit before any
trial launches. Explicit schedules are limited to 10,000 trials in stage 05; a
larger requested protocol returns inconclusive without materializing or running it.
Every subprocess timeout is capped at remaining time after preflight and durable
launch authorization. File checks also gate their start and acceptance on the
deadline. An unfunded protocol stays inconclusive.

Cleanup has an aggregate separate allowance (1 second by default), counts against
the launch ceiling and is included in reported tuning cost. Primary available
time excludes cleanup only up to that allowance, so exploratory cleanup cannot
consume reserved confirmation time. Total authorized active time is the primary
budget plus this separately declared allowance. Cleanup may finish beyond the
main deadline only within that allowance. Process teardown and filesystem
latency can also overrun; overruns are evidence, never extra authorization.
Setup/cleanup and fixed-cwd compatibility remain explicit Python API options.

`experiment.json` atomically retains the versioned search extension, schedule,
cursor, pending intent, scores and budget after every trial. `trials.jsonl` is the
existing flushed append-only lifecycle journal; each isolated trial retains raw
artifacts and checks. A separate hashed completion marker is written last for
terminal experiments. Budget exhaustion at a safe boundary leaves an incomplete,
resumable experiment with an `inconclusive` label.

`resume_search()` and the minimal `tune --resume` adapter take the prior ledger;
`--budget` adds explicitly authorized seconds. Without it, only prior unspent
time is usable. Time while the job was stopped is excluded, recovery startup is
included, and launch/trial caps never reset. An OS file lock prevents concurrent
resume and releases after a crashed process. Workload/environment identity,
schedule linkage, terminal records and the journal must agree. Uncertain,
partially journaled or interrupted work is never automatically repeated.
Completed experiments are immutable. Config/ID/JSON resume is implemented; no
uncertain work is retried. Captured trial/validator logs have no configured size
ceiling and are buffered in memory; file-backed streaming remains future work.

## Results and costs

Outcomes are `validated_improvement`, `no_improvement`, `inconclusive`, `failed`
and `interrupted`. A workload success alone never implies scientific correctness.
CLI statuses are 0 for validated improvement/no improvement, 1 for inconclusive
or validation failure, 2 for invalid/incompatible configuration, 124 for a trial
timeout, and 130 for interruption. Actual workload statuses remain in trial records.

Per-case raw ratios, intervals, log-ratio variability and arithmetic seconds stay
in the search extension. Tuning cost uses the full job as a conservative additional
cost, including reference runs. Conditional break-even is
`ceil(tuning_seconds / validated_seconds_saved)` only for positive validated
savings, where savings are the difference of weighted arithmetic held-out means
under the predeclared case mix. A geometric ratio is never converted to seconds.
Otherwise break-even is null. These timings do not represent allocated CPU-hours,
consumed CPU seconds or scheduler charges.
