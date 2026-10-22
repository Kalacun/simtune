# Profiles

Use built-ins without dependencies, or install the `yaml` extra and select a
custom YAML file explicitly:

```bash
simtune list-modes --profiles profiles/default.yaml
simtune run --mode desktop_friendly --profiles profiles/default.yaml --out runs --dry-run -- python examples/cpu_bound.py
```

Files are mappings keyed by names containing letters, digits, underscores and
hyphens, starting with a letter or digit. Each profile needs `description`,
`safety_notes`, `actions` (a list), and `fallback_mode` (a known profile name or
`null` to stop fallback). Repeated `--profiles` files are merged in order, with
later definitions overriding earlier names. They replace the built-in collection.
Empty/missing files, malformed YAML, unknown action types and invalid values
are errors. Auto mode requires all four built-in profile names to be present.

## Minimal custom profile

Save this as `local-profile.yaml`:

```yaml
local:
  description: "Limit interference while setting a workload parameter"
  safety_notes: "Changes only the launched process and descendants"
  fallback_mode: null
  actions:
    - type: nice
      level: 5
    - type: env
      vars:
        OMP_NUM_THREADS: "2"
```

```bash
simtune run --mode local --profiles local-profile.yaml --out runs --dry-run -- python examples/cpu_bound.py
```

`OMP_NUM_THREADS` only affects software that recognizes it; the pure-Python
example does not. Setting an environment variable is not proof that it changes
a workload's thread count.

## Actions

Every action supports a boolean `optional` field (default `false`). Unknown
action fields are rejected to catch typos.

| Type | Fields and behavior |
| --- | --- |
| `nice` | Integer `level` from −20 to 19, an absolute nice value. Unprivileged requests to increase priority preserve the inherited value and record an adjustment. |
| `ionice` | `class`: `idle`, `best-effort` (default), or `realtime`/`real-time`. Optional integer `level` 0–7; ignored for idle class. Tool presence does not imply scheduler support. |
| `taskset` | `cpus`: `available`, a list of nonnegative IDs, or a string such as `0-3,6`. Explicit IDs intersect the affinity snapshot; the resolved list is recorded. No available members means the action is skipped. |
| `numactl` | `interleave`, `membind`, `physcpubind` as nonempty strings or integers, plus optional string-list `args`. Node-expression semantics are delegated to numactl. WSL auto mode skips the action. |
| `cgroup_systemd` | Nonempty `properties` mapping, e.g. `CPUQuota: "80%"`, `MemoryMax: "4G"`. Uses `systemd-run --user --scope`; requires support for `--expand-environment=no`. Properties and controller permissions are checked by systemd at launch. |
| `env` | Nonempty `vars` mapping. Scalar values become strings, merged into the inherited environment. Keys cannot contain `=` or NUL; values cannot contain NUL. Quote YAML strings when exact spelling matters. |
| `ulimit` | Nonempty `limits` mapping, each value an integer or `[soft, hard]`. A scalar sets both limits. `-1` means unlimited on both Linux and macOS. Unknown or invalid limits fail validation. Unavailable platform resources or unprivileged hard-cap increases are skipped with a reason. |
| `mac_taskpolicy` | `policy: background` maps to `-b`; optional string-list `args` supplies explicit taskpolicy flags. No built-in mode requests this action. |

Resource names: `as`, `core`, `cpu`, `data`, `fsize`, `nofile`, `nproc`, `rss`,
`stack`. Availability and enforcement vary by OS. Units follow the underlying
resource API: CPU seconds, file-descriptor/process counts, and bytes for memory,
file-size and stack limits. Not every kernel enforces every exposed limit.

## Partial support and fallback

Unavailable required action types select the profile's fallback. Unavailable
optional actions do not force fallback. Cycles terminate with an explicit
rationale and the supported subset of the original profile. Concrete constraints
are resolved afterward; a required action may still be skipped if, for example,
none of its requested CPUs are available. Runtime wrapper errors fail the
iteration and never trigger a workload retry.

Built-in `desktop_friendly` makes Linux-specific actions optional so macOS can
use nice without a fallback cycle. `throughput` uses the available CPU set
instead of assuming CPUs 0–3 exist; on an unrestricted host this may be a no-op.
`latency` retains optional CPUs 0–1, intersected with allowed affinity.

Custom profiles are trusted configuration. Raw wrapper arguments and arbitrary
systemd properties are not a sandboxed configuration language. Review them just
as you would review a shell command, even though simtune does not invoke a shell.
