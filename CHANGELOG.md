# Changelog

## Unreleased — experimental core integration

### GitHub finalization — 2026-10-22

- Normalize source-archive ownership, modes, extended metadata and timestamps;
  reject identifying headers in the audit, including directory entries.
- Repair curated-document fallback when preparing source from a public checkout.
- Preserve observed process-group disappearance during cancellation and bound
  retries of transient final-signal errors; persistent errors still stay uncertain.
- Add a reproducible real-data clustering example with paired manual profiles,
  output-equivalence checks and complete measurement records; no runtime dependency added.
- Rewrite the README around motivation, usable workflows, measured evidence and
  explicit experimental limits. Preserve all historical records and CLI behavior.

### Readiness candidate follow-up

- Reject unsupported archive link schemes before authority/empty-path exemptions;
  preserve web/mail links and archive-only relative-resource resolution.
- Include effective CLI-only collectors and CLI/default provenance in
  `tune --show-config`, with no discovery probes or workload launches.
- Add the complete eleven-command user guide and portable candidate review
  summary; correct current strict-launch, heat and review status wording.
- Select ignore rules and new public documents explicitly for a separate public
  source tree. Native acceptance, hosted CI, study and independent repair review
  remain pending. Version, license and legacy readers/evidence are unchanged.

### Scientific-workflow planning and test kit

- Add a staged P0–P2 implementation playbook with model/effort recommendations,
  copyable agent prompts, shared contracts, handoffs and independent review gates.
  The original packet was planning only; search/recipes/config/import now exist.
  Scheduler/ensemble features remain planned and native acceptance stays pending.
- Add an original C/OpenMP heat stencil with discrete-solution validation and
  a manual paired comparison harness retaining warmups, failures and provenance.
- Add candidate runtime/NUMA profiles, local/HPC reproduction guidance, Slurm
  templates and a public evidence template. No speedup or native HPC validation
  is claimed from these additions.
- Include C/shell/example documentation in source distributions and test small
  numerical/benchmark workflows without privileged tools.

### Checked heat workflow and public integration (R05)

- Add optional trial-local `heat-result.json` output, byte-identical to stdout;
  preserve numerical method, fixed tolerance/work and default output. Write
  failures exit nonzero. Keep the manual paired harness independent.
- Add a fresh hashed TOML manifest helper and pinned strict-JSON validator with
  fixed per-case metadata, actual OpenMP threads and reference error checks.
  Builds/runtime linkage remain explicit user declarations; nothing is built,
  installed, tuned or rehashed implicitly. Accept `libgomp` in the existing strict
  OpenMP adapter. No new search dimensions or speedup assertion.
- Reconcile public implemented/experimental/planned claims, source-only example
  distribution, replay cwd/argv and training-only negative recipes. Ship the tiny
  TOML project config in sdists; wheels deliberately contain the package only.
- Extend installed-wheel base/extras Python 3.11–3.14 Linux/macOS CI, new command
  and synthetic recipe boundaries, and real Linux strict/OpenMP smoke gates.
  CI configuration is not an observed hosted pass; target studies remain R06.

### Correctness and reliability

- Fix tune JSON reporting a requested recipe path when export was skipped.
  `recipe_path` is now null until export succeeds; additive `recipe_export`
  reports saved/skipped/not-requested and explicit skip reasons, including
  dry-run and preparation stops. Existing files, outcomes and replay gates are
  unchanged; no record migration or version change.
- Add R04's shared versioned decision report to tune/show/explain: paired case
  evidence, arithmetic seconds, correct ratio-to-time-reduction percentages,
  failures, settings, full cost and conditional break-even. Bare tuning warns
  before unchecked search. No statistical or candidate gate changes.
- Record conservative versioned Linux x86/ARM processor provenance separately
  from topology. New recipe extension v3 requires known matching classes for
  override-free reuse; older recipes stay inspectable and require explicit
  revalidation or the existing unvalidated override. Historical validation,
  current mechanical compatibility and unchecked replay output are distinct.
  No hidden baselines, source-evidence migration or hardware speed claim.

