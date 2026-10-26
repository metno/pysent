"""Exceptions raised by pysent processing.

Most subclass :class:`RuntimeError`, so callers that already catch
``RuntimeError`` keep working; :class:`UnsupportedProductError` is a
:class:`ValueError` instead, because it says the *request* was wrong rather
than that the run failed. None of them carry GDAL objects and all pickle
cleanly, so they survive the trip back from a worker process.
"""
from __future__ import annotations

from typing import Any

__all__ = ["EmptyInputError", "EmptySceneError", "PartialFailure", "UnsupportedProductError"]


class EmptySceneError(RuntimeError):
    """The input has no valid pixels, so there is nothing to stretch.

    Raised instead of a GDAL error so a bulk runner can tell an empty scene
    (skip it) from a real failure (retry or report it).
    """


class PartialFailure(RuntimeError):
    """Some products of a multi-product call failed.

    Every product is attempted before this is raised. ``results`` holds the
    result dicts of the products that finished, in request order; their files
    are in place. ``errors`` maps each failed product (S2 product name or S1
    variable) to its exception.

    A call that requests a single product raises that product's own exception
    instead.
    """

    def __init__(self, message: str, results: list[dict[str, Any]], errors: dict[str, BaseException]):
        # Keep every constructor argument in ``args`` so the exception unpickles.
        super().__init__(message, results, errors)
        self.results = results
        self.errors = errors

    def __str__(self) -> str:
        return str(self.args[0])


class UnsupportedProductError(ValueError):
    """The input is a Sentinel product this release cannot process.

    A :class:`ValueError` rather than a :class:`RuntimeError`: nothing went
    wrong at run time, the request named something that is out of scope. It is
    raised before any file is opened, so a caller can reject a job cheaply.

    Covers three different situations, each spelled out in the message:
    a family that could not be identified at all, a product type that is
    recognised but deliberately out of scope (Sentinel-1 SLC, Sentinel-3
    ``OL_1_EFR``), and a product type that is planned but not implemented in
    this release. The important part is what does *not* happen: an
    unrecognised product never falls through to the Sentinel-1 pipeline.
    """


class EmptyInputError(RuntimeError):
    """The input file exists but holds no data, so there is nothing to read.

    Distinct from :class:`EmptySceneError`, which means a readable product
    whose pixels are all invalid. This one means the file itself is empty —
    in practice a zero-byte archive stub, which is common enough in the NBS
    archive to deserve its own error: about a third of near-real-time
    Sentinel-5P files and 38 of 53 sampled Sentinel-3 ``NR`` files were
    zero bytes. Raised before any GDAL call, so the message says "empty file"
    instead of a driver-level parse failure.
    """
