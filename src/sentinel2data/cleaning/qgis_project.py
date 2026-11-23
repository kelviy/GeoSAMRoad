"""Writes the per-zone QGIS project (``.qgs``) without needing QGIS installed.

The project opens ready to edit in plain QGIS 3 (no plugin): every local
layer is referenced relative to the zone folder, so the folder can be copied
between annotators' machines. Layers, top to bottom:

* Flags (points), Flags (areas): two views of the one ``flags`` table.
* Labels: editable, styled by ``status``, with value lists for ``status``,
  ``defect`` and ``road_class``. A new road defaults to ``added`` /
  ``Other Road``, and its ``buffer`` follows its ``road_class``.
* Raw labels: read-only.
* Overture roads: read-only, drawn as a line of circles, when the zone was
  packaged with a second source. Snapping never uses it, so an annotator
  can't copy its geometry into a label by accident.
* Tiles: kept tiles outlined, ``dropped_no_road`` hatched, the rest greyed.
* Sentinel-2 RGB and Sentinel-2 false colour (NIR, red, green), two views of
  one 4-band file, both hidden at start, and the NGI aerial reference
  imagery (XYZ).

QGIS fills every element left out here with its defaults.
"""
import xml.etree.ElementTree as ET
from pathlib import Path

from pyproj import CRS

from sentinel2data.cleaning.contract import (
    ADDED_ROAD_CLASSES,
    DEFECTS,
    FLAG_DECISIONS,
    FLAG_FIELDS,
    FLAG_TYPES,
    FLAGS_LAYER,
    LABEL_STATUSES,
    LABELS_LAYER,
    OVERTURE_LAYER,
    PENDING,
    RAW_LABELS_LAYER,
    ROAD_BUFFER,
    ROAD_SCALE,
    TILES_LAYER,
)
from sentinel2data.generator.config import TILE_STATUSES

# CDNGI aerial imagery, as served for OpenStreetMap South Africa.
NGI_AERIAL_URL = "https://aerial.openstreetmap.org.za/layer/ngi-aerial/{z}/{x}/{y}.png"
NGI_AERIAL_MAX_ZOOM = 20

QGIS_VERSION = "3.44.0"
VALUE_MAP_NULL = "{2839923C-8B7D-419E-B84B-CA2FE9B80EC7}"  # how a ValueMap stores NULL
ANNOTATOR_DEFAULT = "@user_full_name"

LABEL_STATUS_COLOURS = {
    "kept": "31,120,180,255",
    "modified": "255,127,0,255",
    "added": "51,160,44,255",
    "deleted": "150,150,150,200",
    "proposed": "227,26,180,255",
}

# How each flag type is drawn: the layer it shows in, its colour and, for a
# point, its marker shape, so a type reads by shape as well as colour.
FLAG_TYPE_STYLES = {
    "gap": ("Point", "247,129,191,255", "diamond"),
    "stub": ("Point", "228,26,28,255", "circle"),
    "duplicate": ("Point", "152,78,163,255", "square"),
    "crossing": ("Point", "166,86,40,255", "triangle"),
    "missing?": ("Polygon", "228,26,28,255", None),
    "phantom?": ("Polygon", "152,78,163,255", None),
}
assert set(FLAG_TYPE_STYLES) == set(FLAG_TYPES), "every flag type needs a style"

# A flag decided real or not_defect needs nothing more, so it draws grey.
# needs_qgis keeps its type colour: it is the work left to do in QGIS.
DECIDED = "decided"
DECIDED_COLOUR = "150,150,150,160"
FLAG_STYLE_KEY = f"if(\"decision\" IN ('real', 'not_defect'), '{DECIDED}', \"type\")"
FLAG_LABEL = "\"type\" || if(\"decision\" = 'pending', '', ' · ' || \"decision\")"


# --- XML helpers ------------------------------------------------------------

def _el(parent, tag, text=None, **attrs):
    e = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        e.text = str(text)
    return e


def _options(parent, options: dict):
    m = _el(parent, "Option", type="Map")
    for name, value in options.items():
        _el(m, "Option", type="QString", name=name, value=value)
    return m


def _srs(parent, crs: CRS):
    srs = _el(parent, "spatialrefsys", nativeFormat="Wkt")
    _el(srs, "wkt", crs.to_wkt())
    authid = crs.to_authority(min_confidence=90)
    _el(srs, "authid", ":".join(authid) if authid else "")
    _el(srs, "description", crs.name)
    _el(srs, "geographicflag", str(crs.is_geographic).lower())
    return srs


# --- symbols ----------------------------------------------------------------

