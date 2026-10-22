# simtune

**Measure how you launch a program. Keep the science unchanged.**

A scientific program can spend unnecessary time synchronizing threads, moving
between CPUs, or accessing remote memory. Rewriting the solver is not always the
right first step. simtune helps you inspect the machine, try explicit launch
settings, check the outputs, and retain evidence for the settings you reuse.

It runs your existing command. It does not modify its source, algorithm, inputs,
precision, or scientific parameters. There are no required Python runtime
dependencies, no daemon, and no privileged system tuning.

simtune is an **experimental, MIT-licensed CLI**, supporting Python 3.11–3.14.
Manual profiling works on Linux and macOS. Strict automatic search requires
Linux capabilities. Windows native execution is unsupported; WSL is treated as
Linux with capability limits.

## Why use it?

- **Understand the launch:** see which settings were requested, applied, skipped,
  or unsupported, along with timing and available resource measurements.
- **Compare unchanged work:** keep individual trials, failed runs and slow samples.
  Failed commands cannot become apparent speedups.
- **Test launch hypotheses:** use a manual profile for environment variables,
  process limits, CPU affinity or optional NUMA controls.
- **Search conservatively on Linux:** declare a checked workload, run bounded
  paired trials at fixed resource counts, and confirm candidates on held-out inputs.
- **Reuse with context:** inspect recipes, verify compatibility, explicitly
  revalidate them, and replay a command once without repeating a search.

A successful launch is not proof of a performance improvement. The tool reports
`no_improvement`, `inconclusive`, unsupported settings and failed checks as useful
outcomes. It does not promise an optimal policy or a speedup on another machine.

## Install

From this source repository:

```bash
git clone https://github.com/Kalacun/simtune.git
cd simtune
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
simtune doctor
```

Optional extras are `yaml` for custom profiles and `rich` for terminal formatting:

```bash
python -m pip install '.[yaml,rich]'
```

External tools such as `taskset`, `numactl` and `perf` are detected independently.
The package does not install them. The wheel contains the CLI and Python package;
the source repository and sdist also contain examples, tests and documentation.
There is no assumed PyPI release.

## Try a harmless command

```bash
simtune list-modes
simtune run --mode throughput --out runs --dry-run -- python examples/cpu_bound.py --iterations 20000000
simtune profile --out runs --repeat 3 --timeout 60 -- python examples/cpu_bound.py --iterations 20000000
simtune run --mode throughput --out runs --repeat 3 --timeout 60 -- python examples/cpu_bound.py --iterations 20000000
```

Use the two printed run paths with `simtune compare BASELINE_PATH CANDIDATE_PATH
--out reports`. Each run stores the argv, launch actions, individual measurements,
raw output and execution status. The built-in `throughput` profile is a hypothesis;
on some hosts it changes nothing useful. This short example is a CLI tutorial,
not a controlled performance experiment.

## A real-data example

The [handwritten-digits demonstration](docs/results/digits-20261022/README.md)
clusters 1,797 real UCI digit images using an unchanged scikit-learn program.
In the recorded Apple M2 session, a manual one-thread profile reduced mean
workload elapsed time by **24.0%** (2.096 s to 1.593 s). It compares the
inherited OpenMP thread setting with that profile,
checks every clustering result, and retains ten balanced pairs plus warmups.

```bash
python -m pip install '.[yaml]' -r examples/digits-requirements.txt
python examples/digits_benchmark.py --out benchmark-results/digits
```

The pinned scientific dependencies require Python 3.12 or newer.
The output directory must be new. Inspect `protocol.json`, `results.json`,
`summary.json` and each trial's original simtune run. To inspect the policy alone:

```bash
simtune run --profiles profiles/digits.yaml --mode one_thread --out runs --dry-run -- python examples/digits_workload.py
```

This example changes the thread budget through the manual interface. It is not
a result from the automatic tuner, which currently holds CPU and thread counts
fixed. The result report states the measured benefit and its limits.

## Checked automatic tuning on Linux

The [heat diffusion walkthrough](examples/HEAT_EXPERIMENT.md) builds a small
OpenMP teaching solver and generates a manifest with hashed code, executable,
runtime and input identities. Follow its build and manifest steps first. Then:

```bash
simtune tune --workload heat.toml --budget 10m --out results/heat --dry-run
simtune tune --workload heat.toml --budget 10m --out results/heat --save heat.recipe.json
simtune show results/heat
```

Inspect the outcome before using a recipe. Incomplete evidence may produce no
recipe; a training-only reference recipe is unvalidated. When a recipe exists:

```bash
simtune explain heat.recipe.json
simtune validate --recipe heat.recipe.json --workload heat.toml --budget 10m --out results/validation
```

The walkthrough shows how to inspect the derived validation recipe and construct
its exact replay argv. Replay executes once and does not automatically validate
that new output. On unsupported hosts, strict tuning records exclusions and
launches no workload. Tiny heat inputs test correctness, not performance.

## Commands and documentation

| Need | Commands / guide |
| --- | --- |
| Inspect the host and profiles | `doctor`, `list-modes`; [capabilities](docs/capability_matrix.md) |
| Measure or apply a manual policy | `profile`, `run`; [profiles](docs/profiles.md) |
| Compare stored manual runs | `compare`; [measurement methods](docs/benchmarks.md) |
| Search a checked workload | `tune`; [experiments](docs/experiments.md), [search](docs/search.md) |
| Read results and inspect compatibility | `show`, `explain`; [decision reports](docs/decision-report.md) |
| Confirm or reuse a recipe | `validate`, `exec`; [recipes](docs/recipes.md) |
| Import external counters | `import-profile`; [measurements](docs/measurements.md) |
| All options and worked examples | [User guide](docs/user-guide.md) |

Manual `run` retains its historical best-effort fallback behavior. Strict search
and replay reject missing required settings. `auto` is a legacy heuristic: it
performs one baseline plus the requested repeats, not a measured policy search.
Every command following `--` is passed as argv, without an implicit shell.

## Status and limitations

See the [current checks and release scope](docs/candidate-review.md) and
[measured results](docs/results/README.md). Unit tests use synthetic hosts;
Linux tests, physical NUMA observations and performance studies are separate
forms of evidence. The [verified 25-job CI run](https://github.com/Kalacun/simtune/actions/runs/39763864452)
covers source and installed packages on Linux/macOS with Python 3.11–3.14;
the check report explains optional-dependency and compiler-related skips.

- Strict Linux search is experimental. Physical NUMA placement, real perf counters
  and Slurm integration have no validated result in this release.
- Slurm adapters, ensembles, automatic thread-count search, MPI/multi-node tuning,
  GPU locality and energy objectives are deferred. Existing scheduler scripts
  are examples, not a scheduler integration.
- Workloads must be trusted and safe to repeat. Trial directories are not a
  security sandbox. Captured output is held in memory; keep commands quiet.
- Recipes pin declared artifacts, not every dynamic dependency or external input.
  Compatibility does not guarantee current correctness or performance transfer.
- Legacy run records remain readable. Historical failed-launch ratios are
  explicitly rejected as speedup evidence.

## Contribute and test

```bash
python -m pip install '.[dev,yaml,rich]'
python -m pytest -ra
python -m ruff check .
python -m ruff format --check .
python -m build
python tests/check_archives.py dist .
```

Set `SIMTUNE_OPENMP_CC` to an existing GCC/OpenMP compiler to require the numerical
example tests. Linux-only tests skip on macOS. The archive audit checks selected
files, ownership metadata, source identities, links and package contents.
See [contributing](CONTRIBUTING.md) and [the distribution policy](distribution/README.md).

[MIT license](LICENSE).
