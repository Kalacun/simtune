# R06 — Native Linux preparation, execution pending

This portable guide replaces the checkout's internal R06 assignment in the
source archive. Original preparation handoffs and personal execution records
are private historical evidence and unavailable here. R06 is in progress,
not implemented or accepted. No authorized target dataset exists.

Follow the [authoritative runbook](r06/RUNBOOK.md) in order on an explicitly
authorized Linux compute node. It supplies dependency setup, installed-package
checks, diagnostics, cancellation/replay gates, two sizing pilots, protocol
freeze, A/A controls, checked study, equivalent launches and reconstruction.
The [protocol draft](r06/protocol-pending.json) is not frozen;
[evidence status](r06/evidence-status.json) and [pending report](r06/report-pending.md)
remain explicitly not run. All three Python helpers, tests/fixtures, examples,
profiles and developer archive audit are included locally in the sdist.
The wheel installs only simtune and its CLI; it does not install this kit.

Do not substitute synthetic tests for target evidence, submit jobs implicitly,
install compilers implicitly, change science or remove failures/slow samples.
NUMA, Slurm, counter accuracy, repeatability and public study review require
separate evidence. Missing or failed prerequisites stop dependent execution.
