"""Background execution helpers so the GUI thread never blocks on DAQ I/O.

``ScanWorker`` runs a full line-by-line scan on a dedicated QThread and
streams results back via signals. ``QuickCommand`` runs a single one-shot
DAQ call (center stage, go-to-position, ...) on Qt's global thread pool so
even a slow TCP round trip to the lock-in never freezes the window.
"""

from __future__ import annotations

import traceback
from typing import Callable, Sequence

from PyQt6.QtCore import QObject, QRunnable, QThread, pyqtSignal, pyqtSlot

from ..core.scanner import PiezoScanner, ScanLineResult


class ScanWorker(QThread):
    line_ready = pyqtSignal(object)      # ScanLineResult
    progress_changed = pyqtSignal(int)   # 0-100
    status_changed = pyqtSignal(str)
    finished_ok = pyqtSignal(bool)        # True = ran to completion, False = aborted/errored
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

        self._abort = False
        self._paused = False

    def request_abort(self) -> None:
        self._abort = True

    def set_paused(self, paused: bool) -> None:
        self._paused = paused

    def _should_abort(self) -> bool:
        return self._abort

    def run(self) -> None:
        completed_lines = 0
        try:
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
                should_abort=self._should_abort,
            )
            for result in lines:  # type: ScanLineResult
                while self._paused and not self._abort:
                    self.status_changed.emit(f"Paused at line {completed_lines}/{self.y_points}")
                    self.msleep(150)

                if self._abort:
                    break

                completed_lines += 1
                self.line_ready.emit(result)
                self.progress_changed.emit(int(100 * completed_lines / self.y_points))
                self.status_changed.emit(f"Scanning line {completed_lines}/{self.y_points}")

            self.finished_ok.emit(not self._abort and completed_lines == self.y_points)
        except Exception as exc:  # surfaced to the GUI rather than killing the thread silently
            self.error_occurred.emit(f"{exc}\n{traceback.format_exc()}")
            self.finished_ok.emit(False)


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
