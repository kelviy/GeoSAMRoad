#!/bin/bash
#SBATCH --account=l40sugrd
#SBATCH --partition=l40s
#SBATCH --job-name="gsr"
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=4
#SBATCH --time=10:00:00
#SBATCH --mail-user=wxxkel001@myuct.ac.za
#SBATCH --mail-type=ALL
#SBATCH --output=/scratch/wxxkel001/logs/slurm-%j.out

set -euo pipefail

# ============================ CONFIG ============================
USER_NAME="${USER:-$(whoami)}"
REPO_DIR="${REPO_DIR:-/scratch/${USER_NAME}/InstaRoadPrototype}"
VENV_DIR="${VENV_DIR:-/scratch/${USER_NAME}/InstaRoadPrototype/.venv}"
DATASET_DIR="${DATASET_DIR:-/scratch/${USER_NAME}/data/ROSADataset}"
FINAL="${FINAL:-1}"
if [ "$FINAL" = "1" ]; then FINAL_FLAG="--final"; RUN_SUFFIX="_final"; else FINAL_FLAG=""; RUN_SUFFIX=""; fi
RUN_DIR="${RUN_DIR:-/scratch/${USER_NAME}/output/sam${RUN_SUFFIX}_geosamroad}"
SAM_CKPT_PATH="${SAM_CKPT_PATH:-/scratch/${USER_NAME}/pretrain_checkpoints/sam_vit_b_01ec64.pth}"

SEED="${SEED:-42}"
MAX_EPOCHS="${MAX_EPOCHS:-100}"
BATCH_SIZE="${BATCH_SIZE:-16}"
CROP_SIZE="${CROP_SIZE:-128}"
UPSCALE="${UPSCALE:-2}"
NUM_WORKERS="${NUM_WORKERS:-3}"
PRECISION="${PRECISION:-32}"
WANDB_MODE="${WANDB_MODE:-null}"
WANDB_PROJECT="${WANDB_PROJECT:-geosamroad}"
# ===========================================================================

CONFIG="$REPO_DIR/src/geosamroad/configs/hpc/sam.yaml"

source "$VENV_DIR/bin/activate"
export PYTHONPATH="$REPO_DIR:$REPO_DIR/src:${PYTHONPATH:-}"
export PYTHONUNBUFFERED=1
echo "python=$(which python)"

for f in "$CONFIG" "$SAM_CKPT_PATH"; do
  [ -e "$f" ] || { echo "ERROR: missing $f" >&2; exit 1; }
done
[ -d "$DATASET_DIR/splits" ] || { echo "ERROR: no splits/ in $DATASET_DIR" >&2; exit 1; }

mkdir -p "$RUN_DIR"
cd "$RUN_DIR"

echo "=== FIT (geosamroad sam, ${MAX_EPOCHS} epochs, bs=${BATCH_SIZE}, " \
     "crop=${CROP_SIZE}->$((CROP_SIZE * UPSCALE))px x${UPSCALE}) ==="

python -m geosamroad.train \
  --config "$CONFIG" \
  --accelerator gpu \
  --devices 1 \
  $FINAL_FLAG \
  --dataset-dir "$DATASET_DIR" \
  --sam-ckpt-path "$SAM_CKPT_PATH" \
  --set CHECKPOINT_DIR="$RUN_DIR/checkpoints" \
  --set BATCH_SIZE="$BATCH_SIZE" \
  --set CROP_SIZE="$CROP_SIZE" \
  --set UPSCALE="$UPSCALE" \
  --set DATA_WORKER_NUM="$NUM_WORKERS" \
  --set TRAIN_EPOCHS="$MAX_EPOCHS" \
  --set PRECISION="$PRECISION" \
  --set SEED="$SEED" \
  --set WANDB_MODE="$WANDB_MODE" \
  --set WANDB_PROJECT="$WANDB_PROJECT"

echo "=== DONE ===  checkpoints in $RUN_DIR/checkpoints"
