# Study title — replace before publication

**Template only: no measurements have been supplied.** Delete this notice only
after completing the fields from real reviewed evidence. Do not replace missing
data with zero or invented hardware descriptions.

## Question and status

- Study ID/date and investigator:
- Manual-policy comparison or implemented automatic-tuner evaluation:
- Hypothesis and minimum practical effect declared before confirmation:
- Selection versus confirmation datasets/allocations:
- Outcome: improvement / no improvement / inconclusive / failed:

## Reproduction and environment

- Repository commit or source archive/hash manifest; dirty changes recorded:
- Workload source/binary hashes, build command, compiler/runtime versions:
- simtune/Python versions and optional collector/tool versions:
- CPU model, sockets, physical cores, SMT, NUMA nodes and distances:
- Actual task affinity, permitted memory nodes, allocation and memory caps:
- OS/kernel, container/VM/WSL status, shared/exclusive node, power policy:
- Scheduler job-step settings and relevant environment (reviewed/redacted):
- Fixed inputs/size/steps/wave/initialization, threads and numerical check:
- Exact commands and original evidence location:

## Protocol

- Baseline and candidate policies; requested versus observed effects:
- Primary metric and scope; separately labeled secondary metrics:
- Warmups, repetitions, randomized/alternating order, seed and timeouts:
- Independent allocation blocks and held-out cases:
- Failure/outlier/missing-data policy and any deviations:
- Selection rule, uncertainty method, threshold and limitations:
- For automatic tuning: search budget/candidates, validation reserve and cost:

## Results

Fill this table from actual samples; add rows for failures and no-improvement
cases. Ratios use baseline/candidate; uncertainty must name the method and unit
of resampling. Do not combine incomparable metrics or different workloads.

| Case / allocation | Baseline policy | Candidate | Valid / attempted pairs | Observed ratio | Interval / method | Failures / checks |
| --- | --- | --- | --- | --- | --- | --- |
| Not measured | — | — | — | — | — | — |

- Link to curated raw samples, all exclusions and reproducible analysis:
- Scientific-output validation and effective-policy diagnostics:
- Tuning/replay overhead and conditional break-even, if actually measured:
- Independent confirmation result:

## Interpretation and limits

State the result for this workload/build/input/allocation class only. Explain
sharing, thermal/cache/runtime effects, uncertainty, unavailable counters and
selection/confirmation limitations. Distinguish manual settings from automated
selection and per-process from whole-job measurements. Make no universal claim.

## Publication review

- Source/license and site sharing permissions checked:
- Redactions made (original evidence retained locally):
- Independent reviewer, reviewed checkpoint and unresolved findings:
- Exact sentence eligible for the repository README, if any:
