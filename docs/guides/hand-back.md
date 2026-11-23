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

## Upload the zone

Upload only `<zone>.gpkg`. It holds all your cleaning work: your label edits, your flag decisions and your tile reviews. Kelviy already has the other files of the zone package.

1. Open the shared Drive. Ask kelviy for the link.
2. Upload `<zones-dir>/<zone>/<zone>.gpkg` to the zone's folder, or to the place kelviy told you.

Upload the file unchanged, exactly as QGIS saved it. Do not rename it, and do not zip it.

!!! note "Screenshot: the shared Drive with a cleaned zone GeoPackage uploaded"

## Mark the zone done

1. Open the shared Sheet. Ask kelviy for the link.
2. In your row for the zone, set **status** to `done`.
3. Set **returned date** to the date of the upload.

Kelviy picks up the zone from there. Cleaned zones will be read back by `raw_id` and `status` and turned into tiles again, as [Assemble and retile](assemble-and-retile.md) describes. That step is not built yet.
