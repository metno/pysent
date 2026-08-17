"""Input resolution shared by the pysent documentation notebooks.

Every notebook needs the same thing: *some* Sentinel input to demonstrate on.
Where that comes from differs by environment, so resolution is centralised here
and tried in this order:

1. ``SAFE_PATH``     - an explicit ``.SAFE`` / ``.zip`` you point at.
2. ``IDENTIFIER``    - a catalogue UUID, resolved via ``pysent.archive``.
3. ``DATA_ROOT``     - a mounted NBS archive, scanned for the newest product.
4. the committed test fixtures under ``tests/data/`` - small windows cut from
   real scenes, used when nothing else is available.

Cases 1-3 give a full SAFE, so the whole pipeline (warp included) runs. Case 4
is **fixture mode**: the warp cannot run because a windowed extract is not a
SAFE, so those notebooks benchmark and demonstrate ``read -> stretch -> write``
only. This is what lets CI execute the notebooks on every push and catch rot,
without needing gigabytes of Sentinel data.

All four are driven by papermill parameters, so the same notebook serves CI and
a real archive:

    papermill 01_quickstart.ipynb out.ipynb -p DATA_ROOT /data/nbsArchive
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ResolvedInput", "fixture_dir", "resolve_input", "describe"]

# docs/notebooks/nbtools.py -> repo root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "data"

_SAFE_SUFFIXES = (".SAFE", ".zip")


@dataclass
class ResolvedInput:
    """What a notebook should run on, and how much of the pipeline is possible."""

    platform: str            # "S1" | "S2"
    mode: str                # "safe" | "fixture"
    path: Path               # SAFE archive, or fixture GeoTIFF
    label: str               # human-readable product name
    source: str              # how it was found, for the notebook to print

    @property
    def is_fixture(self) -> bool:
        return self.mode == "fixture"

    @property
    def can_warp(self) -> bool:
        """Fixture extracts are plain rasters - there is no SAFE to warp from."""
        return self.mode == "safe"


def fixture_dir() -> Path:
    """Directory holding the committed real-scene extracts."""
    return FIXTURE_DIR


def _fixture(platform: str) -> ResolvedInput:
    import json

    manifest_path = FIXTURE_DIR / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"No Sentinel input available and no fixtures at {FIXTURE_DIR}. "
            "Either mount an archive (DATA_ROOT), pass SAFE_PATH/IDENTIFIER, "
            "or run: python scripts/make_test_data.py"
        )
    manifest = json.loads(manifest_path.read_text())
    product = manifest["products"][platform]
    key = "Amplitude_VV" if platform == "S1" else "rgb"
    artifact = product["artifacts"][key]
    return ResolvedInput(
        platform=platform,
        mode="fixture",
        path=FIXTURE_DIR / artifact["path"],
        label=product["title"],
        source=f"committed test fixture (window {artifact['src_window']} of the real product)",
    )


def _scan_archive(root: Path, platform: str) -> Path | None:
    """Newest matching SAFE product under a mounted archive.

    Layout is ``<root>/<platform><unit>/YYYY/MM/DD/[mode/]<PRODUCT>.zip``; rather
    than assume the depth, glob for products and pick the newest by name (the
    sensing timestamp sorts correctly).
    """
    if not root.is_dir():
        return None
    pattern = "S1*_*GRD*" if platform == "S1" else "S2*_MSIL*"
    candidates = [
        p for p in root.rglob(pattern)
        if p.suffix in _SAFE_SUFFIXES or p.name.endswith(".SAFE")
    ]
    return max(candidates, key=lambda p: p.name) if candidates else None


def resolve_input(
    platform: str,
    *,
    safe_path: str | os.PathLike[str] | None = None,
    identifier: str | None = None,
    data_root: str | os.PathLike[str] | None = None,
    endpoint: str | None = None,
) -> ResolvedInput:
    """Find something to run on, preferring a real SAFE over the fixtures."""
    platform = platform.upper()
    if platform not in ("S1", "S2"):
        raise ValueError(f"platform must be 'S1' or 'S2', got {platform!r}")

    if safe_path:
        path = Path(safe_path)
        if not path.exists():
            raise FileNotFoundError(f"SAFE_PATH does not exist: {path}")
        return ResolvedInput(platform, "safe", path, path.name, "explicit SAFE_PATH")

    if identifier:
        from pysent.archive import resolve_safe_archive_from_uuid

        resolution = resolve_safe_archive_from_uuid(identifier, endpoint=endpoint)
        if not resolution.exists:
            raise FileNotFoundError(
                f"Catalogue resolved {identifier} to {resolution.safe_path}, which is not "
                "present locally. Run where the archive is mounted, or set NBS_ARCHIVE_ROOT."
            )
        return ResolvedInput(
            platform=resolution.platform.get("family", platform),
            mode="safe",
            path=resolution.safe_path,
            label=resolution.metadata.get("title") or identifier,
            source=f"catalogue UUID {identifier}",
        )

    root = Path(data_root or os.environ.get("NBS_ARCHIVE_ROOT", "/data/nbsArchive"))
    found = _scan_archive(root, platform)
    if found is not None:
        return ResolvedInput(platform, "safe", found, found.name, f"newest {platform} product under {root}")

    return _fixture(platform)


def describe(resolved: ResolvedInput) -> str:
    """One-block summary for a notebook to print after resolution."""
    lines = [
        f"platform : {resolved.platform}",
        f"product  : {resolved.label}",
        f"path     : {resolved.path}",
        f"source   : {resolved.source}",
        f"mode     : {resolved.mode}",
    ]
    if resolved.is_fixture:
        lines.append(
            "\nFIXTURE MODE - this is a small window cut from a real scene, not a SAFE\n"
            "archive, so the warp step is skipped and only read -> stretch -> write runs.\n"
            "Mount an archive and set DATA_ROOT (or pass SAFE_PATH / IDENTIFIER) for the\n"
            "full pipeline."
        )
    return "\n".join(lines)
