# Recipes, explanation and replay

Stage 06 adds `tune --save`, `explain`, `exec` and `validate`. They use the
existing version-1 records, strict launcher and isolated trial engine. Legacy
manual `run`, its best-effort fallback and `auto`'s N+1 launches are unchanged.
The [checked heat walkthrough](../examples/HEAT_EXPERIMENT.md) supplies a fresh
hashed manifest and generated replay argv with an existing absolute cwd.
Native Linux/NUMA/Slurm validation is still pending; synthetic test hosts and
local harmless processes establish software behavior only.

```bash
simtune tune --workload experiment.toml --budget 10m --out results/search-01 --save results/recipe.json
simtune explain results/recipe.json
simtune explain results/recipe.json --json --workload experiment.toml
simtune exec --recipe results/recipe.json -- PROGRAM ARGUMENTS
simtune validate --recipe results/recipe.json --workload experiment.toml --budget 10m --out results/validation-02
```

Use real argv elements for `PROGRAM ARGUMENTS`, matching a declared case. A
manifest containing `{trial_dir}` expects the replay argument at that position
to equal the absolute replay working directory. `exec --cwd DIR` selects that
existing directory; otherwise it is the current directory. Replay creates no
trial workspace, invokes no setup/cleanup hooks or validators, and inserts no
baseline. It inherits stdin, stdout and stderr directly (including binary data).
Recipes depending on setup/cleanup hooks require explicit fresh validation
without those hooks before single-launch replay. An explicit fixed-cwd source
requires that same working directory. Logs go to a new `--out DIR`, defaulting to `.simtune/replays/replay-ID`. The recipe
label and log path go to stderr, leaving workload stdout intact.

## Evidence and labels

Saving requires a completed supported experiment with a selected policy. An
incomplete/unsupported `tune --save` keeps the original outcome and reports that
no recipe was saved. `save_recipe(EXPERIMENT, NEW_FILE)` is also available to
Python callers. Put NEW_FILE outside the original experiment and use a new name.

`recipe.json.evidence/` contains independent copies of the experiment, journal,
completion marker, workload/resources, strict plans, trial records, launch results
and scientific check evidence. `recipe-identity.json` retains the exact workload
fingerprint payload. Relative references are confined through symlinks and
SHA-256 digests cover every copied artifact. Publication of `recipe.json` is
atomic and comes last. Existing recipe paths and source evidence are never
replaced. A failed export may leave a partial sidecar for diagnosis. Keep the
recipe and its sidecar together when moving them. Digests detect damage and
inconsistent records; these files are not signed proof against forged evidence.

Loading verifies the complete inventory, completion/journal linkage, source
fingerprints, launch metrics and successful check evidence. It recomputes the
paired scores and held-out rule and checks the recipe against the derived facts.
The recipe retains logical placement and the actual evidence CPU/node masks
separately. Saved absolute masks are never used as replay instructions.

Scientific checks, confirmation protocol validity, historical performance outcome, and replay validation are
separate facts. `explain` shows the selected policy and hypotheses, tool/settings,
requested/resolved/unobserved effects, per-case ratios and bootstrap uncertainty,
failed/excluded candidates, tuning cost, conditional break-even and limitations.
JSON includes those same facts. A retained reference/no-improvement recipe is
useful; it has no invented speedup. The selected policy is a tested choice,
not proof of global optimality.

A recipe needs successful science, independent eligible held-out confirmation,
complete declared runtime identity and complete relevant topology to have a
historical **validated** label. R04 additionally requires known matching recorded
and current processor-class evidence for override-free reuse. Otherwise `exec`
requires explicit revalidation or `--allow-unvalidated`. Stderr explicitly scopes
the label to historical evidence; JSON retains `label` with `label_scope` and
separate current compatibility/class/authorization/output fields. Earlier stage-05 experiments
without captured runtime controls remain readable/exportable but unvalidated.
The override cannot bypass compatibility, evidence integrity or required settings.
Every replay itself is scientifically **unchecked**, since it adds no validators.

Complete confirmation also requires independent scientific inputs and fresh
trials after training completes. Renaming the same inputs as held-out cases does
not satisfy that rule, even when both phases contain eligible samples. The
confirmation result exposes `protocol_valid` and `protocol_reasons`. A fully
checked independent comparison with no practical gain is valid evidence and may
authorize a retained-reference recipe; training alone cannot do so.

New recipes write stage-06 extension version 3 within the unchanged version-1
Recipe envelope. Older version-1/2 extensions are explicitly supported through
recomputation. Old recipes whose saved validation claims disagree with their
evidence are refused, including with `--allow-unvalidated`. `simtune explain`
and its `--json` form diagnose intact but inconsistent bundles with a **rejected**
label and exit status 2. JSON retains the original `stored_recipe` separately
from recomputed facts. Inspect the reasons and run `tune` with checked,
independent cases in a new directory to create new evidence. Source experiments
and recipe bytes remain unchanged; nothing is migrated or rerun automatically.

## Applicability before launch

