"""Backend driving NI cards *directly* through NI-DAQmx — no nidaqstudio
process in between, no request/reply per line, no software writer thread.

The model here is the one an Asylum-style controller uses: the **entire
raster** (every line's settle + ramp on X, the Y staircase, and a constant
on Z / any other held channel) is built up front as one array per output
channel, written into a finite hardware-timed AO task in full *before* the
task starts, and played out by the card on its own sample clock. AI runs as
a finite task slaved to that same clock and start trigger, so output and
input are sample-locked in hardware; line *k* of the scan is simply samples
``[k*n, (k+1)*n)`` of the acquisition (after the converters' fixed filter
delay is subtracted), read back in chunks as they arrive so the image still
fills in line by line.

Nothing has to be refilled or patched while the scan runs, so there is no
software timing margin to protect at all: the only limits left are the
card's own sample rate, the piezo/amplifier's bandwidth, and detector
noise vs. dwell time. Settle is purely whatever the stage physically needs.

This is only usable on the PC that has the PXI chassis (DAQmx is a local
driver), which also removes every network hop.

Channel numbers are **1-indexed sequentially across the configured
devices**, in the order they're listed, matching this app's "0 = disabled"
convention: with ``devices = [PXI1Slot2, PXI1Slot3]`` and 2 AO/2 AI per
card, output 1 = ``PXI1Slot2/ao0``, 2 = ``PXI1Slot2/ao1``, 3 =
``PXI1Slot3/ao0``, ... and likewise for inputs. Cards *not* listed are never
touched, so the rest of the chassis stays free for other software.

Multi-card synchronization follows the standard NI DSA recipe (the same
one nidaqstudio uses on this hardware): common reference clock, the
master's SyncPulse to align delta-sigma filter state, a shared settle
delay, and every slave task start-triggered from the master AO task.
Every step is guarded and recorded in :attr:`NidaqmxBackend.sync_report`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Sequence

import numpy as np

from .base import ScannerBackend

DEFAULT_SAMPLE_RATE = 51_200.0

# How much of the acquisition to read per DAQmx read call, in seconds.
# Smaller = faster abort response; it has no effect on the data itself
# (the whole raster is already committed to the card).
_READ_CHUNK_SECONDS = 0.1

# Length of the hardware-timed burst used to move an output when the card
# won't accept a software-timed (on-demand) write -- DSA cards like the
# 4461 are hardware-timed only.
_DC_BURST_SECONDS = 0.02


# ======================================================================
# Pure raster arithmetic (hardware-free, unit-testable)
# ======================================================================


@dataclass
class RasterPlan:
    """Everything about a scan's sample layout, decided before any task
    exists, so slicing the data back into lines is pure arithmetic."""

    sample_rate: float
    wait_n: int        # settle samples at x_min at the start of each line
    ramp_n: int        # samples in each line's x_min -> x_max ramp
    y_points: int
    tail_n: int        # extra samples after the last line (park + drain delay)
    fast: np.ndarray   # full X trajectory, len == total
    slow: np.ndarray   # full Y trajectory, len == total

    @property
    def line_n(self) -> int:
        return self.wait_n + self.ramp_n

    @property
    def total(self) -> int:
        return self.y_points * self.line_n + self.tail_n

    def line_bounds(self, k: int, delay_samples: int) -> tuple[int, int]:
        """AI sample range holding line ``k``'s *ramp* (settle excluded),
        shifted by the converters' delay."""
        start = k * self.line_n + self.wait_n + delay_samples
        return start, start + self.ramp_n


