"""Exceptions raised by pysent processing.

Both subclass :class:`RuntimeError`, so callers that already catch
``RuntimeError`` keep working. They carry no GDAL objects and pickle cleanly,
so they survive the trip back from a worker process.
"""
from __future__ import annotations

from typing import Any

__all__ = ["EmptySceneError", "PartialFailure"]


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
