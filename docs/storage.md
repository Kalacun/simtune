# Run storage contract

New runs use `schema_version: 2` in `meta.json`. Missing version means legacy
version 1. Comparison supports both and rejects unknown versions. The seven
file names remain compatible with earlier runs.

| File | Contract |
| --- | --- |
| `meta.json` | Object: schema/package version, requested/resolved mode, argv, UTC timestamp, cwd, repeat/timeout, platform, auto flags, and available Git commit/branch/dirty status |
| `capabilities.json` | Object containing `capabilities` snapshot and `doctor` text; CPU logical count/allowed affinity are recorded when known |
| `applied_actions.json` | `requested`, `applied`, `skipped`, `adjusted` lists and `final_command` argv; execution status, note and notification result |
| `metrics.json` | `iterations` list of objects and `aggregate` object |
| `raw_profiler.txt` | Per-iteration stdout, stderr, time and perf output, separated by iteration delimiters |
| `command.txt` | One argument per line; human convenience only |
| `rationale.json` | Resolved mode, optional auto rule/inputs/baseline metrics and path, fallback reasons |

Metadata is written last. An interrupted write can leave an incomplete directory;
missing or malformed metadata must not be treated as a completed run. Files are
not a transactional database. Run directory allocation uses atomic `mkdir` with
numeric collision suffixes, allowing same-second concurrent directory creation.
Profiling itself still requires sequential use per Python process.

JSON is UTF-8, indented and sorted; serialization rejects NaN/infinity. Structural
validation always runs without an optional dependency: the required fields and
container types are checked, iterations must be objects, and final argv entries
must be strings. This is an intentionally small schema, not comprehensive
semantic validation of every nested OS-tool result. Extensions may add fields.

## Samples and aggregates

Each iteration records `return_code`, `timed_out`, monotonic `wall_time_sec`,
raw `rusage`, profiler metadata and normalized measurements. Launch failures
also include an `error` message. A timeout maps to return code 124.

Normalization prefers explicit normalized values, then time-tool CPU/RSS values,
then resource deltas. Zero is a real value. Missing RSS remains `null`. Fault,
context-switch and block-operation counts are resource deltas; they are neither
byte totals nor latency distributions. `max_rss_kb` uses KiB, including on macOS.

The aggregate records `iteration_count`, `successful_count`, `failed_count` and
`timed_out_count`. Each finite numeric measurement from successful iterations
gets `_count`, `_min`, `_median`, `_mean`, and sample `_stdev`. A single sample's
stdev is 0 by convention; it is not evidence of stability. Exit codes and
iteration numbers are not averaged. An all-failed run has counts but no
successful measurement averages. An empty input produces an empty aggregate.

The Python aggregation helper accepts metric-only dictionaries with absent
status for backwards compatibility. Stored-run comparison is stricter: each
nonempty iteration must contain an integer `return_code` and any `timed_out`
value must be boolean. It rejects status-free samples instead of assuming that
a fast command succeeded.

## Comparisons and legacy data

Comparison recomputes aggregates from iterations, including version 1 inputs,
because old aggregates may include failed commands. Raw metrics are not
rewritten. Empty iteration lists produce `status=unknown` and no measured means;
aggregate-only values cannot establish successful execution.

Each row includes mode, run path, success/failure/timeout counts, selected means,
and available `meta_` fields. JSON retains original strings. CSV prefixes cells
that could be interpreted as spreadsheet formulas; it is a display/export form,
not the authoritative record. Exports overwrite `compare.json` and `compare.csv`
in the selected output directory (the current directory by default).

Historical runs without command source hashes, complete machine metadata or
valid success status do not become reproducible just because they can be loaded.
See [the artifact audit](benchmarks.md#historical-artifact-audit).

## Extension records

The independent [version-1 extension formats](formats.md) are available as typed
Python APIs. They do not replace this run schema, change legacy comparison reads,
or migrate existing run directories. The stage-04
[experiment store](experiments.md#durable-layout-and-recovery) now implements
atomic experiment/trial record replacement, flushed state journals and a
separate final completion marker for the new strict path. These files are not
read as legacy runs, and the existing seven-file run format is unchanged.
