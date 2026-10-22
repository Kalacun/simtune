# Public release status

Updated 2026-10-22. The archive ownership defect R07-F06 has a local repair and
header regressions; rebuilt artifacts pass the strengthened ownership/content
checks. The source-selection helper also supports public checkouts with curated
documentation already at its destination. Old leaking tarballs are not published.

The [current check matrix](../../candidate-review.md) records final local and
hosted verification separately. The [real-data manual-profile result](../../results/digits-20261022/README.md)
includes all ten pairs and successful equivalence checks on macOS. It establishes
no automatic fixed-resource Linux gain or cross-machine performance guarantee.

R06's dedicated Linux study remains pending. Physical NUMA, native perf and
Slurm gates remain unrun/deferred; no additional roadmap stage is marked complete.
The finalization changes have author verification, not independent review.
Version 0.1.0 remains experimental. See the [user guide](../../user-guide.md).
