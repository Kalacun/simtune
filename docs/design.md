# Architecture and design tradeoffs

simtune separates policy from platform mechanics. A profile can express a
resource policy without embedding OS discovery or subprocess management.
The planner records how that policy was resolved before execution. Legacy manual
profiles and the experimental strict `tune/validate/exec` path coexist. Start with
the [checked heat workflow](../examples/HEAT_EXPERIMENT.md); native/review gates
remain separate in the [release ledger](implementation/release-plan/STATUS.md).

## Data flow

```text
Legacy CLI arguments ──> capability snapshot ──> profile selection / fallback
                                             │
  auto only: saved baseline ──> heuristics ────┘
                                             │
                                             v
                 action plan (wrappers, environment, process settings)
                                             │
                                             v
                           profiler: one launch per iteration
                                             │
                                             v
                  normalized samples ──> aggregates ──> run storage
                                                           │
                                                           v
                                                 comparison exports
```

| Module | Responsibility |
| --- | --- |
| `cli.py` | Argument validation, saved auto baseline, exit status and diagnostics |
| `capabilities.py` | Tool discovery, bounded harmless probes, platform support and CPU affinity snapshot |
| `profiles.py` | Independent built-ins, explicit YAML loading, action validation |
| `runner.py` | Fallback traversal, action planning, environment/preexec settings, repeat orchestration |
| `launch.py` | Fail-closed typed policy resolution, fresh preflight, and strict exactly-once launch |
| `profiler.py` | Minimal/profiled subprocess launch, time/perf isolation, timeout cleanup, resource deltas |
| `workloads.py` | Strict manifest path resolution, streamed identity checks, case argv expansion and fingerprints |
| `checks.py` | Confined exact-hash, finite numeric JSON and bounded command validators |
| `experiments.py` | Isolated strict trials, budgets, hooks, atomic records, journal recovery and completion markers |
| `candidates.py` / `search.py` / `statistics.py` | Fixed-count hypotheses, budgets, balanced paired trials, held-out bootstrap decision |
| `recipes.py` / `replay.py` | Immutable evidence export, applicability, exactly-once replay and fresh derived validation |
| `tune.py` / `lookup.py` | Config precedence, IDs, JSON/progress and legacy/experiment inspection |
| `collectors.py` / `importers.py` | Optional provenanced counters and declared external hints |
| `hardware.py` / `reporting.py` | Processor-class context and shared decision projection |
| `metrics.py` | Normalization, finite successful-sample aggregation, failure counts |
| `storage.py` | Versioned metadata, structural validation and collision-safe directories |
| `compare.py` | Legacy/new run loading, status-aware comparison and JSON/CSV exports |
| `util.py` | Bounded probes, serialization, terminal rendering and optional notifications |

## Execution boundary

Wrappers are composed as argv lists. The planner's order is retained, with a
systemd scope outermost among tuning wrappers. `time` is outside `perf`, which
is outside the tuning command. No shell is inserted. User scope execution
explicitly disables systemd's argument environment expansion so literal dollar
signs reach the workload unchanged. Older systemd-run versions without that
option fail the capability probe and fall back to other actions.

