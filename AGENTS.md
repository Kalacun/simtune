# Working on simtune

simtune changes how trusted programs are launched. It does not optimize their
source, scientific parameters, precision, or algorithms. Preserve that boundary.

For the planned extension, start with [the implementation index](docs/implementation/README.md),
[shared contracts](docs/implementation/CONTRACTS.md), and the stage assigned by
the user. Read the actual relevant source and tests; plans are not evidence that
a feature already exists. Complete the assigned stage, not the entire roadmap.

- Preserve existing CLI semantics, legacy run readability and meaningful data.
- Keep core runtime dependencies optional/minimal and Python 3.11–3.14 support.
- Use argv lists, bounded subprocesses, explicit environment overrides and
  process-local settings. Never introduce shell interpolation or root tuning.
- New strict execution must reject lost required settings; current manual
  best-effort behavior remains compatible unless explicitly migrated.
- Unit tests must use synthetic hosts and harmless commands. Linux/NUMA/Slurm
  support claims require target-system evidence beyond mocked tests.
- Record unsupported, failed, interrupted, unchecked and inconclusive outcomes.
  Do not manufacture speedups, infer missing counters, or remove slow trials.
- Run focused regression tests and the full suite. Follow existing Ruff checks.
- Finish each implementation stage with its handoff record and actual check
  results; implementation completion and independent review are different states.
- Do not overwrite another agent's work. Sequential stages are the default;
  shared-file edits require integration before the next stage proceeds.

For planning-only or example/documentation tasks, make the requested artifacts
without marking implementation stages complete. User instructions define scope.
