"""Backend driving NI PXIe cards through a running ``nidaqstudio`` process
(GUI or ``--api-only``) over its ZMQ API.

Three distinct modes share one connection:

- :meth:`set_dc` holds an AO channel at a fixed DC voltage *continuously* —
  the way this app expects an output to behave between sweeps (center
  stage, jog, Z step). This is done with the raw ``nidaqstudio.client``
  API: patch the channel to ``shape="dc"`` at the target offset, make sure
  the engine is running continuously.
- :meth:`run_sweep` plays an exact per-sample table on one or more AO
  channels while recording AI channels, as one isolated finite run
  (``nidaqstudio.scanner.Scanner.table``). Used for the Find Surface axis
  sweep. A scan like this explicitly disables every channel not part of
  it and restores the engine's prior configuration (unstarted) when it
  finishes — so every channel currently held by :meth:`set_dc` is folded
  into the sweep as a flat (constant) table, so nothing glitches or drops
  during the run.
- :meth:`run_scan_lines` overrides the base class's one-sweep-per-line
  default with one *continuous* acquisition for the whole scan. See its
  docstring for why and how — this is where the real speed difference
  between backends lives.

Channel numbers here are **1-indexed**, matching the "0 = disabled"
convention used everywhere else in this app's configuration: app channel 1
is nidaqstudio's ``AO0``/``AI0``, channel 2 is ``AO1``/``AI1``, and so on —
the same sequential numbering nidaqstudio's own GUI shows.
"""

from __future__ import annotations

import copy
import json
import time
from typing import Callable, Iterator, Sequence

import numpy as np

from .base import ScannerBackend

# Matches nidaqstudio's own default rather than the old Lockin's daq_fs
# (13000, a leftover from that instrument with no bearing on this one) --
# see run_scan_lines for why this, not the card's max rate, is what
# actually governs how short Settle can safely go in continuous mode.
DEFAULT_SAMPLE_RATE = 51_200.0

# run_scan_lines' continuous mode needs the queued output buffer (whatever
# chunk_samples * buffer_chunks is already configured -- deliberately not
# shrunk towards nidaqstudio's minimum, see the safety check in
# run_scan_lines for why) to drain well within one line's Settle window, or
# a live per-line Y update can't reliably land before the line it's meant
# for even starts. Below that, fall back to the base class's safe
# isolated-sweep-per-line default instead of risking misaligned data --
# raise Settle, or lower chunk_samples/buffer_chunks in nidaqstudio's own
# config if your hardware tolerates it, if you want continuous mode on
# very short/fast lines.


