"""Robustness of the processing entry points under unattended, bulk use.

Covers findings B1-B4, B6-B8 and B10-B12 of
``PLANNING/TODO_PLANNING_bulk_processing.md``. The SAFE warp is replaced by a
fake that writes a synthetic warped raster, so everything downstream of it
(scratch files, atomic writes, stretch, error collection, GDAL settings) runs
for real without a Sentinel product.
"""
from __future__ import annotations

import os
import pickle
import subprocess
import sys
import textwrap
from concurrent.futures import Future, ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
gdal = pytest.importorskip("osgeo.gdal")

from rasterio.transform import from_origin  # noqa: E402

from pysent import s1, s2  # noqa: E402

S2_PRODUCTS = ("true_color_vegetation", "false_color_glacier", "false_color_vegetation")


def _write_raster(path: Path, data: np.ndarray) -> None:
    data = data if data.ndim == 3 else data[None]
    with rasterio.open(
        path, "w", driver="GTiff", height=data.shape[1], width=data.shape[2], count=data.shape[0],
        dtype=data.dtype, crs="EPSG:32633", transform=from_origin(0, 1000, 10, 10), nodata=0,
    ) as dst:
        dst.write(data)


def _rgb(fill: int | None = None) -> np.ndarray:
    if fill is not None:
        return np.full((3, 64, 64), fill, dtype="uint16")
    rgb = np.random.default_rng(0).integers(1, 9000, size=(3, 64, 64)).astype("uint16")
    rgb[:, :8, :] = 0
    return rgb


