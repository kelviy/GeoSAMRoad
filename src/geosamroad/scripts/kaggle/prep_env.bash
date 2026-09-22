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

if [ "$SKIP_INSTALL" != "1" ]; then
  # Pin whatever CUDA build Kaggle already has, so nothing in the dependency
  # tree can drag in a different (CPU or mismatched-CUDA) wheel.
  CONSTRAINTS=/kaggle/working/torch-constraints.txt
  mkdir -p "$(dirname "$CONSTRAINTS")"
  "$PY" - "$CONSTRAINTS" <<'EOF'
import sys
import torch, torchvision
with open(sys.argv[1], "w") as f:
    f.write(f"torch=={torch.__version__.split('+')[0]}\n")
    f.write(f"torchvision=={torchvision.__version__.split('+')[0]}\n")
print("pinning", torch.__version__, torchvision.__version__)
EOF

  # pyproject points these two at git via [tool.uv.sources], which plain pip
  # does not read -- install them first so the extra resolves them as satisfied.
  echo "=== installing git-sourced deps"
  "$PY" -m pip install -q -c "$CONSTRAINTS" \
    "segment-anything @ git+https://github.com/facebookresearch/segment-anything.git" \
    "terratorch @ git+https://github.com/terrastackai/terratorch.git"

  echo "=== installing geosamroad extra"
  "$PY" -m pip install -q -c "$CONSTRAINTS" -e ".[geosamroad]"

  "$PY" - <<'EOF'
import torch
print(f"torch {torch.__version__}  cuda_available={torch.cuda.is_available()}")
if torch.cuda.is_available():
    print(" device:", torch.cuda.get_device_name(0))
else:
    raise SystemExit("ERROR: CUDA is gone after install -- a dependency replaced torch")
EOF
fi

echo "=== resolving dataset"
"$PY" src/geosamroad/scripts/kaggle/resolve_dataset.py --require-highres

echo "=== prep done"