Replay verifies the executable and fixed declared source/artifact hashes,
workload family, command structure, permitted whole-argument substitutions,
input classes, declared runtime name/version/identity, fixed environment and
CPU/thread/memory requirements. Unknown dynamic libraries, services, containers,
undeclared files and variables outside the documented runtime controls remain
untracked. Declare important runtime binaries/libraries as hashed artifacts;
runtime version/identity is user-declared metadata, not an automatic ABI probe.

Search and replay share strict memory feasibility: a declared need above any
known tighter allocation cap is refused even under inherited NUMA. Changed NUMA
policies also need observed selected-node capacity. Unknown allocation caps stay
unknown; a declared need establishes neither reserved memory nor an enforced RSS
limit. Strict CPU resolution requires observed inherited task affinity even when
optional cgroup data is unavailable.

Without `--workload`, argv must match a saved case; only that case's inputs and
all fixed artifacts are rehashed. New values require `exec --workload FILE` with
the same command template, family, runtime and fixed artifacts, permitted input
classes and explicit new case/input identities. Only declared case arguments and
the trial directory vary. Relative file arguments must resolve to their hashed
declared artifacts from the replay cwd; matching text cannot select another file.
Use `--case ID` for ambiguous identical argv with
different input identities. New inputs inherit conditional family evidence;
replay does not claim to check their output. Changed executable/source, runtime,
input class, fixed scientific arguments or resources require explicit validation.

The topology class encodes the eligible CPUs' socket/core/SMT/NUMA relationships,
full sibling counts, permitted memory nodes and relative NUMA distances. It
normalizes numeric IDs in sorted eligible order. Equal CPU counts, host names,
or unknown-to-unknown topology do not pass. New sparse IDs may map compatibly;
a narrower envelope, changed relationship or changed selected-mask relationship
fails. The normalization is conservative: some equivalent reordered layouts also
require validation. Processor identity is a separate versioned extension and
never guarantees that future performance matches historical results. See the
[decision-report and strict replay migration contract](decision-report.md) for
hardware provenance, unknown-class behavior and the scope of historical labels.

The strict planner resolves the logical policy inside current inherited/online/
permitted limits and verifies required tools and settings. Memory requirements
also need known adequate allowed-node capacity and all observed allocation caps.
Known inherited runtime controls (OpenMP/GOMP/KMP and common BLAS thread controls)
are captured in new experiments and must match after fixed/policy overrides.
Explicit CPU-ID controls (`GOMP_CPU_AFFINITY`, explicit KMP proclists and
nonlogical `OMP_PLACES`) are rejected rather than reused as logical placement.
No shell interpolation, root tuning or implicit tool installation is involved.
Identity and resources are rechecked immediately before the one strict launch.
A successful invocation still does not observe every thread's affinity or NUMA
page residency.

`--dry-run` on exec/validate creates no evidence directory and runs no workload,
validator, hook or capability probe. `explain --workload FILE` reports current
applicability failures in human and JSON output without launching. Replay uses
minimal execution, with neither time nor perf wrappers. `--timeout` can lower the
saved timeout, not enlarge it. Signals are forwarded to the workload process
group with a 0.2-second grace interval before SIGKILL of remaining owned members.
Raw negative signal status is retained; shell status is `128+signal` (Ctrl-C 130,
parent SIGTERM 143, SIGHUP 129). Metrics distinguish parent cancellation from a
workload self-signal and retain the subprocess's actual exit, including 0 after
cooperative cancellation. Teardown/pipe/reap bounds and escaped-group/log limits
are described in [experiments](experiments.md#cancellation-and-log-limits).
Timeout returns 124, incompatibility returns 2, and other workload statuses are
propagated. These launch APIs are sequential; future concurrent callers must
use separate processes.

## Explicit revalidation

`validate` loads a fresh manifest from the same declared family and tests only
the source logical policy against a newly resolved equal-resource reference.
It preserves its launch settings while taking runtime identity and resource
requirements from the supplied manifest. It changes no scientific parameters
within a case. A new family requires `tune`.

Fresh balanced training pairs and at least five independent held-out pairs per
case run through the existing wall-time/launch ledger, strict trial isolation,
scientific checks, journal and bootstrap rule. Complete scientific checks and
held-out cases are required. Confirmation is attempted even if training shows
no gain; it can validate an honest reference/no-improvement result. Failed science,
insufficient budget, unsupported settings, timeout or interruption remain visible.
No other candidate is searched and no old timing enters the new comparison.

`--out DIR` creates the new experiment. When a completed record has a supported
selection, `DIR.recipe.json` and `DIR.recipe.json.evidence/` are emitted beside
it, linked to the source recipe ID, digest, location and full original record.
Original recipes and evidence stay untouched, including after failure. A derived
recipe after failed/incomplete checks remains unvalidated. Validation returns
0 for confirmed improvement/no-improvement, 1 for inconclusive/failed validation,
2 for invalid configuration, 124 for timeout and 128+signal for parent cancellation (Ctrl-C 130). No resume
of uncertain work or automatic revalidation occurs during exec.
