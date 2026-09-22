"""Encoder variants, keyed by the name the UI shows.

Each variant names the samroad YAML its checkpoint was trained with (shipped in
``geosamroad/configs/hpc``) and the ``globalurbanmapper_gee`` band list its
encoder reads. Checkpoints live in ``<CHECKPOINT_ROOT>/<name>/*.ckpt``.
"""
from importlib.resources import files
from pathlib import Path

# name -> (config stem, band list, label, road IoU from the eval run)
VARIANTS = {
    "sgcn":           {"config": "sgcn",      "band_list": "s1s2",
                       "label": "SGCN",           "road_iou": 0.469},
    "unet":           {"config": "unet",      "band_list": "s1s2",
                       "label": "U-Net",          "road_iou": 0.441},
    "terramind_base": {"config": "terramind", "band_list": "s1s2",
                       "label": "TerraMind base", "road_iou": 0.429},
    "sam_rgb":        {"config": "sam",       "band_list": "rgb",
                       "label": "SAM (RGB)",      "road_iou": 0.427},
}

DEFAULT_VARIANT = "sgcn"   # best road IoU


def config_path(stem):
    """Filesystem path of a samroad YAML inside the installed geosamroad package."""
    path = Path(str(files("geosamroad") / "configs" / "hpc" / f"{stem}.yaml"))
    if not path.exists():
        raise FileNotFoundError(f"geosamroad ships no config {stem!r} at {path}")
    return path


def get_variant(name):
    """Variant record plus its resolved config path.

    Raises:
        KeyError: if ``name`` is not a known variant.
    """
    if name not in VARIANTS:
        raise KeyError(f"Unknown variant {name!r}, expected one of {sorted(VARIANTS)}")
    record = dict(VARIANTS[name])
    record["name"] = name
    record["config_path"] = config_path(record["config"])
    return record


def available(checkpoint_root):
    """Variants that actually have a checkpoint on disk, best-first.

    The UI lists only these, so a missing checkpoint shows up as an absent
    option rather than a failed job.
    """
    root = Path(checkpoint_root)
    out = []
    for name, rec in sorted(VARIANTS.items(), key=lambda kv: -kv[1]["road_iou"]):
        if sorted((root / name).glob("*.ckpt")):
            out.append({"name": name, "label": rec["label"],
                        "band_list": rec["band_list"], "road_iou": rec["road_iou"]})
    return out
