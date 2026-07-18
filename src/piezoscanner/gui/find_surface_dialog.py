"""Find Surface: sweep the Z output while recording a signal channel,
plot signal vs Z, and optionally park Z at the maximum."""

from __future__ import annotations

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from PyQt6.QtCore import QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ..core.scanner import PiezoScanner
from . import theme
from .scan_worker import AxisSweepWorker, QuickCommand


class FindSurfaceDialog(QDialog):
    """Modal Z-sweep tool. Emits ``z_moved`` when Go to Maximum parks Z."""

    z_moved = pyqtSignal(float)

    def __init__(self, scanner: PiezoScanner, z_channel: int, dark: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Find Surface — Z Sweep")
        self.resize(640, 520)

        self.scanner = scanner
        self.z_channel = z_channel
        self._dark = dark
        self._worker: AxisSweepWorker | None = None
        self._z: np.ndarray | None = None
        self._signal: np.ndarray | None = None

        layout = QVBoxLayout(self)

        profile = scanner.profile
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)

        grid.addWidget(QLabel("Signal channel"), 0, 0)
        self.spin_signal = QSpinBox()
        self.spin_signal.setRange(1, 64)
        self.spin_signal.setPrefix("AI ")
        self.spin_signal.setValue(8)
        self.spin_signal.setToolTip("Detector (analog input) channel to record during the sweep")
        grid.addWidget(self.spin_signal, 0, 1)

        grid.addWidget(QLabel("Points"), 0, 2)
        self.spin_points = QSpinBox()
        self.spin_points.setRange(10, 5000)
        self.spin_points.setValue(200)
        grid.addWidget(self.spin_points, 0, 3)

        grid.addWidget(QLabel("Z range"), 1, 0)
        self.spin_z_min = QDoubleSpinBox()
        self.spin_z_max = QDoubleSpinBox()
        for spin, default in ((self.spin_z_min, profile.vmin), (self.spin_z_max, profile.vmax)):
            spin.setRange(-100.0, 100.0)
            spin.setDecimals(3)
            spin.setSuffix(" V")
            spin.setValue(default)
        grid.addWidget(self.spin_z_min, 1, 1)
        grid.addWidget(self.spin_z_max, 1, 2)

        grid.addWidget(QLabel("Sweep time"), 1, 3)
        self.spin_time = QDoubleSpinBox()
        self.spin_time.setRange(0.1, 600.0)
        self.spin_time.setDecimals(1)
        self.spin_time.setSuffix(" s")
        self.spin_time.setValue(2.0)
        grid.addWidget(self.spin_time, 1, 4)

        layout.addLayout(grid)

        self.figure = Figure(figsize=(6, 4))
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.ax = self.figure.add_subplot(111)
        self._style_axes()
        layout.addWidget(self.canvas, 1)

        controls = QHBoxLayout()
        self.btn_sweep = QPushButton("Sweep Z")
        self.btn_sweep.setObjectName("primaryAction")
        self.btn_sweep.clicked.connect(self._on_sweep)
        controls.addWidget(self.btn_sweep)

        self.btn_goto = QPushButton("Go to Maximum")
        self.btn_goto.setEnabled(False)
        self.btn_goto.clicked.connect(self._on_goto_max)
        controls.addWidget(self.btn_goto)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.lbl_result = QLabel("Sweep Z to locate the surface.")
        self.lbl_result.setProperty("muted", True)
        layout.addWidget(self.lbl_result)

    def _style_axes(self):
        self.ax.set_xlabel("Z (V)")
        self.ax.set_ylabel("Signal (V)")
        theme.style_figure(self.figure, self.ax, self._dark)
        try:
            self.figure.tight_layout()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _on_sweep(self):
        z_min = self.spin_z_min.value()
        z_max = self.spin_z_max.value()
        if z_min >= z_max:
            QMessageBox.warning(self, "Invalid Range", "Z min must be below Z max.")
            return
        profile = self.scanner.profile
        if z_min < profile.vmin or z_max > profile.vmax:
            QMessageBox.warning(
                self, "Out of Range",
                f"{profile.name} safe range is {profile.vmin} to {profile.vmax} V.",
            )
            return

        self.btn_sweep.setEnabled(False)
        self.btn_goto.setEnabled(False)
        self.lbl_result.setText("Sweeping Z…")

        self._worker = AxisSweepWorker(
            self.scanner,
            channel=self.z_channel,
            v_min=z_min,
            v_max=z_max,
            points=self.spin_points.value(),
            sweep_time=self.spin_time.value(),
            detector_channel=self.spin_signal.value(),
        )
        self._worker.result_ready.connect(self._on_result)
        self._worker.error_occurred.connect(self._on_error)
        self._worker.start()

    def _on_result(self, z: np.ndarray, signal: np.ndarray):
        self._z = z
        self._signal = signal
        idx = int(np.argmax(signal))

        self.ax.clear()
        self.ax.plot(z, signal, lw=1.2)
        self.ax.axvline(z[idx], ls="--", lw=1, color="#e5484d")
        self.ax.plot([z[idx]], [signal[idx]], "o", color="#e5484d", ms=5)
        self._style_axes()
        self.canvas.draw_idle()

        self.lbl_result.setText(f"Peak {signal[idx]:.5g} V at Z = {z[idx]:.3f} V")
        self.btn_sweep.setEnabled(True)
        self.btn_goto.setEnabled(True)

    def _on_error(self, message: str):
        QMessageBox.critical(self, "Sweep Error", message)
        self.lbl_result.setText("Sweep failed.")
        self.btn_sweep.setEnabled(True)

    # ------------------------------------------------------------------
    def _on_goto_max(self):
        if self._z is None or self._signal is None:
            return
        z_target = float(self._z[int(np.argmax(self._signal))])
        self.btn_goto.setEnabled(False)
        self.lbl_result.setText(f"Moving Z to {z_target:.3f} V…")

        command = QuickCommand(self.scanner.daq.setAO_DC, self.z_channel, z_target)
        command.signals.result.connect(lambda _: self._on_moved(z_target))
        command.signals.error.connect(self._on_move_error)
        QThreadPool.globalInstance().start(command)

    def _on_moved(self, z_target: float):
        self.lbl_result.setText(f"Z parked at {z_target:.3f} V (signal maximum).")
        self.btn_goto.setEnabled(True)
        self.z_moved.emit(z_target)

    def _on_move_error(self, message: str):
        QMessageBox.critical(self, "Stage Error", f"Failed to move Z: {message}")
        self.btn_goto.setEnabled(True)

    # ------------------------------------------------------------------
    def closeEvent(self, event):
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(10000)
        event.accept()
