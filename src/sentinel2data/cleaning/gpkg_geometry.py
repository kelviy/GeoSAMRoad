"""GeoPackage geometry blobs over plain SQLite: read and write them, and the
``ST_*`` functions GDAL's R-tree triggers call when a geometry is written.

A blob is a header (``GP``, version, flags, srs id, optional envelope)
followed by ISO WKB.
"""

import struct

import shapely

# Envelope indicator (flags bits 1-3) -> envelope size in bytes.
_ENVELOPE_SIZE = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
_EMPTY = 0b1_0000
_LITTLE_ENDIAN = 0b1
_ENVELOPE_XY = 0b10


def srs_id(blob: bytes) -> int:
    order = "<" if blob[3] & _LITTLE_ENDIAN else ">"
    return struct.unpack(f"{order}i", blob[4:8])[0]


def decode(blob: bytes) -> shapely.Geometry:
    start = 8 + _ENVELOPE_SIZE[(blob[3] >> 1) & 0b111]
    return shapely.from_wkb(bytes(blob[start:]))


def encode(geometry: shapely.Geometry, srs: int) -> bytes:
    """A little-endian blob with an xy envelope. Not for empty geometries."""
    minx, miny, maxx, maxy = shapely.bounds(geometry)
    header = b"GP" + bytes([0, _LITTLE_ENDIAN | _ENVELOPE_XY])
    return (
        header
        + struct.pack("<i", srs)
        + struct.pack("<4d", minx, maxx, miny, maxy)
        + shapely.to_wkb(
            geometry,
            byte_order=1,
            flavor="iso",
            output_dimension=3 if shapely.has_z(geometry) else 2,
        )
    )


def reencode(blob: bytes, geometry: shapely.Geometry) -> bytes:
    """``geometry`` as a blob with the srs id of ``blob``."""
    return encode(geometry, srs_id(blob))


def register_rtree_functions(con):
    """Give ``con`` the ``ST_*`` functions GDAL's R-tree triggers call. An
    UPDATE compiles every trigger on its table, so they must exist even when
    no geometry changes."""

    def bound(i):
        return lambda blob: (
            None if blob is None else float(shapely.bounds(decode(blob))[i])
        )

    def is_empty(blob):
        if blob is None:
            return None
        return int(bool(blob[3] & _EMPTY) or shapely.is_empty(decode(blob)))

    con.create_function("ST_IsEmpty", 1, is_empty, deterministic=True)
    for name, i in (("ST_MinX", 0), ("ST_MinY", 1), ("ST_MaxX", 2), ("ST_MaxY", 3)):
        con.create_function(name, 1, bound(i), deterministic=True)
