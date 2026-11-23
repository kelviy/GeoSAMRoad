# Review queue

How the review core in `sentinel2data.cleaning.review` steps an annotator through a zone's flags and kept tiles, and writes each decision into the zone GeoPackage. `sentinel2data.cli progress` reports how far each zone has got, and `sentinel2data.cli needs-qgis` lists the flags left for plain QGIS editing.

> **Status, 2026-11-23.** KEL-52 built the core: the queue, flag decisions, tile review and progress. KEL-54 added going back to change a decision, resuming, filters, hidden detectors and the `needs_qgis` list. Accepting and rejecting proposals is KEL-56, and the precision report is KEL-55. The QGIS plugin that will sit on top does not exist yet. [detectors.md](detectors.md) describes where the flags come from.

## Core idea

The review queue is every flag in the zone, then every kept tile, each swept across the zone in tile order. The annotator works through it one item at a time. A flag is marked `real`, `not_defect` or `needs_qgis`, and a kept tile is marked reviewed, with an optional note. `real` on a stub or `phantom?` flag also deletes the roads it names, so a simple deletion costs one key. A deletion only sets `status = deleted` and never removes a row. The annotator can step back to an earlier item and change its decision, which first undoes what the old decision deleted. The core keeps no state of its own. Every call reads the GeoPackage again, and every decision, deletion log and queue position goes straight into it with the annotator's name, so a crash loses nothing and reopening the zone resumes where the annotator stopped.

## How it works

1. **Build the queue.** `build_queue` reads the `flags` and `tiles` layers. Tiles are sorted by `row`, then `col`, so row 0, the northern row, comes first. Each flag goes with the tile nearest its representative point. Flags are ordered by that tile, then north to south, then west to east, then by fid. After the flags come the `kept` tiles, in the same tile order. Flags from hidden detectors are left out, and a `QueueFilter` can narrow the queue further.
2. **Find the current item.** `current_item` is the stored position when that item is in the queue. Otherwise it is the first item that is not done. A flag is done once its decision is anything other than `pending`, `needs_qgis` included. A tile is done once `reviewed` is set.
3. **Give its extent.** Each item carries a map extent: a flag's bounds grown by `FLAG_MARGIN_M`, 50 m, on every side, or a tile's own bounds.
4. **Decide a flag.** `decide_flag` writes `decision` and `annotator` on the flag. For `real` on a deleting flag type, it sets the label rows to delete to `status = deleted`, with the matching defect and the annotator, in the same transaction. It returns the labels fids it deleted.
5. **Step back or forward.** `go_back` moves to the item before the current one, and `go_forward` to the item after it. Each stores the new item as the position. Every decision and tile review clears the position, so after a change the annotator is back at the first item not done.
6. **Review a tile.** `review_tile` sets `reviewed`, `note` and `annotator` on a kept tile. Reviewing it again replaces the annotator. It replaces the note only when a new one is given, so a second review without a note keeps the first note.
7. **Report progress.** `progress` counts decided flags out of all queued flags and reviewed tiles out of kept tiles. Hidden detectors' flags are not counted. The `progress` command prints one line per zone, for example `Z: 3/9 done, 6 remaining (flags 2/7 decided, kept tiles 1/2 reviewed)`.
8. **List the `needs_qgis` flags.** `needs_qgis_items` returns every flag marked `needs_qgis`, in queue order, each with a point on it and its extent. The `needs-qgis` command prints one line per flag, for example `Z: flag 12 stub at 512340.0, 6249050.0`, in the zone's CRS.

## Which rows `real` deletes

`DELETING_FLAG_TYPES` maps `stub` to the defect `stub` and `phantom?` to the defect `phantom`. Only road rows count, meaning rows with status `kept`, `modified` or `added`, and only rows whose `raw_id` is in the flag's `raw_ids`.

- **`stub` deletes every named row.** A stub flag names the rows of one short branch, so all of it goes.
- **`phantom?` deletes only the named rows that lie wholly inside the flag area.** The second-source detector names every row that holds an unmatched stretch. A long road whose unmatched stretch is a short part of it is named too, and deleting it whole would delete road that Overture does match. So a row that runs outside the area stays `kept`.
- **Nothing deletable is refused.** If a `phantom?` flag names road rows but none lies inside its area, `decide_flag` writes nothing and raises `NothingToDelete`, whose message says to mark it `needs_qgis`. The annotator then splits the road in QGIS.
- **Rows already deleted are skipped.** A named row that is no longer a road keeps its own `defect` and `annotator`. If every named row is already deleted, `real` records the decision and deletes nothing.

## Changing a decision

`decide_flag` on a decided flag replaces its decision and annotator. Before it applies the new decision, it undoes the old one.

