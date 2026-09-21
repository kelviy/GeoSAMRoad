import re
from dataclasses import dataclass
from pathlib import Path
from sentinel2data.generator.config import SPLIT_NAMES

# [ZoneName_]Biome_<lat>_<lon>_<UrbanClass>
# p in filename indicates decimal point
# ZoneName is optional
ZONE_FILENAME_RE = re.compile(
    r"^(?:(?P<zone>[A-Za-z]+)_)?(?P<biome>[A-Za-z]+)"
    r"_(?P<lat>-?\d+(?:p\d+)?)_(?P<lon>-?\d+(?:p\d+)?)"
    r"_(?P<urban>[A-Za-z]+)$"
)

GRAMMAR_HINT = (
    "expected '[ZoneName_]Biome_<lat>_<lon>_<UrbanClass>', where ZoneName and "
    "Biome are single alphabetic tokens and 'p' is the decimal point "
    "(e.g. 'AlbanyThicket_-32p93_24p66_Rural' or "
    "'Soebatsfontein_SucculentKaroo_-30p12_17p59_PeriUrban')"
)

RANDOM_SAMPLE_PREFIX = "RandomSample"
UNKNOWN_LABEL = "Unknown"

@dataclass(frozen=True)
class ZoneMetadata:
    zone_filename: str    # base name (unique per zone), filename contains metadata
    zone_name: str      # Optional: parsed name, or RandomSampleN
    biome: str
    urban_class: str    # Classification from GEE custom strata mask
    lat: float
    lon: float
    named: bool         # If there is a zone name


def _coord(token: str) -> float:
    """Convert p to points. E.g: -32p93 to -32.93"""
    return float(token.replace("p", "."))


def parse_zone_filename(file_name: str) -> ZoneMetadata:
    """Parse one zone COG"""
    match = ZONE_FILENAME_RE.match(file_name)
    if match is None:
        raise ValueError(f"cannot parse zone filename {file_name!r}: {GRAMMAR_HINT}")
    return ZoneMetadata(
        zone_filename=file_name,
        zone_name=match["zone"] or "",
        biome=match["biome"],
        urban_class=match["urban"],
        lat=_coord(match["lat"]),
        lon=_coord(match["lon"]),
        named=match["zone"] is not None,
    )


def _split_rank(split: str) -> int:
    return SPLIT_NAMES.index(split) if split in SPLIT_NAMES else len(SPLIT_NAMES)


def assign_zone_metadata(jobs) -> dict:
    """Parses zones per split folder (split, cog_path) and numbers the unnamed zones.
    Args:
        Jobs are (split, cog_path) tuples
    """
    jobs = list(jobs)
    ordered = sorted(jobs, key=lambda job: (Path(job[1]).stem, _split_rank(job[0])))

    parsed, failures = {}, []
    for split, path in ordered:
        filename = Path(path).stem
        try:
            parsed[(split, path)] = parse_zone_filename(filename)
        except ValueError as exc:
            failures.append(f"  {Path(path).name}: {exc}")

    if failures:
        raise ValueError(
            f"{len(failures)} source COG filename(s) do not match the zone grammar:\n"
            + "\n".join(failures)
        )

    counter = 0
    for key in parsed:  # dict preserves the sorted insertion order
        meta: ZoneMetadata = parsed[key]
        if meta.named:
            continue
        counter += 1
        parsed[key] = ZoneMetadata(
            zone_filename=meta.zone_filename,
            zone_name=f"{RANDOM_SAMPLE_PREFIX}{counter}",
            biome=meta.biome,
            urban_class=meta.urban_class,
            lat=meta.lat,
            lon=meta.lon,
            named=False,
        )
    return parsed