def _files(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


# Band resolutions of a real S2 product, so grid grouping behaves as it would on a scene.
_BAND_RESOLUTION = {
    "B1": 60.0, "B2": 10.0, "B3": 10.0, "B4": 10.0, "B5": 20.0, "B6": 20.0, "B7": 20.0,
    "B8": 10.0, "B8A": 20.0, "B9": 60.0, "B10": 60.0, "B11": 20.0, "B12": 20.0,
}
FAKE_SOURCES = {
    band: {"subdataset_name": f"FAKE:{band}", "band_index": 1, "resolution": resolution, "epsg": "EPSG:32633"}
    for band, resolution in _BAND_RESOLUTION.items()
}


class FakeS2Stack:
    """Stands in for the scene warp: writes one raster band per requested band.

    Band *k* carries the marker value ``1000 * k``, so a product's output shows
    which bands of the stack it actually read.
    """

    def __init__(self, *, fail_for: tuple[str, ...] = (), fill: int | None = None, marker: bool = False):
        self.fail_for = fail_for
        self.fill = fill
        self.marker = marker
        self.calls: list[dict] = []

    def __call__(self, input_dataset, band_names, warped_path, *, target_epsg=None, target_resolution=None, **kwargs):
        band_names = tuple(band_names)
        self.calls.append({
            "band_names": band_names,
            "warped_path": Path(warped_path),
            "target_epsg": target_epsg,
            "target_resolution": target_resolution,
            "compression": kwargs.get("compression"),
            "GDAL_NUM_THREADS": gdal.GetConfigOption("GDAL_NUM_THREADS"),
            "cachemax": gdal.GetCacheMax(),
        })
        stack = np.zeros((len(band_names), 64, 64), dtype="uint16")
        for index in range(len(band_names)):
            if self.fill is not None:
                stack[index] = self.fill
            elif self.marker:
                stack[index] = 1000 * (index + 1)
            else:
                stack[index] = np.random.default_rng(index).integers(1, 9000, size=(64, 64)).astype("uint16")
                stack[index, :8, :] = 0  # nodata strip
        _write_raster(Path(warped_path), stack)  # the intermediate exists before any failure
        if any(band in self.fail_for for band in band_names):
            raise RuntimeError("simulated warp failure")
        return target_epsg, target_resolution


@pytest.fixture
def fake_s2_warp(monkeypatch):
    def install(**kwargs) -> FakeS2Stack:
        fake = FakeS2Stack(**kwargs)
        monkeypatch.setattr(s2, "_collect_sentinel_s2_band_sources", lambda dataset: dict(FAKE_SOURCES))
        monkeypatch.setattr(s2, "_warp_sentinel_s2_bands", fake)
        return fake
    return install


@pytest.fixture
def record_product_writes(monkeypatch):
    """Record the GDAL settings seen while each product is written."""
    calls: list[dict] = []
    real_writer = s2._write_stretched_sentinel_s2_rgb

    def writer(subset_path, output_path, **kwargs):
        calls.append({
            "GDAL_NUM_THREADS": gdal.GetConfigOption("GDAL_NUM_THREADS"),
            "cachemax": gdal.GetCacheMax(),
        })
        return real_writer(subset_path, output_path, **kwargs)

    monkeypatch.setattr(s2, "_write_stretched_sentinel_s2_rgb", writer)
    return calls


def run_s2(output_dir: Path, products=S2_PRODUCTS[:1], **options):
    return s2.process_sentinel_s2_safe(
        input_dataset="unused.zip",
        output_dir=output_dir,
        product_bands={name: s2.S2_DEFAULT_PRODUCTS[name] for name in products},
        output_names={name: f"scene_{name}.tif" for name in products},
        processing_options={"histogram_stretch": True, "overview_factors": [2], **options},
    )


# --------------------------------------------------------------------------- #
# B2/B3: scratch directory and atomic outputs
# --------------------------------------------------------------------------- #
def test_s2_success_leaves_only_the_final_output(tmp_path, fake_s2_warp):
    fake = fake_s2_warp()
    results = run_s2(tmp_path / "out")
    assert _files(tmp_path / "out") == ["scene_true_color_vegetation.tif"]
    assert results[0]["path"] == str(tmp_path / "out" / "scene_true_color_vegetation.tif")
    with rasterio.open(results[0]["path"]) as ds:
        assert ds.colorinterp[0].name == "red"
    # The intermediate was not named after the output in output_dir, where two
    # jobs writing the same name would overwrite each other's.
    assert fake.calls[0]["warped_path"].parent != tmp_path / "out"


def test_s2_intermediates_go_to_work_dir(tmp_path, fake_s2_warp):
    fake = fake_s2_warp()
    run_s2(tmp_path / "out", work_dir=str(tmp_path / "scratch"))
    assert fake.calls[0]["warped_path"].parent.parent == tmp_path / "scratch"
    assert _files(tmp_path / "scratch") == []
    assert _files(tmp_path / "out") == ["scene_true_color_vegetation.tif"]


@pytest.mark.parametrize("work_dir", [None, "scratch"])
def test_s2_failed_warp_leaves_no_intermediates(tmp_path, fake_s2_warp, work_dir):
    fake_s2_warp(fail_for=("B4",))
    options = {"work_dir": str(tmp_path / work_dir)} if work_dir else {}
    with pytest.raises(RuntimeError, match="simulated warp failure"):
        run_s2(tmp_path / "out", **options)
    assert _files(tmp_path / "out") == []
    if work_dir:
        assert _files(tmp_path / work_dir) == []


def test_s2_failed_write_leaves_no_output_under_the_final_name(tmp_path, fake_s2_warp, monkeypatch):
    fake_s2_warp()

    def broken_writer(warped_path, output_path, **kwargs):
        Path(output_path).write_bytes(b"II*\x00 truncated")
        raise RuntimeError("simulated write failure")

    monkeypatch.setattr(s2, "_write_stretched_sentinel_s2_rgb", broken_writer)
    with pytest.raises(RuntimeError, match="simulated write failure"):
        run_s2(tmp_path / "out")
    assert _files(tmp_path / "out") == []


KILLED_MID_WRITE = textwrap.dedent('''
    import os, signal, sys
    from pathlib import Path
    import numpy as np, rasterio
    from rasterio.transform import from_origin
    from pysent import s2

    def fake_warp(input_dataset, band_names, warped_path, **kwargs):
        bands = len(tuple(band_names))
        data = np.random.default_rng(0).integers(1, 9000, size=(bands, 256, 256)).astype("uint16")
        with rasterio.open(warped_path, "w", driver="GTiff", height=256, width=256, count=bands, dtype="uint16",
                           crs="EPSG:32633", transform=from_origin(0, 1000, 10, 10), nodata=0) as dst:
            dst.write(data)
        return "EPSG:32633", 10.0

    real_writer = s2._write_stretched_sentinel_s2_rgb

    def killed_writer(warped_path, output_path, **kwargs):
        real_writer(warped_path, output_path, **kwargs)
        os.truncate(output_path, os.path.getsize(output_path) // 2)
        os.kill(os.getpid(), signal.SIGKILL)  # OOM killer / Slurm time limit, mid-write

    s2._collect_sentinel_s2_band_sources = lambda dataset: {
        band: {"subdataset_name": "FAKE", "band_index": 1, "resolution": 10.0, "epsg": "EPSG:32633"}
        for band in s2.S2_SUPPORTED_BANDS
    }
    s2._warp_sentinel_s2_bands = fake_warp
    s2._write_stretched_sentinel_s2_rgb = killed_writer
    name = "true_color_vegetation"
    s2.process_sentinel_s2_safe(
        input_dataset="unused.zip", output_dir=Path(sys.argv[1]),
        product_bands={name: s2.S2_DEFAULT_PRODUCTS[name]}, output_names={name: "scene.tif"},
        processing_options={"histogram_stretch": True},
    )
''')


def test_s2_worker_killed_mid_write_leaves_no_final_file(tmp_path):
    script = tmp_path / "killed.py"
    script.write_text(KILLED_MID_WRITE)
    out = tmp_path / "out"
    proc = subprocess.run([sys.executable, str(script), str(out)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == -9, proc.stderr
    # Resume logic that skips existing outputs must not see a truncated file as done.
    assert not (out / "scene.tif").exists()


def test_s1_failed_stretch_leaves_no_intermediates_or_output(tmp_path, monkeypatch):
    def fake_warp(input_dataset, variable, warped_path, **kwargs):
        _write_raster(Path(warped_path), np.random.default_rng(0).random((64, 64), dtype=np.float32) + 1)

    def broken_writer(warped_path, output_path, **kwargs):
        Path(output_path).write_bytes(b"II*\x00 truncated")
        raise RuntimeError("simulated stretch failure")

    monkeypatch.setattr(s1, "_warp_sentinel_s1_amplitude", fake_warp)
    monkeypatch.setattr(s1, "_write_quicklook_from_warped", broken_writer)
    with pytest.raises(RuntimeError, match="simulated stretch failure"):
        s1.process_sentinel_s1_netcdf(
            input_dataset="unused.nc", output_dir=tmp_path / "out", output_names={"Amplitude_VV": "scene_vv.tif"},
        )
    assert _files(tmp_path / "out") == []


def test_s1_success_writes_final_output_only(tmp_path, monkeypatch):
    def fake_warp(input_dataset, variable, warped_path, **kwargs):
        _write_raster(Path(warped_path), np.random.default_rng(0).random((64, 64), dtype=np.float32) + 1)

    monkeypatch.setattr(s1, "_warp_sentinel_s1_amplitude", fake_warp)
    results = s1.process_sentinel_s1_netcdf(
        input_dataset="unused.nc", output_dir=tmp_path / "out",
        output_names={"Amplitude_VV": "scene_vv.tif", "Amplitude_VH": "scene_vh.tif"},
        processing_options={"work_dir": str(tmp_path / "scratch")},
    )
    assert [r["variable"] for r in results] == ["Amplitude_VV", "Amplitude_VH"]
    assert _files(tmp_path / "out") == ["scene_vh.tif", "scene_vv.tif"]
    assert _files(tmp_path / "scratch") == []


# --------------------------------------------------------------------------- #
# P2/P4: one warp per scene
# --------------------------------------------------------------------------- #
def test_scene_bands_are_warped_once_for_all_products(tmp_path, fake_s2_warp):
    # The three default products share bands (B3 in all three, B4 and B8A in two);
    # warping per product decoded them again each time.
    fake = fake_s2_warp()
    results = run_s2(tmp_path / "out", S2_PRODUCTS)
    assert len(fake.calls) == 1
    assert fake.calls[0]["band_names"] == ("B4", "B3", "B2", "B12", "B8A")
    assert fake.calls[0]["compression"] == "NONE"  # the stack is read once and deleted
    assert len(results) == 3
    assert _files(tmp_path / "out") == sorted(f"scene_{name}.tif" for name in S2_PRODUCTS)


def test_products_on_different_grids_get_their_own_stack(tmp_path, fake_s2_warp):
    # A 20 m-only product must not be dragged onto the 10 m grid of another product.
    fake = fake_s2_warp()
    s2.process_sentinel_s2_safe(
        input_dataset="unused.zip",
        output_dir=tmp_path / "out",
        product_bands={"ten": ("B4", "B3", "B2"), "twenty": ("B12", "B11", "B8A")},
        output_names={"ten": "ten.tif", "twenty": "twenty.tif"},
        processing_options={"histogram_stretch": True, "overview_factors": [2]},
    )
    assert [call["target_resolution"] for call in fake.calls] == [10.0, 20.0]
    assert [call["band_names"] for call in fake.calls] == [("B4", "B3", "B2"), ("B12", "B11", "B8A")]


def test_coarser_target_resolution_keeps_one_stack_per_product(tmp_path, fake_s2_warp):
    # Downsampling warps from source overviews, and which one GDAL picks depends
    # on the bands in the stack, so sharing there would change the output.
    fake = fake_s2_warp()
    run_s2(tmp_path / "out", S2_PRODUCTS, target_resolution=60)
    assert len(fake.calls) == 3
    assert [call["band_names"] for call in fake.calls] == [s2.S2_DEFAULT_PRODUCTS[name] for name in S2_PRODUCTS]
    assert {call["target_resolution"] for call in fake.calls} == {60.0}


def test_each_product_reads_its_own_bands_from_the_stack(tmp_path, fake_s2_warp):
    # Stack band k holds 1000 * k; without the stretch those values pass through,
    # so the output proves which stack bands the product selected, and in what order.
    fake_s2_warp(marker=True)
    results = run_s2(tmp_path / "out", S2_PRODUCTS, histogram_stretch=False)
    stack_bands = ("B4", "B3", "B2", "B12", "B8A")
    for result in results:
        with rasterio.open(result["path"]) as ds:
            values = [int(ds.read(index)[32, 32]) for index in (1, 2, 3)]
        assert values == [1000 * (stack_bands.index(band) + 1) for band in result["bands"]]


def test_failed_scene_warp_fails_every_product_of_that_grid(tmp_path, fake_s2_warp):
    from pysent.errors import PartialFailure

    fake_s2_warp(fail_for=("B3",))
    with pytest.raises(PartialFailure) as caught:
        run_s2(tmp_path / "out", S2_PRODUCTS)
    assert sorted(caught.value.errors) == sorted(S2_PRODUCTS)
    assert caught.value.results == []
    assert _files(tmp_path / "out") == []


# --------------------------------------------------------------------------- #
# P1: the quicklook preset
# --------------------------------------------------------------------------- #
def test_quicklook_preset_moves_the_output_grid(tmp_path, fake_s2_warp):
    fake = fake_s2_warp()
    run_s2(tmp_path / "out", preset="quicklook")
    assert fake.calls[0]["target_resolution"] == 60.0

    settings = s1._resolve_sentinel_s1_processing_settings({"preset": "quicklook"}, output_count=1)
    assert settings["target_resolution"] == 160.0


def test_an_explicit_option_beats_the_preset(tmp_path, fake_s2_warp):
    fake = fake_s2_warp()
    run_s2(tmp_path / "out", preset="quicklook", target_resolution=20)
    assert fake.calls[0]["target_resolution"] == 20.0


def test_the_default_preset_changes_nothing(tmp_path, fake_s2_warp):
    fake = fake_s2_warp()
    run_s2(tmp_path / "out")
    assert fake.calls[0]["target_resolution"] == 10.0  # the scene's own resolution


@pytest.mark.parametrize("module", [s1, s2])
def test_an_unknown_preset_is_rejected(module):
    resolve = (
        module._resolve_sentinel_s1_processing_settings
        if module is s1
        else module._resolve_sentinel_s2_processing_settings
    )
    with pytest.raises(ValueError, match="preset"):
        resolve({"preset": "thumbnail"}, output_count=1)


# --------------------------------------------------------------------------- #
# B9: options that used to do nothing
# --------------------------------------------------------------------------- #
def _fake_s1_warp(recorder: list):
    def warp(input_dataset, variable, warped_path, **kwargs):
        recorder.append(kwargs)
        _write_raster(Path(warped_path), np.random.default_rng(0).random((64, 64), dtype=np.float32) + 1)

    return warp


def run_s1_safe(output_dir: Path, monkeypatch, recorder: list, **options):
    monkeypatch.setattr(s1, "_warp_sentinel_s1_safe_amplitude", _fake_s1_warp(recorder))
    monkeypatch.setattr(s1, "detect_sentinel_s1_polarizations", lambda dataset: ["Amplitude_VV"])
    return s1.process_sentinel_s1_safe(
        input_dataset="unused.zip", output_dir=output_dir,
        output_names={"Amplitude_VV": "vv.tif"}, processing_options=options,
    )


@pytest.mark.parametrize("use_tps", [True, False])
def test_s1_use_tps_reaches_the_warp(tmp_path, monkeypatch, use_tps):
    # B9b: the option existed on the warp but no caller could set it.
    calls: list = []
    run_s1_safe(tmp_path / "out", monkeypatch, calls, use_tps=use_tps)
    assert calls[0]["use_tps"] is use_tps


def test_s1_defaults_to_the_thin_plate_spline_warp(tmp_path, monkeypatch):
    calls: list = []
    run_s1_safe(tmp_path / "out", monkeypatch, calls)
    assert calls[0]["use_tps"] is True


def test_s1_accepts_either_stretch_spelling(tmp_path, monkeypatch):
    # B9c: passing the Sentinel-2 spelling to S1 used to fall back to the defaults.
    calls: list = []
    results = run_s1_safe(tmp_path / "a", monkeypatch, calls, stretch_percentiles=(5.0, 95.0))
    assert results[0]["stretch"]["p_low"] >= 0

    settings = s1._resolve_sentinel_s1_processing_settings({"stretch_percentiles": (5.0, 95.0)}, output_count=1)
    assert settings["percentiles"] == (5.0, 95.0)

    with pytest.deprecated_call(match="stretch_percentiles"):
        legacy = s1._resolve_sentinel_s1_processing_settings(
            {"histogram_stretch": {"percentiles": (10.0, 90.0)}}, output_count=1
        )
    assert legacy["percentiles"] == (10.0, 90.0)


# --------------------------------------------------------------------------- #
# B1/B6: numba
# --------------------------------------------------------------------------- #
def _python_env(**overrides) -> dict[str, str]:
    env = dict(os.environ, PYTHONWARNINGS="ignore", **overrides)
    return {key: value for key, value in env.items() if value is not None}


NUMBA_FROM_THREADS = textwrap.dedent('''
    import threading
    import numpy as np
    from pysent.s1 import stretch_sentinel_s1_grayscale

    data = np.random.default_rng(0).random((2000, 2000), dtype=np.float32) + 0.1
    data[:, :100] = 0
    reference = stretch_sentinel_s1_grayscale(data, nodata=0.0, use_numba=False)
    errors = []

    def run():
        try:
            for _ in range(5):
                gray, alpha, _ = stretch_sentinel_s1_grayscale(data, nodata=0.0, use_numba=True)
                # The kernel rounds in float32 and numpy in float64: equal to +-1 grey level.
                assert np.abs(gray.astype(int) - reference[0]).max() <= 1
                assert np.array_equal(alpha, reference[1])
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
''')


def test_s1_numba_stretch_is_safe_from_threads(tmp_path):
    pytest.importorskip("numba")
    # In a subprocess: on failure numba aborts the whole interpreter.
    proc = subprocess.run(
        [sys.executable, "-c", NUMBA_FROM_THREADS],
        capture_output=True, text=True, timeout=300, env=_python_env(NUMBA_CACHE_DIR=str(tmp_path)),
    )
    assert proc.returncode == 0, proc.stderr[-2000:]


NUMBA_WITHOUT_CACHE_LOCATION = textwrap.dedent('''
    import numba.core.caching as caching
    # Nowhere to cache: read-only package directory, no writable HOME, no NUMBA_CACHE_DIR.
    caching.CacheImpl._locator_classes = []
    import numpy as np
    from pysent.s1 import stretch_sentinel_s1_grayscale
    gray, alpha, _ = stretch_sentinel_s1_grayscale(np.arange(1, 101, dtype=np.float32).reshape(10, 10), use_numba=True)
    assert alpha.all() and gray.max() == 255
''')


def test_s1_imports_and_stretches_without_a_numba_cache_location():
    pytest.importorskip("numba")
    proc = subprocess.run(
        [sys.executable, "-c", NUMBA_WITHOUT_CACHE_LOCATION],
        capture_output=True, text=True, timeout=300, env=_python_env(NUMBA_CACHE_DIR=None),
    )
    assert proc.returncode == 0, proc.stderr[-2000:]


# --------------------------------------------------------------------------- #
# B4/B8: CPU budget and GDAL settings
# --------------------------------------------------------------------------- #
def test_cpu_budget_follows_affinity_not_host_count(monkeypatch):
    from pysent import _runtime

    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0, 1}, raising=False)
    monkeypatch.setattr(os, "process_cpu_count", lambda: 2, raising=False)
    monkeypatch.setattr(_runtime, "_cgroup_cpu_limit", lambda: None)
    for module in (s1, s2):
        assert module._resolve_product_workers(None, 3) == 2
        assert module._resolve_gdal_num_threads(None, 1) == "2"


def test_cpu_budget_follows_cgroup_quota(monkeypatch):
    from pysent import _runtime

    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: set(range(16)), raising=False)
    monkeypatch.setattr(os, "process_cpu_count", lambda: 16, raising=False)
    monkeypatch.setattr(_runtime, "_cgroup_cpu_limit", lambda: 4)  # docker --cpus=4
    assert s2._resolve_product_workers(None, 8) == 4
    assert s2._resolve_gdal_num_threads(None, 2) == "4"


def test_cgroup_v2_quota_is_read_from_the_process_cgroup_and_its_ancestors(tmp_path):
    from pysent._runtime import _cgroup_cpu_limit

    proc = tmp_path / "cgroup"
    proc.write_text("0::/slurm/job_1/step_0\n")
    (tmp_path / "fs/slurm/job_1/step_0").mkdir(parents=True)
    (tmp_path / "fs/slurm/job_1/step_0/cpu.max").write_text("max 100000\n")
    assert _cgroup_cpu_limit(tmp_path / "fs", proc) is None
    (tmp_path / "fs/slurm/job_1/cpu.max").write_text("350000 100000\n")
    assert _cgroup_cpu_limit(tmp_path / "fs", proc) == 4


def test_cgroup_v1_quota(tmp_path):
    from pysent._runtime import _cgroup_cpu_limit

    (tmp_path / "cpu").mkdir()
    (tmp_path / "cpu/cpu.cfs_quota_us").write_text("-1\n")
    (tmp_path / "cpu/cpu.cfs_period_us").write_text("100000\n")
    assert _cgroup_cpu_limit(tmp_path, tmp_path / "missing") is None
    (tmp_path / "cpu/cpu.cfs_quota_us").write_text("200000\n")
    assert _cgroup_cpu_limit(tmp_path, tmp_path / "missing") == 2


