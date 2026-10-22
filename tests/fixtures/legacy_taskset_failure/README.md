# Historical failure fixture

This fixture keeps the first observed iteration from the local archive
`runs/run_20260115_184317_throughput`. It is **a failed launch, not a workload
performance measurement**. The original stderr says:

```text
taskset: failed to execute --: No such file or directory
```

The first iteration's elapsed time and exit code are copied unchanged. The
legacy aggregate is reduced to that one iteration. Most fields and machine
paths have been omitted; this is a comparison input, not a complete new run.
The fixture preserves the old schema's failure-averaging behavior so tests can
verify that comparison excludes it. The full archive remains local and ignored.

To inspect it without running a workload:

```bash
simtune compare tests/fixtures/legacy_taskset_failure --out reports/legacy-check
```

The result must show `status=failed`, one failure, and no successful wall-time
mean. It must not interpret the short elapsed time as improved throughput.
