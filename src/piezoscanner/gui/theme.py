"""Modern flat theme for the scanner GUI: a Qt stylesheet plus matching
matplotlib figure colors, so plots don't look like a different app bolted
onto the controls."""

from __future__ import annotations

ACCENT = "#3d8bfd"
ACCENT_HOVER = "#5c9fff"
ACCENT_PRESSED = "#2f70d6"
DANGER = "#e5484d"
DANGER_HOVER = "#f2696d"

_DARK_BG = "#1e2124"
_DARK_PANEL = "#26292d"
_DARK_PANEL_ALT = "#2e3236"
_DARK_BORDER = "#3a3e43"
_DARK_TEXT = "#e6e8eb"
_DARK_TEXT_MUTED = "#9aa1a9"

_LIGHT_BG = "#f4f5f7"
_LIGHT_PANEL = "#ffffff"
_LIGHT_PANEL_ALT = "#eceef1"
_LIGHT_BORDER = "#d6d9de"
_LIGHT_TEXT = "#1c1f23"
_LIGHT_TEXT_MUTED = "#5b6169"


def _build_qss(*, bg, panel, panel_alt, border, text, text_muted) -> str:
    return f"""
    * {{
        font-family: "Segoe UI", "Inter", sans-serif;
        font-size: 10.5pt;
        color: {text};
    }}
    QMainWindow, QDialog {{ background: {bg}; }}
    QWidget {{ background: transparent; }}

    QGroupBox {{
        background: {panel};
        border: 1px solid {border};
        border-radius: 8px;
        margin-top: 14px;
        padding: 10px 8px 8px 8px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 6px;
        color: {text_muted};
    }}

    QLabel {{ background: transparent; }}
    QLabel[muted="true"] {{ color: {text_muted}; }}

    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
        background: {panel_alt};
        border: 1px solid {border};
        border-radius: 5px;
        padding: 4px 6px;
        selection-background-color: {ACCENT};
    }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
        border: 1px solid {ACCENT};
    }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox QAbstractItemView {{
        background: {panel_alt};
        border: 1px solid {border};
        selection-background-color: {ACCENT};
    }}

    QPushButton {{
        background: {panel_alt};
        border: 1px solid {border};
        border-radius: 6px;
        padding: 6px 12px;
    }}
    QPushButton:hover {{ border: 1px solid {ACCENT}; }}
    QPushButton:pressed {{ background: {border}; }}
    QPushButton:disabled {{ color: {text_muted}; }}

    QPushButton#primaryAction {{
        background: {ACCENT};
        border: 1px solid {ACCENT};
        color: white;
        font-weight: 600;
        padding: 9px 12px;
        border-radius: 7px;
    }}
    QPushButton#primaryAction:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
    QPushButton#primaryAction:pressed {{ background: {ACCENT_PRESSED}; }}

    QPushButton#dangerAction {{
        background: {DANGER};
        border: 1px solid {DANGER};
        color: white;
        font-weight: 600;
        padding: 9px 12px;
        border-radius: 7px;
    }}
    QPushButton#dangerAction:hover {{ background: {DANGER_HOVER}; border-color: {DANGER_HOVER}; }}

    QPushButton#chipButton {{
        border-radius: 12px;
        padding: 3px 10px;
        background: {panel_alt};
    }}

    QCheckBox::indicator {{
        width: 15px; height: 15px;
        border-radius: 4px;
        border: 1px solid {border};
        background: {panel_alt};
    }}
    QCheckBox::indicator:checked {{
        background: {ACCENT};
        border: 1px solid {ACCENT};
    }}

    QTabWidget::pane {{
        border: 1px solid {border};
        border-radius: 8px;
        background: {panel};
        top: -1px;
    }}
    QTabBar::tab {{
        background: transparent;
        padding: 7px 16px;
        margin-right: 2px;
        border-top-left-radius: 6px;
        border-top-right-radius: 6px;
        color: {text_muted};
    }}
    QTabBar::tab:selected {{
        background: {panel};
        color: {text};
        font-weight: 600;
        border: 1px solid {border};
        border-bottom: none;
    }}
    QTabBar::tab:hover {{ color: {text}; }}

    QScrollArea {{ border: none; }}
    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: {border};
        border-radius: 5px;
        min-height: 24px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

    QProgressBar {{
        background: {panel_alt};
        border: 1px solid {border};
        border-radius: 6px;
        text-align: center;
        height: 16px;
    }}
    QProgressBar::chunk {{
        background: {ACCENT};
        border-radius: 5px;
    }}

    QStatusBar {{
        background: {panel};
        border-top: 1px solid {border};
    }}
    QMenuBar {{ background: {panel}; border-bottom: 1px solid {border}; }}
    QMenuBar::item:selected {{ background: {panel_alt}; }}
    QMenu {{ background: {panel}; border: 1px solid {border}; }}
    QMenu::item:selected {{ background: {ACCENT}; color: white; }}

    QSlider::groove:horizontal {{
        height: 4px;
        background: {border};
        border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background: {ACCENT};
        width: 15px;
        margin: -6px 0;
        border-radius: 7px;
    }}

    QToolTip {{
        background: {panel_alt};
        color: {text};
        border: 1px solid {border};
        padding: 4px;
    }}
    """


DARK_QSS = _build_qss(
    bg=_DARK_BG, panel=_DARK_PANEL, panel_alt=_DARK_PANEL_ALT,
    border=_DARK_BORDER, text=_DARK_TEXT, text_muted=_DARK_TEXT_MUTED,
)

LIGHT_QSS = _build_qss(
    bg=_LIGHT_BG, panel=_LIGHT_PANEL, panel_alt=_LIGHT_PANEL_ALT,
    border=_LIGHT_BORDER, text=_LIGHT_TEXT, text_muted=_LIGHT_TEXT_MUTED,
)


def matplotlib_colors(dark: bool) -> dict:
    """Colors to apply to a Figure/Axes so plots match the active theme."""
    if dark:
        return dict(
            figure_bg=_DARK_PANEL,
            axes_bg=_DARK_PANEL,
            text=_DARK_TEXT,
            grid=_DARK_BORDER,
        )
    return dict(
        figure_bg=_LIGHT_PANEL,
        axes_bg=_LIGHT_PANEL,
        text=_LIGHT_TEXT,
        grid=_LIGHT_BORDER,
    )


def style_figure(fig, ax, dark: bool, cbar=None) -> None:
    """Apply theme colors to a matplotlib Figure/Axes (and optionally a
    Colorbar) pair in place."""
    colors = matplotlib_colors(dark)
    fig.patch.set_facecolor(colors["figure_bg"])
    ax.set_facecolor(colors["axes_bg"])
    ax.tick_params(colors=colors["text"])
    ax.xaxis.label.set_color(colors["text"])
    ax.yaxis.label.set_color(colors["text"])
    ax.title.set_color(colors["text"])
    for spine in ax.spines.values():
        spine.set_color(colors["grid"])

    if cbar is not None:
        cbar.ax.yaxis.label.set_color(colors["text"])
        cbar.ax.tick_params(colors=colors["text"])
        cbar.outline.set_edgecolor(colors["grid"])


def style_axes3d(ax, dark: bool) -> None:
    """Apply theme colors to a mplot3d Axes3D in place."""
    colors = matplotlib_colors(dark)
    ax.set_facecolor((0, 0, 0, 0))
    for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
        axis.label.set_color(colors["text"])
        axis.set_pane_color((0, 0, 0, 0))
    ax.tick_params(colors=colors["text"])
