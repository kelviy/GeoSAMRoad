# GlobalUrbanMapper Google Earth Engine Javascript to Python Port
This is a python port of the GlobalUrbanMapper javascript code from Google Earth Engine. It is utilised to fetch a Sentinel Satellite Imagery from a specified region.

## Usage

```python
from globalurbanmapper_gee import api

result = api.fetch_region(
    lon=30.47, lat=-22.94, width_km=25, height_km=25,
    start_date="2020-01-01", end_date="2021-01-01",
    band_list="s1s2",
    out_dir="tiles/thohoyandou",   # one 512 px COG per tile + metadata.csv
)
```

Output modes: `out_path` (one COG over the region of interest), `out_dir` (multiple 512 px tiles over region of interest), or neither (returns a `(C, H, W)` float32 array).

## Band lists

- rgb - enhanced rgb
- s1s2 - enhanced rgb, sentinel2, sentinel 1 bands

## Auth

```bash
export GEE_PROJECT=your-cloud-project
export GEE_SERVICE_ACCOUNT=svc@your-project.iam.gserviceaccount.com
export GEE_PRIVATE_KEY_FILE=/path/to/key.json
```

## Citation
```
@article{ZHOU2024114242,
  title = {Building up a data engine for global urban mapping},
  journal = {Remote Sensing of Environment},
  volume = {311},
  pages = {114242},
  year = {2024},
  issn = {0034-4257},
  doi = {https://doi.org/10.1016/j.rse.2024.114242},
  url = {https://www.sciencedirect.com/science/article/pii/S0034425724002608},
  author = {Yuhan Zhou and Qihao Weng},
}
```

Github Repo: https://github.com/LauraChow77/GlobalUrbanMapper
GEE Link: https://code.earthengine.google.com/0b3fc6acd36c3d651eea522dc2ba8b25?noload=true
