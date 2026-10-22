# R06 Linux study — pending target access

**Preparation only. No native measurements supplied or reviewed.** Based on
[the report template](../../../templates/benchmark-report.md). This is not a
published study and supplies no performance or support sentence for README.

## Question and status

- Proposed question: does fixed-resource CPU/OpenMP launch selection reduce
  process wall time for the unchanged checked heat workload on one Linux node?
- Date: 2026-10-05; investigator: Codex preparation session, no independent review.
- Target/authorization/allocation: unavailable; preparation host macOS 27.0.1 arm64.
- Automatic selection and independent manual comparison are specified in the
  [runbook](RUNBOOK.md); a draft [protocol](protocol-pending.json) is not frozen.
- Native functional gate, pilots, selection, confirmation and study: **not run**.
- Outcome: **not measured**, neither improvement nor no-improvement established.

## Reproduction and environment

Source/wheel identity instructions are in the runbook. Original R06 handoffs
and raw preparation logs are private historical evidence, unavailable in this
archive. This portable counterpart preserves the pending study status. Target CPU model/topology,
memory/affinity/allocation, kernel/virtualization/load, compiler/runtime linkage,
binary and validator identities remain pending. Local checks are regression
evidence, never native samples. Source archives/wheel are local, not public links.

## Protocol

One proposed single-session, two-CPU/two-thread timing-only study; different
training/held-out sizes; fixed steps selected from two retained sizing pilots;
paired selection and fresh confirmation with prespecified thresholds. A/A and
selected-policy comparisons use the existing manual harness. Equivalent direct
launch diagnostics use the same OS/runtime controls and minimal timing path.
Diagnostics never enter primary inference. Every failure/slow observation and
incomplete pair stays visible; no automatic retries or protocol shopping.

## Results

| Case / allocation | Baseline | Candidate | Valid / attempted pairs | Ratio | Interval | Checks |
| --- | --- | --- | --- | --- | --- | --- |
| Not measured | pending | pending | unavailable | unavailable | unavailable | native gate not run |

No numerical dataset exists. [Evidence status](evidence-status.json) uses null
native conclusions and explicitly names missing observations. Tuning cost,
validated savings and break-even are unavailable. No finite break-even is
established. Do not reinterpret an absent measurement as zero or no improvement.

## Interpretation and limits

Local synthetic-resource tests and actual macOS numerical/process tests do not
establish Linux acceptance. Preparation scripts have syntax/help/guard checks;
their native paths are unexecuted and require target review. This teaching heat
example cannot establish general scientific/HPC gains. NUMA, Slurm, counter
accuracy and repeatability remain separate gates.

## Publication review

- Independent reviewer/checkpoint: not assigned; not self-approved.
- Native site sharing/source/build/runtime access and redactions: pending.
- Original local preparation evidence retained outside the checkout; no native
  files, samples or host descriptions fabricated, and no public study curated.
- Sentence eligible for README: none.
