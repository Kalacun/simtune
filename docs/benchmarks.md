# Benchmark methodology and artifact audit

simtune records measurements; it does not establish that a tuning policy is
better. Distinguish a working launch, a valid measurement, and an experimental
conclusion. No performance improvement is claimed from the existing archive.
The [results directory](results/README.md) now contains a bounded manual-profile
demonstration on real handwritten-digit data. It is separate from the historical
archive and from the pending fixed-resource Linux placement study.

For a new study, follow [the heat workload and HPC protocol](hpc-testing.md).
It adds a fixed-work numerical example, a manual paired harness, Slurm templates
and [a report template](templates/benchmark-report.md). The example harness has
its own descriptive paired-bootstrap summary; legacy `simtune compare`
command still provides descriptive aggregate comparisons without intervals.
No new performance claim follows merely from adding these tools.

## Reproducible experiments

1. Use a fixed amount of work and verify equivalent output. Increase
   `examples/cpu_bound.py --iterations` until launch overhead is small relative
   to the workload. The current CPU and mixed examples use fixed work.
2. Record the exact source revision, arguments, working directory, Python and
   dependency versions, CPU model, logical/physical cores, RAM, OS/kernel,
   virtualization, available affinity, power mode and relevant background load.
   simtune records only part of this context; add a short experiment manifest.
3. Run `doctor`, inspect a dry plan, then inspect actual actions and exit status.
   A skipped action or successful wrapper exit is not proof of policy effect.
   Confirm cgroup/affinity/NUMA effects separately when they matter.
4. Use the same profiler path for baseline and tuned runs. Run warmups
   separately if needed, state how many, and do not silently discard outliers.
5. Alternate or randomize baseline/tuned batches, use enough repetitions for the
   question, and retain individual samples. Account for caches, contention,
   thermal drift and power management. The report example runs sequential
   batches and does not do this experimental design for you.
6. Check every return code. Report failure rates and sample counts alongside
   timings. Partial success can bias a mean even though failures are excluded.
