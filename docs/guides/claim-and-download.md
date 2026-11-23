# File Downloads

Download from the shared Drive.

## Download the zone package

Each zone package is a folder named after its zone in the shared Drive. Ask kelviy for the link.

1. In the shared Drive, find the folder of the zone you claimed. For a shared zone, take the copy made for you. Ask kelviy if you cannot tell the copies apart.
2. Download the whole folder. Drive downloads a folder as a zip file.
3. Unzip it into `<zones-dir>`, a folder you choose for zone packages. You get `<zones-dir>/<zone>/`.
4. Check that `<zone>.gpkg` and `<zone>.qgs` are in that folder.
5. Start QGIS.
6. Select **Project > Open**, then select `<zone>.qgs`.

The review queue panel docks on the right and goes to the first item that is not done. If the panel does not show, select **Plugins > ROSA review queue > Review queue panel**. Continue with [Review with the plugin](review-with-plugin.md).

!!! note "Screenshot: a zone project just opened, with the review queue panel docked on the right"

## What is in a zone package

Kelviy built the package with `package-zones` and then ran the detectors on it with `detect`. The GeoPackage already holds the detectors' flags and proposals when you download it.

| File | What it is |
| --- | --- |
| `<zone>.gpkg` | The zone GeoPackage. It holds the layers `raw_labels`, `labels`, `tiles`, `flags` and `detector_runs`. The review queue adds `flag_deletions`, `review_position`, `proposal_rows` and `connector_vertices` the first time it writes. |
| `<zone>.qgs` | The QGIS project. Open this file. |
| `<zone>_s2_rgbnir.tif` | The zone's Sentinel-2 red, green, blue and near-infrared bands, each contrast-enhanced for viewing. |
| `<zone>_overture.gpkg` | The Overture roads clipped to the zone, with `road_class`. It is there only if the zone was packaged with `--overture`. |

All paths in the project are relative, so you can put the folder anywhere. Move or copy the folder as a whole.

The project shows these layers, from top to bottom:

- **Flags (points)** and **Flags (areas)**: the detectors' flags.
- **Labels**: the layer you edit, coloured by `status`.
- **Raw labels**: the frozen raw labels, read-only, drawn as a yellow casing under the labels.
- **Overture roads**: the second source, read-only.
- **Tiles**: kept tiles outlined in cyan. You edit only inside kept tiles.
- **Sentinel-2 RGB** and **Sentinel-2 false colour**: hidden when the project opens.
- **NGI aerial**: the reference imagery, loaded from the web.

[Review with the plugin](review-with-plugin.md) and [Clean by hand in QGIS](clean-by-hand.md) explain how to use each layer.
