"""Drop-in stand-in for ``flex.inst.levylab.Lockin.Lockin`` used when no
real lock-in is reachable, so :class:`PiezoScanner` runs the exact same
code path in simulation as it does against real hardware — no
``if daq is not None`` branching scattered through the app."""

from __future__ import annotations

import time

import numpy as np


class _SyntheticWaveform:
    """Lazily synthesizes a plausible detector trace for one AI channel,
    shaped by the fast/slow axis tables from the most recent sweep and a
    ``bias_factor`` derived from the DC state of the *other* (non-swept)
    outputs — this is what makes 3D slices fade in/out as Z steps through
    a simulated "focus"."""

    def __init__(self, x_table: list[float], y_table: list[float], num_samples: int, bias_factor: float = 1.0):
        self._x_table = np.asarray(x_table, dtype=float) if x_table else np.zeros(1)
        self._y_table = np.asarray(y_table, dtype=float) if y_table else np.zeros(1)
        self._num_samples = max(int(num_samples), 4)
        self._bias_factor = bias_factor

    def __getitem__(self, channel_index: int) -> dict:
        n = self._num_samples
        src = np.arange(len(self._x_table))
        dst = np.linspace(0, len(self._x_table) - 1, n)
        x = np.interp(dst, src, self._x_table)
        y = np.interp(dst, np.arange(len(self._y_table)), self._y_table)

        x_c = x - np.mean(x)
        y_c = y - np.mean(y)
        r_sq = (x_c / 2.0) ** 2 + (y_c / 2.0) ** 2

        rng = np.random.default_rng(seed=(channel_index * 7919 + 17) % (2**31))
        phase = channel_index * 0.7
        signal = (
            0.01 * self._bias_factor * np.exp(-r_sq / 2.0)
            + 0.0015 * np.cos(3 * x_c + phase)
            + 0.002
            + rng.normal(0, 0.002, n)
        )
        return {"Y": signal.tolist()}


class SimulatedDaq:
    """Implements just enough of the Lockin surface for PiezoScanner:
    ``setAO_DC``, ``lockin_sweep``, ``getSweepWaveforms``.

    Returned waveform length follows the sweep duration at the effective
    saved-sample rate (``daq_fs / daq_num_samples``), matching how the real
    lock-in's saved data scales with sweep time.
    """

    def __init__(self, daq_fs: float = 13000, daq_num_samples: int = 1000, speed_factor: float = 12.0):
        self._ao_state: dict[int, float] = {}
        self._last_sweep_config: dict | None = None
        self._swept_channels: set[int] = set()
        self._save_rate = daq_fs / daq_num_samples
        self._speed_factor = speed_factor

    def setAO_DC(self, channel: int, value: float) -> None:
        self._ao_state[channel] = value

    def getAO(self, channel: int):
        return {channel: {"Y": [self._ao_state.get(channel, 0.0)]}}

    def lockin_sweep(self, sweep_config: dict, timeout: float = 10) -> None:
        self._last_sweep_config = sweep_config
        self._swept_channels = {
            c["Channel"] for c in sweep_config.get("Channels", []) if c.get("Enable?")
        }
        sweep_time = sweep_config.get("Sweep Time (s)", 0.1)
        time.sleep(min(sweep_time / self._speed_factor, 0.5))

    def getSweepWaveforms(self) -> dict:
        if self._last_sweep_config is None:
            return {"AI": []}

        channels = self._last_sweep_config.get("Channels", [])
        x_table = channels[0]["Table"] if len(channels) > 0 else []
        y_table = channels[1]["Table"] if len(channels) > 1 else []

        sweep_time = self._last_sweep_config.get("Sweep Time (s)", 0.1)
        num_samples = int(round(self._save_rate * sweep_time))

        # Non-swept outputs (e.g. a stepped Z during a 3D scan) modulate the
        # synthetic signal: peak response when they sit at 5 V, simulating a
        # sample surface / focal plane there.
        others = [v for k, v in self._ao_state.items() if k not in self._swept_channels]
        bias = float(np.mean(others)) if others else 5.0
        bias_factor = float(np.exp(-(((bias - 5.0) / 2.5) ** 2)))

        return {"AI": _SyntheticWaveform(x_table, y_table, num_samples, bias_factor)}
