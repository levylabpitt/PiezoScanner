# FLEX PiezoScanner

PyQt6 app for driving a piezo scan stage and grabbing raster-scan images
off up to 3 detector channels. Supports plain 2D scans and 3D stacks (a 2D
scan at each Z level), plus a find-surface tool for locating the sample
along Z. The hardware backend is switchable — either the Levylab FLEX
Multichannel Lockin, or an NI PXIe rig driven through `nidaqstudio`.

This replaces the old `app.py` / `piezoscanner.py` scripts. Same basic idea,
but rewritten as a proper installable package: scanning runs on its own
thread so the UI doesn't lock up mid-scan, channels and hardware wiring are
configured in the GUI instead of by editing code, and a couple of bugs from
the old version are fixed (centering used to jump to a hardcoded 5V no
matter what profile/range you had set).

## Requirements

- Python 3.10+
- PyQt6, numpy, matplotlib, PyYAML — these get installed automatically
- One of the two hardware backends, if you want to talk to real hardware.
  Both are internal packages, not on PyPI, so you install whichever one you
  need separately into the same environment. Without either, the app just
  starts in Simulation Mode — useful for checking the UI works before
  you're next to the actual stage.
  - **Lockin backend**: `flex` (the Levylab FLEX framework).
  - **nidaqstudio backend**: the `nidaqstudio` Python package. Its own GUI
    or headless server (`python -m nidaqstudio`) runs separately — often on
    a different machine than this app — and this package just needs its
    client library importable to talk to that server over the network.

## Installing

Clone it:

```bash
git clone https://github.com/levylabpitt/PiezoScanner.git
cd PiezoScanner
```

