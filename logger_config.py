"""
logger_config.py

Automated Runtime Logger & DLL Environment Manager for SpotCheck.
- Ensures all native DLL search paths (PyZbar, OpenCV, PyMuPDF) are registered.
- Automatically creates and manages timestamped & 'latest' log files in a dedicated 'logs' directory whenever SpotCheck starts.
- Captures all stdout, stderr, exceptions, and worker thread crashes to the log file.
- Provides UI helper functions to open the log file or log directory directly.
"""

import os
import sys
import time
import logging
import datetime
import traceback
import threading

_LOG_INITIALIZED = False
_CURRENT_LOG_FILE = None
_LOG_DIR = None
_ROOT_LOGGER = None


def _setup_dynamic_library_paths():
    """
    Ensure dynamic libraries (DLLs on Windows, .dylib on macOS, .so on Linux)
    are discoverable both in frozen (PyInstaller) and standard execution modes across all OS platforms.
    """
    candidate_dirs = []

    if getattr(sys, 'frozen', False):
        # Running as PyInstaller frozen package / executable
        base_dir = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
        exe_dir = os.path.dirname(sys.executable)
        candidate_dirs.extend([
            base_dir,
            os.path.join(base_dir, 'pyzbar'),
            exe_dir,
            os.path.join(exe_dir, '_internal'),
            os.path.join(exe_dir, '_internal', 'pyzbar')
        ])
    else:
        # Development / script mode
        app_dir = os.path.dirname(os.path.abspath(__file__))
        candidate_dirs.append(app_dir)
        try:
            import pyzbar
            pyzbar_dir = os.path.dirname(pyzbar.__file__)
            candidate_dirs.append(pyzbar_dir)
        except Exception:
            pass

    # Platform-specific search locations
    if sys.platform == 'darwin':
        # macOS Homebrew & standard library locations
        candidate_dirs.extend([
            '/opt/homebrew/lib',
            '/usr/local/lib',
            '/opt/homebrew/opt/zbar/lib',
            '/usr/local/opt/zbar/lib',
            '/opt/homebrew/opt/libiconv/lib',
            '/usr/local/opt/libiconv/lib'
        ])
    elif sys.platform.startswith('linux'):
        # Linux standard shared library locations
        candidate_dirs.extend([
            '/usr/lib',
            '/usr/local/lib',
            '/usr/lib/x86_64-linux-gnu',
            '/usr/lib64',
            '/usr/lib/aarch64-linux-gnu'
        ])

    for d in candidate_dirs:
        if os.path.isdir(d):
            # 1. Windows DLL search path (Python 3.8+)
            if sys.platform == 'win32' and hasattr(os, 'add_dll_directory'):
                try:
                    os.add_dll_directory(d)
                except Exception:
                    pass

            # 2. Update platform environment variables for C dynamic linkers
            env_vars = ['PATH']
            if sys.platform == 'darwin':
                env_vars.extend(['DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH'])
            elif sys.platform.startswith('linux'):
                env_vars.extend(['LD_LIBRARY_PATH'])

            for ev in env_vars:
                cur_val = os.environ.get(ev, '')
                if d not in cur_val:
                    os.environ[ev] = d + os.pathsep + cur_val if cur_val else d


# Alias for backward compatibility and immediate execution on module load
_setup_dll_directories = _setup_dynamic_library_paths
_setup_dynamic_library_paths()


class LogTeeStream:
    """
    A thread-safe stream wrapper that duplicates writes to the original stream
    (e.g., console / GUI redirector) and simultaneously appends to the log file.
    """
    def __init__(self, original_stream, log_file_path, prefix=""):
        self.original_stream = original_stream
        self.log_file_path = log_file_path
        self.prefix = prefix
        self._lock = threading.Lock()

    def write(self, text):
        if not text:
            return

        # 1. Forward to original stream if it exists
        if self.original_stream is not None:
            try:
                self.original_stream.write(text)
            except Exception:
                pass

        # 2. Append to log file
        if self.log_file_path:
            with self._lock:
                try:
                    with open(self.log_file_path, 'a', encoding='utf-8', errors='replace') as f:
                        f.write(text)
                except Exception:
                    pass

    def flush(self):
        if self.original_stream is not None and hasattr(self.original_stream, 'flush'):
            try:
                self.original_stream.flush()
            except Exception:
                pass

    @property
    def encoding(self):
        return 'utf-8'

    def isatty(self):
        if self.original_stream is not None and hasattr(self.original_stream, 'isatty'):
            return self.original_stream.isatty()
        return False