def build_raster(
    *,
    x_min: float,
    x_max: float,
    y_values: Sequence[float],
    line_time: float,
    initial_wait: float,
    sample_rate: float,
    tail_seconds: float,
) -> RasterPlan:
    """Lay out the whole scan as sample-exact X/Y trajectories.

    Each line is ``initial_wait`` seconds flat at ``x_min`` (the stage's
    flyback settle) followed by a ``line_time`` linear ramp to ``x_max``; Y
    holds that line's value throughout. After the last line a short tail
    parks X back at ``x_min`` (and keeps Y at its last value) so the scan
    itself leaves the stage where the per-line flyback would have -- and
    that tail is also what lets the filter-delayed AI of the final line
    fully arrive before the acquisition ends.
    """
    y_values = np.asarray(list(y_values), dtype=float)
    wait_n = int(round(max(initial_wait, 0.0) * sample_rate))
    ramp_n = max(int(round(line_time * sample_rate)), 2)
    tail_n = max(int(round(tail_seconds * sample_rate)), 2)

    line_x = np.concatenate([np.full(wait_n, x_min), np.linspace(x_min, x_max, ramp_n)])
    fast = np.concatenate([np.tile(line_x, len(y_values)), np.full(tail_n, x_min)])
    slow = np.concatenate([
        np.repeat(y_values, wait_n + ramp_n),
        np.full(tail_n, y_values[-1] if len(y_values) else 0.0),
    ])
    return RasterPlan(
        sample_rate=sample_rate, wait_n=wait_n, ramp_n=ramp_n,
        y_points=len(y_values), tail_n=tail_n, fast=fast, slow=slow,
    )


def resample_line(segment: np.ndarray, x_points: int) -> np.ndarray:
    """Resample one line's ramp-portion samples onto the pixel grid."""
    if segment.size == x_points:
        return segment.astype(float, copy=True)
    src = np.linspace(0.0, 1.0, segment.size)
    dst = np.linspace(0.0, 1.0, x_points)
    return np.interp(dst, src, segment)


# ======================================================================
# Hardware layer
# ======================================================================


