"""Load a QGIS project headlessly and print what an annotator would get, as JSON.

Run with the Python of a QGIS install (not the repo venv):

    QT_QPA_PLATFORM=offscreen PYTHONPATH=<qgis>/share/qgis/python \
        <qgis python> qgis_inspect.py <project.qgs>

It has no repo imports, so it runs in QGIS's own environment.
"""
import json
import sys

from qgis.core import (
    QgsApplication,
    QgsExpression,
    QgsFeature,
    QgsFieldConstraints,
    QgsGeometry,
    QgsPointXY,
    QgsMapLayerType,
    QgsProject,
    QgsRenderContext,
    QgsVectorLayerUtils,
)

NULL_VALUE_MAP_KEY = "{2839923C-8B7D-419E-B84B-CA2FE9B80EC7}"  # QGIS ValueMap NULL entry


def symbol_layers(symbol):
    return [sl.layerType() for sl in symbol.symbolLayers()] if symbol else []


def symbol_info(symbol):
    """Each symbol layer's type, and for a marker line the shape it repeats."""
    layers = []
    for sl in symbol.symbolLayers():
        entry = {"type": sl.layerType()}
        sub = sl.subSymbol()
        if sub is not None and sub.symbolLayerCount() and hasattr(sub.symbolLayer(0), "shape"):
            entry["marker"] = sub.symbolLayer(0).shape().name
        layers.append(entry)
    return layers


def renderer_info(renderer):
    info = {"type": renderer.type()}
    if renderer.type() == "singleSymbol":
        info["symbol"] = symbol_info(renderer.symbol())
    if renderer.type() == "categorizedSymbol":
        info["field"] = renderer.classAttribute()
        info["categories"] = {
            str(c.value()): {
                "symbol_layers": symbol_layers(c.symbol()),
                "fill_alpha": [sl.color().alpha() for sl in c.symbol().symbolLayers()],
            }
            for c in renderer.categories()
        }
    return info


def field_info(layer):
    fields = {}
    cfg = layer.editFormConfig()
    for i, field in enumerate(layer.fields()):
        setup = layer.editorWidgetSetup(i)
        entry = {"widget": setup.type(), "editable": not cfg.readOnly(i)}
        if setup.type() == "ValueMap":
            values = []
            for item in setup.config().get("map", []):
                values.extend(item.values())
            entry["values"] = [None if v == NULL_VALUE_MAP_KEY else v for v in values]
        default = layer.defaultValueDefinition(i)
        entry["default"] = default.expression()
        entry["default_on_update"] = default.applyOnUpdate()
        fields[field.name()] = entry
    return fields


def _plain(value):
    return None if value is None or str(value) == "NULL" else value


def new_road(layer, road_class=None):
    """Digitise a road in an edit session, then pick ``road_class`` for it,
    as an annotator does in the attribute form. Returns its saved attributes.
    The edit is rolled back, so the GeoPackage is untouched."""
    fields = layer.fields()
    layer.startEditing()
    feat = QgsVectorLayerUtils.createFeature(
        layer, QgsGeometry.fromWkt("LINESTRING(370300 6249300, 370400 6249300)"),
        context=layer.createExpressionContext(),
    )
    layer.addFeature(feat)
    if road_class is not None:
        layer.changeAttributeValue(feat.id(), fields.indexOf("road_class"), road_class)
    saved = layer.getFeature(feat.id())
    out = {f.name(): _plain(saved.attribute(i)) for i, f in enumerate(fields)}
    layer.rollBack()
    return out


def split_road(layer, raw_id):
    """Split the road ``raw_id`` across its midpoint in an edit session, as the
    Split Features tool does. Returns the pieces' attributes; rolled back."""
    fields = layer.fields()
    feat = next(f for f in layer.getFeatures() if f["raw_id"] == raw_id)
    mid = feat.geometry().interpolate(feat.geometry().length() / 2).asPoint()
    layer.startEditing()
    layer.selectByIds([feat.id()])
    cut = [QgsPointXY(mid.x() - 50, mid.y() - 50), QgsPointXY(mid.x() + 50, mid.y() + 50)]
    result = layer.splitFeatures(cut, True)
    pieces = [
        {f.name(): _plain(p.attribute(i)) for i, f in enumerate(fields) if f.name() != "fid"}
        for p in layer.getFeatures() if p["raw_id"] == raw_id
    ]
    layer.rollBack()
    return {"result": int(result), "pieces": pieces}


