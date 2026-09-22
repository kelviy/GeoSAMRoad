#!/usr/bin/env bash
# Local end-to-end run for the high-res (2.5m, 512px) UNet+RGB SAM-Road model:
#   train -> threshold sweep on val -> inference on test.
# Graph metrics (APLS/TOPO) are deliberately not run here; they go on the HPC.
#
#   bash src/geosamroad/scripts/run_unet_rgb_highres.sh            # full run
#   STAGE=smoke bash src/geosamroad/scripts/run_unet_rgb_highres.sh # pipeline check only
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}"
DATASET_DIR="${DATASET_DIR:-/media/kelvin/FILES/ROSADataset}"
RUN_DIR="${RUN_DIR:-$REPO_DIR/runs/unet_rgb_highres}"
CONFIG="${CONFIG:-$REPO_DIR/src/geosamroad/configs/hpc/unet_rgb_highres.yaml}"
PYTHON="${PYTHON:-$REPO_DIR/.venv/bin/python}"

BATCH_SIZE="${BATCH_SIZE:-4}"
ACCUM="${ACCUM:-4}"
NUM_WORKERS="${NUM_WORKERS:-4}"
MAX_EPOCHS="${MAX_EPOCHS:-100}"
INFER_BATCH_SIZE="${INFER_BATCH_SIZE:-4}"
WANDB_MODE="${WANDB_MODE:-online}"
STAGE="${STAGE:-all}"

export PYTHONPATH="$REPO_DIR:$REPO_DIR/src:${PYTHONPATH:-}"
export PYTHONUNBUFFERED=1

CKPT_DIR="$RUN_DIR/checkpoints"
SWEEP_OUT="$RUN_DIR/val_thresholds.yaml"
mkdir -p "$CKPT_DIR"

common_sets=(
  --set DATASET_DIR="$DATASET_DIR"
  --set BATCH_SIZE="$BATCH_SIZE"
  --set ACCUMULATE_GRAD_BATCHES="$ACCUM"
  --set DATA_WORKER_NUM="$NUM_WORKERS"
)

if [ "$STAGE" = "smoke" ]; then
  echo "=== SMOKE: 1 train + 1 val batch on 4 tiles ==="
  "$PYTHON" -m geosamroad.train --config "$CONFIG" --accelerator gpu --devices 1 \
    --fast-dev-run "${common_sets[@]}" --set PRELOAD_GRAPHS=false \
    --set CHECKPOINT_DIR="$RUN_DIR/smoke"
  exit 0
fi

if [ "$STAGE" = "all" ] || [ "$STAGE" = "train" ]; then
  echo "=== TRAIN: ${MAX_EPOCHS} epochs, 512px @2.5m, bs=${BATCH_SIZE}x${ACCUM} ==="
  "$PYTHON" -m geosamroad.train --config "$CONFIG" --accelerator gpu --devices 1 \
    "${common_sets[@]}" \
    --set CHECKPOINT_DIR="$CKPT_DIR" \
    --set TRAIN_EPOCHS="$MAX_EPOCHS" \
    --set WANDB_MODE="$WANDB_MODE"
fi

# best checkpoint = the monitored one (road_iou), i.e. not last.ckpt
BEST_CKPT="${BEST_CKPT:-$(ls -t "$CKPT_DIR"/epoch*.ckpt 2>/dev/null | head -1)}"
[ -n "$BEST_CKPT" ] || { echo "ERROR: no epoch*.ckpt in $CKPT_DIR" >&2; exit 1; }
echo "Using checkpoint: $BEST_CKPT"

if [ "$STAGE" = "all" ] || [ "$STAGE" = "sweep" ]; then
  echo "=== SWEEP: ITSC/ROAD/TOPO thresholds on the val split ==="
  "$PYTHON" -m geosamroad.test --config "$CONFIG" --checkpoint "$BEST_CKPT" \
    --split val --accelerator gpu --devices 1 \
    "${common_sets[@]}" \
    --set PRETRAINED=false \
    --set THRESHOLD_SWEEP_OUT="$SWEEP_OUT" \
    | tee "$RUN_DIR/sweep_val.log"
fi

if [ "$STAGE" = "all" ] || [ "$STAGE" = "infer" ]; then
  [ -f "$SWEEP_OUT" ] || { echo "ERROR: no $SWEEP_OUT; run the sweep first" >&2; exit 1; }
  read_thr() { "$PYTHON" -c "import yaml,sys;print(yaml.safe_load(open('$SWEEP_OUT'))['$1'])"; }
  ITSC=$(read_thr ITSC_THRESHOLD); ROAD=$(read_thr ROAD_THRESHOLD); TOPO=$(read_thr TOPO_THRESHOLD)
  echo "=== INFER: test split with ITSC=$ITSC ROAD=$ROAD TOPO=$TOPO ==="
  "$PYTHON" -m geosamroad.inferencer --config "$CONFIG" --checkpoint "$BEST_CKPT" \
    --split test --output-dir "$RUN_DIR/infer_test" --device cuda \
    --set DATASET_DIR="$DATASET_DIR" \
    --set INFER_BATCH_SIZE="$INFER_BATCH_SIZE" \
    --set ITSC_THRESHOLD="$ITSC" \
    --set ROAD_THRESHOLD="$ROAD" \
    --set TOPO_THRESHOLD="$TOPO" \
    | tee "$RUN_DIR/infer_test.log"
fi

echo "=== DONE ==="
echo "  checkpoints: $CKPT_DIR"
echo "  thresholds:  $SWEEP_OUT"
echo "  graphs:      $RUN_DIR/infer_test/graph  (feed these to the HPC graph metrics)"
