# R06 target runbook — prepared, native execution pending

Prepared 2026-10-05 against integrated R01–R05. No authorized Linux endpoint or
allocation was supplied in this session. The preparation host is macOS 27.0.1
arm64; it is not the target. No Linux samples, native gate passes, performance
outcome, NUMA acceptance or Slurm acceptance are asserted.

Use one authorized Linux node and one already available GCC/libgomp build
environment. The target execution sequence creates no allocation, installs no
system tools, submits no job and changes no host settings. Separate Python
environment preparation is explicit below. Commands must execute in the approved compute
context, never on a cluster login node. Each failed or uncertain command ends
its dependent sequence. Keep its artifacts; no automatic workload retries.

## Checkpoint and prerequisites

The source distribution supplies this complete preparation kit at its documented
relative paths. This file is the authoritative command implementation, including
the corrected taskset argv from repair 02. The original private R06/R05 snapshots
and execution handoffs remain in the maintainer checkout; their personal paths
and raw evidence are unavailable here. No historical native result is implied.

Unpack the supplied sdist at a new absolute SOURCE_ROOT and preserve its SHA-256.
Build a wheel there with Python 3.11–3.14 and the declared build tools:
`python -m pip install build`, then `python -m build --outdir EXTERNAL_DIST`.
These are explicit operator setup steps, requiring approved package access or a
local package cache; do not install compilers or submit jobs. Audit with
`python tests/check_archives.py EXTERNAL_DIST SOURCE_ROOT`. Record actual wheel,
sdist and source inventory hashes. An unavailable backend stops preparation.

Create a separate virtual environment outside SOURCE_ROOT, then explicitly
install that exact wheel and developer prerequisites: `python -m pip install
WHEEL_PATH pytest ruff PyYAML`. Build tools are needed only for source packaging;
PyYAML is needed by the existing manual heat harness, not core simtune. Require
an existing approved GCC/libgomp and Linux taskset/ps; identify their actual paths
and versions. No implicit system installation is part of any target command.

Copy tests, examples, profiles, docs and distribution, plus the root .gitignore, LICENSE,
README.md, CHANGELOG.md, CONTRIBUTING.md, AGENTS.md and pyproject.toml from the
unpacked archive to a new external KIT **without a simtune/ package**.
These supply all tests/fixtures, archive-policy data, helpers and configs; keep
SOURCE_ROOT separately. Remove PYTHONPATH and verify installed simtune files
against the supplied wheel with zipfile/SHA-256. Version 0.1.0 alone does not
identify the checkpoint. Site-authored r06_site.py and site-notes.json are inputs
created below; generated output/recipe paths do not exist until their gate runs.

Fill these site values before any target workload. All paths below are absolute.
`SESSION` is a new directory under the site's approved `benchmark-results/`.
`KIT` is the external resource kit, not the source root. The runbook's scripts
are in `KIT/docs/implementation/release-plan/r06/`.

```python
# Save as r06_site.py inside the external kit; no command runs by this declaration.
from pathlib import Path
PY = Path('/SITE/existing-environment/bin/python')
CLI = PY.parent / 'simtune'
CC = Path('/SITE/existing-gcc')
LIB = Path('/SITE/actual-loaded-libgomp.so')
RUNTIME_VERSION = 'SITE verified libgomp/build version'
KIT = Path('/SITE/external-kit')
SESSION = Path('/SITE/benchmark-results/r06-session-001')
# Record endpoint/access authorization, approved allocation/time, shared/exclusive,
# VM/container/WSL status, known power policy, load and expected future case mix
# in a site-notes.json file. Unknown observations stay null with reasons.
```

Require Python 3.11–3.14; observed inherited affinity containing **more than two**
eligible logical CPUs; an approved two-CPU/two-thread working budget; at least
1 GiB documented available allocation headroom for the proposed 2048/3072 cases;
and sufficient approved time (up to one hour including bounded tests/build,
diagnostics, controls, 10-minute tuning and optional 10-minute negative
confirmation). A cap is not free memory: record actual available memory/load,
all known cgroup/ancestor caps and site allocation limits. If these prerequisites
are unavailable, stop or declare a separately labeled site-adjusted protocol
before any evaluation. No invented CPU IDs or guessed runtime library/version.

