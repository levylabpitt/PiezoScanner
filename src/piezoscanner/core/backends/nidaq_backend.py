"""Backend driving NI PXIe cards through a running ``nidaqstudio`` process
(GUI or ``--api-only``) over its ZMQ API.

Two distinct modes share one connection:

- :meth:`set_dc` holds an AO channel at a fixed DC voltage *continuously* —
  the way this app expects an output to behave between sweeps (center
  stage, jog, line flyback, Z step). This is done with the raw
  ``nidaqstudio.client`` API: patch the channel to ``shape="dc"`` at the
  target offset, make sure the engine is running continuously.
- :meth:`run_sweep` plays an exact per-sample table on one or more AO
  channels while recording AI channels, as one isolated finite run
  (``nidaqstudio.scanner.Scanner.table``). A scan like this explicitly
  disables every channel not part of it and restores the engine's prior
  configuration (unstarted) when it finishes — so every channel currently
  held by :meth:`set_dc` is folded into the sweep as a flat (constant)
  table alongside whichever channels are actually being swept, so nothing
  glitches or drops during the run. The next :meth:`set_dc` call (this
  app's scan loop always flies the fast axis back right after a sweep)
  naturally resumes the continuous hold afterward.

Channel numbers here are **1-indexed**, matching the "0 = disabled"
convention used everywhere else in this app's configuration: app channel 1
is nidaqstudio's ``AO0``/``AI0``, channel 2 is ``AO1``/``AI1``, and so on —
the same sequential numbering nidaqstudio's own GUI shows.
"""

from __future__ import annotations

from typing import Sequence

import numpy as np

from .base import ScannerBackend

DEFAULT_SAMPLE_RATE = 13_000.0


class NidaqBackend(ScannerBackend):
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        *,
        connect_timeout: float = 2.0,
        call_timeout: float = 30.0,
        sample_rate: float = DEFAULT_SAMPLE_RATE,
    ):
        try:
            from nidaqstudio.client import NidaqStudio
            from nidaqstudio.scanner import Scanner
        except ImportError as exc:
            raise ImportError(
                "The 'nidaqstudio' package is not installed in this environment. "
                "Install it (pip install -e <path to nidaqstudio>) or switch back "
                "to the Multichannel Lockin backend."
            ) from exc

        endpoint = f"tcp://{host}:{port}"
        self.rig = NidaqStudio(endpoint, timeout=connect_timeout)
        # NidaqStudio's ZMQ REQ socket connects lazily and doesn't fail just
        # because nothing is listening -- probe with a short timeout here so
        # an unreachable server fails fast, right where the connection was
        # requested, instead of on the first real call made much later.
        try:
            self.rig.status()
        except Exception as exc:
            self.rig.close()
            raise ConnectionError(f"Could not reach nidaqstudio at {endpoint}: {exc}") from exc
        self.rig.timeout = call_timeout

        self.scanner = Scanner(self.rig)
        self.sample_rate = sample_rate
        self._held: dict[int, float] = {}  # app channel -> last commanded DC value
        self._ao_names: list[str] | None = None
        self._ai_names: list[str] | None = None

    # ------------------------------------------------------------------
    # Channel addressing
    # ------------------------------------------------------------------
    def _channel_names(self) -> dict[str, list[str]]:
        if self._ao_names is None or self._ai_names is None:
            names = self.rig.channels()
            self._ao_names = names["ao"]
            self._ai_names = names["ai"]
        return {"ao": self._ao_names, "ai": self._ai_names}

    def _ao_name(self, channel: int) -> str:
        names = self._channel_names()["ao"]
        idx = channel - 1
        if not (0 <= idx < len(names)):
            raise ValueError(
                f"No AO channel {channel} -- nidaqstudio reports {len(names)} "
                f"AO channel(s) (1..{len(names)})."
            )
        return names[idx]

    # ------------------------------------------------------------------
    # Continuous DC hold
    # ------------------------------------------------------------------
    def set_dc(self, channel: int, value: float, settle: bool = True) -> None:
        self._held[channel] = value
        self._push_hold_config()
        self.rig.start()
        if settle:
            self.rig.wait_until_running(timeout=5.0)
            self.rig.settle()

    def _push_hold_config(self) -> None:
        config = self.rig.config()
        held_names = {self._ao_name(channel): value for channel, value in self._held.items()}
        for ch in config["ao_channels"]:
            value = held_names.get(ch["physical_channel"])
            if value is not None:
                ch["enabled"] = True
                ch["shape"] = "dc"
                ch["offset"] = value
                ch["amplitude"] = 0.0
        config["timing"]["mode"] = "continuous"
        self.rig.set_config(config)

    # ------------------------------------------------------------------
    # Synchronized sweep
    # ------------------------------------------------------------------
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

        n_coarse = len(next(iter(ao_tables.values())))
        if any(len(table) != n_coarse for table in ao_tables.values()):
            raise ValueError("All ao_tables must be the same length")

        initial_wait = max(initial_wait, 0.0)
        wait_samples = int(round(initial_wait * self.sample_rate))
        ramp_samples = max(int(round(duration * self.sample_rate)), 2)
        n_dense = wait_samples + ramp_samples

        dense_tables: dict[int, np.ndarray] = {}
        for channel, coarse in ao_tables.items():
            coarse = np.asarray(coarse, dtype=float)
            ramp = np.interp(
                np.linspace(0, n_coarse - 1, ramp_samples), np.arange(n_coarse), coarse
            )
            dense_tables[channel] = (
                np.concatenate([np.full(wait_samples, ramp[0]), ramp]) if wait_samples else ramp
            )

        # Fold in every DC-held channel not already part of this sweep (e.g.
        # a stepped Z during a 3D scan) as a flat table, so the isolated
        # finite run below doesn't disable/glitch it.
        for channel, value in self._held.items():
            if channel not in ao_tables:
                dense_tables[channel] = np.full(n_dense, value)

        # nidaqstudio's Scanner accepts plain 0-indexed AO{n}/AI{n} ints
        # directly -- no need to resolve physical names for this call, and
        # the result comes back keyed by the same deterministic "AO{n}"/
        # "AI{n}" labels, which we can reconstruct without an extra round trip.
        ao_by_index = {channel - 1: table for channel, table in dense_tables.items()}
        ai_by_index = [channel - 1 for channel in ai_channels]

        result = self.scanner.table(ao_by_index, ai_by_index, self.sample_rate)

        t_target = np.linspace(0, duration, n_coarse)
        out: dict[int, np.ndarray] = {}
        for channel in ai_channels:
            label = f"AI{channel - 1}"
            trace = result.ai[label]
            # `trace` is already AO+AI filter-delay corrected by the library;
            # drop our own lead-in (the initial_wait settle) from the front
            # too, then resample its natural time axis onto the caller's grid.
            trimmed = trace[min(wait_samples, max(len(trace) - 2, 0)):]
            if trimmed.size < 2:
                trimmed = trace
            t_source = np.linspace(0, duration, trimmed.size)
            out[channel] = np.interp(t_target, t_source, trimmed)
        return out

    def close(self) -> None:
        try:
            self.rig.stop()
        except Exception:
            pass
        self.rig.close()