def test_processes_mode_runs_serial_inside_a_worker_process():
    # A bulk runner's ProcessPoolExecutor workers are not daemonic; the old
    # daemon check let each of them start a second pool per scene.
    with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as executor:
        settings = executor.submit(
            s1._resolve_sentinel_s1_processing_settings, {"parallel_mode": "processes"}, output_count=2
        ).result()
    assert settings["parallel_mode"] == "serial"


def test_gdal_threads_and_cache_apply_while_products_run(tmp_path, fake_s2_warp, record_product_writes, monkeypatch):
    monkeypatch.delenv("GDAL_NUM_THREADS", raising=False)
    cache_before = gdal.GetCacheMax()
    fake = fake_s2_warp()
    run_s2(tmp_path / "out", S2_PRODUCTS, gdal_num_threads=3, gdal_cachemax_mb=37, parallel_mode="serial")
    # The scene warp and the products all run one at a time here.
    assert [call["GDAL_NUM_THREADS"] for call in fake.calls] == ["3"]
    assert [call["GDAL_NUM_THREADS"] for call in record_product_writes] == ["3", "3", "3"]
    assert {call["cachemax"] for call in fake.calls + record_product_writes} == {37 * 2**20}
    # Restored afterwards.
    assert gdal.GetConfigOption("GDAL_NUM_THREADS") is None
    assert gdal.GetCacheMax() == cache_before


