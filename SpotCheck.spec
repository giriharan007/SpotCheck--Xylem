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
    # The page scan runs across worker processes. A worker re-launches this
    # executable and imports the worker function's module by name, and the
    # barcode import inside it is deferred, so both are named here rather than
    # left to static analysis.
    'multiprocessing',
    'multiprocessing.spawn',
    'concurrent.futures',
    'concurrent.futures.process',
    'core.docscan',
    'core.barcode_qr',
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
    'core.metadata',
    'core.templates',
    'core.page_diff',
    'core.region_engine',
    'core.text_overlap',
    'core.untranslated',
    'core.margin_overflow',
    'gui',
    'gui.theme',
    'gui.app_window',
    'gui.region_dialog',
    'gui.comparison_gallery',
    'gui.page_diff_view',
    'gui.metadata_tab',
    # Every tab is imported inside a try/except in _build_*_tab, so a module
    # the analysis missed would not crash the build OR the application - the
    # tab would just quietly say it was unavailable, and the feature would be
    # gone from the shipped exe with nothing in the log. Naming them costs
    # nothing and removes that failure mode.
    'gui.text_checks_tab',
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
#
# collect_all('cv2') pulls in the entire opencv-python wheel, including two
# pieces this app never uses and that account for most of its weight:
#   - opencv_videoio_ffmpeg*.dll (~28 MB) - the FFmpeg backend behind
#     cv2.VideoCapture / cv2.VideoWriter. Nothing here reads or writes video;
#     every cv2 call in this codebase is image-array template matching, ink
#     density, and colour conversion (grep for VideoCapture/VideoWriter turns
#     up nothing).
#   - cv2/data/*.xml (~10 MB) - the bundled Haar cascade classifiers for face,
#     eye and body detection. Nothing here uses CascadeClassifier either; the
#     manuals this tool inspects are PDFs, not photographs of people.
# Cut, that is about 38 MB of dead weight this build was carrying for
# features it never calls. If a future check ever needs real video decoding
# or face detection, this filter is exactly where to stop excluding them.
try:
    cv2_datas, cv2_binaries, cv2_hidden = collect_all('cv2')

    def _cv2_is_unused_bloat(path):
        # Backslash or forward slash depending on the host OS running the
        # build - normalise before matching rather than relying on
        # os.path.join to reproduce whatever separator the path actually uses.
        lower = path.lower().replace('\\', '/')
        return ('opencv_videoio_ffmpeg' in lower and lower.endswith('.dll')) or \
               ('cv2/data/' in lower and lower.endswith('.xml'))

    cv2_binaries = [b for b in cv2_binaries if not _cv2_is_unused_bloat(b[0])]
    cv2_datas = [d for d in cv2_datas if not _cv2_is_unused_bloat(d[0])]

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
    # UPX is OFF on purpose. It mangles some Windows DLLs - libzbar-64.dll and
    # the VC runtime among them - and the failure does not appear at build
    # time: the exe is produced, looks fine, and then dies on a machine that is
    # not the one it was built on. The folder is a few tens of MB larger. That
    # is the better trade for something being handed to other people.
    upx=False,
    # A GUI build with no console: a crash before the window opens leaves the
    # user with nothing at all. Set SPOTCHECK_DEBUG_BUILD=1 before building to
    # get a console window that shows the traceback.
    console=bool(os.environ.get('SPOTCHECK_DEBUG_BUILD')),
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
    upx=False,
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