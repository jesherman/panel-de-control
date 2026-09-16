"""Intel (xe) GPU-utilisation source for Panel de Control.

WHY: the controller is GPU%-driven, but the only source the plugin knows is
`gpu_busy_percent`, an AMDGPU node. Intel's `xe` driver does not expose it, so on an
Intel handheld the auto loop gets `gpu_busy=None` -> `_qualify_gameplay` returns False
-> the loop holds at its seed forever. This module supplies the missing signal.

SOURCE: `xe` exposes per-client GPU engine cycles in procfs fdinfo:

    /proc/<pid>/fdinfo/<fd>
        drm-driver:             xe
        drm-cycles-rcs:         <busy cycles>
        drm-total-cycles-rcs:   <total cycles>

Utilisation for an engine over an interval = busy delta / total delta. Same accounting
`intel_gpu_top` uses, read from procfs rather than the PMU, so it needs no `perf` binary
and no relaxed perf_event_paranoid.

TWO SHAPES, both measured on MSI Claw 8 AI+ (MS-1T52, Lunar Lake / Arc 140V):

  * One process can hold SEVERAL fdinfo fds that all report the same GPU cycles for
    that pid (a process with 6 open render fds reports its cycles 6 times). Summing
    across fds therefore inflates the ratio — observed reading 108%. Deltas are
    aggregated PER PID, so each process contributes at most once.
  * A pid's own fds report that pid's cycles, so summing across DIFFERENT pids is the
    correct way to get total system GPU work.

Measured: idle ~3-5% raw, vkcube immediate-mode ~80%, Spyro (Proton game) ~90%.
Because the raw metric is a true 0-100 scale, NO idle-floor subtraction is applied by
default — subtracting a floor measured while the GPU was busy silently clamps real
usage to zero, which is exactly the failure this module's first version had.

Never raises; returns None when no xe client is readable, exactly like
PowerReader.read_gpu_busy.
"""

import glob
import os
import time

_ENGINES = ("rcs", "ccs", "vecs", "vcs", "bcs")
# The 3D/render engine is what gpu_busy_percent reflects on AMD; use it as primary.
_PRIMARY_ENGINE = "rcs"
# Ignore deltas whose cycle total is below this: a client that opened/closed its
# device node yields a tiny denominator and would poison the ratio. At ~2 GHz this is
# ~5 microseconds of elapsed GPU cycles, i.e. "nothing happened at all".
_MIN_TOTAL_DELTA = 10_000


class IntelGpuUtil:
    """GPU utilisation on Intel `xe` from procfs fdinfo, as 0-100 %."""

    def __init__(
        self,
        root="/",
        engine=_PRIMARY_ENGINE,
        min_total_delta=_MIN_TOTAL_DELTA,
    ):
        self._root = root
        self._engine = engine
        self._min_total_delta = max(0, int(min_total_delta))

    # ---- procfs parsing -------------------------------------------------

    def _proc_root(self):
        return os.path.join(self._root, "proc")

    def _counters(self):
        """Aggregate engine counters once per PID.

        A process can hold several fdinfo fds, but only the fd that actually
        submitted work reports cycles — the rest report zero busy against the same
        total (measured: a Proton game's 7 fds, exactly one at 89%, six at 0%).
        Selecting any other fd silently reports ~0% GPU, which is a failure this
        module shipped with once. So: per pid, keep the fd with the HIGHEST busy.
        Busy is monotonic for the working fd, so this also keeps the same fd across
        consecutive samples, which is required for a valid delta.
        """
        counters = {}
        pattern = os.path.join(self._proc_root(), "[0-9]*", "fdinfo", "*")
        busy_key = f"drm-cycles-{self._engine}:"
        total_key = f"drm-total-cycles-{self._engine}:"
        for path in glob.glob(pattern):
            try:
                with open(path) as handle:
                    text = handle.read()
            except (OSError, PermissionError):
                continue
            if "drm-driver:\txe" not in text and "drm-driver: xe" not in text:
                continue
            busy = total = None
            for line in text.splitlines():
                if line.startswith(busy_key):
                    try:
                        busy = int(line.split(":", 1)[1].strip())
                    except (ValueError, IndexError):
                        pass
                elif line.startswith(total_key):
                    try:
                        total = int(line.split(":", 1)[1].strip())
                    except (ValueError, IndexError):
                        pass
            if busy is None or total is None:
                continue
            try:
                pid = int(path.split("/")[-3])
            except (ValueError, IndexError):
                continue
            existing = counters.get(pid)
            if existing is None or busy > existing[0]:
                counters[pid] = (busy, total)
        return counters

    # ---- sampling -------------------------------------------------------

    def _ratio_over(self, seconds):
        first = self._counters()
        if not first:
            return None
        time.sleep(max(0.05, seconds))
        second = self._counters()
        if not second:
            return None
        busy_delta = 0
        total_delta = 0
        for pid, (busy, total) in second.items():
            earlier = first.get(pid)
            if earlier is None:
                continue  # client appeared mid-window: no valid baseline
            prev_busy, prev_total = earlier
            db = busy - prev_busy
            dt = total - prev_total
            if db < 0 or dt < self._min_total_delta:
                continue  # counter wrap/reset, or an idle-closed client
            busy_delta += db
            total_delta = max(total_delta, dt)
        if total_delta <= 0:
            return None
        # Clamp: busy can exceed one engine's total when several processes run
        # concurrently on the same engine, and gpu_busy_percent is 0-100 too.
        return max(0.0, min(100.0, 100.0 * busy_delta / total_delta))

    def read_gpu_busy(self, seconds=0.35):
        """GPU utilisation 0-100 (int), or None when no xe client is readable."""
        raw = self._ratio_over(seconds)
        if raw is None:
            return None
        return int(round(raw))

    def available(self):
        return bool(self._counters())
