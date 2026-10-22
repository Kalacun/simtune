# Implementation guide for the source distribution

This is the portable public counterpart of the checkout's implementation index.
Original stage prompts, private handoffs and raw execution evidence are retained
in the maintainer checkout and are unavailable in this archive. Their omission
is deliberate; these summaries are documentation, not substitute evidence.

Read [shared contracts](CONTRACTS.md) and [acceptance checks](ACCEPTANCE.md).
The implemented command boundaries are documented in [formats](../formats.md),
[search](../search.md), [recipes](../recipes.md) and [measurements](../measurements.md).
For current release limits see [the release guide](release-plan/README.md) and
[public status](release-plan/STATUS.md). Sequential integration and independent
review remain required. Plans alone establish no implementation or native result.

The unchanged trusted program, scientific parameters, precision and algorithms
remain outside launch tuning. Preserve legacy behavior and records, fail closed
for required strict settings, use bounded argv subprocesses and process-local
settings, retain failures and uncertainty, and run focused/full pytest and Ruff.
Python 3.11–3.14 and optional runtime extras remain supported package targets.
No root tuning, automatic job submission, favorable-result retries or inferred
speedups are authorized. See [AGENTS](../../AGENTS.md) for contributor rules.
