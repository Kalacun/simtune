# Capability matrix

“Available” means the launch path exists, not that a setting is effective or
permitted. `simtune doctor` reports the current host. No action changes global
kernel settings.

| Feature | Linux | WSL | macOS |
| --- | --- | --- | --- |
| CPU time / resource counters | POSIX child-resource deltas | Same, guest-kernel limits | Same, OS-specific counters |
| RSS | GNU-compatible time output, else unknown | Same | Compatible `time -l` output, bytes converted to KiB |
| `nice` | POSIX; priority increases constrained | Same | Same |
| `env` | Child environment | Child environment | Child environment |
| `ulimit` | Available resource constants; kernel-dependent enforcement | Same | Available subset; kernel-dependent enforcement |
| `ionice` | Installed tool; I/O scheduler-dependent | Guest tool and scheduler-dependent | Skipped |
| `taskset` | Installed tool; intersects allowed CPU affinity | Guest-visible CPU affinity | Skipped |
| `numactl` | Installed tool; topology/permission-dependent | Auto skips it; explicit requests may fail | Skipped |
| systemd user scope | Functional probe, supported command flags, delegated resources | Requires a working guest user manager | Skipped |
| `perf stat` | Functional harmless probe; runtime may still fail | Guest counters/permissions may be unavailable | Skipped |
| `taskpolicy` | Skipped | Skipped | Installed tool; custom profiles only |

## Implementation and acceptance

| State | Scope | Gate |
| --- | --- | --- |
| Implemented | Manual legacy CLI; strict manifests/checks, fixed CPU/thread paired search, recipe export/load/replay/validation, config/IDs/show/resume, perf imports | Local software tests; [release handoffs](implementation/release-plan/STATUS.md) |
| Experimental | Strict one-program single-node Linux/OpenMP/NUMA launches and perf counter collection | Hosted Linux affinity/OpenMP and checked CLI workflow passed; dedicated R06 study, physical NUMA and real counters remain pending |
| Planned/deferred | Slurm adapter, ensemble executor, thread-count search and workflow integration | R08/R09/R10 or later scope |

The [checked heat example](../examples/HEAT_EXPERIMENT.md) uses a declared runtime,
pinned validator and fresh artifact hashes without optional Python extras.
Strict settings reject missing inherited affinity, known memory conflicts and
lost requirements. Manual profiles keep best-effort fallback. Allocation CPU
counts are never CPU IDs. Repeated commands still repeat external side effects;
isolated trial cwd only separates local files. Captured logs buffer in memory
without a size cap, so verbose long jobs need the separate streaming follow-up.

Dedicated Linux placement-study acceptance remains **pending R06**; physical NUMA,
real perf counters and real Slurm remain **not run**. Earlier readiness findings
have been repaired and rechecked locally. See the [current check matrix](candidate-review.md)
for the exact local/hosted state and independent-review limits. The
[manual real-data result](results/digits-20261022/README.md) is a macOS thread-budget
comparison, not a Linux placement result. Native CI checks establish only the
behavior actually executed on those runners, not performance transfer.

Probes use short timeouts. Missing binaries, permission errors and probe timeouts
become unavailable capabilities with diagnostics. Time is probed with the exact
output flags used for execution; incompatible implementations are skipped.
Systemd scopes require `--expand-environment=no` support to preserve command
arguments (the option was introduced in systemd 254). A successful scope probe
does not validate every profile property or controller delegation.

WSL detection checks environment markers and the Linux kernel version. It does
not rigorously distinguish WSL1 from WSL2; the legacy JSON field `is_wsl` means “WSL detected.” See
[WSL notes](wsl2_notes.md).

Native Windows workload execution is rejected with a clear CLI error. Help,
mode listing and basic doctor output do not require the Unix resource module.
CI is configured to check Linux/macOS deterministic planning, installed base/extras
wheels and small native Linux strict subprocess/OpenMP behavior; privileged perf, NUMA effectiveness and delegated cgroups need manual
validation on the target machine.
