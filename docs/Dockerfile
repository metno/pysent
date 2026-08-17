# Executable documentation environment for pysent.
#
# Ships the library, the geo stack and Jupyter, so the notebooks under
# docs/notebooks/ can be run as-is - by CI against the small committed test
# fixtures, and by operators against a real mounted Sentinel archive.
#
#   docker build -f docs/Dockerfile -t pysent-docs .
#   docker run --rm -p 8888:8888 \
#       -v /lustre/storeB/NBS2/.../nbsArchive:/data/nbsArchive:ro \
#       pysent-docs
#
# The geo stack comes from Debian packages, not pip: GDAL's Python bindings have
# no reliable wheel, and mixing an apt GDAL with a pip rasterio (which bundles
# its own) causes hard-to-debug loader conflicts. This mirrors how the
# FastAPI-mapserver runtime image is built, so what you benchmark here matches
# production.
FROM debian:forky-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    JUPYTER_PLATFORM_DIRS=1 \
    # Where a mounted Sentinel archive is expected; notebooks default to this.
    NBS_ARCHIVE_ROOT=/data/nbsArchive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 \
       python3-pip \
       python3-numpy \
       python3-rasterio \
       python3-gdal \
       python3-numba \
       python3-matplotlib \
       python3-pandas \
       gdal-bin \
       git \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Jupyter + the notebook executor. --break-system-packages because the geo stack
# above is Debian-managed; these are pure-Python and safe to layer on top.
RUN python3 -m pip install --no-cache-dir --break-system-packages \
       jupyterlab \
       ipykernel \
       papermill

WORKDIR /opt/pysent

# Install the library itself. Copied in two steps so dependency metadata is
# cached independently of the notebooks, which change far more often.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python3 -m pip install --no-cache-dir --break-system-packages --no-deps -e '.[csw]' \
    && python3 -m pip install --no-cache-dir --break-system-packages owslib

COPY docs ./docs
COPY scripts ./scripts
COPY tests ./tests

# Fail the build early if the geo stack or the library is broken, rather than
# surfacing it as a confusing notebook error later.
RUN python3 -c "\
import pysent, rasterio; from osgeo import gdal; \
from pysent.s1 import process_sentinel_s1_safe; \
from pysent.s2 import process_sentinel_s2_safe; \
import pysent.qa; \
print('pysent', pysent.__version__, '| GDAL', gdal.__version__, '| rasterio', rasterio.__version__)"

EXPOSE 8888
VOLUME ["/data"]

CMD ["jupyter", "lab", "--ip=0.0.0.0", "--port=8888", "--no-browser", \
     "--allow-root", "--ServerApp.token=", "--ServerApp.root_dir=/opt/pysent/docs/notebooks"]
