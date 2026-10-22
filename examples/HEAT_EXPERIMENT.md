# Checked heat diffusion workflow

[`heat2d.c`](heat2d.c) is an original teaching kernel: a fixed-work,
double-precision five-point diffusion stencil, with a discrete eigenmode reference.
It is not a production physics solver. simtune changes its launch settings;
source, binary, precision, steps, wave and first-touch stay fixed across policies.
Tiny cases check implementation only and cannot demonstrate speedup.

## Build and smoke test

Use a checkout or unpacked source distribution; wheels install the CLI and typed
package, **not** sibling examples, profiles or docs. Install simtune first with
Python 3.11–3.14 (`python -m pip install .`). This checked route needs neither YAML
nor Rich. Building is explicit and separate from tuning. With a known GCC/OpenMP
compiler, from the source root:

```bash
command -v gcc
mkdir -p build
gcc --version > build/heat-build.txt
printf '%s\n' 'gcc -O3 -std=c11 -fopenmp examples/heat2d.c -lm -o build/heat2d' >> build/heat-build.txt
gcc -O3 -std=c11 -fopenmp examples/heat2d.c -lm -o build/heat2d
OMP_NUM_THREADS=2 OMP_DYNAMIC=FALSE build/heat2d --size 64 --steps 3
```

Check exit 0, `valid: true`, positive `openmp`, and `threads: 2`. On macOS, system
`gcc` may be Apple Clang without OpenMP. Use an **already installed** compiler
(e.g. a versioned GCC), record its actual version and use that same executable in
all build commands. No helper builds, downloads, installs tools or runs the solver.
A serial `cc -O3 -std=c11 examples/heat2d.c -lm -o build/heat2d-serial` remains a
numerical/legacy smoke route; the checked threaded manifest requires OpenMP.
Do not compare different builds as a simtune improvement.

## Generate a fresh checked manifest

