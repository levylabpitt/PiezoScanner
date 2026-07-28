"""Left-hand control panel: scan configuration (2D/3D), profile and
calibration, dynamic input-channel manager, save location, stage
navigation, and the main scan/pause/abort controls.

Output (AO) channel assignments live in the hardware configuration dialog,
not here — this panel only needs to know whether a Z axis exists so it can
offer 3D mode.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.profiles import ScannerProfile

MAX_CHANNELS = 3
COLORMAPS = ["viridis", "plasma", "inferno", "magma", "cividis", "turbo", "jet", "gray", "hot", "coolwarm", "RdBu_r"]
_DEFAULT_CHANNEL_NUMBERS = [8, 9, 10]


@dataclass(frozen=True)
class ChannelSlot:
    number: int
    label: str
    colormap: str
    enabled: bool


class ChannelRow(QWidget):
    """One row in the channel manager: enable checkbox, AI channel number,
    display label, colormap, remove button."""

    changed = pyqtSignal()
    remove_requested = pyqtSignal(QWidget)

    def __init__(self, index: int, number: int, parent=None):
        super().__init__(parent)
        self._index = index

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.chk_enabled = QCheckBox()
        self.chk_enabled.setChecked(True)
        self.chk_enabled.setToolTip("Include this channel in the next scan and show its plot tab")
        self.chk_enabled.toggled.connect(self.changed.emit)

        self.spin_channel = QSpinBox()
        self.spin_channel.setRange(1, 64)
        self.spin_channel.setValue(number)
        self.spin_channel.setPrefix("AI ")
        self.spin_channel.setToolTip("Detector (analog input) channel number")
        self.spin_channel.valueChanged.connect(self.changed.emit)

        self.edit_label = QLineEdit(f"Channel {index + 1}")
        self.edit_label.setToolTip("Display name shown on the plot tab")
        self.edit_label.setMaximumWidth(110)
        self.edit_label.textChanged.connect(self.changed.emit)

        self.combo_colormap = QComboBox()
        self.combo_colormap.addItems(COLORMAPS)
        self.combo_colormap.setCurrentText(COLORMAPS[index % len(COLORMAPS)])
        self.combo_colormap.setToolTip("Colormap used for this channel's image")
        self.combo_colormap.currentTextChanged.connect(self.changed.emit)

        self.btn_remove = QPushButton("✕")
        self.btn_remove.setObjectName("chipButton")
        self.btn_remove.setFixedWidth(28)
        self.btn_remove.setToolTip("Remove this channel")
        self.btn_remove.clicked.connect(lambda: self.remove_requested.emit(self))

        layout.addWidget(self.chk_enabled)
        layout.addWidget(self.spin_channel)
        layout.addWidget(self.edit_label, 1)
        layout.addWidget(self.combo_colormap)
        layout.addWidget(self.btn_remove)

    def to_slot(self) -> ChannelSlot:
        return ChannelSlot(
            number=self.spin_channel.value(),
            label=self.edit_label.text().strip() or f"Channel {self._index + 1}",
            colormap=self.combo_colormap.currentText(),
            enabled=self.chk_enabled.isChecked(),
        )


class ControlPanel(QWidget):
    # --- action signals consumed by MainWindow ---
    start_scan_requested = pyqtSignal(bool)  # slow_axis_down
    abort_scan_requested = pyqtSignal()
    pause_toggled = pyqtSignal(bool)
    center_stage_requested = pyqtSignal()
    full_range_requested = pyqtSignal()
    move_to_requested = pyqtSignal(float, float)
    browse_directory_requested = pyqtSignal(str)
    profile_changed = pyqtSignal(str)
    channels_changed = pyqtSignal()
    find_surface_requested = pyqtSignal()

    def __init__(self, profiles: dict[str, ScannerProfile], z_available: bool = False, parent=None):
        super().__init__(parent)
        self._profiles: dict[str, ScannerProfile] = dict(profiles)
        self._channel_rows: list[ChannelRow] = []
        self._is_scanning = False
        self._z_available = False
        self.save_directory = os.getcwd()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)

        body = QWidget()
        scroll.setWidget(body)
        self._layout = QVBoxLayout(body)
        self._layout.setSpacing(8)

        self._build_scan_config_group()
        self._build_profile_group()
        self._build_channels_group()
        self._build_save_group()
        self._build_navigation_group()
        self._layout.addStretch(1)
        self._build_scan_control_group()  # pinned below the scroll area

        outer.addWidget(self._scan_control_group)

        self.set_z_available(z_available)
        self._update_scan_size_readout()
        self._update_calibration_state()

    # ------------------------------------------------------------------
    # Scan configuration (dense grid: one row per axis)
    # ------------------------------------------------------------------
    def _build_scan_config_group(self):
        group = QGroupBox("Scan")
        grid = QGridLayout(group)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        # Row 0: mode + line time
        grid.addWidget(QLabel("Mode"), 0, 0)
        self.combo_mode = QComboBox()
        self.combo_mode.addItems(["2D", "3D"])
        self.combo_mode.setToolTip("3D steps the Z output through its range, running one 2D scan per level")
        self.combo_mode.currentTextChanged.connect(self._sync_z_enabled)
        grid.addWidget(self.combo_mode, 0, 1)

        grid.addWidget(QLabel("Line time"), 0, 3)
        self.spin_line_time = QDoubleSpinBox()
        self.spin_line_time.setRange(0.01, 3600.0)
        self.spin_line_time.setDecimals(2)
        self.spin_line_time.setSuffix(" s")
        self.spin_line_time.setValue(4.0)
        grid.addWidget(self.spin_line_time, 0, 4)

        # Row 1: pixels + lag
        grid.addWidget(QLabel("Pixels X×Y"), 1, 0)
        self.spin_x_points = QSpinBox()
        self.spin_x_points.setRange(2, 4096)
        self.spin_x_points.setValue(50)
        grid.addWidget(self.spin_x_points, 1, 1)
        self.spin_y_points = QSpinBox()
        self.spin_y_points.setRange(2, 4096)
        self.spin_y_points.setValue(50)
        grid.addWidget(self.spin_y_points, 1, 2)

        grid.addWidget(QLabel("Lag"), 1, 3)
        self.spin_delay = QSpinBox()
        self.spin_delay.setRange(-1000, 1000)
        self.spin_delay.setValue(0)
        self.spin_delay.setToolTip("Samples to shift every line by, to correct constant acquisition lag (usually 0)")
        grid.addWidget(self.spin_delay, 1, 4)

        # Rows 2-4: axis ranges, one row each
        def _v_spin(default: float) -> QDoubleSpinBox:
            spin = QDoubleSpinBox()
            spin.setRange(-100.0, 100.0)
            spin.setDecimals(3)
            spin.setSuffix(" V")
            spin.setValue(default)
            spin.valueChanged.connect(self._update_scan_size_readout)
            return spin

        grid.addWidget(QLabel("X range"), 2, 0)
        self.spin_x_min = _v_spin(0.0)
        self.spin_x_max = _v_spin(10.0)
        grid.addWidget(self.spin_x_min, 2, 1)
        grid.addWidget(self.spin_x_max, 2, 2)

        grid.addWidget(QLabel("Y range"), 3, 0)
        self.spin_y_min = _v_spin(0.0)
        self.spin_y_max = _v_spin(10.0)
        grid.addWidget(self.spin_y_min, 3, 1)
        grid.addWidget(self.spin_y_max, 3, 2)

        self._lbl_z_range = QLabel("Z range")
        grid.addWidget(self._lbl_z_range, 4, 0)
        self.spin_z_min = _v_spin(0.0)
        self.spin_z_max = _v_spin(10.0)
        grid.addWidget(self.spin_z_min, 4, 1)
        grid.addWidget(self.spin_z_max, 4, 2)
        self._lbl_z_steps = QLabel("Steps")
        grid.addWidget(self._lbl_z_steps, 4, 3)
        self.spin_z_steps = QSpinBox()
        self.spin_z_steps.setRange(2, 500)
        self.spin_z_steps.setValue(10)
        self.spin_z_steps.setToolTip("Number of Z levels (one 2D scan per level)")
        grid.addWidget(self.spin_z_steps, 4, 4)
        self._z_widgets = (
            self._lbl_z_range, self.spin_z_min, self.spin_z_max,
            self._lbl_z_steps, self.spin_z_steps,
        )

        # Row 5: physical size readout
        self.lbl_scan_size = QLabel("")
        self.lbl_scan_size.setProperty("muted", True)
        grid.addWidget(self.lbl_scan_size, 5, 0, 1, 5)

        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        grid.setColumnStretch(4, 1)
        self._layout.addWidget(group)

    def _update_scan_size_readout(self, *_):
        cal = self._current_calibration()
        w_um = abs(self.spin_x_max.value() - self.spin_x_min.value()) * cal
        h_um = abs(self.spin_y_max.value() - self.spin_y_min.value()) * cal
        self.lbl_scan_size.setText(f"{w_um:.2f} × {h_um:.2f} um  (at {cal:g} um/V)")

    # ------------------------------------------------------------------
    # 2D/3D mode availability
    # ------------------------------------------------------------------
    def set_z_available(self, available: bool):
        """Enable/disable 3D mode based on whether a Z output channel is
        configured. The Z row is hidden entirely when unavailable."""
        self._z_available = available
        model = self.combo_mode.model()
        item = model.item(1)
        if item is not None:
            item.setEnabled(available)
        if not available and self.combo_mode.currentIndex() == 1:
            self.combo_mode.setCurrentIndex(0)
        for widget in self._z_widgets:
            widget.setVisible(available)
        self._sync_z_enabled()

    def _sync_z_enabled(self, *_):
        is_3d = self._z_available and self.combo_mode.currentText() == "3D"
        for widget in (self.spin_z_min, self.spin_z_max, self.spin_z_steps):
            widget.setEnabled(is_3d)

    # ------------------------------------------------------------------
    # Profile & calibration
    # ------------------------------------------------------------------
    def _build_profile_group(self):
        group = QGroupBox("Profile && Calibration")
        grid = QGridLayout(group)
        grid.setHorizontalSpacing(8)

        grid.addWidget(QLabel("Profile"), 0, 0)
        self.combo_profile = QComboBox()
        self.combo_profile.addItems(list(self._profiles.keys()))
        self.combo_profile.currentTextChanged.connect(self._on_profile_changed)
        grid.addWidget(self.combo_profile, 0, 1)

        grid.addWidget(QLabel("Calibration"), 0, 2)
        self.spin_calibration = QDoubleSpinBox()
        self.spin_calibration.setRange(0.001, 10000.0)
        self.spin_calibration.setDecimals(4)
        self.spin_calibration.setSuffix(" um/V")
        self.spin_calibration.valueChanged.connect(self._update_scan_size_readout)
        grid.addWidget(self.spin_calibration, 0, 3)

        self.lbl_uncalibrated = QLabel()
        self.lbl_uncalibrated.setWordWrap(True)
        self.lbl_uncalibrated.setStyleSheet("color: #d9a441; font-style: italic;")
        grid.addWidget(self.lbl_uncalibrated, 1, 0, 1, 4)

        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        self._layout.addWidget(group)
        self._on_profile_changed(self.combo_profile.currentText())

    def set_profiles(self, profiles: dict[str, ScannerProfile]):
        """Replace the available profiles (after a config change),
        preserving the current selection when it still exists."""
        self._profiles = dict(profiles)
        current = self.combo_profile.currentText()
        self.combo_profile.blockSignals(True)
        self.combo_profile.clear()
        self.combo_profile.addItems(list(self._profiles.keys()))
        if current in self._profiles:
            self.combo_profile.setCurrentText(current)
        self.combo_profile.blockSignals(False)
        self._on_profile_changed(self.combo_profile.currentText())

    def _on_profile_changed(self, name: str):
        profile = self._profiles.get(name)
        if profile is None:
            return
        self.spin_calibration.blockSignals(True)
        self.spin_calibration.setValue(profile.calibration_um_per_v)
        self.spin_calibration.blockSignals(False)
        self._update_calibration_state()
        self._update_scan_size_readout()
        self.profile_changed.emit(name)

    def _update_calibration_state(self):
        profile = self._profiles.get(self.combo_profile.currentText())
        if profile is not None and not profile.calibrated:
            self.lbl_uncalibrated.setText(f"⚠ Uncalibrated profile — {profile.notes}")
            self.lbl_uncalibrated.setVisible(True)
        else:
            self.lbl_uncalibrated.setVisible(False)

    def _current_calibration(self) -> float:
        return self.spin_calibration.value()

    def current_profile_name(self) -> str:
        return self.combo_profile.currentText()

    # ------------------------------------------------------------------
    # Channel manager
    # ------------------------------------------------------------------
    def _build_channels_group(self):
        group = QGroupBox(f"Input Channels (up to {MAX_CHANNELS})")
        self._channels_layout = QVBoxLayout(group)
        self._channels_layout.setSpacing(6)

        self._rows_container = QVBoxLayout()
        self._rows_container.setSpacing(4)
        self._channels_layout.addLayout(self._rows_container)

        self.btn_add_channel = QPushButton("+ Add Channel")
        # Wrapped in a lambda: QPushButton.clicked emits a `checked` bool that
        # would otherwise land in _add_channel_row's optional `slot` parameter.
        self.btn_add_channel.clicked.connect(lambda: self._add_channel_row())
        self._channels_layout.addWidget(self.btn_add_channel)

        self.lbl_no_channels = QLabel("No channels added — add at least one before scanning.")
        self.lbl_no_channels.setProperty("muted", True)
        self.lbl_no_channels.setWordWrap(True)
        self._channels_layout.addWidget(self.lbl_no_channels)

        self._layout.addWidget(group)

        # Start with one channel enabled by default so the app is usable immediately.
        self._add_channel_row()

    def _add_channel_row(self, slot: ChannelSlot | None = None):
        if len(self._channel_rows) >= MAX_CHANNELS:
            return
        index = len(self._channel_rows)
        number = slot.number if slot else _DEFAULT_CHANNEL_NUMBERS[index % len(_DEFAULT_CHANNEL_NUMBERS)]
        row = ChannelRow(index, number)
        if slot is not None:
            row.edit_label.setText(slot.label)
            row.combo_colormap.setCurrentText(slot.colormap)
            row.chk_enabled.setChecked(slot.enabled)
        row.changed.connect(self.channels_changed.emit)
        row.remove_requested.connect(self._remove_channel_row)
        self._rows_container.addWidget(row)
        self._channel_rows.append(row)
        self._sync_channel_ui()
        self.channels_changed.emit()

    def clear_channels(self):
        for row in list(self._channel_rows):
            self._channel_rows.remove(row)
            self._rows_container.removeWidget(row)
            row.deleteLater()
        self._sync_channel_ui()
        self.channels_changed.emit()

    def restore_channels(self, slots: list[ChannelSlot]):
        self.clear_channels()
        for slot in slots[:MAX_CHANNELS]:
            self._add_channel_row(slot)

    def _remove_channel_row(self, row: ChannelRow):
        if row in self._channel_rows:
            self._channel_rows.remove(row)
            self._rows_container.removeWidget(row)
            row.deleteLater()
            self._sync_channel_ui()
            self.channels_changed.emit()

    def _sync_channel_ui(self):
        self.btn_add_channel.setEnabled(len(self._channel_rows) < MAX_CHANNELS)
        self.lbl_no_channels.setVisible(len(self._channel_rows) == 0)

    def channel_slots(self) -> list[ChannelSlot]:
        return [row.to_slot() for row in self._channel_rows]

    def enabled_channel_slots(self) -> list[ChannelSlot]:
        return [slot for slot in self.channel_slots() if slot.enabled]

    # ------------------------------------------------------------------
    # Save location
    # ------------------------------------------------------------------
    def _build_save_group(self):
        group = QGroupBox("Save Location")
        layout = QHBoxLayout(group)

        self.btn_browse = QPushButton("Browse...")
        self.btn_browse.clicked.connect(self.browse_directory)
        layout.addWidget(self.btn_browse)

        self.lbl_directory = QLabel()
        self.lbl_directory.setProperty("muted", True)
        self.lbl_directory.setWordWrap(True)
        layout.addWidget(self.lbl_directory, 1)

        self._layout.addWidget(group)
        self._update_directory_label()

    def browse_directory(self):
        path = QFileDialog.getExistingDirectory(self, "Select Save Directory", self.save_directory or "")
        if path:
            self.set_save_directory(path)

    def set_save_directory(self, path: str):
        self.save_directory = path
        self._update_directory_label()
        self.browse_directory_requested.emit(path)

    def _update_directory_label(self):
        path = self.save_directory
        shown = path if len(path) <= 40 else f"...{path[-37:]}"
        self.lbl_directory.setText(shown)

    # ------------------------------------------------------------------
    # Navigation (stage moves + find surface)
    # ------------------------------------------------------------------
    def _build_navigation_group(self):
        group = QGroupBox("Navigation")
        layout = QVBoxLayout(group)
        layout.setSpacing(6)

        row1 = QHBoxLayout()
        self.btn_center = QPushButton("Center Stage")
        self.btn_center.setToolTip("Move to the midpoint of the active profile's safe voltage range")
        self.btn_center.clicked.connect(self.center_stage_requested.emit)
        row1.addWidget(self.btn_center)

        self.btn_full_range = QPushButton("Full Range")
        self.btn_full_range.setToolTip("Reset scan ranges to the active profile's safe voltage range")
        self.btn_full_range.clicked.connect(self.full_range_requested.emit)
        row1.addWidget(self.btn_full_range)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Go to"))
        self.spin_move_x = QDoubleSpinBox()
        self.spin_move_y = QDoubleSpinBox()
        for spin in (self.spin_move_x, self.spin_move_y):
            spin.setRange(-100.0, 100.0)
            spin.setDecimals(3)
            spin.setSuffix(" V")
            row2.addWidget(spin)
        self.btn_move = QPushButton("Move")
        self.btn_move.clicked.connect(
            lambda: self.move_to_requested.emit(self.spin_move_x.value(), self.spin_move_y.value())
        )
        row2.addWidget(self.btn_move)
        layout.addLayout(row2)

        self.btn_find_surface = QPushButton("Find Surface…")
        self.btn_find_surface.setToolTip("Sweep the Z output while recording a signal channel to locate the surface")
        self.btn_find_surface.clicked.connect(self.find_surface_requested.emit)
        layout.addWidget(self.btn_find_surface)

        self._layout.addWidget(group)

    def set_full_range(self, vmin: float, vmax: float):
        self.set_range(vmin, vmax, vmin, vmax)
        if self._z_available:
            self.spin_z_min.setValue(vmin)
            self.spin_z_max.setValue(vmax)

    def set_range(self, x_min: float, x_max: float, y_min: float, y_max: float):
        self.spin_x_min.setValue(x_min)
        self.spin_x_max.setValue(x_max)
        self.spin_y_min.setValue(y_min)
        self.spin_y_max.setValue(y_max)

    def is_scanning(self) -> bool:
        return self._is_scanning

    # ------------------------------------------------------------------
    # Scan control (start/abort/pause + progress)
    # ------------------------------------------------------------------
    def _build_scan_control_group(self):
        self._scan_control_group = QWidget()
        layout = QVBoxLayout(self._scan_control_group)
        layout.setContentsMargins(0, 8, 0, 0)

        row = QHBoxLayout()
        self.btn_scan_up = QPushButton("▲ SCAN UP")
        self.btn_scan_up.setObjectName("primaryAction")
        self.btn_scan_up.setMinimumHeight(38)
        self.btn_scan_up.setToolTip("Scan with the slow axis stepping from Y min up to Y max")
        self.btn_scan_up.clicked.connect(lambda: self.start_scan_requested.emit(False))
        row.addWidget(self.btn_scan_up, 3)

        self.btn_scan_down = QPushButton("▼ SCAN DOWN")
        self.btn_scan_down.setObjectName("primaryAction")
        self.btn_scan_down.setMinimumHeight(38)
        self.btn_scan_down.setToolTip("Scan with the slow axis stepping from Y max down to Y min")
        self.btn_scan_down.clicked.connect(lambda: self.start_scan_requested.emit(True))
        row.addWidget(self.btn_scan_down, 3)

        self.btn_abort = QPushButton("ABORT SCAN")
        self.btn_abort.setObjectName("dangerAction")
        self.btn_abort.setMinimumHeight(38)
        self.btn_abort.setVisible(False)
        self.btn_abort.clicked.connect(self.abort_scan_requested.emit)
        row.addWidget(self.btn_abort, 6)

        self.chk_pause = QCheckBox("Pause")
        self.chk_pause.setEnabled(False)
        self.chk_pause.toggled.connect(self.pause_toggled.emit)
        row.addWidget(self.chk_pause, 1)
        layout.addLayout(row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        layout.addWidget(self.progress_bar)

        status_row = QHBoxLayout()
        self.lbl_status = QLabel("Status: Idle")
        self.lbl_status.setStyleSheet("font-weight: 600;")
        status_row.addWidget(self.lbl_status, 1)
        self.lbl_eta = QLabel("")
        self.lbl_eta.setProperty("muted", True)
        status_row.addWidget(self.lbl_eta)
        layout.addLayout(status_row)

    def set_scanning_state(self, scanning: bool):
        self._is_scanning = scanning
        self.btn_scan_up.setVisible(not scanning)
        self.btn_scan_down.setVisible(not scanning)
        self.btn_abort.setVisible(scanning)
        self.chk_pause.setEnabled(scanning)
        if not scanning:
            self.chk_pause.setChecked(False)
        self._set_inputs_enabled(not scanning)

    def _set_inputs_enabled(self, enabled: bool):
        # Navigation controls (center/full-range/move/find-surface) are locked
        # too: repositioning the stage mid-sweep would corrupt the running scan.
        for widget in (
            self.combo_mode,
            self.spin_x_points, self.spin_y_points, self.spin_line_time,
            self.spin_x_min, self.spin_x_max, self.spin_y_min, self.spin_y_max,
            self.spin_delay, self.combo_profile, self.spin_calibration,
            self.btn_add_channel,
            self.btn_center, self.btn_full_range,
            self.spin_move_x, self.spin_move_y, self.btn_move,
            self.btn_find_surface,
        ):
            widget.setEnabled(enabled)
        for row in self._channel_rows:
            row.setEnabled(enabled)
        if enabled:
            self._sync_z_enabled()
        else:
            for widget in (self.spin_z_min, self.spin_z_max, self.spin_z_steps):
                widget.setEnabled(False)

    def set_status(self, text: str):
        self.lbl_status.setText(f"Status: {text}")

    def set_progress(self, percent: int):
        self.progress_bar.setValue(percent)

    def set_eta(self, text: str):
        self.lbl_eta.setText(text)

    # ------------------------------------------------------------------
    # Scan configuration for MainWindow
    # ------------------------------------------------------------------
    def get_scan_config(self) -> dict:
        return dict(
            mode=self.combo_mode.currentText(),
            x_points=self.spin_x_points.value(),
            y_points=self.spin_y_points.value(),
            line_time=self.spin_line_time.value(),
            x_min=self.spin_x_min.value(),
            x_max=self.spin_x_max.value(),
            y_min=self.spin_y_min.value(),
            y_max=self.spin_y_max.value(),
            z_min=self.spin_z_min.value(),
            z_max=self.spin_z_max.value(),
            z_steps=self.spin_z_steps.value(),
            delay_samples=self.spin_delay.value(),
            profile=self.combo_profile.currentText(),
            calibration_um_per_v=self.spin_calibration.value(),
        )

    def set_scan_config(self, cfg: dict) -> None:
        """Restore a previously-saved configuration (e.g. from QSettings).
        Unknown/missing keys are ignored so partial configs are safe."""
        mapping = {
            "x_points": self.spin_x_points,
            "y_points": self.spin_y_points,
            "line_time": self.spin_line_time,
            "x_min": self.spin_x_min,
            "x_max": self.spin_x_max,
            "y_min": self.spin_y_min,
            "y_max": self.spin_y_max,
            "z_min": self.spin_z_min,
            "z_max": self.spin_z_max,
            "z_steps": self.spin_z_steps,
            "delay_samples": self.spin_delay,
        }
        for key, widget in mapping.items():
            if key in cfg and cfg[key] is not None:
                widget.setValue(cfg[key])

        mode = cfg.get("mode")
        if mode == "3D" and self._z_available:
            self.combo_mode.setCurrentText("3D")
        elif mode == "2D":
            self.combo_mode.setCurrentText("2D")

        if cfg.get("profile") in self._profiles:
            self.combo_profile.setCurrentText(cfg["profile"])
        if cfg.get("calibration_um_per_v") is not None:
            self.spin_calibration.setValue(float(cfg["calibration_um_per_v"]))