def _symbol_layer(parent, layer_class):
    return _el(parent, "layer", **{"class": layer_class, "pass": 0}, enabled=1, locked=0)


def _symbol(parent, symbol_type, name):
    return _el(parent, "symbol", type=symbol_type, name=name, alpha=1, clip_to_extent=1, force_rhr=0)


def _line_layer(parent, colour, width=0.6, style="solid"):
    layer = _symbol_layer(parent, "SimpleLine")
    _options(layer, {"line_color": colour, "line_width": width, "line_width_unit": "MM",
                     "line_style": style, "capstyle": "round", "joinstyle": "round"})
    return layer


def _line_symbol(parent, name, colour, width=0.6, style="solid"):
    sym = _symbol(parent, "line", name)
    _line_layer(sym, colour, width, style)
    return sym


def _fill_layer(parent, fill, outline, outline_width=0.4, style="solid"):
    layer = _symbol_layer(parent, "SimpleFill")
    _options(layer, {"color": fill, "style": style, "outline_color": outline,
                     "outline_width": outline_width, "outline_width_unit": "MM",
                     "outline_style": "solid", "joinstyle": "miter"})
    return layer


def _fill_symbol(parent, name, fill, outline, outline_width=0.4, hatch=None):
    sym = _symbol(parent, "fill", name)
    _fill_layer(sym, fill, outline, outline_width)
    if hatch:
        layer = _symbol_layer(sym, "LinePatternFill")
        _options(layer, {"angle": 45, "distance": 3, "distance_unit": "MM"})
        _line_symbol(layer, f"@{name}@1", hatch, width=0.3)
    return sym


def _marker_symbol(parent, name, colour, shape="diamond"):
    sym = _symbol(parent, "marker", name)
    layer = _symbol_layer(sym, "SimpleMarker")
    _options(layer, {"name": shape, "color": colour, "outline_color": "0,0,0,255",
                     "size": 3.5, "size_unit": "MM", "outline_width": 0.3})
    return sym


def _flag_symbol(geometry, colour, shape):
    if geometry == "Point":
        return lambda s, n: _marker_symbol(s, n, colour, shape)
    return lambda s, n: _fill_symbol(s, n, colour.rsplit(",", 1)[0] + ",40", colour, 0.8)


def _labels(layer, expression, placement):
    """Label every feature with ``expression``: black text, white halo."""
    layer.set("labelsEnabled", "1")
    settings = _el(_el(layer, "labeling", type="simple"), "settings")
    style = _el(settings, "text-style", fieldName=expression, isExpression=1, fontSize=9,
                fontSizeUnit="Point", textColor="0,0,0,255")
    _el(style, "text-buffer", bufferDraw=1, bufferSize=1, bufferSizeUnits="MM", bufferColor="255,255,255,255")
    _el(settings, "placement", placement=placement, dist=1, distUnits="MM")
    _el(settings, "rendering", drawLabels=1)


def _circle_line_symbol(parent, name, colour):
    """A line drawn as circles every 4 mm, white with a ``colour`` outline."""
    sym = _symbol(parent, "line", name)
    layer = _symbol_layer(sym, "MarkerLine")
    _options(layer, {"placements": "Interval", "interval": 4, "interval_unit": "MM", "rotate": 0})
    circle = _symbol(layer, "marker", f"@{name}@0")
    _options(_symbol_layer(circle, "SimpleMarker"), {
        "name": "circle", "color": "255,255,255,255", "outline_color": colour,
        "size": 1.8, "size_unit": "MM", "outline_width": 0.4,
    })
    return sym


def _categorized(parent, attr, categories):
    """``categories``: [(value, add_symbol(symbols_el, name))]."""
    r = _el(parent, "renderer-v2", type="categorizedSymbol", attr=attr,
            symbollevels=0, enableorderby=0, forceraster=0)
    cats, syms = _el(r, "categories"), _el(r, "symbols")
    for i, (value, add_symbol) in enumerate(categories):
        _el(cats, "category", value=value, label=value, symbol=i, render="true", type="string")
        add_symbol(syms, str(i))
    return r


def _single(parent, add_symbol):
    r = _el(parent, "renderer-v2", type="singleSymbol", symbollevels=0, enableorderby=0, forceraster=0)
    add_symbol(_el(r, "symbols"), "0")
    return r


# --- fields -----------------------------------------------------------------

def _value_map(values, allow_null=False):
    entries = [("(none)", VALUE_MAP_NULL)] if allow_null else []
    entries += [(v, v) for v in values]
    return ("ValueMap", entries)


