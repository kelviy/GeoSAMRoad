# Hand back your zone

Upload your cleaned zone and mark it done.

Hand back a zone when every item in its review queue is done and you have fixed the rest by hand, as described in [Review with the plugin](review-with-plugin.md) and [Clean by hand in QGIS](clean-by-hand.md). Terms such as zone GeoPackage and cleaning work are defined in the [glossary](../glossary.md).

## Close QGIS

While QGIS has a zone GeoPackage open, it writes your changes to a separate file next to it, `<zone>.gpkg-wal`. Changes in that file are not yet in `<zone>.gpkg`. When QGIS closes the GeoPackage, it moves them into `<zone>.gpkg` and deletes the `-wal` file.

1. Select **Save Layer Edits** on the toolbar, or select **Layer > Save Layer Edits**.
2. Quit QGIS.
3. List the zone folder:

    ```sh
    ls <zones-dir>/<zone>/
    ```

4. Check that the folder has no `<zone>.gpkg-wal` file.

If a `-wal` file is still there after QGIS crashed, start QGIS, open `<zone>.qgs`, and quit QGIS again. That writes the edits from the `-wal` file into `<zone>.gpkg`. Do not delete a `-wal` file yourself, because your last edits may be in it.

`package-zones` also refuses a zone that has a `-wal` file, so a forgotten `-wal` file blocks kelviy too.

## Assembler

The Assembler would use the cleaned zones to be turned into tiles again. As [Assemble and retile](assemble-and-retile.md) describes. That step is not built yet.