With `uv` (if you don't have it yet, on Windows run
`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
then reopen your terminal so it picks up PATH):

```bash
uv venv
uv pip install -e .
```

Or plain pip:

```bash
python -m venv .venv
.venv\Scripts\activate      # on Windows
pip install -e .
```

Then, if this PC is hooked up to actual hardware, install `flex` into the
same environment however you normally do that on the lab machines.

## Running it

```bash
python -m piezoscanner
```

or, since installing the package also gives you a console script:

```bash
piezoscanner
```

## Hardware configuration

Backend choice, output wiring, and stage profiles all live in a YAML file
at `%LOCALAPPDATA%\Levylab\PiezoScanner\config.yaml`, created with defaults
on first run. Edit it by hand or use **Settings → Configure Hardware…** in
the app — same thing. It holds:

- which backend to use: `lockin` or `nidaqstudio` (and, for nidaqstudio,
  the host/port it's listening on, plus the sample rate used to play each
  line's table — see below)
- which output channel drives X, Y, and Z (set a channel to 0 to disable
  that axis — Z is off by default, and 3D mode / Find Surface only appear
  once you give Z a channel)
- the list of stage profiles: safe voltage range, um/V calibration, and a
  calibrated yes/no flag (uncalibrated profiles show a warning in the app)

Adding a new stage is just adding a few lines to that file.

Switching backend or its connection settings takes effect as soon as you
hit Save in the dialog — no restart needed (it can't be changed mid-scan
though; finish or abort first).

### Choosing a backend

**Lockin** talks to the Levylab FLEX Multichannel Lockin the way this app
always has. Output/input channel numbers are the Lockin's own AO/AI
numbers (11, 12, 8, 9, ... — whatever your instrument uses).

**nidaqstudio** talks to NI PXIe cards through a separately-running
`nidaqstudio` process (its own GUI, or `python -m nidaqstudio --api-only`
for a headless rack PC) over ZMQ — this app never touches the DAQmx driver
directly. Start that process first, then in the config dialog:

1. Switch the backend dropdown to nidaqstudio.
2. Set Host/Port (defaults to `127.0.0.1:8765`, nidaqstudio's default).
3. Hit **Test Connection** to confirm it's reachable before saving.

Channel numbers for this backend are **1-indexed into nidaqstudio's own
AO0/AO1/... and AI0/AI1/... sequence** (the same numbering its GUI shows,
sequential across every card): channel 1 = AO0/AI0, channel 2 = AO1/AI1,
and so on — 0 still means disabled. Since nidaqstudio auto-detects hardware,
the channel count depends on whatever cards are actually in the chassis.

**Run the nidaqstudio server from its own environment, not this app's.**
This app only needs `nidaqstudio`'s client (`nidaqstudio.client` /
`nidaqstudio.scanner`), which is Qt-free — but `nidaqstudio`'s own package
declares PySide6 + pyqtgraph as hard dependencies for its GUI, and *this*
app depends on PyQt6. If both end up installed in the same environment and
you then launch `nidaqstudio`'s own GUI (plain `nidaqstudio --simulate`,
no `--api-only`) from that environment, pyqtgraph's Qt-binding
autodetection can pick the wrong one and it'll crash on startup with a
`TypeError` from `addWidget` — that's `nidaqstudio`'s GUI failing, not
this app. Keep them apart: run `nidaqstudio` (GUI or `--api-only`) from
wherever you normally run it, and only `pip install`/`uv pip install`
`nidaqstudio` into *this* app's environment for the client library. If you
do need to launch it from a shared environment for some reason,
`--api-only` sidesteps the problem — it never imports the GUI at all.

### Making scans faster

Total scan time is `y_points × (line_time + Settle)`, roughly, plus backend
overhead. That overhead looks very different depending on the backend:

**Lockin** does one hardware sweep per line — there's no way around that,
it's how the instrument works. The only levers are the ones you already
control directly:

1. **Settle** (Scan Configuration group) — how long each line holds at its
   start before ramping, to let the flyback finish settling. It's charged
   once *per line*, so at the old fixed 1 s default a 50-line scan spent 50
   extra seconds here alone. Lower it until you see a distorted/smeared
   left edge on your lines, then back off a bit — that's your real floor,
   and it depends on your stage's mechanical settling time, not the
   software.
2. **Line time** — the direct lever, with the direct tradeoff: less
   integration time per pixel means more noise. How far you can push it
   depends on your signal.

**nidaqstudio** doesn't have to sweep line-by-line at all: the whole scan
runs as *one* continuous acquisition. X plays a fixed waveform set up once
at the start (it never changes between lines, since every line's ramp is
identical), Y's value live-updates between lines with no task restart, and
data comes back over nidaqstudio's push-based data stream instead of being
polled for. Concretely, this replaced roughly 20 request/reply round trips
per line with about 1 — on the same local connection that's already a
measurable win, and it gets much larger the moment the nidaqstudio server
isn't sharing zero-latency loopback with this app (a separate rack PC, or
any real network hop): in a simulated-latency test at 20 ms/call, a
15-line scan went from 2.6× the ideal time down to 1.1× it.

This needs Settle to be long enough for a live Y update to land reliably
inside it (roughly `Settle ≳ 2 × 192 / sample_rate` seconds, using
nidaqstudio's own minimum buffering — a couple ms at the default 13000
Sa/s, taking longer only at unusually low sample rates). If Settle is too
short for that, the app automatically falls back to the same one-sweep-
per-line approach Lockin uses, correct either way — it just won't be as
fast. Practically: **raise the nidaqstudio sample rate rather than lower
it** if you want continuous mode on very short/fast lines — unlike the old
per-line approach, the continuous path never re-sends a per-sample table,
so a higher rate no longer costs more network payload, and it directly
widens how short a Settle time can safely go.

Under the hood, this app holds X/Y/Z continuously at their last commanded
voltage between sweeps (so the stage doesn't drift back to 0 V), and folds
every currently-held channel into whatever's running (an isolated sweep,
or the continuous scan acquisition) so nothing glitches mid-scan — you
don't need to think about any of this, it's just what makes "Center Stage"
and a running scan cooperate correctly.

## 3D scans

Switch Mode to 3D, give it a Z range and number of steps, and the app steps
Z through the range running a full 2D scan at each level. The live view
shows the current slice (with its Z value), and next to it a 3D render
builds up slice by slice. Each slice is written to disk as it completes,
into a folder named `3DScan_<date>_<time>` inside your save directory —
so even an aborted run keeps whatever slices finished.

## Find surface

The **Find Surface…** button sweeps Z across a range you pick while
recording a signal channel of your choice, plots signal vs Z, and marks the
peak. **Go to Maximum** then parks Z right at the peak — handy for focusing
before a scan.

## Layout

- `src/piezoscanner/core/` — the scanning logic itself, no GUI code in here.
  - `profiles.py` — the default stage profiles (PSJ, PI) used to seed the
    config file.
  - `config.py` — reads/writes the YAML hardware config.
  - `scanner.py` — the scan pattern (raster generation, line-by-line
    scanning, single-axis sweeps, image reconstruction). Talks to hardware
    only through a `backend`, never directly.
  - `backends/` — the two backend implementations behind that interface:
    `lockin_backend.py` (Lockin/simulated DAQ) and `nidaq_backend.py`
    (nidaqstudio, over its ZMQ client). Swapping backends only ever
    touches this folder plus `simulated_daq.py`.
  - `simulated_daq.py` — stands in for the real lock-in when there's no
    hardware, so the rest of the app doesn't need to know the difference.
- `src/piezoscanner/gui/` — the PyQt6 interface (control panel, plots,
  config dialog, find-surface dialog, background workers, main window).
- `lockin.py` at the repo root is just a reference copy of the real driver
  that lives in `flex` — the app imports it from there
  (`flex.inst.levylab.Lockin`), this copy isn't used directly.

## A couple of things worth knowing

- Scanning is one-directional: data is only collected on the forward
  (x_min → x_max) pass of each line, then the stage snaps back and settles
  before the next line. Collecting in both directions looked faster but
  the forward/backward misalignment put zipper artifacts in the images.
  The Lag setting now just shifts every line by the same amount (leave at
  0 unless you see a constant offset).
- Scan Up vs Scan Down picks which way the slow (Y) axis steps: up goes
  y_min → y_max, down goes y_max → y_min. Either way the image (and the
  saved files) come out in the same orientation — lines are placed at
  their true Y position, not in acquisition order.
- The PI profile's calibration (1 um/V) is a placeholder — it hasn't
  actually been measured yet. PSJ's is confirmed at 8 um/V. You can type a
  real value into the Calibration field in the GUI for a one-off session,
  or fix it permanently in the config file / config dialog once measured.
- Window size, last-used scan settings, save folder, channel setup, and
  theme all get remembered between runs.
- If the configured backend can't be reached at startup (Lockin not
  running, or nidaqstudio unreachable at its configured host/port), the
  app falls back to Simulation Mode rather than failing to launch — the
  status bar says which backend was actually requested vs. that it's
  simulated, so it's obvious when you're not talking to real hardware.
