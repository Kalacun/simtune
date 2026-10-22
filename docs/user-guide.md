# simtune user guide

simtune launches unchanged, trusted programs under inspectable process settings.
Use it when you run the same scientific solver or analysis repeatedly and want to
measure placement/runtime policies before reusing one. Scientific inputs, seeds,
precision, source, algorithms, binary and resource counts stay fixed within a
comparison. A launch setting is a hypothesis, not a promise of acceleration.

This guide describes version 0.1.0's actual command parsers and execution paths.
All eleven commands below accept `-h` / `--help`; `simtune --version` prints the
package version and `simtune --help` lists commands. Put simtune options **before**
`-- COMMAND ARG...`. The command tail is an argv list: simtune does not interpret
pipes, redirects, variable expansion or shell expressions. Your interactive shell
still processes the command you type; quote literal arguments appropriately.

## Installation and choosing a workflow

From a source tree or unpacked sdist, with Python 3.11–3.14:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
simtune --version
```

The base package has no third-party runtime dependencies. Optional
`python -m pip install '.[yaml,rich]'` enables explicit YAML profiles and richer
tables. Development checks use `.[dev]`. Wheels contain the Python package;
examples, tests, profiles, public docs and the native preparation kit are in the
source distribution. Neither installation nor a helper installs system tools.

| Need | Route | Execution and evidence |
| --- | --- | --- |
| See what this host offers | `doctor`, `list-modes` | Discovery and profile descriptions; no scientific workload |
| Establish manual baseline or apply a known mode | `profile`, `run`, `compare`, `show` | Best effort, descriptive metrics; no automatic scientific checks |
| Test fixed-resource placement hypotheses | `tune --workload` | Strict Linux policies, checked isolated pairs, budgets and held-out confirmation |
| Inspect or reuse measured settings | `explain`, `exec` | Historical evidence and current compatibility are separate; exec runs once with current output unchecked |
| Obtain new checked evidence | `validate` | Fresh reference/policy pairs and derived recipe; source evidence stays immutable |
| Preserve external counter diagnostics | `import-profile` | Declared external provenance; hints only, never fresh selection evidence |

Manual profiling works on macOS. Strict CPU/NUMA search and replay require actual
Linux resource observations; macOS tune records unsupported exclusions and does
not launch strict trials. WSL has guest limits. Native Windows workload execution
is unsupported. See the [capability matrix](capability_matrix.md) and
[candidate review summary](candidate-review.md). The source archive ownership
repair is covered by header regressions and actual artifact audits. A measured
manual-profile example is available in the [results](results/README.md). See the
candidate review for current local and hosted checks. Dedicated Linux performance,
physical NUMA, native perf and Slurm remain unverified. Configured CI and synthetic
hosts do not establish those passes.

## A runnable macOS manual tutorial

Run these small, harmless commands from an installed environment. This tutorial
uses built-ins and needs no extras or compiler. Python's integer sum is only a
repeatable demonstration, not a scientific benchmark or speedup claim. New
measurements go to a new directory; existing runs remain intact.

```bash
simtune doctor
simtune list-modes
python - <<'PY'
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from simtune.lookup import find_run, show_run

root = Path(tempfile.mkdtemp(prefix='simtune-manual-'))
command = [sys.executable, '-c', 'print(sum(i*i for i in range(100000)))']
def call(*args):
    subprocess.run(['simtune', *args], check=True, timeout=60)

call('profile', '--out', str(root / 'baseline'), '--repeat', '2',
     '--timeout', '10', '--', *command)
call('run', '--mode', 'desktop_friendly', '--out', str(root / 'manual'),
     '--repeat', '2', '--timeout', '10', '--', *command)
baseline = next((root / 'baseline').iterdir())
manual = next((root / 'manual').iterdir())
call('show', str(baseline), '--json')
call('compare', str(baseline), str(manual), '--out', str(root / 'comparison'))
call('run', '--mode', 'auto', '--out', str(root / 'auto'),
     '--repeat', '2', '--timeout', '10', '--', *command)
assert len(list((root / 'auto').iterdir())) == 2  # one baseline + one repeated run
for run in (root / 'auto').iterdir():
    report = show_run(find_run(str(run)))
    print(run.name, 'iterations:', len(report['metrics']['iterations']))