class FiniteRun:
    """One synchronized finite AO+AI run over the configured devices.

    Construct, then call :meth:`start`, then :meth:`read` repeatedly until
    ``samples_read == total``; :meth:`close` always. Split out from the
    backend so the backend's scan logic can be tested against a fake run.
    """

    def __init__(
        self,
        nidaqmx: Any,
        *,
        ao: dict[str, np.ndarray],
        ai: Sequence[str],
        sample_rate: float,
        ao_range: float,
        ai_range: float,
        sync: bool,
    ):
        from nidaqmx.constants import AcquisitionType, DigitalWidthUnits

        self._nidaqmx = nidaqmx
        self.ao_names = list(ao)
        self.ai_names = list(ai)
        if not self.ao_names:
            raise ValueError("A finite run needs at least one AO channel")
        if not self.ai_names:
            raise ValueError("A finite run needs at least one AI channel")

        lengths = {v.size for v in ao.values()}
        if len(lengths) != 1:
            raise ValueError("All AO tables must be the same length (one shared sample clock)")
        self.total = lengths.pop()
        self.sample_rate = sample_rate
        self.delay_seconds = 0.0
        self.sync_report: list[str] = []
        self.samples_read = 0

        self._ao_tasks: dict[str, Any] = {}   # device -> task
        self._ai_tasks: dict[str, Any] = {}
        self._ao_rows: dict[str, list[str]] = {}
        self._ai_rows: dict[str, list[str]] = {}
        self._readers: list[tuple[Any, list[int]]] = []  # (reader, output row indices)
        self._master_task: Any = None
        self._closed = False

        try:
            # --- tasks, one per device per direction ------------------
            for name in self.ao_names:
                dev = _device_of(name)
                if dev not in self._ao_tasks:
                    self._ao_tasks[dev] = nidaqmx.Task()
                    self._ao_rows[dev] = []
                chan = self._ao_tasks[dev].ao_channels.add_ao_voltage_chan(
                    name, min_val=-ao_range, max_val=ao_range)
                _try_set_maintain_value(chan, nidaqmx)
                self._ao_rows[dev].append(name)
            for name in self.ai_names:
                dev = _device_of(name)
                if dev not in self._ai_tasks:
                    self._ai_tasks[dev] = nidaqmx.Task()
                    self._ai_rows[dev] = []
                self._ai_tasks[dev].ai_channels.add_ai_voltage_chan(
                    name, min_val=-ai_range, max_val=ai_range)
                self._ai_rows[dev].append(name)

            # --- timing ------------------------------------------------
            for task in list(self._ao_tasks.values()) + list(self._ai_tasks.values()):
                task.timing.cfg_samp_clk_timing(
                    rate=sample_rate, sample_mode=AcquisitionType.FINITE,
                    samps_per_chan=self.total)
            actual = float(next(iter(self._ao_tasks.values())).timing.samp_clk_rate)
            if abs(actual - sample_rate) / sample_rate > 1e-6:
                raise RuntimeError(
                    f"Hardware coerced the sample rate {sample_rate:g} -> {actual:g} S/s; "
                    f"the raster was laid out for {sample_rate:g}. Use a rate the card "
                    f"supports natively (the backend probes this at connect time).")

            # --- synchronization ---------------------------------------
            if sync:
                self._configure_sync()
            else:
                self.sync_report.append("Synchronization disabled by configuration.")

            # --- filter delay (DSA group delay, AO + AI) ---------------
            ai_delay = ao_delay = 0.0
            try:
                ch = next(iter(self._ai_tasks.values())).ai_channels[0]
                ch.ai_filter_delay_units = DigitalWidthUnits.SECONDS
                ai_delay = float(ch.ai_filter_delay)
            except Exception:  # noqa: BLE001 - non-DSA cards don't report one
                pass
            try:
                ch = next(iter(self._ao_tasks.values())).ao_channels[0]
                ch.ao_filter_delay_units = DigitalWidthUnits.SECONDS
                ao_delay = float(ch.ao_filter_delay)
            except Exception:  # noqa: BLE001
                pass
            self.delay_seconds = ai_delay + ao_delay
            self.sync_report.append(
                f"Filter delay -- AI: {ai_delay * 1e6:.1f} us, AO: {ao_delay * 1e6:.1f} us")

            # --- commit the whole waveform before anything starts ------
            from nidaqmx.stream_readers import AnalogMultiChannelReader
            from nidaqmx.stream_writers import AnalogMultiChannelWriter

            for dev, task in self._ao_tasks.items():
                block = np.ascontiguousarray(
                    np.vstack([ao[n] for n in self._ao_rows[dev]]), dtype=np.float64)
                AnalogMultiChannelWriter(task.out_stream, auto_start=False).write_many_sample(
                    block, timeout=60.0)
            for dev, task in self._ai_tasks.items():
                rows = [self.ai_names.index(n) for n in self._ai_rows[dev]]
                self._readers.append((AnalogMultiChannelReader(task.in_stream), rows))
        except Exception:
            self.close()
            raise

    # -- sync ------------------------------------------------------------
    def _configure_sync(self) -> None:
        master_dev = next(iter(self._ao_tasks))
        self._master_task = self._ao_tasks[master_dev]
        all_tasks = {**{f"ao:{d}": t for d, t in self._ao_tasks.items()},
                     **{f"ai:{d}": t for d, t in self._ai_tasks.items()}}
        slaves = {k: t for k, t in all_tasks.items() if t is not self._master_task}
        self.sync_report.append(f"Master: {master_dev} (ao task)")
        if not slaves:
            self.sync_report.append("Single task -- no cross-task sync needed.")
            return

        # 1. Reference clock: PXIe_Clk100 first, PXI_Clk10 as the fallback.
        for src, rate in (("PXIe_Clk100", 100e6), ("PXI_Clk10", 10e6)):
            ok, failed = 0, []
            for key, task in all_tasks.items():
                try:
                    task.timing.ref_clk_src = src
                    task.timing.ref_clk_rate = rate
                    ok += 1
                except Exception as exc:  # noqa: BLE001
                    failed.append(f"{key} ({exc.__class__.__name__})")
            if ok == len(all_tasks):
                self.sync_report.append(f"Reference clock: {src} on all {ok} task(s)")
                break
            self.sync_report.append(f"Reference clock {src} rejected by: {', '.join(failed)}")
        else:
            self.sync_report.append(
                "Reference clock: none applied -- cards run on independent timebases.")

        # 2. Sync pulse (DSA delta-sigma filter alignment), 3. settle delay.
        pulse = f"/{master_dev}/SyncPulse"
        applied = 0
        for key, task in slaves.items():
            try:
                task.timing.sync_pulse_src = pulse
                applied += 1
            except Exception as exc:  # noqa: BLE001
                self.sync_report.append(f"{key}: sync pulse failed ({exc.__class__.__name__})")
        if applied:
            self.sync_report.append(f"Sync pulse {pulse} -> {applied} slave task(s)")
            sync_times = []
            for task in all_tasks.values():
                try:
                    sync_times.append(float(task.timing.sync_pulse_sync_time))
                except Exception:  # noqa: BLE001
                    pass
            if sync_times:
                worst = max(sync_times)
                for task in all_tasks.values():
                    try:
                        task.timing.sync_pulse_min_delay_to_start = worst
                    except Exception:  # noqa: BLE001
                        pass
                self.sync_report.append(f"Sync settle delay: {worst * 1e3:.3f} ms")

        # 4. Start trigger: everything follows the master AO task.
        trigger = f"/{master_dev}/ao/StartTrigger"
        applied = 0
        for key, task in slaves.items():
            try:
                task.triggers.start_trigger.cfg_dig_edge_start_trig(trigger)
                applied += 1
            except Exception as exc:  # noqa: BLE001
                self.sync_report.append(f"{key}: start trigger failed ({exc})")
        self.sync_report.append(f"Start trigger {trigger} -> {applied} slave task(s)")

    # -- runtime -----------------------------------------------------------
    def start(self) -> None:
        # 5. Slaves first (armed, waiting on the trigger), master last.
        for task in self._ai_tasks.values():
            if task is not self._master_task:
                task.start()
        for task in self._ao_tasks.values():
            if task is not self._master_task:
                task.start()
        if self._master_task is not None:
            self._master_task.start()
        else:
            for task in list(self._ao_tasks.values()) + list(self._ai_tasks.values()):
                task.start()

    def read(self, n: int, timeout: float) -> np.ndarray:
        """Read the next ``n`` samples of every AI channel, shape
        ``(len(ai_names), n)``, rows in ``ai_names`` order."""
        n = min(n, self.total - self.samples_read)
        out = np.empty((len(self.ai_names), n), dtype=np.float64)
        for reader, rows in self._readers:
            buf = np.empty((len(rows), n), dtype=np.float64)
            reader.read_many_sample(buf, number_of_samples_per_channel=n, timeout=timeout)
            out[rows] = buf
        self.samples_read += n
        return out

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in list(self._ao_tasks.values()) + list(self._ai_tasks.values()):
            try:
                task.stop()
            except Exception:  # noqa: BLE001
                pass
            try:
                task.close()
            except Exception:  # noqa: BLE001
                pass
        self._ao_tasks.clear()
        self._ai_tasks.clear()
        self._readers.clear()


