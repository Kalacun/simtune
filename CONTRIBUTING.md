# Contributing

Keep changes small, explain the behavior being corrected, and add a regression
test for substantive bugs. Keep policy decisions visible and reproducible,
including when implementing automatic tuning.

For the extension, use [the implementation playbook](docs/implementation/README.md)
and [shared contracts](docs/implementation/CONTRACTS.md). Run stages in order,
record handoffs, and keep implementation completion separate from independent
review and native-platform evidence. The plans do not establish feature support.

## Local setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,yaml,rich]'
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m build
```

Ruff supplies a small error/import check and a consistent formatter. There is
no comprehensive static type-check pipeline; public annotations are helpful
interfaces, not a claim that dynamic capability/profile dictionaries are fully
modeled. Do not introduce a large dependency just to validate a small mapping.

The Python support target is CPython 3.11–3.14. GitHub Actions is configured to run tests without
optional extras on Linux/macOS, plus an extras/build job. When changing YAML,
rendering or packaging, test both base and extras installations and install the
wheel outside the source tree. Source distributions include docs, examples and
tests; wheels include the package and its built-ins. Configuration is not an
observed hosted CI pass; see the release status ledger.

## Tests and platform changes

Mock tool discovery and probe results for unit tests. Use `sys.executable` for
small subprocess fixtures. Never require `sudo`, perf access, NUMA hardware,
systemd controllers, notifications, or persistent system changes in tests.
Timeout tests should verify bounded cleanup rather than benchmark elapsed time.

For manual checks on a target Linux machine, run `doctor`, inspect a dry plan,
and launch a small fixed-work example. Confirm actual affinity and cgroup
settings independently when claiming their effect. Record host versions and
skips. macOS CI cannot validate Linux-only tools; mocked Linux tests cannot
prove a kernel controller's behavior. WSL currently has no dedicated CI job.

The optional C heat example is tested against an independent small-grid
recurrence and across OpenMP thread/initialization settings. Set
`SIMTUNE_OPENMP_CC` to an OpenMP compiler executable to require that check;
otherwise absent compiler support produces an explicit skip. No timing-speedup
assertions belong in CI. See [the local/HPC protocol](docs/hpc-testing.md) for
performance experiments and publication criteria.

Review changes to wrapper argv carefully: each tool has its own option parsing,
exit-code behavior and potential variable expansion. Never retry a failed
profiler invocation with the workload unless the user explicitly starts a new
run. Preserve failure diagnostics and historical readability.

## Reporting issues

Include the command, simtune/Python version, relevant doctor output, expected
behavior and actual exit status. A minimal reproducer with a harmless command
is best. Review arguments, environment overrides, usernames and output before
sharing run files. Do not post tokens or private workload data.

## Publication checklist

- Review the README and changelog against actual CLI help and behavior.
- Verify the MIT copyright attribution and contributor names with the owners.
- Add repository URLs to package metadata once the real GitHub URL is known.
- Check ignored files before the first commit; keep local runs and environments
  out of the repository. No Git remote or release is required for development.
- Use python distribution/prepare_public.py DESTINATION to build a fresh
  source tree from the explicit public policy. Never initialize Git in the
  private maintainer checkout. See [GitHub publishing](docs/github-publishing.md).
- Build fresh source archives and run the ownership/content audit. Old archives
  from before the R07-F06 repair still contain builder identity and must not be
  uploaded. This does not change the explicit Git file selection.
- Wait for hosted CI, then perform target-machine checks appropriate to claims.
- Only publish benchmark evidence with an experiment manifest; see
  [benchmark methodology](docs/benchmarks.md).
