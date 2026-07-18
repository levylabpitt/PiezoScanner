"""Hardware configuration dialog: output channel assignments (X/Y/Z) and
the editable table of stage profiles. Writes straight back to the YAML
config file on Save."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..core.config import AppConfig, OutputConfig, config_path, save_config
from ..core.profiles import ScannerProfile

_COLUMNS = ["Name", "V min", "V max", "µm/V", "Calibrated", "Notes"]


class ConfigDialog(QDialog):
    """Modal editor for the hardware config. After ``exec()`` returns
    Accepted, the saved configuration is available as ``result_config``."""

    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Hardware Configuration")
        self.resize(680, 520)
        self.result_config: AppConfig | None = None

        layout = QVBoxLayout(self)

        # --- Outputs ---
        outputs_group = QGroupBox("Output Channels (AO)")
        form = QFormLayout(outputs_group)

        def _out_spin(value: int, tip: str) -> QSpinBox:
            spin = QSpinBox()
            spin.setRange(0, 64)
            spin.setValue(value)
            spin.setSpecialValueText("Disabled")
            spin.setToolTip(tip)
            return spin

        self.spin_x = _out_spin(config.outputs.x_channel, "AO channel driving the X (fast) axis")
        self.spin_y = _out_spin(config.outputs.y_channel, "AO channel driving the Y (slow) axis")
        self.spin_z = _out_spin(config.outputs.z_channel,
                                "AO channel driving the Z axis — required for 3D scans and Find Surface")
        form.addRow("X output:", self.spin_x)
        form.addRow("Y output:", self.spin_y)
        form.addRow("Z output:", self.spin_z)
        layout.addWidget(outputs_group)

        # --- Profiles ---
        profiles_group = QGroupBox("Stage Profiles")
        pv = QVBoxLayout(profiles_group)

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(len(_COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        pv.addWidget(self.table)

        btn_row = QHBoxLayout()
        btn_add = QPushButton("+ Add Profile")
        btn_add.clicked.connect(lambda: self._add_row())
        btn_row.addWidget(btn_add)
        btn_remove = QPushButton("Remove Selected")
        btn_remove.clicked.connect(self._remove_selected)
        btn_row.addWidget(btn_remove)
        btn_row.addStretch(1)
        pv.addLayout(btn_row)

        layout.addWidget(profiles_group, 1)

        for profile in config.profiles.values():
            self._add_row(profile)

        # --- Footer ---
        lbl_path = QLabel(f"Stored at: {config_path()}")
        lbl_path.setProperty("muted", True)
        lbl_path.setWordWrap(True)
        layout.addWidget(lbl_path)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    def _add_row(self, profile: ScannerProfile | None = None):
        row = self.table.rowCount()
        self.table.insertRow(row)

        name = profile.name if profile else f"Profile{row + 1}"
        self.table.setItem(row, 0, QTableWidgetItem(name))

        def _dspin(value: float, lo: float, hi: float, decimals: int) -> QDoubleSpinBox:
            spin = QDoubleSpinBox()
            spin.setRange(lo, hi)
            spin.setDecimals(decimals)
            spin.setValue(value)
            return spin

        self.table.setCellWidget(row, 1, _dspin(profile.vmin if profile else 0.0, -100.0, 100.0, 3))
        self.table.setCellWidget(row, 2, _dspin(profile.vmax if profile else 10.0, -100.0, 100.0, 3))
        self.table.setCellWidget(row, 3, _dspin(profile.calibration_um_per_v if profile else 1.0, 0.0001, 10000.0, 4))

        chk = QCheckBox()
        chk.setChecked(profile.calibrated if profile else False)
        self.table.setCellWidget(row, 4, chk)

        self.table.setItem(row, 5, QTableWidgetItem(profile.notes if profile else ""))

    def _remove_selected(self):
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)

    # ------------------------------------------------------------------
    def _collect(self) -> AppConfig | None:
        """Build an AppConfig from the widgets, or show what's wrong and
        return None."""
        problems: list[str] = []

        outputs = OutputConfig(
            x_channel=self.spin_x.value(),
            y_channel=self.spin_y.value(),
            z_channel=self.spin_z.value(),
        )
        nonzero = [c for c in (outputs.x_channel, outputs.y_channel, outputs.z_channel) if c != 0]
        if len(nonzero) != len(set(nonzero)):
            problems.append("X, Y and Z outputs must use different channels.")

        profiles: dict[str, ScannerProfile] = {}
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 0)
            name = name_item.text().strip() if name_item else ""
            if not name:
                problems.append(f"Row {row + 1}: profile name is empty.")
                continue
            if name in profiles:
                problems.append(f"Duplicate profile name '{name}'.")
                continue

            vmin = self.table.cellWidget(row, 1).value()
            vmax = self.table.cellWidget(row, 2).value()
            cal = self.table.cellWidget(row, 3).value()
            calibrated = self.table.cellWidget(row, 4).isChecked()
            notes_item = self.table.item(row, 5)
            notes = notes_item.text().strip() if notes_item else ""

            if vmin >= vmax:
                problems.append(f"Profile '{name}': V min must be below V max.")
                continue

            profiles[name] = ScannerProfile(
                name=name, vmin=vmin, vmax=vmax,
                calibration_um_per_v=cal, calibrated=calibrated, notes=notes,
            )

        if not profiles:
            problems.append("At least one profile is required.")

        if problems:
            QMessageBox.warning(self, "Invalid Configuration", "\n".join(problems))
            return None

        return AppConfig(outputs=outputs, profiles=profiles)

    def _on_save(self):
        config = self._collect()
        if config is None:
            return
        try:
            save_config(config)
        except OSError as exc:
            QMessageBox.critical(self, "Save Error", f"Could not write config file: {exc}")
            return
        self.result_config = config
        self.accept()
