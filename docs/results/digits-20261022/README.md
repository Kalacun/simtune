# Real handwritten-digit clustering, 2026-10-22

**A manual one-thread OpenMP profile reduced mean workload time from 2.096 s to
1.593 s (24.0%) on this Apple M2.** All 22 launches, including two warmups, passed
numerical and output-equivalence checks. This is a bounded local demonstration,
not an automatic-tuning result or a promise about other computers.

## Work and motivation

The input contains 1,797 real handwritten-digit images represented by 64 pixel
features, supplied with [scikit-learn's load_digits](https://scikit-learn.org/stable/modules/generated/sklearn.datasets.load_digits.html)
from the [UCI Optical Recognition of Handwritten Digits dataset](https://archive.ics.uci.edu/dataset/80/optical+recognition+of+handwritten+digits).
No artificial data expansion or sleep is used.

The program evaluates clustering stability across five fixed seeds, each with
50 K-means initializations and 10 clusters. It uses the Lloyd algorithm and the
same float64 inputs, code, dependencies and scientific parameters for every
launch. It recomputes each resulting sum of squared distances independently.
This is a small real-data numerical analysis, not a production-scale simulation.

Small parallel jobs can spend more time coordinating workers than doing useful
work. The manual profile sets only `OMP_NUM_THREADS=1` in the child environment.
The reference preserves the inherited environment. Threadpool diagnostics report
a configured OpenMP pool of eight versus one. These are runtime configuration
observations, not per-worker CPU-placement traces. simtune does not change the
parent environment or require elevated privileges.

## Protocol and complete data

- Hardware: Apple M2, eight logical CPUs, 8 GiB RAM; physical macOS host, no VM.
  Darwin 27.0.0, arm64, Python 3.14.8. Power mode and unrelated background load
  were not independently instrumented; the machine was not dedicated.
- Scientific dependencies: NumPy 2.5.4, SciPy 1.18.1, scikit-learn 1.9.1,
  threadpoolctl 3.7.0. simtune 0.1.0; YAML 6.0.3.
- Exploratory pilots checked a kernel-eigenvalue analysis and a single-seed
  clustering job. They are retained in [pilots.json](pilots.json), including the
  modest kernel result. Pilots may overlap regression testing and are excluded
  from the fresh measurements below. Their local library paths are omitted.
- After the regression suite finished, the [frozen protocol](protocol.json)
  prescribed one warmup per mode and ten pairs, with five orders in each direction
  shuffled using a fixed seed. No retry, outlier removal or timing-based stopping.
- Every measured run uses the same simtune profiler path, one repetition and a
  60-second timeout. Workload timing includes Python startup, imports, analysis,
  validation and shutdown. CLI timing also includes simtune discovery and recording.
- Every result must have the same dataset hash and all five identical label hashes
  as the first reference warmup. Objectives must match at relative tolerance
  1e-10 / absolute tolerance 1e-6; every objective is also independently recomputed.
  All checks passed. No outputs were discarded for being slow.

[results.json](results.json) contains every warmup and paired sample, CLI status,
wall/compute time, runtime observations, input digest, label digests and numerical
objectives. [summary.json](summary.json) contains the arithmetic means, individual
pair ratios and a descriptive paired bootstrap. Original simtune run records are
retained locally; the published projection omits local paths and machine names.
It retains all timing samples and scientific checks.

| Measured quantity | Inherited profile | One-thread profile |
| --- | ---: | ---: |
| Mean workload elapsed, including Python startup | 2.096 s | 1.593 s |
| Mean analysis and in-program checking | 1.248 s | 0.742 s |
| Mean entire simtune CLI invocation | 2.228 s | 1.726 s |
| Measured launches | 10 | 10 |
| Warmups retained separately | 1 | 1 |
| Failed launches or equivalence checks | 0 | 0 |

The geometric mean of paired baseline/candidate workload ratios is **1.316×**.
A seeded 10,000-resample paired percentile interval is **1.299–1.332×**.
This interval describes sampling within this session; it does not correct for
pilot selection, thermal drift, autocorrelation or machine-to-machine variation.
The complete CLI invocation also improved, by about 22.5% in mean elapsed time.

The absolute workload saving is about **0.503 seconds per analysis** here. This
is a practical illustration of a launch setting that can matter for repeated
analyses; it is not an estimate of automatic-search amortization.

## Reproduce

Use Python **3.12–3.14** for the pinned scientific dependencies (simtune itself
supports 3.11–3.14), from the source repository:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install '.[yaml]' -r examples/digits-requirements.txt
simtune doctor
python examples/digits_benchmark.py --out benchmark-results/digits
```

Do not run other benchmarks or tests at the same time. Inspect the generated
protocol before interpreting results: another platform may have a different BLAS
backend, thread configuration, power state or workload cost. A slower candidate
is a valid result. The output directory must be fresh; attempts and failures remain
recorded. A mismatch stops the experiment with no performance ratio.

Inspect a single launch with:

```bash
simtune run --profiles profiles/digits.yaml --mode one_thread --out runs --timeout 60 -- python examples/digits_workload.py
```

The harness and workload source hashes are recorded in the protocol. This
example uses **manual profiles with different thread budgets**. The automatic
Linux tuner currently holds resource counts fixed; its distinct held-out-input
and recipe-validation requirements are not satisfied by this experiment.
No Linux placement, physical NUMA, real perf-counter, Slurm or cross-host benefit
is claimed. Unchanged clustering results here do not prove equivalence for
arbitrary scientific software; users must define appropriate checks.