CONSTRAINT_EXPRESSION = 4  # QgsFieldConstraints.ConstraintExpression
STRENGTH_HARD = 1  # QgsFieldConstraints.ConstraintStrengthHard: enforced, QGIS won't save the row


def _field_config(layer, fields: dict):
    """``fields``: name -> {"widget": (type, config), "default": (expr, on_update),
    "readonly": bool, "constraint": (expr, description)}."""
    fc = _el(layer, "fieldConfiguration")
    defaults = _el(layer, "defaults")
    constraints = _el(layer, "constraints")
    constraint_exprs = _el(layer, "constraintExpressions")
    editable = _el(layer, "editable")
    for name, spec in fields.items():
        field = _el(fc, "field", name=name, configurationFlags="NoFlag")
        widget_type, config = spec.get("widget", ("", None))
        widget = _el(field, "editWidget", type=widget_type)
        cfg = _el(widget, "config")
        if widget_type == "ValueMap":
            outer = _el(cfg, "Option", type="Map")
            lst = _el(outer, "Option", type="List", name="map")
            for label, value in config:
                _el(_el(lst, "Option", type="Map"), "Option", type="QString", name=label, value=value)
        elif widget_type == "CheckBox":
            _options(cfg, {"CheckedState": "", "UncheckedState": "", "TextDisplayMethod": "0"})
        else:
            _el(cfg, "Option")
        expr, on_update = spec.get("default", ("", False))
        _el(defaults, "default", field=name, expression=expr, applyOnUpdate=int(on_update))
        if spec.get("readonly"):
            _el(editable, "field", name=name, editable=0)
        if "constraint" in spec:
            expr, desc = spec["constraint"]
            _el(constraints, "constraint", field=name, constraints=CONSTRAINT_EXPRESSION,
                exp_strength=STRENGTH_HARD, notnull_strength=0, unique_strength=0)
            _el(constraint_exprs, "constraint", field=name, exp=expr, desc=desc)


def _case(column, mapping, fallback, quote=False):
    def lit(v):
        return "'" + str(v).replace("'", "''") + "'" if quote else repr(float(v))
    # QGIS expressions only have the searched form (CASE WHEN <condition>).
    whens = " ".join(f"WHEN \"{column}\" = '{k}' THEN {lit(v)}" for k, v in mapping.items())
    return f'CASE {whens} ELSE "{fallback}" END'


def _road_class_rule():
    """Existing roads are never reclassified: a row with a raw_id keeps its raw
    label's class. A road added by an annotator takes one of the allowed classes."""
    allowed = ", ".join(f"'{c}'" for c in ADDED_ROAD_CLASSES)
    raw_class = f"attribute(get_feature('{_layer_id('raw_labels')}', 'raw_id', \"raw_id\"), 'road_class')"
    return f'if("raw_id" IS NULL, "road_class" IN ({allowed}), "road_class" = {raw_class})'


def _road_fields(columns):
    """Field config for the road attribute columns of the labels layers."""
    spec = {}
    for col in columns:
        if col == "road_class":
            spec[col] = {
                "widget": _value_map(ADDED_ROAD_CLASSES),
                "default": (f"'{ADDED_ROAD_CLASSES[0]}'", False),
                "constraint": (_road_class_rule(), "Existing roads keep their raw class; "
                               f"added roads are {', '.join(ADDED_ROAD_CLASSES)}"),
            }
        elif col == "buffer":
            # Follows road_class on every save, so choosing a class sets the buffer.
            spec[col] = {"default": (_case("road_class", ROAD_BUFFER, "buffer"), True), "readonly": True}
        elif col == "scale_class":
            spec[col] = {"default": (_case("road_class", ROAD_SCALE, "scale_class", quote=True), True), "readonly": True}
        else:
            spec[col] = {}
    return spec


# --- layers -----------------------------------------------------------------

def _layer_id(key):
    # QGIS discards a saved layer id of 10 characters or fewer and makes a new
    # one, which orphans the layer's tree node. Prefix keeps every id longer.
    return f"rosa_zone_{key}"


def _vector_layer(layers_el, *, layer_id, name, source, crs, geometry, wkb_type, read_only=False):
    ml = _el(layers_el, "maplayer", type="vector", geometry=geometry, wkbType=wkb_type,
             readOnly=int(read_only), styleCategories="AllStyleCategories")
    _el(ml, "id", _layer_id(layer_id))
    _el(ml, "datasource", source)
    _el(ml, "layername", name)
    _srs(_el(ml, "srs"), crs)
    _el(ml, "provider", "ogr", encoding="UTF-8")
    return ml


