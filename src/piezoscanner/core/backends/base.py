"""Hardware abstraction that :class:`~piezoscanner.core.scanner.PiezoScanner`
drives, so the scan-pattern logic (raster generation, direction, lag
correction) never has to know which instrument is actually attached.

A backend exposes:

- ``set_dc``: immediately (and persistently) drive one output channel to a
  fixed voltage — used for centering, jogging, click-to-move, and stepping
  Z between 3D slices.
- ``run_sweep``: play a synchronized table on one or more output channels
  while recording one or more input channels, returning each input channel
  resampled onto the same grid as the given tables — used for the Find
  Surface axis sweep, and by the default ``run_scan_lines`` below.
- ``run_scan_lines``: acquire a whole raster's worth of lines. The default
  implementation just calls ``run_sweep`` once per line (correct for any
  backend), but a backend whose hardware can do better — e.g. nidaqstudio
  running one continuous acquisition for the entire scan instead of
  restarting per line — should override it. This is where the real speed
  difference between backends lives; everything above it stays identical.

Channel numbers are whatever the concrete backend's instrument uses
natively (see each implementation's docstring).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Iterator, Sequence

import numpy as np


class ScannerBackend(ABC):
    @abstractmethod
    def set_dc(self, channel: int, value: float, settle: bool = True) -> None:
        """Drive ``channel`` to ``value`` volts, holding it there.

        ``settle`` controls whether this call waits for the value to
        physically land before returning. Callers on a hot path that is
        about to wait anyway (e.g. a scan line's own lead-in, or a fixed
        Z-settle sleep) should pass ``settle=False`` to avoid waiting twice;
        interactive one-off moves (jog, center, click-to-move) should leave
        it ``True`` so the caller can trust the move has completed.
        """

    @abstractmethod
    def run_sweep(
        self,
        ao_tables: dict[int, Sequence[float]],
        ai_channels: Sequence[int],
        duration: float,
        initial_wait: float = 0.0,
    ) -> dict[int, np.ndarray]:
        """Play every table in ``ao_tables`` synchronously over ``duration``
        seconds while recording every channel in ``ai_channels``, holding at
        each table's first value for ``initial_wait`` seconds first.

        Every table in ``ao_tables`` must be the same length; each returned
        array is resampled onto that same length, so callers can treat the
        result as "the table's grid, but measured" regardless of how the
        backend actually samples the hardware.
        """

    def run_scan_lines(
        self,
        *,
        x_min: float,
        x_max: float,
        x_points: int,
        y_values: Sequence[float],
        fast_axis_channel: int,
        slow_axis_channel: int,
        detector_channels: Sequence[int],
        line_time: float,
        initial_wait: float,
        should_abort: Callable[[], bool] | None = None,
    ) -> Iterator[dict[int, np.ndarray]]:
        """Acquire one line per entry in ``y_values``, in order, yielding
        each line's ``{channel: pixels}`` (length ``x_points``, not yet
        lag-shifted) as it completes.

        Default: one isolated ``run_sweep`` per line, with a flyback to
        ``x_min`` between them — correct for every backend, since it's built
        only from the two operations above. Override this to do better.
        """
        for y_val in y_values:
            if should_abort is not None and should_abort():
                return
            raw = self.run_sweep(
                ao_tables={
                    fast_axis_channel: np.linspace(x_min, x_max, x_points),
                    slow_axis_channel: np.full(x_points, y_val),
                },
                ai_channels=detector_channels,
                duration=line_time,
                initial_wait=initial_wait,
            )
            # Fly back to the line start immediately; the *next* line's own
            # initial_wait lead-in is what actually provides settle time, so
            # this doesn't wait on its own (see PiezoScanner.scan_lines).
            self.set_dc(fast_axis_channel, x_min, settle=False)
            yield raw

    def close(self) -> None:
        """Release any held connections. Default: nothing to release."""