def test_products_in_parallel_threads_leave_gdal_num_threads_unset(tmp_path, fake_s2_warp, record_product_writes, monkeypatch):
    # GDAL_NUM_THREADS turns on multi-threaded GeoTIFF compression; with several
    # products writing from threads of one process, GDAL 3.8 segfaults in
    # GDALRasterBlock::Internalize() within a few real scenes. The scene warp
    # before them is a single GDAL call, so it keeps the setting.
    monkeypatch.delenv("GDAL_NUM_THREADS", raising=False)
    fake = fake_s2_warp()
    run_s2(tmp_path / "out", S2_PRODUCTS, gdal_num_threads=3, gdal_cachemax_mb=37,
           parallel_mode="threads", parallel_workers=3)
    assert [call["GDAL_NUM_THREADS"] for call in fake.calls] == ["3"]
    assert [call["GDAL_NUM_THREADS"] for call in record_product_writes] == [None, None, None]
    assert {call["cachemax"] for call in record_product_writes} == {37 * 2**20}


def test_user_gdal_num_threads_with_parallel_products_warns(tmp_path, fake_s2_warp, monkeypatch):
    monkeypatch.setenv("GDAL_NUM_THREADS", "4")
    fake_s2_warp()
    with pytest.warns(RuntimeWarning, match="GDAL_NUM_THREADS"):
        run_s2(tmp_path / "out", S2_PRODUCTS, parallel_mode="threads", parallel_workers=3)