## Recording and target observations

Run Python from outside the source tree, remove `PYTHONPATH`, and assert
`simtune.__file__` is in site-packages. Use the supplied `record_command.record`
for each command. It calls the existing bounded executor once and retains argv,
cwd, explicit overrides, start/end, actual exit/timeout/cancellation, and
STDOUT/STDERR sections. Its whole-command elapsed time is a diagnostic, never
an additional primary sample. Existing workload/trial records own study timings.
Every directory must be new. A running intent without a result stays uncertain.

Execute the ordered snippets in one `PY` session from the external kit, or
assemble a reviewed site driver. Run the common preamble once. Metadata review
and protocol freezing are explicit operator steps; missing inputs stop execution.
Use arrays throughout, never execute formatted shell text.

The executable common preamble is:

```python
import os, sys, time
from pathlib import Path
from r06_site import PY, CLI, CC, LIB, RUNTIME_VERSION, KIT, SESSION
assert sys.platform == 'linux', 'native Linux required; no emulation label'
assert Path(sys.executable).resolve() == PY.resolve()
os.environ.pop('PYTHONPATH', None)
import simtune
assert 'site-packages' in Path(simtune.__file__).parts
sys.path.insert(0, str(KIT / 'docs/implementation/release-plan/r06'))
from record_command import record
SESSION.mkdir(parents=True, exist_ok=False)  # Once only; never reuse a session directory.
session_deadline = time.monotonic() + 3600  # Whole session ceiling; stop on exhaustion.
def run(name, argv, timeout, overrides=None, *, cwd=None):
    timeout = min(timeout, session_deadline - time.monotonic())
    assert timeout > 0, 'session budget exhausted; dependent gates remain pending'
    metrics, raw = record(argv, SESSION / 'commands' / name, timeout,
                          cwd=cwd or SESSION, overrides=overrides)
    assert metrics['return_code'] == 0, (name, metrics, raw)
    assert not metrics['timed_out']
    return metrics, raw
```

Create SESSION once, with `mkdir(exist_ok=False)`, and record the actual hostname
locally (review/redact before sharing), kernel, architecture and Python version.
Capture actual discovery with `search_snapshot()` (read-only kernel/sysfs/proc
observations, no synthetic providers), including `simtune.hardware` CPU identity,
affinity, memory nodes, caps, missing reasons/conflicts and scheduler hints.
Retain `/proc/self/status`, `/proc/loadavg`, `/proc/meminfo` and the relevant
CPU-info/topology provenance; do not copy credentials or the entire environment.
Optional `lscpu`/virtualization metadata commands need their own short timeouts;
errors/permissions denied are retained, unknown virtualization stays unknown.

Construct an isolated PATH containing symlinks to the **existing** `taskset` and
`ps` executables only. Resolve their original paths before changing PATH. Do not
link perf, numactl, systemd tools, hwloc or time. The manual harness and strict
path then both use elapsed/rusage timing without tool wrappers or counter probes.
This is process-local capability absence, not uninstallation or a claim that the
host lacks those tools. Keep original PATH private and record only the selected
tool paths/versions and reviewed runtime-variable allowlist.

```python
import json, shutil
from pathlib import Path
original_mask = sorted(os.sched_getaffinity(0))
assert len(original_mask) > 2
assert LIB.is_file() and CC.is_file()
bin_dir = SESSION / 'path-without-optional-tools'
bin_dir.mkdir()
for name in ('taskset', 'ps'):
    executable = shutil.which(name)
    assert executable, name
    (bin_dir / name).symlink_to(Path(executable).resolve())
from simtune.search import search_snapshot
(SESSION / 'resources-original.json').write_text(search_snapshot().to_json())
(SESSION / 'mask.json').write_text(json.dumps(original_mask[:2]))
# Apply the isolated PATH only after regression/compiler commands below.
```

