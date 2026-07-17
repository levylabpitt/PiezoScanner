"""Top-level window: wires the control panel, plot panel, background
scan/quick-command workers, hardware connection, and settings persistence
together."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, replace as dc_replace

import numpy as np
from PyQt6.QtCore import QSettings, Qt, QThreadPool, QTimer
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import QApplication, QLabel, QMainWindow, QMessageBox, QSplitter, QStatusBar

from ..core.profiles import PROFILES
from ..core.scanner import PiezoScanner, ScanLineResult
from ..core.simulated_daq import SimulatedDaq
from . import theme
from .control_panel import ChannelSlot, ControlPanel
from .plot_panel import PlotPanel
from .scan_worker import QuickCommand, ScanWorker

ORG_NAME = "LevyLab"
APP_NAME = "PiezoScanner"


def _connect_hardware():
    """Try the real Levylab FLEX lock-in; fall back to a simulated DAQ that
    implements the same surface so the rest of the app never has to know
    the difference."""
    try:
        from flex.inst.levylab.Lockin import Lockin

        return Lockin(), True
    except Exception as exc:
        print(f"Hardware initialization failed: {exc}. Running in Simulation Mode.")
        return SimulatedDaq(), False


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PiezoScanner — Research-Grade Piezo Raster Scan Controller")
        self.resize(1500, 900)

        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.daq, self.hardware_connected = _connect_hardware()

        self.worker: ScanWorker | None = None
        self._active_config: dict | None = None
        self._active_slots: list[ChannelSlot] = []
        self._scan_start_time: float | None = None
        self._dark_theme = True

        self.control_panel = ControlPanel()
        self.plot_panel = PlotPanel()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.control_panel)
        splitter.addWidget(self.plot_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([380, 1120])
        self.setCentralWidget(splitter)

        self._build_menu()
        self._build_status_bar()
        self._wire_signals()

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._update_elapsed)

        self._restore_settings()
        self._update_hardware_status_label()

    # ------------------------------------------------------------------
    # Menu / status bar
    # ------------------------------------------------------------------
    def _build_menu(self):
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("&File")
        act_browse = QAction("Set Save Directory...", self)
        act_browse.triggered.connect(self.control_panel.browse_directory)
        file_menu.addAction(act_browse)
        file_menu.addSeparator()
        act_exit = QAction("Exit", self)
        act_exit.triggered.connect(self.close)
        file_menu.addAction(act_exit)

        scan_menu = menu_bar.addMenu("&Scan")
        self.act_start = QAction("Start Scan", self)
        self.act_start.setShortcut(QKeySequence("Ctrl+Return"))
        self.act_start.triggered.connect(self.start_scan)
        scan_menu.addAction(self.act_start)

        self.act_abort = QAction("Abort Scan", self)
        self.act_abort.setShortcut(QKeySequence("Esc"))
        self.act_abort.setEnabled(False)
        self.act_abort.triggered.connect(self.abort_scan)
        scan_menu.addAction(self.act_abort)

        scan_menu.addSeparator()
        act_center = QAction("Center Stage", self)
        act_center.triggered.connect(self.center_stage)
        scan_menu.addAction(act_center)

        act_full = QAction("Reset to Full Range", self)
        act_full.triggered.connect(self.full_range)
        scan_menu.addAction(act_full)

        view_menu = menu_bar.addMenu("&View")
        self.act_dark_theme = QAction("Dark Theme", self)
        self.act_dark_theme.setCheckable(True)
        self.act_dark_theme.setChecked(True)
        self.act_dark_theme.toggled.connect(self.set_dark_theme)
        view_menu.addAction(self.act_dark_theme)

        help_menu = menu_bar.addMenu("&Help")
        act_about = QAction("About", self)
        act_about.triggered.connect(self._show_about)
        help_menu.addAction(act_about)

    def _build_status_bar(self):
        bar = QStatusBar()
        self.setStatusBar(bar)

        self.lbl_hw_status = QLabel()
        bar.addPermanentWidget(self.lbl_hw_status)

        self.lbl_position = QLabel("Position: —")
        bar.addPermanentWidget(self.lbl_position)

        self.lbl_elapsed = QLabel("")
        bar.addPermanentWidget(self.lbl_elapsed)

    def _update_hardware_status_label(self):
        if self.hardware_connected:
            self.lbl_hw_status.setText("● Hardware Connected")
            self.lbl_hw_status.setStyleSheet("color: #3fb950; font-weight: 600;")
        else:
            self.lbl_hw_status.setText("● Simulation Mode")
            self.lbl_hw_status.setStyleSheet("color: #d9a441; font-weight: 600;")

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------
    def _wire_signals(self):
        cp = self.control_panel
        cp.start_scan_requested.connect(self.start_scan)
        cp.abort_scan_requested.connect(self.abort_scan)
        cp.pause_toggled.connect(self._on_pause_toggled)
        cp.center_stage_requested.connect(self.center_stage)
        cp.full_range_requested.connect(self.full_range)
        cp.move_to_requested.connect(self._move_to_voltage)
        cp.channels_changed.connect(self._on_channels_changed)
        cp.browse_directory_requested.connect(
            lambda path: self.control_panel.set_status(f"Save directory: {path}")
        )

        pp = self.plot_panel
        pp.region_selected_um.connect(self._on_region_selected_um)
        pp.move_requested_um.connect(self._on_move_requested_um)

    def _on_channels_changed(self):
        self.plot_panel.sync_channels(self.control_panel.enabled_channel_slots())

    # ------------------------------------------------------------------
    # Scan lifecycle
    # ------------------------------------------------------------------
    def start_scan(self):
        if self.worker is not None:
            return

        cfg = self.control_panel.get_scan_config()

        if cfg["x_min"] >= cfg["x_max"] or cfg["y_min"] >= cfg["y_max"]:
            QMessageBox.warning(self, "Invalid Range", "Min must be less than Max for both X and Y.")
            return

        profile = PROFILES[cfg["profile"]]
        for value in (cfg["x_min"], cfg["x_max"], cfg["y_min"], cfg["y_max"]):
            if value < profile.vmin or value > profile.vmax:
                QMessageBox.warning(
                    self, "Out of Range",
                    f"{profile.name} safe range is {profile.vmin} to {profile.vmax} V.",
                )
                return

        slots = self.control_panel.enabled_channel_slots()
        if not slots:
            QMessageBox.warning(self, "No Channels", "Add and enable at least one input channel before scanning.")
            return
        channel_numbers = [slot.number for slot in slots]
        if len(set(channel_numbers)) != len(channel_numbers):
            QMessageBox.warning(self, "Duplicate Channels", "Each enabled channel needs a distinct AI channel number.")
            return

        scanner = PiezoScanner(
            daq=self.daq,
            profile=cfg["profile"],
            fast_axis_channel=cfg["fast_axis_channel"],
            slow_axis_channel=cfg["slow_axis_channel"],
            daq_fs=13000,
            daq_num_samples=1000,
        )
        # Apply the (possibly user-edited) calibration shown in the GUI
        # without mutating the shared PROFILES table.
        scanner.profile = dc_replace(scanner.profile, calibration_um_per_v=cfg["calibration_um_per_v"])

        self._active_config = cfg
        self._active_slots = slots

        cal = cfg["calibration_um_per_v"]
        extent = (cfg["x_min"] * cal, cfg["x_max"] * cal, cfg["y_min"] * cal, cfg["y_max"] * cal)
        self.plot_panel.reset_for_scan(slots, cfg["x_points"], cfg["y_points"], extent)

        self.worker = ScanWorker(
            scanner,
            x_points=cfg["x_points"],
            y_points=cfg["y_points"],
            line_time=cfg["line_time"],
            x_min=cfg["x_min"],
            x_max=cfg["x_max"],
            y_min=cfg["y_min"],
            y_max=cfg["y_max"],
            detector_channels=channel_numbers,
            delay_samples=cfg["delay_samples"],
        )
        self.worker.line_ready.connect(self._on_line_ready)
        self.worker.progress_changed.connect(self.control_panel.set_progress)
        self.worker.status_changed.connect(self.control_panel.set_status)
        self.worker.finished_ok.connect(self._on_scan_finished)
        self.worker.error_occurred.connect(self._on_scan_error)

        self.control_panel.set_scanning_state(True)
        self.act_start.setEnabled(False)
        self.act_abort.setEnabled(True)
        self._scan_start_time = time.monotonic()
        self._elapsed_timer.start()
        self.worker.start()

    def abort_scan(self):
        if self.worker is not None:
            self.worker.request_abort()
            self.control_panel.set_status("Aborting...")

    def _on_pause_toggled(self, paused: bool):
        if self.worker is not None:
            self.worker.set_paused(paused)

    def _on_line_ready(self, result: ScanLineResult):
        for channel_number, pixels in result.pixels.items():
            self.plot_panel.update_line(channel_number, result.line_index, pixels)

        if self._active_config is not None:
            remaining = self._active_config["y_points"] - (result.line_index + 1)
            eta_s = max(remaining, 0) * self._active_config["line_time"]
            self.control_panel.set_eta(f"ETA: {self._format_duration(eta_s)}")

    def _on_scan_finished(self, completed: bool):
        self._elapsed_timer.stop()
        self._scan_start_time = None
        self.control_panel.set_scanning_state(False)
        self.act_start.setEnabled(True)
        self.act_abort.setEnabled(False)
        self.control_panel.set_eta("")

        if self.worker is not None:
            self.worker.wait()
        self.worker = None

        has_data = any(
            view.image_data is not None and np.any(~np.isnan(view.image_data))
            for view in self.plot_panel.channel_views().values()
        )
        if has_data:
            self.control_panel.set_status("Saving data..." if completed else "Stopped — saving partial data...")
            self._save_scan_data()
            self.control_panel.set_status("Complete & saved" if completed else "Stopped (partial data saved)")
        else:
            self.control_panel.set_status("Complete" if completed else "Stopped")

    def _on_scan_error(self, message: str):
        QMessageBox.critical(self, "Scan Error", message)

    # ------------------------------------------------------------------
    # Save / export
    # ------------------------------------------------------------------
    def _save_scan_data(self):
        save_dir = self.control_panel.save_directory or os.getcwd()
        try:
            os.makedirs(save_dir, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, "Save Error", f"Could not create save directory: {exc}")
            return

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        cfg = self._active_config
        profile = PROFILES[cfg["profile"]]
        cal = cfg["calibration_um_per_v"]

        meta_lines = [
            f"timestamp: {timestamp}",
            f"profile: {cfg['profile']} (safe range {profile.vmin} to {profile.vmax} V)",
            f"calibration_um_per_v: {cal}",
            f"x_range_v: {cfg['x_min']} to {cfg['x_max']}",
            f"y_range_v: {cfg['y_min']} to {cfg['y_max']}",
            f"x_range_um: {cfg['x_min'] * cal:.4f} to {cfg['x_max'] * cal:.4f}",
            f"y_range_um: {cfg['y_min'] * cal:.4f} to {cfg['y_max'] * cal:.4f}",
            f"points: {cfg['x_points']} x {cfg['y_points']}",
            f"line_time_s: {cfg['line_time']}",
            f"lag_delay_samples: {cfg['delay_samples']}",
            f"fast_axis_channel: {cfg['fast_axis_channel']}",
            f"slow_axis_channel: {cfg['slow_axis_channel']}",
        ]

        try:
            meta_path = os.path.join(save_dir, f"scan_{timestamp}_meta.txt")
            with open(meta_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(meta_lines) + "\n")

            saved_files = [os.path.basename(meta_path)]
            for slot in self._active_slots:
                view = self.plot_panel.channel_views().get(slot.number)
                if view is None or view.image_data is None:
                    continue
                safe_label = "".join(c if c.isalnum() else "_" for c in slot.label)

                txt_path = os.path.join(save_dir, f"scan_{timestamp}_AI{slot.number}_{safe_label}.txt")
                np.savetxt(
                    txt_path, view.image_data, delimiter="\t",
                    header=f"{slot.label} (AI{slot.number}) - tab separated Volts, NaN = not acquired",
                )

                png_path = os.path.join(save_dir, f"scan_{timestamp}_AI{slot.number}_{safe_label}.png")
                view.figure.savefig(png_path, facecolor=view.figure.get_facecolor())

                saved_files += [os.path.basename(txt_path), os.path.basename(png_path)]

            QMessageBox.information(self, "Scan Saved", "Saved:\n" + "\n".join(saved_files))
        except OSError as exc:
            QMessageBox.critical(self, "Save Error", f"Failed to save scan data: {exc}")

    # ------------------------------------------------------------------
    # Stage positioning (center / full range / jog / click-to-move)
    # ------------------------------------------------------------------
    def center_stage(self):
        profile = PROFILES[self.control_panel.current_profile_name()]
        mid = (profile.vmin + profile.vmax) / 2.0
        self._move_to_voltage(mid, mid)

    def full_range(self):
        profile = PROFILES[self.control_panel.current_profile_name()]
        self.control_panel.set_full_range(profile.vmin, profile.vmax)
        self.control_panel.set_status("Reset to full range")

    def _move_to_voltage(self, x_v: float, y_v: float):
        # Single chokepoint for every hardware-motion path (center stage,
        # jog, click-to-move, and the menu actions that bypass disabled
        # widgets) — never move the stage while a scan owns the DAQ.
        if self.control_panel.is_scanning():
            QMessageBox.warning(self, "Scan in Progress", "Cannot move the stage while a scan is running.")
            return

        cfg = self.control_panel.get_scan_config()
        profile = PROFILES[cfg["profile"]]
        x_v = profile.clip_voltage(x_v)
        y_v = profile.clip_voltage(y_v)
        fast_channel = cfg["fast_axis_channel"]
        slow_channel = cfg["slow_axis_channel"]

        self.control_panel.set_status(f"Moving to X={x_v:.3f} V, Y={y_v:.3f} V...")

        def _do_move():
            self.daq.setAO_DC(fast_channel, x_v)
            self.daq.setAO_DC(slow_channel, y_v)
            return x_v, y_v

        command = QuickCommand(_do_move)
        command.signals.result.connect(self._on_move_done)
        command.signals.error.connect(self._on_move_error)
        QThreadPool.globalInstance().start(command)

    def _on_move_done(self, result):
        x_v, y_v = result
        cal = self.control_panel.get_scan_config()["calibration_um_per_v"]
        self.lbl_position.setText(f"Position: X={x_v:.3f} V ({x_v * cal:.2f} μm), Y={y_v:.3f} V ({y_v * cal:.2f} μm)")
        self.control_panel.set_status(f"Positioned at X={x_v:.3f} V, Y={y_v:.3f} V")

    def _on_move_error(self, message: str):
        QMessageBox.critical(self, "Stage Error", f"Failed to move stage: {message}")
        self.control_panel.set_status("Move failed")

    def _on_region_selected_um(self, x1_um: float, x2_um: float, y1_um: float, y2_um: float):
        if self.control_panel.is_scanning():
            return
        cfg = self.control_panel.get_scan_config()
        profile = PROFILES[cfg["profile"]]
        cal = cfg["calibration_um_per_v"]

        x1_v = profile.clip_voltage(x1_um / cal)
        x2_v = profile.clip_voltage(x2_um / cal)
        y1_v = profile.clip_voltage(y1_um / cal)
        y2_v = profile.clip_voltage(y2_um / cal)

        answer = QMessageBox.question(
            self, "Update Scan Range",
            "Set the scan window to the selected region?\n\n"
            f"X: {x1_v:.3f} to {x2_v:.3f} V  ({x1_um:.2f} to {x2_um:.2f} μm)\n"
            f"Y: {y1_v:.3f} to {y2_v:.3f} V  ({y1_um:.2f} to {y2_um:.2f} μm)",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.control_panel.set_range(x1_v, x2_v, y1_v, y2_v)

    def _on_move_requested_um(self, x_um: float, y_um: float):
        if self.control_panel.is_scanning():
            return
        cal = self.control_panel.get_scan_config()["calibration_um_per_v"]
        self._move_to_voltage(x_um / cal, y_um / cal)

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------
    def _update_elapsed(self):
        if self._scan_start_time is None:
            return
        elapsed = time.monotonic() - self._scan_start_time
        self.lbl_elapsed.setText(f"Elapsed: {self._format_duration(elapsed)}")

    @staticmethod
    def _format_duration(seconds: float) -> str:
        seconds = max(int(seconds), 0)
        minutes, secs = divmod(seconds, 60)
        return f"{minutes:02d}:{secs:02d}"

    def set_dark_theme(self, dark: bool):
        self._dark_theme = dark
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.DARK_QSS if dark else theme.LIGHT_QSS)
        self.plot_panel.set_theme(dark)

    def _show_about(self):
        QMessageBox.about(
            self, "About PiezoScanner",
            "PiezoScanner\n\n"
            "Research-grade piezo raster-scan controller for a Levylab FLEX Lockin.\n\n"
            f"Hardware: {'Connected' if self.hardware_connected else 'Simulation Mode'}",
        )

    # ------------------------------------------------------------------
    # Settings persistence
    # ------------------------------------------------------------------
    def _restore_settings(self):
        geometry = self.settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)

        dark = self.settings.value("theme/dark", True, type=bool)
        self.act_dark_theme.setChecked(dark)
        self.set_dark_theme(dark)

        save_dir = self.settings.value("save/directory", "", type=str)
        if save_dir:
            self.control_panel.set_save_directory(save_dir)

        scan_cfg_json = self.settings.value("scan/config", "", type=str)
        if scan_cfg_json:
            try:
                self.control_panel.set_scan_config(json.loads(scan_cfg_json))
            except (ValueError, TypeError):
                pass

        channels_json = self.settings.value("channels/slots", "", type=str)
        if channels_json:
            try:
                slots = [ChannelSlot(**item) for item in json.loads(channels_json)]
                if slots:
                    self.control_panel.restore_channels(slots)
            except (ValueError, TypeError, KeyError):
                pass

        self._on_channels_changed()

    def _save_settings(self):
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.setValue("theme/dark", self._dark_theme)
        self.settings.setValue("save/directory", self.control_panel.save_directory)
        self.settings.setValue("scan/config", json.dumps(self.control_panel.get_scan_config()))
        slots = [asdict(slot) for slot in self.control_panel.channel_slots()]
        self.settings.setValue("channels/slots", json.dumps(slots))

    def closeEvent(self, event):
        if self.worker is not None:
            answer = QMessageBox.question(self, "Scan in Progress", "A scan is currently running. Abort and exit?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.request_abort()
            self.worker.wait(5000)

        self._save_settings()
        event.accept()
