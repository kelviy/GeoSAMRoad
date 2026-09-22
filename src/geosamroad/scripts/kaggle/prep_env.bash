#!/usr/bin/env bash
# Kaggle environment prep for the high-res SAM-Road training notebook.
#
#   bash src/geosamroad/scripts/kaggle/prep_env.bash
#
# Installs the geosamroad extra WITHOUT touching Kaggle's preinstalled
# torch/torchvision (replacing them silently breaks CUDA on the session's GPU),
# then resolves a single DATASET_DIR out of whatever is mounted at /kaggle/input
# and writes it to /kaggle/working/geosamroad_dataset_dir.txt.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)}"
PY="${PY:-python}"
SKIP_INSTALL="${SKIP_INSTALL:-0}"

cd "$REPO_DIR"
echo "=== repo: $REPO_DIR"

EXTRA="${EXTRA:-kaggle}"

if [ "$SKIP_INSTALL" != "1" ]; then
  # Pin the host image's own torch/torchvision/numpy. torch/torchvision because a
  # replacement wheel silently loses CUDA; numpy because the image ships numba
  # prebuilt against it -- let numpy float and pip has to rebuild numba from
  # source, which fails at "Getting requirements to build wheel".
  WORK_DIR="${WORK_DIR:-$([ -d /kaggle/working ] && echo /kaggle/working || echo /tmp)}"
  CONSTRAINTS="$WORK_DIR/host-constraints.txt"
  mkdir -p "$WORK_DIR"
  "$PY" - "$CONSTRAINTS" <<'EOF'
import sys
import numpy, torch, torchvision
pins = {
    "torch": torch.__version__.split("+")[0],
    "torchvision": torchvision.__version__.split("+")[0],
    "numpy": numpy.__version__,
}
with open(sys.argv[1], "w") as f:
    for name, version in pins.items():
        f.write(f"{name}=={version}\n")
print("pinning host versions:", ", ".join(f"{k}=={v}" for k, v in pins.items()))
EOF

  # pyproject points segment-anything at git via [tool.uv.sources], which plain
  # pip does not read -- install it first so the extra resolves it as satisfied.
  echo "=== installing git-sourced deps"
  "$PY" -m pip install -c "$CONSTRAINTS" \
    "segment-anything @ git+https://github.com/facebookresearch/segment-anything.git" 2>&1 | tail -3

  # The `kaggle` extra leaves out terratorch on purpose; unet/sgcn never import
  # it. Use EXTRA=geosamroad if you need the terramind backbone here.
  echo "=== installing .[$EXTRA] extra"
  "$PY" -m pip install -c "$CONSTRAINTS" -e ".[$EXTRA]" 2>&1 | tail -6

  "$PY" - <<'EOF'
import numpy, torch
print(f"torch {torch.__version__}  numpy {numpy.__version__}  "
      f"cuda_available={torch.cuda.is_available()}")
if not torch.cuda.is_available():
    raise SystemExit("ERROR: CUDA is gone after install -- a dependency replaced torch")
print(" device:", torch.cuda.get_device_name(0))
try:
    import numba
    print(f" numba {numba.__version__} imports cleanly")
except Exception as exc:                      # numpy/numba ABI mismatch
    raise SystemExit(f"ERROR: numba is broken after install ({exc}); "
                     f"the numpy pin did not hold")
import geosamroad, lightning, segmentation_models_pytorch, rasterio, geopandas
import igraph, rtree, tcod, cv2
print(" geosamroad imports OK")
EOF
fi

echo "=== resolving dataset"
"$PY" src/geosamroad/scripts/kaggle/resolve_dataset.py --require-highres

echo "=== prep done"