class _DataSubscriber:
    """Minimal PUB/SUB subscriber for nidaqstudio's ``data`` topic.

    The high-level ``NidaqStudio.stream()`` helper discards ``start_sample``;
    this keeps it, because that's what lets :meth:`NidaqBackend.run_scan_lines`
    detect a block nidaqstudio dropped (a slow subscriber "just drops
    messages rather than holding up acquisition", per nidaqstudio's own
    README) instead of silently concatenating non-contiguous data.
    """

    def __init__(self, pub_endpoint: str, timeout_s: float):
        import zmq  # optional dependency (comes with nidaqstudio); lazy on purpose

        self._zmq = zmq
        self._ctx = zmq.Context.instance()
        self._sock = self._ctx.socket(zmq.SUB)
        self._sock.setsockopt(zmq.SUBSCRIBE, b"data")
        self._sock.setsockopt(zmq.RCVTIMEO, int(timeout_s * 1000))
        self._sock.connect(pub_endpoint)
        # ZMQ's PUB/SUB "slow joiner" problem: a subscriber can miss the
        # first messages published just after it connects, since PUB does
        # not queue for subscribers that haven't finished subscribing yet.
        # A short grace period before the caller starts the acquisition
        # avoids racing this.
        time.sleep(0.2)

    def recv(self) -> dict:
        try:
            _topic, payload = self._sock.recv_multipart()
        except self._zmq.Again as exc:
            raise TimeoutError("No data received from nidaqstudio's PUB stream in time.") from exc
        return json.loads(payload)

    def close(self) -> None:
        self._sock.close(0)


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

    def _ai_name(self, channel: int) -> str:
        names = self._channel_names()["ai"]
        idx = channel - 1
        if not (0 <= idx < len(names)):
            raise ValueError(
                f"No AI channel {channel} -- nidaqstudio reports {len(names)} "
                f"AI channel(s) (1..{len(names)})."
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

    # ------------------------------------------------------------------
    # Continuous multi-line scan
    # ------------------------------------------------------------------
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
        """Acquire the whole scan as *one* continuous run instead of an
        isolated finite sweep per line.

        Every line's fast-axis (X) trajectory is identical, so X is set up
        **once**, as a native periodic waveform, and never touched again for
        the rest of the scan:

        - X plays an ``expression`` shape that holds flat at x_min for the
          settle fraction of each cycle, then ramps linearly to x_max —
          exactly the wait-then-ramp-then-snap-back pattern the isolated
          per-line sweep builds fresh each time, just expressed once as a
          repeating cycle instead of replayed as a table every line. This
          deliberately avoids ``shape="arbitrary"``: nidaqstudio's live
          config patches preserve the phase accumulator across a change (by
          design, so tweaking a running sine's frequency doesn't glitch),
          which means swapping an *arbitrary table's content* mid-cycle
          would start reading the new table from the old table's phase
          position, not from its own sample 0 -- silently misaligning
          pixels. A fixed periodic expression sidesteps that: it's only
          ever set up once, before the acquisition starts.
        - Y is ``shape="dc"``, whose value has no phase/cycle dependency at
          all, so live-patching its offset between lines is genuinely
          glitch-free. Line k+1's Y update is pushed the instant line k's
          samples have all been received — the earliest moment it's safe,
          since line k's ramp needs Y[k] stable right up to its last sample —
          giving the update all of line k+1's own Settle window to land
          before that line's ramp begins.

        Line boundaries in the data are pure arithmetic, not detection: since
        X's frequency and this call's own sample_rate never change once the
        acquisition starts, line *k* is deterministically samples
        ``[k*n, (k+1)*n)`` counting from when the task started. Data itself
        is consumed from the ``PUB`` stream rather than polled, since that
        was most of the round trips the isolated-sweep path spent per line.

        Falls back to the base class's isolated-sweep-per-line default when
        Settle (``initial_wait``) is too short relative to the queued output
        buffer's drain time for a live Y update to reliably land inside it.
        """
        y_values = list(y_values)
        y_points = len(y_values)
        if y_points == 0:
            return
        if not detector_channels:
            raise ValueError("run_scan_lines requires at least one detector channel")

        initial_wait = max(initial_wait, 0.0)
        n = max(int(round((line_time + initial_wait) * self.sample_rate)), 2)
        wait_frac = initial_wait / (line_time + initial_wait) if initial_wait > 0 else 0.0
        wait_n = int(round(wait_frac * n))

        original_config = self.rig.config()
        chunk_samples = int(original_config["timing"].get("chunk_samples", 2048))
        buffer_chunks = int(original_config["timing"].get("buffer_chunks", 4))

        # Y's live update for line k+1 is only safe to land within line k+1's
        # own settle window (wait_n samples = initial_wait seconds): any
        # earlier and it can still corrupt the tail of line k's ramp, any
        # later and it corrupts the start of line k+1's ramp. If the queued
        # output buffer alone (chunk_samples * buffer_chunks) takes longer
        # than that to drain, an update can't reliably land in time -- fall
        # back to the safe isolated-sweep-per-line default rather than risk
        # misaligned pixels. (Settle = 0 has no landing window at all, so it
        # always falls back.)
        #
        # This deliberately uses whatever chunk_samples/buffer_chunks are
        # already configured rather than shrinking them towards
        # nidaqstudio's absolute minimum (64 / 3) to buy more timing margin:
        # a thinner buffer has less headroom against real OS/driver
        # scheduling jitter refilling it in time, and underflowing it stops
        # the acquisition outright -- something the simulator, with no real
        # hardware timing pressure, won't ever show you. If Settle needs to
        # be longer to accommodate your hardware's actual buffer depth,
        # that's the safer trade to make.
        predicted_output_latency = (chunk_samples * buffer_chunks) / self.sample_rate
        if initial_wait <= 0.0 or predicted_output_latency > initial_wait * 0.5:
            yield from super().run_scan_lines(
                x_min=x_min, x_max=x_max, x_points=x_points, y_values=y_values,
                fast_axis_channel=fast_axis_channel, slow_axis_channel=slow_axis_channel,
                detector_channels=detector_channels, line_time=line_time,
                initial_wait=initial_wait, should_abort=should_abort,
            )
            return

        x_name = self._ao_name(fast_axis_channel)
        y_name = self._ao_name(slow_axis_channel)
        ai_names = [self._ai_name(ch) for ch in detector_channels]

        config = copy.deepcopy(original_config)

        amplitude = abs(x_max - x_min) / 2.0
        offset = (x_max + x_min) / 2.0
        frequency = self.sample_rate / n
        if wait_frac > 0:
            expression = (
                f"where(p < {wait_frac!r}, -1.0, "
                f"-1.0 + 2.0*(p - {wait_frac!r}) / {1.0 - wait_frac!r})"
            )
        else:
            expression = "2.0*p - 1.0"

        # Every other channel this backend is currently holding (e.g. Z
        # during a 3D scan) rides along as its own flat DC channel, exactly
        # like run_sweep folds held channels into an isolated sweep.
        other_held = {
            self._ao_name(ch): value
            for ch, value in self._held.items()
            if ch not in (fast_axis_channel, slow_axis_channel)
        }

        for ch in config["ao_channels"]:
            phys = ch["physical_channel"]
            if phys == x_name:
                ch.update(
                    enabled=True, shape="expression", expression=expression,
                    amplitude=amplitude, amplitude_unit="Vpk", offset=offset,
                    frequency=frequency, phase_deg=0.0,
                )
            elif phys == y_name:
                ch.update(enabled=True, shape="dc", offset=float(y_values[0]), amplitude=0.0)
            elif phys in other_held:
                ch.update(enabled=True, shape="dc", offset=other_held[phys], amplitude=0.0)
            else:
                ch["enabled"] = False

        for ch in config["ai_channels"]:
            ch["enabled"] = ch["physical_channel"] in ai_names

        # chunk_samples/buffer_chunks are intentionally left as already
        # configured -- see the safety check above for why.
        timing = config["timing"]
        timing["mode"] = "continuous"
        timing["sample_rate"] = self.sample_rate
        timing["history_seconds"] = max(timing.get("history_seconds", 10.0), 4 * n / self.sample_rate)

        subscriber = _DataSubscriber(self.rig.pub_endpoint, timeout_s=max(line_time * 20.0, 10.0))
        last_completed_line = -1
        try:
            self.rig.set_config(config)
            self.rig.start()
            if not self.rig.wait_until_running(timeout=10.0):
                raise TimeoutError("nidaqstudio did not start the continuous scan acquisition in time.")

            buffer_offset = 0  # global sample index that buffers[...][0] corresponds to
            buffers: dict[str, np.ndarray] = {phys: np.empty(0, dtype=np.float64) for phys in ai_names}
            received = 0
            next_prefetch = 1

            for line_idx in range(y_points):
                if should_abort is not None and should_abort():
                    break

                target = (line_idx + 1) * n
                while received < target:
                    try:
                        msg = subscriber.recv()
                    except TimeoutError:
                        # Enrich with what nidaqstudio itself last reported --
                        # in particular underflows/overruns/last_error, so a
                        # real-hardware stall (the output task couldn't be
                        # refilled in time and gave up) is distinguishable
                        # from, say, the server having simply gone away.
                        try:
                            status = self.rig.status()
                            detail = (
                                f"engine state={status.get('state')}, "
                                f"underflows={status.get('underflows')}, "
                                f"overruns={status.get('overruns')}, "
                                f"last_error={status.get('last_error')!r}"
                            )
                        except Exception as status_exc:
                            detail = f"(could not fetch engine status: {status_exc})"
                        raise TimeoutError(
                            f"No data received from nidaqstudio's PUB stream in time "
                            f"while waiting for line {line_idx + 1}/{y_points}. {detail}"
                        ) from None
                    start_sample = int(msg["start_sample"])
                    if start_sample != received:
                        raise RuntimeError(
                            f"nidaqstudio data stream gap detected (expected sample "
                            f"{received}, got {start_sample}) -- the subscriber fell "
                            f"behind and nidaqstudio dropped a block. Try a lower "
                            f"sample rate or a larger buffer."
                        )
                    data = msg["data"]
                    block_len = len(data[0]) if data else 0
                    for i, phys in enumerate(msg["channels"]):
                        if phys in buffers:
                            buffers[phys] = np.concatenate(
                                [buffers[phys], np.asarray(data[i], dtype=np.float64)]
                            )
                    received += block_len

                # Only now is it safe to move Y on to the next line: we've
                # just finished needing this line's Y value to stay stable.
                # This gives the update the entirety of the next line's own
                # Settle window to land before that line's ramp begins.
                if next_prefetch < y_points and next_prefetch == line_idx + 1:
                    self.rig.set_ao(y_name, offset=float(y_values[next_prefetch]))
                    next_prefetch += 1

                start_local = line_idx * n - buffer_offset
                end_local = start_local + n
                pixels: dict[int, np.ndarray] = {}
                for channel, phys in zip(detector_channels, ai_names):
                    segment = buffers[phys][start_local:end_local][wait_n:]
                    if segment.size >= 2:
                        pixels[channel] = np.interp(
                            np.linspace(0, 1, x_points), np.linspace(0, 1, segment.size), segment,
                        )
                    else:
                        pixels[channel] = np.full(x_points, np.nan)
                yield pixels
                last_completed_line = line_idx

                new_offset = (line_idx + 1) * n
                drop_local = new_offset - buffer_offset
                for phys in ai_names:
                    buffers[phys] = buffers[phys][drop_local:]
                buffer_offset = new_offset
        finally:
            subscriber.close()
            try:
                self.rig.stop()
            finally:
                self.rig.set_config(original_config)
                # Always leave the stage actively, continuously held
                # somewhere real afterward -- matches the guarantee the
                # per-line default gives via its flyback -- rather than
                # leaving the engine configured-but-idle.
                resting_line = max(last_completed_line, 0)
                self._held[fast_axis_channel] = x_min
                self._held[slow_axis_channel] = float(y_values[resting_line])
                self._push_hold_config()
                self.rig.start()

    def close(self) -> None:
        try:
            self.rig.stop()
        except Exception:
            pass
        self.rig.close()
