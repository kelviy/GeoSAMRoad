import numpy as np

from sentinel2data.generator.config import RGBEnhanceConfig
from sentinel2data.generator.helper import enhance_rgb


def append_enhanced_rgb(tile, config=None):
    if config is None:
        config = RGBEnhanceConfig()
    rgb_idx = [b - 1 for b in config.rgb_bands]
    return np.concatenate([tile, enhance_rgb(tile[rgb_idx], config)], axis=0)