def _get_default_log_dir():
    """
    Determine the best writable directory for logs.
    1. Alongside the executable (or script in dev).
    2. Fallback to %USERPROFILE%/.spotcheck/logs if exe folder is read-only.
    """
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))

    target_dir = os.path.join(base, "logs")

    try:
        os.makedirs(target_dir, exist_ok=True)
        # Test write permissions
        test_file = os.path.join(target_dir, ".perm_test")
        with open(test_file, "w") as f:
            f.write("ok")
        os.remove(test_file)
        return target_dir
    except Exception:
        # Fallback to user home directory
        user_dir = os.path.join(os.path.expanduser("~"), ".spotcheck", "logs")
        os.makedirs(user_dir, exist_ok=True)
        return user_dir


def init_logging():
    """
    Initialize runtime logging for SpotCheck.
    - Creates a new timestamped log file for each run: logs/spotcheck_YYYY-MM-DD_HH-MM-SS.log
    - Updates logs/spotcheck_latest.log
    - Hooks sys.stdout, sys.stderr, sys.excepthook, and threading.excepthook
    - Logs detailed startup environment specs.
    """
    global _LOG_INITIALIZED, _CURRENT_LOG_FILE, _LOG_DIR, _ROOT_LOGGER

    if _LOG_INITIALIZED:
        return _CURRENT_LOG_FILE

    _LOG_DIR = _get_default_log_dir()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    _CURRENT_LOG_FILE = os.path.join(_LOG_DIR, f"spotcheck_{now_str}.log")
    latest_log_file = os.path.join(_LOG_DIR, "spotcheck_latest.log")

    # Configure Python root logger
    _ROOT_LOGGER = logging.getLogger("SpotCheck")
    _ROOT_LOGGER.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # File Handler
    file_handler = logging.FileHandler(_CURRENT_LOG_FILE, mode="a", encoding="utf-8", errors="replace")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    _ROOT_LOGGER.addHandler(file_handler)

    # Also attach file handler to the root logger to capture library logs
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger().addHandler(file_handler)

    # Wrap stdout and stderr with LogTeeStream
    orig_stdout = sys.stdout
    orig_stderr = sys.stderr

    sys.stdout = LogTeeStream(orig_stdout, _CURRENT_LOG_FILE)
    sys.stderr = LogTeeStream(orig_stderr, _CURRENT_LOG_FILE, prefix="[STDERR] ")

    # Update latest log pointer / copy
    try:
        with open(latest_log_file, "w", encoding="utf-8") as lf:
            lf.write(f"SpotCheck Latest Log Session: {now_str}\n")
            lf.write(f"Active Session Log: {_CURRENT_LOG_FILE}\n")
            lf.write("=" * 80 + "\n\n")
    except Exception:
        pass

    # Exception Hook for uncaught main thread crashes
    def uncaught_exception_handler(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        tb_lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
        tb_text = "".join(tb_lines)

        msg = f"\n{'=' * 80}\n[CRITICAL UNCAUGHT EXCEPTION]\n{tb_text}{'=' * 80}\n"
        sys.stderr.write(msg)
        sys.stderr.flush()

        # If tkinter is loaded and window exists, show error dialog
        try:
            import tkinter.messagebox as mb
            mb.showerror(
                "SpotCheck - Fatal Error",
                f"A fatal error occurred:\n\n{exc_value}\n\nDetailed crash logs have been saved to:\n{_CURRENT_LOG_FILE}"
            )
        except Exception:
            pass

    sys.excepthook = uncaught_exception_handler

    # Threading exception hook (Python 3.8+)
    if hasattr(threading, 'excepthook'):
        def thread_exception_handler(args):
            tb_lines = traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
            tb_text = "".join(tb_lines)
            msg = f"\n{'=' * 80}\n[THREAD EXCEPTION in {args.thread.name}]\n{tb_text}{'=' * 80}\n"
            sys.stderr.write(msg)
            sys.stderr.flush()

        threading.excepthook = thread_exception_handler

    # Write initial startup banner
    _log_startup_diagnostics()
    _LOG_INITIALIZED = True
    return _CURRENT_LOG_FILE


def _log_startup_diagnostics():
    """Record startup diagnostic info into the log file."""
    is_frozen = getattr(sys, 'frozen', False)
    exe_path = sys.executable if is_frozen else os.path.abspath(__file__)
    
    print("=" * 80)
    print(f"SPOTCHECK LOG SESSION INITIALIZED: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    print(f"Application     : SpotCheck - Xylem PDF Quality & Visual Inspection Engine")
    print(f"Mode            : {'Frozen Executable (PyInstaller)' if is_frozen else 'Development / Python Script'}")
    print(f"Executable/Entry: {exe_path}")
    print(f"Current Dir     : {os.getcwd()}")
    print(f"Log File        : {_CURRENT_LOG_FILE}")
    print(f"Python Version  : {sys.version}")
    print(f"Platform        : {sys.platform} ({os.name})")

    if is_frozen:
        print(f"PyInstaller Dir : {getattr(sys, '_MEIPASS', 'N/A')}")

    # Check key dependencies
    print("-" * 80)
    print("DEPENDENCY STATUS:")

    # 1. PyZbar
    try:
        import pyzbar
        from pyzbar.pyzbar import decode as _
        print(f"  [+] pyzbar       : Available ({pyzbar.__file__})")
    except Exception as e:
        print(f"  [-] pyzbar       : Failed to load ({e})")

    # 2. PyMuPDF / fitz
    try:
        import pymupdf
        print(f"  [+] pymupdf      : Available ({pymupdf.__version__})")
    except Exception as e:
        print(f"  [-] pymupdf      : Failed to load ({e})")

    # 3. OpenCV
    try:
        import cv2
        print(f"  [+] cv2 (OpenCV) : Available ({cv2.__version__})")
    except Exception as e:
        print(f"  [-] cv2 (OpenCV) : Failed to load ({e})")

    # 4. CustomTkinter
    try:
        import customtkinter as ctk
        print(f"  [+] customtkinter: Available ({ctk.__version__})")
    except Exception as e:
        print(f"  [-] customtkinter: Failed to load ({e})")

    # 5. pdfplumber
    try:
        import pdfplumber
        print(f"  [+] pdfplumber   : Available ({pdfplumber.__version__})")
    except Exception as e:
        print(f"  [-] pdfplumber   : Failed to load ({e})")

    # 6. openpyxl
    try:
        import openpyxl
        print(f"  [+] openpyxl     : Available ({openpyxl.__version__})")
    except Exception as e:
        print(f"  [-] openpyxl     : Failed to load ({e})")

    print("=" * 80 + "\n")


def get_current_log_path():
    """Return absolute path of the current session's log file."""
    global _CURRENT_LOG_FILE
    if not _CURRENT_LOG_FILE:
        init_logging()
    return _CURRENT_LOG_FILE


def get_log_dir():
    """Return the absolute path of the logs directory."""
    global _LOG_DIR
    if not _LOG_DIR:
        init_logging()
    return _LOG_DIR


def open_current_log():
    """Open the current log file in the default system text editor (Notepad, TextEdit, etc.)."""
    log_path = get_current_log_path()
    if log_path and os.path.exists(log_path):
        try:
            if sys.platform == 'win32':
                os.startfile(log_path)
            elif sys.platform == 'darwin':
                import subprocess
                subprocess.Popen(['open', log_path])
            else:
                import subprocess
                subprocess.Popen(['xdg-open', log_path])
            return True
        except Exception as e:
            print(f"[Error] Failed to open log file: {e}")
            return False
    return False


def open_log_folder():
    """Open the logs directory in system file explorer (Explorer, Finder, Nautilus, etc.)."""
    log_dir = get_log_dir()
    if log_dir and os.path.exists(log_dir):
        try:
            if sys.platform == 'win32':
                os.startfile(log_dir)
            elif sys.platform == 'darwin':
                import subprocess
                subprocess.Popen(['open', log_dir])
            else:
                import subprocess
                subprocess.Popen(['xdg-open', log_dir])
            return True
        except Exception as e:
            print(f"[Error] Failed to open log folder: {e}")
            return False
    return False

