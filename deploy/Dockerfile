# Single-container GeoSAMRoad web app: API + UI + model in one process.
# Build from the repo root:  docker compose -f deploy/docker-compose.yml up -d --build
FROM ubuntu:24.04 AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH=/app/.venv/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git \
        gdal-bin libgdal-dev libspatialindex-dev libproj-dev \
        build-essential python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# ---- dependencies (cached until pyproject/lock change) ----
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra app --extra geosamroad --extra globalurbanmapper \
            --no-install-project

# The GTX-1060-class GPU this is aimed at needs cu121; the default cu128 wheels
# will not run on it. Re-pin LAST so it wins over whatever uv sync resolved.
# Build with --build-arg TORCH_VARIANT=cpu on a host without an NVIDIA GPU.
ARG TORCH_VARIANT=cu121
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ "$TORCH_VARIANT" = "cpu" ]; then \
        echo "TORCH_VARIANT=cpu -> keeping the resolved CPU wheels"; \
    else \
        UV_TORCH_BACKEND=cu121 uv pip install --python /app/.venv/bin/python \
            "torch==2.5.1" "torchvision==0.20.1" --torch-backend=cu121; \
    fi

# ---- vendored map library (no CDN at runtime, works offline) ----
ARG MAPLIBRE_VERSION=4.7.1
RUN mkdir -p /app/vendor && \
    curl -fsSL "https://unpkg.com/maplibre-gl@${MAPLIBRE_VERSION}/dist/maplibre-gl.js" \
         -o /app/vendor/maplibre-gl.js && \
    curl -fsSL "https://unpkg.com/maplibre-gl@${MAPLIBRE_VERSION}/dist/maplibre-gl.css" \
         -o /app/vendor/maplibre-gl.css

# ---- application source ----
COPY src /app/src
RUN cp -r /app/vendor /app/src/geosamroad_app/static/vendor

ENV PYTHONPATH=/app/src \
    GEOSAMROAD_CHECKPOINTS=/app/checkpoints \
    GEOSAMROAD_DATA=/app/data

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["uvicorn", "geosamroad_app.main:app", "--host", "0.0.0.0", "--port", "8000"]
