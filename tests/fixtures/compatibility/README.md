# Existing-command compatibility baseline

`cli-help.json` captures all five existing commands and root help, with an
80-column terminal, before the stage-01 implementation. It is a golden fixture;
new planned commands must not appear as implemented features.

`schema2/` is synthetic partial-success comparison input. Its intentionally
stale aggregate must be recomputed from successful samples: mean 2 seconds,
one success and one failure, overall failed. It is not a performance claim.
The real reduced historical schema-1 failure fixture remains unchanged at
`../legacy_taskset_failure`.
