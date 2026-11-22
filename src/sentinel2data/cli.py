from pathlib import Path
from typing import Annotated, List, Optional, Tuple
import typer
import yaml

from sentinel2data.supplementary.roads import RoadVectorExtractor
from sentinel2data.generator.config import DROPPED_BANDS, EXCLUDED_ROAD_CLASSES
from sentinel2data.generator.pipeline import make_rosa_pipeline

app = typer.Typer(help="ROSA Dataset CLI")

DATASET_HELP = "Base path to the ROSA dataset directory"
DatasetDir = Annotated[Path, typer.Option(help=DATASET_HELP)]


@app.callback()
def _main(
    ctx: typer.Context,
    config: Annotated[
        Optional[Path],
        typer.Option("--config", help="Check example config or cli.py for config options"),
    ] = None,
):
    """The flag (--config) pre-fills each command's options."""
    if config is not None:
        loaded = yaml.safe_load(config.read_text()) or {}
        ctx.default_map = {**(ctx.default_map or {}), **loaded}


@app.command()
def generate(
    roads: Annotated[
        Path, 
        typer.Option(help="Cleaned geoparquet roads. Use `roads` command")
    ],
    imagery_dir: Annotated[
        Optional[Path],
        typer.Option(help="Pre-split imagery root folder containing train, val, test of imagery"),
    ],
    output_dir: Annotated[
        Optional[Path], 
        typer.Option(help="Output dataset directory")
    ],
    tile_size: Annotated[
        int, 
        typer.Option(help="Tiling size in pixels")
    ] = 512,
    patch_size: Annotated[
        int, 
        typer.Option(help="Image COG internal block size")
    ] = 256,
    exclude_road_class: Annotated[
        List[str],
        typer.Option(help="Road class removed before any label is produced. Repeat for several"),
    ] = list(EXCLUDED_ROAD_CLASSES),
    keep_all_road_classes: Annotated[
        bool,
        typer.Option(help="Exclude no road classes (ignores --exclude-road-class)"),
    ] = False,
    keep_s1_descending: Annotated[
        bool,
        typer.Option(help="Keep the Sentinel-1 descending VV/VH bands (legacy 23-band layout)"),
    ] = False,
):
    """Generate the ROSA dataset from a pre-split imagery root"""
    make_rosa_pipeline(
        imagery_dir=imagery_dir,
        output_dir=output_dir,
        roads_parquet_path=roads,
        tile_size=tile_size,
        patch_size=patch_size,
        excluded_road_classes=() if keep_all_road_classes else tuple(exclude_road_class),
        dropped_bands=() if keep_s1_descending else DROPPED_BANDS,
    ).run()


@app.command()
def package_zones(
    roads: Annotated[
        Path,
        typer.Option(help="The baseline roads parquet that `generate` was run with"),
    ],
    imagery_dir: Annotated[
        Path,
        typer.Option(help="Pre-split imagery root (train, val, test) that `generate` was run with"),
    ],
    dataset_dir: Annotated[
        Path,
        typer.Option(help="Baseline dataset directory written by `generate` (holds tile_status/)"),
    ],
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory to write one folder per zone into"),
    ],
    zone: Annotated[
        Optional[List[str]],
        typer.Option(help="Zone filename stem to package. Repeat for several; default all zones"),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option(help="Repackage zones whose GeoPackage already holds cleaning work (old file kept as .bak)"),
    ] = False,
):
    """Package zones for cleaning: a zone GeoPackage and a QGIS project per zone."""
    from sentinel2data.cleaning.packager import ZoneHasCleaningWork, ZoneIsOpen, find_zones, package_zone

    zones = find_zones(imagery_dir)
    names = list(zone) if zone else sorted(zones)
    unknown = [n for n in names if n not in zones]
    if unknown:
        raise typer.BadParameter(f"No zone COG under {imagery_dir} for: {', '.join(unknown)}")

    refused = []
    for name in names:
        try:
            folder = package_zone(
                zones[name], roads_path=roads, dataset_dir=dataset_dir,
                output_dir=output_dir, overwrite=overwrite,
            )
        except (ZoneHasCleaningWork, ZoneIsOpen) as exc:
            refused.append(name)
            typer.echo(f"Refused {name}: {exc}", err=True)
            continue
        typer.echo(f"Packaged {name} -> {folder.root}")

    if refused:
        typer.echo(f"{len(refused)} zone(s) not repackaged (cleaning work, or open elsewhere): {', '.join(refused)}", err=True)
        raise typer.Exit(1)


