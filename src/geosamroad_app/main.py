"""GeoSAMRoad web app -- one FastAPI process serving the API and the UI.

Config is environment only, so deploying is `docker compose up`:

    GEOSAMROAD_CHECKPOINTS  dir holding <variant>/*.ckpt   (default /app/checkpoints)
    GEOSAMROAD_DATA         scratch + preview dir           (default /app/data)
    GEE_PROJECT / GEE_SERVICE_ACCOUNT / GEE_PRIVATE_KEY_FILE   Earth Engine auth
"""
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from geosamroad_app import grid, jobs, pipeline, variants

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger(__name__)

CHECKPOINT_ROOT = Path(os.getenv("GEOSAMROAD_CHECKPOINTS", "/app/checkpoints"))
DATA_ROOT = Path(os.getenv("GEOSAMROAD_DATA", "/app/data"))
STATIC_DIR = Path(__file__).resolve().parent / "static"

DATA_ROOT.mkdir(parents=True, exist_ok=True)
(DATA_ROOT / "previews").mkdir(parents=True, exist_ok=True)

app = FastAPI(title="GeoSAMRoad", docs_url="/api/docs", openapi_url="/api/openapi.json")


class RunRequest(BaseModel):
    tile_ids: list[str] = Field(..., min_length=1, max_length=24)
    variant: str = variants.DEFAULT_VARIANT
    date: str = "2020-06-01"


@app.get("/api/config")
def get_config():
    """What the UI needs to render itself: variants present, grid size, defaults."""
    available = variants.available(CHECKPOINT_ROOT)
    return {
        "variants": available,
        "default_variant": (available[0]["name"] if available
                            else variants.DEFAULT_VARIANT),
        "tile_km": round(grid.TILE_M / 1000, 2),
        "max_tiles_per_view": grid.MAX_TILES_PER_VIEW,
        "max_tiles_per_job": 24,
        "checkpoints_found": bool(available),
    }


@app.get("/api/tiles")
def get_tiles(west: float, south: float, east: float, north: float):
    """The selectable grid for a viewport, as GeoJSON.

    These are the exact cells the fetcher would cut -- hovering one shows
    precisely what would be processed.
    """
    try:
        return grid.tiles_for_viewport(west, south, east, north)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/run")
def run(request: RunRequest):
    """Queue a fetch+infer job over the selected cells."""
    if not variants.available(CHECKPOINT_ROOT):
        raise HTTPException(
            status_code=503,
            detail=f"No checkpoints found under {CHECKPOINT_ROOT}. "
                   "Mount them and restart.",
        )
    try:
        variants.get_variant(request.variant)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    for tile_id in request.tile_ids:
        try:
            grid.parse_tile_id(tile_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    def run_fn(job, progress):
        return pipeline.run(
            job.tile_ids, job.variant, job.date,
            data_root=DATA_ROOT / "jobs" / job.id,
            checkpoint_root=CHECKPOINT_ROOT,
            progress=progress,
        )

    job = jobs.registry.submit(request.tile_ids, request.variant, request.date, run_fn)
    log.info("Queued job %s: %d tile(s), variant=%s",
             job.id, len(request.tile_ids), request.variant)
    return job.as_dict(include_result=False)


@app.get("/api/jobs")
def list_jobs():
    return {"jobs": jobs.registry.list(), "busy": jobs.registry.busy()}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No job {job_id}")
    return job.as_dict()


@app.get("/api/previews/{job_id}/{tile_id}.png")
def get_preview(job_id: str, tile_id: str):
    """True-colour preview of what the model saw for one tile."""
    path = DATA_ROOT / "jobs" / job_id / "previews" / f"{tile_id}.png"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No preview for that tile")
    return FileResponse(path, media_type="image/png")


@app.get("/healthz")
def healthz():
    return {"ok": True, "checkpoints": bool(variants.available(CHECKPOINT_ROOT))}


# Mounted last so /api/* wins; html=True serves index.html at /.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