def test_gdal_cachemax_env_set_after_gdal_started_is_honoured(tmp_path, fake_s2_warp, monkeypatch):
    gdal.GetCacheMax()  # the block cache now exists, so GDAL itself ignores later env changes
    monkeypatch.setenv("GDAL_CACHEMAX", "41")
    fake = fake_s2_warp()
    run_s2(tmp_path / "out")
    assert fake.calls[0]["cachemax"] == 41 * 2**20


def test_user_gdal_num_threads_is_kept_unless_an_option_overrides_it(tmp_path, fake_s2_warp, monkeypatch):
    monkeypatch.setenv("GDAL_NUM_THREADS", "2")
    fake = fake_s2_warp()
    run_s2(tmp_path / "a")
    run_s2(tmp_path / "b", gdal_num_threads=5)
    assert [call["GDAL_NUM_THREADS"] for call in fake.calls] == ["2", "5"]
    # Restoring must not leave the environment's value behind as a config option.
    monkeypatch.delenv("GDAL_NUM_THREADS")
    assert gdal.GetConfigOption("GDAL_NUM_THREADS") is None


# --------------------------------------------------------------------------- #
# B7/B10/B11: errors
# --------------------------------------------------------------------------- #
def test_s2_empty_scene_raises_empty_scene_error(tmp_path, fake_s2_warp):
    from pysent.errors import EmptySceneError

    warped = tmp_path / "empty.tif"
    _write_raster(warped, _rgb(fill=0))
    with pytest.raises(EmptySceneError, match="no valid pixels"):
        s2._write_stretched_sentinel_s2_rgb(warped, tmp_path / "o.tif", percentiles=(2, 98), block_size=64, overview_factors=(2,))

    fake_s2_warp(fill=0)
    with pytest.raises(EmptySceneError):
        run_s2(tmp_path / "out")
    assert _files(tmp_path / "out") == []


