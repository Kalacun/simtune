# Cross-stage acceptance and review gates

These checks constrain the implementation; passing a model's self-review alone
is insufficient. Each stage handoff maps its work to the relevant IDs below.
Automated tests use fixtures/fake clocks/harmless processes, not timing speedups.

| ID | Required invariant | Evidence |
| --- | --- | --- |
| C01 | Existing command/default/exit/fallback behavior and schema-2/legacy reads survive | Current tests plus golden CLI/storage fixtures |
| C02 | CPU sets never widen inherited/known permitted limits by default | Sparse IDs, empty sets, SMT, offline CPUs, conflicting sources |
| C03 | Memory-node and cgroup limits are independent of host RAM/CPU count | v1/v2/ancestor/unlimited/missing/malformed fixture trees |
| C04 | Strict requests never silently skip/fallback or become another policy | No-workload-launch spy for every invalid required setting |
| C05 | argv/env/path handling preserves literal values and confines artifacts | Spaces, Unicode, dollar signs, newlines, traversal and symlink tests |
| C06 | Failures/timeouts/interrupts launch at most once and retain diagnostics | Sentinel process, child cleanup, partial-output and start failure tests |
| C07 | Concurrent workers cannot contaminate rusage or preexec state | Separate process workers; no threaded profiling calls |
| C08 | Science is fixed per case; unchecked/incorrect output cannot win | Hash mismatch, numeric tolerance, NaN, validator failure/timeout |
| C09 | Trials have isolated workspaces and bounded explicit hooks | Output collision, cleanup escape, setup failure and disk error tests |
| C10 | Versioned records survive interruption without fabricated success | Partial journal line, interrupted rename, corrupt/incomplete/unknown version |
| C11 | Search respects both time and launch budgets, including confirmation | Fake clock around discovery/setup/warmup/validator/cleanup; tiny budget |
| C12 | Baseline/candidate pairing, warmups, seeds and case weights are explicit | Deterministic schedules; no silently dropped samples or collector mismatch |
| C13 | Winner selection and held-out confirmation use disjoint evidence | False winner, negative/noisy results, tiny samples, case regression |
| C14 | Recipes verify identity/resources and required settings before replay | Changed binary/input class/runtime/topology; missing tools; new CPU IDs |
| C15 | Replay adds no baseline and preserves workload exit/signal semantics | Launch counter and no-profiler replay smoke test |
| C16 | Resume cannot duplicate uncertain side effects or extend time silently | Crash before/after launch, stale lock, concurrent resume, changed fingerprint |
| C17 | Imported/missing/multiplexed counters cannot become invented conclusions | perf fixtures including not-supported/not-counted/units/scaling variants |
| C18 | Slurm counts/IDs/step scope cannot bypass kernel boundaries | Submission host, fake env, nested binding, multi-task/multi-node rejection |
| C19 | Ensemble layouts fit disjoint CPU and combined memory budgets | Heterogeneous tasks, undersized allocation, failed worker, cancellation |
| C20 | Public claims match runnable commands and available evidence | README/help audit, source archive check, target-host test record |

## Automated gate

From an activated development environment:

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m build
```

Use focused tests while changing code, then the full suite for the stage gate.
Run package builds when schemas/config/resources/entry points change, and at
stages 01, 07, 11 and 12. Install the wheel into a fresh environment outside the
checkout and exercise implemented commands. Test base dependencies and extras;
the C/OpenMP example is optional for Python package installation.

Do not introduce a static type checker just to produce a badge. If needed for
new typed modules, scope it explicitly and make CI enforce what docs claim.

## Manual gates and what they establish

| Environment | Gate | What it cannot establish |
| --- | --- | --- |
| macOS/local | Legacy CLI, small serial/OpenMP solver, current harness | Linux NUMA, cpuset enforcement or cluster compatibility |
| Linux without privileges | Affinity restriction, missing tools, strict errors, timeouts | Multi-socket NUMA behavior if only one node is visible |
| Linux with real NUMA | Allowed-node discovery, requested policy inspection, paired workload trials | Universal speedups or arbitrary HPC compatibility |
| Slurm allocation | One node/one task, compute-step discovery, resource inheritance, captured binding | MPI, distributed profiling or other scheduler support |
| Repeated controlled allocations | Predeclared candidate validation and curated evidence | A universal optimum across workload families |

Use [the HPC testing guide](../hpc-testing.md). Never simulate target-host results
or mark native tests passed because mocks pass. A missing machine leaves a
specific manual gate pending; it need not block unrelated deterministic work.

## Final review order

1. Reconstruct actual feature status from source/help/tests and handoffs.
2. Inspect resource boundaries and every launch/error path before presentation.
3. Audit experiment identity, selection leakage, budgets, failures and replay.
4. Re-run deterministic checks and representative safe CLI workflows.
5. Review published evidence independently of implementation test success.
6. Record severity, file/line, reproducer, consequence and required correction.
7. After fixes, rerun affected checks and integration checks; do not erase prior
   findings. Mark unresolved target-platform claims explicitly.