def _device_of(physical_channel: str) -> str:
    return physical_channel.split("/", 1)[0]


def _try_set_maintain_value(chan: Any, nidaqmx: Any) -> None:
    """Ask the card to hold its last value once a task stops, on cards
    that expose the choice; the rest either hold anyway or don't, and
    there's nothing to be done about it from software."""
    try:
        from nidaqmx.constants import AOIdleOutputBehavior

        chan.ao_idle_output_behavior = AOIdleOutputBehavior.MAINTAIN_EXISTING_VALUE
    except Exception:  # noqa: BLE001
        pass


# ======================================================================
# Backend
# ======================================================================


@dataclass
class _ChannelMap:
    ao: list[str] = field(default_factory=list)
    ai: list[str] = field(default_factory=list)


class NidaqmxBackend(ScannerBackend):
    def __init__(
        self,
        devices: Sequence[str],
        *,
        sample_rate: float = DEFAULT_SAMPLE_RATE,
        ao_range: float = 10.0,
        ai_range: float = 10.0,
        sync: bool = True,
    ):
        try:
            import nidaqmx
            import nidaqmx.system
        except ImportError as exc:
            raise ImportError(
                "The 'nidaqmx' package is not installed in this environment "
                "(pip install nidaqmx), or the NI-DAQmx driver is missing. "
                "This backend only works on the PC the PXI chassis is plugged into."
            ) from exc

        devices = [d.strip() for d in devices if d and d.strip()]
        if not devices:
            raise ValueError("No DAQmx devices configured for the NI-DAQmx backend.")

        self._nidaqmx = nidaqmx
        self.devices = devices
        self.ao_range = float(ao_range)
        self.ai_range = float(ai_range)
        self.sync = bool(sync)
        self.sync_report: list[str] = []
        self._held: dict[int, float] = {}
        self._dc_mode: str | None = None  # "on_demand" | "burst", learned on first use

        self.channels = self._discover(nidaqmx.system.System.local(), devices)
        self.sample_rate = self._probe_rate(float(sample_rate))

    # -- discovery ---------------------------------------------------------
    @staticmethod
    def _discover(system: Any, devices: Sequence[str]) -> _ChannelMap:
        known = {d.name: d for d in system.devices}
        cmap = _ChannelMap()
        for name in devices:
            dev = known.get(name)
            if dev is None:
                raise ConnectionError(
                    f"DAQmx device '{name}' not found. NI-DAQmx sees: "
                    f"{', '.join(sorted(known)) or 'no devices at all'}.")
            cmap.ao.extend(ch.name for ch in dev.ao_physical_chans)
            cmap.ai.extend(ch.name for ch in dev.ai_physical_chans)
        if not cmap.ao:
            raise ConnectionError(f"Devices {devices} expose no analog outputs.")
        return cmap

    def _probe_rate(self, requested: float) -> float:
        """Ask the first card what it will actually run at for the requested
        rate -- DSA cards coerce to their own clock divisors -- so the
        raster is laid out on the true rate rather than a nominal one."""
        from nidaqmx.constants import AcquisitionType

        with self._nidaqmx.Task() as task:
            task.ao_channels.add_ao_voltage_chan(
                self.channels.ao[0], min_val=-self.ao_range, max_val=self.ao_range)
            task.timing.cfg_samp_clk_timing(
                rate=requested, sample_mode=AcquisitionType.FINITE, samps_per_chan=2)
            actual = float(task.timing.samp_clk_rate)
        if abs(actual - requested) / requested > 1e-6:
            self.sync_report.append(
                f"Sample rate coerced by hardware: {requested:.6g} -> {actual:.6g} S/s")
        return actual

    def _ao_name(self, channel: int) -> str:
        idx = channel - 1
        if not (0 <= idx < len(self.channels.ao)):
            raise ValueError(
                f"No AO channel {channel} -- devices {self.devices} provide "
                f"{len(self.channels.ao)} AO channel(s) (1..{len(self.channels.ao)}).")
        return self.channels.ao[idx]

    def _ai_name(self, channel: int) -> str:
        idx = channel - 1
        if not (0 <= idx < len(self.channels.ai)):
            raise ValueError(
                f"No AI channel {channel} -- devices {self.devices} provide "
                f"{len(self.channels.ai)} AI channel(s) (1..{len(self.channels.ai)}).")
        return self.channels.ai[idx]

    # -- DC positioning ----------------------------------------------------
    def set_dc(self, channel: int, value: float, settle: bool = True) -> None:
        name = self._ao_name(channel)
        self._write_dc(name, float(value))
        self._held[channel] = float(value)

    def _write_dc(self, name: str, value: float) -> None:
        """Move one output to ``value`` and leave it there.

        General-purpose cards take a software-timed single write. DSA cards
        (4461 etc.) are hardware-timed only, so for them play a short
        finite burst of the constant instead; either way the DAC holds the
        last sample once the task ends.
        """
        nidaqmx = self._nidaqmx
        if self._dc_mode != "burst":
            try:
                with nidaqmx.Task() as task:
                    chan = task.ao_channels.add_ao_voltage_chan(
                        name, min_val=-self.ao_range, max_val=self.ao_range)
                    _try_set_maintain_value(chan, nidaqmx)
                    task.write(value, auto_start=True)
                self._dc_mode = "on_demand"
                return
            except Exception:  # noqa: BLE001 - on-demand unsupported; fall through
                if self._dc_mode == "on_demand":
                    raise
                self._dc_mode = "burst"

        from nidaqmx.constants import AcquisitionType
        from nidaqmx.stream_writers import AnalogMultiChannelWriter

        n = max(int(round(_DC_BURST_SECONDS * self.sample_rate)), 2)
        with nidaqmx.Task() as task:
            chan = task.ao_channels.add_ao_voltage_chan(
                name, min_val=-self.ao_range, max_val=self.ao_range)
            _try_set_maintain_value(chan, nidaqmx)
            task.timing.cfg_samp_clk_timing(
                rate=self.sample_rate, sample_mode=AcquisitionType.FINITE, samps_per_chan=n)
            AnalogMultiChannelWriter(task.out_stream, auto_start=False).write_many_sample(
                np.full((1, n), value, dtype=np.float64))
            task.start()
            task.wait_until_done(timeout=10.0)

    # -- finite runs -------------------------------------------------------
    def _open_run(self, ao: dict[str, np.ndarray], ai: Sequence[str]) -> FiniteRun:
        run = FiniteRun(
            self._nidaqmx, ao=ao, ai=ai, sample_rate=self.sample_rate,
            ao_range=self.ao_range, ai_range=self.ai_range, sync=self.sync)
        self.sync_report = list(run.sync_report)
        return run

    def _held_tables(self, exclude: Sequence[int], total: int) -> dict[str, np.ndarray]:
        """Every DC-held output not otherwise in a run, as a flat table, so
        it's driven (not left to whatever the card does when idle) for the
        duration -- e.g. Z during a 3D slice."""
        return {
            self._ao_name(ch): np.full(total, val)
            for ch, val in self._held.items() if ch not in exclude
        }

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
        if any(len(t) != n_coarse for t in ao_tables.values()):
            raise ValueError("All ao_tables must be the same length")

        rate = self.sample_rate
        wait_n = int(round(max(initial_wait, 0.0) * rate))
        ramp_n = max(int(round(duration * rate)), 2)
        # Generous tail: the delayed AI of the sweep's end must arrive
        # before the finite acquisition stops.
        tail_n = max(int(round(0.02 * rate)), 2)
        total = wait_n + ramp_n + tail_n

        ao: dict[str, np.ndarray] = {}
        for ch, coarse in ao_tables.items():
            coarse = np.asarray(coarse, dtype=float)
            ramp = np.interp(np.linspace(0, n_coarse - 1, ramp_n), np.arange(n_coarse), coarse)
            ao[self._ao_name(ch)] = np.concatenate(
                [np.full(wait_n, ramp[0]), ramp, np.full(tail_n, ramp[-1])])
        ao.update(self._held_tables(exclude=list(ao_tables), total=total))
        ai_names = [self._ai_name(ch) for ch in ai_channels]

        run = self._open_run(ao, ai_names)
        try:
            run.start()
            data = np.empty((len(ai_names), 0))
            chunk = max(int(round(_READ_CHUNK_SECONDS * rate)), 1)
            while run.samples_read < run.total:
                block = run.read(chunk, timeout=chunk / rate + 10.0)
                data = np.concatenate([data, block], axis=1)
        finally:
            run.close()

        d = int(round(run.delay_seconds * rate))
        start = min(wait_n + d, total - 2)
        stop = min(start + ramp_n, total)
        out: dict[int, np.ndarray] = {}
        for i, ch in enumerate(ai_channels):
            out[ch] = resample_line(data[i, start:stop], n_coarse)
        # The sweep's own tables end where they end; record where every
        # swept output was left so later runs fold it in correctly.
        for ch, coarse in ao_tables.items():
            self._held[ch] = float(np.asarray(coarse, dtype=float)[-1])
        return out

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
        """The whole scan as one hardware-timed finite run -- see the
        module docstring. Lines are yielded as their samples arrive; the
        card keeps generating regardless of how fast they're consumed, so
        pausing the consumer only delays *display*, never the stage.
        Aborting stops the tasks mid-raster and parks X at x_min.
        """
        y_values = list(y_values)
        if not y_values:
            return
        if not detector_channels:
            raise ValueError("run_scan_lines requires at least one detector channel")

        rate = self.sample_rate
        # The tail must outlast the filter delay by a margin so the last
        # line's delayed samples are inside the acquisition; a settle-sized
        # park at x_min is also the natural "flyback" the per-line default
        # ends with.
        tail_seconds = max(initial_wait, 0.05)
        plan = build_raster(
            x_min=x_min, x_max=x_max, y_values=y_values, line_time=line_time,
            initial_wait=initial_wait, sample_rate=rate, tail_seconds=tail_seconds)

        ao = {
            self._ao_name(fast_axis_channel): plan.fast,
            self._ao_name(slow_axis_channel): plan.slow,
        }
        ao.update(self._held_tables(
            exclude=[fast_axis_channel, slow_axis_channel], total=plan.total))
        ai_names = [self._ai_name(ch) for ch in detector_channels]

        run = self._open_run(ao, ai_names)
        completed = 0
        try:
            run.start()
            d = int(round(run.delay_seconds * rate))
            if d > plan.tail_n:
                raise RuntimeError(
                    f"Converter filter delay ({run.delay_seconds * 1e3:.2f} ms) exceeds the "
                    f"scan tail ({plan.tail_n / rate * 1e3:.2f} ms); the last line's data "
                    f"would fall outside the acquisition.")

            chunk = max(int(round(_READ_CHUNK_SECONDS * rate)), 1)
            buf = np.empty((len(ai_names), 0))
            consumed = 0  # samples dropped off the front of buf so far
            k = 0
            while k < plan.y_points:
                if should_abort is not None and should_abort():
                    return
                start, stop = plan.line_bounds(k, d)
                while consumed + buf.shape[1] < stop:
                    if should_abort is not None and should_abort():
                        return
                    if run.samples_read >= run.total:
                        raise RuntimeError("Acquisition ended before the last line arrived.")
                    block = run.read(chunk, timeout=chunk / rate + 10.0)
                    buf = np.concatenate([buf, block], axis=1)
                seg = buf[:, start - consumed:stop - consumed]
                pixels = {
                    ch: resample_line(seg[i], x_points) for i, ch in enumerate(detector_channels)
                }
                # Drop everything before this line's end; nothing looks back.
                buf = buf[:, stop - consumed:]
                consumed = stop
                completed = k + 1
                k += 1
                yield pixels
            # Let the tail (park at x_min) finish playing before tearing down.
            while run.samples_read < run.total:
                run.read(chunk, timeout=chunk / rate + 10.0)
        finally:
            run.close()
            self._held[fast_axis_channel] = x_min
            self._held[slow_axis_channel] = float(
                y_values[completed - 1] if completed else y_values[0])
            if completed < plan.y_points:
                # Aborted mid-raster: X is wherever the ramp was cut off.
                # Park it explicitly, as the per-line default's flyback would.
                try:
                    self._write_dc(self._ao_name(fast_axis_channel), x_min)
                except Exception:  # noqa: BLE001 - best effort during abort
                    pass

    def close(self) -> None:
        pass


def list_devices() -> list[dict[str, Any]]:
    """Every device NI-DAQmx can see, for the config dialog."""
    import nidaqmx.system

    out = []
    for dev in nidaqmx.system.System.local().devices:
        try:
            n_ao = len(list(dev.ao_physical_chans))
            n_ai = len(list(dev.ai_physical_chans))
        except Exception:  # noqa: BLE001
            n_ao = n_ai = 0
        try:
            simulated = bool(dev.dev_is_simulated)
        except Exception:  # noqa: BLE001
            simulated = False
        out.append({
            "name": dev.name,
            "product": getattr(dev, "product_type", "") or "",
            "ao": n_ao, "ai": n_ai, "simulated": simulated,
        })
    return out
