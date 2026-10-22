"""``in_child_process`` sees every kind of worker process, Celery's included.

Celery's prefork pool is ``billiard``. Inside one of its children
``multiprocessing.current_process()`` is a ``ForkPoolWorker`` but
``multiprocessing.parent_process()`` answers ``None`` (measured in a real
Celery 5 worker, billiard 4.3.1), so a guard that only asked for the parent
let each Celery child open a process pool of its own per scene.
"""
from __future__ import annotations

import multiprocessing
import multiprocessing.process
from concurrent.futures import ProcessPoolExecutor

import pytest

from pysent._runtime import in_child_process


def _answer(connection) -> None:
    connection.send(in_child_process())


def _answer_like_billiard(connection) -> None:
    # What a billiard child looks like to this module: a registered worker
    # process (not "MainProcess") whose multiprocessing parent is unset.
    multiprocessing.process._parent_process = None
    assert multiprocessing.parent_process() is None
    connection.send(in_child_process())


def _ask_in_child(target) -> bool:
    context = multiprocessing.get_context("fork")
    parent_end, child_end = context.Pipe(duplex=False)
    process = context.Process(target=target, args=(child_end,))
    process.start()
    answer = parent_end.recv()
    process.join()
    return answer


def test_the_main_process_is_not_a_child():
    assert in_child_process() is False


def test_a_multiprocessing_child_is_a_child():
    assert _ask_in_child(_answer) is True


def test_a_process_pool_worker_is_a_child():
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        assert pool.submit(in_child_process).result() is True


def test_a_worker_whose_parent_process_is_unset_is_a_child():
    """Fails on main: ``parent_process()`` is ``None`` in a billiard child."""
    assert _ask_in_child(_answer_like_billiard) is True


def test_a_real_billiard_pool_worker_is_a_child():
    """Fails on main when billiard is installed (Celery's own pool); skipped otherwise."""
    billiard = pytest.importorskip("billiard")
    pool = billiard.Pool(1)
    try:
        assert pool.apply(in_child_process) is True
    finally:
        pool.terminate()
        pool.join()
