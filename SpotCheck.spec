# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs, collect_data_files

block_cipher = None

# Base list of hidden imports
all_hiddenimports = [
    'fitz',
    'pymupdf',
    'pdfplumber',
    'cv2',
    'numpy',
    'pyzbar',
    'pyzbar.pyzbar',
    'pyzbar.zbar_library',
    'pyzbar.wrapper',
    'pyzbar.locations',
    'openpyxl',
    'customtkinter',
    'PIL',
    'PIL.Image',
    'PIL.ImageTk',
    'logger_config',
    'settings',
    'core',
    'core.pipeline',
    'core.toc',
    'core.barcode_qr',
    'core.crop_images',
    'core.compare_crops',
    'core.image_counts',
    'core.margins',
    'core.templates',
    'core.page_diff',
    'core.region_engine',
    'gui',
    'gui.theme',
    'gui.app_window',
    'gui.region_dialog',
    'gui.comparison_gallery',
    'gui.page_diff_view',
]

all_datas = []
all_binaries = []

# 1. PyZbar Collection (Critical for barcode & QR detection)
try:
    pz_datas, pz_binaries, pz_hidden = collect_all('pyzbar')
    all_datas.extend(pz_datas)
    all_binaries.extend(pz_binaries)
    all_hiddenimports.extend(pz_hidden)
except Exception as e:
    print(f"[SPEC WARNING] PyZbar collect_all failed: {e}")

# Explicitly find and include PyZbar DLLs in both 'pyzbar' and '.' directories
try:
    import pyzbar
    pyzbar_dir = os.path.dirname(pyzbar.__file__)
    for fname in os.listdir(pyzbar_dir):
        if fname.lower().endswith('.dll'):
            fpath = os.path.join(pyzbar_dir, fname)
            all_binaries.append((fpath, 'pyzbar'))
            all_binaries.append((fpath, '.'))
            all_datas.append((fpath, 'pyzbar'))
            all_datas.append((fpath, '.'))
except Exception as e:
    print(f"[SPEC WARNING] PyZbar DLL search failed: {e}")

# 2. CustomTkinter Collection (Theme assets, fonts, json configs)
try:
    ctk_datas, ctk_binaries, ctk_hidden = collect_all('customtkinter')
    all_datas.extend(ctk_datas)
    all_binaries.extend(ctk_binaries)
    all_hiddenimports.extend(ctk_hidden)
except Exception as e:
    print(f"[SPEC WARNING] CustomTkinter collect_all failed: {e}")

# 3. PyMuPDF Collection
try:
    mupdf_datas, mupdf_binaries, mupdf_hidden = collect_all('pymupdf')
    all_datas.extend(mupdf_datas)
    all_binaries.extend(mupdf_binaries)
    all_hiddenimports.extend(mupdf_hidden)
except Exception as e:
    print(f"[SPEC WARNING] PyMuPDF collect_all failed: {e}")

# 4. OpenCV (cv2) Collection
try:
    cv2_datas, cv2_binaries, cv2_hidden = collect_all('cv2')
    all_datas.extend(cv2_datas)
    all_binaries.extend(cv2_binaries)
    all_hiddenimports.extend(cv2_hidden)
except Exception as e:
    print(f"[SPEC WARNING] OpenCV collect_all failed: {e}")

# 5. OpenPyXL Collection
try:
    xl_datas, xl_binaries, xl_hidden = collect_all('openpyxl')
    all_datas.extend(xl_datas)
    all_binaries.extend(xl_binaries)
    all_hiddenimports.extend(xl_hidden)
except Exception as e:
    print(f"[SPEC WARNING] openpyxl collect_all failed: {e}")

# Remove duplicates while preserving order
def deduplicate_list_of_tuples(items):
    seen = set()
    result = []
    for item in items:
        key = (item[0], item[1]) if isinstance(item, (list, tuple)) else item
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result

all_datas = deduplicate_list_of_tuples(all_datas)
all_binaries = deduplicate_list_of_tuples(all_binaries)
all_hiddenimports = list(dict.fromkeys(all_hiddenimports))

a = Analysis(
    ['run_gui.py'],
    pathex=['.'],
    binaries=all_binaries,
    datas=all_datas,
    hiddenimports=all_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SpotCheck',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # Set to False for clean GUI mode (no black cmd window)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='SpotCheck',
)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='SpotCheck.app',
        icon=None,
        bundle_identifier='com.xylem.spotcheck',
    )

