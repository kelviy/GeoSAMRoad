"""Detector precision: the share of judged flags (``real`` or ``not_defect``) marked ``real``,
per detector and flag type, read from the decisions in zone GeoPackages.

Precision is ``real / (real + not_defect)``. ``needs_qgis`` and pending flags
are left out of it and counted apart: ``needs_qgis`` sends a flag to plain
QGIS editing without saying whether it was a defect. An accepted proposal is
a ``real`` flag and a rejected one a ``not_defect`` flag, so proposals need no
rule of their own. Hidden detectors' flags count: the precision check is what
decides which detectors to hide.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

import pyogrio

from sentinel2data.cleaning.contract import FLAG_DECISIONS, FLAGS_LAYER, PENDING

# (detector, flag type)
FlagGroup = tuple[str, str]


@dataclass(frozen=True)
class DecisionCounts:
    real: int = 0
    not_defect: int = 0
    needs_qgis: int = 0
    pending: int = 0

    @property
    def judged(self) -> int:
        """Flags inside the share: marked ``real`` or ``not_defect``. Unlike
        ``review.Progress.flags_decided``, this leaves out ``needs_qgis``."""
        return self.real + self.not_defect

    @property
    def precision(self) -> float | None:
        """``real / judged``, None when no flag is judged."""
        return self.real / self.judged if self.judged else None

    def __add__(self, other: "DecisionCounts") -> "DecisionCounts":
        return DecisionCounts(
            self.real + other.real,
            self.not_defect + other.not_defect,
            self.needs_qgis + other.needs_qgis,
            self.pending + other.pending,
        )


def zone_counts(gpkg) -> dict[FlagGroup, DecisionCounts]:
    """Decision counts per (detector, flag type) over every flag in the zone,
    hidden detectors included. A NULL decision is pending; any value outside
    ``FLAG_DECISIONS`` raises ``ValueError``."""
    flags = pyogrio.read_dataframe(
        gpkg,
        layer=FLAGS_LAYER,
        columns=["detector", "type", "decision"],
        read_geometry=False,
        fid_as_index=True,
    )
    decisions = (
        flags["decision"].astype(object).where(flags["decision"].notna(), PENDING)
    )
    unknown = flags.index[~decisions.isin(FLAG_DECISIONS)]
    if len(unknown):
        raise ValueError(
            f"{gpkg}: flags {list(unknown)} have decisions outside {list(FLAG_DECISIONS)}: "
            f"{sorted(set(decisions.loc[unknown]))}"
        )
    tally = Counter(zip(flags["detector"], flags["type"], decisions))
    groups = sorted({(str(d), str(t)) for d, t, _ in tally})
    return {
        (d, t): DecisionCounts(
            real=tally[d, t, "real"],
            not_defect=tally[d, t, "not_defect"],
            needs_qgis=tally[d, t, "needs_qgis"],
            pending=tally[d, t, PENDING],
        )
        for d, t in groups
    }


def pooled(
    zones: Iterable[dict[FlagGroup, DecisionCounts]],
) -> dict[FlagGroup, DecisionCounts]:
    """Counts summed over zones, so the overall share pools every zone's flags
    rather than averaging the per-zone shares."""
    total: dict[FlagGroup, DecisionCounts] = {}
    for zone in zones:
        for group, c in zone.items():
            total[group] = total.get(group, DecisionCounts()) + c
    return dict(sorted(total.items()))