7. State the hardware and uncertainty when presenting results. Legacy
   `simtune compare` provides descriptive aggregate comparisons without intervals.
   The [tuner's decision method](search.md#protocol-and-decision-rule) uses
   seeded paired-bootstrap 95% intervals for baseline/candidate ratios (>1
   favors the candidate), aggregated with predeclared case weights in log space.
   Validated improvement requires fresh, checked confirmation on distinct held-out
   scientific inputs, with at least five complete pairs per held-out case and
   aggregate improvement and every-case regression gates.
   The [manual heat harness](hpc-testing.md#8-confirm-analyze-and-publish-only-bounded-claims)
   has its own descriptive paired-bootstrap summary: intervals are absent below
   five pairs and describe only that session, without correcting for candidate
   selection, autocorrelation or allocation-level variation. Its summary is not
   automatically tuner evidence or [recipe replay authorization](recipes.md#evidence-and-labels).
   Small samples, drift and the chosen case mix limit inference. Intervals do not
   guarantee experimental validity, an optimum or transfer; none of these methods
   establishes causal attribution or automatic cross-machine comparability.

A useful local workflow (from an activated checkout):

```bash
simtune doctor
simtune run --mode throughput --out runs --dry-run -- python examples/cpu_bound.py --iterations 20000000
simtune profile --out runs --repeat 5 -- python examples/cpu_bound.py --iterations 20000000
simtune run --mode throughput --out runs --repeat 5 -- python examples/cpu_bound.py --iterations 20000000
```

Use the exact printed paths with `simtune compare ... --out reports`. The
`throughput` name does not imply a performance gain; applying the already allowed
CPU mask may change nothing. NUMA policy can help, hurt, fail, or be irrelevant.

## Example limitations

The original CPU/mixed examples looped for a fixed time and printed an amount
of work. Comparing their wall times is not a fixed-work throughput experiment.
The current versions perform fixed operations; their results should not be
combined with the old runs as if the source were identical.

The I/O example writes and reads buffered temporary files, then sleeps. Page
cache behavior and the deliberate sleep dominate interpretation; it does not
measure device bandwidth, durable writes, or I/O latency. Block operations in
rusage do not specify byte volume and differ across platforms.

## Historical artifact audit

The original local data is preserved byte-for-byte in `runs/`, `zagovor/`, and
root `compare.json`/`compare.csv`, and excluded from future Git tracking. These
paths are documented history, not required package resources. No meaningful
run data was deleted. A small anonymized [failure fixture](../tests/fixtures/legacy_taskset_failure/README.md)
is included for regression testing.

The archive has 11 run directories with metadata, 3 report directories with
output, and one empty report directory. All recorded run Git fields are empty.
Only architecture and kernel information identify the machines; CPU model,
RAM, power state and concurrent load were not captured.

| Local artifact | Recorded environment / outcome | Interpretation |
| --- | --- | --- |
| `runs/run_20260109_220339_baseline` | WSL2 x86_64, kernel 5.15.167.4, Python 3.13.2; 1 successful iteration | Launch/format example only |
| `runs/run_20260115_181714_baseline` | Same WSL kernel, Python 3.14.0; 5 exits with code 2 | Wrong script path; not workload timing |
| `runs/run_20260115_184317_throughput` | WSL, Python 3.14.0; 5 exits with code 127 | taskset tried to execute `--`; not workload timing |
| `runs/run_20260118_175747_throughput` | WSL, Python 3.14.0; 5 exits with code 127 | Same taskset bug; desktop notification also failed |
| `runs/run_20260118_174441_baseline` | WSL, Python 3.14.0; 5 successful iterations | Older example source is not revision-pinned |
| `runs/run_20260115_184459_isolated` | WSL, Python 3.14.0; 5 successful I/O-example iterations | Buffered I/O/sleep; controller effectiveness not verified |
| `runs/run_20260115_184537_isolated` | WSL, Python 3.14.0; 5 successful mixed-example iterations | Source revision and controlled conditions unavailable |
| `runs/run_20260118_175802_isolated` | WSL, Python 3.14.0; 5 successful mixed-example iterations | Source revision and controlled conditions unavailable |
| `zagovor/run_20260121_163956_baseline` | Darwin 25.2.0 arm64, Python 3.12.4; 3 successful CPU-example iterations | Historical format example, no causal performance evidence |
| `zagovor/run_20260121_164135_throughput` | Same Darwin/Python; 3 successful iterations | Only nice level 0 planned; taskset and NUMA skipped |
| `zagovor/run_20260121_164305_desktop_friendly` | Same Darwin/Python; 3 successful I/O-example iterations | Only nice level 5 planned; Linux actions skipped |

The WSL capability records report functional systemd-run and no perf or numactl.
The Darwin records report time/taskpolicy, with no Linux-specific tools. These
are historical snapshots, not compatibility guarantees for current machines.

The report `runs/run_20260115_184256_report/report.txt` advertises **51.674×** by
dividing a successful baseline time by failed taskset launch time. That number
is invalid as a speedup. The other two reports (`184457_report`, `184528_report`)
also lack controlled experimental conditions and pinned workload source; their
ratios are not evidence of tuning effectiveness. `184235_report` is empty.
Root comparison exports summarize the two macOS CPU runs; they do not establish
a meaningful improvement.

Earlier code also mislabeled cumulative rusage RSS, silently swallowed process
setting failures, and averaged failure exit codes as if they were performance
metrics. Existing artifacts cannot retroactively prove which settings took
effect. New code records these boundaries and refuses failed-run timing ratios.

Before including new evidence in a paper, application or README, curate a small
reviewed dataset with an experiment manifest and exact reproduction commands.
Do not force-add the unreviewed local archive simply to populate the repository.
