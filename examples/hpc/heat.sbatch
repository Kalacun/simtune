#!/usr/bin/env bash
# Example only: review account/partition/resources and site binding rules first.
# Submit from the repository root after installing simtune in .venv.
#SBATCH --job-name=simtune-heat
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=4G
#SBATCH --time=00:15:00
#SBATCH --output=simtune-heat-%j.log

set -euo pipefail
cd "${SLURM_SUBMIT_DIR:?Submit from the simtune checkout}"
test -x .venv/bin/python
test -f examples/hpc/heat_step.sh

# All measurements/discovery run INSIDE this step, not around the srun launcher.
# Adjust this explicit cores binding if your site requires another convention.
# Never remove inherited binding just to expose more machine CPUs.
srun --nodes=1 --ntasks=1 --cpus-per-task="${SLURM_CPUS_PER_TASK:?}" \
  --cpu-bind=verbose,cores \
  bash examples/hpc/heat_step.sh "benchmark-results/heat-${SLURM_JOB_ID:?}"
