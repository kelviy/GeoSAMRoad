# Review queue

How the review core in `sentinel2data.cleaning.review` steps an annotator through a zone's proposals, flags and kept tiles, and writes each decision into the zone GeoPackage. `sentinel2data.cli progress` reports how far each zone has got, `sentinel2data.cli needs-qgis` lists the flags left for plain QGIS editing, and `sentinel2data.cli precision` reports how often each detector's flags turned out real.

> **Status, 2026-11-23.** KEL-52 built the core: the queue, flag decisions, tile review and progress. KEL-54 added going back to change a decision, resuming, filters, hidden detectors and the `needs_qgis` list. KEL-55 added the precision report. KEL-56 added accepting and rejecting proposals. KEL-57 added the QGIS plugin, which [review-plugin.md](review-plugin.md) describes. [detectors.md](detectors.md) describes where the flags come from.

## Core idea

The review queue is every proposal in the zone, then every other flag, then every kept tile, each swept across the zone in tile order. The annotator works through it one item at a time. A flag is marked `real`, `not_defect` or `needs_qgis`, and a kept tile is marked reviewed, with an optional note. `real` on a stub or `phantom?` flag also deletes the roads it names, so a simple deletion costs one key. `real` on a proposal accepts it, and its connector becomes a road. `not_defect` rejects it, and the connector is removed. A deletion only sets `status = deleted` and never removes a row. The annotator can step back to an earlier item and change its decision, which first undoes what the old decision deleted. The core keeps no state of its own. Every call reads the GeoPackage again, and every decision, deletion log and queue position goes straight into it with the annotator's name, so a crash loses nothing and reopening the zone resumes where the annotator stopped.

## How it works

