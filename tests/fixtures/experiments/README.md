# Stage-04 executable fixture

`workload.toml` is a complete strict manifest whose relative paths resolve from
this directory. `workload.py` is a harmless fixed-work executable and
`input.txt` is a declared input artifact. Their SHA-256 values are pinned in the
manifest, so editing either file intentionally makes loading fail until a new
fixture identity is reviewed.

The workload writes `result.json` below `{trial_dir}`. The numeric JSON check
expects the fixed value `6`. Tests also substitute an intentionally wrong
reference and require a failed scientific validation. No timing from this
fixture is used for candidate selection or a speedup claim.
