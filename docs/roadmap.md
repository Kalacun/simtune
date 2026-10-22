# Scientific execution tuning: implemented core and future direction

**Current state:** strict topology/resource discovery, isolated checked trials,
fixed-count measured search, saved recipes/replay/revalidation, config/show/resume,
and optional perf collection/import are implemented. They remain experimental
pending native Linux acceptance and independent release review. The
[checked heat workflow](../examples/HEAT_EXPERIMENT.md) is runnable with a user-built
binary and declared runtime; the manual paired harness remains independent.
See the [capability/status matrix](capability_matrix.md) and
[release ledger](implementation/release-plan/STATUS.md).

The intent/priorities below describe the original roadmap, not a claim that every
row exists. The [release implementation packet](implementation/release-plan/README.md)
now orders work: repair the core, checked workflow/docs, native R06 evidence and
R07 review; NUMA/Slurm/workflow gates follow separately. Thread-count search,
Slurm adapter and ensemble executor remain planned. MPI/multi-node/GPU/energy
remain excluded, with no implicit release commitment.

## Product intent

Help researchers find, validate and reuse an efficient **way to launch an
unchanged scientific program** across repeated experiments. The main targets
are simulations, parameter sweeps, ensemble runs and repeated numerical
analysis on Linux workstations and allocated HPC compute nodes.

The product should optimize a workload's execution policy: CPU placement, NUMA
memory policy, and explicitly permitted runtime thread settings. It should not
rewrite source, change algorithms, recompile binaries, change scientific input
parameters, or tune global kernel settings. Keep precision, seeds and solver
settings fixed during each comparison. Changing parallelism is opt-in because
unchanged source does not imply identical numerical results.

The central deliverable should be a **validated, reusable launch recipe with its
evidence and applicability conditions**. A recipe says which configuration was
selected, how it was tested, and when it must be checked again.

## The legacy heuristic boundary

The existing architecture is a useful foundation: capability discovery, profile
validation, argv planning, repeated execution and readable run records already
exist. However, `auto` classifies a baseline with fixed thresholds; it neither
compares candidate configurations nor saves a learned policy for reuse.

The built-in profiles mix desktop coexistence with resource constraints.
`isolated` deliberately throttles CPU use; `throughput` may preserve the current
CPU mask and request NUMA interleaving. These policies are not evidence-based
scientific tuning. CPU time and block-operation counts alone cannot identify
the right memory placement or prove a memory-bandwidth bottleneck.

Current best-effort fallback can skip even a required action when no usable
fallback exists. That is appropriate for exploration only if disclosed; a
production recipe must not silently lose the setting that justified its result.

## Recommended first scope

Start with **one command on one Linux compute node**, either standalone or inside
an existing allocation. Support unchanged executables and scripts, regardless
of whether the application is Python, C, C++, Fortran or another language.
Begin with placement and memory-policy candidates at a fixed resource count;
add thread-count search only when the user allows it.

Keep MPI rank counts, multi-node placement, GPU policies and ensemble concurrency
outside the first optimizer. Preserve existing explicit/manual commands and
keep `auto` behavior until a documented migration is justified. Add a separate
`tune` command rather than changing `auto` into an expensive search invisibly.

## Priorities and useful completion criteria

| Priority | Addition | User benefit | Completion criterion |
| --- | --- | --- | --- |
| P0 | Topology and allocation model | Avoid wrong-core, wrong-node and oversubscription assumptions | Candidates use only permitted CPUs/memory nodes and preserve narrower task bindings by default |
| P0 | Budgeted candidate experiments | Replace guesswork with measured comparisons | Baseline and candidate trials, explicit budget, retained failures, held-out validation and an honest no-improvement result |
| P0 | Saved recipes with strict replay | Pay tuning cost once per relevant workload/environment class | Replay validates compatibility, resolves logical placement and fails on missing required settings |
| P1 | Easier project workflow | Reduce long command lines and manual directory navigation | Optional project config, run IDs, labels, progress, `show`/`explain`, resumable search and machine-readable results |
| P1 | Broader measurements and validation | Explain observations and protect scientific validity | Optional collectors with units/provenance; user-supplied output checks; no invented missing measurements |
| P1 | Slurm integration | Fit existing cluster workflows | Allocation-aware plans and reviewed job-step templates; per-task measurements distinguished from whole-job timing |
| P2 | Ensemble and workflow integrations | Improve experiments completed per allocation | Joint worker/thread budgeting, isolated trial outputs, integration with existing workflow tools |
| Later | MPI, multiple nodes, GPU locality and energy objectives | Extend to more complex scientific workloads | Explicit placement ownership, appropriate collectors, hardware access and end-to-end validation |