Supply `HEAT_RUNTIME_VERSION` and `HEAT_RUNTIME_LIBRARY` from your actual build
configuration: the version of its OpenMP runtime and the existing absolute library
path linked/loaded by this binary. Do not copy a library path from another host.
For GCC, a bounded `gcc -print-file-name=libgomp.so` (or the platform's dylib name)
can assist a build audit; a returned bare filename does **not** establish a usable
library. Inspect the actual build/linkage at your site. The helper records these
values as user declarations, not automatically verified linkage/version facts.

```bash
: "${HEAT_RUNTIME_VERSION:?Set the runtime version from your build environment}"
: "${HEAT_RUNTIME_LIBRARY:?Set the existing library path from your build environment}"
python examples/make_heat_workload.py \
  --binary build/heat2d --cpus 2 --threads 2 \
  --training-size 64 --validation-size 80 --steps 3 --wave 1 \
  --first-touch parallel --runtime-name libgomp \
  --runtime-version "$HEAT_RUNTIME_VERSION" \
  --runtime-library "$HEAT_RUNTIME_LIBRARY" --build-record build/heat-build.txt \
  --out heat.toml
```

`--help` lists the exact required inputs. Relative inputs resolve from the helper's
invocation directory. Output must be a new `.toml` path with an existing parent;
missing library/version, invalid numbers, equal training/held-out sizes or an
existing output fails with exit 2. This creates inspectable absolute executable,
source, validator, interpreter and library identities with SHA-256 hashes. An
optional build log is also pinned. Source/binary correspondence remains declared;
a hash does not prove how a binary was compiled. Keep the same build for both arms.
Changed artifacts are rejected at load/launch; explicitly create a new manifest
and experiment after intentional changes. Never refresh an old experiment's hashes.

The only scientific substitution is the whole argument `{case.size}`; all other
scientific values are fixed. The output argument is the whole `{trial_dir}`.
Each case has an explicit validator argv containing its expected size/steps/wave/
first-touch/thread count (validator case placeholders are not supported).
[`check_heat_result.py`](check_heat_result.py) rejects malformed/duplicate/nonfinite
JSON, missing output, wrong schema/metadata/work count, wrong actual OpenMP team,
`valid` other than exactly true, negative/nonfinite error and max error > `1e-9`.
It refuses any self-reported tolerance different from that fixed reference. The
command-check engine records its invocation, result and budget cost with the trial.
The validator's file read is bounded to 64 KiB; strict trial stdout/stderr still
buffers the full log in memory. Keep workloads and validators quiet.

Both sizes share one resource-capped CPU/thread budget (`threads <= cpus`),
sufficient for the largest case's fixed team. The target resolves a deterministic
reference inside observed inherited affinity; the helper embeds no host CPU IDs.
CPU/thread counts never vary in search. Equivalent resolved masks are deduplicated.
Memory need is undeclared: `16*N*N` grid bytes omit runtime/process overhead and
are not a safe capacity bound. Strict NUMA candidates requiring a declared need
are excluded. Other NUMA proposals may be excluded for missing topology/tools or
capacity. Inspect the ledger; successful plans do not prove page residency.

## Tune, inspect and confirm

On an actual supported Linux node, with enough inherited CPUs:

```bash
simtune tune --workload heat.toml --budget 10m --out results/heat --dry-run
simtune tune --workload heat.toml --budget 10m --out results/heat --save heat.recipe.json
simtune show results/heat
simtune explain heat.recipe.json
```

Dry-run launches no solver, validator or discovery subprocess and creates no
evidence directory. Real tuning repeats the solver/check in isolated trial
workspaces; `--out` stores evidence. External side effects still repeat. All slow
trials, failures, exclusions and unchecked effects remain recorded. Resource limits
and missing required settings fail strictly; legacy `run` stays best-effort.
On macOS strict search is unsupported, returns inconclusive/exit 1 with exclusions,
and launches no solver. A recipe is emitted only if a supported selection exists;
if absent, inspect `show` and remedy the recorded capability/protocol issue first.

For a shorter implementation smoke, copy [`heat-smoke.toml`](heat-smoke.toml)
beside the generated `heat.toml`, then use
`simtune tune --config heat-smoke.toml --dry-run` and
`simtune tune --config heat-smoke.toml`. It requests one training pair, no warmups,
30 seconds and the existing five-pair held-out confirmation reserve. This still
may be inconclusive; it grants no extra time/counts. It is never speedup evidence.

`no_improvement` (exit 0) means a completed eligible decision retained the
reference; it is useful negative evidence. `inconclusive` (exit 1) means the
protocol could not establish a decision, for example unsupported settings, failed
checks or exhausted budget. Timeout is 124, configuration/incompatibility 2,
Ctrl-C 130, parent TERM/HUP 143/129. Failures retain their actual subprocess status.

Training can retain the reference **without running confirmation**. Its saved
recipe then remains historically unvalidated, even though training checks passed.
Do not add `--allow-unvalidated` as a routine workaround or invent a gain.
When appropriate, explicitly fund fresh confirmation of that recipe:

```bash
simtune validate --recipe heat.recipe.json --workload heat.toml --budget 10m --out results/validation
simtune show results/validation
simtune explain results/validation.recipe.json --workload heat.toml
```

Validation compares only the saved policy/reference using new training and at
least five fresh held-out pairs; no old timing enters the decision. It can return
checked no-improvement. Inspect the sibling derived recipe, including protocol,
science and hardware authorization; failure/incomplete evidence stays unvalidated.
Original recipe/evidence remain immutable. Mechanical compatibility and recorded
processor class also matter. A class match promises no speed or current output.

## Replay the generated argv

After inspection authorizes reuse, create an existing working/output directory.
This command derives argv **from the actual generated manifest**, so no placeholder
binary or stale scientific arguments are copied. With the Python environment that
has simtune installed, from the same directory as `heat.toml`:

```bash
mkdir -p replay/heat
python - <<'PY'
import subprocess
from pathlib import Path
from simtune.workloads import load_workload, expand_trial_argv
workload = load_workload('heat.toml')
cwd = Path('replay/heat').resolve(strict=True)
argv = expand_trial_argv(workload.spec, workload.case('heldout'), cwd)
print('Replay argv:', repr(argv))
subprocess.run(['simtune', 'exec', '--recipe', 'results/validation.recipe.json',
                '--cwd', str(cwd), '--', *argv], check=True, timeout=65)
PY
```

`--output-dir` equals the absolute `--cwd`, as `{trial_dir}` matching requires.
Replay runs exactly once with inherited streams, no fresh baseline or validator;
its current output is **unchecked**. Run the pinned validator explicitly afterward
when the current result needs checking (or use the fresh `validate` workflow).
An unvalidated/incompatible recipe fails before launch; review the explanation
and perform fresh validation. Reusing the same cwd overwrites this teaching
solver's result file; choose separate directories for separate experiments.

For larger studies generate a new manifest (for example sides 2048/4096 and
hundreds of fixed steps) only after checking the allocation's memory headroom and
wall time. Keep compiler flags, binary, runtime, fixed steps/wave/first-touch and
thread counts identical across policies. Follow the [HPC evidence protocol](../docs/hpc-testing.md)
and [R06 native gate](../docs/implementation/release-plan/R06-linux-evidence.md).
No local smoke, test fixture, CI configuration or negative walkthrough establishes
Linux/NUMA performance or a transferable speed gain.

## Inputs and numerical check

| Option | Default | Meaning |
| --- | --- | --- |
| `--size N` | 512 | Grid side including boundaries; 3–32768 |
| `--steps N` | 100 | Fixed number of updates; 1–1,000,000 |
| `--wave N` | 1 | Sine-mode index, positive and less than size−1 |
| `--first-touch parallel` | parallel | Initialize both arrays using static parallel row ownership |
| `--first-touch serial` | — | Initialize in one thread; a distinct workload condition |

`--output-dir DIR` optionally writes the same JSON line to an existing
`DIR/heat-result.json`; stdout/default behavior is retained. Write failure exits 1.

Threads are controlled by `OMP_NUM_THREADS`; `OMP_DYNAMIC=FALSE` prevents dynamic
team-size adjustment when the runtime honors it. Output reports actual kernel
team size. Runtime affinity controls such as `OMP_PLACES`/`OMP_PROC_BIND` are
launch settings; see the [LLVM OpenMP reference](https://openmp.llvm.org/design/Runtimes.html).

For grid side `N`, initial values are
`sin(pi * wave * i / (N-1)) * sin(pi * wave * j / (N-1))`, with exact zero boundary
values. With update coefficient `alpha = 0.2`, this is an eigenmode of the
**discrete** update. Each step multiplies it by
`lambda = 1 - 8 * alpha * sin(pi * wave / (2 * (N-1)))^2`.
The program checks the complete final grid against `lambda^steps` times the
initial mode, using a fixed maximum absolute error tolerance of `1e-9`.

This checks the implemented recurrence, not physical-model accuracy. Output
includes `valid`, `max_abs_error`, a serially accumulated checksum, actual threads,
OpenMP version macro, grid bytes, updates, initialization time and kernel time.
Numerical failure exits 1; invalid inputs exit 2. Tiny kernels can have zero
reported duration at timer resolution and are unsuitable for timing conclusions.

## Memory and timing

Two grids require `16 * N^2` bytes, plus a small shape vector and runtime/process
overhead. Stay comfortably below the allocation's memory limit; Linux allocation
success does not guarantee pages can later be faulted in without an OOM kill.

| Side N | Two-grid memory |
| --- | --- |
| 2048 | 64 MiB |
| 4096 | 256 MiB |
| 8192 | 1 GiB |
| 16384 | 4 GiB |

Large cases can exercise memory hierarchy and placement, but do not infer
measured bandwidth or remote accesses from elapsed time alone. A cache-resident
case may behave very differently. Parallel first-touch is the normal example;
serial first-touch is useful as a separately labeled mechanism experiment, not
a deliberately weakened baseline to advertise gains.

The primary manual comparison uses simtune process wall time, including launch,
initialization, kernel, validation and profiler wrappers. Internal `kernel_seconds`
is a secondary diagnostic. Never mix those timing scopes in a ratio.

## Manual/legacy comparison (independent route)

```bash
python examples/heat_benchmark.py --binary build/heat2d \
  --mode inherited --size 512 --steps 100 --threads 2 \
  --pairs 6 --out benchmark-results/heat-control

python examples/heat_benchmark.py --binary build/heat2d \
  --mode omp_close --size 512 --steps 100 --threads 2 \
  --pairs 6 --out benchmark-results/heat-close
```

Output directories must be new. The default profile file is
[`heat-experiment.yaml`](../profiles/heat-experiment.yaml). Candidates are
`inherited` (a no-action control), `omp_close`, `omp_spread`, and Linux-only
`numa_interleave`. None is a measured winner. Inspect plans and actual runtime
binding separately. Missing/skipped/adjusted actions and wrong actual thread
counts make this harness reject the experiment's summary ratio.

The harness randomizes baseline/candidate order within each pair using a saved
seed, retains warmups and failures, uses the same fixed work, and exports raw
simtune runs, a manifest, `trials.jsonl` and `summary.json`. It is a manual
single-candidate experiment, independent of the implemented strict tuner. It conservatively
rejects any skipped/adjusted action, including adjustments you might otherwise
permit manually. Planned action checks still do not prove effective OS policy.

`--include-direct` adds an untuned execution without simtune wrappers. Its
`launcher_elapsed_sec` is useful for inspecting end-to-end overhead. Direct-arm
times are **excluded from the primary baseline/candidate ratio** because their
measurement path differs. Retain binary/input/thread equality in all arms.

The summary's observed ratio is the geometric mean of paired baseline/candidate
wall ratios; above 1 favors the candidate. With at least five pairs it also
reports a seeded 2,000-resample percentile bootstrap interval. This assumes
exchangeable pairs within the session. It does not establish independence across
allocations or correct for choosing the best of several candidates. Confirm a
chosen policy in fresh trials before citing an improvement.

For clear local/HPC experiments and publication criteria, follow
[the testing protocol](../docs/hpc-testing.md). Larger benchmark runs are opt-in;
the examples do not imply that simtune will make this kernel faster.
