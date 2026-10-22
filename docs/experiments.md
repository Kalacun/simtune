# Workloads and isolated trial records

Stage 04 adds an internal experiment API without replacing the existing
`profile`, `run`, or `compare` commands. It is the execution and evidence layer
for the stage-05 [tune command](search.md); it does not generate candidates, rank policies, save
recipes, or claim a speedup.

## Workload loading and identity

`simtune.workloads.load_workload()` accepts strict version-1 TOML or JSON
`WorkloadSpec` files. A relative `base_directory` is resolved from the manifest
directory. The executable and declared artifact paths are then resolved from
that base, independent of the caller's current directory. The executable must
be an executable regular file. Every declared SHA-256 is streamed and verified
when the manifest is loaded and again at the trial boundary; simtune does not
silently refresh an identity after a file changes.

Only exact whole arguments `{case.NAME}` and `{trial_dir}` are substituted.
Substituted case values are not expanded a second time. Shell syntax,
environment-variable expansion, partial placeholders and arbitrary attributes
have no special meaning. The workload fingerprint includes the normalized
manifest, declared hashes, resolved paths, runtime identity and fixed
environment overrides. Undeclared files, dynamic libraries, containers,
services, network state, accelerator state and ambient variables outside
`fixed_env` remain explicitly untracked.

The harmless [stage-04 fixture](../tests/fixtures/experiments/workload.toml)
is a complete loadable manifest. It writes a fixed numeric result and checks
that result; it is a correctness fixture, not benchmark evidence.

## Trial execution

`simtune.experiments.TrialExecutor.run()` accepts an explicit `TrialRequest`.
The case, policy, phase, pair and global order come from the caller. The
executor requires the policy runtime and resource budget to match the fixed
workload/case declarations. It uses `plan_strict_launch()` and
`execute_strict_launch()`; a missing required mechanism is never replaced by a
best-effort fallback under the requested policy name.

Each default trial owns `trials/TRIAL_ID/work/`, which is both its working
directory and the value of `{trial_dir}`. Plan, launch, validation and hook
evidence live next to that workspace, so ordinary workload output cannot
overwrite simtune's records by name. The exact argv, cwd, environment overrides,
policy actions and state are durably written before the first subprocess.

`TrialExecutionOptions(workspace_mode="fixed_cwd", fixed_cwd=...)` is the
explicit compatibility path for trusted legacy programs that require a fixed
working directory. `{trial_dir}` still names a unique output workspace, while
the workload cwd uses the fixed directory. The record carries a collision
warning, and a host-wide nonblocking lock rejects concurrent use of the same
fixed directory. This mode cannot prevent an undeclared legacy output from
colliding, so isolated mode is the default.

Setup and cleanup hooks are opt-in `HookSpec` argv tuples. They run without a
shell, with the isolated workspace as cwd and in `SIMTUNE_TRIAL_DIR`, and with a
bounded timeout. They are trusted commands, not a filesystem sandbox. simtune
itself never deletes an undeclared path and rechecks every path it writes for
traversal or symlink escape. A setup failure prevents workload launch. Cleanup
is skipped after a workload or scientific-validation failure so diagnostic
artifacts remain available; its separate allowance is recorded even if cleanup
makes total elapsed work exceed the main authorization.

## Scientific checks

`simtune.checks.run_checks()` implements three explicit check types:

- `file_hash`: an exact streamed SHA-256 of a confined output file;
- `numeric_json`: a finite, non-boolean JSON number at an object-key path,
  compared with declared absolute and relative tolerances;
- `command`: one bounded argv-only validator launched in the trial workspace.

Missing outputs, malformed JSON, duplicate JSON keys, NaN/infinity, tolerance
misses, nonzero validator status and validator timeout all produce `failed`.
When no checks are declared the result is `unchecked`, even when the workload
exits zero. Process success and scientific validity remain separate fields.

## Durable layout and recovery

An experiment directory uses this additive format; legacy schema-2 run
directories are unchanged.

