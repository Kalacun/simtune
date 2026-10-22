# Public release status

Updated 2026-10-22. The archive ownership defect R07-F06 has a local repair and
header regressions; rebuilt artifacts pass the strengthened ownership/content
checks. The source-selection helper also supports public checkouts with curated
documentation already at its destination. Old leaking tarballs are not published.

The [current check matrix](../../candidate-review.md) records final local and
hosted verification separately, including an earlier 25-job Linux/macOS
Python 3.11–3.14 pass and a later macOS cancellation race. That race is repaired
with deterministic regressions and bounded final-signal retry; see the current
main-branch CI link in the matrix for the exact post-repair commit. The installed
Linux CLI functional gate also passed, with
actual worker masks and scientific checks inspected; its tiny heat search and
revalidation reported no improvement. The [real-data manual-profile result](../../results/digits-20261022/README.md)
includes all ten pairs and successful equivalence checks on macOS. It establishes
no automatic fixed-resource Linux gain or cross-machine performance guarantee.

R06's dedicated Linux study remains pending. Physical NUMA, native perf and
Slurm gates remain unrun/deferred; no additional roadmap stage is marked complete.
The finalization changes have author verification, not independent review.
Version 0.1.0 remains experimental. See the [user guide](../../user-guide.md).
