#!/bin/bash
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=40
#SBATCH --time=12:00:00
#SBATCH --partition=gpu4,gpu3,gpu6,gpu2,gpu1,cpu1
#SBATCH --job-name=ANSYS
#SBATCH -o ./log/SLURM.%N.%j.out
#SBATCH -e ./log/SLURM.%N.%j.err

set -euo pipefail

mkdir -p ./log ./simul_log ./simulation

module purge

source ~/miniconda3/etc/profile.d/conda.sh
conda activate pyaedt2026v1

module load ansys-electronics/v252

export NUM_PROCESSES="${NUM_PROCESSES:-10}"
export CORES_PER_PROCESS="${CORES_PER_PROCESS:-4}"
export COUNT_PER_PROCESS="${COUNT_PER_PROCESS:-${LOOPS_PER_PROCESS:-1000}}"
export LOOPS_PER_PROCESS="${LOOPS_PER_PROCESS:-${COUNT_PER_PROCESS}}"
export TOTAL_COUNT="${TOTAL_COUNT:-0}"
export RESULT_CSV="${RESULT_CSV:-ipmsm_simulation_results.csv}"
export SIMULATION_DIR="${SIMULATION_DIR:-simulation}"
export STAGGER_SECONDS="${STAGGER_SECONDS:-30}"

export OMP_NUM_THREADS="${CORES_PER_PROCESS}"
export MKL_NUM_THREADS="${CORES_PER_PROCESS}"

echo "HOST=$(hostname)"
echo "SLURM_JOB_ID=${SLURM_JOB_ID:-}"
echo "NUM_PROCESSES=${NUM_PROCESSES}"
echo "CORES_PER_PROCESS=${CORES_PER_PROCESS}"
echo "COUNT_PER_PROCESS=${COUNT_PER_PROCESS}"
echo "LOOPS_PER_PROCESS=${LOOPS_PER_PROCESS}"
echo "TOTAL_COUNT=${TOTAL_COUNT}"
echo "RESULT_CSV=${RESULT_CSV}"
echo "SIMULATION_DIR=${SIMULATION_DIR}"

cmd=(
  python subprocess_run.py
  --processes "${NUM_PROCESSES}"
  --cores-per-process "${CORES_PER_PROCESS}"
  --count-per-process "${COUNT_PER_PROCESS}"
  --simulation-dir "${SIMULATION_DIR}"
  --result-csv "${RESULT_CSV}"
  --stagger-seconds "${STAGGER_SECONDS}"
  --analyze
  --non-graphical
  --cleanup-linux
)

if [[ "${TOTAL_COUNT}" != "0" ]]; then
  cmd+=(--total-count "${TOTAL_COUNT}")
fi

if [[ -n "${CASES_CSV:-}" ]]; then
  cmd+=(--cases "${CASES_CSV}")
fi

if [[ "${SETUP_ONLY:-0}" == "1" ]]; then
  cmd=("${cmd[@]/--analyze/--setup-only}")
fi

if [[ "${PERIODIC_BOUNDARY:-0}" == "1" ]]; then
  cmd+=(--periodic-boundary)
fi

if [[ "${KEEP_PROJECTS:-0}" == "1" ]]; then
  cmd=("${cmd[@]/--cleanup-linux/--keep-projects}")
fi

printf 'Running command:'
printf ' %q' "${cmd[@]}"
printf '\n'

srun --cpu-bind=cores "${cmd[@]}"
