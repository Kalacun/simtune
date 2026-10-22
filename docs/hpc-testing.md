# Local and HPC test protocol

Start with the [checked heat workflow](../examples/HEAT_EXPERIMENT.md): build
explicitly, declare runtime/artifact identities, tune a strict checked manifest,
inspect the outcome and obtain fresh `validate` confirmation before reuse.
Tiny cases cannot show speedup. Native Linux acceptance is separately assigned to
[R06](implementation/release-plan/R06-linux-evidence.md), physical NUMA to R08,
and Slurm to R09. No Slurm adapter or ensemble executor exists.

The protocol below preserves the **independent manual/legacy paired route** and
site shell templates. Its results measure manually selected launch policies;
they are not automatic-search evidence. Both routes need actual target-system
observations and independent confirmation for bounded performance claims.

## 1. State the question before collecting timings

Start with: “For this fixed heat-stencil workload, binary and allocation, does
this explicit launch policy reduce elapsed time while preserving the numerical
check?” Choose one primary metric: simtune process wall time. Keep internal
kernel time and direct launcher time as separately named diagnostics.

Use three levels of testing:

| Level | Purpose | Acceptable conclusion |
| --- | --- | --- |
| Small local smoke | CLI/artifacts, numerical check, failure handling | The workflow operates on the tested host |
| Controlled single-node A/B | Same binary/input/resources, repeated paired trials | An observed policy effect for this case/environment |
| Independent confirmation | Fresh trials and allocations, held-out inputs | Evidence for a bounded workload/environment claim |

Do not infer NUMA effectiveness from a macOS test, a virtualized host exposing
one memory node, or a kernel-time-only microcase. Do not deliberately throttle
the baseline with nice/systemd quotas to manufacture a speedup.

## 2. Build once and check correctness

From the checkout, using an activated Python 3.11+ environment and an available
GCC/OpenMP compiler:

```bash
python -m pip install -e '.[yaml,dev]'
mkdir -p build
gcc --version > build/heat-compiler.txt
gcc -O3 -std=c11 -fopenmp examples/heat2d.c -lm -o build/heat2d
OMP_NUM_THREADS=2 OMP_DYNAMIC=FALSE build/heat2d --size 128 --steps 20
python -m pytest tests/test_heat_example.py
```

