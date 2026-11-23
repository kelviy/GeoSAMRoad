# Review with the plugin

Work through a zone's review queue in the docked panel, one key per decision.

Before you start, install the plugin and set your full name, as [Setup](setup.md) describes. Terms such as flag, proposal and kept tile are defined in the [glossary](../glossary.md).

## Plugin and Defect Detectors

The detectors help scan defects. They placed a flag at each suspected defect inside the kept tiles. For gaps, the gap detector also drew the fix, a connector line, so the flag is a proposal. The review queue takes you to each item in turn, and one key decides it:

- **A proposal.** `A` accepts the connector and makes it a road. `X` rejects it and removes it.
- **A `stub` or `phantom?` flag.** `R` confirms the defect and deletes the roads the flag names.
- **Any other flag.** `R` or `N` records whether the defect is real.
- **A kept tile.** `T` marks it reviewed after you have looked over the whole tile.

The plugin also can filter based on the kind and type of label. This allows for concurrent semi-automated workflow in QGIS.

Every decision recorded on plugin is recorded into the zone GeoPackage and tagged with an annotator. Just make sure to save in order to not lose work, in the case of a crash. Hopefully, the plugin will do the heavy lifting on the actual road label editing.

## Open the queue

1. Open the zone's `<zone>.qgs` in QGIS.
2. The **ROSA review queue** panel docks on the right.
3. Click inside the panel so that it has focus. The keys work only while the panel has focus.

If you closed the panel, select **Plugins > ROSA review queue > Review queue panel** to show it again. When the panel is closed as you open a zone, the map keeps the project's saved view.

A project that is not a zone project leaves the panel idle. It shows "Open a zone project (`<zone>.qgs`)."

The panel shows, from top to bottom:

- the zone name and your name as the annotator
- the current item, for example "Flag: stub from stub (labels), flag 12, pending"
- the item's place in the queue, for example "Item 4 of 37"
- progress: items done, flags decided, kept tiles reviewed, and how many flags are marked `needs_qgis`
- one button per key, the tile note field, the Sentinel-2 checkbox, the filters and **Reload queue**

For each item, the map zooms to it and selects it. A flag gets a 50 m margin on every side. A tile fills the view. A selected area flag or tile is drawn as a yellow outline with no fill, so you can see the imagery inside it.

!!! note "Screenshot: QGIS with a zone project open and the review queue panel docked on the right"


## Filters and display

The three filters narrow the queue. Their values come from the zone.

- **Kind:** `proposal`, `flag` or `tile`. Kind `flag` means flags that are not proposals.
- **Flag type:** one of `gap`, `stub`, `duplicate`, `crossing`, `missing?` and `phantom?`.
- **Source:** `labels` or `overture`.

The filtered queue keeps the usual order. A flag type or a source filter leaves out every tile. To see the whole queue again, set each filter back to its "Any" value.

Below the filters, the panel lists any hidden detectors. The team lead hides a detector in the detector config, and its flags then stay out of the queue and out of progress. You cannot show a hidden detector from the panel.

**Reload queue** reads the queue again from the zone.

On each item, the panel turns these layers back on: NGI aerial, raw labels, labels, both flag layers and tiles. A layer you hid comes back on the next item. The Sentinel-2 RGB starts off and follows the checkbox and `S`. It lies above the aerial imagery, so while it is on, it hides the aerial imagery.

!!! note "Screenshot: the panel's filters set to one flag type, with the hidden detectors line below them"


## Proposals

A proposal is a `gap` flag. It is a pink diamond at a road end that dangles within 30 m of another road. Its connector is a dashed magenta line in the labels layer, from the dangling end to the other road.

Look at the aerial imagery under the connector. Decide whether a road joins the two there.

- **The road joins.** Press `A`. The connector becomes a road with `status = added` and `defect = gap`. Where the connector ends in the middle of the other road, that road gets a new vertex, so the road graph joins there.
- **No road joins.** Press `X`. The connector is removed from the labels layer.
- **A road joins, but not along the connector.** Press `Q` to mark it `needs_qgis`. The connector stays as a proposal, and you draw the right road later in QGIS.

!!! note "Screenshot: the panel on a gap proposal, with the dashed magenta connector selected on the map"

## Flags

The panel names the flag type. Each type has its own colour and marker on the map. Press `R` when the defect is real, `N` when it is not, and `Q` when the fix needs plain QGIS editing.

For `R` on a flag that does not delete anything, fix the labels by hand first, then press `R`. The panel saves your edit before it writes the decision. [Clean by hand in QGIS](clean-by-hand.md) shows how to keep, modify, add and delete a road.

### `stub`

A red circle at the dead end of a branch shorter than 30 m.

- **A stub is a defect.** Press `R`. Every row of the branch gets `status = deleted` and `defect = stub`. The panel lists the label rows it deleted.
- **The road really ends there.** Press `N`.

!!! warning "A stub can delete the far piece of a split road"
    `R` deletes every road row whose `raw_id` the flag names. When you split a road in QGIS, both pieces keep the same `raw_id`. If you split this branch's road earlier, check the other piece after you press `R`.

!!! note "Screenshot: the panel on a stub flag, with the red circle selected at the dead end"

### `duplicate`

A purple square at the middle of a road row that lies within 10 m of one other row over most of its length.

- **The two rows are one road.** Delete the extra row by hand, then press `R`. On a `duplicate` flag, `R` deletes nothing by itself.
- **The rows are two carriageways of one dual road.** Press `N`.

