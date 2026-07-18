"""Right-hand plot area: one tab per enabled input channel, each a live
2D image (physical units, micrometers) with its own colormap, contrast
controls, drag-to-select scan region, and double-click-to-move.

In 3D mode each tab splits into two views: the live 2D slice on the left
and an accumulating stack of translucent slices (one per completed Z
level) on the right.
"""

from __future__ import annotations

import numpy as np
from matplotlib import colormaps
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.widgets import RectangleSelector
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401 — registers the '3d' projection
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .control_panel import COLORMAPS, ChannelSlot

_SLIDER_STEPS = 2000
_STACK_MAX_DIM = 32  # slices are downsampled to this for the 3D view


def _downsample(data: np.ndarray, max_dim: int = _STACK_MAX_DIM) -> np.ndarray:
    ny, nx = data.shape
    sy = max(1, int(np.ceil(ny / max_dim)))
    sx = max(1, int(np.ceil(nx / max_dim)))
    return data[::sy, ::sx]


class ChannelPlotView(QWidget):
    """A single channel's live image, contrast controls, and interaction
    handlers, embedded as one tab. Optionally shows a 3D stack view."""

    region_selected_um = pyqtSignal(float, float, float, float)  # x_min, x_max, y_min, y_max
    move_requested_um = pyqtSignal(float, float)

    def __init__(self, channel_number: int, colormap: str, dark: bool, parent=None):
        super().__init__(parent)
        self.channel_number = channel_number
        self.image_data: np.ndarray = np.zeros((2, 2))
        self.extent_um = (0.0, 10.0, 0.0, 10.0)
        self._dark = dark
        self._data_bounds = (0.0, 1.0)
        self._initial_cmap = colormap

        self._stack_enabled = False
        self._stack: list[tuple[float, np.ndarray]] = []
        self._z_range: tuple[float, float] | None = None

        layout = QVBoxLayout(self)

        self.figure = Figure(figsize=(6, 5))
        self.canvas = FigureCanvasQTAgg(self.figure)
        layout.addWidget(self.canvas, 1)

        self.ax = None
        self.ax3d = None
        self.im = None
        self.cbar = None
        self.selector = None
        self._build_axes()

        self.canvas.mpl_connect("button_press_event", self._on_click)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Colormap:"))
        self.combo_colormap = QComboBox()
        self.combo_colormap.addItems(COLORMAPS)
        self.combo_colormap.setCurrentText(colormap)
        self.combo_colormap.currentTextChanged.connect(self._on_colormap_changed)
        controls.addWidget(self.combo_colormap)

        self.chk_autoscale = QCheckBox("Live autoscale")
        self.chk_autoscale.setChecked(True)
        controls.addWidget(self.chk_autoscale)

        self.btn_autoscale_now = QPushButton("Autoscale now")
        self.btn_autoscale_now.clicked.connect(self.autoscale_now)
        controls.addWidget(self.btn_autoscale_now)
        controls.addStretch(1)
        layout.addLayout(controls)

        sliders = QHBoxLayout()
        sliders.addWidget(QLabel("Min:"))
        self.slider_min = QSlider(Qt.Orientation.Horizontal)
        self.slider_min.setRange(0, _SLIDER_STEPS)
        self.slider_min.valueChanged.connect(self._on_slider_changed)
        sliders.addWidget(self.slider_min, 1)
        self.lbl_min = QLabel("0.00000")
        sliders.addWidget(self.lbl_min)

        sliders.addWidget(QLabel("Max:"))
        self.slider_max = QSlider(Qt.Orientation.Horizontal)
        self.slider_max.setRange(0, _SLIDER_STEPS)
        self.slider_max.setValue(_SLIDER_STEPS)
        self.slider_max.valueChanged.connect(self._on_slider_changed)
        sliders.addWidget(self.slider_max, 1)
        self.lbl_max = QLabel("1.00000")
        sliders.addWidget(self.lbl_max)
        layout.addLayout(sliders)

        self._set_slider_bounds(0.0, 1.0)

    # -- axes / layout ----------------------------------------------------
    def _current_cmap_name(self) -> str:
        combo = getattr(self, "combo_colormap", None)
        return combo.currentText() if combo is not None else self._initial_cmap

    def _build_axes(self):
        """(Re)create the figure's axes for the current 2D/3D layout,
        preserving current image data, extent, and colormap."""
        self.figure.clf()
        if self._stack_enabled:
            self.ax = self.figure.add_subplot(1, 2, 1)
            self.ax3d = self.figure.add_subplot(1, 2, 2, projection="3d")
        else:
            self.ax = self.figure.add_subplot(1, 1, 1)
            self.ax3d = None

        self.im = self.ax.imshow(
            self.image_data, origin="lower", aspect="auto",
            cmap=self._current_cmap_name(), extent=self.extent_um,
        )
        self.cbar = self.figure.colorbar(self.im, ax=self.ax, label="Signal (V)")
        self.ax.set_xlabel("X position (μm)")
        self.ax.set_ylabel("Y position (μm)")
        self.ax.set_title(f"AI{self.channel_number}")
        theme.style_figure(self.figure, self.ax, self._dark, self.cbar)
        if self.ax3d is not None:
            theme.style_axes3d(self.ax3d, self._dark)

        self.selector = RectangleSelector(
            self.ax,
            self._on_select_box,
            useblit=True,
            props=dict(facecolor="#3d8bfd", edgecolor="#3d8bfd", alpha=0.2, fill=True),
            button=[1],
            minspanx=0.1,
            minspany=0.1,
            interactive=True,
        )

        try:
            self.figure.tight_layout()
        except Exception:
            pass
        self.canvas.draw_idle()

    def set_stack_enabled(self, enabled: bool):
        if enabled == self._stack_enabled:
            return
        self._stack_enabled = enabled
        if not enabled:
            self._stack = []
        self._build_axes()

    # -- data -----------------------------------------------------------
    def reset_image(self, shape: tuple[int, int], extent_um: tuple[float, float, float, float]):
        self.image_data = np.full(shape, np.nan)
        self.extent_um = extent_um
        self.im.set_data(self.image_data)
        self.im.set_extent(extent_um)
        self.ax.set_xlim(extent_um[0], extent_um[1])
        self.ax.set_ylim(extent_um[2], extent_um[3])
        self.canvas.draw_idle()

    def set_row(self, row_index: int, values: np.ndarray):
        if self.image_data is None or row_index >= self.image_data.shape[0]:
            return
        self.image_data[row_index, :] = values
        self.im.set_data(self.image_data)

        valid = ~np.isnan(self.image_data)
        if np.any(valid) and self.chk_autoscale.isChecked():
            lo, hi = float(np.min(self.image_data[valid])), float(np.max(self.image_data[valid]))
            self._set_slider_bounds(lo, hi)
            self.im.set_clim(vmin=lo, vmax=hi)
        self.canvas.draw_idle()

    def autoscale_now(self):
        if self.image_data is None:
            return
        valid = ~np.isnan(self.image_data)
        if not np.any(valid):
            return
        lo, hi = float(np.min(self.image_data[valid])), float(np.max(self.image_data[valid]))
        self._set_slider_bounds(lo, hi)
        self.im.set_clim(vmin=lo, vmax=hi)
        self.canvas.draw_idle()

    # -- 3D stack ---------------------------------------------------------
    def begin_stack(self, z_min: float, z_max: float):
        self._stack = []
        self._z_range = (z_min, z_max)
        if self.ax3d is not None:
            self.ax3d.clear()
            theme.style_axes3d(self.ax3d, self._dark)
            self.canvas.draw_idle()

    def add_slice(self, z_value: float):
        """Snapshot the current image as the slice at ``z_value`` and
        redraw the stack view."""
        if not self._stack_enabled or self.image_data is None:
            return
        self._stack.append((z_value, _downsample(self.image_data.copy())))
        self._redraw_stack()

    def _redraw_stack(self):
        if self.ax3d is None or not self._stack:
            return

        ax = self.ax3d
        ax.clear()

        all_vals = np.concatenate([s.ravel() for _, s in self._stack])
        all_vals = all_vals[~np.isnan(all_vals)]
        if all_vals.size == 0:
            return
        vmin, vmax = float(all_vals.min()), float(all_vals.max())
        if vmax <= vmin:
            vmax = vmin + 1e-12

        cmap = colormaps[self._current_cmap_name()]
        x0, x1, y0, y1 = self.extent_um

        for z_value, data in self._stack:
            d = np.nan_to_num(data, nan=vmin)
            ny, nx = d.shape
            xs, ys = np.meshgrid(np.linspace(x0, x1, nx), np.linspace(y0, y1, ny))
            zs = np.full_like(xs, z_value)
            face_colors = cmap((d - vmin) / (vmax - vmin))
            face_colors[..., 3] = 0.6
            ax.plot_surface(
                xs, ys, zs,
                rstride=1, cstride=1,
                facecolors=face_colors,
                shade=False, linewidth=0, antialiased=False,
            )

        ax.set_xlabel("X (μm)")
        ax.set_ylabel("Y (μm)")
        ax.set_zlabel("Z (V)")
        if self._z_range is not None:
            lo, hi = sorted(self._z_range)
            if hi > lo:
                ax.set_zlim(lo, hi)
        theme.style_axes3d(ax, self._dark)
        self.canvas.draw_idle()

    # -- contrast ---------------------------------------------------------
    def _set_slider_bounds(self, lo: float, hi: float):
        if hi <= lo:
            hi = lo + 1.0
        padding = (hi - lo) * 0.05
        self._data_bounds = (lo - padding, hi + padding)

        self.slider_min.blockSignals(True)
        self.slider_max.blockSignals(True)
        self.slider_min.setValue(self._value_to_slider(lo))
        self.slider_max.setValue(self._value_to_slider(hi))
        self.slider_min.blockSignals(False)
        self.slider_max.blockSignals(False)
        self.lbl_min.setText(f"{lo:.5f}")
        self.lbl_max.setText(f"{hi:.5f}")

    def _value_to_slider(self, value: float) -> int:
        lo, hi = self._data_bounds
        frac = 0.0 if hi == lo else (value - lo) / (hi - lo)
        return int(min(max(frac, 0.0), 1.0) * _SLIDER_STEPS)

    def _slider_to_value(self, slider_value: int) -> float:
        lo, hi = self._data_bounds
        return lo + (slider_value / _SLIDER_STEPS) * (hi - lo)

    def _on_slider_changed(self, _=None):
        v_min = self._slider_to_value(self.slider_min.value())
        v_max = self._slider_to_value(self.slider_max.value())
        if v_min >= v_max:
            return
        self.im.set_clim(vmin=v_min, vmax=v_max)
        self.lbl_min.setText(f"{v_min:.5f}")
        self.lbl_max.setText(f"{v_max:.5f}")
        self.canvas.draw_idle()

    def _on_colormap_changed(self, name: str):
        self.im.set_cmap(name)
        if self._stack:
            self._redraw_stack()
        self.canvas.draw_idle()

    def set_theme(self, dark: bool):
        self._dark = dark
        theme.style_figure(self.figure, self.ax, dark, self.cbar)
        if self.ax3d is not None:
            theme.style_axes3d(self.ax3d, dark)
        self.canvas.draw_idle()

    # -- interaction ------------------------------------------------------
    def _on_click(self, event):
        if event.dblclick and event.inaxes == self.ax and event.xdata is not None and event.ydata is not None:
            self.move_requested_um.emit(event.xdata, event.ydata)

    def _on_select_box(self, eclick, erelease):
        x1, y1 = eclick.xdata, eclick.ydata
        x2, y2 = erelease.xdata, erelease.ydata
        if None in (x1, y1, x2, y2):
            return
        self.region_selected_um.emit(min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2))


