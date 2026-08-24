"""
run_gui.py

Entry point for the SpotCheck desktop application.

    python run_gui.py

Kept at the repository root (rather than inside the gui package) so that
PyInstaller has a plain top-level script to analyse, and so `core` and `gui`
both resolve as packages without any sys.path manipulation.
"""

from core import docscan
from gui.app_window import main

if __name__ == "__main__":
    # Page scanning runs across several processes. In a PyInstaller build a
    # worker process re-launches this executable, so without this call each
    # worker would start a second copy of the application instead of doing the
    # work it was handed. It must be the first thing that happens.
    docscan.support_frozen_build()
    main()