Preserve site runtime settings. For this small study predeclare process overrides
`OMP_NUM_THREADS=2`, `OMP_DYNAMIC=FALSE`, `OMP_PLACES=cores`,
`OMP_PROC_BIND=FALSE`, `OMP_DISPLAY_ENV=FALSE`, `OMP_DISPLAY_AFFINITY=FALSE`.
Record any inherited GOMP/KMP/other OMP controls; if site controls conflict,
stop and document a reviewed alternative before evaluation. Runtime candidate
overrides change only `OMP_PROC_BIND` to close/spread. The reference's FALSE is
an explicitly declared runtime policy, never a throttled baseline.

## Local regressions on the target, then the native functional gate

Use the installed wheel and external kit for focused/full checks. Existing
synthetic Linux fixture tests remain labeled synthetic even on Linux. The
native test discovers its real kernel mask, but its library fixture is a
synthetic declared artifact; it alone does not establish runtime linkage.
Set the existing compiler explicitly so unavailable OpenMP is a failure.

```python
env = {'SIMTUNE_REQUIRE_INSTALLED': '1', 'SIMTUNE_OPENMP_CC': str(CC)}
focused = ['test_heat_example.py', 'test_heat_workflow.py', 'test_native_workflow.py',
           'test_installed_workflow.py', 'test_topology.py', 'test_strict_launch.py',
           'test_search_budgets.py', 'test_search_recovery.py', 'test_replay.py']
# Tests compile explicitly through CC; retain a normal approved compiler PATH here.
# Apply the isolated study PATH only after these regressions/build steps.
run('focused', [str(PY), '-m', 'pytest', *[str(KIT/'tests'/p) for p in focused],
    '-ra', '--basetemp', str(SESSION/'focused-tmp')], 900, env, cwd=KIT)
run('full', [str(PY), '-m', 'pytest', str(KIT/'tests'), '-ra',
    '--basetemp', str(SESSION/'full-tmp')], 900, env, cwd=KIT)
```

From the transferred source root, record `python -m ruff check .` and
`python -m ruff format --check .`; source checks have no installed-import claim.
Use existing build tools only for explicit `python -m build --no-isolation` and
`python tests/check_archives.py DIST SOURCE` if repackaging was necessary.
An absent backend stops that build; never install it implicitly. Any source
repair requires new checkpoint/wheel hashes and affected/full/Ruff checks before
native measurement. Do not substitute a previously failed gate's evidence.

Build heat once (30-second bound) with argv
`[CC, '-O3', '-std=c11', '-fopenmp', KIT/'examples/heat2d.c', '-lm', '-o', BINARY]`.
Record `CC --version` (5 seconds), exact flags, source/binary hashes and known
runtime linkage/library hash/version in `heat-build.txt`. Inspect linkage using
the site's bounded read-only method, for example `ldd BINARY` (5 seconds) for
this trusted binary; require the supplied LIB to agree with that observation.
Preserve failures; a compiler's bare `libgomp.so` filename is not verification.

```python
import hashlib
BINARY = SESSION / 'heat2d'
_, compiler_text = run('compiler-version', [str(CC), '--version'], 5)
build_argv = [str(CC), '-O3', '-std=c11', '-fopenmp', str(KIT/'examples/heat2d.c'),
              '-lm', '-o', str(BINARY)]
run('heat-build', build_argv, 30)
ldd = shutil.which('ldd')  # Resolve before isolating PATH; use only this trusted binary.
assert ldd, 'supply a documented bounded site linkage method instead; do not guess'
_, linkage = run('linkage', [ldd, str(BINARY)], 5)
assert str(LIB.resolve()) in linkage, 'independently review supplied runtime linkage'
(SESSION/'heat-build.txt').write_text(compiler_text + '\n' + repr(build_argv) + '\n' + linkage)
(SESSION/'build-hashes.json').write_text(json.dumps({str(p): hashlib.sha256(p.read_bytes()).hexdigest()
    for p in (BINARY, LIB, KIT/'examples/heat2d.c', KIT/'examples/check_heat_result.py')}))
os.environ['PATH'] = str(bin_dir)
assert shutil.which('perf') is None and shutil.which('numactl') is None
os.environ.update({'OMP_NUM_THREADS':'2', 'OMP_DYNAMIC':'FALSE', 'OMP_PLACES':'cores',
    'OMP_PROC_BIND':'FALSE', 'OMP_DISPLAY_ENV':'FALSE', 'OMP_DISPLAY_AFFINITY':'FALSE'})
run('doctor', [str(CLI), 'doctor'], 30)
```

