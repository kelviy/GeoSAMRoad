# Review queue

How the review core in `sentinel2data.cleaning.review` steps an annotator through a zone's flags and kept tiles, and writes each decision into the zone GeoPackage. `sentinel2data.cli progress` reports how far each zone has got.

> **Status, 2026-11-23.** KEL-52 built the core: the queue, flag decisions, tile review and progress. Going back to change a decision, resuming, filters and hidden detectors are KEL-54. Accepting and rejecting proposals is KEL-56, and the precision report is KEL-55. The QGIS plugin that will sit on top does not exist yet. [detectors.md](detectors.md) describes where the flags come from.

## Core idea

The review queue is every flag in the zone, then every kept tile, each swept across the zone in tile order. The annotator works through it one item at a time. A flag is marked `real`, `not_defect` or `needs_qgis`, and a kept tile is marked reviewed, with an optional note. `real` on a stub or `phantom?` flag also deletes the roads it names, so a simple deletion costs one key. A deletion only sets `status = deleted` and never removes a row. The core keeps no state of its own. Every call reads the GeoPackage again, and every decision goes straight into it with the annotator's name, so a crash loses nothing and the file is the only record.

## How it works

1. **Build the queue.** `build_queue` reads the `flags` and `tiles` layers. Tiles are sorted by `row`, then `col`, so row 0, the northern row, comes first. Each flag goes with the tile nearest its representative point. Flags are ordered by that tile, then north to south, then west to east, then by fid. After the flags come the `kept` tiles, in the same tile order.
2. **Find the current item.** `current_item` is the first item that is not done. A flag is done once its decision is anything other than `pending`, `needs_qgis` included. A tile is done once `reviewed` is set.
3. **Give its extent.** Each item carries a map extent: a flag's bounds grown by `FLAG_MARGIN_M`, 50 m, on every side, or a tile's own bounds.
4. **Decide a flag.** `decide_flag` writes `decision` and `annotator` on the flag. For `real` on a deleting flag type, it sets the label rows to delete to `status = deleted`, with the matching defect and the annotator, in the same transaction. It returns the labels fids it deleted.
5. **Review a tile.** `review_tile` sets `reviewed`, `note` and `annotator` on a kept tile. Reviewing it again replaces the annotator. It replaces the note only when a new one is given, so a second review without a note keeps the first note.
6. **Report progress.** `progress` counts decided flags out of all flags and reviewed tiles out of kept tiles. The `progress` command prints one line per zone, for example `Z: 3/9 done, 6 remaining (flags 2/7 decided, kept tiles 1/2 reviewed)`.

## Which rows `real` deletes

`DELETING_FLAG_TYPES` maps `stub` to the defect `stub` and `phantom?` to the defect `phantom`. Only road rows count, meaning rows with status `kept`, `modified` or `added`, and only rows whose `raw_id` is in the flag's `raw_ids`.

- **`stub` deletes every named row.** A stub flag names the rows of one short branch, so all of it goes.
- **`phantom?` deletes only the named rows that lie wholly inside the flag area.** The second-source detector names every row that holds an unmatched stretch. A long road whose unmatched stretch is a short part of it is named too, and deleting it whole would delete road that Overture does match. So a row that runs outside the area stays `kept`.
- **Nothing deletable is refused.** If a `phantom?` flag names road rows but none lies inside its area, `decide_flag` writes nothing and raises `NothingToDelete`, whose message says to mark it `needs_qgis`. The annotator then splits the road in QGIS.
- **Rows already deleted are skipped.** A named row that is no longer a road keeps its own `defect` and `annotator`. If every named row is already deleted, `real` records the decision and deletes nothing.

## Details that matter