def _raster_layer(layers_el, *, layer_id, name, source, provider, crs, bands=None):
    """``bands``: the (red, green, blue) band numbers to draw, for a local file."""
    ml = _el(layers_el, "maplayer", type="raster", styleCategories="AllStyleCategories")
    _el(ml, "id", _layer_id(layer_id))
    _el(ml, "datasource", source)
    _el(ml, "layername", name)
    _srs(_el(ml, "srs"), crs)
    _el(ml, "provider", provider)
    if bands:
        red, green, blue = bands
        _el(_el(ml, "pipe"), "rasterrenderer", type="multibandcolor", opacity=1, alphaBand=-1,
            redBand=red, greenBand=green, blueBand=blue)
    return ml


def _snapping(root, snap_layers, never_snap):
    """Snapping, once the annotator turns it on, uses only ``snap_layers``.
    QGIS would otherwise snap to every layer, ``never_snap`` included."""
    settings = _el(root, "snapping-settings", mode=3, enabled=0, type=1, tolerance=12, unit=1,
                   **{"intersection-snapping": 0, "self-snapping": 0})
    layers = _el(settings, "individual-layer-settings")
    for key, enabled in [(k, 1) for k in snap_layers] + [(k, 0) for k in never_snap]:
        _el(layers, "layer-setting", id=_layer_id(key), enabled=enabled, type=1, tolerance=12, units=1)