### `crossing`

A brown triangle where two roads cross at a point that is not a vertex of both.

- **The roads meet at a junction.** Add a shared vertex at the crossing by hand, then press `R`.
- **One road is a bridge or an underpass.** Press `N`.

### `missing?`

A red area where Overture has a road and the labels have none within 15 m.

- **The aerial imagery shows a road.** Draw it from the aerial imagery, then press `R`. Never copy the Overture geometry.
- **No road is there, or it is a faint vehicle track.** Press `N`.

### `phantom?`

A purple area over a stretch of label with no Overture road within 15 m.

- **No road is there.** Press `R`. The named rows that lie wholly inside the area get `status = deleted` and `defect = phantom`. A row that runs outside the area stays. Check what is left.
- **The road is there, but the label is off to one side.** That is a misalignment. Press `Q`, then move the road in QGIS. Do not press `R`, because it deletes the road.
- **The label matches the road.** Press `N`.

If none of the named rows lies wholly inside the area, `R` writes nothing. The panel shows the refusal in red and tells you to mark the flag `needs_qgis`. Press `Q`, then split the road in QGIS and delete the phantom piece.

!!! note "Screenshot: the panel on a phantom? flag, with the purple area outlined in yellow"

### Temporal mismatch

When the road is in the aerial imagery and not in the 2020 Sentinel-2 imagery, or the other way round, the Sentinel-2 imagery decides. Press `S` to show the Sentinel-2 RGB over the aerial imagery. The Sentinel-2 false-colour layer is not on a key. Switch it on in the **Layers** panel.

## Kept tiles

After the flags, the queue steps through every kept tile. A kept tile has a cyan outline.

1. Look over the whole tile for defects that no detector flagged.
2. Fix any you find by hand.
3. To leave a note, type it in the tile note field.
4. Press `T`, or press Enter in the note field, to mark the tile reviewed.

Letters you type in the note field go into the note, not to the panel's keys. If you review a tile again without a note, the first note stays.

!!! note "Screenshot: the panel on a kept tile, with the tile outlined in yellow and a note typed in the note field"

## Keys

| Key | Item | What it does |
| --- | --- | --- |
| `A` | Proposal | Accept. The connector becomes an added road |
| `X` | Proposal | Reject. The connector is removed |
| `R` | Flag | Real. On a `stub` or `phantom?` flag, it also deletes the named roads |
| `N` | Flag | Not a defect |
| `Q` | Proposal or flag | Needs QGIS. Leaves the fix for plain QGIS editing |
| `T` | Kept tile | Reviewed, with the note if you typed one |
| Enter | Kept tile, in the note field | Reviewed, with the note |
| `B` | Any | Back to the previous item |
| `F` | Any | Forward to the next item |
| `S` | Any | Sentinel-2 RGB on or off |

Each key also has a button in the panel. A key that does not fit the current item writes nothing, and the panel says so, for example "X does not apply to a flag." The panel ignores keys pressed with Ctrl, Alt or Shift, and QGIS gets them.

### The keys need the panel to have focus

The panel takes its keys only while focus is inside it. While it has focus, its keys come before QGIS's own shortcuts. QGIS binds `S` to snapping, `T` to tracing and `R` to stream digitizing.

A click on the map moves focus to the map. The keys then go back to QGIS, so `S` toggles snapping instead of Sentinel-2. Click the panel again before your next key.

## Saving and undo

Before it writes each decision or tile review, the panel saves every unsaved edit on the zone's layers. The layers stay in edit mode. After the write, the panel reloads the layers, so an accepted connector shows as a road at once. You can edit between items without pressing **Save Layer Edits**.

!!! warning "A decision clears undo history"
    Saving clears QGIS's undo history for those layers. After a decision, Ctrl+Z cannot undo an edit you made before it.

If QGIS cannot save your edits, the panel writes nothing and shows QGIS's errors in red. The panel saves layer by layer, so one layer can be saved before another fails.

Every write records your computer account's full name as the annotator. If the account has no full name, the panel refuses to write and says so. [Setup](setup.md) shows how to set one.

To change a decision:

1. Press `B` until you reach the item.
2. Press the key for the new decision.

The panel undoes the old decision first. A road the old `R` deleted comes back, and a connector you accepted turns back into a proposal. A road you edited in QGIS after the old decision keeps your edit.

`B` and `F` write no decision. A decision always takes you to the first item that is not done. So `F` past a pending item skips it, and your next decision brings you back to it.

## Items that need QGIS

Mark an item `needs_qgis` with `Q` when one key cannot fix it:

- a connector that runs along the wrong line
- a misaligned road under a `phantom?` flag
- a `phantom?` flag that `R` refuses
- a case you are unsure about and want to come back to

A `needs_qgis` item counts as done, so the queue moves on. On the map, the flag keeps its type colour and its label reads, for example, "stub · needs_qgis". A flag decided `real` or `not_defect` turns grey.

When you have finished the queue, list the flags you marked. Run this from the repo, with the repo's Python:

```sh
python -m sentinel2data.cli needs-qgis --zones-dir <zones folder> --zone <zone>
```

It prints one line per flag, with a point on the flag in the zone's coordinate system, for example `Z: flag 12 stub at 512340.0, 6249050.0`. Go to each point in QGIS and fix the labels by hand, as [Clean by hand in QGIS](clean-by-hand.md) describes.