1. **Build the queue.** `build_queue` reads the `flags` and `tiles` layers. Tiles are sorted by `row`, then `col`, so row 0, the northern row, comes first. Each flag goes with the tile nearest its representative point. Proposals come before the other flags. Within each group, flags are ordered by that tile, then north to south, then west to east, then by fid. After the flags come the `kept` tiles, in the same tile order. Flags from hidden detectors are left out, and a `QueueFilter` can narrow the queue further.
2. **Find the current item.** `current_item` is the stored position when that item is in the queue. Otherwise it is the first item that is not done. A flag is done once its decision is anything other than `pending`, `needs_qgis` included. A tile is done once `reviewed` is set.
3. **Give its extent.** Each item carries a map extent: a flag's bounds grown by `FLAG_MARGIN_M`, 50 m, on every side, or a tile's own bounds.
4. **Decide a flag.** `decide_flag` writes `decision` and `annotator` on the flag. For `real` on a deleting flag type, it sets the label rows to delete to `status = deleted`, with the matching defect and the annotator, in the same transaction. It returns the labels fids it deleted. On a proposal, `real` accepts it and `not_defect` rejects it, as [Accepting and rejecting proposals](#accepting-and-rejecting-proposals) describes.
5. **Step back or forward.** `go_back` moves to the item before the current one, and `go_forward` to the item after it. Each stores the new item as the position. Every decision and tile review clears the position, so after a change the annotator is back at the first item not done.
6. **Review a tile.** `review_tile` sets `reviewed`, `note` and `annotator` on a kept tile. Reviewing it again replaces the annotator. It replaces the note only when a new one is given, so a second review without a note keeps the first note.
7. **Report progress.** `progress` counts decided flags out of all queued flags and reviewed tiles out of kept tiles. Hidden detectors' flags are not counted. The `progress` command prints one line per zone, for example `Z: 3/9 done, 6 remaining (flags 2/7 decided, kept tiles 1/2 reviewed)`.
8. **List the `needs_qgis` flags.** `needs_qgis_items` returns every flag marked `needs_qgis`, in queue order, each with a point on it and its extent. The `needs-qgis` command prints one line per flag, for example `Z: flag 12 stub at 512340.0, 6249050.0`, in the zone's CRS.
9. **Report precision.** `precision.zone_counts` counts each zone's flags by detector, flag type and decision, and `precision.pooled` sums those counts over zones. The `precision` command prints one line per zone, detector and type, then the same lines for the zones together, labelled `All zones` or, under `--zone`, `N selected zone(s)`, for example `Z: stub:stub 66.7% (2 real / 3 judged), needs_qgis 1, pending 4`.

## Which rows `real` deletes

`DELETING_FLAG_TYPES` maps `stub` to the defect `stub` and `phantom?` to the defect `phantom`. Only road rows count, meaning rows with status `kept`, `modified` or `added`, and only rows whose `raw_id` is in the flag's `raw_ids`.

- **`stub` deletes every named row.** A stub flag names the rows of one short branch, so all of it goes.
- **`phantom?` deletes only the named rows that lie wholly inside the flag area.** The second-source detector names every row that holds an unmatched stretch. A long road whose unmatched stretch is a short part of it is named too, and deleting it whole would delete road that Overture does match. So a row that runs outside the area stays `kept`.
- **Nothing deletable is refused.** If a `phantom?` flag names road rows but none lies inside its area, `decide_flag` writes nothing and raises `NothingToDelete`, whose message says to mark it `needs_qgis`. The annotator then splits the road in QGIS.
- **Rows already deleted are skipped.** A named row that is no longer a road keeps its own `defect` and `annotator`. If every named row is already deleted, `real` records the decision and deletes nothing.

## Accepting and rejecting proposals

A proposal is a flag whose `proposal_fid` points at a `status = proposed` row in `labels`. Today only the gap detector writes them, each with a connector line. The flag records the decision and the annotator as for any flag. `cleaning/proposals.py` changes `labels` in the same transaction.

- **Accept (`real`)** turns the `proposed` row into `status = added` with defect `gap` and the annotator. Its geometry, class and fid stay the same.
- **Accept also adds vertices.** The road graph that `generate` builds joins lines only at shared vertices. A connector end that lies within 1e-6 m (`ON_ROAD_M`) of a road row, but not on one of its vertices, is inserted into that row as a new vertex, on its nearest segment. Z is interpolated when the row has Z. A `kept` row so edited becomes `modified` with defect `gap` and the annotator. A `modified` or `added` row keeps its status, defect and annotator. On the synthetic gap case both ends already lie on vertices, so only the connector changes.
- **Reject (`not_defect`)** removes the `proposed` row from `labels`. So `labels` only holds rows someone decided on, apart from pending proposals.
- **`needs_qgis` leaves the row `proposed`.** The annotator draws the fix in QGIS.
- **A row changed in QGIS is left alone.** If the proposed row is no longer `proposed` when the decision is written, the decision is recorded and `labels` is not touched.
- **Re-running `detect` leaves decided proposals alone.** A decided flag is never replaced, and its `flag_key` stops the same gap getting a new flag. After an accept, the gap is closed, so the detector does not find it again.
- **The zone counts as cleaning work.** A decided flag and an `added` row both stop `package-zones` without `--overwrite`.

## Changing a decision

`decide_flag` on a decided flag replaces its decision and annotator. Before it applies the new decision, it undoes the old one.

- **Each deletion is logged.** When `real` deletes a row, the same transaction writes the row's previous `status`, `defect` and `annotator` to the `flag_deletions` table, keyed by flag fid and labels fid.
- **Undo puts the row back as it was.** It restores the logged values and drops the log rows. A `kept` row goes back to `kept` with no defect. A `modified` row goes back to `modified` with its own defect and annotator.
- **A row changed since stays.** Undo restores a row only while it is still `deleted` with the flag's defect and the annotator who decided `real`. A row someone edited in QGIS after the deletion keeps its edit.
- **A row another flag still deletes stays deleted.** If another flag decided `real` with a deleting type names the row's `raw_id`, the row stays `deleted`. It takes that flag's defect and annotator, and its log entry moves to that flag, so changing that flag later restores it.
- **Proposal edits are logged too.** `proposal_rows` keeps the whole proposed row, as JSON, for each accept or reject. `connector_vertices` keeps one entry per vertex an accept added, with the row's previous status, defect and annotator when the accept made it `modified`.
- **Undoing a proposal puts it back.** A rejected row is inserted again with its old fid. An accepted row goes back to `proposed` while it is still `added` for `gap` by the annotator who accepted it. Each vertex the accept added is removed from its road. A road the accept made `modified` goes back to its old values once no other accept's vertex remains on it.
- **Accepts on one road are undone one vertex at a time.** Two connectors can end on the same road. Undoing the first removes only its own vertex, and the road stays `modified` for the second, with the second flag's annotator. If another accepted connector ends exactly on the vertex being removed, the vertex stays and its log entry moves to that connector's flag. An earlier version restored whole-row snapshots instead. On four real zones, undoing in a different order from the accepts left extra vertices.
- **The same decision again changes nothing,** including the annotator.
- **Concurrent decisions are kept.** The flag `UPDATE` requires the decision to be the one just read, otherwise it raises `FlagChangedSinceRead` and writes nothing. In the same way, each label `UPDATE` requires the row to still be a road, so a row deleted in QGIS after the read keeps its own defect and annotator.

## Filters and hidden detectors

- **`QueueFilter` has three optional fields.** `kind` is `proposal`, `flag` or `tile`. `flag_type` is one of the flag types, and `source` is one of `FLAG_SOURCES`, `labels` or `overture`. Every field given must match, so a flag type or source keeps no tiles. The kinds do not overlap. A proposal is a flag with a `proposal_fid`, and kind `flag` means the other flags. The filtered queue keeps the default order. An unknown kind, flag type or source raises `ValueError`.
- **Hidden detectors** are set in the detector config. Each detector section has `hidden: false` next to `enabled`. `hidden_detectors` reads the config of the zone's latest `detect` run from `detector_runs`. A hidden detector still runs and writes its flags. Only the queue and `progress` leave them out. To hide or show one, the team lead edits the config and runs `detect` again.
- **`needs_qgis_items` ignores hiding.** A flag marked `needs_qgis` before its detector was hidden still needs fixing.

## Detector precision

Detector precision is `real / (real + not_defect)` for one detector and flag type. The paper reports it, and the early check on about 30 flags per type in one Urban, one PeriUrban and one Rural zone uses it to pick the detectors to hide.

- **`needs_qgis` is left out of the share** and counted on its own. It sends a flag to plain QGIS editing and does not record whether the flag was a defect. `NothingToDelete` tells the annotator to mark a real `phantom?` flag it can't delete `needs_qgis`. If most `needs_qgis` flags are real, leaving them out lowers the share. The report prints the count, so a reader can recompute the share either way.
- **`real` and `not_defect` flags are the judged flags.** The share is `real` over judged. `progress` counts `needs_qgis` as decided, so judged is fewer than decided.
- **Pending flags are left out of the share** and counted on their own. A NULL decision counts as pending.
- **Proposals have no rule of their own.** An accepted proposal marks its flag `real` and a rejected one `not_defect`, so they count like any other flag.
- **The overall lines pool the flags.** They sum the counts over the zones and then divide, so a zone with many flags weighs more than one with few. The command prints this rule in its first line.
- **Hidden detectors count.** The report reads every flag in the `flags` layer, because the precision check is what decides which detectors to hide.
- **An unknown decision stops the report.** A value outside `pending`, `real`, `not_defect` and `needs_qgis`, which only a hand edit in QGIS can write, raises `ValueError` with the flag fids, and the command prints it and exits 1.

## Details that matter

- **A decision clears the position.** After a change made by going back, the annotator lands on the first item not done again. A step forward past a pending item works as a skip, and the next decision brings the annotator back to the skipped item.
- **The position names a layer and fid.** `review_position` holds at most one row. A position whose item has left the queue, because a filter or hiding drops it or a re-run removed the flag, is ignored. The annotator then lands on the first item not done. The position is per zone, not per annotator or per filter.
- **`go_back` when every item is done** goes to the last item. `go_back` at the first item and `go_forward` at the last return None and leave the position as it is.
- **The review tables are created on first use.** `_ensure_review_tables` creates `flag_deletions`, `review_position`, `proposal_rows` and `connector_vertices` and registers them in `gpkg_contents` as attribute tables, so GDAL and QGIS list them. Zones packaged before KEL-54 need no repackaging.
- **Refusals write nothing.** A `pending` or unknown decision, a blank annotator, an unknown flag fid, and a tile that is not `kept` all raise `ValueError` before any write.
- **Writes go through plain SQLite.** GDAL's R-tree triggers call `ST_IsEmpty`, `ST_MinX`, `ST_MaxX`, `ST_MinY` and `ST_MaxY`, which plain SQLite lacks, and SQLite compiles every trigger on a table for any `UPDATE`. Accepting a proposal changes road geometries, and undoing a reject inserts a row, so these triggers do fire. `cleaning/gpkg_geometry.py` reads and writes GeoPackage geometry blobs, and `_writing` registers working versions of those functions from it, so the R-tree stays in step. On four real zones, after accepting and undoing every proposal, the R-tree held exactly one entry per labels row. `detect` avoids `UPDATE` altogether, as [detectors.md](detectors.md) explains.
- **An open zone is not refused.** `detect` refuses a zone with a `-wal` file. The review core does not, because KEL-11 has the QGIS plugin record decisions into the GeoPackage while QGIS has the zone open. SQLite locks the file for each write, so the two writers never interleave a transaction.
- **Empty values read as not done.** A flag whose `decision` is NULL is pending, and a tile whose `reviewed` is NULL is not reviewed.
- **The annotator name loses leading and trailing spaces** and is otherwise stored as given. Nothing checks it against the zone's owner.

## Why it's like this

- **A deletion log, not derivation.** The ticket's agent notes suggest deriving the undo from the flag's type and `raw_ids`. That gives the rows but not what they were. A row the stub flag deleted may have been `modified` with its own defect and annotator, and that is lost once the row says `deleted, stub`. So `real` logs the previous values.
- **Hidden in the detector config.** KEL-54's agent notes call the detector config the natural home, because the team lead sets it once for all zones. KEL-54 reads it from the latest run record so that the review core reads only the zone GeoPackage and the plugin needs no config path.
- **Filters, resume, go back, hiding and the `needs_qgis` list.** KEL-11 user stories 26, 28, 29, 34 and 24.
- **Proposals, then flags, then kept tiles, each in spatial order.** KEL-11 user story 27.
- **Accept and reject in one key, and `labels` holds only decisions.** KEL-11 user stories 20 and 21, and KEL-56.
- **Accept adds the vertex to the other road.** KEL-11 user story 2 asks that accepting a connector reconnect the graph. KEL-53 found that 2,113 of 2,734 connectors on the 60 zones end mid-segment, where the vertex-only road graph would not join them, and [detectors.md](detectors.md) records that the accept has to add the vertex.
- **One key per decision, and `real` applies the deletion.** KEL-11 user stories 22 and 23.
- **No separate app state.** KEL-11 user story 31: a crash must lose nothing. That is why every call reopens the file and nothing is cached between calls.
- **The annotator on every write.** KEL-11 user story 30, for the agreement study.
- **`status = deleted`, never a removed row.** KEL-5 user story 19 says every raw row must survive for the raw versus cleaned comparison. [zone-cleaning.md](zone-cleaning.md) covers this.
- **Precision per detector and flag type, per zone and pooled.** KEL-11 user story 33. KEL-55's agent notes ask for pooling across zones and for the output to say so.
- **No QGIS import.** KEL-11 makes the core plain Python so that a local web app could replace the plugin if the plugin turns out to be impractical. A test runs a fresh interpreter, imports the core and checks that no `qgis` or PyQt module got loaded.
- **Covered rows only for `phantom?`.** The ticket's agent notes leave it open whether `real` deletes whole rows. Deleting whole rows would delete roads the second source matches. Refusing only when nothing is covered still lets a fully phantom road go in one key.

## Weak spots and ideas

- **A `real` decided before KEL-54 cannot be undone.** Observed in the code. It has no log rows, so changing it restores nothing and its rows stay deleted. The annotator has to restore them in QGIS.
- **Hiding needs a `detect` re-run.** By design, but it means the team lead cannot hide a detector on a zone that is open in QGIS, because `detect` refuses open zones.
- **One position for the zone.** Two annotators on one copy, or two filters, share it. This is suspected to be harmless, since a zone has one annotator.
- **Hand-over to another flag uses `raw_ids` only.** Suspected. A `phantom?` flag that names a row but does not cover it still keeps the row deleted when the other flag is undone.
- **Every detector config needs `hidden`.** Observed. The config parser rejects missing keys, so a config written before KEL-54 fails to load until `hidden` is added to all five sections.
- **Undoing an accept can leave another connector unjoined.** Observed in the code. If connector B ends mid-way along accepted connector A, accepting B adds a vertex to A. Undoing A sets it back to `proposed`, so B now ends on a row that is not a road. A keeps B's vertex until B is undone too.
- **`real` on a proposal edited in QGIS changes no label and says nothing.** Observed in a test. This happens when someone deleted the proposed row in QGIS or changed its status. `real` still records the decision, and `decide_flag` returns as usual.
- **A connector that ends near, not on, a road joins nothing.** Suspected. Accept adds a vertex only within 1e-6 m. KEL-53 measured the largest mid-segment error at 5e-10 m, but a connector moved in QGIS before acceptance could miss.
- **Re-encoded geometries are not byte-identical.** Observed. A road whose vertex is removed again is written in this module's blob layout, little-endian with an xy envelope, not GDAL's. The geometry is equal and GDAL reads it, but a byte comparison of the file shows a change.
- **Deleting by `raw_id` also catches split pieces.** Suspected. QGIS copies `raw_id` to both pieces of a split road. `real` on a stub then deletes every road row with that `raw_id`, including a long piece far from the stub. The `phantom?` coverage check guards against this, but the stub rule does not.
- **Partial deletion can go unnoticed.** Observed in a test. A `phantom?` flag over the end of a long road deletes the rows inside it, records `real`, and leaves the rest. The caller only learns which rows were deleted, not which named rows stayed.
- **Added rows are never deleted.** By design: they have no `raw_id`, so no flag names them.
- **The whole queue is rebuilt on every call.** `current_item` and `progress` build the full queue each time. On the densest real zone, CapeTown_Fynbos_-33p96_18p61_Urban, with no flags, that takes 27 ms. Not measured with thousands of flags.
- **A flag's tile is the nearest tile of any status.** A flag next to a dropped tile can sort with that tile, not with the kept tile it touches. The order is still deterministic.
- **Precision mixes detector settings.** Observed. A flag does not record the config that placed it, so a share over flags from runs with different thresholds mixes those thresholds. Tuning a detector between the early check and full cleaning leaves both kinds of flag in one share.
- **No machine-readable output.** The `precision` command prints text only. Someone copies the paper's tables from it by hand.

## Code map

- `cleaning/review.py`, `build_queue`, `FlagItem`, `TileItem`: the queue and its items.
- `cleaning/review.py`, `QueueFilter`, `hidden_detectors`: filters and hiding.
- `cleaning/review.py`, `current_item`, `go_back`, `go_forward`, `_store_position`: where the annotator is, stored in `review_position`.
- `cleaning/review.py`, `progress`, `Progress`: how far through.
- `cleaning/review.py`, `decide_flag`, `_rows_to_delete`, `DELETING_FLAG_TYPES`: flag decisions and the deletion rule.
- `cleaning/review.py`, `_delete_row`, `_undo_deletions`, `_ensure_review_tables`: the deletion log in `flag_deletions` and its undo.
- `cleaning/review.py`, `needs_qgis_items`: the `needs_qgis` list.
- `cleaning/review.py`, `review_tile`: tile review.
- `cleaning/precision.py`, `zone_counts`, `pooled`, `DecisionCounts`: detector precision.
- `cleaning/proposals.py`, `accept`, `reject`, `undo`: proposal decisions and their logs in `proposal_rows` and `connector_vertices`.
- `cleaning/gpkg_geometry.py`, `decode`, `encode`, `register_rtree_functions`: GeoPackage geometry blobs and the R-tree trigger functions.
- `cleaning/review.py`, `_writing`: the SQLite write path.
- `cleaning/contract.py`, `parse_raw_ids`: reads a flag's `raw_ids` field.
- `cli.py`, `progress`, `needs_qgis` and `precision`: the commands. `_packaged_zones` picks the zones for them and for `detect`.
- `tests/sentinel2data/test_review.py`: end-to-end tests on the packaged synthetic zone after `detect`. Phantom flags are hand-written rows.
- `tests/sentinel2data/test_review_proposals.py`: accepting, rejecting and undoing proposals on the synthetic zone after `detect`.
- `tests/sentinel2data/test_precision.py`: precision on hand-written decision rows in the packaged synthetic zones.
