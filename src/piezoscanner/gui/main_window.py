"""Top-level window: wires the control panel, plot panel, background
scan/quick-command workers, hardware connection, configuration, and
settings persistence together."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, replace as dc_replace

import numpy as np
from PyQt6.QtCore import QSettings, Qt, QThreadPool, QTimer
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStatusBar,
)

from ..core.backends import LockinBackend, NidaqBackend, NidaqmxBackend, ScannerBackend
from ..core.config import AppConfig, load_config
from ..core.profiles import ScannerProfile
from ..core.scanner import PiezoScanner, ScanLineResult
from ..core.simulated_daq import SimulatedDaq
from . import theme
from .config_dialog import ConfigDialog
from .control_panel import ChannelSlot, ControlPanel
from .find_surface_dialog import FindSurfaceDialog
from .plot_panel import PlotPanel
from .scan_worker import QuickCommand, ScanWorker

ORG_NAME = "LevyLab"
APP_NAME = "PiezoScanner"


def _connect_backend(app_config: AppConfig) -> tuple[ScannerBackend, bool, str]:
    """Connect to whichever backend the config selects.

    Returns ``(backend, connected, label)``. If the selected backend can't
    be reached, falls back to a simulated DAQ (shared by both backend
    kinds, wrapped in a LockinBackend since that's the interface it
    implements) so the app stays fully usable either way — ``connected``
    and ``label`` tell the caller what was actually requested and whether
    it's real or simulated.
    """
    if app_config.backend == "nidaqstudio":
        try:
            backend = NidaqBackend(
                host=app_config.nidaq.host,
                port=app_config.nidaq.port,
                sample_rate=app_config.nidaq.sample_rate,
            )
            return backend, True, "nidaqstudio"
        except Exception as exc:
            print(f"nidaqstudio connection failed: {exc}. Running in Simulation Mode.")
            return LockinBackend(SimulatedDaq()), False, "nidaqstudio (unreachable)"

    if app_config.backend == "nidaqmx":
        try:
            backend = NidaqmxBackend(
                app_config.nidaqmx.devices,
                sample_rate=app_config.nidaqmx.sample_rate,
                ao_range=app_config.nidaqmx.ao_range,
                ai_range=app_config.nidaqmx.ai_range,
                sync=app_config.nidaqmx.sync,
            )
            for line in backend.sync_report:
                print(f"NI-DAQmx: {line}")
            return backend, True, "NI-DAQmx"
        except Exception as exc:
            print(f"NI-DAQmx initialization failed: {exc}. Running in Simulation Mode.")
            return LockinBackend(SimulatedDaq()), False, "NI-DAQmx (unreachable)"

    try:
        from flex.inst.levylab.Lockin import Lockin

        return LockinBackend(Lockin()), True, "Multichannel Lockin"
    except Exception as exc:
        print(f"Hardware initialization failed: {exc}. Running in Simulation Mode.")
        return LockinBackend(SimulatedDaq()), False, "Multichannel Lockin (unreachable)"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FLEX PiezoScanner")
        self.resize(1500, 900)

        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.app_config, config_error = load_config()
        self.backend, self.hardware_connected, self._backend_label = _connect_backend(self.app_config)

        self.worker: ScanWorker | None = None
        self._active_config: dict | None = None
        self._active_slots: list[ChannelSlot] = []
        self._scan_start_time: float | None = None
        self._dark_theme = True

        # 3D scan bookkeeping
        self._scan3d_folder: str | None = None
        self._n_slices = 1
        self._slices_saved = 0
        self._lines_done = 0

        self.control_panel = ControlPanel(
            profiles=self.app_config.profiles,
            z_available=self.app_config.outputs.z_enabled,
        )
        self.plot_panel = PlotPanel()

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.control_panel)
        splitter.addWidget(self.plot_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([400, 1100])
        self.setCentralWidget(splitter)

        self._build_menu()
        self._build_status_bar()
        self._wire_signals()

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._update_elapsed)

        self._restore_settings()
        self._update_hardware_status_label()

        if config_error:
            QTimer.singleShot(0, lambda: QMessageBox.warning(self, "Configuration", config_error))

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
        self.act_scan_up = QAction("Scan Up", self)
        self.act_scan_up.setShortcut(QKeySequence("Ctrl+Return"))
        self.act_scan_up.triggered.connect(lambda: self.start_scan(False))
        scan_menu.addAction(self.act_scan_up)

        self.act_scan_down = QAction("Scan Down", self)
        self.act_scan_down.setShortcut(QKeySequence("Ctrl+Shift+Return"))
        self.act_scan_down.triggered.connect(lambda: self.start_scan(True))
        scan_menu.addAction(self.act_scan_down)

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

        scan_menu.addSeparator()
        act_surface = QAction("Find Surface...", self)
        act_surface.triggered.connect(self.open_find_surface)
        scan_menu.addAction(act_surface)

        settings_menu = menu_bar.addMenu("S&ettings")
        act_config = QAction("Configure Hardware...", self)
        act_config.setShortcut(QKeySequence("Ctrl+,"))
        act_config.triggered.connect(self.open_config_dialog)
        settings_menu.addAction(act_config)

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

        self.lbl_position = QLabel("XY: —")
        bar.addPermanentWidget(self.lbl_position)

        self.lbl_z = QLabel("Z: —")
        bar.addPermanentWidget(self.lbl_z)

        self.lbl_elapsed = QLabel("")
        bar.addPermanentWidget(self.lbl_elapsed)

    def _update_hardware_status_label(self):
        if self.hardware_connected:
            self.lbl_hw_status.setText(f"● {self._backend_label} Connected")
            self.lbl_hw_status.setStyleSheet("color: #3fb950; font-weight: 600;")
        else:
            self.lbl_hw_status.setText(f"● Simulation Mode ({self._backend_label})")
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
        cp.find_surface_requested.connect(self.open_find_surface)

        pp = self.plot_panel
        pp.region_selected_um.connect(self._on_region_selected_um)
        pp.move_requested_um.connect(self._on_move_requested_um)

    def _on_channels_changed(self):
        self.plot_panel.sync_channels(self.control_panel.enabled_channel_slots())

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def open_config_dialog(self):
        if self.worker is not None:
            QMessageBox.warning(self, "Scan in Progress", "Finish or abort the scan before changing configuration.")
            return
        dialog = ConfigDialog(self.app_config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_config is not None:
            self.app_config = dialog.result_config
            self._apply_config()
            self.control_panel.set_status("Configuration saved")

    def _apply_config(self):
        self.control_panel.set_profiles(self.app_config.profiles)
        self.control_panel.set_z_available(self.app_config.outputs.z_enabled)
        self._reconnect_backend()

    def _reconnect_backend(self):
        old_backend = self.backend
        self.backend, self.hardware_connected, self._backend_label = _connect_backend(self.app_config)
        self._update_hardware_status_label()
        old_backend.close()

    def _profile_for(self, name: str) -> ScannerProfile:
        profile = self.app_config.profiles.get(name)
        if profile is None:
            profile = next(iter(self.app_config.profiles.values()))
        return profile

    def _make_scanner(self, cfg: dict) -> PiezoScanner:
        profile = self._profile_for(cfg["profile"])
        # Apply the (possibly user-edited) calibration shown in the GUI
        # without mutating the shared config.
        profile = dc_replace(profile, calibration_um_per_v=cfg["calibration_um_per_v"])
        return PiezoScanner(
            backend=self.backend,
            profile=profile,
            fast_axis_channel=self.app_config.outputs.x_channel,
            slow_axis_channel=self.app_config.outputs.y_channel,
            initial_wait=cfg.get("initial_wait", 1.0),
        )

    # ------------------------------------------------------------------
    # Scan lifecycle
    # ------------------------------------------------------------------
    def start_scan(self, slow_axis_down: bool = False):
        if self.worker is not None:
            return

        cfg = self.control_panel.get_scan_config()
        cfg["slow_axis_down"] = slow_axis_down
        outputs = self.app_config.outputs

        if outputs.x_channel == 0 or outputs.y_channel == 0:
            QMessageBox.warning(
                self, "Outputs Not Configured",
                "X and Y output channels must be set (Settings → Configure Hardware…).",
            )
            return

        if cfg["x_min"] >= cfg["x_max"] or cfg["y_min"] >= cfg["y_max"]:
            QMessageBox.warning(self, "Invalid Range", "Min must be less than Max for both X and Y.")
            return

        profile = self._profile_for(cfg["profile"])
        for value in (cfg["x_min"], cfg["x_max"], cfg["y_min"], cfg["y_max"]):
            if value < profile.vmin or value > profile.vmax:
                QMessageBox.warning(
                    self, "Out of Range",
                    f"{profile.name} safe range is {profile.vmin} to {profile.vmax} V.",
                )
                return

        is_3d = cfg["mode"] == "3D"
        z_values: list[float] | None = None
        if is_3d:
            if not outputs.z_enabled:
                QMessageBox.warning(
                    self, "No Z Output",
                    "3D mode needs a Z output channel (Settings → Configure Hardware…).",
                )
                return
            if cfg["z_min"] >= cfg["z_max"]:
                QMessageBox.warning(self, "Invalid Range", "Z min must be less than Z max.")
                return
            for value in (cfg["z_min"], cfg["z_max"]):
                if value < profile.vmin or value > profile.vmax:
                    QMessageBox.warning(
                        self, "Out of Range",
                        f"{profile.name} safe range is {profile.vmin} to {profile.vmax} V.",
                    )
                    return
            z_values = list(np.linspace(cfg["z_min"], cfg["z_max"], cfg["z_steps"]))

        slots = self.control_panel.enabled_channel_slots()
        if not slots:
            QMessageBox.warning(self, "No Channels", "Add and enable at least one input channel before scanning.")
            return
        channel_numbers = [slot.number for slot in slots]
        if len(set(channel_numbers)) != len(channel_numbers):
            QMessageBox.warning(self, "Duplicate Channels", "Each enabled channel needs a distinct AI channel number.")
            return

        # 3D scans stream each slice to disk as it completes, so the target
        # folder has to exist before the scan starts.
        self._scan3d_folder = None
        self._slices_saved = 0
        if is_3d:
            save_dir = self.control_panel.save_directory or os.getcwd()
            folder = os.path.join(save_dir, time.strftime("3DScan_%Y%m%d_%H%M%S"))
            try:
                os.makedirs(folder, exist_ok=True)
                self._write_3d_meta(folder, cfg, z_values)
            except OSError as exc:
                QMessageBox.critical(self, "Save Error", f"Could not create scan folder: {exc}")
                return
            self._scan3d_folder = folder

        scanner = self._make_scanner(cfg)

        self._active_config = cfg
        self._active_slots = slots
        self._n_slices = len(z_values) if z_values else 1
        self._lines_done = 0

        cal = cfg["calibration_um_per_v"]
        extent = (cfg["x_min"] * cal, cfg["x_max"] * cal, cfg["y_min"] * cal, cfg["y_max"] * cal)
        z_range = (cfg["z_min"], cfg["z_max"]) if is_3d else None
        self.plot_panel.reset_for_scan(slots, cfg["x_points"], cfg["y_points"], extent, z_range=z_range)

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
            slow_axis_down=slow_axis_down,
            z_values=z_values,
            z_channel=outputs.z_channel,
        )
        self.worker.line_ready.connect(self._on_line_ready)
        self.worker.slice_started.connect(self._on_slice_started)
        self.worker.slice_completed.connect(self._on_slice_completed)
        self.worker.progress_changed.connect(self.control_panel.set_progress)
        self.worker.status_changed.connect(self.control_panel.set_status)
        self.worker.finished_ok.connect(self._on_scan_finished)
        self.worker.error_occurred.connect(self._on_scan_error)

        self.control_panel.set_scanning_state(True)
        self.act_scan_up.setEnabled(False)
        self.act_scan_down.setEnabled(False)
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

    def _on_line_ready(self, slice_index: int, result: ScanLineResult):
        for channel_number, pixels in result.pixels.items():
            self.plot_panel.update_line(channel_number, result.row_index, pixels)

        if self._active_config is not None:
            self._lines_done += 1
            total = self._n_slices * self._active_config["y_points"]
            remaining = max(total - self._lines_done, 0)
            eta_s = remaining * self._active_config["line_time"]
            self.control_panel.set_eta(f"ETA: {self._format_duration(eta_s)}")

    def _on_slice_started(self, slice_index: int, z_value: float):
        self.plot_panel.clear_current_images()
        self.plot_panel.set_slice_info(
            f"Slice {slice_index + 1}/{self._n_slices} — Z = {z_value:.3f} V"
        )
        self.lbl_z.setText(f"Z: {z_value:.3f} V")

    def _on_slice_completed(self, slice_index: int, z_value: float):
        folder = self._scan3d_folder
        if folder is not None:
            try:
                for slot in self._active_slots:
                    view = self.plot_panel.channel_views().get(slot.number)
                    if view is None or view.image_data is None:
                        continue
                    safe_label = "".join(c if c.isalnum() else "_" for c in slot.label)
                    fname = f"slice{slice_index:03d}_Z{z_value:.4f}V_AI{slot.number}_{safe_label}.txt"
                    np.savetxt(
                        os.path.join(folder, fname), view.image_data, delimiter="\t",
                        header=f"{slot.label} (AI{slot.number}) at Z = {z_value:.6f} V — tab separated Volts",
                    )
                self._slices_saved += 1
            except OSError as exc:
                self.control_panel.set_status(f"Slice save failed: {exc}")

        self.plot_panel.add_slice(z_value)

    def _on_scan_finished(self, completed: bool):
        self._elapsed_timer.stop()
        self._scan_start_time = None
        self.control_panel.set_scanning_state(False)
        self.act_scan_up.setEnabled(True)
        self.act_scan_down.setEnabled(True)
        self.act_abort.setEnabled(False)
        self.control_panel.set_eta("")

        if self.worker is not None:
            self.worker.wait()
        self.worker = None

        if self._scan3d_folder is not None:
            folder = self._scan3d_folder
            self._scan3d_folder = None
            try:
                with open(os.path.join(folder, "meta.txt"), "a", encoding="utf-8") as fh:
                    fh.write(f"slices_completed: {self._slices_saved}\n")
                    fh.write(f"finished: {'complete' if completed else 'aborted'}\n")
            except OSError:
                pass

            state = "complete" if completed else "stopped"
            self.control_panel.set_status(f"3D scan {state} — {self._slices_saved} slice(s) saved")
            if self._slices_saved > 0:
                QMessageBox.information(
                    self, "3D Scan Saved",
                    f"{self._slices_saved} slice(s) saved to:\n{folder}",
                )
            return

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
    def _meta_lines(self, cfg: dict, timestamp: str) -> list[str]:
        profile = self._profile_for(cfg["profile"])
        outputs = self.app_config.outputs
        cal = cfg["calibration_um_per_v"]
        lines = [
            f"timestamp: {timestamp}",
            f"mode: {cfg['mode']}",
            f"profile: {cfg['profile']} (safe range {profile.vmin} to {profile.vmax} V)",
            f"calibration_um_per_v: {cal}",
            f"x_range_v: {cfg['x_min']} to {cfg['x_max']}",
            f"y_range_v: {cfg['y_min']} to {cfg['y_max']}",
            f"x_range_um: {cfg['x_min'] * cal:.4f} to {cfg['x_max'] * cal:.4f}",
            f"y_range_um: {cfg['y_min'] * cal:.4f} to {cfg['y_max'] * cal:.4f}",
            f"points: {cfg['x_points']} x {cfg['y_points']}",
            f"slow_axis_direction: {'down (y_max -> y_min)' if cfg.get('slow_axis_down') else 'up (y_min -> y_max)'}",
            f"line_time_s: {cfg['line_time']}",
            f"initial_wait_s: {cfg.get('initial_wait', 1.0)}",
            f"lag_delay_samples: {cfg['delay_samples']}",
            f"x_output_channel: {outputs.x_channel}",
            f"y_output_channel: {outputs.y_channel}",
        ]
        if cfg["mode"] == "3D":
            lines += [
                f"z_output_channel: {outputs.z_channel}",
                f"z_range_v: {cfg['z_min']} to {cfg['z_max']}",
                f"z_steps: {cfg['z_steps']}",
            ]
        return lines

    def _write_3d_meta(self, folder: str, cfg: dict, z_values: list[float]) -> None:
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        lines = self._meta_lines(cfg, timestamp)
        lines.append("z_values_v: " + ", ".join(f"{z:.4f}" for z in z_values))
        with open(os.path.join(folder, "meta.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def _save_scan_data(self):
        save_dir = self.control_panel.save_directory or os.getcwd()
        try:
            os.makedirs(save_dir, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, "Save Error", f"Could not create save directory: {exc}")
            return

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        cfg = self._active_config

        try:
            meta_path = os.path.join(save_dir, f"scan_{timestamp}_meta.txt")
            with open(meta_path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(self._meta_lines(cfg, timestamp)) + "\n")

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
        profile = self._profile_for(self.control_panel.current_profile_name())
        mid = (profile.vmin + profile.vmax) / 2.0
        self._move_to_voltage(mid, mid)

    def full_range(self):
        profile = self._profile_for(self.control_panel.current_profile_name())
        self.control_panel.set_full_range(profile.vmin, profile.vmax)
        self.control_panel.set_status("Reset to full range")

    def _move_to_voltage(self, x_v: float, y_v: float):
        # Single chokepoint for every hardware-motion path (center stage,
        # jog, click-to-move, and the menu actions that bypass disabled
        # widgets) — never move the stage while a scan owns the DAQ.
        if self.control_panel.is_scanning():
            QMessageBox.warning(self, "Scan in Progress", "Cannot move the stage while a scan is running.")
            return

        outputs = self.app_config.outputs
        if outputs.x_channel == 0 or outputs.y_channel == 0:
            QMessageBox.warning(
                self, "Outputs Not Configured",
                "X and Y output channels must be set (Settings → Configure Hardware…).",
            )
            return

        cfg = self.control_panel.get_scan_config()
        profile = self._profile_for(cfg["profile"])
        x_v = profile.clip_voltage(x_v)
        y_v = profile.clip_voltage(y_v)

        self.control_panel.set_status(f"Moving to X={x_v:.3f} V, Y={y_v:.3f} V...")

        def _do_move():
            self.backend.set_dc(outputs.x_channel, x_v)
            self.backend.set_dc(outputs.y_channel, y_v)
            return x_v, y_v

        command = QuickCommand(_do_move)
        command.signals.result.connect(self._on_move_done)
        command.signals.error.connect(self._on_move_error)
        QThreadPool.globalInstance().start(command)

    def _on_move_done(self, result):
        x_v, y_v = result
        cal = self.control_panel.get_scan_config()["calibration_um_per_v"]
        self.lbl_position.setText(f"XY: {x_v:.3f}, {y_v:.3f} V ({x_v * cal:.2f}, {y_v * cal:.2f} μm)")
        self.control_panel.set_status(f"Positioned at X={x_v:.3f} V, Y={y_v:.3f} V")

    def _on_move_error(self, message: str):
        QMessageBox.critical(self, "Stage Error", f"Failed to move stage: {message}")
        self.control_panel.set_status("Move failed")

    def _on_region_selected_um(self, x1_um: float, x2_um: float, y1_um: float, y2_um: float):
        if self.control_panel.is_scanning():
            return
        cfg = self.control_panel.get_scan_config()
        profile = self._profile_for(cfg["profile"])
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
    # Find surface
    # ------------------------------------------------------------------
    def open_find_surface(self):
        if self.worker is not None:
            QMessageBox.warning(self, "Scan in Progress", "Finish or abort the scan before using Find Surface.")
            return
        outputs = self.app_config.outputs
        if not outputs.z_enabled:
            QMessageBox.information(
                self, "No Z Output",
                "Find Surface needs a Z output channel.\nSet one under Settings → Configure Hardware…",
            )
            return

        cfg = self.control_panel.get_scan_config()
        scanner = self._make_scanner(cfg)
        dialog = FindSurfaceDialog(scanner, outputs.z_channel, self._dark_theme, self)
        dialog.z_moved.connect(self._on_z_moved)
        dialog.exec()

    def _on_z_moved(self, z_value: float):
        self.lbl_z.setText(f"Z: {z_value:.3f} V")
        self.control_panel.set_status(f"Z parked at {z_value:.3f} V")

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
            self, "About FLEX PiezoScanner",
            "FLEX PiezoScanner\n\n"
            "Piezo raster-scan controller with a switchable hardware backend "
            "(Multichannel Lockin, nidaqstudio, or NI-DAQmx direct).\n\n"
            f"Backend: {self._backend_label} "
            f"({'connected' if self.hardware_connected else 'simulated'})",
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
        self.backend.close()
        event.accept()