Generate a fresh functional smoke manifest with the R05 helper:

```python
BINARY = SESSION / 'heat2d'
helper = KIT / 'examples/make_heat_workload.py'
def manifest(name, training, heldout, steps):
    run(name, [str(PY), str(helper), '--binary', str(BINARY), '--cpus', '2',
        '--threads', '2', '--training-size', str(training), '--validation-size', str(heldout),
        '--steps', str(steps), '--wave', '1', '--first-touch', 'parallel',
        '--runtime-name', 'libgomp', '--runtime-version', RUNTIME_VERSION,
        '--runtime-library', str(LIB), '--build-record', str(SESSION/'heat-build.txt'),
        '--out', str(SESSION/(name+'.toml'))], 10)
manifest('smoke', 64, 80, 3)
run('native-basic', [str(PY), str(KIT/'docs/implementation/release-plan/r06/native_gate.py'),
    '--workload', str(SESSION/'smoke.toml'), '--kit', str(KIT),
    '--out', str(SESSION/'functional-basic')], 180)
```

`native_gate.py` refuses non-Linux/source imports/optional-tool presence. It
observes the original real mask, narrows only its own process to its first two
allowed IDs and uses real snapshots for every strict launch. It runs child
affinity diagnostics and checked heat under every eligible CPU/runtime plan,
records a deliberately wrong expected reference's failure, and refuses a required
absent numactl plan with zero launches. No NUMA policy is executed. It reuses
the existing harmless readiness/liveness helpers for actual workload/validator
parent TERM cleanup, then parent SIGKILL/uncertain-resume refusal. Their scalar
output is a functional fixture, never heat science/performance evidence; their
host and elapsed-time providers are **not** used. All subprocesses are bounded.
SIGKILL's remaining child is explicitly killed after checking refusal, and
ordinary TERM liveness is asserted before unconditional helper cleanup.