@app.command()
def roads(
    out: Annotated[
        Path, 
        typer.Option(help="Output path (.parquet GeoParquet or .gpkg)")
    ],
    cdngi: Annotated[
        Optional[Path],
        typer.Option(help="CDNGI GeoPackage file, or a directory of province *.gpkg"),
    ] = None,
    overture: Annotated[
        Optional[Path], typer.Option(help="Overture roads GeoParquet")
    ] = None,
    frst: Annotated[
        Optional[Path], typer.Option(help="FRST roads shapefile (.shp)")
    ] = None,
):
    """Converts road vector files from CDNGI, Overture or FRST into a cleaned roads parquet."""
    if sum(p is not None for p in (cdngi, overture, frst)) != 1:
        raise typer.BadParameter("Provide exactly one of --cdngi, --overture or --frst.")

    RoadVectorExtractor.from_paths(
        out_path=out, cdngi_path=cdngi, overture_path=overture, frst_path=frst,
    ).build()


@app.command()
def norm_stats(
    dataset_dir: DatasetDir,
    out: Annotated[
        Optional[Path],
        typer.Option(help="Output config YAML (default <dataset_dir>/norm_stats.yaml)"),
    ] = None,
    exclude_zero: Annotated[
        bool,
        typer.Option(help="If COG contains no nodata, drop 0-valued pixels from the stats"),
    ] = True,
    sar_clip: Annotated[
        Optional[Tuple[float, float]],
        typer.Option(help="Input (LOW, HIGH). Clip SAR bands (VV/VH) before stats"),
    ] = None,
):
    """Pre-compute per-band mean and std over train split"""
    from sentinel2data.supplementary.compute_norm_stats import (
        compute,
        print_table,
        write_stats_yaml,
    )

    out = out or dataset_dir / "norm_stats.yaml"
    names, mean, std = compute(dataset_dir, exclude_zero=exclude_zero, sar_clip=sar_clip)
    saved = write_stats_yaml(out, mean, std)
    typer.echo(f"\nSaved {saved}")
    print_table(names, mean, std)


@app.command()
def change_report(
    dataset_dir: Annotated[
        Path,
        typer.Option(help="Baseline dataset directory written by `generate` (holds tile_status/)"),
    ],
    previous_dir: Annotated[
        Path,
        typer.Option(help="Previous dataset directory to compare with (its generation_report.yaml)"),
    ],
    out: Annotated[
        Optional[Path],
        typer.Option(help="Output markdown (default <dataset_dir>/kept_dropped_report.md)"),
    ] = None,
):
    """Kept/dropped tiles per split and zone against a previous dataset, plus excluded road classes."""
    from sentinel2data.supplementary.change_report import write_change_report

    typer.echo(f"Wrote {write_change_report(dataset_dir, previous_dir, out)}")


@app.command()
def summary(
    dataset_dir: DatasetDir,
    out: Annotated[
        Optional[Path],
        typer.Option(help="Output summary YAML (default <dataset_dir>/dataset_summary.yaml)"),
    ] = None,
):
    """Per-split biome + urbanisation ratios and road-density stats from metadata.parquet."""
    from sentinel2data.supplementary.summary import summarise_metadata

    metadata_path = dataset_dir / "metadata.parquet"
    if not metadata_path.exists():
        raise typer.BadParameter(f"metadata.parquet not found under {dataset_dir}")
    summarise_metadata(metadata_path, out)


if __name__ == "__main__":
    app()
