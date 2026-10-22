# Scientific workflows with the current CLI

The [checked heat example](../examples/HEAT_EXPERIMENT.md) is the complete
implemented path: explicit build, fresh artifact hashes, fixed-count CPU/OpenMP
resources, isolated outputs, a pinned command validator, bounded paired search,
inspection, fresh validation and exact-argv replay. It uses TOML and the base
package; no YAML/Rich is required. Strict search requires actual Linux resource
observations. macOS keeps the manual route below. Native acceptance and performance
claims remain pending [R06](implementation/release-plan/R06-linux-evidence.md).

`tune` preserves every failure/exclusion and scientific settings within each case.
It deduplicates identical resolved plans and never changes CPU/thread counts.
Checks include exact file hashes, finite numeric JSON or bounded command validators.
Exit success without a declared check is unchecked. `no_improvement` may retain a
training-only unvalidated reference; use explicit fresh `validate`, then inspect
the derived recipe. `exec` checks applicability and runs exactly once with no
baseline/check; current output remains unchecked. Large captured logs are buffered
in memory; quiet programs are the current supported study scope.

## Manual/legacy route

The following independent route preserves the original `profile/run/compare`
semantics, custom YAML and best-effort fallback. It does not use the strict
manifest, trial isolation or scientific check engine. All caveats below refer to
these manual commands. See [search](search.md), [recipes](recipes.md) and
[measurements/imports](measurements.md) for the implemented strict commands.

## 1. Choose a representative workload

Use the same program, input, seed and scientific settings for every policy in a
comparison. Include production-like memory use: a small test that fits in cache
may tell you little about a large simulation. Reserve additional representative
cases for checking a promising result.

The commands below use `./solver` and `inputs/*.in` as **placeholders for your
own program and input files**. They are not included examples. For a runnable
functional demonstration, follow the quick start in the [README](../README.md).

Every repetition executes the command again. Arrange separate outputs or a safe
reset using your program's options or your own wrapper; manual `profile/run` does not isolate trial output directories or run
setup/cleanup hooks; strict workloads do. simtune's `--out`
controls measurement storage, not the workload's working directory or output.

## 2. Define a candidate explicitly

Install the YAML extra from the checkout with `python -m pip install '.[yaml]'`.
Save this file as `launch.yaml`:

```yaml
inherited:
  description: "Preserve inherited execution settings"
  safety_notes: "No tuning actions; the workload is still profiled"
  fallback_mode: null
  actions: []

numa_interleave:
  description: "Candidate: interleave memory across permitted NUMA nodes"
  safety_notes: "Linux numactl required; may be slower or unavailable"
  fallback_mode: null
  actions:
    - type: numactl
      interleave: all
```

This candidate requests a memory policy and adds no CPU-binding request.
`numactl` normally interprets `all` within the current cpuset. A requested memory
policy may still be rejected or affected by the application. Interleaving is
one hypothesis to test, not a default recommendation. See the
[numactl manual](https://man7.org/linux/man-pages/man8/numactl.8.html).

Use this NUMA candidate only on an appropriate Linux host where cluster policy
permits it. Avoid hard-coded CPU or node IDs copied from another machine. The manual planner does not validate NUMA topology or memory-node availability;
the separate strict planner checks declared requirements.

```bash
simtune doctor
simtune list-modes --profiles launch.yaml
results=$(mktemp -d)
simtune run --profiles launch.yaml --mode numa_interleave --out "$results/candidate" --dry-run -- ./solver inputs/reference.in
```

Review the planned command, skipped actions and rationale before measuring.
**`fallback_mode: null` stops fallback traversal; it does not enforce strict
execution.** Current `run` can skip an unavailable action and still run the
workload. Do not label that measurement a successful NUMA-policy trial.
The recorded `applied` actions are a plan, not independent kernel-state checks.

Custom files replace the built-in profile collection. This example deliberately
uses explicit modes; it does not define the profiles required by `auto`.
See [the profile format](profiles.md) for CPU-affinity, environment and other
actions. For a declared OpenMP application, environment actions can request
`OMP_NUM_THREADS`, `OMP_PLACES` and `OMP_PROC_BIND`; choose settings within the
allocation and validate the numerical outputs. Other runtimes may ignore them.
The [LLVM runtime reference](https://openmp.llvm.org/design/Runtimes.html)
documents these OpenMP controls.

## 3. Measure and validate the comparison

After confirming that the requested policy is appropriate and available:

```bash
simtune profile --out "$results/baseline" --repeat 5 -- ./solver inputs/reference.in
simtune run --profiles launch.yaml --mode numa_interleave --out "$results/candidate" --repeat 5 -- ./solver inputs/reference.in
simtune compare "$results"/baseline/run_* "$results"/candidate/run_* --out "$results/comparison"
```

These are simple sequential batches. For a serious comparison, alternate fresh
baseline and candidate batches, control warmup/cache conditions as appropriate,
and repeat on additional representative inputs. Manual `profile/run/compare` does not randomize trials, compute paired bootstrap
intervals or validate scientific outputs; `tune/validate` supplies those features.

Inspect failures before interpreting means. Check output hashes when exact
reproducibility is expected, or use domain-specific tolerances and invariants.
Compare variability and practical savings as well as averages; five repetitions
are an example, not a guarantee of adequate evidence. Keep the inherited policy
when the candidate is unavailable, incorrect, slower or indistinguishable from
noise. Record hardware, allocation, software, input identity and the manual
decision alongside the run data. Current metadata is not a complete experiment
manifest; see [benchmark methodology](benchmarks.md).

## 4. Reuse the manually selected profile

Keep `launch.yaml` with the experiment's configuration and decision record. For
example, this launches a new input with the inherited profile:

```bash
selected_profile=inherited
simtune run --profiles launch.yaml --mode "$selected_profile" --out runs/experiments -- ./solver inputs/experiment-001.in
```

Set `selected_profile=numa_interleave` only if your comparison and validation
justify it. Repeat the explicit-mode command from your existing batch script or
workflow for later inputs. Explicit `run` applies that profile without adding
an auto-mode baseline; it still records and profiles every execution.

With manual profiles you manage compatibility yourself. A chosen policy may stop helping
when input size, thread count, runtime, topology, allocation or host load changes.
Manual profiles carry no workload-identity/replay gate or performance guarantee.
The separate strict recipe path checks declared identity and applicability and
executes with minimal instrumentation; it still promises no current speed gain.

## HPC execution boundary

Run simtune where the program actually executes: **inside the allocated
compute-node job step**, with the intended task's inherited binding. Follow
your site's batch-script and launcher conventions. Profiling an `srun` or
`mpirun` launcher on a submission host does not collect remote ranks' CPU or
memory usage.

Slurm can control both allocation and task binding; CPU numbering in its
metadata can differ from Linux numbering. Preserve those decisions unless
intentional rebinding is permitted and understood. See Slurm's
[CPU management guide](https://slurm.schedmd.com/cpu_management.html).

The current CLI has no Slurm adapter. Strict discovery uses observed task/cgroup
restrictions; manual profiles require you to check CPU and memory restrictions, avoid overlapping launcher and profile binding
requests, and do not use a systemd desktop quota as a dedicated-node performance
policy. Neither the name `throughput` nor an available `numactl` binary establishes
that a policy is useful or permitted on your cluster.

This guide does not establish MPI, multi-node or whole-job measurement support.
Those need explicit placement and measurement adapters. The [roadmap](roadmap.md)
proposes that progression while keeping the initial tuner focused on one node.
