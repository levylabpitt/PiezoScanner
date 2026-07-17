"""Entry point: ``python -m piezoscanner`` or the installed ``piezoscanner``
console script."""

from __future__ import annotations

import os
import sys

# Make sure matplotlib's Qt backend picks PyQt6 rather than guessing/erroring
# on a machine that also has PyQt5/PySide installed.
os.environ.setdefault("QT_API", "pyqt6")


def main() -> int:
    from PyQt6.QtWidgets import QApplication

    from .gui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("PiezoScanner")
    app.setOrganizationName("LevyLab")

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
