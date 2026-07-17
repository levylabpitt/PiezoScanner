# PiezoScanner

PyQt6 app for driving a piezo scan stage through the Levylab FLEX Lockin and
grabbing raster-scan images off up to 3 detector channels.

This replaces the old `app.py` / `piezoscanner.py` scripts. Same basic idea,
but rewritten as a proper installable package: scanning now runs on its own
thread so the UI doesn't lock up mid-scan, you can add/remove input channels
from the GUI instead of editing code, and a couple of bugs from the old
version are fixed (centering used to jump to a hardcoded 5V no matter what
profile/range you had set).

## Requirements

- Python 3.10+
- PyQt6, numpy, matplotlib — these get installed automatically
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

With `uv`:

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

## Layout

- `src/piezoscanner/core/` — the scanning logic itself, no GUI code in here.
  - `profiles.py` — stage profiles (PSJ, PI), their safe voltage range, and
    the um/V calibration for each.
  - `scanner.py` — talks to the DAQ, generates the raster, reconstructs the
    image from detector data.
  - `simulated_daq.py` — stands in for the real lock-in when there's no
    hardware, so the rest of the app doesn't need to know the difference.
- `src/piezoscanner/gui/` — the PyQt6 interface (control panel, plots,
  background scan worker, main window).
- `lockin.py` at the repo root is just a reference copy of the real driver
  that lives in `flex` — the app imports it from there
  (`flex.inst.levylab.Lockin`), this copy isn't used directly.

## A couple of things worth knowing

- The PI profile's calibration (1 um/V) is a placeholder — it hasn't
  actually been measured yet. PSJ's is confirmed at 8 um/V. You can type a
  real value into the Calibration field in the GUI for a one-off session,
  or edit it permanently in `src/piezoscanner/core/profiles.py` once you've
  measured it.
- Window size, last-used scan settings, save folder, channel setup, and
  theme all get remembered between runs.
