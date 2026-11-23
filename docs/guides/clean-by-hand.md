# Clean by hand in QGIS

Keep, modify, add and delete roads in the labels layer, for QGIS beginners.

You edit one layer, **Labels**. Every row in it is a road. Each row has a `status` that says what you did to it, and a `defect` that says why. The packager filled the layer with a copy of the raw labels, all with `status = kept`. Your job is to change only the rows that disagree with the reference imagery, and to set `status` and `defect` on each one you change. The [review queue plugin](review-with-plugin.md) does most of the simple cases with one key. This page covers everything you draw yourself.

Terms are defined in the [glossary](../glossary.md).

Before you start, finish [Setup](setup.md) and [Claim and download a zone](claim-and-download.md). To open the zone, select **Project > Open** (++ctrl+o++) and choose `<zone>.qgs` in the zone package folder. Open the `.qgs` file, not the `.gpkg` file. The `.qgs` file holds the styles, drop-down lists and rules that this page relies on.

## The layers

The **Layers** panel lists the layers of the zone project. If you cannot see the panel, select **View > Panels > Layers**. Tick or untick the box next to a layer to show or hide it. The layers are listed here from top to bottom.

| Layer | What it is | How it is drawn |
| --- | --- | --- |
| **Flags (points)** | Point flags from the detectors: `gap`, `stub`, `duplicate` and `crossing`. | Each type has its own colour and marker. `gap` is a pink diamond, `stub` a red circle, `duplicate` a purple square and `crossing` a brown triangle. Each flag is labelled with its type, then its decision once it has one, for example `stub · real`. |
| **Flags (areas)** | Area flags from the second source: `missing?` and `phantom?`. | `missing?` is a red box and `phantom?` a purple box, each with a light fill. |
| **Labels** | The layer you edit. One row per road. | Coloured by `status`. `kept` is blue, `modified` orange and `added` green. `deleted` is a grey dashed line. `proposed` is a magenta dashed line. |
| **Raw labels** | The frozen baseline. Read-only. | A wide yellow line under the labels. Where a label still matches its raw label, you see a blue line with a yellow casing. |
| **Overture roads** | Roads from the second source, clipped to the zone. Read-only. Only present when the zone was packaged with Overture. | A line of white circles with a black outline. |
| **Tiles** | The tile grid of the zone. | Kept tiles have a cyan outline. `dropped_no_road` tiles are hatched grey. `dropped_nan` and `untiled` tiles are greyed out. |
| **Sentinel-2 RGB** | The 2020 Sentinel-2 image of the zone in true colour. | Hidden at start. |
| **Sentinel-2 false colour** | The same image with NIR shown as red. | Hidden at start. Vegetation is red. Roads, bare ground and buildings are not. |
| **NGI aerial** | The reference imagery, a high-resolution aerial basemap loaded from the internet. | Visible at start. Needs a network connection. |

A flag decided `real` or `not_defect` turns grey. A `needs_qgis` flag keeps its colour, because it still needs work in QGIS.

!!! note "Screenshot: the Layers panel of a freshly opened zone, with the map showing labels over the NGI aerial and the cyan tile outlines"

## QGIS basics

QGIS tools act on the **active layer**. The active layer is the one highlighted in the **Layers** panel. Click **Labels** in the panel before you use any tool on this page.

### Start and save edits

1. In the **Layers** panel, click **Labels**.
2. Select **Layer > Toggle Editing**, or click the pencil button on the **Digitizing** toolbar. Small vertex markers appear on the roads, and the editing tools become active.
3. Make your edits.
4. Select **Layer > Save Layer Edits**, or click the save button next to the pencil on the **Digitizing** toolbar. QGIS writes your edits to the zone GeoPackage.

Your work lives in the GeoPackage, not in the project file. Saving the project does not save your edits. You can leave editing on for the whole session and save layer edits as you go.

If a toolbar is missing, select **View > Toolbars** and tick it. This page uses the **Digitizing**, **Advanced Digitizing**, **Snapping** and **Attributes** toolbars.

!!! note "Screenshot: the Digitizing toolbar with Toggle Editing and Save Layer Edits marked"

### Open the attribute form

The attribute form shows the fields of one road, and you set `status`, `defect` and `road_class` in it. The fields behave as follows:

- `status`, `defect` and `road_class` are drop-down lists of the allowed values. You cannot type other values.
- `raw_id`, `buffer` and `scale_class` are read-only.
- `annotator` fills itself with your full name each time you save a change to the row.

There are three ways to open the form:

- **On draw.** When you finish a new line with **Add Line Feature**, the form opens by itself.
- **With Identify.** Click the **Identify Features** button on the **Attributes** toolbar, then click a road. The **Identify Results** panel opens. Click the **View Feature Form** button at the top of that panel. While editing is on, you can change the fields in the form.
- **With the attribute table.** Right-click **Labels** in the **Layers** panel and select **Open Attribute Table** (++f6++). Each row is a road. Click a cell to change it while editing is on.

!!! note "Screenshot: the attribute form of a Labels row, with the status, defect and road_class drop-downs open"

### Reshape a road with the Vertex tool

1. Turn editing on for **Labels**.
2. On the **Digitizing** toolbar, click the **Vertex Tool** button. Use the drop-down next to it to pick **Vertex Tool (Current Layer)**, so you cannot edit other layers by mistake.
3. Click a vertex to pick it up. Move the mouse and click again to put it down.
4. To add a vertex, double-click on the line where you want it.
5. To remove a vertex, click it so that it is selected, then press ++delete++.

!!! note "Screenshot: the Vertex tool moving one vertex of a road"

### Draw a road with Add Line Feature

1. Turn editing on for **Labels**.
2. On the **Digitizing** toolbar, click the **Add Line Feature** button.
3. Left-click on the map for each vertex of the road.
4. Right-click to finish the line. The attribute form opens.
5. To cancel a line before you finish it, press ++esc++.

!!! note "Screenshot: Add Line Feature mid-way through drawing a road, and the attribute form that opens when it is finished"

### Cut a road with Split Features

**Split Features** cuts one row into two rows. Both pieces keep every attribute of the original row, `raw_id` included. You need it to delete or move only part of a road.

1. Turn editing on for **Labels**.
2. Select **Edit > Edit Geometry > Split Features**, or click the **Split Features** button on the **Advanced Digitizing** toolbar.
3. Left-click on one side of the road, then left-click on the other side, so that your line crosses the road where you want to cut it.
4. Right-click to finish. The road is now two rows.

!!! note "Screenshot: Split Features cutting a road, with the two pieces selected afterwards"

### Turn on snapping and tracing

Snapping makes a new vertex jump onto an existing vertex, so that two roads meet exactly. The road graph that the dataset is built from joins roads only where they share a vertex. A road that ends a few centimetres short of another road is not connected.

Snapping is off when the project opens. The project sets it to snap to vertices of **Labels** and **Raw labels** only, within 12 pixels. It never snaps to **Overture roads**.

1. If the **Snapping** toolbar is hidden, select **View > Toolbars > Snapping Toolbar**.
2. Click the magnet button to turn snapping on, or press ++s++.
3. To trace along an existing road while you draw, click the **Tracing** button on the same toolbar, or press ++t++. Tracing needs snapping to be on.
4. To check which layers it snaps to, open the drop-down next to the magnet button and select **Open Snapping Options**. Leave the advanced setting in place. QGIS's default setting snaps to every layer, **Overture roads** included.

The ++s++ and ++t++ keys belong to QGIS while the map has focus. While the review queue panel has focus, the panel takes these keys instead. See [Review with the plugin](review-with-plugin.md).

!!! note "Screenshot: the Snapping toolbar with snapping and tracing on, and the snapping options showing Labels and Raw labels ticked"

## Keep a road

A road that matches the reference imagery needs nothing.

1. Leave the row as it is. Its `status` stays `kept` and its `defect` stays empty.

Leave the fields of a kept road alone, even to set a value back. A save stamps your name in `annotator`, and that counts as cleaning work even when nothing else changed.

## Modify a road

Modify a road when it exists in the reference imagery but its line is wrong. Examples are a road drawn off to one side, a curve cut short, or a road that stops short of the road it joins.

