# tests/test_executor_scheduling.py
import datetime as dt
from quant.execution.executor import _slice_windows, _next_slice_due


def test_2_slice_windows():
    wins = _slice_windows(slice_count=2)
    assert wins == [dt.time(10, 30), dt.time(14, 30)]


def test_4_slice_windows():
    wins = _slice_windows(slice_count=4)
    assert wins == [dt.time(10, 30), dt.time(11, 50), dt.time(13, 10), dt.time(14, 30)]


def test_1_slice_window():
    assert _slice_windows(slice_count=1) == [dt.time(10, 0)]


def test_next_slice_due_finds_oldest_unsubmitted():
    now = dt.datetime(2026, 4, 17, 12, 0)
    wins = [dt.time(10, 30), dt.time(14, 30)]
    idx = _next_slice_due(now=now, windows=wins, slices_submitted=0)
    assert idx == 0


def test_next_slice_due_returns_none_if_future():
    now = dt.datetime(2026, 4, 17, 10, 0)
    wins = [dt.time(10, 30), dt.time(14, 30)]
    assert _next_slice_due(now=now, windows=wins, slices_submitted=0) is None


def test_next_slice_due_advances_after_submission():
    now = dt.datetime(2026, 4, 17, 13, 0)
    wins = [dt.time(10, 30), dt.time(14, 30)]
    assert _next_slice_due(now=now, windows=wins, slices_submitted=1) is None
    now = dt.datetime(2026, 4, 17, 14, 30)
    assert _next_slice_due(now=now, windows=wins, slices_submitted=1) == 1


def test_next_slice_due_returns_none_when_all_submitted():
    now = dt.datetime(2026, 4, 17, 15, 0)
    wins = [dt.time(10, 30), dt.time(14, 30)]
    assert _next_slice_due(now=now, windows=wins, slices_submitted=2) is None
