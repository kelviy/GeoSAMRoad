# GeoSAMRoad docs

These docs show you how to clean the ROSA road labels and how the pipeline behind them works.

- **[Guides](guides/setup.md)** walk you through the work, step by step. Start with [Setup](guides/setup.md).
- **[How it works](explainers/README.md)** explains how each part is built and why. Read it when you want to change a part or question a design.
- The **[Glossary](glossary.md)** defines the terms both sections use, such as zone, tile, flag and proposal.

## The cleaning flow

Kelviy packages each zone and runs the detectors. You claim a zone, review its flags and proposals with the plugin, fix the rest by hand, and upload the zone again. The assembler and retiler then turn the cleaned zones back into the ROSA dataset that the models train on. These two steps are **not built yet**.

```mermaid
flowchart TB
    subgraph prep [Prepare and review]
        direction LR
        raw[Raw labels<br>and imagery] --> pkg[Package zones] --> det[Run detectors] --> claim[Claim and<br>download a zone] --> review[Review with<br>the plugin]
    end
    subgraph rebuild [Clean and rebuild]
        direction LR
        hand[Clean by hand<br>in QGIS] --> back[Hand back<br>your zone] --> asm[Assemble<br>not built yet] --> tile[Retile<br>not built yet] --> ds[ROSA dataset<br>for the models]
    end
    prep --> rebuild

    classDef you fill:#d6ecff,stroke:#1f6fb2,color:#0b2a44
    classDef pending stroke-dasharray:5 5
    class claim,review,hand,back you
    class asm,tile pending
```

The blue steps are yours. The dashed steps do not exist yet.

| Step | Who | Guide |
| --- | --- | --- |
| Package zones and run detectors | kelviy, or anyone changing a detector | [Change a detector or the packager](guides/change-detector-or-packager.md) |
| Claim and download a zone | you | [File Downloads](guides/claim-and-download.md) |
| Review flags and proposals | you | [Review with the plugin](guides/review-with-plugin.md) |
| Fix what is left | you | [Clean by hand in QGIS](guides/clean-by-hand.md) |
| Upload the cleaned zone | you | [Hand back your zone](guides/hand-back.md) |
| Assemble and retile | not built yet | [Assemble and retile](guides/assemble-and-retile.md) |

The files each step needs, and where to get them, are on [Data Description](guides/data-and-files.md).