@pytest.mark.parametrize("parallel_mode", ["threads", "serial"])
def test_one_failed_product_does_not_discard_the_others(tmp_path, fake_s2_warp, parallel_mode, monkeypatch):
    from pysent.errors import PartialFailure

    fake_s2_warp()
    real_writer = s2._write_stretched_sentinel_s2_rgb

    def writer(subset_path, output_path, **kwargs):
        if "false_color_glacier" in Path(output_path).name:
            raise RuntimeError("simulated warp failure")
        return real_writer(subset_path, output_path, **kwargs)

    monkeypatch.setattr(s2, "_write_stretched_sentinel_s2_rgb", writer)
    with pytest.raises(PartialFailure) as caught:
        run_s2(tmp_path / "out", S2_PRODUCTS, parallel_mode=parallel_mode)
    failure = caught.value
    assert [r["product_name"] for r in failure.results] == ["true_color_vegetation", "false_color_vegetation"]
    assert list(failure.errors) == ["false_color_glacier"]
    assert "simulated warp failure" in str(failure.errors["false_color_glacier"])
    assert isinstance(failure, RuntimeError)
    assert _files(tmp_path / "out") == ["scene_false_color_vegetation.tif", "scene_true_color_vegetation.tif"]


def test_partial_failure_survives_a_process_boundary():
    from pysent.errors import EmptySceneError, PartialFailure

    original = PartialFailure("1 of 2 products failed", [{"path": "a.tif"}], {"b": EmptySceneError("empty")})
    copy = pickle.loads(pickle.dumps(original))
    assert str(copy) == "1 of 2 products failed"
    assert copy.results == [{"path": "a.tif"}]
    assert isinstance(copy.errors["b"], EmptySceneError)