- **A decided flag is not decided again.** `decide_flag` raises `FlagAlreadyDecided`. The `UPDATE` also requires the flag to still be pending, so a decision written by someone else after the read is kept. In the same way, each label `UPDATE` requires the row to still be a road, so a row deleted in QGIS after the read keeps its own defect and annotator. Changing a decision is KEL-54, because undoing a `real` must also restore the label rows.
- **Refusals write nothing.** A `pending` or unknown decision, a blank annotator, an unknown flag fid, and a tile that is not `kept` all raise `ValueError` before any write.
- **Writes go through plain SQLite.** GDAL's R-tree triggers name `ST_IsEmpty`, `ST_MinX`, `ST_MaxX`, `ST_MinY` and `ST_MaxY`, and SQLite compiles every trigger on a table for any `UPDATE`. So `_writing` registers stand-ins for those functions before it writes. The triggers that call them fire only when a geometry or a fid changes, and the core never changes either, so the stand-ins are never called. They raise if they are. `detect` avoids `UPDATE` altogether, as [detectors.md](detectors.md) explains.
- **An open zone is not refused.** `detect` refuses a zone with a `-wal` file. The review core does not, because KEL-11 has the QGIS plugin record decisions into the GeoPackage while QGIS has the zone open. SQLite locks the file for each write, so the two writers never interleave a transaction.
- **Empty values read as not done.** A flag whose `decision` is NULL is pending, and a tile whose `reviewed` is NULL is not reviewed.
- **The annotator name loses leading and trailing spaces** and is otherwise stored as given. Nothing checks it against the zone's owner.

## Why it's like this

- **Flags, then kept tiles, each in spatial order.** KEL-11 user story 27. Proposals are meant to come first. None exist yet, so they will need their own place in the order when KEL-56 lands.
- **One key per decision, and `real` applies the deletion.** KEL-11 user stories 22 and 23.
- **No separate app state.** KEL-11 user story 31: a crash must lose nothing. That is why every call reopens the file and nothing is cached between calls.
- **The annotator on every write.** KEL-11 user story 30, for the agreement study.
- **`status = deleted`, never a removed row.** KEL-5 user story 19 says every raw row must survive for the raw versus cleaned comparison. [zone-cleaning.md](zone-cleaning.md) covers this.
- **No QGIS import.** KEL-11 makes the core plain Python so that a local web app could replace the plugin if the plugin turns out to be impractical. A test runs a fresh interpreter, imports the core and checks that no `qgis` or PyQt module got loaded.
- **Covered rows only for `phantom?`.** The ticket's agent notes leave it open whether `real` deletes whole rows. Deleting whole rows would delete roads the second source matches. Refusing only when nothing is covered still lets a fully phantom road go in one key.

## Weak spots and ideas

- **Deleting by `raw_id` also catches split pieces.** Suspected. QGIS copies `raw_id` to both pieces of a split road. `real` on a stub then deletes every road row with that `raw_id`, including a long piece far from the stub. The `phantom?` coverage check guards against this, but the stub rule does not.
- **Partial deletion can go unnoticed.** Observed in a test. A `phantom?` flag over the end of a long road deletes the rows inside it, records `real`, and leaves the rest. The caller only learns which rows were deleted, not which named rows stayed.
- **Added rows are never deleted.** By design: they have no `raw_id`, so no flag names them.
- **The whole queue is rebuilt on every call.** `current_item` and `progress` build the full queue each time. On the densest real zone, CapeTown_Fynbos_-33p96_18p61_Urban, with no flags, that takes 27 ms. Not measured with thousands of flags.
- **A flag's tile is the nearest tile of any status.** A flag next to a dropped tile can sort with that tile, not with the kept tile it touches. The order is still deterministic.

## Code map

- `cleaning/review.py`, `build_queue`, `FlagItem`, `TileItem`: the queue and its items.
- `cleaning/review.py`, `current_item` and `progress`, `Progress`: where the annotator is, and how far through.
- `cleaning/review.py`, `decide_flag`, `_rows_to_delete`, `DELETING_FLAG_TYPES`: flag decisions and the deletion rule.
- `cleaning/review.py`, `review_tile`: tile review.
- `cleaning/review.py`, `_writing`: the SQLite write path with the trigger stand-ins.
- `cleaning/contract.py`, `parse_raw_ids`: reads a flag's `raw_ids` field.
- `cli.py`, `progress`: the command. `_packaged_zones` picks the zones for it and for `detect`.
- `tests/sentinel2data/test_review.py`: end-to-end tests on the packaged synthetic zone after `detect`. Phantom flags are hand-written rows.
