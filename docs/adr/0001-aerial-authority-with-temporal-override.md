---
status: accepted
---

# Reference imagery decides road position; 2020 Sentinel-2 decides existence on a clear temporal mismatch

The ROSA cleaning spec uses the high-resolution CDNGI aerial basemap as the authority for whether a road exists and where it lies, because 10 m Sentinel-2 is too coarse to judge either reliably. A road that is invisible in Sentinel-2 is therefore *not* removed on that basis. The aerial imagery is not from 2020, though, and the labels must describe the 2020 Sentinel-2 image they are paired with. So when a road is clearly present in one source and clearly absent in the other, the 2020 Sentinel-2 imagery wins. The road is kept or added (traced from Sentinel-2, with no misalignment correction), or it is removed, and the label revision gets the `temporal` reason. When either source is unclear, the aerial imagery decides and the raw label stands.

## Considered options

- **Sentinel-2 only**: label only what is visible at 10 m. Rejected because it bakes the model's limits into the ground truth and is highly subjective.
- **Aerial only**: rejected because it labels roads built or removed after 2020 that the paired image cannot show.
