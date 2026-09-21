import os
from datetime import datetime
import yaml
from addict import Dict


def _load_yaml_with_base(path):
    with open(path) as file:
        cfg = yaml.safe_load(file) or {}
    base_rel = cfg.pop("_BASE_", None)
    if base_rel:
        base_path = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(path)), base_rel)
        )
        base = _load_yaml_with_base(base_path)
        base.update(cfg)
        cfg = base
    return cfg


def load_config(path):
    return Dict(_load_yaml_with_base(path))


def _parse_override(raw):
    value = yaml.safe_load(raw)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def apply_overrides(config, assignments):
    """Apply (--set KEY=VALUE) overrides; values are yaml-parsed."""
    for item in assignments or []:
        if "=" not in item:
            raise ValueError(f"--set expects KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        config[key.strip()] = _parse_override(value)
    return config


COMPLETE_SEG_ENCODERS = ("unet", "sgcn")
ENCODER_MODELS = ("sam", "terramind") + COMPLETE_SEG_ENCODERS
TERRAMIND_VERSIONS = ("small", "base")


def finalize_config(config):
    """Derive dependent keys and sanity-checks"""
    if not config.DATASET_DIR:
        raise ValueError(
            "DATASET_DIR is not set. Point it at the ROSA root "
            "(--dataset-dir or --set DATASET_DIR=...)."
        )
    config.PATCH_SIZE = int(config.CROP_SIZE) * int(config.UPSCALE)

    encoder = str(config.get("ENCODER_MODEL") or "").lower()
    if encoder not in ENCODER_MODELS:
        raise ValueError(
            f"ENCODER_MODEL must be one of {ENCODER_MODELS}, got {config.get('ENCODER_MODEL')!r}"
        )
    config.ENCODER_MODEL = encoder

    if encoder == "terramind":
        version = str(config.get("TERRAMIND_VERSION") or "small").lower()
        if version not in TERRAMIND_VERSIONS:
            raise ValueError(
                f"TERRAMIND_VERSION must be one of {TERRAMIND_VERSIONS}, got "
                f"{config.get('TERRAMIND_VERSION')!r}"
            )
        config.TERRAMIND_VERSION = version
    return config


def build_dataset_config(config):
    from geosamroad.dataset.samroad_dataset import SamRoadDatasetConfig

    return SamRoadDatasetConfig(
        dataset_dir=config.DATASET_DIR,
        model_encoder=str(config.ENCODER_MODEL),
        RGB_INPUT=bool(config.RGB_INPUT),
        rgb_source=config.get("RGB_SOURCE") or None,
        crop_size=int(config.CROP_SIZE),
        upscale=int(config.UPSCALE),
        augment=bool(config.AUGMENT),
        train_len_factor=float(config.TRAIN_LEN_FACTOR),
        min_road_density=float(config.MIN_ROAD_DENSITY),
        min_crop_road_px=int(config.MIN_CROP_ROAD_PX),
        max_crop_attempts=int(config.MAX_CROP_ATTEMPTS),
        preload_graphs=bool(config.PRELOAD_GRAPHS),
        final_train=bool(config.get("FINAL_TRAIN", False)),
        keypoint_buffer_m=float(config.KEYPOINT_BUFFER_M),
        TOPO_SAMPLE_NUM=int(config.TOPO_SAMPLE_NUM),
        TOPONET_VERSION=config.TOPONET_VERSION,
        ROAD_NMS_RADIUS=int(config.ROAD_NMS_RADIUS),
        NEIGHBOR_RADIUS=int(config.NEIGHBOR_RADIUS),
        MAX_NEIGHBOR_QUERIES=int(config.MAX_NEIGHBOR_QUERIES),
        ITSC_NMS_RADIUS=int(config.ITSC_NMS_RADIUS),
    )


def create_output_dir_and_save_config(output_dir_prefix, config, specified_dir=None):
    if specified_dir:
        output_dir = specified_dir
    else:
        # Generate the output directory name with the current timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = f"{output_dir_prefix}_{timestamp}"

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    config_path = os.path.join(output_dir, "config.yaml")

    with open(config_path, "w") as file:
        yaml.dump(config.to_dict(), file)

    return output_dir
