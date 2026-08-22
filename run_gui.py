"""
run_gui.py

Entry point for the SpotCheck desktop application.

    python run_gui.py

Kept at the repository root (rather than inside the gui package) so that
PyInstaller has a plain top-level script to analyse, and so `core` and `gui`
both resolve as packages without any sys.path manipulation.
"""

from gui.app_window import main

if __name__ == "__main__":
    main()
