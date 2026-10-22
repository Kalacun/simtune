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

Local full suites before the hosted test-portability repair: **916 passed, two
Linux-only skips** for both the source
suite (161.94 s) and a fresh installed wheel outside the checkout (163.51 s),
Python 3.14.8/macOS. Focused packaging/demo checks: 79 passed. Ruff lint/format
passed; the subsequent test repair passed 95 focused regressions. The selected
source tree and fresh wheel/sdist passed ownership, content,
identity and link audits; two fixed-epoch builds produced identical archives.
The bounded Linux CLI driver has also passed on an actual GitHub-hosted Linux
runner using the installed wheel outside the checkout.

Hosted verification is recorded in [run 39763864452](https://github.com/Kalacun/simtune/actions/runs/39763864452)
at commit `fee0d83295193c5e02e739b9c2439da51dd43724`: **all 25 jobs passed**.
This covers source and installed-wheel tests on Linux/macOS across Python
3.11–3.14, plus lint, formatting, HPC script syntax and rebuilt archive audits.
The Linux base environments recorded
912 passed / six skips; extras environments recorded 917 passed / one skip.
Five skips concern intentionally absent YAML/Rich extras, and one is the
non-Linux unsupported-host case. The two actual affinity/OpenMP checks also
passed separately with zero skips on every Linux source job.

Hosted macOS base environments recorded 883 passed / 35 skips, and extras
environments recorded 888 passed / 30 skips. The default hosted macOS compiler
lacks OpenMP, accounting for 28 skips; two checks require the actual Linux kernel.
Base environments also skip the same five optional-dependency checks. Those
OpenMP numerical tests passed locally with the configured compiler and in Linux
CI. A hosted macOS job passing does not mean its skipped OpenMP gates ran.

The Linux CLI check inherited four CPUs, narrowed child work to CPUs 0–1, and
observed both OpenMP workers within that allowed mask for all three admitted
policies. It checked numerical outputs, wrong-reference rejection, missing
required-tool rejection, TERM cancellation of workload and validator, and refusal
to resume interrupted or uncertain work. Tune/export, explicit fresh validation
and replay passed. Modified binary/runtime hashes and a narrower CPU allocation
were rejected before launch. The outer parent's CPU mask was unchanged.

Both the tiny heat search and its validation recorded `no_improvement`; the
derived reference recipe was replay-validatable after successful scientific and
protocol checks. These millisecond-scale diagnostic inputs establish functional
behavior only. They do not establish distinct worker placement, physical NUMA
locality or a performance gain. The run retains a `native-cli-evidence` artifact
for 30 days; the maintainer also retains the downloaded records and their hashes.

A nearly full local disk interrupted earlier verification attempts (ENOSPC).
Their logs were retained; temporary environments/test scratch were removed and
the successful full suites above were rerun sequentially. A scratch-directory
configuration error also correctly triggered source-tree separation guards and
was corrected without weakening those guards.

The [first hosted run](https://github.com/Kalacun/simtune/actions/runs/39762951741)
exposed a unit test that assumed macOS. It was cancelled after the failure was
established. The repaired test injects a synthetic unsupported host; actual Linux
execution remains covered separately. No failed run is counted as a pass.

| Gate | State |
| --- | --- |
| Final full local suite / Ruff | Passed; counts and scope above |
| Selected public tree / rebuilt distribution | Passed ownership/content/link checks; deterministic fixed-epoch builds |
| Hosted Linux/macOS, Python 3.11–3.14 | All 25 jobs passed at the linked commit; skips explained above |
| Linux affinity / OpenMP checks | Passed on actual hosted Linux; installed CLI workflow and worker masks inspected |
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
