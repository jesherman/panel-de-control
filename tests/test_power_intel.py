"""Tests for the Intel (xe) GPU-utilisation source.

Synthetic procfs trees, no hardware needed, CI-safe. The hardware measurements are in
the module docstring and the PR description.
"""
import os

import pytest

from power.intel import IntelGpuUtil
from power.reader import PowerReader


# ---------------------------------------------------------------- helpers


def _mk_fdinfo(root, pid, fd, busy, total, driver="xe", engine="rcs"):
    d = os.path.join(root, "proc", str(pid), "fdinfo")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, str(fd)), "w") as f:
        f.write("pos:\t0\nflags:\t02\nmnt_id:\t1\n")
        f.write(f"drm-driver:\t{driver}\n")
        f.write("drm-pdev:\t0000:00:02.0\n")
        f.write(f"drm-cycles-{engine}:\t{busy}\n")
        f.write(f"drm-total-cycles-{engine}:\t{total}\n")


class SequencedUtil(IntelGpuUtil):
    """IntelGpuUtil whose counter read returns prepared snapshots in order.

    min_total_delta=0: these tests use small synthetic cycle counts, so the
    real-hardware noise floor (which rejects sub-10k-cycle deltas) is disabled.
    """

    def __init__(self, snapshots, **kwargs):
        kwargs.setdefault("min_total_delta", 0)
        super().__init__(**kwargs)
        self._snapshots = list(snapshots)
        self._calls = 0

    def _counters(self):
        if self._calls < len(self._snapshots):
            snap = self._snapshots[self._calls]
        else:
            snap = self._snapshots[-1]
        self._calls += 1
        return snap


# ---------------------------------------------------------------- parsing


def test_parses_engine_cycles_from_fdinfo(tmp_path):
    root = str(tmp_path)
    _mk_fdinfo(root, 100, 3, busy=100, total=1000)
    util = IntelGpuUtil(root=root)
    assert util.available() is True
    assert util._counters() == {100: (100, 1000)}


def test_unavailable_without_xe_clients(tmp_path):
    """No xe clients -> available() False and None (never a fabricated 0)."""
    util = IntelGpuUtil(root=str(tmp_path))
    assert util.available() is False
    assert util.read_gpu_busy(seconds=0.01) is None


def test_non_xe_driver_is_ignored(tmp_path):
    _mk_fdinfo(str(tmp_path), 100, 3, busy=100, total=1000, driver="amdgpu")
    util = IntelGpuUtil(root=str(tmp_path))
    assert util.available() is False


# ---------------------------------------------------------------- ratio


def test_utilisation_from_busy_and_total_delta(tmp_path):
    """busy 100->300 over total 1000->3000 == 10%."""
    util = SequencedUtil(
        [{100: (100, 1000)}, {100: (300, 3000)}],
        root=str(tmp_path),
    )
    assert util.read_gpu_busy(seconds=0.01) == 10


def test_multiple_fds_of_one_pid_are_not_double_counted(tmp_path):
    """REGRESSION: a process's fds all report that pid's cycles.

    Counting each fd separately inflated the ratio (observed 108% on hardware).
    Each pid must contribute once.
    """
    root = str(tmp_path)
    # same pid, three fds, identical counters
    for fd in (3, 4, 5):
        _mk_fdinfo(root, 100, fd, busy=500, total=5000)
    util = IntelGpuUtil(root=root)
    assert util._counters() == {100: (500, 5000)}

    # and the ratio uses the pid once: 500/5000 = 10%, not 30%
    util = SequencedUtil(
        [{100: (0, 0)}, {100: (500, 5000)}],
        root=root,
    )
    assert util.read_gpu_busy(seconds=0.01) == 10


def test_separate_pids_are_summed(tmp_path):
    """Different processes each contribute their own work."""
    util = SequencedUtil(
        [
            {100: (0, 0), 200: (0, 0)},
            {100: (300, 1000), 200: (700, 1000)},
        ],
        root=str(tmp_path),
    )
    # (300 + 700) / 1000 = 100%
    assert util.read_gpu_busy(seconds=0.01) == 100


def test_ratio_is_clamped_to_100(tmp_path):
    """Concurrent work on one engine can exceed its total delta; clamp like AMD does."""
    util = SequencedUtil(
        [
            {100: (0, 0), 200: (0, 0)},
            {100: (900, 1000), 200: (900, 1000)},
        ],
        root=str(tmp_path),
    )
    assert util.read_gpu_busy(seconds=0.01) == 100


def test_raw_scale_is_not_recalibrated():
    """REGRESSION: the first version subtracted an idle floor measured while the GPU
    was busy, which clamped real gameplay to ~0. The metric is already 0-100."""
    util = IntelGpuUtil()
    assert not hasattr(util, "calibrate")
    assert not hasattr(util, "idle_floor")


def test_negative_delta_is_discarded(tmp_path):
    """A counter that went backwards (client restarted) yields None, not garbage."""
    util = SequencedUtil(
        [{100: (5000, 9000)}, {100: (10, 20)}],
        root=str(tmp_path),
    )
    assert util.read_gpu_busy(seconds=0.01) is None


def test_new_client_mid_window_is_skipped(tmp_path):
    """A client with no baseline in the first read must not contribute."""
    util = SequencedUtil(
        [{100: (0, 0)}, {100: (500, 1000), 999: (900, 1000)}],
        root=str(tmp_path),
    )
    # only pid 100 counts: 500/1000 = 50%
    assert util.read_gpu_busy(seconds=0.01) == 50


def test_keeps_the_fd_that_reports_cycles(tmp_path):
    """REGRESSION: only one fd per pid carries cycles; the others read 0.

    Measured on a Proton game: 7 fds, one at 89% and six at 0%. Picking by total
    (or keeping the first) silently reported ~0% GPU.
    """
    root = str(tmp_path)
    _mk_fdinfo(root, 100, 3, busy=0, total=40940150)       # zero-busy fd
    _mk_fdinfo(root, 100, 4, busy=36594011, total=40940580)  # the working fd
    _mk_fdinfo(root, 100, 5, busy=0, total=40932294)       # another zero-busy fd
    util = IntelGpuUtil(root=root)
    assert util._counters() == {100: (36594011, 40940580)}


# ------------------------------------------------- PowerReader integration


def test_power_reader_falls_back_to_intel(tmp_path):
    """With no gpu_busy_percent (Intel), PowerReader uses the xe source."""
    root = str(tmp_path)
    _mk_fdinfo(root, 100, 3, busy=0, total=0)
    reader = PowerReader(root=root)
    assert reader._find_gpu_busy_path() is None
    assert reader._intel_util() is not None


def test_power_reader_returns_none_without_any_source(tmp_path):
    """No AMD node and no xe clients -> honest None, never a fake zero."""
    reader = PowerReader(root=str(tmp_path))
    assert reader.read_gpu_busy() is None
    assert reader._intel_util() is None
