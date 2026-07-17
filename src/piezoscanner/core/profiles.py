"""Stage profiles: voltage range and voltage->distance calibration.

Each profile describes the safe output-voltage window for a given piezo
stage/amplifier combination, plus the linear calibration factor that
converts drive voltage to physical displacement (micrometers per volt).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ScannerProfile:
    """Immutable description of a piezo stage/amplifier configuration."""

    name: str
    vmin: float
    vmax: float
    calibration_um_per_v: float
    calibrated: bool = True
    notes: str = ""

    @property
    def span_v(self) -> float:
        return self.vmax - self.vmin

    @property
    def span_um(self) -> float:
        return self.span_v * self.calibration_um_per_v

    def clip_voltage(self, value: float) -> float:
        """Clamp a requested voltage to this profile's safe range."""
        return min(max(value, self.vmin), self.vmax)

    def volts_to_um(self, volts: float) -> float:
        return volts * self.calibration_um_per_v

    def um_to_volts(self, um: float) -> float:
        return um / self.calibration_um_per_v


PROFILES: dict[str, ScannerProfile] = {
    "PSJ": ScannerProfile(
        name="PSJ",
        vmin=0.0,
        vmax=10.0,
        calibration_um_per_v=8.0,
        calibrated=True,
        notes="80 um travel over 10 V (8 um/V), calibrated.",
    ),
    "PI": ScannerProfile(
        name="PI",
        vmin=-2.0,
        vmax=12.0,
        calibration_um_per_v=1.0,
        calibrated=False,
        notes="Placeholder calibration (1 um/V) — stage not yet calibrated. "
              "Verify before trusting reported micrometer positions.",
    ),
}

DEFAULT_PROFILE = "PSJ"
