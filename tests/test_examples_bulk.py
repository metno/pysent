"""The bulk runner in ``examples/bulk_convert.py``.

These exercise the runner's own logic - discovery, resume, failure isolation,
pool rebuilding, sizing, exit codes - with fake processors, so they need no GDAL
and no SAFE product. The processors run in real worker processes, which is the
point: that is where a scene is lost when a worker dies.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
sys.path.insert(0, str(EXAMPLES))

import bulk_convert  # noqa: E402

S2_NAME = "S2C_MSIL2A_20260810T092031_N0512_R093_T35VPE_20260810T124910"
S1_NAME = "S1D_IW_GRDH_1SDV_20260810T052300_20260810T052325_004059_007650_5857"


# --------------------------------------------------------------------------- #
# Fake processors: module level, so worker processes can import them
# --------------------------------------------------------------------------- #
def _finish(job: dict, status: str = bulk_convert.OK, outputs: int = 2) -> dict:
    """Write a scene's outputs and its sidecar, the way process_scene does."""
    out_dir = Path(job["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    if status == bulk_convert.OK:
        for index in range(outputs):
            path = out_dir / f"{bulk_convert.scene_name(job['scene'])}_{index}.tif"
            path.write_text("raster")
            written.append(str(path))
    record = {
        "scene": job["scene"],
        "name": bulk_convert.scene_name(job["scene"]),
        "status": status,
        "outputs": written,
        "options": job["options"],
    }
    if status in (bulk_convert.OK, bulk_convert.EMPTY):
        (out_dir / f"{bulk_convert.scene_name(job['scene'])}.json").write_text(json.dumps(record))
    return record


def fake_ok(job: dict) -> dict:
    return _finish(job)


def fake_empty(job: dict) -> dict:
    return _finish(job, status=bulk_convert.EMPTY)


def fake_one_failure(job: dict) -> dict:
    """Everything succeeds except the scene whose name contains "BAD"."""
    if "BAD" in job["scene"]:
        return {
            "scene": job["scene"],
            "name": bulk_convert.scene_name(job["scene"]),
            "status": bulk_convert.FAILED,
            "error_type": "RuntimeError",
            "error": "simulated product failure",
        }
    return _finish(job)


def fake_crash(job: dict) -> dict:
    """The worker dies outright, as the OOM killer would kill it."""
    if "CRASH" in job["scene"]:
        os._exit(1)
    return _finish(job)


def fake_flaky(job: dict) -> dict:
    """Fails once with a network-like error, then succeeds."""
    marker = Path(job["output_dir"]).parent / f"{bulk_convert.scene_name(job['scene'])}.attempt"
    marker.parent.mkdir(parents=True, exist_ok=True)
    attempts = int(marker.read_text()) if marker.exists() else 0
    marker.write_text(str(attempts + 1))
    if attempts == 0:
        return {
            "scene": job["scene"],
            "name": bulk_convert.scene_name(job["scene"]),
            "status": bulk_convert.FAILED,
            "error_type": "RuntimeError",
            "error": "CPLE_OpenFailed: /vsicurl/ HTTP response 503 from the archive",
        }
    return _finish(job)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def make_args(output_dir: Path, **overrides) -> "object":
    argv = ["--output-dir", str(output_dir)]
    for key, value in overrides.items():
        flag = "--" + key.replace("_", "-")
        if value is True:
            argv.append(flag)
        elif value is not False:
            argv += [flag, str(value)]
    args = bulk_convert.build_parser().parse_args(argv)
    args.workers = bulk_convert.resolve_workers(args.workers, threads_per_worker=1, mem_per_worker_gb=1.0)
    return args


def scene_paths(tmp_path: Path, *names: str) -> list[str]:
    scenes = []
    for name in names:
        path = tmp_path / "archive" / f"{name}.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("safe")
        scenes.append(str(path))
    return scenes


def statuses(summary) -> list[str]:
    return [record["status"] for record in summary.records]


# --------------------------------------------------------------------------- #
# Discovery and naming
# --------------------------------------------------------------------------- #
def test_discovery_finds_zips_and_safe_directories(tmp_path):
    (tmp_path / "a/b").mkdir(parents=True)
    (tmp_path / f"a/{S2_NAME}.zip").write_text("x")
    (tmp_path / f"a/b/{S1_NAME}.SAFE").mkdir()
    (tmp_path / f"a/b/{S1_NAME}.SAFE/manifest.safe").write_text("x")
    (tmp_path / "a/notes.txt").write_text("x")
    (tmp_path / "a/.hidden.zip").write_text("x")

    found = bulk_convert.discover_scenes(input_dir=tmp_path)

    assert [Path(scene).name for scene in found] == [f"{S2_NAME}.zip", f"{S1_NAME}.SAFE"]


def test_discovery_reads_a_list_with_comments_and_urls(tmp_path):
    listing = tmp_path / "scenes.txt"
    listing.write_text(
        f"# a comment\n\n/archive/{S2_NAME}.zip\n"
        f"/vsizip//vsicurl/https://host/nbs/{S1_NAME}.zip\n"
        f"/archive/{S2_NAME}.zip\n"  # duplicate
    )

    found = bulk_convert.discover_scenes(input_list=listing)

    assert found == [f"/archive/{S2_NAME}.zip", f"/vsizip//vsicurl/https://host/nbs/{S1_NAME}.zip"]


@pytest.mark.parametrize(
    "scene, name, family",
    [
        (f"/archive/{S2_NAME}.zip", S2_NAME, "S2"),
        (f"/vsizip//vsicurl/https://host/{S1_NAME}.zip", S1_NAME, "S1"),
        (f"/archive/{S1_NAME}.SAFE/", S1_NAME, "S1"),
        ("/archive/something_else.zip", "something_else", "unknown"),
    ],
)
def test_scene_name_and_family(scene, name, family):
    assert bulk_convert.scene_name(scene) == name
    assert bulk_convert.scene_family(scene) == family


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #
def test_workers_are_sized_by_cpu_and_memory():
    # 16 CPUs / 2 threads = 8, and (24 - 2) / 1.2 = 18 -> the CPUs are the limit.
    assert bulk_convert.resolve_workers("auto", threads_per_worker=2, mem_per_worker_gb=1.2, cpus=16, memory_gb=24) == 8
    # A small-memory node is limited by memory instead.
    assert bulk_convert.resolve_workers("auto", threads_per_worker=2, mem_per_worker_gb=1.2, cpus=16, memory_gb=8) == 5
    # Never zero, whatever the budget.
    assert bulk_convert.resolve_workers("auto", threads_per_worker=8, mem_per_worker_gb=4, cpus=2, memory_gb=2) == 1
    # An explicit count wins.
    assert bulk_convert.resolve_workers("3", threads_per_worker=2, mem_per_worker_gb=1.2, cpus=64, memory_gb=64) == 3


def test_slurm_allocation_is_respected(monkeypatch):
    monkeypatch.setenv("SLURM_CPUS_PER_TASK", "4")
    monkeypatch.setenv("SLURM_MEM_PER_NODE", "8192")  # MB
    assert bulk_convert.usable_cpus() <= 4
    assert bulk_convert.usable_memory_gb() <= 8.0


# --------------------------------------------------------------------------- #
# Running scenes
# --------------------------------------------------------------------------- #
def test_every_scene_is_processed_and_logged(tmp_path):
    scenes = scene_paths(tmp_path, S2_NAME, S1_NAME)
    args = make_args(tmp_path / "out", workers=2)

    summary = bulk_convert.run_bulk(scenes, output_dir=tmp_path / "out", args=args, process=fake_ok, log=lambda m: None)

    assert summary.counts[bulk_convert.OK] == 2 and summary.failed == 0
    # Outputs are laid out per platform and scene.
    assert (tmp_path / "out" / "S2" / S2_NAME / f"{S2_NAME}.json").exists()
    assert (tmp_path / "out" / "S1" / S1_NAME / f"{S1_NAME}.json").exists()
    logged = [json.loads(line) for line in (tmp_path / "out" / "bulk_results.jsonl").read_text().splitlines()]
    assert sorted(record["name"] for record in logged) == sorted([S1_NAME, S2_NAME])


def test_workers_get_serial_products_and_the_thread_budget(tmp_path):
    scenes = scene_paths(tmp_path, S2_NAME)
    args = make_args(tmp_path / "out", workers=1, threads_per_worker=3, gdal_cachemax_mb=128, scratch=tmp_path / "s")

    summary = bulk_convert.run_bulk(scenes, output_dir=tmp_path / "out", args=args, process=fake_ok, log=lambda m: None)

    options = summary.records[0]["options"]
    assert options["parallel_mode"] == "serial"  # never concurrent GDAL writes in one process
    assert options["gdal_num_threads"] == 3
    assert options["gdal_cachemax_mb"] == 128
    assert options["work_dir"] == str(tmp_path / "s")
    assert options["histogram_stretch"] is True


def test_presets_and_extra_options(tmp_path):
    scenes = scene_paths(tmp_path, S2_NAME, S1_NAME)
    args = make_args(tmp_path / "out", workers=1, preset="quicklook")
    args.option = ["compression=JPEG", "block_size=512"]

    summary = bulk_convert.run_bulk(scenes, output_dir=tmp_path / "out", args=args, process=fake_ok, log=lambda m: None)

    options = {record["name"]: record["options"] for record in summary.records}
    assert options[S2_NAME]["target_resolution"] == 60.0
    assert options[S1_NAME]["target_resolution"] == 160.0
    assert options[S2_NAME]["compression"] == "JPEG" and options[S2_NAME]["block_size"] == 512


# --------------------------------------------------------------------------- #
# Resume
# --------------------------------------------------------------------------- #
def test_a_finished_scene_is_skipped_and_force_redoes_it(tmp_path):
    scenes = scene_paths(tmp_path, S2_NAME)
    args = make_args(tmp_path / "out", workers=1)
    bulk_convert.run_bulk(scenes, output_dir=tmp_path / "out", args=args, process=fake_ok, log=lambda m: None)

    again = bulk_convert.run_bulk(scenes, output_dir=tmp_path / "out", args=args, process=fake_ok, log=lambda m: None)
    assert statuses(again) == [bulk_convert.SKIPPED]

    args.force = True
    forced = bulk_convert.run_bulk(scenes, output_dir=tmp_path / "out", args=args, process=fake_ok, log=lambda m: None)
    assert statuses(forced) == [bulk_convert.OK]


def test_an_interrupted_write_leaves_no_sidecar_so_the_scene_runs_again(tmp_path):
    scenes = scene_paths(tmp_path, S2_NAME)
    out = tmp_path / "out"
    # A worker killed mid-scene: outputs present, sidecar never written.
    scene_dir = bulk_convert.scene_output_dir(out, scenes[0])
    scene_dir.mkdir(parents=True)
    (scene_dir / f"{S2_NAME}_0.tif").write_text("half a raster")

    assert not bulk_convert.scene_is_done(out, scenes[0])
    summary = bulk_convert.run_bulk(scenes, output_dir=out, args=make_args(out, workers=1),
                                    process=fake_ok, log=lambda m: None)
    assert statuses(summary) == [bulk_convert.OK]


def test_a_missing_output_makes_the_scene_run_again(tmp_path):
    scenes = scene_paths(tmp_path, S2_NAME)
    out = tmp_path / "out"
    args = make_args(out, workers=1)
    bulk_convert.run_bulk(scenes, output_dir=out, args=args, process=fake_ok, log=lambda m: None)

    # Someone deleted one of the GeoTIFFs; the sidecar alone must not count.
    (bulk_convert.scene_output_dir(out, scenes[0]) / f"{S2_NAME}_1.tif").unlink()

    assert not bulk_convert.scene_is_done(out, scenes[0])


def test_an_empty_scene_is_recorded_and_not_retried(tmp_path):
    scenes = scene_paths(tmp_path, S2_NAME)
    out = tmp_path / "out"
    args = make_args(out, workers=1)

    first = bulk_convert.run_bulk(scenes, output_dir=out, args=args, process=fake_empty, log=lambda m: None)
    assert statuses(first) == [bulk_convert.EMPTY]

    again = bulk_convert.run_bulk(scenes, output_dir=out, args=args, process=fake_empty, log=lambda m: None)
    assert statuses(again) == [bulk_convert.SKIPPED]


# --------------------------------------------------------------------------- #
# Failure isolation
# --------------------------------------------------------------------------- #
def test_one_failing_scene_does_not_stop_the_others(tmp_path):
    scenes = scene_paths(tmp_path, S2_NAME, f"{S2_NAME}_BAD", S1_NAME)
    out = tmp_path / "out"

    summary = bulk_convert.run_bulk(scenes, output_dir=out, args=make_args(out, workers=2),
                                    process=fake_one_failure, log=lambda m: None)

    assert summary.counts[bulk_convert.OK] == 2 and summary.failed == 1
    failure = next(record for record in summary.records if record["status"] == bulk_convert.FAILED)
    assert "simulated product failure" in failure["error"]


def test_a_dead_worker_loses_only_the_scene_it_held(tmp_path):
    # Sequential workers, so exactly one scene is in flight when the worker dies.
    scenes = scene_paths(tmp_path, S2_NAME, f"{S2_NAME}_CRASH", S1_NAME)
    out = tmp_path / "out"

    summary = bulk_convert.run_bulk(scenes, output_dir=out, args=make_args(out, workers=1),
                                    process=fake_crash, log=lambda m: None)

    assert summary.counts[bulk_convert.OK] == 2, statuses(summary)
    assert summary.failed == 1
    failure = next(record for record in summary.records if record["status"] == bulk_convert.FAILED)
    assert failure["error_type"] == "BrokenProcessPool"
    assert "CRASH" in failure["scene"]


def test_a_network_failure_is_retried(tmp_path):
    scenes = scene_paths(tmp_path, S2_NAME)
    out = tmp_path / "out"
    args = make_args(out, workers=1, retries=1)

    summary = bulk_convert.run_bulk(scenes, output_dir=out, args=args, process=fake_flaky, log=lambda m: None)

    assert statuses(summary) == [bulk_convert.OK]
    assert int((out / "S2" / f"{S2_NAME}.attempt").read_text()) == 2


def test_a_permanent_failure_is_not_retried(tmp_path):
    scenes = scene_paths(tmp_path, f"{S2_NAME}_BAD")
    out = tmp_path / "out"
    args = make_args(out, workers=1, retries=3)

    summary = bulk_convert.run_bulk(scenes, output_dir=out, args=args, process=fake_one_failure, log=lambda m: None)

    assert statuses(summary) == [bulk_convert.FAILED]
    assert len(summary.records) == 1  # one attempt only


# --------------------------------------------------------------------------- #
# The command line
# --------------------------------------------------------------------------- #
def test_dry_run_reports_and_changes_nothing(tmp_path, capsys):
    scenes = scene_paths(tmp_path, S2_NAME, S1_NAME)
    listing = tmp_path / "scenes.txt"
    listing.write_text("\n".join(scenes))

    code = bulk_convert.main(["--input-list", str(listing), "--output-dir", str(tmp_path / "out"), "--dry-run"])

    assert code == 0
    printed = capsys.readouterr().out
    assert "2 scene(s): 2 to process" in printed
    assert not (tmp_path / "out").exists()


def test_exit_code_and_log_report_failures(tmp_path):
    # An input that is neither S1 nor S2 fails in the worker, without GDAL.
    listing = tmp_path / "scenes.txt"
    listing.write_text(str(tmp_path / "mystery_product.zip"))
    out = tmp_path / "out"

    code = bulk_convert.main(["--input-list", str(listing), "--output-dir", str(out), "--workers", "1", "--retries", "0"])

    assert code == 1
    logged = [json.loads(line) for line in (out / "bulk_results.jsonl").read_text().splitlines()]
    assert [record["status"] for record in logged] == [bulk_convert.FAILED]
    assert "Sentinel-1 or Sentinel-2" in logged[0]["error"]


def test_no_scenes_is_an_error(tmp_path, capsys):
    listing = tmp_path / "empty.txt"
    listing.write_text("# nothing here\n")

    assert bulk_convert.main(["--input-list", str(listing), "--output-dir", str(tmp_path / "out")]) == 1
    assert "no scenes found" in capsys.readouterr().err
