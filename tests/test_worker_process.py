from __future__ import annotations

import multiprocessing
import time

from wen.web.app import _terminate_process


def _sleeping_worker() -> None:
    time.sleep(30)


def test_query_worker_can_be_terminated_immediately() -> None:
    process = multiprocessing.get_context("spawn").Process(target=_sleeping_worker)
    process.start()
    callback_called: list[bool] = []
    started = time.monotonic()

    _terminate_process(
        process,
        grace_seconds=0.2,
        after_terminate=lambda: callback_called.append(True),
    )

    assert time.monotonic() - started < 1.5
    assert not process.is_alive()
    assert callback_called == [True]
