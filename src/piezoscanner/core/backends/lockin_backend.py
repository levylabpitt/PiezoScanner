"""Backend wrapping a Levylab FLEX ``Lockin`` (or the drop-in
:class:`~piezoscanner.core.simulated_daq.SimulatedDaq`).

Channel numbers are whatever the Lockin instrument itself uses — the same
numbers already used throughout this app (AO 11/12/13, AI 8/9/10, ...).
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

from .base import ScannerBackend


class LockinBackend(ScannerBackend):
    """The DAQ object (``daq``) must provide:
        setAO_DC(channel, value)
        lockin_sweep(sweep_config, timeout)
        getSweepWaveforms() -> {"AI": [{"Y": [...]}, ...]}
    """

    def __init__(self, daq, daq_fs: float = 13000, daq_num_samples: int = 1000):
        self.daq = daq
        self.daq_fs = daq_fs
        self.daq_num_samples = daq_num_samples

    def set_dc(self, channel: int, value: float, settle: bool = True) -> None:
        # The Lockin holds the commanded DC value in hardware as soon as the
        # command is acknowledged; there is no separate "settle" step at
        # this layer, so `settle` has nothing to do here.
        self.daq.setAO_DC(channel, value)

    def run_sweep(
        self,
        ao_tables: dict[int, Sequence[float]],
        ai_channels: Sequence[int],
        duration: float,
        initial_wait: float = 0.0,
    ) -> dict[int, np.ndarray]:
        if not ao_tables:
            raise ValueError("run_sweep requires at least one AO channel")
        if not ai_channels:
            raise ValueError("run_sweep requires at least one AI channel")

        n = len(next(iter(ao_tables.values())))
        if any(len(table) != n for table in ao_tables.values()):
            raise ValueError("All ao_tables must be the same length")

        config = {
            "Sweep Time (s)": duration,
            "Initial Wait (s)": initial_wait,
            "Return to Start": False,
            "Channels": [
                {
                    "Enable?": True,
                    "Channel": channel,
                    "Start": 0,
                    "End": 0,
                    "Pattern": "Table",
                    "Table": np.asarray(table, dtype=float).tolist(),
                }
                for channel, table in ao_tables.items()
            ],
        }
        self.daq.lockin_sweep(config, timeout=duration + initial_wait + 30)

        data = self.daq.getSweepWaveforms()
        ai = data["AI"]
        save_rate = self.daq_fs / self.daq_num_samples
        t = np.linspace(0, duration, n)

        result: dict[int, np.ndarray] = {}
        for channel in ai_channels:
            raw = np.asarray(ai[channel - 1]["Y"])
            det_time = np.arange(len(raw)) / save_rate
            result[channel] = np.interp(t, det_time, raw)
        return result
