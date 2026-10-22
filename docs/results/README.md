# Measured results

- [Handwritten-digit clustering, 2026-10-22](digits-20261022/README.md): real UCI
  images, ten balanced pairs on Apple M2/macOS, all numerical/label checks passed.
  A manual one-thread OpenMP profile reduced mean workload time by 24.0% in this
  session. Includes the protocol, every sample, warmups and exploratory pilots.

This manual thread-budget result is separate from automatic fixed-resource
Linux placement search. A dedicated Linux placement study, physical NUMA and
Slurm evidence remain absent. The heat program is a correctness/test example,
not itself evidence of a gain.

For new studies use the [methodology](../benchmarks.md) and
[report template](../templates/benchmark-report.md). Retain failures, slow samples
and no-improvement results. Curate published data deliberately; keep unreviewed
raw output in ignored local directories.