Use the compiler available on your machine/site; see
[build notes](../examples/HEAT_EXPERIMENT.md#build-and-smoke-test) for macOS and
a serial fallback. To require OpenMP tests with a specific compiler, set
`SIMTUNE_OPENMP_CC` to its executable path when running pytest. Without that
setting an unavailable OpenMP compiler is a reported skip, not a passed test.

Check JSON `valid: true`, `openmp > 0` for threaded tests and actual `threads`.
The complete grid is checked against its discrete eigenmode solution with
maximum absolute error ≤ `1e-9`. Inputs/build/precision must stay fixed across
policies; changing `--wave` or `--first-touch` defines a separate case.

## 3. Run the control and inspect a candidate

First verify the harness with no tuning actions:

```bash
python examples/heat_benchmark.py --binary build/heat2d \
  --mode inherited --size 512 --steps 100 --threads 2 \
  --pairs 6 --include-direct --out benchmark-results/control-001
```

This compares `simtune profile` with a no-action `simtune run`, plus an optional
direct invocation. Small timing differences are noise/overhead observations,
not tuning gains. The direct arm is not included in the primary ratio.

Then inspect a declared runtime candidate:

```bash
simtune doctor
simtune run --profiles profiles/heat-experiment.yaml --mode omp_close \
  --out benchmark-results/unused --dry-run -- build/heat2d --size 2048 --steps 200
```

On a suitable Linux NUMA host, `numa_interleave` is another candidate. Check
allowed memory nodes and site policy first. It may be slower, irrelevant or
unavailable. No profile name proves that the requested setting took effect.
The [numactl manual](https://man7.org/linux/man-pages/man8/numactl.8.html) explains
policy semantics and cpuset-relative resource selection.

## 4. Pilot, then freeze the protocol

Run a few **separately labeled pilot** invocations to choose a workload large
enough that startup/clock resolution is small relative to execution. Several
seconds per run is a useful starting target, not a statistical requirement.
Increase steps to extend work without increasing memory. Choose grid sizes
within your allocation: two grids use 64 MiB at 2048, 256 MiB at 4096 and 1 GiB
at 8192, plus runtime overhead. Check cgroup/Slurm caps, not only host RAM.

Before the measured study, record:

- Baseline (inherited policy) and candidate(s), including all runtime variables.
- Binary/source hashes, compiler/version/flags, Python/simtune/runtime versions.
- Fixed size, steps, wave, first-touch mode and thread count for each case.
- CPU/core/SMT/NUMA topology, actual affinity, allowed memory nodes and memory cap.
- Warmup count, pair count, trial-order seed, timeout and primary metric.
- Candidate-selection cases and independent confirmation cases/allocations.
- Minimum effect worth claiming (for example a prespecified 2% wall-time
  reduction), interval method, failure policy and allowed numerical error.
- Shared versus exclusive node, power settings if known, concurrent load and
  relevant scheduler binding. Record unknown values as unknown.

Warmups are retained but excluded from the measured ratio. Do not delete slow
samples. Running longer until a desired result appears is not a fixed protocol.

## 5. Collect paired measurements

For a frozen case, this is an example command, not a calibrated workload size:

```bash
python examples/heat_benchmark.py --binary build/heat2d \
  --mode omp_close --size 4096 --steps 500 --threads 4 \
  --pairs 10 --warmups 1 --seed 2718 --timeout 120 \
  --out benchmark-results/close-case-a-001
```

Use no more threads than the task is allowed to use. The harness checks actual
thread count and Linux affinity size, but it is not a full allocation validator.
It fixes `OMP_NUM_THREADS` and `OMP_DYNAMIC=FALSE` in every arm. Record inherited
binding variables: the baseline is your actual inherited environment, not an
imagined clean machine.

Run another candidate in a new directory only if it belongs to the declared
search. Candidates include `omp_spread` and, on an appropriate Linux host,
`numa_interleave`. Keep a control case in the study. Ten pairs are a starting
protocol, not a guarantee that the effect can be resolved.

For an input-size study, use a small/cache-oriented case and a larger case whose
working set is significant for the available cache/memory hierarchy. Keep
parallel first-touch as the normal condition. A serial-first-touch mechanism
study must be separately labeled and must use the same initialization mode in
both arms. Do not present a contrived initialization penalty as a universal gain.

Every directory preserves:

```text
manifest.json             # identity, fixed argv, environment subset, seed, status
candidate-profiles.yaml   # exact profile collection used
trials.jsonl              # phase/pair/order, status, metrics and checks per launch
summary.json              # paired ratio, interval, failures and limitations
warmup-.../               # retained warmups and raw run records
measurement-.../          # launcher logs, trial records, raw simtune directories
```

The harness rejects the summary ratio if any warmup/trial fails, a requested
action is skipped/adjusted, a fallback occurs, numerical validation fails,
actual thread count changes or the profiler configuration differs within a
pair. This is intentionally conservative. Artifacts remain available for
diagnosis; exit 1 does not mean they should be deleted. Interruption leaves
partial data and cannot be resumed by this example harness.

## 6. Run on Slurm

Use your site's supported compiler/Python modules and create the environment
where compute nodes can access it. Do not run a large benchmark on a login node.
Review [`heat.sbatch`](../examples/hpc/heat.sbatch) and
[`heat_step.sh`](../examples/hpc/heat_step.sh) before submitting. The defaults
request one node, one task, four CPUs, 4 GiB and 15 minutes; these are example
resource requests, not assumptions about your cluster or sufficient NUMA coverage.

Submit **from the repository root**, after choosing the account/partition and
resource settings required by your site. For example, with site options already
configured or added explicitly:

```bash
sbatch examples/hpc/heat.sbatch
```

The default is an inherited-versus-inherited control. A subsequent candidate
job can use exported parameters, reviewed before submission:

```bash
HEAT_MODE=omp_close HEAT_SIZE=4096 HEAT_STEPS=500 HEAT_PAIRS=10 \
  HEAT_SEED=2718 sbatch --export=ALL examples/hpc/heat.sbatch
```

Each job contains its own paired baseline/candidate trials on the same node.
Do not divide timings from unrelated control and candidate jobs. For a NUMA
experiment, request a site-approved allocation whose **task mask and memory
permissions actually span the intended NUMA domains**, then inspect them.
A multi-socket machine with a task pinned to one domain does not provide the
same experiment as an allocation spanning domains.

The script invokes the harness *inside* `srun`, so discovery and measurements
inherit the actual task context. It uses an explicit `--cpu-bind=verbose,cores`
example; adapt it if your site requires another binding policy, and preserve
that choice across arms. Do not disable binding just to obtain a wider mask.
Slurm allocation and binding are separate concepts; see its
[CPU management guide](https://slurm.schedmd.com/cpu_management.html) and
[srun reference](https://slurm.schedmd.com/srun.html).

The step stores compiler/build details, source hashes/snapshot, CPU/NUMA reports,
proc restrictions, a runtime diagnostic, candidate plan and the paired trials.
Optional metadata-tool failures remain in their logs; missing reports are not
proof of support. The script does not request exclusivity by default. For
performance claims, use a site-approved exclusive allocation where practical
or clearly report sharing and repeat across independent allocations.

Keep scheduler stdout (`simtune-heat-JOBID.log`) with the experiment. Capture site
accounting after completion, if available, with explicit scope/units; a job's
allocated CPU-hours, consumed CPU seconds, maximum RSS and billed cost are not
interchangeable. Script generation/syntax testing here does not establish that
the script has run on your cluster.

## 7. Verify policy effects separately

Before timed trials, perform a longer diagnostic run and inspect actual runtime
thread binding (for supported OpenMP runtimes, `OMP_DISPLAY_ENV=VERBOSE` and
`OMP_DISPLAY_AFFINITY=TRUE` are useful). The batch template captures a small
inherited-runtime diagnostic. Capture a candidate diagnostic separately as well.

On Linux, inspect the **actual workload PID**, not just the simtune/srun parent:
its `/proc/PID/status` affinity/memory permissions, `/proc/PID/numa_maps`, and
`numastat -p PID` when permitted. A node-level memory total alone does not prove
that every thread accesses local pages. Record tool errors or permissions denied.
Do not install privileged samplers or change system policy for this test.

For a numactl policy, `numactl --interleave=all -- numactl --show` is a small
policy-inheritance diagnostic within the same allocation; it does not measure
the simulation's final page placement. Keep such diagnostics separate from
timing samples and match the actual candidate options. simtune's planned-action
record is not an independent verification of these effects.

## 8. Confirm, analyze and publish only bounded claims

Choose a candidate using the declared selection cases. Freeze it before fresh
confirmation trials with a different order seed and held-out inputs, preferably
across at least three independent allocations as an initial replication target.
Three is not a proof of adequate sample size. Report per-allocation results;
do not pretend all tightly grouped trials are independent machines.

`summary.json` reports `R = exp(mean(log(baseline_i / candidate_i)))`. Above 1
favors the candidate. Its interval is a seeded 2,000-resample percentile bootstrap
over complete pairs within that session (absent below five pairs). It does not
correct for selection among candidates, autocorrelation or allocation-level
variation. Analyze independent allocation blocks separately; use an appropriate
block-level method if combining them. Do not pool different case sizes into one
unexplained average.

If a prespecified wall-time reduction threshold is `d`, the corresponding ratio
threshold is `1 / (1-d)`; ratio increase and time reduction are not identical.
Report the exact criterion used. Overlap with no effect, inconsistent allocations,
failed checks or unchanged effective policy should lead to an inconclusive or
no-improvement conclusion, not a speedup headline.

For claims from the implemented automatic tuner, additionally retain search
budget, all candidates/failures, held-out confirmation, recipe identity and
tuning overhead. Report break-even only for positive validated savings. The
manual harness alone cannot establish automatic selection quality.

Create a reviewed study under `docs/results/STUDY_ID/` using
[the report template](templates/benchmark-report.md). Include exact reproduction
commands, source revision or archived source hashes, environment, all relevant
samples/failures, analysis method and limitations. Preserve original raw evidence
locally; publish a small redacted copy plus a redaction record. Hostnames, paths,
job/account IDs and runtime diagnostics may disclose site/private information.

Publish no measured numbers until they actually exist. Label synthetic test
fixtures as synthetic. Valid no-improvement results are useful evidence of
engineering care; this one example cannot establish general scientific/HPC gains.
