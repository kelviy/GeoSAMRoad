# ROSA cleaning: open directions

Options for the ROSA label cleaning that are deliberately **not** in the current cleaning spec. They are recorded here so the team can decide later. Each one changes the methodology the paper reports, so adopting one means updating the spec and the affected ADRs.

## Copying geometry from a second source

**Current rule:** second sources (Overture, OpenStreetMap, etc.) are flags only. An annotator always draws the label revision from the reference imagery and never accepts a second-source line as-is.

**Possible direction:** where a second source already has a suitable road line (e.g. a missing road that Overture/OSM maps well), copy it into the cleaned labels instead of redrawing it.

- **To decide:** the team needs to agree, and first test by hand how much annotator time it actually saves compared with tracing.
- **Licensing consequence:** Overture transportation and OSM are ODbL. Copying their geometry makes the cleaned labels a derivative database, so the published ROSA labels would have to be released under ODbL (share-alike plus attribution) and could not use a licence of our own choice. Check this against CDNGI's licence terms, which govern the raw labels.
- **Provenance:** copied features would need their own `status`/source value so raw vs cleaned statistics can separate them from features drawn from imagery.

## Moving tiles to cover more valuable roads

**Current rule:** the tile grid is fixed by the baseline regeneration, and annotators edit only within `kept` tiles.

**Possible direction:** shift or re-place tiles within a zone so they cover more of the valuable roads, instead of the fixed grid from the zone origin.

- Not planned yet. It would change the tile set, the baseline vs cleaned comparison and possibly the split statistics, so it would need its own decision.

## Model-assisted detectors

**Current rule:** all detectors are model-agnostic (geometry rules plus disagreement with a second source) on every split.

**Possible direction:** use GeoSAMRoad predictions to flag (option C) or propose geometry for (option D) suspected missing or phantom roads, on train/val only.

- **Never on test:** it would bias evaluation towards our own model.
- **Known problems:** the model was trained on the same noisy CDNGI labels, so it tends to agree with their errors. It has memorised the train tiles unless k-fold retrained. At about 50 APLS it would produce many false proposals.
- **Likely not pursued** before the 5 Dec 2026 deadline.
