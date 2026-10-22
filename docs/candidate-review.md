# Release checks and scope

Updated 2026-10-22. simtune 0.1.0 remains experimental. This page distinguishes
local engineering checks, actual measurements and target-system evidence.

The prior review's archive ownership defect (R07-F06) is repaired: the sdist
writer clears UID/GID/account names and extended filesystem metadata, normalizes
permissions and timestamps, and emits a neutral gzip header. The archive checker
now rejects identifying ownership on directories and files, plus unexpected PAX
metadata. A second repair lets the source-selection helper operate from a public
checkout or unpacked sdist while still checking curated content hashes and paths.

The [real-data demonstration](results/digits-20261022/README.md) records ten paired
manual-profile comparisons on an Apple M2 using UCI handwritten-digit images.
Every label hash matched; every clustering objective passed a recomputation and
cross-run tolerance check. The mean workload time decreased from 2.096 to 1.593
seconds by setting OMP_NUM_THREADS=1. This is a manual thread-budget experiment,
not evidence for fixed-resource automatic tuning, held-out confirmation or transfer.

Current local gates: **916 passed, two Linux-only skips** for both the source
suite (161.94 s) and a fresh installed wheel outside the checkout (163.51 s),
Python 3.14.8/macOS. Focused packaging/demo checks: 79 passed. Ruff lint/format
passed. The selected source tree and fresh wheel/sdist passed ownership, content,
identity and link audits; two fixed-epoch builds produced identical archives.
The new bounded Linux CLI driver is exercised by hosted CI, not by macOS tests.

A nearly full local disk interrupted earlier verification attempts (ENOSPC).
Their logs were retained; temporary environments/test scratch were removed and
the successful full suites above were rerun sequentially. A scratch-directory
configuration error also correctly triggered source-tree separation guards and
was corrected without weakening those guards.

| Gate | State |
| --- | --- |
| Final full local suite / Ruff | Passed; counts and scope above |
| Selected public tree / rebuilt distribution | Passed ownership/content/link checks; deterministic fixed-epoch builds |
| Hosted Linux/macOS, Python 3.11–3.14 | Pending first GitHub run |
| Linux affinity / OpenMP checks | Pending hosted Linux execution |
| Dedicated Linux placement performance study (R06) | Not run |
| Physical NUMA / real perf counters / Slurm | Not run / deferred |
| Independent review of these finalization changes | Not performed; author checks only |

Earlier review sessions and historical findings remain preserved privately.
This finalization does not relabel an author check as independent review or mark
the deferred implementation stages complete. The earlier five findings had
already been repaired and rechecked before this continuation.

Captured output is held in memory, dynamic dependencies are only as complete as
the workload declarations, and escaped process groups may leave teardown uncertain.
Use trusted, quiet workloads safe to repeat. Read the [user guide](user-guide.md),
[distribution policy](../distribution/README.md), and [publishing guide](github-publishing.md).
