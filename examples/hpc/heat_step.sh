#!/usr/bin/env bash
# Run only within the single task created by heat.sbatch (or an equivalent step).
set -euo pipefail
: "${SLURM_JOB_ID:?This example requires a Slurm allocation}"
: "${SLURM_STEP_ID:?Run inside the compute-node srun step}"
: "${SLURM_CPUS_PER_TASK:?Declare CPUs per task}"
if [[ "$SLURM_STEP_ID" == batch || "$SLURM_STEP_ID" == extern ]]; then
  echo "Run this script in the single-task srun step, not the batch shell" >&2
  exit 2
fi

heat_output=${1:?Pass a new output directory}
heat_python="${HEAT_PYTHON:-$PWD/.venv/bin/python}"
heat_cc="${HEAT_CC:-gcc}"
heat_threads="${HEAT_THREADS:-$SLURM_CPUS_PER_TASK}"
heat_size="${HEAT_SIZE:-2048}"
heat_steps="${HEAT_STEPS:-200}"
heat_pairs="${HEAT_PAIRS:-10}"
heat_mode="${HEAT_MODE:-inherited}"
heat_seed="${HEAT_SEED:-20260912}"

# The first default run is an inherited-versus-inherited control.
# Increase work only after a pilot, and choose candidates explicitly.
mkdir -p "$(dirname "$heat_output")"
mkdir "$heat_output"
mkdir "$heat_output/build"
"$heat_cc" --version > "$heat_output/build/compiler.txt"
"$heat_cc" -O3 -std=c11 -fopenmp examples/heat2d.c -lm -o "$heat_output/build/heat2d"
cat > "$heat_output/build/build-info.txt" <<'INFO'
Build flags: -O3 -std=c11 -fopenmp examples/heat2d.c -lm
Compiler identity: compiler.txt in this build directory.
No fast-math or candidate-specific rebuild. The same binary is used in every arm.
INFO

"$heat_python" -m simtune.cli doctor > "$heat_output/doctor.txt"
uname -a > "$heat_output/uname.txt"
if command -v lscpu >/dev/null 2>&1; then
  lscpu --json > "$heat_output/lscpu.json" 2> "$heat_output/lscpu.stderr" || true
fi
if command -v numactl >/dev/null 2>&1; then
  numactl --hardware > "$heat_output/numa-hardware.txt" 2>&1 || true
  numactl --show > "$heat_output/numa-inherited-policy.txt" 2>&1 || true
fi

"$heat_python" - "$heat_output" <<'PY'
import hashlib
import json
import os
import platform
import sys
from pathlib import Path

output = Path(sys.argv[1])
files = sorted(Path('simtune').rglob('*.py')) + [
    Path('pyproject.toml'), Path('examples/heat2d.c'), Path('examples/heat_benchmark.py'),
    Path('profiles/heat-experiment.yaml'), Path('examples/hpc/heat.sbatch'),
    Path('examples/hpc/heat_step.sh'),
]
hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
for path in files:
    snapshot = output / 'source-snapshot' / path
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(path.read_bytes())
context = {
    'python': sys.version, 'platform': platform.platform(),
    'affinity': sorted(os.sched_getaffinity(0)), 'source_sha256': hashes,
    'scheduler': {key: os.environ[key] for key in (
        'SLURM_JOB_ID', 'SLURM_STEP_ID', 'SLURM_JOB_NODELIST', 'SLURM_JOB_NUM_NODES',
        'SLURM_CPUS_PER_TASK', 'SLURM_NTASKS', 'SLURM_MEM_PER_NODE',
        'SLURM_CPU_BIND_TYPE', 'SLURM_CPU_BIND_LIST'
    ) if key in os.environ},
}
(output / 'step-context.json').write_text(json.dumps(context, indent=2) + '\n')
for name in ('status', 'cgroup', 'mountinfo'):
    path = Path('/proc/self') / name
    if path.exists():
        (output / f'proc-{name}.txt').write_text(path.read_text())
memory = Path('/proc/meminfo')
if memory.exists():
    (output / 'meminfo.txt').write_text(memory.read_text())
PY

# Diagnostic output is collected separately; these are not timing samples.
OMP_NUM_THREADS="$heat_threads" OMP_DYNAMIC=FALSE OMP_DISPLAY_ENV=VERBOSE \
  OMP_DISPLAY_AFFINITY=TRUE "$heat_output/build/heat2d" --size 64 --steps 3 \
  > "$heat_output/runtime-diagnostic.txt" 2>&1

"$heat_python" -m simtune.cli run --profiles profiles/heat-experiment.yaml \
  --mode "$heat_mode" --out "$heat_output/dry-run-unused" --dry-run \
  -- "$heat_output/build/heat2d" --size "$heat_size" --steps "$heat_steps" \
  > "$heat_output/candidate-plan.txt"

"$heat_python" examples/heat_benchmark.py \
  --binary "$heat_output/build/heat2d" \
  --profiles profiles/heat-experiment.yaml --mode "$heat_mode" \
  --size "$heat_size" --steps "$heat_steps" --threads "$heat_threads" \
  --pairs "$heat_pairs" --warmups 1 --seed "$heat_seed" --timeout 120 \
  --build-info "$heat_output/build/build-info.txt" --out "$heat_output/trials"
