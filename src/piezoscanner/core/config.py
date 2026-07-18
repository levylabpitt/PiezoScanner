"""Hardware configuration stored as a user-editable YAML file.

Holds the AO channel assignments for the X/Y/Z outputs (0 = disabled) and
the set of available stage profiles. Lives at
``%LOCALAPPDATA%/Levylab/PiezoScanner/config.yaml`` and is seeded with
sensible defaults on first run, so users can either edit it by hand or go
through the in-app configuration dialog.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

from .profiles import DEFAULT_PROFILES, ScannerProfile


@dataclass
class OutputConfig:
    """AO channel driving each axis. 0 means the axis is disabled."""

    x_channel: int = 11
    y_channel: int = 12
    z_channel: int = 0

    @property
    def z_enabled(self) -> bool:
        return self.z_channel != 0


@dataclass
class AppConfig:
    outputs: OutputConfig = field(default_factory=OutputConfig)
    profiles: dict[str, ScannerProfile] = field(default_factory=lambda: dict(DEFAULT_PROFILES))


def config_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "Levylab", "PiezoScanner", "config.yaml")


_HEADER = """\
# FLEX PiezoScanner hardware configuration.
#
# outputs: which lock-in AO channel drives each axis. 0 = disabled.
#          A non-zero z_channel is required for 3D scans and Find Surface.
# profiles: safe voltage range and um/V calibration per stage.
#           calibrated: false shows a warning in the app until you've
#           measured and entered a real calibration value.
"""


def _profile_from_dict(name: str, raw: dict) -> ScannerProfile:
    return ScannerProfile(
        name=name,
        vmin=float(raw["vmin"]),
        vmax=float(raw["vmax"]),
        calibration_um_per_v=float(raw.get("calibration_um_per_v", 1.0)),
        calibrated=bool(raw.get("calibrated", True)),
        notes=str(raw.get("notes", "")),
    )


def _profile_to_dict(profile: ScannerProfile) -> dict:
    return {
        "vmin": profile.vmin,
        "vmax": profile.vmax,
        "calibration_um_per_v": profile.calibration_um_per_v,
        "calibrated": profile.calibrated,
        "notes": profile.notes,
    }


def load_config(path: str | None = None) -> tuple[AppConfig, str | None]:
    """Load the config file, creating it with defaults if missing.

    Returns ``(config, error_message)``; on a parse/read error the defaults
    are returned along with a human-readable description of what went wrong,
    so the app can start anyway and tell the user.
    """
    path = path or config_path()

    if not os.path.exists(path):
        config = AppConfig()
        try:
            save_config(config, path)
        except OSError:
            pass  # read-only location; run on defaults without persisting
        return config, None

    try:
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

        outputs_raw = raw.get("outputs") or {}
        outputs = OutputConfig(
            x_channel=int(outputs_raw.get("x_channel", 11)),
            y_channel=int(outputs_raw.get("y_channel", 12)),
            z_channel=int(outputs_raw.get("z_channel", 0)),
        )

        profiles_raw = raw.get("profiles") or {}
        profiles = {
            str(name): _profile_from_dict(str(name), item or {})
            for name, item in profiles_raw.items()
        }
        if not profiles:
            profiles = dict(DEFAULT_PROFILES)

        return AppConfig(outputs=outputs, profiles=profiles), None
    except Exception as exc:
        return AppConfig(), f"Could not read {path} ({exc}). Using default configuration."


def save_config(config: AppConfig, path: str | None = None) -> None:
    path = path or config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    doc = {
        "outputs": {
            "x_channel": config.outputs.x_channel,
            "y_channel": config.outputs.y_channel,
            "z_channel": config.outputs.z_channel,
        },
        "profiles": {name: _profile_to_dict(p) for name, p in config.profiles.items()},
    }

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_HEADER)
        yaml.safe_dump(doc, fh, sort_keys=False, allow_unicode=True)
