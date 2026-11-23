"""The open zone project as the panel sees it: its GeoPackage, its layers,
and what the panel does to them around each review core call."""

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsProject,
    QgsRectangle,
    QgsVectorLayer,
)

# Layer ids the packager writes into every zone project (``qgis_project``).
LABELS = "rosa_zone_labels"
RAW_LABELS = "rosa_zone_raw_labels"
FLAG_POINTS = "rosa_zone_flags_points"
FLAG_AREAS = "rosa_zone_flags_areas"
TILES = "rosa_zone_tiles"
S2_RGB = "rosa_zone_s2_rgb"
AERIAL = "rosa_zone_ngi_aerial"

# The two views of the flags table, one per geometry type.
FLAG_LAYERS = (FLAG_POINTS, FLAG_AREAS)

# Shown on every item: the reference imagery, the raw labels under the
# labels, and the flags, proposals included.
ALWAYS_SHOWN = (AERIAL, RAW_LABELS, LABELS, *FLAG_LAYERS, TILES)


class HandEditsNotSaved(Exception):
    """QGIS could not save the annotator's open edits, so the panel wrote nothing."""

    def __init__(self, layer, errors):
        super().__init__(
            f"Your unsaved edits on {layer} could not be saved, so the decision was "
            "not written:\n" + "\n".join(errors)
        )


class Zone:
    """A zone project loaded in QGIS: one GeoPackage behind its vector layers."""

    def __init__(self, project: QgsProject, labels: QgsVectorLayer):
        self.project = project
        self.gpkg = _gpkg_of(labels)
        self.crs = labels.crs()

    @classmethod
    def of(cls, project: QgsProject) -> "Zone | None":
        """The zone in ``project``, or None when it is not a zone project."""
        labels = project.mapLayer(LABELS)
        if not isinstance(labels, QgsVectorLayer) or labels.providerType() != "ogr":
            return None
        return cls(project, labels)

    @property
    def name(self) -> str:
        return self.project.baseName()

    def vector_layers(self) -> list[QgsVectorLayer]:
        """Every layer of the project on the zone GeoPackage."""
        return [
            layer
            for layer in self.project.mapLayers().values()
            if isinstance(layer, QgsVectorLayer) and _gpkg_of(layer) == self.gpkg
        ]

    def save_hand_edits(self):
        """Save every open edit on the zone's layers, keeping them in edit
        mode, before the review core writes the GeoPackage. Otherwise QGIS
        would later save its edit buffer over what the core wrote, or the core
        would decide on rows the annotator has changed but not saved."""
        for layer in self.vector_layers():
            if (
                layer.isEditable()
                and layer.isModified()
                and not layer.commitChanges(stopEditing=False)
            ):
                raise HandEditsNotSaved(layer.name(), layer.commitErrors())

    def reload(self):
        """Read the zone's layers again after the review core wrote, so an
        accepted proposal shows as a road without reopening the project."""
        for layer in self.vector_layers():
            layer.reload()
            layer.triggerRepaint()

    def show(self, canvas, item: dict | None, show_s2: bool):
        """Zoom ``canvas`` to the item's extent, select its feature, and turn
        on the layers an annotator needs to judge it."""
        root = self.project.layerTreeRoot()
        for layer_id in ALWAYS_SHOWN:
            node = root.findLayer(layer_id)
            if node is not None:
                node.setItemVisibilityChecked(True)
        s2 = root.findLayer(S2_RGB)
        if s2 is not None:
            s2.setItemVisibilityChecked(show_s2)

        for layer_id in (*FLAG_LAYERS, TILES):
            layer = self.project.mapLayer(layer_id)
            if isinstance(layer, QgsVectorLayer):
                layer.removeSelection()
        if item is None:
            return
        selected = (TILES,) if item["kind"] == "tile" else FLAG_LAYERS
        for layer_id in selected:
            layer = self.project.mapLayer(layer_id)
            if isinstance(layer, QgsVectorLayer):
                # selectByIds takes any fid, so ask the layer whether it shows
                # it: each flags layer shows one geometry type of the table.
                request = QgsFeatureRequest(item["fid"]).setNoAttributes()
                layer.selectByIds([f.id() for f in layer.getFeatures(request)])
        canvas.setExtent(self._on_canvas(canvas, QgsRectangle(*item["extent"])))
        canvas.refresh()

    def _on_canvas(self, canvas, extent: QgsRectangle) -> QgsRectangle:
        """``extent``, in the zone's CRS, in the canvas's CRS."""
        target: QgsCoordinateReferenceSystem = canvas.mapSettings().destinationCrs()
        if target == self.crs:
            return extent
        return QgsCoordinateTransform(
            self.crs, target, self.project
        ).transformBoundingBox(extent)


def _gpkg_of(layer: QgsVectorLayer) -> str:
    """The file an OGR layer reads, without its ``|layername=`` options."""
    return layer.source().split("|")[0]