Review each `*/heat.txt` separately for actual OpenMP worker placement; checking
the Python child or parent mask is insufficient. The GCC documentation specifies
that [OMP_DISPLAY_AFFINITY](https://gcc.gnu.org/onlinedocs/libgomp/OMP_005fDISPLAY_005fAFFINITY.html)
prints worker affinity when entering parallel regions. Missing/unsupported
diagnostics leave this gate pending; they are not evidence of placement. Keep
diagnostics out of study timings. Mark the native basic gate passed only after
that review and every script/test result, including skip reasons, is recorded.

Before evaluation, run a **functional-only** tiny tune and one explicit fresh
validation under the two-ID mask, never claiming their times as study data:

```python
taskset = str(bin_dir / 'taskset')
mask = ','.join(map(str, original_mask[:2]))
def restricted(argv):
    return [taskset, '-c', '--', mask, *map(str, argv)]
run('smoke-tune', restricted([CLI, 'tune', '--workload', SESSION/'smoke.toml',
    '--budget', '30s', '--repeat', '1', '--warmups', '0', '--seed', '2718',
    '--collectors', 'timing', '--out', SESSION/'smoke-search',
    '--save', SESSION/'smoke.recipe.json']), 40)
run('smoke-confirm', restricted([CLI, 'validate', '--recipe', SESSION/'smoke.recipe.json',
    '--workload', SESSION/'smoke.toml', '--budget', '60s',
    '--out', SESSION/'smoke-confirmation']), 70)
run('native-replay', [str(PY), str(KIT/'docs/implementation/release-plan/r06/native_gate.py'),
    '--workload', str(SESSION/'smoke.toml'), '--kit', str(KIT),
    '--recipe', str(SESSION/'smoke-confirmation.recipe.json'),
    '--out', str(SESSION/'functional-replay')], 40)
```

If tiny timing produces inconclusive/failed/no recipe, retain it and stop; do
not rerun until favorable. `native-replay` requires normal authorized reuse, one
real launch/no baseline/current output unchecked. It also rejects altered
declared binary/library identities and an actual one-CPU envelope before launch.
It never changes the original binary/library/evidence. Declare any gate repair
as a new functional checkpoint; it is not an automatic workload retry.

## Sizing pilot and protocol freeze

After all functional gates pass, use exactly two separately retained direct
sizing runs: size 2048 and 3072, 100 steps, wave 1, parallel first-touch, two
threads and the declared reference settings. Use 15-second command bounds,
separate existing output directories, the same binary and two-CPU mask. Check
each result with the pinned validator after the launch. Retain failures and stop
on any failure; do not automatically repeat a pilot. Pilot wall times are
diagnostics and never enter selection/confirmation statistics.

For this proposed protocol, choose common `steps = ceil(100*2/min(pilot_walls))`
(clamped to 1..1,000,000) to target at least about two seconds of fixed work.
Record both measured pilot wall times, formula and chosen value. This rule is a
sizing heuristic, not a promise about duration/overhead. If observed initialization
or startup dominates, or the estimate exceeds 60-second trial/10-minute budget
limits, stop and declare a new separately labeled sizing plan before evaluation.
No extra pilot or protocol change is triggered by the sign of a policy result.
Memory headroom must be assessed independently of this wall-time rule.

```python
import math
os.sched_setaffinity(0, set(original_mask[:2]))  # Process-local, observed permitted IDs.
pilot_walls = []
for size in (2048, 3072):
    cwd = SESSION / f'pilot-{size}'
    cwd.mkdir()
    metrics, _ = run(f'pilot-{size}', restricted([BINARY, '--size', str(size),
        '--steps', '100', '--wave', '1', '--first-touch', 'parallel',
        '--output-dir', cwd]), 15)
    run(f'pilot-check-{size}', [str(PY), str(KIT/'examples/check_heat_result.py'),
        '--trial-dir', str(cwd), '--size', str(size), '--steps', '100', '--wave', '1',
        '--first-touch', 'parallel', '--threads', '2'], 5)
    pilot_walls.append(metrics['wall_time_sec'])
assert all(math.isfinite(t) and t > 0 for t in pilot_walls)
STEPS = min(1000000, max(1, math.ceil(100*2/min(pilot_walls))))
(SESSION/'pilot-sizing.json').write_text(json.dumps({'wall_seconds':pilot_walls,
    'steps':STEPS, 'rule':'ceil(100*2/min(pilot_walls)), clamp 1..1000000'}))
```

Generate `study.toml` with `manifest('study', 2048, 3072, STEPS)`. Preserve source,
binary, compiler/flags, runtime/library, validator/interpreter hashes. Freeze
the actual JSON protocol using [protocol-pending.json](protocol-pending.json),
filling target identity, allowed mask/memory/headroom, STEPS, pilot references,
all runtime controls, candidate dry-run ledger/plans, source/wheel/build hashes,
case weights and the expected future case mix. Null required observations stop
evaluation. A pending draft is not a registered target protocol.

Record the timing-only tune dry-run with the exact study options below before
freezing. Within this deliberately narrowed two-CPU envelope, CPU-mask proposals
normally deduplicate; only baseline/runtime-close/runtime-spread should remain.
Verify this from actual plans. Require inherited NUMA for **every eligible** plan
and retain all excluded proposals/reasons. Unexpected eligible dimensions stop
the study before timings. Do not change candidate sets after seeing results.

```python
manifest('study', 2048, 3072, STEPS)
study_args = [CLI, 'tune', '--workload', SESSION/'study.toml', '--budget', '10m',
    '--repeat', '5', '--warmups', '1', '--seed', '2718', '--confirmation-pairs', '5',
    '--confirmation-fraction', '0.5', '--minimum-improvement', '0.02',
    '--max-trials', '100', '--max-launches', '200', '--collectors', 'timing',
    '--out', SESSION/'search', '--save', SESSION/'study.recipe.json', '--json']
run('study-dry', restricted([*study_args, '--dry-run']), 30)
# Fill protocol-pending.json as SESSION/protocol.json with actual observations.
# Inspect every dry-run plan/exclusion and allowed runtime setting first.
protocol_path = SESSION/'protocol.json'
protocol = json.loads(protocol_path.read_text())
assert protocol['state'] == 'frozen'
assert protocol['target'] is not None and protocol['steps'] == STEPS
required = ['target', 'source_archive_sha256', 'source_inventory_sha256', 'wheel_sha256',
    'binary_sha256', 'compiler_and_flags', 'runtime_identity_and_linkage',
    'validator_and_interpreter_sha256', 'actual_resource_snapshot_and_mask',
    'memory_headroom_and_allocation', 'runtime_environment_allowlist', 'pilots',
    'expected_future_case_mix', 'candidate_ledger_and_plans']
assert all(protocol[k] is not None for k in required), 'missing target protocol observations'
(SESSION/'protocol.sha256').write_text(hashlib.sha256(protocol_path.read_bytes()).hexdigest()+'\n')
```

Predeclare one session (single-session claim only), 5 training pairs per candidate,
1 retained warmup pair per candidate, seed 2718, 5 fresh held-out confirmation
pairs, 50% of the 600-second budget reserved, 100 trials/200 launches ceiling,
60-second manifest trial timeout, 5-second validator timeout, timing collector,
default 2,000-resample paired-bootstrap/95% interval and d=0.02/r=0. Retain seeded
balanced AB/BA order from the tuner's saved schedule. Improvement needs protocol
and scientific validity, aggregate interval lower bound > 1/(1-0.02) and every
held-out case lower bound >= 1/(1+0). No outlier deletion or early stopping for
a favorable result. Exhaustion/failures/partial pairs remain recorded.

## Evaluation and equivalent launches

Run the existing manual harness as an inherited/inherited A/A control first,
under the same two-ID mask/runtime/build/work. For **each** size, one warmup pair,
6 measured pairs, seed 1618, timeout 60, `--include-direct`. Its profile/run arms
must report `used_time=false` and `used_perf=false`. Direct-arm whole-launcher
elapsed is a separate overhead diagnostic, excluded from the primary paired
ratio. The harness needs already installed PyYAML. Do not introduce a new
benchmark/statistics harness or substitute its descriptive interval for tuning's
confirmation rule.

```python
for size in (2048, 3072):
    run(f'aa-{size}', restricted([PY, KIT/'examples/heat_benchmark.py',
        '--binary', BINARY, '--profiles', KIT/'profiles/heat-experiment.yaml',
        '--mode', 'inherited', '--size', str(size), '--steps', str(STEPS),
        '--wave', '1', '--first-touch', 'parallel', '--threads', '2',
        '--pairs', '6', '--warmups', '1', '--seed', '1618', '--timeout', '60',
        '--include-direct', '--build-info', SESSION/'heat-build.txt',
        '--out', SESSION/f'aa-{size}']), 1500)
# dry-run is recorded before freeze; this is the single authorized real tune.
run('study', restricted(study_args), 620)
run('show', [str(CLI), 'show', str(SESSION/'search'), '--json'], 15)
run('explain', [str(CLI), 'explain', str(SESSION/'study.recipe.json'), '--json'], 15)
```

A/A failures stop dependent study execution. Apparent A/A effects are retained
and interpreted as variability. Predeclare that if either valid A/A 95% interval
excludes 1, the study's performance interpretation is unstable/inconclusive,
regardless of the tuner's recorded decision. Still collect the fixed protocol;
never rerun a control for a favorable interval. Tune no-improvement/exit 0 is a sound
negative outcome, not permission to retune. Inconclusive/failed/timeout/interrupted
ends evaluation; record its status and do not resume automatically.

Predeclare this sole negative-confirmation branch: if the completed tune saved a
reference recipe but performed no held-out confirmation, fund **one** fresh
`validate --recipe study.recipe.json --workload study.toml --budget 10m
--out negative-confirmation`. Its existing defaults are repeat 3, warmups 1,
seed 0, confirmation 5, fraction 0.5, d=0.02, 2,000 bootstrap samples and manifest
caps. Record this branch in the frozen protocol, including 20 confirmation
workload/check launches and 18 total workload trials/36 launches under those
defaults. It confirms the retained reference; it cannot promote a different
candidate. Keep its samples/report separate from initial search. Failed or
incomplete validation stays unvalidated. No override is used to force replay.

```python
from simtune.recipes import load_recipe
recipe = SESSION/'study.recipe.json'
bundle = load_recipe(recipe)
if not bundle.details['heldout_confirmed']:
    assert bundle.recipe.policy.id == 'baseline', 'unconfirmed challenger is not the negative branch'
    run('negative-confirmation', restricted([CLI, 'validate', '--recipe', recipe,
        '--workload', SESSION/'study.toml', '--budget', '10m',
        '--out', SESSION/'negative-confirmation']), 620)
    recipe = SESSION/'negative-confirmation.recipe.json'
    bundle = load_recipe(recipe)
assert bundle.details['replay_validated'], 'leave confirmation pending; no override'
```

For equivalent direct OS/runtime launches, resolve the actual reference and
selected policies from the recorded experiment/recipe. Re-resolve on the actual
unchanged narrowed target snapshot; store each `plan.to_dict()`. For every case,
perform exactly one diagnostic strict minimal launch and one direct launch with
the **same** plan wrappers/env overrides, fixed environment, binary/science,
two-CPU mask, timeout and output-directory convention. Use the existing
`execute_strict_launch` and `profiler.execute_command` timing path for both.
No recipe baseline is inserted. These are launch-equivalence diagnostics,
excluded from the primary study/selection/confirmation samples:

```python
from simtune.launch import execute_strict_launch
from simtune.profiler import execute_command
from simtune.workloads import load_workload, expand_trial_argv
workload = load_workload(SESSION/'study.toml')
# 'plan' and 'caps' are actual re-resolved objects, never display-text parsing.
assert not plan.rlimits and plan.policy.numa_policy.value == 'inherited'
for case in workload.spec.cases:
    for arm in ('simtune-minimal', 'direct'):
        cwd = SESSION/'equivalence'/plan.policy.id/case.id/arm
        cwd.mkdir(parents=True, exist_ok=False)
        argv = expand_trial_argv(workload.spec, case, cwd)
        env = {**os.environ, **workload.spec.fixed_env, **dict(plan.env_overrides)}
        if arm == 'simtune-minimal':
            result = execute_strict_launch(plan, argv, caps, 60, cwd=cwd,
                snapshot_provider=search_snapshot, base_env=env)
            metrics, raw, final_argv = result.metrics, result.raw_output, list(result.final_argv)
        else:
            final_argv = [arg for wrapper in plan.wrappers for arg in wrapper] + argv
            metrics, raw = execute_command(final_argv, env, 60, cwd=cwd)
        # Persist argv, cwd, reviewed overrides, metrics and raw sections immediately.
        # Run the pinned checks afterward; check time is excluded in both arms.
```

The exact object resolution/persistence loop is supplied in
[equivalent_launches.py](equivalent_launches.py); run it once with `--workload`,
`--recipe`, `--out` under `restricted()`. It discovers the real snapshot,
re-resolves baseline and recipe selected policy and does not parse CLI output.
It asserts requested/observed child affinity in separate diagnostics, checks
all solver outputs and records even failed timed launches before stopping.
It draws no statistical overhead/improvement conclusion from one launch per arm.

```python
run('equivalence', restricted([PY, KIT/'docs/implementation/release-plan/r06/equivalent_launches.py',
    '--workload', SESSION/'study.toml', '--recipe', recipe,
    '--out', SESSION/'equivalence']), 600)
```

Additionally run the existing manual candidate comparator on **each declared
case**, 6 measured/1 warmup pairs, seed 3141, using the matching saved selected
mode (`runtime-close` -> `omp_close`, `runtime-spread` -> `omp_spread`, reference
-> `inherited`) with OMP_PLACES=cores already inherited. Same binary/science/
two-ID mask, timing path and timeout; retain the exact profiles. `--include-direct`
remains an untuned inherited arm and is **not** the equivalent selected-policy
arm when selected binding differs. That equivalent arm is the diagnostic above.
Other selected policies violate this declared protocol and must be reported,
not approximated. No manual result can override the automatic decision.

```python
selected_mode = {'baseline':'inherited', 'runtime-close':'omp_close',
                 'runtime-spread':'omp_spread'}[bundle.recipe.policy.id]
for size in (2048, 3072):
    run(f'manual-{size}', restricted([PY, KIT/'examples/heat_benchmark.py',
        '--binary', BINARY, '--profiles', KIT/'profiles/heat-experiment.yaml',
        '--mode', selected_mode, '--size', str(size), '--steps', str(STEPS),
        '--wave', '1', '--first-touch', 'parallel', '--threads', '2',
        '--pairs', '6', '--warmups', '1', '--seed', '3141', '--timeout', '60',
        '--include-direct', '--build-info', SESSION/'heat-build.txt',
        '--out', SESSION/f'manual-{size}']), 1500)
```

## Reconstruction, review and handoff

Retain the complete raw session under ignored benchmark-results: authorization
notes, checkpoints, build/linkage provenance, original/narrowed resource snapshots,
frozen protocol/hash, pilots, diagnostics, all command intents/results/raw logs,
manual manifests/trials/summaries, strict experiment/journal/trial files and
completion marker if present, recipes and their evidence bundles. Include every
failure, warmup, exclusion, slow sample, unchecked observation and incomplete pair.

Use installed `simtune show SEARCH --json` and `simtune explain RECIPE --json`
to recompute the decision through current domain functions. For manual records,
load **every** line of `trials.jsonl` and call the existing
`heat_benchmark.paired_summary(rows, manifest['pairs'], manifest['seed'])`;
compare to original summary without writing over it. Retain per-case/session
rows; do not merge manual/direct/strict/internal-kernel timings or distinct hosts.
Record total budget ledger cost, validation branch cost separately, and derive
saved seconds under the declared future case mix only when positive and eligible.
Otherwise break-even is null with reason/no finite break-even, never zero.

One session supports only a single-session bounded result. A repeatability claim
requires at least three separately identified sessions/allocations **declared
before evaluation**, each with its own gate/protocol/data/results. No polling for
a speedup and no pooling machine/session conditions into one interval.

After independent evidence/site sharing review, curate `docs/results/STUDY_ID/`
using [the existing report template](../../../templates/benchmark-report.md).
Provide complete machine-readable samples/failures, hashes, source/build/runtime
access, numerical checks, reconstruction commands/methods and redaction ledger.
Retain original private logs; explain absent artifacts/access limits and do not
claim full public reproduction if required artifacts are unavailable. A sound
no-improvement study passes the quality gate. Until data/review exist, keep
[report-pending.md](report-pending.md) as a preparation artifact here; publish no
numerical study or Linux support statement.

Update only R06 handoff/status with actual endpoint/kernel/mask/build/checkpoint,
exact commands/statuses, separate local/synthetic/native/study outcomes, protocol
deviations and remaining gates. Independent review is separate. NUMA, Slurm and
native counter accuracy remain not run even if this single-node CPU/runtime gate
passes; no root tuning, submission or installation is part of this runbook.
