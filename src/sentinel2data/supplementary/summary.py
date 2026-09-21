from pathlib import Path
import geopandas as gpd
import pandas as pd
import yaml


def _ratios(series):
    """Normalised value-count dict (per category), rounded."""
    vc = series.value_counts(dropna=False, normalize=True)
    return {str(k): round(float(v), 4) for k, v in vc.items()}


def _density_stats(series):
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return {"mean": 0.0, "p50": 0.0, "p90": 0.0, "max": 0.0}
    return {
        "mean": round(float(s.mean()), 6),
        "p50": round(float(s.quantile(0.5)), 6),
        "p90": round(float(s.quantile(0.9)), 6),
        "max": round(float(s.max()), 6),
    }


def summarise_dataset(gdf) -> dict:
    summary = {"total_tiles": int(len(gdf)), "splits": {}}
    for split, group in gdf.groupby("split_set"):
        summary["splits"][str(split)] = {
            "tiles": int(len(group)),
            "urbanisation_ratio": _ratios(group["urbanisation_classification"]),
            "road_density_ratio": _ratios(group["road_density_classification"]),
            "biome_ratio": _ratios(group["biome"]),
            "road_density": _density_stats(group["road_density"]),
        }
    return summary


def print_summary(summary):
    print(f"\nDataset summary ({summary['total_tiles']} tiles):")
    for split, s in summary["splits"].items():
        print(f"\n[{split}] {s['tiles']} tiles")
        print("  urbanisation (from filename):")
        for k, v in s["urbanisation_ratio"].items():
            print(f"    {k:>12}: {v:6.1%}")
        print("  road density class (Jenks):")
        for k, v in s["road_density_ratio"].items():
            print(f"    {k:>12}: {v:6.1%}")
        print("  biome:")
        for k, v in s["biome_ratio"].items():
            print(f"    {k:>12}: {v:6.1%}")
        d = s["road_density"]
        print(f"  road_density: mean={d['mean']:.5f} p50={d['p50']:.5f} "
              f"p90={d['p90']:.5f} max={d['max']:.5f}")


def write_summary_yaml(summary, out_path):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.safe_dump(summary, sort_keys=False))
    return out_path


def summarise_metadata(metadata_path, out_path=None):
    """Read a ``metadata.parquet``, print the summary and write a YAML beside it."""
    metadata_path = Path(metadata_path)
    gdf = gpd.read_parquet(metadata_path)
    summary = summarise_dataset(gdf)
    print_summary(summary)
    out_path = Path(out_path) if out_path else metadata_path.parent / "dataset_summary.yaml"
    write_summary_yaml(summary, out_path)
    print(f"\nWrote {out_path}")
    return summary
