# WSL notes

simtune treats WSL as Linux. Detection uses `WSL_INTEROP`, `WSL_DISTRO_NAME` and
kernel markers, not a WSL-version API. The legacy `is_wsl` field does not prove
that a host is WSL2 specifically.

```bash
simtune doctor
simtune profile --out runs -- python examples/cpu_bound.py
simtune run --mode auto --out runs --dry-run -- python examples/cpu_bound.py
```

The first command shows the actual guest tool and permission situation.
Resource counters describe the guest; they do not measure the full Windows
host. CPU affinity is limited to guest-visible, allowed CPUs. In auto mode,
NUMA actions are always skipped on detected WSL hosts. Explicit NUMA requests
are delegated to numactl and can fail.

Perf and systemd user scopes are used only after successful harmless probes.
A missing or incompatible tool is skipped or triggers profile fallback. A
runtime wrapper failure is still a failed iteration; simtune does not promise
that WSL runs cannot fail or retry the workload automatically.

Do not enable systemd, elevate privileges, or alter perf permissions just to run
the test suite. Baseline profiling and best-effort process policies work without
those facilities. For performance work, record host/guest versions, memory/CPU
allocation and whether files reside in the Linux filesystem or a host-mounted
path. The archived WSL results are discussed in [the artifact audit](benchmarks.md).
