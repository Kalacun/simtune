# Synthetic extension format fixtures

These are format examples, not measured benchmarks. `/synthetic` executables do
not exist; zero SHA-256 digests stand in for identity hashes and are not evidence.
No workload, native topology probe, or HPC tool was run to create these fixtures.

`valid/` has one example for each of the eight independently versioned record
kinds. `workload.toml` encodes the same workload as `workload.json`; TOML omits
nullable fields and declares their reasons under `unavailable`. Empty maps are
optional on input and made explicit by serialization. The example's empty
scientific checks and held-out partition intentionally mean **unchecked** and
**inconclusive**, never a validated recipe.

`invalid/` demonstrates unsupported versions, boolean weights, unsafe IDs and
unknown keys. They must fail with `ValidationError`. Tests add nonfinite numbers,
missing fields, duplicate IDs/keys, symlink escapes and contradictory states.

The trial's `actions` are also a readable explanation contract: which setting
was requested, how and why it was planned, and what has actually been observed.
Run this from the checkout to see the synthetic explanation without execution:

```python
from pathlib import Path
from simtune.models import TrialRecord
trial = TrialRecord.from_json(
    Path("tests/fixtures/extension/valid/trial.json").read_text(encoding="utf-8")
)
print(trial.actions[0].describe())
```

All paths and measurements here are synthetic. Existing historical failure data
under `../legacy_taskset_failure` is preserved separately, unchanged.
