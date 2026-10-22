# Strict launch boundary

`simtune.launch` supplies the strict planning/execution API. It is now used by
implemented [workload manifests and isolated trials](experiments.md),
[measured search](search.md), and [recipe replay and fresh validation](recipes.md).
The existing `simtune run` command keeps its manual best-effort fallback behavior.
These implementation facts do not establish native acceptance: R06 Linux
evidence, physical NUMA and native counters remain pending/not run. R07's review
exists with a distinct-author provenance limit; repair follow-up and release
acceptance remain pending in the [status ledger](implementation/release-plan/STATUS.md).

## Strict and manual behavior

The two paths intentionally answer different questions:

- `simtune run` is the compatibility path. It selects a supported fallback or
  skips an unavailable action, executes the user's unchanged argv, and records
  the actual selected mode plus skipped/adjusted actions. A missing `numactl`
  therefore does not prevent the user from running a program.
- `plan_strict_launch()` is for a named experimental policy. It either resolves
  every top-level CPU/NUMA intent and every required setting or raises a typed
  `StrictPlanningError` before workload launch. It never calls a NUMA-free run a
  NUMA-bound run. A caller may offer the user a separately named inherited or
  manual run, but may not relabel that run as the failed strict candidate.

For example, today's manual CLI remains available:

```bash
simtune run --mode throughput --out runs -- ./solver input.dat
```

If `numactl` is missing, inspect the printed fallback/skipped diagnostics and
`applied_actions.json`. The workload still executes through the resolved manual
profile. By contrast, the implemented experiment code constructs a `PolicySpec`, calls
`plan_strict_launch(policy, snapshot, capabilities)`, and treats
`StrictPlanningError.to_dict()` as a candidate exclusion. It does not launch
that strict candidate after the error.

## What strict preflight verifies

Planning is side-effect free. It validates and records all of the following:

- Linux resource discovery has no recorded conflict relevant to the plan;
- the effective CPU set is known and nonempty, and is the stage-02 intersection
  of known online, permitted, and inherited boundaries;
- the requested CPU count fits that set, topology needed by compact/spread/
  one-thread-per-core is complete, and a non-inherited placement has `taskset`;
- inherited placement does not conceal a smaller declared CPU budget;
- local/bind/interleave NUMA policies have `numactl`, a known nonempty allowed
  memory-node set, and nodes associated with the resolved CPUs;
- a declared strict memory budget fits known selected-node capacity and the
  tightest known finite cgroup/allocation memory cap;
- raw NUMA/CPU override settings cannot bypass the resolved boundaries;
- declared runtime/environment settings belong to the initial OpenMP allowlist
  and have supported value shapes; the declared OpenMP thread budget agrees
  with `OMP_NUM_THREADS`;
- process-limit names and values are allowlisted and cannot increase an
  inherited finite hard limit;
- wrappers and the workload remain argv arrays with explicit option boundaries.

Immediately before execution, the planner takes a fresh `ResourceSnapshot` and
resolves the policy again. A changed CPU/NUMA/env/limit resolution fails before
`Popen`. The child environment is copied, and process limits run only in the
child pre-exec hook. Minimal execution adds no `time` or `perf` wrapper; optional
profiling uses the existing profiler. Both paths share bounded process-group
timeout cleanup and attempt the composed command only once. Start errors,
permission failures, wrapper failures, workload failures, signals, timeouts and
interrupts are never retried automatically.

## What remains unobserved

Strict means “no silent policy loss,” not “the requested policy improved the
program” and not complete runtime telemetry. The launch boundary alone does **not** establish:

- that an application runtime keeps the initial affinity or does not rebind
  later-created threads;
- that every thread ran on the resolved CPUs;
- actual NUMA page residency, first-touch behavior, or later memory migration;
- that OpenMP variables are recognized or obeyed by every runtime version;
- scientific correctness, fixed inputs, output equivalence, or a speedup;
- Linux/NUMA/Slurm behavior from the synthetic unit tests.

Wrapper invocation and a zero workload exit are retained as execution facts,
not promoted to those stronger claims. The experiment layer implements isolated
trial workspaces, durable evidence and explicitly declared scientific checks.
Native Linux and physical-NUMA checks
remain separate manual gates.
