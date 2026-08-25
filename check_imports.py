"""
check_imports.py

Import every module the packaged application needs, and say which ones fail.

Run by build_exe.bat before PyInstaller starts. Three minutes of packaging is a
slow way to find out that a module has a typo in it or that a dependency landed
in a different interpreter than the one doing the build; this takes about two
seconds and fails with the name of the thing that is wrong.

It also runs on its own:

    python check_imports.py

Exits 0 when everything imports, 1 when anything does not.
"""

import importlib
import sys

# Everything reachable from run_gui.py. The GUI tabs are listed explicitly
# because each one is imported inside a try/except in app_window: a module that
# fails to import does not crash the application, it just removes that tab and
# leaves a line in the log. Which is exactly the kind of thing that ships.
MODULES = [
    "logger_config",
    "settings",
    "core.pipeline",
    "core.toc",
    "core.barcode_qr",
    "core.crop_images",
    "core.compare_crops",
    "core.image_counts",
    "core.docscan",
    "core.margins",
    "core.metadata",
    "core.page_diff",
    "core.templates",
    "core.region_engine",
    "core.text_overlap",
    "core.untranslated",
    "gui.theme",
    "gui.app_window",
    "gui.region_dialog",
    "gui.comparison_gallery",
    "gui.page_diff_view",
    "gui.metadata_tab",
    "gui.text_checks_tab",
]

# Third-party packages, checked separately so a missing dependency is reported
# as a missing dependency rather than as a broken module of ours.
PACKAGES = [
    ("pymupdf", True),
    ("fitz", True),
    ("cv2", True),
    ("numpy", True),
    ("PIL", True),
    ("openpyxl", True),
    ("customtkinter", True),
    ("tkinter", True),
    ("pdfplumber", True),
    # Optional: without it barcodes are still located, just not decoded.
    ("pyzbar.pyzbar", False),
]


def main():
    failures, warnings = [], []

    print("Third-party packages:")
    for name, required in PACKAGES:
        try:
            importlib.import_module(name)
            print(f"  [+] {name}")
        except Exception as e:
            if required:
                print(f"  [FAIL] {name}: {e}")
                failures.append(name)
            else:
                print(f"  [ ! ] {name}: {e}  (optional)")
                warnings.append(name)

    print("\nApplication modules:")
    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception as e:
            print(f"  [FAIL] {name}: {e}")
            failures.append(name)
    if not failures:
        print(f"  [+] all {len(MODULES)} imported")

    print()
    if failures:
        print(f"{len(failures)} import(s) failed - packaging this would ship a "
              f"build with those pieces missing.")
        return 1
    if warnings:
        print(f"Everything required imported. Optional: {', '.join(warnings)} "
              f"unavailable - barcode DECODING will be off in the build.")
        return 0
    print("Everything imported.")
    return 0


if __name__ == "__main__":
    sys.exit(main())