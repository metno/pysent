"""Benchmark and quality-evaluation harness for the pysent processing routines.

This is the tooling behind the Sentinel GeoTIFF QA notebooks: a step timer that
also records peak process RSS (so GDAL's C-side allocations are visible), raster
statistics, over/under-stretch reports, and the plotting helpers used to eyeball
results.

Install with the ``qa`` extra for the pandas/matplotlib-backed helpers::

    pip install pysent[qa]

Everything is re-exported here, so notebooks can do::

    import pysent.qa as squ
"""
from __future__ import annotations

from .quality import *  # noqa: F401,F403
from .quality import __all__ as _quality_all

__all__ = list(_quality_all)