```text
experiment.json                  atomic ExperimentRecord replacement
trials.jsonl                     append-only, flushed state transitions
completion.json                  written last after terminal records verify
trials/TRIAL_ID/
  trial.json                     atomic TrialRecord replacement
  plan.json                      resolved strict plan written before launch
  launch.json                    final argv, status, snapshot and limitations
  launch.txt                     captured stdout/stderr
  work/                          isolated workload outputs
  checks/CHECK_ID.json           one result per declared scientific check
  hooks/setup.json               present only when setup ran
  hooks/cleanup.json             present only when cleanup ran
```

The state transitions are intentionally small:

```text
not_started ──> running ──> succeeded | failed | timed_out | interrupted
      │              └────> unsupported | uncertain
      └───────────────────> unsupported | failed | interrupted | uncertain
```

A final journal fragment without a newline is ignored as an interrupted append.
A complete malformed line or invalid transition is corruption and fails load.
Recovery maps a last durable `running` state to `uncertain`; it never fabricates
success and never authorizes an automatic retry. A completion marker is refused
while any trial is not-started, running or uncertain, when a partial line exists,
when a terminal trial record is missing, or when its status disagrees with the
journal. `ExperimentRecord.outcome` remains `inconclusive` and
`selected_policy_id` remains null in this stage.

`BudgetController` uses an injectable monotonic clock. Discovery, setup,
warmup/workload, checks, confirmation and cleanup are charged separately.
Before each subprocess it caps the requested timeout to remaining authorization
and enforces the launch count. Training/warmup work cannot consume the declared
confirmation reserve. `TrialExecutor.run_ordered()` follows only the supplied
order and stops starting new trials when the trial/time budget is exhausted.

## Cancellation and log limits

Captured workloads, setup/cleanup hooks and command validators share the same
sequential main-thread execution path. Temporary SIGINT/SIGTERM/SIGHUP handlers
are installed before spawn and retained through durable evidence writes; previous
handlers are restored on exit. Handlers record the first signal only. The parent
forwards it to the new owned process group, gives the group 0.2 seconds to stop,
then sends SIGKILL to remaining group members even if the leader has exited.
Timeout uses immediate SIGKILL. If the final group signal fails, it is retried
for up to one second while reaping the leader; only a successful signal or an
observed absent group establishes teardown. Persistent errors remain unknown.
An already observed absent group is not signalled again. Pipe draining and direct-child reaping each have
separate one-second bounds; cancellation adds at most one polling interval
(0.05 seconds) before forwarding, excluding OS scheduling and spawn/fsync delays.
Cleanup may overrun the workload deadline by these separate allowances.

The CLI returns 130 for Ctrl-C and 128+signal for parent SIGTERM/SIGHUP (143/129).
Workload self-signals retain their raw negative return code and existing shell
status. Cancellation metadata records the parent signal separately from the
interrupted subprocess's actual return code, including a cooperative exit of 0.
An interrupted lifecycle cannot accept incomplete scientific validation or start
another workload/validator/cleanup command. Manual repeated runs stop after parent
cancellation and retain the completed iterations and partial output.

Known teardown produces `interrupted`; inability to establish owned-group teardown
and child reaping produces trial `unknown` / journal `uncertain`. Hooks and command
checks persist `running` intent before launch and terminal evidence afterward.
SIGKILL, power loss or a durable-write failure may leave only running intent:
recovery treats it as uncertain and refuses automatic resume/retry. Previously
completed scientific and process evidence remains in its original artifacts.

This is process-group cleanup for trusted programs. Descendants that deliberately
create another session/group are outside ownership and may survive; a retained
pipe descriptor stops draining during teardown after one second and is labeled `output_complete:
false`. Tests explicitly clean up escaped test processes. This is no process
sandbox or guarantee against deliberate escape.

Captured output is still buffered in memory in full during ordinary execution.
R02 does not provide bounded log memory or stream-to-file logging. Verbose,
long-running workloads need the separate bounded-log patch described in the
release plan before claiming support. Replay inherits stdin/stdout/stderr,
including binary data, and adds no capture, baseline, validator or retry.