- Repair R02/F02 parent cancellation of captured workloads, hooks and validators.
  Forward SIGINT/SIGTERM/SIGHUP to the owned group, bound grace/drain/reaping,
  restore temporary handlers, retain partial evidence and distinguish parent
  signals from actual subprocess exits. Interrupted/uncertain work cannot retry
  on resume; replay keeps inherited binary streams and exactly one launch.
  Native Linux evidence remains pending; whole-output buffering remains a limit.

- Repair R03/F03–F05: strict CPU resolution requires observed inherited task
  affinity; every strict NUMA policy rejects declared memory above a known
  allocation cap. Search/replay share memory feasibility without treating a need
  as reserved memory or RSS enforcement. Perf preparation separately reserves
  each launch/time allowance at startup and resume, preserves confirmation
  reserves, and reports exhaustion/failure with no workload retry. Old ledgers
  retain totals and label missing historical probe counts; manual behavior is
  preserved.
- Preserve full confirmation protocol validity through recipe export, reload,
  explanation and replay (R01/F01). Reused scientific inputs or early
  confirmation cannot authorize validated replay merely because each phase is
  eligible. Complete checked no-improvement evidence remains valid. New recipe
  extensions use version 2; valid version-1 bundles remain supported. Old
  inconsistent claims are refused for execution and diagnosable with `explain`,
  without modifying source artifacts or changing the statistical rule.
- Correct taskset's separator placement, which previously attempted to execute
  `--` and produced failed throughput runs.
- Propagate workload failures to the CLI; preserve failed auto baselines and stop
  before tuned execution. Repeated runs retain every requested iteration.
- Remove stderr-triggered perf retries to avoid duplicate workload execution.
- Kill the launched process group on timeout and bound cleanup of inherited
  pipes. Capture launch/preexec failures and invalid output bytes safely.
- Separate profiler statistics from workload stderr, and leave per-run RSS
  unknown when only cumulative resource data is available.
- Handle inherited nice values, unlimited resource limits on Linux/macOS,
  scope-inherited process settings and macOS background taskpolicy correctly.
- Disable systemd argument expansion; incompatible systemd-run versions fall
  back instead of silently changing literal workload arguments.

### Intentional behavior changes

- Reject malformed actions, invalid repeats/timeouts, missing custom files,
  unknown modes and misspelled action fields before executing workloads.
- Add explicit repeatable `--profiles`, comparison `--out`, and `--version`.
- Built-ins are independent of optional YAML and checkout layout.
  `profiles/default.yaml` is an explicit reference file, not an implicit override.
- `throughput` resolves all currently available CPUs instead of assuming 0–3;
  `desktop_friendly` makes Linux-specific actions optional for macOS fallback.
  Priority increases may be adjusted to the inherited level and are logged.
- Auto dry runs disclose that the choice is provisional; the real auto baseline
  is stored and linked in the rationale. Preference flags require auto mode.
- Version 2 run metadata adds provenance and execution status. Aggregates use
  finite successful samples, include counts, and exclude status/iteration IDs.
  Comparison recomputes legacy aggregates and displays failures explicitly.
- The unused `psutil` extra and dependency-dependent `jsonschema` validation
  extra are removed. Core structural validation always runs; `yaml` and `rich`
  remain optional.
- CPU/mixed examples now use fixed work. The report example uses a common
  profiling path and suppresses ratios on failed or incomplete measurements.
  Old example results are not directly comparable with the revised source.

### Repository quality

- Expand regression tests, add Linux/macOS Python 3.11–3.14 CI, Ruff checks, and
  isolated wheel-install verification.
- Document architecture, schema, safety limits, tradeoffs, reproduction method
  and historical artifacts. Preserve local data while ignoring generated runs.
- Modernize package metadata and retain the existing MIT license attribution.

## 0.1.0 — original project snapshot

Existing CLI for capability inspection, baseline profiling, profile-based
execution, auto selection and stored-run comparison. A public release date or
Git history was not available in the supplied project.