def write_zone_project(path, *, gpkg, s2_imagery, crs, extent, road_columns, overture=None):
    """Write the zone's QGIS project at ``path``.

    ``gpkg``, ``s2_imagery`` and ``overture``, the second-source roads
    GeoPackage or None, must sit in the project's folder. ``extent`` is
    (xmin, ymin, xmax, ymax) in ``crs``, the view the project opens on.
    ``road_columns`` are the road attribute columns of the labels layers.
    """
    path = Path(path)
    crs = CRS.from_user_input(crs)
    gpkg_src = f"./{Path(gpkg).name}"

    root = ET.Element("qgis", version=QGIS_VERSION, projectname=path.stem)
    _el(root, "homePath", path="")
    _el(root, "title", path.stem)
    _srs(_el(root, "projectCrs"), crs)
    tree = _el(root, "layer-tree-group")
    layers = _el(root, "projectlayers")
    order = []

    def add(ml, *, visible=True):
        layer_id, name = ml.findtext("id"), ml.findtext("layername")
        _el(tree, "layer-tree-layer", id=layer_id, name=name, source=ml.findtext("datasource"),
            providerKey=ml.findtext("provider"), checked="Qt::Checked" if visible else "Qt::Unchecked",
            expanded=1)
        order.append(layer_id)

    # Flags: two typed views of one generic-geometry table.
    # Detectors fill every flag field except the annotator's decision.
    flag_fields = {name: {"readonly": True} for name in FLAG_FIELDS} | {
        "type": {"widget": _value_map(FLAG_TYPES), "readonly": True},
        "decision": {"widget": _value_map(FLAG_DECISIONS), "default": (f"'{PENDING}'", False)},
        "annotator": {"default": (ANNOTATOR_DEFAULT, True)},
    }
    # Each layer is styled by type, with a grey class for decided flags, and
    # every flag is labelled with its type and any decision.
    for layer_id, name, geometry, label_placement in (
        ("flags_points", "Flags (points)", "Point", 0),  # around the point
        ("flags_areas", "Flags (areas)", "Polygon", 4),  # horizontal, inside the area
    ):
        ml = _vector_layer(layers, layer_id=layer_id, name=name, crs=crs, geometry=geometry, wkb_type=geometry,
                           source=f"{gpkg_src}|layername={FLAGS_LAYER}|geometrytype={geometry}")
        categories = [(flag_type, _flag_symbol(geometry, colour, shape))
                      for flag_type, (geom, colour, shape) in FLAG_TYPE_STYLES.items() if geom == geometry]
        categories.append((DECIDED, _flag_symbol(geometry, DECIDED_COLOUR, "circle")))
        _categorized(ml, FLAG_STYLE_KEY, categories)
        _labels(ml, FLAG_LABEL, label_placement)
        _field_config(ml, flag_fields)
        add(ml)

    # Labels
    ml = _vector_layer(layers, layer_id="labels", name="Labels", crs=crs, geometry="Line",
                       wkb_type="LineString", source=f"{gpkg_src}|layername={LABELS_LAYER}")
    _categorized(ml, "status", [
        (st, lambda s, n, st=st: _line_symbol(s, n, LABEL_STATUS_COLOURS[st], 0.8,
                                              "dash" if st in ("deleted", "proposed") else "solid"))
        for st in LABEL_STATUSES
    ])
    _field_config(ml, {
        "raw_id": {"readonly": True},
        "status": {"widget": _value_map(LABEL_STATUSES), "default": ("'added'", False)},
        "defect": {"widget": _value_map(DEFECTS, allow_null=True)},
        "annotator": {"default": (ANNOTATOR_DEFAULT, True)},
        **_road_fields(road_columns),
    })
    add(ml)

    # Raw labels: read-only reference
    ml = _vector_layer(layers, layer_id="raw_labels", name="Raw labels", crs=crs, geometry="Line",
                       wkb_type="LineString", source=f"{gpkg_src}|layername={RAW_LABELS_LAYER}", read_only=True)
    _single(ml, lambda s, n: _line_symbol(s, n, "255,255,0,220", 1.6))
    add(ml)

    # Second-source roads: read-only reference, never snapped to.
    if overture is not None:
        ml = _vector_layer(layers, layer_id="overture", name="Overture roads", crs=crs, geometry="Line",
                           wkb_type="LineString", read_only=True,
                           source=f"./{Path(overture).name}|layername={OVERTURE_LAYER}")
        _single(ml, lambda s, n: _circle_line_symbol(s, n, "0,0,0,255"))
        add(ml)

    # Tiles
    ml = _vector_layer(layers, layer_id="tiles", name="Tiles", crs=crs, geometry="Polygon",
                       wkb_type="Polygon", source=f"{gpkg_src}|layername={TILES_LAYER}")
    grey = "120,120,120,150"
    tile_symbols = {
        "kept": lambda s, n: _fill_symbol(s, n, "0,0,0,0", "0,255,255,255", 0.8),
        "dropped_no_road": lambda s, n: _fill_symbol(s, n, "0,0,0,0", "120,120,120,255", 0.4, hatch="120,120,120,255"),
        "dropped_nan": lambda s, n: _fill_symbol(s, n, grey, "120,120,120,255", 0.4),
        "untiled": lambda s, n: _fill_symbol(s, n, grey, "120,120,120,255", 0.4),
    }
    _categorized(ml, "status", [(st, tile_symbols[st]) for st in TILE_STATUSES])
    _field_config(ml, {
        "tile_id": {"readonly": True},
        "row": {"readonly": True},
        "col": {"readonly": True},
        "status": {"readonly": True},
        "reviewed": {"widget": ("CheckBox", None)},
        "note": {},
        "annotator": {"default": (ANNOTATOR_DEFAULT, True)},
    })
    add(ml)

    # Imagery
    s2_source = f"./{Path(s2_imagery).name}"
    for layer_id, name, bands in (
        ("s2_rgb", "Sentinel-2 RGB", (1, 2, 3)),
        ("s2_false_colour", "Sentinel-2 false colour", (4, 1, 2)),  # NIR, red, green
    ):
        add(_raster_layer(layers, layer_id=layer_id, name=name, source=s2_source, provider="gdal",
                          crs=crs, bands=bands), visible=False)
    xyz = f"type=xyz&url={NGI_AERIAL_URL}&zmin=0&zmax={NGI_AERIAL_MAX_ZOOM}"
    add(_raster_layer(layers, layer_id="ngi_aerial", name="NGI aerial", source=xyz,
                      provider="wms", crs=CRS.from_epsg(3857)))

    _snapping(root, ["labels", "raw_labels"], ["overture"] if overture is not None else [])

    lo = _el(root, "layerorder")
    for layer_id in order:
        _el(lo, "layer", id=layer_id)

    props = _el(root, "properties")
    _el(_el(props, "Paths"), "Absolute", "false", type="bool")
    # QGIS ignores <projectCrs> unless this is set.
    _el(_el(props, "SpatialRefSys"), "ProjectionsEnabled", 1, type="int")
    xmin, ymin, xmax, ymax = extent
    view = _el(root, "ProjectViewSettings", UseProjectScales=0, rotation=0)
    dve = _el(view, "DefaultViewExtent", xmin=xmin, ymin=ymin, xmax=xmax, ymax=ymax)
    _srs(dve, crs)
    # The saved canvas view, so the project opens on the zone.
    canvas = _el(root, "mapcanvas", name="theMapCanvas", annotationsVisible=1)
    _el(canvas, "units", "meters")
    ext = _el(canvas, "extent")
    for tag, value in zip(("xmin", "ymin", "xmax", "ymax"), extent):
        _el(ext, tag, value)
    _el(canvas, "rotation", 0)
    _srs(_el(canvas, "destinationsrs"), crs)

    ET.indent(root)
    path.write_text("<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"
                    + ET.tostring(root, encoding="unicode") + "\n")
    return path
