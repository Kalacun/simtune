# Optional measurements and perf stat imports

Use the [checked heat workflow](../examples/HEAT_EXPERIMENT.md) for a quiet,
file-checked example on the base package. Captured solver/validator logs currently
buffer fully in memory; there is no streaming/truncation size cap. Counter tool
configuration is not native perf evidence; see [capabilities](capability_matrix.md).

New `tune` runs use `--collectors timing` by default. This measures elapsed
seconds with a monotonic clock and records sequential child `rusage` in each
trial's `launch.json`; it needs no external profiler. `rusage` CPU time includes
reaped wrappers and is not memory bandwidth. Its `ru_maxrss` lifetime high-water
mark is deliberately unavailable as a per-trial RSS value. Elapsed time is the
only selection objective. Legacy `profile` keeps its existing time/perf defaults.

On Linux, `tune --collectors perf-stat` explicitly requests `task-clock`,
`cycles`, and `instructions` through `perf stat`. Before any workload trial,
simtune reserves and budgets separate bounded version/harmless perf probes
(maximum two seconds each, capped to remaining exploratory time). Dry-run/timing
starts no collector probes. Probes cannot borrow confirmation reserves. Missing perf,
permission errors, or a failed probe stop the request before a workload launch;
use the default `timing` mode on such hosts. A later wrapper or parse failure
marks that trial failed. Simtune never retries the workload without perf.
The same collector mode and version are used for both members of every pair.
Perf adds a wrapper and counter overhead, so compare only trials collected by
the same protocol. Requested counters absent from a successful perf output are
recorded as unavailable, not zero. Multiplexed values retain the reported
running percentage and computed scaling factor; they are never treated as direct
observations. A read-only launcher affinity snapshot is stored as launcher
scope only. It says nothing about later worker affinity or physical page
placement. No bandwidth or remote-memory conclusion is inferred from CPU time,
RSS, or these generic counters.

## Supported external format: `perf-stat-v1`

Export UTF-8 semicolon-delimited statistics from a trusted, already completed
program invocation, for example `perf stat -x ';' --no-big-num -o stats.perf --
PROGRAM ARG...`. Importing never runs that program. Each nonblank, noncomment
row has 3 to 7 fields:

```text
value;unit;event[;event_runtime[;running_percentage[;metric_value[;metric_unit]]]]
```

Examples below are synthetic, not host measurements:

```text
# synthetic example
123,5;msec;task-clock;1000000;75.00;;
<not counted>;;cycles;1000000;0.00
456;;instructions
1,0;seconds;seconds time elapsed
```

Use ungrouped decimal numbers with a dot or comma decimal separator. Thousands
grouping is unsupported. `<not counted>` and `<not supported>` are unavailable
values. Duplicate events remain separate rows; import never sums them. The
supported units are empty (stored as `count`), `msec`, `usec`, `nsec`, `seconds`,
`Joules`, `Watts`, and `GHz`. Unknown units and malformed/partial rows fail
import. Runtime and metric fields are retained verbatim in each row's extension.
The running percentage is stored as a fraction; when below 100%, the reported
value is marked as perf-scaled and the reciprocal factor is recorded. A raw
counter value cannot be recovered from it. All rows use the declared context
scope; events from separate imports/scopes are never combined.

Supply a JSON context file with **exactly** these fields. Its values are user
declarations, not verified facts about the external process:

```json
{
  "kind": "simtune_perf_stat_context",
  "schema_version": 1,
  "command": ["/absolute/path/program", "fixed-argument"],
  "input_identity": "64-character current simtune workload SHA-256 fingerprint",
  "host": "host-or-allocation-label",
  "versions": {"perf": "perf version as observed externally"},
  "scope": "process_tree",
  "fixed_env": {},
  "workload_fingerprint": null,
  "environment_fingerprint": null
}
```

`scope` is one of `process`, `process_tree`, `job_step`, or `job`. A null
fingerprint makes the import inspectable but ineligible as a tuning hint.
To use it with `tune --from-run`, declare both 64-character simtune fingerprints
from the exact current workload/environment, set `input_identity` equal to the
workload fingerprint, and use the exact workload argv template and fixed
environment. Those fields must match again when tuning starts. The host label
and perf version remain declared provenance; the environment fingerprint checks
the current resource/tool snapshot, not the truth of the external measurement.

```bash
simtune import-profile --format perf-stat --input stats.perf --context context.json --out imported-profile
simtune tune --from-run imported-profile --workload experiment.toml --budget 10m --out fresh-search
```

Import creates `raw.perf` and `context.json` with the original bytes, plus a
versioned `profile.json` with SHA-256 digests, typed metrics, units, scope,
availability, and scaling metadata. Loading rejects tampered raw files or
unsupported versions. A compatible, unique, unscaled process-tree task-clock
and elapsed pair can only reorder CPU placement hypotheses. Missing, duplicate,
multiplexed, or differently scoped values leave the deterministic order. No
imported timing or counter enters a trial score, training selection, held-out
confirmation, or recipe validation. Every outcome still requires fresh paired
workload executions and the existing scientific checks.
