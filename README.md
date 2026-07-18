# FLEX PiezoScanner

PyQt6 app for driving a piezo scan stage through the Levylab FLEX Lockin and
grabbing raster-scan images off up to 3 detector channels. Supports plain 2D
scans and 3D stacks (a 2D scan at each Z level), plus a find-surface tool
for locating the sample along Z.

This replaces the old `app.py` / `piezoscanner.py` scripts. Same basic idea,
but rewritten as a proper installable package: scanning runs on its own
thread so the UI doesn't lock up mid-scan, channels and hardware wiring are
configured in the GUI instead of by editing code, and a couple of bugs from
the old version are fixed (centering used to jump to a hardcoded 5V no
matter what profile/range you had set).

## Requirements

- Python 3.10+
- PyQt6, numpy, matplotlib, PyYAML — these get installed automatically
- `flex` (the Levylab FLEX framework), if you want to talk to real hardware.
  It's an internal package, not on PyPI, so you install it separately into
  the same environment. Without it the app just starts in Simulation Mode —
  useful for checking the UI works before you're next to the actual stage.

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

Output wiring and stage profiles live in a YAML file at
`%LOCALAPPDATA%\Levylab\PiezoScanner\config.yaml`, created with defaults on
first run. Edit it by hand or use **Settings → Configure Hardware…** in the
app — same thing. It holds:

- which AO channel drives X, Y, and Z (set a channel to 0 to disable that
  axis — Z is off by default, and 3D mode / Find Surface only appear once
  you give Z a channel)
- the list of stage profiles: safe voltage range, um/V calibration, and a
  calibrated yes/no flag (uncalibrated profiles show a warning in the app)

Adding a new stage is just adding a few lines to that file.

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
  - `scanner.py` — talks to the DAQ: raster generation, line-by-line
    scanning, single-axis sweeps, image reconstruction.
  - `simulated_daq.py` — stands in for the real lock-in when there's no
    hardware, so the rest of the app doesn't need to know the difference.
- `src/piezoscanner/gui/` — the PyQt6 interface (control panel, plots,
  config dialog, find-surface dialog, background workers, main window).
- `lockin.py` at the repo root is just a reference copy of the real driver
  that lives in `flex` — the app imports it from there
  (`flex.inst.levylab.Lockin`), this copy isn't used directly.

## A couple of things worth knowing

- The PI profile's calibration (1 um/V) is a placeholder — it hasn't
  actually been measured yet. PSJ's is confirmed at 8 um/V. You can type a
  real value into the Calibration field in the GUI for a one-off session,
  or fix it permanently in the config file / config dialog once measured.
- Window size, last-used scan settings, save folder, channel setup, and
  theme all get remembered between runs.
