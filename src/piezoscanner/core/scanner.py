"""Piezo raster-scan engine.

Drives a two-axis (plus optional Z) piezo stage through a swappable
:class:`~piezoscanner.core.backends.base.ScannerBackend` and reconstructs 2D
images from one or more detector (input) channels. This module owns the
*scan pattern* (raster generation, line-by-line looping, direction, lag
correction) and never talks to hardware directly — see
``core/backends/`` for the two backends (Multichannel Lockin, nidaqstudio)
and what each does with ``set_dc``/``run_sweep``.

This module has no GUI dependencies. It is safe to import and unit-test
without PyQt6, matplotlib, or real hardware present.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator, Sequence

import numpy as np

from .backends.base import ScannerBackend
from .profiles import DEFAULT_PROFILES, ScannerProfile


@dataclass
class ScanLineResult:
    """One completed raster line (always scanned in the forward direction),
    lag-corrected so consumers can drop it straight into a row of the
    output image.

    ``line_index`` counts acquisition order (0 = first line scanned);
    ``row_index`` is where the line belongs in the image (0 = y_min row),
    so images keep the same orientation whether the slow axis scanned
    up or down.
    """

    line_index: int
    row_index: int
    y_value: float
    pixels: dict[int, np.ndarray]  # channel number -> pixel values


class PiezoScanner:
    """Piezo raster scanner driving a swappable :class:`ScannerBackend`."""

    def __init__(
        self,
        backend: ScannerBackend,
        profile: ScannerProfile | str = "PSJ",
        fast_axis_channel: int = 11,
        slow_axis_channel: int = 12,
        initial_wait: float = 1,
        return_to_start: bool = False,
    ):
        if isinstance(profile, str):
            if profile not in DEFAULT_PROFILES:
                raise ValueError(f"Unknown profile '{profile}'. Known profiles: {list(DEFAULT_PROFILES)}")
            profile = DEFAULT_PROFILES[profile]

        self.backend = backend
        self.profile: ScannerProfile = profile

        self.fast_axis_channel = fast_axis_channel
        self.slow_axis_channel = slow_axis_channel

        self.initial_wait = initial_wait
        self.return_to_start = return_to_start

    # ============================================================
    # Calibration passthroughs
    # ============================================================

    @property
    def calibration_um_per_v(self) -> float:
        return self.profile.calibration_um_per_v

    def volts_to_um(self, volts: float) -> float:
        return self.profile.volts_to_um(volts)

    def um_to_volts(self, um: float) -> float:
        return self.profile.um_to_volts(um)

    def clip_voltage(self, value: float) -> float:
        return self.profile.clip_voltage(value)

    # ============================================================
    # Immediate positioning
    # ============================================================

    def set_dc(self, channel: int, value: float, settle: bool = True) -> None:
        """Drive one output channel to a profile-clipped voltage. See
        :meth:`ScannerBackend.set_dc` for what ``settle`` controls."""
        self.backend.set_dc(channel, self.clip_voltage(value), settle=settle)

    def move_to(self, x_v: float, y_v: float) -> tuple[float, float]:
        """Immediately drive both axes to a clipped, profile-safe voltage.
        Returns the (clipped) voltages actually commanded."""
        x_v = self.clip_voltage(x_v)
        y_v = self.clip_voltage(y_v)
        self.backend.set_dc(self.fast_axis_channel, x_v)
        self.backend.set_dc(self.slow_axis_channel, y_v)
        return x_v, y_v

    # ============================================================
    # Single-axis sweep (used by Find Surface)
    # ============================================================

    def sweep_axis(
        self,
        channel: int,
        v_min: float,
        v_max: float,
        points: int,
        sweep_time: float,
        detector_channels: Sequence[int],
    ) -> tuple[np.ndarray, dict[int, np.ndarray]]:
        """Sweep one output channel linearly from ``v_min`` to ``v_max``
        while recording the given detector channels.

        Returns ``(axis_values, {channel: signal})``, both resampled onto
        the ``points`` grid.
        """
        self._validate_voltage(v_min)
        self._validate_voltage(v_max)
        if not detector_channels:
            raise ValueError("sweep_axis requires at least one detector channel")

        wave = np.linspace(v_min, v_max, points)
        signals = self.backend.run_sweep(
            ao_tables={channel: wave},
            ai_channels=detector_channels,
            duration=sweep_time,
            initial_wait=self.initial_wait,
        )
        return wave, signals

    # ============================================================
    # Line-by-line scanning (used by the live GUI)
    # ============================================================

    def scan_lines(
        self,
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
        should_abort: Callable[[], bool] | None = None,
    ) -> Iterator[ScanLineResult]:
        """Scan one line at a time, yielding a :class:`ScanLineResult` as
        each line completes.

        Scanning is unidirectional: every line is acquired on the forward
        (x_min → x_max) pass only, then the fast axis flies back to x_min
        before the next line. The flyback doesn't wait to settle itself —
        the next line's own ``initial_wait`` lead-in (baked into its sweep)
        is what actually holds the stage at x_min before that line's data
        starts, so there is no double wait. This avoids the forward/backward
        misalignment ("zipper") artifacts of bidirectional collection, so no
        line flipping or per-direction lag correction is needed —
        ``delay_samples`` is applied as one identical shift to every line.

        ``slow_axis_down`` picks the slow-axis direction: False steps Y from
        y_min up to y_max ("scan up"), True steps from y_max down to y_min
        ("scan down"). Either way each result's ``row_index`` places the
        line at its true Y position in the image (row 0 = y_min).

        This is the single source of truth for line-by-line acquisition —
        both the GUI worker and any headless/scripted use should call this
        instead of re-implementing the raster/lag-correction logic.

        ``should_abort`` is polled between lines; if it returns True the
        generator stops (no partial line is yielded).
        """
        for v in (x_min, x_max, y_min, y_max):
            self._validate_voltage(v)
        if not detector_channels:
            raise ValueError("scan_lines requires at least one detector channel")

        y_levels = np.linspace(y_min, y_max, y_points)
        if slow_axis_down:
            y_levels = y_levels[::-1]

        lines = self.backend.run_scan_lines(
            x_min=x_min,
            x_max=x_max,
            x_points=x_points,
            y_values=y_levels,
            fast_axis_channel=self.fast_axis_channel,
            slow_axis_channel=self.slow_axis_channel,
            detector_channels=detector_channels,
            line_time=line_time,
            initial_wait=self.initial_wait,
            should_abort=should_abort,
        )

        for line_idx, raw in enumerate(lines):
            pixels: dict[int, np.ndarray] = {}
            for channel, line_pixels in raw.items():
                if delay_samples:
                    line_pixels = np.roll(line_pixels, -delay_samples)
                pixels[channel] = line_pixels

            y_val = y_levels[line_idx]
            row_index = (y_points - 1 - line_idx) if slow_axis_down else line_idx
            yield ScanLineResult(line_index=line_idx, row_index=row_index, y_value=y_val, pixels=pixels)

    # ============================================================
    # Utilities
    # ============================================================

    def _validate_voltage(self, value: float) -> None:
        if value < self.profile.vmin or value > self.profile.vmax:
            raise ValueError(
                f"{self.profile.name} safe range is {self.profile.vmin} to {self.profile.vmax} V "
                f"(got {value} V)"
            )