Nice levels and resource limits are applied in the child before execution, so
they affect profiler wrappers as well as the workload. A systemd **scope** runs
with the caller's process environment; service-only `Nice=` properties are not
used for scopes. Runtime process-setting errors fail launch rather than being
silently swallowed. See the [systemd-run manual](https://man7.org/linux/man-pages/man1/systemd-run.1.html)
for scope/service behavior and environment expansion.

The `preexec_fn` mechanism is intentionally small but unsuitable for threaded
embedding. Python warns against using it in threaded applications. The CLI is
sequential; a separate process per simtune invocation is the supported model.
See [Python's subprocess documentation](https://docs.python.org/3/library/subprocess.html).

## Policy and fallbacks

Required action **types** drive profile fallback. Optional unavailable actions
are skipped. A fallback chain is traversed once per profile; cycles terminate
with the supported subset of the originally requested profile. Concrete action
constraints, such as a CPU list with no available members, are checked during
planning and logged as skipped. This preserves best-effort behavior; a resolved
profile name does not imply every requested setting was applied.

Profiles are validated before use. Built-ins are defined in Python and copied
on each load, eliminating dependence on the working directory, optional YAML,
or an unpackaged sibling directory. `profiles/default.yaml` is a tested reference
copy, loaded only when explicitly selected. Custom files replace the built-in
collection; later files override matching names.

The strict experimental boundary is separate from this legacy behavior. It
does not traverse profile fallbacks or silently skip required settings. A failed
strict candidate launches nothing under that candidate name; the user can still
run the command through the manual/inherited path, which records its actual
mode. See [strict launch checks and limitations](strict-launch.md).

The strict experiment layer builds on this boundary without changing the
manual tools. It verifies a declared workload, assigns each trial an isolated
workspace, records state before subprocesses, and keeps process success separate
from [scientific validation](experiments.md). Candidate generation and selection are implemented in the separate search layer.
Per-case work remains fixed, equivalent plans are deduplicated, every check/failure
is retained, and no-improvement and inconclusive are separate outcomes.

## Auto-mode rules

Auto mode uses one successful saved baseline. The report example may instead
supply means from its successful baseline batch. Its rules, in priority order:

1. `--latency-important` selects `latency`.
2. `--respect-desktop` selects `desktop_friendly`.
3. Block operations / wall second ≥ 100 **or** estimated waiting fraction ≥ 0.5
   selects `isolated` if a systemd scope is usable, else `desktop_friendly`.
4. (User + system CPU seconds) / wall seconds ≥ 0.7 selects `throughput`.
5. Otherwise select `desktop_friendly`.

Estimated waiting fraction is `max(0, wall - user - system) / wall`. These are
fixed, uncalibrated thresholds. Sleep, scheduler contention and synchronization
can resemble I/O waiting; block operations are not byte counts. CPU/wall ratios
can exceed one for parallel jobs. The heuristic neither measures tail latency
nor explores alternative profiles. Very short commands can be misclassified
because profiler CPU times have limited precision. The recorded inputs and fired rule explain
the choice; they do not establish the correct policy for every workload.

## Measurement decisions

- No workload retry after wrapper failure: detecting a permission-looking
  stderr message cannot establish whether the workload already ran. Retrying
  can duplicate effects and bias measurements.
- Profiler output is written to separate temporary files and appended to the
  stored raw output. Workload stderr cannot masquerade as time/perf statistics.
  The workload's locale is preserved; GNU time uses explicit field labels.
- `RUSAGE_CHILDREN` is cumulative for reaped children. Additive fields use a
  before/after delta. RSS is a high-water mark, so it cannot be isolated by
  subtraction. Without a per-run time-tool value, RSS stays unknown. The
  [resource API](https://docs.python.org/3/library/resource.html) also exposes
  platform-specific resource constants rather than guaranteeing universal limits.
- Successful iterations alone contribute to means. Failures remain in raw
  samples and counts. A failed run is never relabeled successful because a
  subset completed.
- The stored `applied` action list is a plan, not kernel-state telemetry. Runtime
  status is tracked separately. Tool availability and a harmless probe do not
  prove NUMA placement, scheduler behavior, or cgroup enforcement.

## Deliberate limits

There is no persistent daemon, root escalation, global kernel tuning,
hardware calibration, Slurm adapter, ensemble executor or benchmark database.
Paired bootstrap decision intervals exist; they do not promise a speed gain.
Storage is plain files with a documented schema. JSON-friendly dictionaries
keep the small pipeline inspectable but provide less static assurance than a
fully modeled type hierarchy; no comprehensive static type-check claim is made.

The timeout boundary is a process group, not a sandbox. Escaped sessions and
processes launched through unrelated managers may survive. Very large output
is held in memory. Separate processes/threads reaped concurrently can contaminate
resource deltas. These constraints favor small, trusted, sequential experiments.
