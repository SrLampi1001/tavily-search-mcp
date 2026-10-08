"""Tests for the in-memory call counter."""
from __future__ import annotations

import threading

from tavily_search_mcp.call_counter import CallCounter


def test_initial_counter_is_zero():
    c = CallCounter()
    assert c.snapshot() == {"calls_this_session": 0}


def test_charge_increments_counter():
    c = CallCounter()
    n = c.charge()
    assert n == 1
    assert c.snapshot() == {"calls_this_session": 1}


def test_charge_returns_post_increment_value():
    c = CallCounter()
    c.charge()
    c.charge()
    n = c.charge()
    assert n == 3


def test_snapshot_is_a_copy():
    c = CallCounter()
    c.charge()
    snap = c.snapshot()
    snap["calls_this_session"] = 999
    assert c.snapshot() == {"calls_this_session": 1}


def test_charge_is_thread_safe():
    c = CallCounter()
    iterations = 200

    def worker():
        for _ in range(iterations):
            c.charge()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert c.snapshot() == {"calls_this_session": iterations * 4}