- **Each deletion is logged.** When `real` deletes a row, the same transaction writes the row's previous `status`, `defect` and `annotator` to the `flag_deletions` table, keyed by flag fid and labels fid.
- **Undo puts the row back as it was.** It restores the logged values and drops the log rows. A `kept` row goes back to `kept` with no defect. A `modified` row goes back to `modified` with its own defect and annotator.
- **A row changed since stays.** Undo restores a row only while it is still `deleted` with the flag's defect and the annotator who decided `real`. A row someone edited in QGIS after the deletion keeps its edit.
- **A row another flag still deletes stays deleted.** If another flag decided `real` with a deleting type names the row's `raw_id`, the row stays `deleted`. It takes that flag's defect and annotator, and its log entry moves to that flag, so changing that flag later restores it.
- **The same decision again changes nothing,** including the annotator.
- **Concurrent decisions are kept.** The flag `UPDATE` requires the decision to be the one just read, otherwise it raises `FlagChangedSinceRead` and writes nothing. In the same way, each label `UPDATE` requires the row to still be a road, so a row deleted in QGIS after the read keeps its own defect and annotator.

## Filters and hidden detectors

- **`QueueFilter` has three optional fields.** `kind` is `proposal`, `flag` or `tile`. `flag_type` is one of the flag types, and `source` is one of `FLAG_SOURCES`, `labels` or `overture`. Every field given must match, so a flag type or source keeps no tiles. The kinds do not overlap. A proposal is a flag with a `proposal_fid`, and kind `flag` means the other flags. The filtered queue keeps the default order. An unknown kind, flag type or source raises `ValueError`.
- **Hidden detectors** are set in the detector config. Each detector section has `hidden: false` next to `enabled`. `hidden_detectors` reads the config of the zone's latest `detect` run from `detector_runs`. A hidden detector still runs and writes its flags. Only the queue and `progress` leave them out. To hide or show one, the team lead edits the config and runs `detect` again.
- **`needs_qgis_items` ignores hiding.** A flag marked `needs_qgis` before its detector was hidden still needs fixing.

## Details that matter

- **A decision clears the position.** After a change made by going back, the annotator lands on the first item not done again. A step forward past a pending item works as a skip, and the next decision brings the annotator back to the skipped item.
- **The position names a layer and fid.** `review_position` holds at most one row. A position whose item has left the queue, because a filter or hiding drops it or a re-run removed the flag, is ignored. The annotator then lands on the first item not done. The position is per zone, not per annotator or per filter.
- **`go_back` when every item is done** goes to the last item. `go_back` at the first item and `go_forward` at the last return None and leave the position as it is.
- **The two review tables are created on first use.** `_ensure_review_tables` creates `flag_deletions` and `review_position` and registers them in `gpkg_contents` as attribute tables, so GDAL and QGIS list them. Zones packaged before KEL-54 need no repackaging.
- **Refusals write nothing.** A `pending` or unknown decision, a blank annotator, an unknown flag fid, and a tile that is not `kept` all raise `ValueError` before any write.
- **Writes go through plain SQLite.** GDAL's R-tree triggers name `ST_IsEmpty`, `ST_MinX`, `ST_MaxX`, `ST_MinY` and `ST_MaxY`, and SQLite compiles every trigger on a table for any `UPDATE`. So `_writing` registers stand-ins for those functions before it writes. The triggers that call them fire only when a geometry or a fid changes, and the core never changes either, so the stand-ins are never called. They raise if they are. `detect` avoids `UPDATE` altogether, as [detectors.md](detectors.md) explains.
- **An open zone is not refused.** `detect` refuses a zone with a `-wal` file. The review core does not, because KEL-11 has the QGIS plugin record decisions into the GeoPackage while QGIS has the zone open. SQLite locks the file for each write, so the two writers never interleave a transaction.
- **Empty values read as not done.** A flag whose `decision` is NULL is pending, and a tile whose `reviewed` is NULL is not reviewed.
- **The annotator name loses leading and trailing spaces** and is otherwise stored as given. Nothing checks it against the zone's owner.

## Why it's like this

