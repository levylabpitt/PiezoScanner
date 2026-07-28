"""Background execution helpers so the GUI thread never blocks on DAQ I/O.

``ScanWorker`` runs a full scan on a dedicated QThread — either a single 2D
raster or a 3D stack (an outer loop over Z values, one full 2D raster per
Z) — and streams results back via signals. ``AxisSweepWorker`` runs a
single-axis sweep (used by Find Surface). ``QuickCommand`` runs a one-shot
DAQ call (center stage, go-to-position, ...) on Qt's global thread pool so
even a slow TCP round trip to the lock-in never freezes the window.
"""

from __future__ import annotations

import traceback
from typing import Callable, Sequence

from PyQt6.QtCore import QObject, QRunnable, QThread, pyqtSignal, pyqtSlot

from ..core.scanner import PiezoScanner

# How long to let the Z piezo settle after stepping to a new slice level.
Z_SETTLE_MS = 300


class ScanWorker(QThread):
    line_ready = pyqtSignal(int, object)     # slice_index, ScanLineResult
    slice_started = pyqtSignal(int, float)   # slice_index, z_value (3D only)
    slice_completed = pyqtSignal(int, float)  # slice_index, z_value (3D only)
    progress_changed = pyqtSignal(int)       # 0-100 across the whole scan
    status_changed = pyqtSignal(str)
    finished_ok = pyqtSignal(bool)           # True = ran to completion
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        scanner: PiezoScanner,
        *,
        x_points: int,
        y_points: int,
        line_time: float,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
        detector_channels: Sequence[int],
        delay_samples: int = 0,
        slow_axis_down: bool = False,
        z_values: Sequence[float] | None = None,
        z_channel: int = 0,
        parent=None,
    ):
        super().__init__(parent)
        self.scanner = scanner
        self.x_points = x_points
        self.y_points = y_points
        self.line_time = line_time
        self.x_min = x_min
        self.x_max = x_max
        self.y_min = y_min
        self.y_max = y_max
        self.detector_channels = list(detector_channels)
        self.delay_samples = delay_samples
        self.slow_axis_down = slow_axis_down
        self.z_values = list(z_values) if z_values is not None else None
        self.z_channel = z_channel

        self._abort = False
        self._paused = False

    def request_abort(self) -> None:
        self._abort = True

    def set_paused(self, paused: bool) -> None:
        self._paused = paused

    def _should_abort(self) -> bool:
        return self._abort

    def run(self) -> None:
        is_3d = self.z_values is not None
        slices = self.z_values if is_3d else [None]
        n_slices = len(slices)
        total_lines = n_slices * self.y_points
        lines_done = 0

        try:
            for slice_idx, z_value in enumerate(slices):
                if self._abort:
                    break

                if is_3d:
                    self.scanner.daq.setAO_DC(self.z_channel, z_value)
                    self.msleep(Z_SETTLE_MS)
                    self.slice_started.emit(slice_idx, z_value)

                slice_lines = 0
                lines = self.scanner.scan_lines(
                    x_points=self.x_points,
                    y_points=self.y_points,
                    line_time=self.line_time,
                    x_min=self.x_min,
                    x_max=self.x_max,
                    y_min=self.y_min,
                    y_max=self.y_max,
                    detector_channels=self.detector_channels,
                    delay_samples=self.delay_samples,
                    slow_axis_down=self.slow_axis_down,
                    should_abort=self._should_abort,
                )
                for result in lines:
                    while self._paused and not self._abort:
                        self.status_changed.emit("Paused")
                        self.msleep(150)

                    if self._abort:
                        break

                    slice_lines += 1
                    lines_done += 1
                    self.line_ready.emit(slice_idx, result)
                    self.progress_changed.emit(int(100 * lines_done / total_lines))
                    if is_3d:
                        self.status_changed.emit(
                            f"Slice {slice_idx + 1}/{n_slices} — line {slice_lines}/{self.y_points}"
                        )
                    else:
                        self.status_changed.emit(f"Scanning line {slice_lines}/{self.y_points}")

                if self._abort or slice_lines < self.y_points:
                    break

                if is_3d:
                    self.slice_completed.emit(slice_idx, z_value)

            self.finished_ok.emit(not self._abort and lines_done == total_lines)
        except Exception as exc:  # surfaced to the GUI rather than dying silently
            self.error_occurred.emit(f"{exc}\n{traceback.format_exc()}")
            self.finished_ok.emit(False)


class AxisSweepWorker(QThread):
    """Sweep one AO axis while recording one detector channel — the engine
    behind Find Surface."""

    result_ready = pyqtSignal(object, object)  # axis_values, signal (np arrays)
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        scanner: PiezoScanner,
        *,
        channel: int,
        v_min: float,
        v_max: float,
        points: int,
        sweep_time: float,
        detector_channel: int,
        parent=None,
    ):
        super().__init__(parent)
        self.scanner = scanner
        self.channel = channel
        self.v_min = v_min
        self.v_max = v_max
        self.points = points
        self.sweep_time = sweep_time
        self.detector_channel = detector_channel

    def run(self) -> None:
        try:
            axis, signals = self.scanner.sweep_axis(
                self.channel,
                self.v_min,
                self.v_max,
                self.points,
                self.sweep_time,
                [self.detector_channel],
            )
            self.result_ready.emit(axis, signals[self.detector_channel])
        except Exception as exc:
            self.error_occurred.emit(f"{exc}\n{traceback.format_exc()}")


class _QuickCommandSignals(QObject):
    result = pyqtSignal(object)
    error = pyqtSignal(str)
    finished = pyqtSignal()


class QuickCommand(QRunnable):
    """Fire-and-forget wrapper for a single quick DAQ call, run off the
    global thread pool so it never blocks the GUI event loop."""

    def __init__(self, fn: Callable, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = _QuickCommandSignals()

    @pyqtSlot()
    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as exc:
            self.signals.error.emit(str(exc))
        else:
            self.signals.result.emit(result)
        finally:
            self.signals.finished.emit()
