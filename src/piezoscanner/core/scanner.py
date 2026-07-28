"""Piezo raster-scan engine.

Drives a two-axis piezo stage through a Levylab FLEX ``Lockin`` (or any
object exposing the same ``lockin_sweep`` / ``getSweepWaveforms`` /
``setAO_DC`` surface) and reconstructs 2D images from one or more detector
(input) channels acquired during the sweep.

This module has no GUI dependencies. It is safe to import and unit-test
without PyQt6, matplotlib, or real hardware present.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator, Sequence

import numpy as np

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
    """Piezo raster scanner using a DAQ sweep backend.

    The DAQ backend (``daq``) must provide:
        setAO_DC(channel, value)
        lockin_sweep(sweep_config, timeout)
        getSweepWaveforms() -> {"AI": [{"Y": [...]}, ...]}
    """

    def __init__(
        self,
        daq,
        profile: ScannerProfile | str = "PSJ",
        fast_axis_channel: int = 11,
        slow_axis_channel: int = 12,
        initial_wait: float = 1,
        return_to_start: bool = False,
        daq_fs: float = 13000,
        daq_num_samples: int = 1000,
    ):
        if isinstance(profile, str):
            if profile not in DEFAULT_PROFILES:
                raise ValueError(f"Unknown profile '{profile}'. Known profiles: {list(DEFAULT_PROFILES)}")
            profile = DEFAULT_PROFILES[profile]

        self.daq = daq
        self.profile: ScannerProfile = profile

        self.fast_axis_channel = fast_axis_channel
        self.slow_axis_channel = slow_axis_channel

        self.initial_wait = initial_wait
        self.return_to_start = return_to_start

        self.daq_fs = daq_fs
        self.daq_num_samples = daq_num_samples

        # Current line's scan waveforms (populated per-line during scan_lines)
        self.x_wave: np.ndarray | None = None
        self.y_wave: np.ndarray | None = None
        self.time: np.ndarray | None = None
        self.x_points: int | None = None
        self.y_points: int | None = None
        self.scan_time: float | None = None

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
    # Full-frame raster generation (single big sweep)
    # ============================================================

    def generate_raster(
        self, x_points, y_points, scan_time,
        x_min=0, x_max=1, y_min=0, y_max=1,
        flyback_fraction=0.05,
    ):
        """Populate ``x_wave``/``y_wave``/``time`` for one continuous
        unidirectional raster covering the whole frame in a single DAQ
        sweep: each line ramps forward (x_min → x_max) then flies back to
        x_min in a short segment taking ``flyback_fraction`` of the line's
        table points. Only the forward portion of each line carries data.

        Note the GUI does not use this — it scans line-by-line via
        :meth:`scan_lines`; this is for headless/scripted single-sweep use.
        """
        for v in (x_min, x_max, y_min, y_max):
            self._validate_voltage(v)

        n_flyback = max(1, int(round(x_points * flyback_fraction)))
        x_wave: list[float] = []
        y_wave: list[float] = []

        y_levels = np.linspace(y_min, y_max, y_points)
        for y in y_levels:
            x_wave.extend(np.linspace(x_min, x_max, x_points))
            x_wave.extend(np.linspace(x_max, x_min, n_flyback + 1)[1:])
            y_wave.extend(np.full(x_points + n_flyback, y))

        self.x_wave = np.asarray(x_wave)
        self.y_wave = np.asarray(y_wave)

        n = len(self.x_wave)
        self.time = np.linspace(0, scan_time, n)
        self.x_points = x_points
        self.y_points = y_points
        self.scan_time = scan_time

        return self.x_wave, self.y_wave

    # ============================================================
    # DAQ sweep configuration / execution
    # ============================================================

    def get_sweep_config(self) -> dict:
        if self.x_wave is None:
            raise RuntimeError("No scan waveform generated yet.")

        return {
            "Sweep Time (s)": self.scan_time,
            "Initial Wait (s)": self.initial_wait,
            "Return to Start": self.return_to_start,
            "Channels": [
                {
                    "Enable?": True,
                    "Channel": self.fast_axis_channel,
                    "Start": 0,
                    "End": 0,
                    "Pattern": "Table",
                    "Table": self.x_wave.tolist(),
                },
                {
                    "Enable?": True,
                    "Channel": self.slow_axis_channel,
                    "Start": 0,
                    "End": 0,
                    "Pattern": "Table",
                    "Table": self.y_wave.tolist(),
                },
            ],
        }

    def run(self, timeout: float = 60) -> None:
        """Execute the currently-configured sweep on the DAQ."""
        config = self.get_sweep_config()
        self.daq.lockin_sweep(config, timeout=timeout)

    # ============================================================
    # Detector readout (multi-channel)
    # ============================================================

    def read_detector(self, channel: int) -> np.ndarray:
        """Read a single detector waveform. ``channel`` is 1-indexed
        (AI channel 1 -> first entry in the ``AI`` list)."""
        return self.read_detectors([channel])[channel]

    def read_detectors(self, channels: Sequence[int]) -> dict[int, np.ndarray]:
        """Read several detector waveforms from the last completed sweep in
        one round trip to the DAQ."""
        data = self.daq.getSweepWaveforms()
        ai = data["AI"]
        result: dict[int, np.ndarray] = {}
        for channel in channels:
            result[channel] = np.asarray(ai[channel - 1]["Y"])
        return result

    # ============================================================
    # Immediate positioning
    # ============================================================

    def move_to(self, x_v: float, y_v: float) -> tuple[float, float]:
        """Immediately drive both axes to a clipped, profile-safe voltage.
        Returns the (clipped) voltages actually commanded."""
        x_v = self.clip_voltage(x_v)
        y_v = self.clip_voltage(y_v)
        self.daq.setAO_DC(self.fast_axis_channel, x_v)
        self.daq.setAO_DC(self.slow_axis_channel, y_v)
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
        """Sweep one AO channel linearly from ``v_min`` to ``v_max`` while
        recording the given detector channels, using the same lock-in sweep
        mechanism as a scan line.

        Returns ``(axis_values, {channel: signal})``, both resampled onto
        the ``points`` grid.
        """
        self._validate_voltage(v_min)
        self._validate_voltage(v_max)
        if not detector_channels:
            raise ValueError("sweep_axis requires at least one detector channel")

        wave = np.linspace(v_min, v_max, points)
        config = {
            "Sweep Time (s)": sweep_time,
            "Initial Wait (s)": self.initial_wait,
            "Return to Start": False,
            "Channels": [
                {
                    "Enable?": True,
                    "Channel": channel,
                    "Start": 0,
                    "End": 0,
                    "Pattern": "Table",
                    "Table": wave.tolist(),
                },
            ],
        }
        self.daq.lockin_sweep(config, timeout=sweep_time + self.initial_wait + 30)

        raw = self.read_detectors(detector_channels)
        save_rate = self.daq_fs / self.daq_num_samples
        t = np.linspace(0, sweep_time, points)

        signals: dict[int, np.ndarray] = {}
        for det_channel, trace in raw.items():
            det_time = np.arange(len(trace)) / save_rate
            signals[det_channel] = np.interp(t, det_time, trace)

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
        with a direct DC jump before the next line. The next line's sweep
        holds the stage at x_min for ``initial_wait`` before ramping, which
        doubles as post-flyback settle time. This avoids the forward/backward
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
        save_rate = self.daq_fs / self.daq_num_samples

        for line_idx, y_val in enumerate(y_levels):
            if should_abort is not None and should_abort():
                return

            self.x_wave = np.linspace(x_min, x_max, x_points)
            self.y_wave = np.full(x_points, y_val)
            self.time = np.linspace(0, line_time, x_points)
            self.x_points = x_points
            self.y_points = 1
            self.scan_time = line_time

            self.run()
            raw = self.read_detectors(detector_channels)

            # Fly back to the line start immediately so the piezo has the
            # whole readout/plotting gap plus the next sweep's initial wait
            # to settle at x_min.
            self.daq.setAO_DC(self.fast_axis_channel, x_min)

            pixels: dict[int, np.ndarray] = {}
            for channel, raw_line in raw.items():
                det_time = np.arange(len(raw_line)) / save_rate
                line_pixels = np.interp(self.time, det_time, raw_line)
                if delay_samples:
                    line_pixels = np.roll(line_pixels, -delay_samples)
                pixels[channel] = line_pixels

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