def test_gdal_failure_cause_is_in_the_exception(tmp_path):
    missing = tmp_path / "missing.nc"
    with pytest.raises(RuntimeError) as caught:
        s1._warp_sentinel_s1_amplitude(str(missing), "Amplitude_VV", tmp_path / "w.tif")
    # GDAL's own message, not just "warp failed".
    assert str(missing) in str(caught.value)


# --------------------------------------------------------------------------- #
# B12: process pool
# --------------------------------------------------------------------------- #
class RecordingExecutor:
    """A ProcessPoolExecutor stand-in that runs jobs in-process and records its context."""

    contexts: list = []

    def __init__(self, max_workers=None, mp_context=None, **kwargs):
        RecordingExecutor.contexts.append(mp_context)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def submit(self, fn, *args, **kwargs):
        future = Future()
        future.set_result(fn(*args, **kwargs))
        return future


def test_process_pool_does_not_fork(tmp_path, monkeypatch):
    from pysent import _runtime

    def fake_warp(input_dataset, variable, warped_path, **kwargs):
        _write_raster(Path(warped_path), np.random.default_rng(0).random((64, 64), dtype=np.float32) + 1)

    RecordingExecutor.contexts = []
    monkeypatch.setattr(_runtime, "ProcessPoolExecutor", RecordingExecutor)
    monkeypatch.setattr(s1, "_warp_sentinel_s1_amplitude", fake_warp)
    s1.process_sentinel_s1_netcdf(
        input_dataset="unused.nc", output_dir=tmp_path / "out",
        output_names={"Amplitude_VV": "vv.tif", "Amplitude_VH": "vh.tif"},
        processing_options={"parallel_mode": "processes", "parallel_workers": 2},
    )
    assert len(RecordingExecutor.contexts) == 1
    assert RecordingExecutor.contexts[0].get_start_method() in {"forkserver", "spawn"}


def _report_worker_state() -> dict:
    from multiprocessing import parent_process

    return {
        "pid": os.getpid(),
        "in_child": parent_process() is not None,
        "GDAL_NUM_THREADS": gdal.GetConfigOption("GDAL_NUM_THREADS"),
        "cachemax": gdal.GetCacheMax(),
    }


def test_process_workers_get_the_gdal_settings():
    from pysent._runtime import run_product_jobs

    results = run_product_jobs(
        _report_worker_state, [("a", {}), ("b", {})],
        parallel_mode="processes", workers=2, runtime={"num_threads": "3", "cachemax_mb": 29},
    )
    for state in results:
        assert state["in_child"] and state["pid"] != os.getpid()
        assert state["GDAL_NUM_THREADS"] == "3"
        assert state["cachemax"] == 29 * 2**20


# --------------------------------------------------------------------------- #
# P8: one write at a time per process, so the write can use every core
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("module", [s1, s2])
def test_products_run_serially_by_default(module, monkeypatch):
    monkeypatch.delenv("S1_PARALLEL_MODE", raising=False)
    monkeypatch.delenv("S2_PARALLEL_MODE", raising=False)
    resolve = (
        module._resolve_sentinel_s1_processing_settings
        if module is s1
        else module._resolve_sentinel_s2_processing_settings
    )
    assert resolve({}, output_count=3)["parallel_mode"] == "serial"
    assert resolve({"parallel_mode": "threads"}, output_count=3)["parallel_mode"] == "threads"


def test_serial_products_get_the_gdal_thread_budget(tmp_path, fake_s2_warp, record_product_writes, monkeypatch):
    # Threaded GeoTIFF compression is where serial mode wins: 15.6 s -> 5.4 s per
    # product on a real scene. It is safe only because nothing else writes at once.
    monkeypatch.delenv("GDAL_NUM_THREADS", raising=False)
    fake_s2_warp()
    run_s2(tmp_path / "out", S2_PRODUCTS, gdal_num_threads=4)
    assert [call["GDAL_NUM_THREADS"] for call in record_product_writes] == ["4", "4", "4"]