- **A deletion log, not derivation.** The ticket's agent notes suggest deriving the undo from the flag's type and `raw_ids`. That gives the rows but not what they were. A row the stub flag deleted may have been `modified` with its own defect and annotator, and that is lost once the row says `deleted, stub`. So `real` logs the previous values.
- **Hidden in the detector config.** KEL-54's agent notes call the detector config the natural home, because the team lead sets it once for all zones. KEL-54 reads it from the latest run record so that the review core reads only the zone GeoPackage and the plugin needs no config path.
- **Filters, resume, go back, hiding and the `needs_qgis` list.** KEL-11 user stories 26, 28, 29, 34 and 24.
- **Flags, then kept tiles, each in spatial order.** KEL-11 user story 27. Proposals are meant to come first. Since KEL-53 the gap detector writes them, but until KEL-56 they are queued as ordinary flags.
- **One key per decision, and `real` applies the deletion.** KEL-11 user stories 22 and 23.
- **No separate app state.** KEL-11 user story 31: a crash must lose nothing. That is why every call reopens the file and nothing is cached between calls.
- **The annotator on every write.** KEL-11 user story 30, for the agreement study.
- **`status = deleted`, never a removed row.** KEL-5 user story 19 says every raw row must survive for the raw versus cleaned comparison. [zone-cleaning.md](zone-cleaning.md) covers this.
- **No QGIS import.** KEL-11 makes the core plain Python so that a local web app could replace the plugin if the plugin turns out to be impractical. A test runs a fresh interpreter, imports the core and checks that no `qgis` or PyQt module got loaded.
- **Covered rows only for `phantom?`.** The ticket's agent notes leave it open whether `real` deletes whole rows. Deleting whole rows would delete roads the second source matches. Refusing only when nothing is covered still lets a fully phantom road go in one key.

## Weak spots and ideas

- **A `real` decided before KEL-54 cannot be undone.** Observed in the code. It has no log rows, so changing it restores nothing and its rows stay deleted. The annotator has to restore them in QGIS.
- **Hiding needs a `detect` re-run.** By design, but it means the team lead cannot hide a detector on a zone that is open in QGIS, because `detect` refuses open zones.
- **One position for the zone.** Two annotators on one copy, or two filters, share it. This is suspected to be harmless, since a zone has one annotator.
- **Hand-over to another flag uses `raw_ids` only.** Suspected. A `phantom?` flag that names a row but does not cover it still keeps the row deleted when the other flag is undone.
- **Every detector config needs `hidden`.** Observed. The config parser rejects missing keys, so a config written before KEL-54 fails to load until `hidden` is added to all five sections.
- **A gap flag decided here leaves its connector `proposed`.** Observed. `decide_flag` records `real`, `not_defect` or `needs_qgis` on a gap flag like any other flag and does not touch its `proposed` row. Accepting or rejecting the connector is KEL-56. Until then a decided gap flag's row stays `proposed`, and the packager counts it as cleaning work.
- **Deleting by `raw_id` also catches split pieces.** Suspected. QGIS copies `raw_id` to both pieces of a split road. `real` on a stub then deletes every road row with that `raw_id`, including a long piece far from the stub. The `phantom?` coverage check guards against this, but the stub rule does not.
- **Partial deletion can go unnoticed.** Observed in a test. A `phantom?` flag over the end of a long road deletes the rows inside it, records `real`, and leaves the rest. The caller only learns which rows were deleted, not which named rows stayed.
- **Added rows are never deleted.** By design: they have no `raw_id`, so no flag names them.
- **The whole queue is rebuilt on every call.** `current_item` and `progress` build the full queue each time. On the densest real zone, CapeTown_Fynbos_-33p96_18p61_Urban, with no flags, that takes 27 ms. Not measured with thousands of flags.
- **A flag's tile is the nearest tile of any status.** A flag next to a dropped tile can sort with that tile, not with the kept tile it touches. The order is still deterministic.

## Code map

- `cleaning/review.py`, `build_queue`, `FlagItem`, `TileItem`: the queue and its items.
- `cleaning/review.py`, `QueueFilter`, `hidden_detectors`: filters and hiding.
- `cleaning/review.py`, `current_item`, `go_back`, `go_forward`, `_store_position`: where the annotator is, stored in `review_position`.
- `cleaning/review.py`, `progress`, `Progress`: how far through.
- `cleaning/review.py`, `decide_flag`, `_rows_to_delete`, `DELETING_FLAG_TYPES`: flag decisions and the deletion rule.
- `cleaning/review.py`, `_delete_row`, `_undo_deletions`, `_ensure_review_tables`: the deletion log in `flag_deletions` and its undo.
- `cleaning/review.py`, `needs_qgis_items`: the `needs_qgis` list.
- `cleaning/review.py`, `review_tile`: tile review.
- `cleaning/review.py`, `_writing`: the SQLite write path with the trigger stand-ins.
- `cleaning/contract.py`, `parse_raw_ids`: reads a flag's `raw_ids` field.
- `cli.py`, `progress` and `needs_qgis`: the commands. `_packaged_zones` picks the zones for them and for `detect`.
- `tests/sentinel2data/test_review.py`: end-to-end tests on the packaged synthetic zone after `detect`. Phantom flags are hand-written rows.