def flag_looks(layer, types, decisions):
    """For a flag of each type and decision, the legend class QGIS draws it
    with, its symbol colour, and its label text."""
    renderer, labeling = layer.renderer(), layer.labeling()
    settings = labeling.settings() if labeling else None
    label_expr = QgsExpression(settings.fieldName) if settings else None
    context = QgsRenderContext()
    context.setExpressionContext(layer.createExpressionContext())
    renderer.startRender(context, layer.fields())
    looks = {}
    for flag_type in types:
        for decision in decisions:
            feat = QgsFeature(layer.fields())
            feat.setAttribute("type", flag_type)
            feat.setAttribute("decision", decision)
            context.expressionContext().setFeature(feat)
            symbol = renderer.symbolForFeature(feat, context)
            keys = renderer.legendKeysForFeature(feat, context)
            legend = {i.ruleKey(): i.label() for i in renderer.legendSymbolItems()}
            looks[f"{flag_type}/{decision}"] = {
                "class": [legend[k] for k in keys],
                "colour": symbol.color().name() if symbol else None,
                "shape": symbol.symbolLayer(0).shape().name if symbol and hasattr(symbol.symbolLayer(0), "shape") else None,
                "label": label_expr.evaluate(context.expressionContext()) if label_expr else None,
            }
    renderer.stopRender(context)
    return {"labels_enabled": layer.labelsEnabled(), "looks": looks}


def road_class_allowed(layer):
    """Whether QGIS's enforced constraints accept a ``road_class`` value, for an
    existing road kept as it is or reclassified, and for an added road."""
    idx = layer.fields().indexOf("road_class")
    hard = QgsFieldConstraints.ConstraintStrength.ConstraintStrengthHard

    def ok(feat, value):
        feat = QgsFeature(feat)
        feat.setAttribute(idx, value)
        return QgsVectorLayerUtils.validateAttribute(layer, feat, idx, hard)[0]

    existing = next(f for f in layer.getFeatures() if f["road_class"] == "Other Road")
    added = QgsVectorLayerUtils.createFeature(layer, context=layer.createExpressionContext())
    return {
        "existing_unchanged": ok(existing, "Other Road"),
        "existing_reclassified": ok(existing, "Street"),
        "added_allowed_class": ok(added, "Main Road"),
        "added_other_class": ok(added, "National Road"),
    }


def main(project_path):
    app = QgsApplication([], False)
    app.initQgis()
    project = QgsProject.instance()
    ok = project.read(project_path)
    report = {"read": ok, "crs": project.crs().authid(), "layers": {}, "order": []}

    root = project.layerTreeRoot()
    report["order"] = [node.name() for node in root.findLayers()]
    report["visible"] = {node.name(): node.isVisible() for node in root.findLayers()}
    # A tree node whose layer id QGIS didn't keep shows no layer in the panel.
    report["unresolved_tree_nodes"] = [n.name() for n in root.findLayers() if n.layer() is None]
    for layer in project.mapLayers().values():
        entry = {
            "valid": layer.isValid(),
            "provider": layer.providerType(),
            "source": layer.source(),
            "crs": layer.crs().authid(),
        }
        if layer.type() == QgsMapLayerType.VectorLayer:
            entry.update(
                read_only=layer.readOnly(),
                feature_count=layer.featureCount(),
                geometry_type=layer.geometryType().name if hasattr(layer.geometryType(), "name") else int(layer.geometryType()),
                renderer=renderer_info(layer.renderer()),
                fields=field_info(layer),
            )
            if layer.name() == "Labels":
                entry["new_road"] = {
                    cls: new_road(layer, cls) for cls in ("Main Road", "Street", "Other Road")
                }
                entry["new_road"]["default"] = new_road(layer)
                entry["split"] = split_road(layer, raw_id=1)
                entry["road_class_allowed"] = road_class_allowed(layer)
            if layer.name().startswith("Flags"):
                types = layer.editorWidgetSetup(layer.fields().indexOf("type")).config()["map"]
                decisions = layer.editorWidgetSetup(layer.fields().indexOf("decision")).config()["map"]
                entry["flags"] = flag_looks(layer, [v for d in types for v in d.values()],
                                            [v for d in decisions for v in d.values()])
        else:
            entry["band_count"] = layer.bandCount()
            renderer = layer.renderer()
            if renderer is not None and renderer.type() == "multibandcolor":
                entry["rgb_bands"] = [renderer.redBand(), renderer.greenBand(), renderer.blueBand()]
        report["layers"][layer.name()] = entry

    snapping = project.snappingConfig()
    report["snapping"] = {
        "enabled": snapping.enabled(),
        "mode": snapping.mode().name,
        "layers": {
            layer.name(): settings.enabled()
            for layer, settings in snapping.individualLayerSettings().items()
        },
    }
    print(json.dumps(report, default=str))
    app.exitQgis()


if __name__ == "__main__":
    main(sys.argv[1])
