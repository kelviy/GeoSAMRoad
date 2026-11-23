"""Drive the review queue plugin's zone layer and client in headless QGIS, on
a zone project after ``detect``, and print what happened as JSON.

Run with the Python of a QGIS install (not the repo venv):

    QT_QPA_PLATFORM=offscreen PYTHONPATH=<qgis>/share/qgis/python \
        <qgis python> qgis_review_plugin_check.py <plugin parent dir> \
        <project.qgs> <review service python>

Has no repo imports beyond the plugin, so it runs in QGIS's own environment.
"""

import json
import sqlite3
import sys

from qgis.core import (
    QgsApplication,
    QgsFeature,
    QgsGeometry,
    QgsMapRendererParallelJob,
    QgsMapSettings,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
)
from qgis.gui import QgsMapCanvas
from qgis.PyQt.QtCore import QSize, Qt
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QAction, QMainWindow

plugins_dir, project_path, service_python = sys.argv[1:4]
sys.path.insert(0, plugins_dir)

from rosa_review_queue.client import ReviewClient
from rosa_review_queue.panel import ReviewPanel
from rosa_review_queue.zone import FLAG_AREAS, LABELS, S2_RGB, TILES, Zone

app = QgsApplication([], True)
app.initQgis()
project = QgsProject.instance()
assert project.read(project_path), project_path
canvas = QgsMapCanvas()
canvas.setDestinationCrs(project.crs())
report = {}

zone = Zone.of(project)
labels = project.mapLayer(LABELS)
assert isinstance(labels, QgsVectorLayer)


def stored(sql, *args):
    with sqlite3.connect(zone.gpkg) as con:
        return con.execute(sql, args).fetchall()


with ReviewClient(service_python) as client:
    state = client.ask("state", zone.gpkg)["state"]
    item = state["item"]
    report["first_kind"] = item["kind"]
    (proposal_fid,) = stored(
        "SELECT proposal_fid FROM flags WHERE fid = ?", item["fid"]
    )[0]

    # A hand edit, left unsaved in QGIS's edit buffer, on a road the proposal
    # does not touch.
    kept = next(
        f.id()
        for f in labels.getFeatures()
        if f["status"] == "kept" and f.id() != proposal_fid
    )
    labels.startEditing()
    labels.changeAttributeValue(kept, labels.fields().indexOf("defect"), "misalignment")
    labels.changeAttributeValue(kept, labels.fields().indexOf("status"), "modified")
    # And an unsaved decision typed on the same flag. Saved only after the
    # accept, it would leave the flag needs_qgis on an accepted proposal.
    flag_points = project.mapLayer("rosa_zone_flags_points")
    flag_points.startEditing()
    flag_points.changeAttributeValue(
        item["fid"], flag_points.fields().indexOf("decision"), "needs_qgis"
    )
    report["buffer_modified"] = labels.isModified() and flag_points.isModified()

    zone.save_hand_edits()
    answer = client.ask(
        "decide", zone.gpkg, fid=item["fid"], decision="real", annotator="Thandi"
    )
    report["decided"] = answer["ok"]
    zone.reload()
    zone.show(canvas, answer["state"]["item"], show_s2=True)

    shown = {f.id(): f["status"] for f in labels.getFeatures()}
    report["proposal_on_map"] = shown[proposal_fid]
    report["hand_edit_on_map"] = shown[kept]
    report["still_editing"] = labels.isEditable()
    # Closing QGIS saves what is left in the buffer: nothing may undo the accept.
    report["commit_after"] = labels.commitChanges() and flag_points.commitChanges()

    report["stored_proposal"] = stored(
        "SELECT status, defect, annotator FROM labels WHERE fid = ?", proposal_fid
    )[0]
    report["stored_hand_edit"] = stored(
        "SELECT status, defect FROM labels WHERE fid = ?", kept
    )[0]
    report["stored_flag"] = stored(
        "SELECT decision, annotator FROM flags WHERE fid = ?", item["fid"]
    )[0]
    next_item = answer["state"]["item"]
    report["canvas_holds_next_item"] = canvas.extent().contains(
        QgsPointXY(*next_item["location"])
    )
    report["selected"] = sorted(
        project.mapLayer(lid).selectedFeatureIds()[0]
        for lid in (
            "rosa_zone_flags_points",
            "rosa_zone_flags_areas",
            "rosa_zone_tiles",
        )
        if project.mapLayer(lid).selectedFeatureIds()
    )
    report["next_fid"] = next_item["fid"]
    s2 = project.layerTreeRoot().findLayer("rosa_zone_s2_rgb")
    report["s2_visible"] = s2.isVisible() if s2 else None


def inside_pixel(extent, *layers):
    """The colour inside the middle of ``extent``, drawn from ``layers`` over
    the Sentinel-2 RGB, as an annotator sees it."""
    settings = QgsMapSettings()
    settings.setLayers([*layers, project.mapLayer(S2_RGB)])
    settings.setDestinationCrs(project.crs())
    settings.setSelectionColor(project.selectionColor())
    settings.setExtent(extent)
    settings.setOutputSize(QSize(100, 100))
    job = QgsMapRendererParallelJob(settings)
    job.start()
    job.waitForFinished()
    return job.renderedImage().pixelColor(50, 75).getRgb()  # below the label


# A selected area flag must not hide what lies inside it. QGIS fills a
# selected polygon with the project's selection colour, opaque white here.
areas = project.mapLayer(FLAG_AREAS)
tile = next(project.mapLayer(TILES).getFeatures())
area = QgsFeature(areas.fields())
area.setGeometry(QgsGeometry.fromRect(tile.geometry().boundingBox().buffered(-100)))
area["type"], area["decision"] = "phantom?", "pending"
areas.dataProvider().addFeatures([area])
fid = max(f.id() for f in areas.getFeatures())
view = tile.geometry().boundingBox()
imagery = inside_pixel(view)
areas.selectByIds([fid])
default_selected = inside_pixel(view, areas)
Zone.of(project).outline_selection()
report["area_selection"] = {
    "default_hides_inside": default_selected != imagery,
    "outlined_shows_inside": inside_pixel(view, areas) == imagery,
}


# The panel docked in a main window that binds the panel's keys window-wide,
# as QGIS binds S, T and R.
window = QMainWindow()
window.setCentralWidget(canvas)
fired = []
for key in "SRTB":
    action = QAction(key, window)
    action.setShortcut(key)
    action.triggered.connect(lambda _=False, k=key: fired.append(k))
    window.addAction(action)
with ReviewClient(service_python) as client:
    panel = ReviewPanel(canvas, client, window)
    window.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, panel)
    window.show()
    panel.bind(Zone.of(project))
    panel.body.setFocus()
    app.processEvents()
    before = panel.state["index"]
    for key in (Qt.Key.Key_S, Qt.Key.Key_B):
        QTest.keyClick(app.focusWidget(), key)
        app.processEvents()
    report["panel_keys"] = {
        "s2_toggled": panel.show_s2.isChecked(),
        "went_back": panel.state["index"] == before - 1,
        "window_actions_fired": fired,
    }
    panel.note.setEnabled(True)
    panel.note.setFocus()
    QTest.keyClicks(panel.note, "SRTB")
    report["panel_keys"]["note"] = panel.note.text()


print(json.dumps(report))