1. Turn editing on for **Labels**.
2. Reshape the road with the [Vertex tool](#reshape-a-road-with-the-vertex-tool). Follow the centre of the road in the **NGI aerial** layer.
3. Open the [attribute form](#open-the-attribute-form) for the road.
4. Set `status` to `modified`.
5. Set `defect` to the reason, usually `misalignment` or `gap`. See [Defect values](#defect-values).
6. Click **OK**, then save layer edits.

!!! warning "Moving a raw road does not set `modified` for you"
    QGIS does not change `status` when you move vertices. A road you reshaped still reads `kept` until you set `status = modified` yourself. Check the colour on the map. A modified road is orange. A blue road is still `kept`.

To change only part of a road, split it first with [Split Features](#cut-a-road-with-split-features). Then reshape the piece that is wrong and set that piece to `modified`.

!!! note "Screenshot: a misaligned road before and after the fix, with the form showing status = modified and defect = misalignment"

## Add a road

Add a road when the reference imagery shows a road that the labels do not have.

!!! warning "Overture roads are a hint, never a source"
    Use **Overture roads** to find where to look. Draw every vertex from the imagery. Never copy, paste or trace an Overture road into **Labels**. Overture is under the ODbL licence, and copied geometry would make the labels a derivative database. Snapping already ignores the Overture layer, but nothing stops a copy and paste.

1. Turn editing on for **Labels**.
2. Turn snapping on (++s++), so that the new road meets the roads it joins.
3. Click **Add Line Feature** and draw the road along its centre in the **NGI aerial** layer. Start and end on a vertex of the road it joins.
4. Right-click to finish. The form opens with `status = added` and `road_class = Other Road` already set.
5. Set `defect` to `missing`, or to `temporal` for a road that only the 2020 Sentinel-2 image shows.
6. If the road is clearly a main road or a street, change `road_class` to **Main Road** or **Street**. `buffer` follows `road_class` by itself.
7. Click **OK**, then save layer edits.

| `road_class` | `buffer` |
| --- | --- |
| Other Road | 5 m |
| Main Road | 8 m |
| Street | 4 m |

If the new road meets another road in the middle of a segment, the other road has no vertex there. Add one with the [Vertex tool](#reshape-a-road-with-the-vertex-tool) at the junction, on top of your road's end. That road has now changed. Set its `status` to `modified` and give it the same `defect` as the road you added. The plugin does the same when it accepts a gap connector.

!!! note "Screenshot: a new road drawn with snapping on, and the form that opens with status = added and road_class = Other Road"

## Delete a road

Delete a road when the reference imagery shows no road there. Examples are a phantom road or a short dead-end stub.

!!! warning "Never delete a row. Set `status = deleted`."
    Do not use **Delete Selected** or press ++delete++ on a selected road. Every raw road must keep its row, so that the cleaned labels trace back to the raw labels. QGIS cannot block the delete tool, so this is up to you. If you delete a row by mistake, press ++ctrl+z++ before you save, or select **Layer > Toggle Editing** and choose to discard the edits.

1. Turn editing on for **Labels**.
2. Open the [attribute form](#open-the-attribute-form) for the road.
3. Set `status` to `deleted`.
4. Set `defect` to the reason, usually `phantom`, `stub` or `temporal`.
5. Click **OK**, then save layer edits. The road turns into a grey dashed line.

To delete only part of a road, split it first with [Split Features](#cut-a-road-with-split-features). Set the unwanted piece to `deleted`. The other piece is shorter than its raw road now, so set it to `modified` with the same defect.

!!! note "Screenshot: a phantom road set to status = deleted, drawn as a grey dashed line"

## Defect values

Every row you change gets one `defect`. A `kept` row has no defect.

| `defect` | Use it when | Usual fix |
| --- | --- | --- |
| `gap` | A road stops short of a road it clearly joins, or a road has a break that is not in the imagery. | Modify the road to close the gap, or add a short connecting road. |
| `stub` | A short dead-end branch sticks out of a road where the imagery shows no road. The detector looks at branches shorter than 30 m. | Delete the stub. |
| `misalignment` | The road exists but the line lies off its centre in the reference imagery. | Modify the road. |
| `phantom` | The labels have a road that the reference imagery does not show at all. | Delete the road. |
| `missing` | The reference imagery shows a road that the labels do not have. | Add the road. |
| `temporal` | One source clearly shows the road and the other clearly does not, because they were captured at different times. | Follow the 2020 Sentinel-2 image. Add, keep or delete the road to match it. |

The reference imagery decides where a road lies and whether it exists. The one exception is a clear temporal mismatch, where the 2020 Sentinel-2 image decides whether the road exists. When either source is unclear, the reference imagery decides and the raw label stands. [ADR 0001](../adr/0001-aerial-authority-with-temporal-override.md) records this rule.

### Check a temporal mismatch

The aerial imagery is not from 2020, but the labels must describe the 2020 Sentinel-2 image. A road built after 2020 shows in the aerial but not in Sentinel-2.

1. In the **Layers** panel, tick **Sentinel-2 false colour**. It draws above **NGI aerial**.
2. Look along the road. Vegetation is red. A road, bare ground and buildings are not red, so a road through vegetation shows as a thin line that is not red.
3. Tick and untick the layer to compare it with the aerial. **Sentinel-2 RGB** gives a true-colour view of the same image.
4. Decide as follows:
    - If Sentinel-2 clearly shows no road where the aerial shows one, delete the label with `defect = temporal`.
    - If Sentinel-2 clearly shows a road that the aerial does not, add it with `defect = temporal`. Trace it from Sentinel-2, and do not correct it for misalignment.
    - If you are not sure, leave the road as the aerial shows it.
5. Untick the Sentinel-2 layer to see the aerial again.

Sentinel-2 pixels are 10 m wide, so a narrow road may not show at all. A road that is only invisible in Sentinel-2 is not a temporal mismatch.

!!! note "Screenshot: the same place in NGI aerial and in Sentinel-2 false colour, with a new road visible only in the aerial"

## Rules

- **Edit only inside kept tiles.** Kept tiles have a cyan outline. Only kept tiles become part of the dataset, so edits elsewhere are lost effort.
- **Never copy Overture geometry.** Overture roads are a hint for where to look. Draw from the imagery.
- **An existing road keeps its class.** A row with a `raw_id` keeps the `road_class` of its raw road. QGIS refuses to save a change to it. Only a road you added can take **Other Road**, **Main Road** or **Street**.
- **`buffer` follows `road_class`.** It is read-only and is set again on every save.
- **Never delete a row.** Set `status = deleted`.
- **Never merge rows.** Each row traces back to at most one raw road. Splitting is fine. Joining two rows into one is not.

## Tips and tricks

- **Save often.** Select **Layer > Save Layer Edits** after every few fixes. A crash loses only unsaved edits.
- **Undo** with ++ctrl+z++. Undo works only back to your last save. The review queue plugin saves your open edits before each decision, so undo cannot reach past the last key you pressed in the panel.
- **Zoom with the mouse wheel and pan by dragging with the middle button.** The **Pan Map** button on the **Map Navigation** toolbar also pans.
- **Zoom to a road.** Open the attribute table, right-click a row, and select **Zoom to Feature**.
- **Filter the attribute table.** Click **Show All Features** at the bottom left of the attribute table and select **Advanced Filter (Expression)**. Type an expression such as `"raw_id" = 124` or `"status" = 'deleted'`.
- **Hide layers that get in the way.** Untick **Raw labels** to see the imagery under a road. Untick **Flags (areas)** when a box hides what you need to see.
- **Check for a missing defect.** Every road that is not `kept` needs a `defect`. Filter the attribute table on `"status" != 'kept' AND "defect" IS NULL` to find the rows you missed.
- **Leave proposals to the plugin.** A magenta dashed line is a proposal. Press `A` or `X` on it in the review queue panel. If you marked it `needs_qgis`, reshape the line with the Vertex tool and set `status = added`.

### Work through `needs_qgis` items

In the review queue panel, `Q` marks a flag or proposal as `needs_qgis` when the fix needs drawing. Work through those items after you finish the queue.

1. Find them on the map. A `needs_qgis` flag keeps its colour and its label ends in `· needs_qgis`.
2. To list them, right-click **Flags (points)** or **Flags (areas)**, open the attribute table, and filter it on `"decision" = 'needs_qgis'`. Right-click a row and select **Zoom to Feature**.
3. Alternatively, list them from a terminal in the repo, with the Python from [Setup](setup.md):

    ```sh
    python -m sentinel2data.cli needs-qgis --zones-dir <zones folder> --zone <zone>
    ```

    Each line gives the zone, the flag fid, its type and a point on it in the zone's coordinates, for example `Z: flag 12 stub at 512340.0, 6249050.0`.
4. Fix each one with the recipes on this page.

## Troubleshooting

### QGIS refuses to save a road class change

The form or the save shows a constraint message: *Existing roads keep their raw class; added roads are Other Road, Main Road, Street.*

- For a row with a `raw_id`, set `road_class` back to its raw value. To see the raw value, click the road with **Identify Features** and look at the **Raw labels** result.
- For a road you added, pick one of **Other Road**, **Main Road** or **Street**.

Some existing roads have a class, such as National Road, that is not in the drop-down list. That is expected. Leave the value as it is.

### `annotator` is empty, or the panel says your full name is missing

QGIS fills `annotator` with the full name of your computer account. With no full name set, `annotator` stays empty and the review queue panel refuses to write decisions.

1. Set a full name for your account, for example `Jane Doe`. [Setup](setup.md) shows how on Linux and macOS.
2. Quit QGIS and open it again, so that it reads the new name.

Rows you saved before the fix keep an empty `annotator` until you change and save them again. Tell kelviy which zone it was.

### A zone is refused because of a `-wal` file

The packager or `detect` refuses the zone because `<zone>.gpkg-wal` exists next to `<zone>.gpkg`. A `-wal` file means QGIS has the GeoPackage open, and edits may still be waiting in that file.

1. Save layer edits in QGIS.
2. Select **Project > Close**, or quit QGIS. The `-wal` file goes away when QGIS closes the GeoPackage.
3. If QGIS crashed and the `-wal` file is still there, open the `.qgs` project again, then close it properly.

Do not delete the `-wal` file by hand. It can hold edits that are not in the `.gpkg` file yet. Close the zone before you [hand it back](hand-back.md).
