"""Hardware abstraction that :class:`~piezoscanner.core.scanner.PiezoScanner`
drives, so the scan-pattern logic (raster generation, line-by-line looping,
lag correction) never has to know which instrument is actually attached.

A backend exposes exactly two operations:

- ``set_dc``: immediately (and persistently) drive one output channel to a
  fixed voltage — used for centering, jogging, click-to-move, line flyback,
  and stepping Z between 3D slices.
- ``run_sweep``: play a synchronized table on one or more output channels
  while recording one or more input channels, returning each input channel
  resampled onto the same grid as the given tables — used for every line
  scan and for the Find Surface axis sweep.

Channel numbers are whatever the concrete backend's instrument uses
natively (see each implementation's docstring).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

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

    def close(self) -> None:
        """Release any held connections. Default: nothing to release."""
