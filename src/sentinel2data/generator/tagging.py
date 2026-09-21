from abc import ABC, abstractmethod
import geopandas as gpd
import jenkspy
import numpy as np
import pandas as pd
from sentinel2data.generator.config import (
    CLASS_LABELS,
    EMPTY_LABEL,
)

class Tagger(ABC):
    """Compute metadata column aligned to the input rows."""
    column: str

    @abstractmethod
    def tag(self, gdf: gpd.GeoDataFrame) -> pd.Series:
        pass


class RoadDensityClassifier(Tagger):
    """Classify each tile Low/Medium/High by road density over whole dataset."""

    column = "road_density_classification"

    def __init__(self, density_col="road_density"):
        self.density_col = density_col

    def tag(self, gdf) -> pd.Series:
        labels = self._classify(gdf[self.density_col].to_numpy())
        return pd.Series(labels, index=gdf.index, dtype=object).rename(self.column)

    @staticmethod
    def _classify(densities: np.ndarray) -> np.ndarray:
        labels = np.full(densities.shape, EMPTY_LABEL, dtype=object)
        values = densities[densities > 0]
        if values.size == 0:
            return labels

        # checks
        if np.unique(values).size >= 3:
            breaks = sorted(set(jenkspy.jenks_breaks(values, n_classes=3)))
        else:
            breaks = sorted({float(values.min()), float(values.max())})

        n_bins = len(breaks) - 1
        if n_bins < 1:
            per_value = np.full(values.size, CLASS_LABELS[0])
        else:
            bin_labels = CLASS_LABELS[:n_bins]
            per_value = pd.cut(
                values, bins=breaks, labels=bin_labels, include_lowest=True
            ).astype(str)
            print("Density breaks:", [round(b, 5) for b in breaks], "->", bin_labels)
            if n_bins < len(CLASS_LABELS):
                print(
                    f"Note: only {n_bins} density class(es) -- too few distinct "
                    "densities for 3 Jenks breaks."
                )

        labels[densities > 0] = per_value
        return labels