P0 is one coherent release: topology, measured search and reusable recipes are
all necessary to deliver the intended product. More collectors alone are not.

## 1. Discover the resources the workload may actually use

Represent sockets, physical cores, SMT siblings, NUMA nodes and distances,
allowed memory nodes, effective CPU affinity and the scheduler allocation when
known. Distinguish the allocation envelope from a narrower per-task binding.
Use existing OS interfaces first; an optional hwloc adapter can enrich topology.
[hwloc](https://www.open-mpi.org/projects/hwloc/) exposes hardware topology and
binding facilities, making it a relevant optional provider rather than a new
mandatory dependency.

Record whether each setting is supported, permitted, requested and verified.
A binary on PATH is only the first of those checks. Missing topology should
reduce the candidate set, not lead to guessed CPU or memory-node numbers.

Treat inherited binding as the default contract. Never expand a task's mask
merely because more CPUs appear on the machine. Intentional rebinding within
an allocation needs an explicit policy and must cooperate with the launcher.

## 2. Search a small, meaningful configuration space

Use profiling to propose candidates, then benchmark them. Start with a transparent
deterministic search; a complex statistical optimizer is unnecessary for the
first small candidate set.

| Dimension | Candidate ideas | Constraint |
| --- | --- | --- |
| CPU placement | Inherited binding; compact versus spread across eligible cores | Same CPU budget initially; respect SMT siblings and task masks |
| NUMA memory | Inherited/default policy; local allocation; selected-node binding; interleaving across permitted nodes | Check locality and memory capacity; policies are alternatives, not a universal ranking |
| Runtime threads | User-approved thread counts and binding policies for a declared runtime | Do not guess which runtime is active or change parallelism by default |
| Ensemble layout, later | Fewer larger workers versus more smaller workers | All workers must fit the same allocation and combined memory budget |

NUMA CPU placement and memory placement need to be considered together. Strict
memory binding can fail when selected nodes lack memory; interleaving changes
where pages are allocated and may be worse than locality. Linux numactl provides
these policies and relative resource notation. The workload's own allocation
and binding calls can also affect the outcome. See the
[numactl manual](https://man7.org/linux/man-pages/man8/numactl.8.html).

Setting a launch policy cannot restructure an application's first-touch
initialization or memory access pattern. Never infer measured remote-memory
traffic or bandwidth from RSS or page-fault counts alone.

For applications that use OpenMP, thread count and placement can be controlled
through runtime environment variables. Support declared runtime adapters,
starting with `OMP_NUM_THREADS`, `OMP_PLACES` and `OMP_PROC_BIND`, rather than
setting unrelated library variables indiscriminately. See the
[LLVM OpenMP runtime reference](https://openmp.llvm.org/design/Runtimes.html).

## 3. Make the experiment trustworthy and affordable

### Profiling inputs

Accept an existing simtune run first, or collect a fresh baseline when none is
provided. Later, add explicit import adapters for external profiler exports
such as `perf stat`, retaining the original data and recording metric names,
units, collection scope and environment. Missing fields should remain unknown.
Reject incompatible records or use them only to suggest new measurements.

Elapsed/CPU time and RSS are useful starting points. Optional hardware counters,
NUMA placement observations and memory-bandwidth collectors should answer
specific questions when the host exposes them. Their absence should still allow
direct timing comparisons. Profiling supplies candidate hypotheses; the search
must establish whether a different launch policy actually helps.

### Experiment protocol

- Define a primary objective: elapsed time for one run initially. Later add
  experiments/hour or measured resource cost. Report allocated CPU-hours and
  consumed CPU seconds separately; neither necessarily equals the bill.
- Accept representative cases with declared weights. Hold each case's data,
  seeds and scientific parameters fixed when comparing policies. Validate on
  cases not used to select the winner; very small pilots may not represent
  production memory behavior.
- Bound both trials and elapsed tuning time. Record warmup policy, trial order
  and search seed. Interleave baseline/candidate trials to expose drift; retain
  all samples and report uncertainty. Do not silently discard slow samples.
- Require successful execution **and** a user-defined correctness check where
  available: exact hashes for deterministic outputs, or domain-specific
  tolerances and invariants for numerical results. A zero exit code is not a
  correctness proof. Mark unchecked results explicitly.
- Give each trial an isolated output directory and declare any setup/cleanup.
  Repeated commands can overwrite files or produce external side effects;
  setup hooks must be explicit, bounded and recorded.
- Confirm promising policies in a separate validation stage. Specify minimum
  practical improvement and uncertainty criteria before selection. If the
  evidence is weak or a candidate fails validation, keep the inherited policy.
- Reuse earlier profiling only when workload, input class and environment
  metadata are compatible. Imported measurements can propose candidates, but
  cannot substitute for a current paired comparison.
- Export a result even when no improvement is found. The report must include
  tuning overhead, failures, validation status and the baseline policy.

A useful decision is whether tuning pays back over the expected remaining runs.
For a time-only objective, let `C` be the additional tuning/validation time and
`delta = baseline_time - selected_time` be a positive, validated per-run saving.
The break-even run count is `ceil(C / delta)`; there is no finite break-even if
`delta <= 0`. This is a decision model, not a performance claim, and should use
conservative estimates when measurements are noisy.

## 4. Reuse policies across experiments without assuming universality

Keep three separate records:

- **Workload definition:** executable/source identity, declared runtime, fixed
  command structure, allowed input variations, representative cases and checks.
- **Experiment evidence:** original run directories, trial metadata, failures,
  objective, uncertainty and validation results.
- **Launch recipe:** selected logical policy, required capabilities, resource
  budget, matching environment/workload conditions and links to its evidence.

Prefer logical intent such as “one thread per eligible core within one NUMA
domain” over saving a machine-specific mask as a universally reusable recipe.
Resolve it inside the new allocation. Record concrete masks in each resulting
run. If a topology, input-size class, runtime version or allocation changes,
require validation or retuning; do not silently reuse a stale winner.

Strict replay errors before launch when required settings cannot be honored.
`--allow-unvalidated` acknowledges absent historical/class validation, bypassing
no mechanical or evidence-integrity gate. Legacy `run` remains best-effort as
described in [profiles](profiles.md).

The normal replay path should apply the saved policy with minimal instrumentation.
Full perf collection belongs in tuning/diagnosis or opt-in checks. Scheduled
revalidation should be configurable, not an extra baseline on every experiment.
A recipe records the selected tested configuration; it is not a global optimum.

## 5. Fit HPC scheduling and existing workflows

Slurm decides resource allocation and may also impose CPU binding. CPU IDs in
scheduler metadata need not match Linux CPU numbering. A Slurm adapter must
reconcile those layers rather than infer eligibility from a core count alone.
See Slurm's [CPU management guide](https://slurm.schedmd.com/cpu_management.html)
and [binding precedence](https://slurm.schedmd.com/resource_binding.html).

Run discovery and measurement **inside the actual compute-node job step**.
Profiling an `srun` or `mpirun` launcher on a submission host does not collect
remote ranks' resource usage. Start with one task/one node and explicit labels
for per-task timing; a later adapter must collect and aggregate job/rank data.

Slurm or the MPI runtime should own distributed rank placement. simtune should
emit compatible settings and validate the result, not stack competing taskset,
numactl and launcher masks. Do not depend on a systemd user manager, request
root access, or modify host-wide sysctls as part of the HPC path. Generate
reviewable job-step snippets first; job submission and scheduler-specific
accounting can be separate, explicit integrations.

Preserve shell-loop and batch-script composability. An eventual JSON/Python
interface can make recipes usable from
[Snakemake](https://github.com/snakemake/snakemake) and other workflow systems.
Integrate at their command-execution boundary instead of building another DAG
scheduler, software-environment manager or cluster queue.

## Implemented user experience

```text
simtune tune --workload experiment.toml --budget 10m --out results/search --save recipe.json
simtune tune --from-run RUN_DIR --workload experiment.toml --budget 10m --out results/new-search
simtune explain recipe.json
simtune exec --recipe recipe.json -- PROGRAM DECLARED_CASE_ARGUMENTS
simtune validate --recipe recipe.json --workload experiment.toml --budget 10m --out results/validation
simtune show results/search --json
simtune import-profile --format perf-stat --input FILE --context CONTEXT_JSON --out imported
```

These are current syntax with placeholder files/programs; use the complete heat
walkthrough for runnable commands and exact replay cwd/argv. Bare command tuning
is unchecked/input-specific; past/imported profiles only propose hypotheses.
Configuration, run IDs, progress, remaining budgets, JSON and conservative resume
are implemented. Read [search](search.md) and [recipes](recipes.md) for protocol,
scientific validation, compatibility and current limitations.

## Broader use cases, in order

| Workload | Potential value | Evidence or integration needed |
| --- | --- | --- |
| Repeated CPU simulations on a workstation | Reuse an appropriate placement policy | Fixed-work representative cases and output checks |
| Memory-intensive single-node solvers | Evaluate CPU/NUMA locality jointly | Real NUMA hardware, sufficient memory, placement checks |
| Numerical analysis using threaded libraries | Avoid ineffective thread layouts or oversubscription | Declared runtime, opt-in thread changes, numerical validation |
| Monte Carlo ensembles and parameter sweeps | Optimize completed experiments per allocation | Multiple cases, joint worker/thread budgeting and isolated outputs |
| Shared research servers | Balance completion time with a coexistence policy | Separate objective from fastest dedicated-node execution |
| Containerized scientific jobs | Resolve recipes against visible allocated resources | Container/cgroup identity, usable tools, policy permissions |
| MPI/hybrid jobs and GPU workloads | Coordinate rank/thread/device locality | Dedicated adapters and measurements; later-stage scope |

Do not broaden into compiler optimization, solver-parameter selection, generic
hyperparameter search or autonomous cluster administration. Keep the execution
boundary precise so a reviewer can tell what caused a measured change.

## Architecture and remaining evaluation

Extend the existing planner/profiler/storage layers rather than replace them.
Implemented components include topology discovery, candidate generation, strict
trials/checks, search, recipe resolution and shared reporting. Use small typed
records at these boundaries, retaining the current readable artifact format.
Collectors should expose name, units, scope, availability and provenance; add
adapters only when their output answers a concrete question.

Evaluate on unchanged, fixed-work scientific programs with different compute
and memory behavior, on declared machines. Compare against inherited placement,
existing built-ins, a documented manual configuration and a simple fixed
candidate search under the same budget. Report no-improvement cases, failures,
search cost, recipe portability and effects of changing input sizes.

An appropriate research question is: **Can a small, allocation-aware search find
repeatable execution-policy improvements whose savings repay tuning across a
scientific workload family?** This can produce useful engineering and honest
negative results without claiming a new universal optimizer.

## Presentation references

The README uses a short purpose statement, runnable examples and links to deeper
documentation. Its restrained emoji cues follow the presentation style of
[Ruff's README](https://github.com/astral-sh/ruff); the command-first walkthrough
is also informed by [Hyperfine](https://github.com/sharkdp/hyperfine). These are
presentation references, not integrations, copied benchmarks or endorsements.