class PlotPanel(QWidget):
    region_selected_um = pyqtSignal(float, float, float, float)
    move_requested_um = pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dark = True
        self._views: dict[int, ChannelPlotView] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.lbl_slice = QLabel("")
        self.lbl_slice.setStyleSheet("font-weight: 600;")
        self.lbl_slice.setVisible(False)
        layout.addWidget(self.lbl_slice)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        self.lbl_empty = QLabel("Add and enable an input channel in the left panel to see its live image here.")
        self.lbl_empty.setProperty("muted", True)
        self.lbl_empty.setWordWrap(True)
        layout.addWidget(self.lbl_empty)
        self._update_empty_state()

    def _update_empty_state(self):
        self.lbl_empty.setVisible(self.tabs.count() == 0)
        self.tabs.setVisible(self.tabs.count() > 0)

    def set_theme(self, dark: bool):
        self._dark = dark
        for view in self._views.values():
            view.set_theme(dark)

    def set_slice_info(self, text: str):
        self.lbl_slice.setText(text)
        self.lbl_slice.setVisible(bool(text))

    def sync_channels(self, slots: list[ChannelSlot]) -> None:
        """Reconcile plot tabs with the given (enabled) channel slots,
        keyed by AI channel number."""
        wanted = {slot.number: slot for slot in slots}

        for number in list(self._views.keys()):
            if number not in wanted:
                view = self._views.pop(number)
                index = self.tabs.indexOf(view)
                if index >= 0:
                    self.tabs.removeTab(index)
                view.deleteLater()

        for number, slot in wanted.items():
            if number in self._views:
                index = self.tabs.indexOf(self._views[number])
                if index >= 0:
                    self.tabs.setTabText(index, slot.label)
                continue
            view = ChannelPlotView(number, slot.colormap, self._dark)
            view.region_selected_um.connect(self.region_selected_um.emit)
            view.move_requested_um.connect(self.move_requested_um.emit)
            self._views[number] = view
            self.tabs.addTab(view, slot.label)

        self._update_empty_state()

    def reset_for_scan(
        self,
        slots: list[ChannelSlot],
        x_points: int,
        y_points: int,
        extent_um,
        z_range: tuple[float, float] | None = None,
    ) -> None:
        self.sync_channels(slots)
        for slot in slots:
            view = self._views.get(slot.number)
            if view is None:
                continue
            view.set_stack_enabled(z_range is not None)
            view.reset_image((y_points, x_points), extent_um)
            if z_range is not None:
                view.begin_stack(*z_range)
        self.set_slice_info("")

    def clear_current_images(self) -> None:
        """Blank every channel's live 2D image (used between 3D slices)."""
        for view in self._views.values():
            view.reset_image(view.image_data.shape, view.extent_um)

    def add_slice(self, z_value: float) -> None:
        for view in self._views.values():
            view.add_slice(z_value)

    def update_line(self, channel_number: int, line_index: int, values: np.ndarray) -> None:
        view = self._views.get(channel_number)
        if view is not None:
            view.set_row(line_index, values)

    def channel_views(self):
        return dict(self._views)
