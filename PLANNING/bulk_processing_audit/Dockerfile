FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3-gdal python3-rasterio python3-numpy python3-pytest python3-pip gdal-bin ca-certificates time && rm -rf /var/lib/apt/lists/* \
 && pip install --break-system-packages --no-deps numba==0.60.0 llvmlite==0.43.0
ENV PYTHONPATH=/src/src PYTHONWARNINGS=ignore::FutureWarning
WORKDIR /work
