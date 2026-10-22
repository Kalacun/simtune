# Public release preparation guide

This is a curated counterpart of the maintainer's release implementation packet.
The original task prompts and private execution handoffs remain in the checkout;
they are unavailable historical evidence in this distribution.

The local R01–R05 work covers evidence validity, cancellation, resource/budget
boundaries, reporting and the checked heat workflow. R07 identified five further
findings: native command ordering (F01), missing preparation resources (F02),
private information in archives (F03), interval wording (F04), and recipe export
reporting (F05). Local repairs are subject to independent review. Original
findings are retained privately; this guide does not replace their audit evidence.

Use [public status](STATUS.md), [contracts](../CONTRACTS.md),
[acceptance checks](../ACCEPTANCE.md), and the [checked heat workflow](../../../examples/HEAT_EXPERIMENT.md).
The [R06 preparation guide](R06-linux-evidence.md) leads to the complete local
kit and ordered commands. Native Linux behavior and a scientific study remain
not run. Physical NUMA, Slurm and native counters remain unverified. R08–R10
are deferred. Release acceptance and public reproducibility are not established.
No publication, version bump or target execution is implied by packaging.

For a source change, checkpoint externally, reproduce first, integrate
sequentially, run affected tests then the full suite, Ruff, build and actual
archive checks. Install the wheel in separate base and extras environments
outside the source tree with PYTHONPATH removed, and verify site-packages imports.
Record unavailable interpreters, failed/interrupted/inconclusive checks and
actual hosted CI separately. See [distribution policy](../../../distribution/README.md).
