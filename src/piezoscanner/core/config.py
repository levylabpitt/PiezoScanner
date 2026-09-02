"""Hardware configuration stored as a user-editable YAML file.

Holds which backend drives the scanner (Multichannel Lockin, nidaqstudio,
or NI-DAQmx directly), the AO channel assignments for the X/Y/Z outputs
(0 = disabled), and the set of available stage profiles. Lives at
``%LOCALAPPDATA%/Levylab/PiezoScanner/config.yaml`` and is seeded with
sensible defaults on first run, so users can either edit it by hand or go
through the in-app configuration dialog.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

import yaml

from .backends.nidaq_backend import DEFAULT_SAMPLE_RATE as NIDAQ_DEFAULT_SAMPLE_RATE
from .backends.nidaqmx_backend import DEFAULT_SAMPLE_RATE as NIDAQMX_DEFAULT_SAMPLE_RATE
from .profiles import DEFAULT_PROFILES, ScannerProfile

BackendName = Literal["lockin", "nidaqstudio", "nidaqmx"]
BACKEND_NAMES: tuple[str, ...] = ("lockin", "nidaqstudio", "nidaqmx")


@dataclass
class OutputConfig:
    """Output channel driving each axis. 0 means the axis is disabled.

    Channel numbers are interpreted by whichever backend is active: for
    Multichannel Lockin they're the instrument's own AO numbers; for
    nidaqstudio they're 1-indexed into its AO0/AO1/... sequence (channel 1
    = AO0, channel 2 = AO1, ...); for NI-DAQmx they're 1-indexed
    sequentially across the configured devices, in order.
    """

    x_channel: int = 11
    y_channel: int = 12
    z_channel: int = 0

    @property
    def z_enabled(self) -> bool:
        return self.z_channel != 0


@dataclass
class NidaqConfig:
    """Connection settings for the nidaqstudio backend. Only used when
    ``AppConfig.backend == "nidaqstudio"``."""

    host: str = "127.0.0.1"
    port: int = 8765
    sample_rate: float = NIDAQ_DEFAULT_SAMPLE_RATE


@dataclass
class NidaqmxConfig:
    """Settings for the direct NI-DAQmx backend. Only used when
    ``AppConfig.backend == "nidaqmx"``.

    ``devices`` are DAQmx device names (as NI MAX shows them, e.g.
    ``PXI1Slot2``), in the order their channels should be numbered. Cards
    not listed are left alone entirely.
    """

    devices: list[str] = field(default_factory=lambda: ["PXI1Slot2"])
    sample_rate: float = NIDAQMX_DEFAULT_SAMPLE_RATE
    ao_range: float = 10.0
    ai_range: float = 10.0
    sync: bool = True


@dataclass
class AppConfig:
    backend: BackendName = "lockin"
    outputs: OutputConfig = field(default_factory=OutputConfig)
    nidaq: NidaqConfig = field(default_factory=NidaqConfig)
    nidaqmx: NidaqmxConfig = field(default_factory=NidaqmxConfig)
    profiles: dict[str, ScannerProfile] = field(default_factory=lambda: dict(DEFAULT_PROFILES))


def config_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "Levylab", "PiezoScanner", "config.yaml")


_HEADER = """\
# FLEX PiezoScanner hardware configuration.
#
# backend: "lockin" (Levylab FLEX Multichannel Lockin), "nidaqstudio", or
#          "nidaqmx" (NI cards driven directly through NI-DAQmx -- only on
#          the PC the PXI chassis is plugged into).
# outputs: which AO channel drives each axis. 0 = disabled. A non-zero
#          z_channel is required for 3D scans and Find Surface. For the
#          nidaqstudio backend, channels are 1-indexed into its AO0/AO1/...
#          and AI0/AI1/... sequence (channel 1 = AO0/AI0, 2 = AO1/AI1, ...).
#          For nidaqmx they're 1-indexed sequentially across `devices`, in
#          the order listed (2 AO + 2 AI per 4461: 1,2 = first card, 3,4 =
#          second card, ...).
# nidaq: connection settings, only used when backend is "nidaqstudio".
# nidaqmx: device list (NI MAX names, e.g. PXI1Slot2), sample rate and
#          voltage ranges, only used when backend is "nidaqmx".
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

        backend = raw.get("backend", "lockin")
        if backend not in BACKEND_NAMES:
            backend = "lockin"

        outputs_raw = raw.get("outputs") or {}
        outputs = OutputConfig(
            x_channel=int(outputs_raw.get("x_channel", 11)),
            y_channel=int(outputs_raw.get("y_channel", 12)),
            z_channel=int(outputs_raw.get("z_channel", 0)),
        )

        nidaq_raw = raw.get("nidaq") or {}
        nidaq = NidaqConfig(
            host=str(nidaq_raw.get("host", "127.0.0.1")),
            port=int(nidaq_raw.get("port", 8765)),
            sample_rate=float(nidaq_raw.get("sample_rate", NIDAQ_DEFAULT_SAMPLE_RATE)),
        )

        mx_raw = raw.get("nidaqmx") or {}
        devices_raw = mx_raw.get("devices")
        if isinstance(devices_raw, str):
            devices_raw = [d for d in devices_raw.split(",")]
        devices = [str(d).strip() for d in (devices_raw or []) if str(d).strip()]
        nidaqmx = NidaqmxConfig(
            devices=devices or NidaqmxConfig().devices,
            sample_rate=float(mx_raw.get("sample_rate", NIDAQMX_DEFAULT_SAMPLE_RATE)),
            ao_range=float(mx_raw.get("ao_range", 10.0)),
            ai_range=float(mx_raw.get("ai_range", 10.0)),
            sync=bool(mx_raw.get("sync", True)),
        )

        profiles_raw = raw.get("profiles") or {}
        profiles = {
            str(name): _profile_from_dict(str(name), item or {})
            for name, item in profiles_raw.items()
        }
        if not profiles:
            profiles = dict(DEFAULT_PROFILES)

        return AppConfig(
            backend=backend, outputs=outputs, nidaq=nidaq, nidaqmx=nidaqmx, profiles=profiles,
        ), None
    except Exception as exc:
        return AppConfig(), f"Could not read {path} ({exc}). Using default configuration."


def save_config(config: AppConfig, path: str | None = None) -> None:
    path = path or config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)

    doc = {
        "backend": config.backend,
        "outputs": {
            "x_channel": config.outputs.x_channel,
            "y_channel": config.outputs.y_channel,
            "z_channel": config.outputs.z_channel,
        },
        "nidaq": {
            "host": config.nidaq.host,
            "port": config.nidaq.port,
            "sample_rate": config.nidaq.sample_rate,
        },
        "nidaqmx": {
            "devices": list(config.nidaqmx.devices),
            "sample_rate": config.nidaqmx.sample_rate,
            "ao_range": config.nidaqmx.ao_range,
            "ai_range": config.nidaqmx.ai_range,
            "sync": config.nidaqmx.sync,
        },
        "profiles": {name: _profile_to_dict(p) for name, p in config.profiles.items()},
    }

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_HEADER)
        yaml.safe_dump(doc, fh, sort_keys=False, allow_unicode=True)
