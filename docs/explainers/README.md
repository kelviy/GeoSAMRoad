# Explainers

An **explainer** is a short, human-readable doc on how one feature works. You should be able to read it in a few minutes, understand the feature well enough to reason about it, and spot things that could be better.

Explainers cover how a feature works and why. They are not usage manuals, API references or changelogs. Usage guides live in `docs/guides/`, for example `docs/guides/clean-by-hand.md`.

## Index

| Explainer | Feature |
| --- | --- |
| [overview.md](overview.md) | How the parts fit together, from source imagery and roads to a scored road graph. Start here |
| [imagery-fetch.md](imagery-fetch.md) | How `globalurbanmapper_gee` builds the Sentinel-1/2 composite in Earth Engine and downloads it as ROSA-layout tiles for the web app |
| [road-vectors.md](road-vectors.md) | How `roads` turns one CDNGI, Overture or FRST source into the road parquet that `generate` reads |
| [dataset-tiling.md](dataset-tiling.md) | How `generate` turns zone imagery and road vectors into ROSA tiles |
| [dataset-stats.md](dataset-stats.md) | How `norm-stats` and `summary` describe a dataset, and how the models normalise their input bands |
| [zone-cleaning.md](zone-cleaning.md) | How `package-zones` turns the frozen baseline into a zone package for cleaning in QGIS |
| [detectors.md](detectors.md) | How `detect` runs model-agnostic detectors over zone packages and writes their flags |
| [review-queue.md](review-queue.md) | How the review core steps an annotator through a zone's proposals, flags and kept tiles, writes each decision into the zone GeoPackage, accepts or rejects proposals, and undoes a changed decision. Also covers filters, hidden detectors, resuming, and what `progress`, `needs-qgis` and `precision` report |
| [review-plugin.md](review-plugin.md) | How the QGIS plugin docks the review queue, drives the review core through the review service, and keeps QGIS's unsaved edits and the core's writes from overwriting each other |
| [zone-assignment.md](zone-assignment.md) | How `assign-zones` splits the zones between annotators and picks the shared test zones |
| [training-data.md](training-data.md) | How `SAMROAD_Dataset` turns ROSA tiles into cropped, upsampled, augmented samples with road, keypoint and graph labels |
| [models.md](models.md) | How `GeoSAMRoad` segments road and keypoint masks, scores links with TopoNet, and what the sam, terramind, unet and sgcn variants swap |
| [training.md](training.md) | How `geosamroad.train` runs a training job and picks the best checkpoint, and how test, triage, sweeps and the HPC job fit around it |
| [inference.md](inference.md) | How `geosamroad.inferencer` slides patches over each tile, merges the masks and turns them into a road graph |
| [mask-postprocessing.md](mask-postprocessing.md) | How the CRESIv2 port turns a road mask into a road graph by thresholding, thinning and pruning. Nothing calls it yet |
| [evaluation.md](evaluation.md) | How `eval_graphs` scores predicted road graphs against tile graphs with TOPO and APLS |
| [web-app.md](web-app.md) | How the web app turns map cells into an Earth Engine fetch, model inference and GeoJSON roads, and how it is deployed |

## Writing one

Start from the core idea. If the feature was explained to a colleague at a whiteboard, the first two sentences said are the core idea; everything else is detail. For tiling that is: *"Overlay the road vectors on the zone image and cut a fixed grid. Keep the tiles that have roads and clean pixels."*

Use this outline. Leave out a section that has nothing in it.

```markdown
# <Feature name>

<One line: what it is and where it runs.>

## Core idea
Two to five sentences. The naive mental model, with no code names. A reader who stops
here should still predict the feature's behaviour correctly in the common case.

## How it works
The steps in order, in plain language. A short numbered list. Mention a
function name only when the reader would want to open it.

## Details that matter
The specific implementation choices that change the outcome: constants,
thresholds, edge cases, ordering, what gets dropped silently. Each one says
what it is and what effect it has.

## Why it's like this
The decisions behind the design and the alternatives turned down. Link the
ADR when one exists.

## Weak spots and ideas
Known limitations, surprising behaviour, and candidate improvements to the
method, API design, architecture or code structure. Say what is observed
and what is only suspected.

## Code map
The files and main symbols, one line each.
```

Rules:

- **Short.** The core idea fits on one screen; the whole explainer fits in about two. If it grows past that, split the feature or move reference detail into the code's docstrings.
- **Plain words, glossary terms.** Use the terms defined in `GLOSSARY.md`, such as zone, tile, split and raw label. Add a term there when you find a missing one.
- **Symbols, not line numbers.** Write `ROSAProcessor._tile_zone` in `generator/processor.py`. Line numbers go stale with every edit.
- **Concrete.** Give real values, such as 512 px or a 5 m buffer, not "a configurable size".
- **Unslopped.** Run the `pstack:unslop` skill over every explainer you write or edit, before you finish.
- **No guessed reasons.** "Why it's like this" holds only reasons backed by evidence: an ADR, a doc, a commit message, a Linear issue or a code comment. When the reason is unknown, write "Reason unknown, see KEL-<n>" and open a Linear issue that states the question, the evidence found and the candidate reasons. Make it a sub-issue of KEL-12 and label it `ready-for-human`. When the reason is found, write it in and close the issue.
- **Honest weak spots.** The point is for a human to review the design. Write down what looks wrong or awkward, even when it is not being fixed.

## Keeping them true

An explainer is part of the feature. A change that alters how a feature behaves updates its explainer in the same change. A wrong explainer is worse than none, because people trust it.

When a feature has no explainer yet and you are changing it substantially, write one and add it to the index above.