print('Artifacts:', root)
print('Comparison:', json.loads((root / 'comparison/compare.json').read_text()))
PY
```

Inspect the resolved mode and skipped/adjusted actions. On macOS Linux-only
wrappers are unavailable; `desktop_friendly` usually applies nice level 5.
Auto adds **one baseline execution** before the requested two repeats: N+1
executions, stored in two run directories. A failed auto baseline stops the
sequence. Use manual modes for commands with external side effects and count
all repeats deliberately. No timeout bounds total N-repeat wall time unless an
outer caller does so. `--dry-run` previews manual plans without running the
workload, but manual capability detection can invoke harmless bounded probes.

## `doctor`: inspect capabilities

```bash
simtune doctor
```

No options beyond help. Prints platform/WSL, CPU/topology/restriction information,
tool paths/versions, functional probes, reasons for unavailable capabilities and
limits of observations. It can run bounded harmless discovery probes. It writes
no run directory and normally exits 0, even when some tools are unavailable.
Use it before designing a workload policy. Installed tools or an observed launcher
mask do not prove every worker's affinity, page residency or performance.

## `list-modes`: inspect manual profiles

```bash
simtune list-modes
simtune list-modes --profiles profiles/default.yaml
```

Prints a name/description/required-action table; launches no workload and stores
no measurements. `--profiles PATH` is repeatable and requires the YAML extra.
Explicit files replace the built-in collection; later files override earlier
names. Invalid profiles fail with exit 2. Built-ins are `desktop_friendly`
(gentler priority), `throughput` (available CPU affinity and optional interleave),
`isolated` (systemd user scope), and `latency` (priority and optional affinity).
They are initial policies to inspect, not measured winners or security isolation.

Custom profiles can declare `nice`, `ionice`, `taskset`, `numactl`,
`cgroup_systemd`, `env`, `ulimit` and macOS `mac_taskpolicy`. Each profile needs
`description`, `safety_notes`, `actions` and `fallback_mode`; each action's
`optional` defaults to false. Review the [profile schema and field table](profiles.md)
for exact shapes, resource units, trusted raw arguments and fallback behavior.
For example an `env` action with `vars: {OMP_NUM_THREADS: "2"}` changes the child
only; it cannot prove a runtime used two threads. Custom actions stay in the
manual workflow and do not become arbitrary strict search dimensions.

## `profile`: measure an unchanged baseline

```bash
simtune profile --out results/baseline --repeat 3 --timeout 60 -- ./solver input.dat
```

| Option | Default / meaning |
| --- | --- |
| `--out DIR` | Required parent for newly allocated run directories |
| `--repeat N` | 1; positive integer workload executions |
| `--timeout SECONDS` | None; finite positive per-iteration bound when supplied |
| `--dry-run` | Off; print a plan and create no run directory |
| `COMMAND ARG...` | Required argv after `--`; runs from the caller's cwd |

Uses the legacy profiler's compatible time/perf paths and sequential child
resource counters. Prints the saved directory. Use this to establish descriptive
manual measurements under the inherited environment, including wrapper overhead.
It performs no output validator. Exit zero certifies execution only. Wrappers
can affect timing; compare equivalent measurement paths. See [storage](storage.md)
for units, raw metrics and missing values.

## `run`: apply a manual mode or heuristic auto

```bash
simtune run --mode desktop_friendly --out results/manual --repeat 3 --timeout 60 -- ./solver input.dat
simtune run --mode auto --respect-desktop --out results/auto --repeat 3 --timeout 60 -- ./solver input.dat
```

Uses the same `--out`, `--repeat`, seconds-only `--timeout`, `--dry-run` and argv
as profile. Additional options:

| Option | Default / meaning |
| --- | --- |
| `--mode NAME` | Required built-in/custom name or `auto` |
| `--profiles PATH` | None; repeatable explicit YAML collections |
| `--latency-important` | Off; auto-only hint preferring latency |
| `--respect-desktop` | Off; auto-only hint preferring desktop-friendly settings |
| `--notify` | Off; request an optional completion desktop notification |

Unknown modes and auto-only hints on manual modes fail with exit 2. Auto requires
all four built-in profile names even with custom files. It uses one profiled
baseline and heuristics, then N repeated executions of the chosen mode; it does
**not** run paired search, validate science or establish an optimum. Dry-run
omits that baseline. Manual mode executes N times, not N+1.

Unavailable required *action types* can select the declared fallback; optional
actions may be skipped. Later concrete constraints may still be adjusted/skipped.
The resulting mode/actions and explanation are recorded. A wrapper/runtime
failure is retained as a failed iteration, never retried without the wrapper.
Best effort preserves compatibility; use tune/exec for required strict settings.
Manual repetitions may repeat failed commands according to the explicit repeat
count; parent cancellation stops further repetitions. These are trusted process
settings, with no root tuning or host-global optimization.

## `compare`: export descriptive manual comparisons

```bash
simtune compare results/baseline/run_ID results/manual/run_ID --out results/comparison
```

`run_dirs` is one or more saved legacy/schema-2 run directories, not experiment
IDs. `--out DIR` defaults to the current directory. Prints a comparison table and
writes/overwrites `compare.json` and `compare.csv`. Recomputes aggregates from raw
iterations, preserving success/failure/timeout counts and excluding failed timings
from successful means. Aggregate-only historical records remain unknown; legacy
readability does not establish truth or reproducibility. CSV neutralizes formula
cells; JSON retains original values. Invalid/missing records fail with exit 2.
A successful compare exits 0 even if an input records failed workloads: read the
counts. This command provides descriptive statistics, no confidence intervals,
selection correction or causal attribution. Use paired tune/validation for the
[separate statistical decision protocol](search.md#protocol-and-decision-rule).

## `show`: inspect saved records

```bash
simtune show results/search
simtune show search-PREFIX --runs-dir results --json
```

`run` is an exact directory or unique direct-child ID prefix. `--runs-dir DIR`
defaults to `runs`; ambiguous prefixes fail rather than pick a run. `--json`
defaults off and emits a finite, versioned `simtune_show` projection. Human output
shows decision, phase, budget, exclusions and next resume suggestion for a search;
legacy records show schema/aggregates. Reads only, no launch or rewrite. An
incomplete record remains incomplete. Missing/corrupt records fail with exit 2.
Use [decision report](decision-report.md) to distinguish raw ratio, time reduction,
conditional break-even, historical validity and current applicability.

## `tune`: bounded, strict paired search

```bash
simtune tune --workload heat.toml --budget 10m --out results/search --save heat.recipe.json --dry-run
simtune tune --workload heat.toml --budget 10m --out results/search --save heat.recipe.json --json
```

Choose exactly one of `--workload FILE` or `-- COMMAND ARG...`. A checked version-1
TOML/JSON manifest pins executable/artifacts, runtime, fixed environment, resource
budget, predeclared case weights, training and distinct held-out cases, and output
checks. Relative manifest/base/artifact paths have defined resolution; hashes are
verified at load and launch. Only whole `{case.NAME}` and `{trial_dir}` arguments
substitute, with no shell expansion. The [experiment guide](experiments.md) and
[format reference](formats.md) define these records. Scientific checks are exact
output `file_hash`, finite `numeric_json` values with explicit tolerances, or a
bounded `command` validator (exit zero means passed). Validator/interpreter/source
identities must be declared; malformed/nonfinite or failed checks cannot win.

Bare commands pin the executable only and remain unchecked/input-specific, with
no held-out cases. They run in isolated trial cwd; use absolute inputs and avoid
assuming the invocation directory is preserved. They cannot earn validated recipe
reuse. Isolation separates local output, not external side effects or malicious
commands. Undeclared files/libraries/network state remain untracked.

| Option | Default / meaning |
| --- | --- |
| `--workload FILE` | None; strict manifest, mutually exclusive with command tail |
| `--budget DURATION` | Required for a new search; seconds or suffix `s`, `m`, `h`; additional authorization on resume |
| `--out DIR` | New `runs/search-DATE-LABEL-ID` directory if omitted; existing destinations refused |
| `--save FILE` | None; export a new recipe plus sibling `FILE.evidence` bundle when eligible |
| `--repeat N` | 3 exploration pairs per case/candidate; integer ≥1 |
| `--warmups N` | 1 warmup pair per training case/candidate; integer ≥0, retained but not scored |
| `--seed N` | 0; nonnegative schedule/bootstrap seed, not a promise of repeatable timings |
| `--confirmation-pairs N` | 5; integer ≥5 fresh pairs per held-out case |
| `--confirmation-fraction F` | 0.5; strictly between 0 and 1, time reserved for confirmation |
| `--minimum-improvement F` | 0.02 relative time reduction; 0≤F<1 |
| `--max-trials N` | Manifest ceiling (bare command 100); positive CLI value may only lower it |
| `--max-launches N` | Manifest ceiling (bare command 300); includes validators/hooks/probes; CLI may only lower it |
| `--timeout DURATION` | Bare command 60s; manifests keep their own trial timeout |
| `--from-run DIR` | None; exact identity-matching legacy/imported hints only; old timing never scored |
| `--collectors timing\|perf-stat` | `timing`; CLI-only, saved in protocol; optional perf diagnostics cannot authorize a winner |
| `--config FILE` | Explicit TOML or current-directory `simtune.toml` if present |
| `--show-config` | Off; print effective options and default/CLI/config-file sources; zero discovery probes/launches |
| `--label NAME` | `run` in generated ID; safe ASCII name, maximum 64 characters |
| `--json` | Off; one versioned result on stdout; progress/diagnostics on stderr |
| `--dry-run` | Off; load/hash/discover and print protocol, but no subprocesses or evidence directory |
| `--resume ID_OR_PATH` | None; continue saved never-started work with unspent authorization |

The candidate set is small and deterministic: inherited/capped reference,
compact/spread/one-thread-per-core placement, fixed-count OpenMP close/spread, and
NUMA local/bind/interleave, subject to strict feasibility/deduplication. CPU and
thread counts do not vary. Lost required settings, unknown inherited affinity,
known memory conflicts or unsupported tools/platforms exclude candidates before
launch; immediate prelaunch discovery must still agree. All trials and failures
remain evidence, including slow trials. A successful wrapper is not proof of
all worker affinity or physical page placement. [Strict launch](strict-launch.md)
defines that boundary.

Training uses predeclared weighted mean log paired baseline/candidate ratios;
ratios >1 favor the candidate. At most one challenger receives fresh confirmation
on distinct held-out inputs. The seeded 95% paired bootstrap and per-case
regression gates decide validated improvement. When no challenger qualifies, the result is
`no_improvement` with the supported reference retained; missing/failed/unchecked
or insufficient evidence produces `inconclusive`. Training-only no-improvement
can export a historically unvalidated reference. [Search](search.md) explains
thresholds, selection bias, uncertainty, reservations and break-even exactly.

The time budget includes loading, hashing, discovery, warmups, trials, checks,
persistence and scoring. Confirmation count/time reserves must fit before trials
start. Cleanup has a separately bounded default one-second allowance, recorded
in total cost. Teardown/filesystem latency can overrun; it cannot authorize further
work. Trial and subprocess caps never reset. `timing` uses monotonic elapsed time
and sequential child rusage without external profilers. `perf-stat` requires
budgeted harmless Linux probes; failure stops the request, no silent timing retry.
Missing/multiplexed counters retain reasons/scaling. See [measurements](measurements.md).

Project config supports only `[tune]` keys: `workload`, `budget`, `out`, `save`,
`repeat`, `warmups`, `seed`, `confirmation_pairs`, `confirmation_fraction`,
`minimum_improvement`, `max_trials`, `max_launches`, `timeout`, `from_run`, `label`.
CLI overrides config; config overrides defaults. Relative config paths use the
config file's parent; no ancestor/home discovery. `collectors` is **CLI-only**;
a config key with that name fails. `--show-config` includes its value and
CLI/default source; it is incompatible with argv, resume and dry-run. Config is
ignored on resume; explicit `--config` is rejected. Defaults in show-config are
CLI defaults, not a resolution of a manifest's resource or timeout declarations.

Resume accepts the saved protocol, optional additional `--budget`, reporting
`--json` and an invocation-local `--save` request. It refuses workload/out/collector
and protocol overrides. Completed experiments are immutable; interrupted or
uncertain side effects are never retried automatically. A safe budget stop may
leave an incomplete resumable record. Locks, identities, schedule, journal and
trial statuses must agree. Read diagnostics before deciding to fund a new search.

Outputs include `experiment.json`, `workload.json`, `trials.jsonl`, per-trial
workspaces/plan/launch/check/raw evidence and a final `completion.json` marker for
terminal searches. Exact durable layout is in [experiments](experiments.md#durable-layout-and-recovery).
Startup exhaustion may return structured evidence before creating a store.
JSON `recipe_path` is null unless this invocation exported successfully;
`recipe_export` reports saved/skipped/not_requested, requested_path and skip reason.
An existing requested file is not proof of this invocation's success. An export
failure exits 2; inspect retained experiment evidence before acting again.

## `import-profile`: retain external perf diagnostics

```bash
simtune import-profile --format perf-stat --input stats.perf --context context.json --out imported-profile
```

All four options are required. `--format` currently accepts only `perf-stat`.
`--input` is UTF-8 semicolon-delimited `perf stat -x ';'` output; `--context` is a
versioned JSON declaration of command, input identity, host, perf version, scope,
fixed_env and nullable workload/environment fingerprints. `--out` must be new.
See [the exact format/context](measurements.md#supported-external-format-perf-stat-v1).
Creates original-byte `raw.perf`, `context.json`, and hash-linked `profile.json`.
Prints imported row count, normally exits 0; invalid data/path fails with exit 2.
Never launches the external program. Provenance remains declared, not verified.
Missing, duplicate, scaled and unavailable counters stay explicit. Only compatible
unscaled hints can reorder hypotheses with `tune --from-run`; no imported sample
enters scoring or fresh confirmation. No general profiler format is supported.

## `explain`: historical evidence and current applicability

```bash
simtune explain heat.recipe.json --workload heat.toml --json
```

`recipe` positional path is required. `--json` emits the structured report;
`--workload FILE` optionally checks declared identities and current resources
without a workload launch. Without it applicability remains not checked. Prints
policy/rationale, scientific state, protocol validity, performance outcome,
constraints, capabilities, hardware class and limits. Older readable bundles
remain historical; invalid evidence can be diagnosed yet rejected for execution.
Missing/corrupt data or failed applicability/execution gates return 2. Read-only,
no recipe rewrite. A historical validated label never checks current output or
proves transfer; matching hardware class alone promises no performance.

## `exec`: strict, exactly-once recipe replay

```bash
simtune exec --recipe heat.recipe.json --cwd replay/heat --out results/replay --dry-run -- /absolute/path/heat2d FIXED_ARGS
```

This is a syntax illustration; use the [generated heat argv](../examples/HEAT_EXPERIMENT.md#replay-the-generated-argv)
for runnable replay, after applicable checked evidence exists.

| Option | Default / meaning |
| --- | --- |
| `--recipe FILE` | Required bundle; its hash-linked evidence must be accessible |
| `--workload FILE` | None; optionally declare permitted case values/input identity/runtime |
| `--case ID` | None; disambiguate a declared case |
| `--cwd DIR` | Current directory; must exist; argv trial/output directory must match |
| `--out DIR` | New `.simtune/replays/ID` log directory |
| `--timeout DURATION` | Saved workload bound; supplied duration can only lower it |
| `--allow-unvalidated` | Off; explicit historical/hardware acknowledgment, after inspection; never bypasses identity/resource/required-setting checks |
| `--dry-run` | Off; print replay record with zero launches and no log directory; still check gates |
| `COMMAND ARG...` | Required exact permitted argv after `--` |

Checks evidence, code/input/runtime/environment identities, logical resources and
hardware authorization, then refreshes strict requirements immediately before
one launch. Override-free reuse needs historical validation plus known matching
hardware class. Older recipes without processor context need fresh validation
or explicit unvalidated acknowledgment. Identity/incompatibility failures launch
zero times. It adds no baseline, scientific validator, profiler or retry; stdin/
stdout/stderr are inherited. Current output is **unchecked**, current performance
unproven, regardless of historical labels. Run a pinned validator separately when
needed or fund `validate`. Logs contain `replay.json` with requested/resolved/
action/outcome provenance. The workload exit or 128+signal status is propagated;
timeout is 124; configuration/preflight errors are 2. Choose a fresh cwd/output
directory to avoid overwriting workload files. See [recipes](recipes.md).

## `validate`: fresh checked policy evidence

```bash
simtune validate --recipe heat.recipe.json --workload heat.toml --out results/validation --budget 10m --dry-run
```

`--recipe`, `--workload`, `--out` are required. `--budget DURATION` defaults to
600 seconds (10m); `--dry-run` previews without subprocesses/artifacts. The new
manifest must declare the same family, checks and independent held-out cases.
Compares only the saved logical policy and reference, with fresh training and at
least five confirmation pairs per held-out case; old timings never enter the
decision. Creates a new experiment and, when complete/eligible, sibling
`DIR.recipe.json` plus its evidence bundle. Never modifies the source recipe.
No overwrite of derived recipe/evidence destinations. Can confirm no improvement;
a gain is not required for useful checked evidence. Prints outcome, evidence path
and actual derived path/unavailable, or structured startup/dry-run results.
Use show/explain before reuse. Statuses follow the outcome table below.

## Failures, artifacts and practical limits

| Status | Meaning / next step |
| --- | --- |
| 0 | Successful command; tune/validate validated improvement or no improvement. Read science/confirmation labels before reuse. |
| 1 | Tune/validate inconclusive/failed decision; retain exclusions/checks/budget and diagnose. Missing evidence is not no improvement. |
| 2 | Invalid configuration, corrupt record, missing dependency or incompatible strict replay; correct the cause before a new request. |
| 124 | Timeout; inspect partial output/termination evidence. No automatic retry. |
| 130 / 143 / 129 | Parent INT / TERM / HUP; retain interrupted/unknown records and inspect side effects. |
| Other workload status | Manual/replay workload or wrapper failure; raw codes and signals remain recorded. |

Legacy profile/run keep seven files: `meta.json`, `capabilities.json`,
`applied_actions.json`, `metrics.json`, `raw_profiler.txt`, `command.txt`,
`rationale.json`. Printed paths identify each run. They remain readable as
schema-1/schema-2 without rewriting historical records. Metrics include elapsed
seconds, CPU times, RSS KiB when available, resource deltas and success/failure
counts. Missing is null with a reason, not zero. [Storage](storage.md) and
[benchmarks](benchmarks.md) describe scope and historical invalid-speedup evidence.

Captured manual/trial/validator/hook output buffers fully in memory with **no
size cap**. Quiet bounded commands are suitable for this candidate; verbose long
jobs need the deferred streaming work. Replay inherits streams. Process-group
cleanup is bounded, but descendants that escape into other groups/sessions are
outside ownership; uncertain teardown and incomplete output are recorded.
Isolated workspaces are not a sandbox. Hook/fixed-cwd APIs exist but have no CLI
flags; they require explicit trusted API use and do not sanitize side effects.

There is no Slurm adapter/automatic submission, ensemble executor, thread-count
search, MPI/hybrid or multi-node optimization, GPU locality, energy objective,
compiler tuning or scientific parameter optimization. Existing Slurm templates
are manual examples with separate unobserved target gates. No current study
establishes a speedup, transfer guarantee, universal optimum or public
reproducibility badge. Local engineering passes and review acceptance are separate.

## The checked Linux heat path

Use [HEAT_EXPERIMENT](../examples/HEAT_EXPERIMENT.md) in order: build once with an
existing audited OpenMP compiler, inspect actual runtime linkage, generate a fresh
manifest (distinct 64/80 smoke sizes), dry-run, tune, show, explain, fund validate
as appropriate, and derive replay argv from the actual manifest. Pin the validator
and expected thread/scientific values. Tiny cases check correctness only. Keep
build/input/precision fixed across arms; do not renew hashes to resume changed work.

On this macOS host the solver/validator can be checked and strict dry-run inspected;
actual tune is unsupported/inconclusive with zero strict workloads and no recipe.
For target evidence use the [native runbook](implementation/release-plan/r06/RUNBOOK.md)
and its complete helpers in an authorized Linux allocation, then separate NUMA/
perf/Slurm gates. These instructions do not authorize cluster submission or study
retries. The [HPC testing protocol](hpc-testing.md) covers controls, sizing, complete
failure retention and publication criteria; no mocked pass substitutes for it.
