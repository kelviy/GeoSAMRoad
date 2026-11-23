# Review queue plugin

How the QGIS plugin `rosa_review_queue` docks the review queue in QGIS and drives the review core through the review service. [review-queue.md](review-queue.md) describes the review core itself.

> **Status, 2026-11-23.** KEL-57 built the plugin. Tests cover the review service, the client, installing, and the plugin's zone layer and keys in headless QGIS. A human has not yet checked the plugin in a real QGIS session.

## Core idea

The annotator opens a zone project, and a panel docked on the right shows one queue item at a time. Each key sends one request, such as "decide flag 12 real", and the panel shows the state that comes back: the current item, its place in the queue, and progress. The map zooms to the item and selects it. The review core makes every decision. The plugin only sends keys, moves the map, and saves QGIS's open edits before the core writes.

## How it works

1. **Start the service.** QGIS's Python has no pandas, shapely or pyogrio, so the plugin cannot import the review core. On the first request it starts `python -m sentinel2data.cleaning.review_service` in the repo's Python. That Python's path is in the plugin's `service.json`, which `install-qgis-plugin` writes, and `ROSA_REVIEW_PYTHON` overrides it. Each request is one JSON line on stdin and gets one JSON line back. A refusal comes back as `ok: false` with the error, and the panel shows it in red. The service keeps running.
2. **Find the zone.** When a project opens, the panel looks for the layer id `rosa_zone_labels` that `package-zones` writes. Its source gives the zone GeoPackage. Any other project leaves the panel idle.
3. **Save hand edits, then write.** Before each decision or tile review, the panel saves every unsaved edit on the zone's layers and leaves them in edit mode. Then it sends the request, and afterwards it reloads those layers. So an accepted proposal turns from a dashed magenta line into an added road without reopening the project.
4. **Show the item.** The panel zooms the map to the item's extent and selects its feature. It turns on the NGI aerial, raw labels, labels, flags and tiles. The Sentinel-2 RGB follows the panel's checkbox.
5. **Record the annotator.** Every write sends `QgsApplication.userFullName()`, the full name of the computer account. QGIS's own `@user_full_name`, which fills `annotator` on hand edits, is the same value. With no full name, the panel refuses to write and says where to set it.

## Details that matter

- **The keys.** `A` accept and `X` reject a proposal, `R` real and `N` not a defect for a flag, `Q` needs_qgis for either, `T` or Enter in the note field to mark a tile reviewed, `B` back, `F` forward, `S` Sentinel-2 on or off. A key that does not fit the current item does nothing and says so.
- **The keys work while the panel has focus, and there they take priority over QGIS's.** QGIS binds S, T and R window-wide, to snapping, tracing and stream digitizing. Qt fires neither of two shortcuts that match one key. So the panel does not use shortcuts. It claims its keys from the `ShortcutOverride` event while focus is inside it. The note field is left out, so a note can hold those letters. A click on the map moves the focus away, and the keys go back to QGIS.
- **Filters and hidden detectors.** The filters are three lists, one per `QueueFilter` field. Their values come from the service. Hidden detectors are listed as text, because hiding one takes a `detect` re-run, as [review-queue.md](review-queue.md) explains.
- **A selected area is outlined, not filled.** Zone projects set no selection colour, so QGIS uses opaque white and fills a selected polygon with it. A selected `missing?` or `phantom?` flag, or a tile, then hides the imagery the annotator has to judge. When the panel opens a zone, `Zone.outline_selection` gives the flag-area and tile layers a selection symbol of a 0.8 mm yellow outline with no fill. Point flags keep QGIS's default selection.
- **A closed panel does not move the map.** Opening a zone with the panel closed leaves the project's saved view.
- **Each state request takes 0.08 s** on the synthetic zone, measured over 20 requests, because the service builds the queue three times. The first request also starts the service. Importing the review core took 0.5 s here.

## Why it's like this

- **A service process, not an import.** QGIS 3.44's Python in `qgis_env` has none of the core's libraries. KEL-11 keeps the core plain Python so that a local web app could replace the plugin, and that web app would need the same Python. The service keeps running between requests, so the core is imported once.
- **Save hand edits before each write.** KEL-57's agent notes warn about QGIS's open edit buffer next to the core's direct writes. QGIS saves only the fields an edit changed. So the harm comes from an unsaved edit to a field the core also writes. A headless QGIS test types `needs_qgis` on a flag without saving and then accepts it. Without the save, QGIS later saves `needs_qgis` over `real`, and the flag reads `needs_qgis` on an accepted proposal.
- **No review logic in the plugin.** KEL-57 asks for a thin layer, and KEL-11 leaves the Qt panel untested, so the logic stays in the tested core.

## Weak spots and ideas

- **Sentinel-2 RGB is a toggle, not always on.** The ticket asks for it to be visible on every item. It sits above the aerial in the project, so showing it always would hide the aerial. The panel leaves it to `S`, and it starts off.
- **The plugin saves the annotator's edits without asking.** By design: one key per decision. A half-drawn road that QGIS can save is saved too. Saving also clears QGIS's undo history for those layers, so Ctrl+Z cannot undo an edit made before the last decision.
- **A failed save can leave edits half saved.** Observed in the code. The panel saves layer by layer, so if the second layer fails, the first is already saved. The decision is not written, and the panel shows QGIS's errors.
- **Only the panel fixes the selection style.** Observed. A zone opened without the plugin still draws a selected area as a white box. The packager could write the selection symbol into the project, but repackaging refuses zones with cleaning work, so the plugin sets it at run time.
- **Every item turns the layers back on.** A layer the annotator hid comes back on the next item.
- **Each call blocks QGIS.** The panel waits for each answer. Not measured on a zone with thousands of flags, where a slow call would freeze QGIS while it runs.
- **The plugin finds layers by id.** It uses the ids `qgis_project` writes. A layer the annotator removed and added again gets a new id, and the panel no longer turns it on or selects in it.
- **The plugin knows the decisions and item kinds.** `panel.ACTIONS` names `real`, `not_defect`, `needs_qgis` and the three kinds. A new decision or kind in the contract needs a change here too.
- **The QGIS-side code is not checked by pyright.** Observed. `qgis.PyQt` re-exports PyQt5, which the repo venv lacks, so pyright cannot resolve the Qt names.

## Code map

- `cleaning/review_service.py`, `handle`, `serve`: the review service, one JSON line in and one out.
- `cleaning/qgis_plugin/rosa_review_queue/client.py`, `ReviewClient`, `service_python`: starts the service and sends it requests. No QGIS import.
- `cleaning/qgis_plugin/rosa_review_queue/zone.py`, `Zone`, `Zone.outline_selection`: finds the zone layers, saves hand edits, reloads, zooms, selects, and outlines selected areas.
- `cleaning/qgis_plugin/rosa_review_queue/panel.py`, `ReviewPanel`, `ACTIONS`, `ReviewPanel.eventFilter`: the docked panel and its keys.
- `cleaning/qgis_plugin/rosa_review_queue/plugin.py`, `ReviewQueuePlugin`: the menu entry, the dock, and rebinding when a project opens or closes.
- `cleaning/plugin_install.py`, `install`, and `cli.py`, `install_qgis_plugin`: copy the plugin into a QGIS profile.
- `tests/sentinel2data/test_review_service.py`: the service's requests and refusals.
- `tests/sentinel2data/test_review_plugin.py` and `qgis_review_plugin_check.py`: the client against a real service, installing, and the zone layer and panel keys in headless QGIS.
