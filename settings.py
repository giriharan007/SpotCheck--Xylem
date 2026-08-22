"""
settings.py

Small persistent store for the paths the user configured last time.

Sits alongside logger_config.py rather than inside core/ or gui/: like logging,
this is cross-cutting application infrastructure, not inspection logic and not a
widget.

The file lands next to the application when that location is writable, and falls
back to the user's profile otherwise — the same strategy logger_config uses for
its logs, so a copy installed under Program Files still remembers its settings.

Everything here is best-effort. A missing, unreadable or corrupt settings file
must never stop the application from starting, so every operation degrades to
"no stored settings" rather than raising.
"""

import json
import os
import sys

APP_DIR_NAME = ".spotcheck"
SETTINGS_FILENAME = "settings.json"

# Only these keys are persisted. An unknown key in the file is ignored, so a
# hand-edited or older file can never inject unexpected state into the app.
ALLOWED_KEYS = ("english_pdf", "translated_dir", "output_dir", "template", "margins")

# Most settings are paths, so string is the normal type and anything else is
# rejected. Margins are the exception: four numbers that belong together, and
# splitting them across four keys would let them get out of step.
DICT_KEYS = ("margins",)


def _acceptable(key, value):
    if key in DICT_KEYS:
        return isinstance(value, dict)
    return isinstance(value, str)

_settings_path = None


def _candidate_dirs():
    """Where to try to keep settings, best first."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return [base, os.path.join(os.path.expanduser("~"), APP_DIR_NAME)]


def _writable(directory):
    try:
        os.makedirs(directory, exist_ok=True)
        probe = os.path.join(directory, ".perm_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


def get_settings_path():
    """Resolve (and remember) the settings file location."""
    global _settings_path
    if _settings_path:
        return _settings_path
    for d in _candidate_dirs():
        if _writable(d):
            _settings_path = os.path.join(d, SETTINGS_FILENAME)
            return _settings_path
    # Nowhere writable: still return a path so callers can log it; writes fail quietly.
    _settings_path = os.path.join(_candidate_dirs()[-1], SETTINGS_FILENAME)
    return _settings_path


def load():
    """Return the stored settings as a dict. Never raises."""
    path = get_settings_path()
    try:
        if not os.path.isfile(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if k in ALLOWED_KEYS and _acceptable(k, v)}
    except Exception as e:
        print(f"[Settings] Could not read {path}: {e}")
        return {}


def save(values):
    """
    Merge `values` into the stored settings and write them out.

    Returns True on success. Never raises — failing to remember a path is not a
    reason to interrupt the user.
    """
    path = get_settings_path()
    data = load()
    data.update({k: v for k, v in (values or {}).items()
                 if k in ALLOWED_KEYS and _acceptable(k, v)})
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)          # atomic, so a crash cannot truncate the file
        return True
    except Exception as e:
        print(f"[Settings] Could not write {path}: {e}")
        return False


def load_paths():
    """
    The remembered paths, filtered to those that still exist.

    A folder that has since been deleted or a drive that is not mounted would
    otherwise come back as a pre-filled path that fails the moment the user
    presses Run, so stale entries are dropped and reported instead.
    """
    data = load()
    out = {}
    if data.get("template"):
        out["template"] = data["template"]
    if isinstance(data.get("margins"), dict):
        out["margins"] = data["margins"]
    for key, check in (("english_pdf", os.path.isfile),
                       ("translated_dir", os.path.exists),
                       ("output_dir", os.path.isdir)):
        val = (data.get(key) or "").strip()
        if not val:
            continue
        if check(val):
            out[key] = val
        else:
            print(f"[Settings] Ignoring remembered {key}, no longer present: {val}")
    return out


def save_paths(english_pdf=None, translated_dir=None, output_dir=None):
    """Remember the paths currently in use. Blank values are left unchanged."""
    values = {}
    if english_pdf:
        values["english_pdf"] = os.path.abspath(english_pdf)
    if translated_dir:
        values["translated_dir"] = os.path.abspath(translated_dir)
    if output_dir:
        values["output_dir"] = os.path.abspath(output_dir)
    return save(values) if values else False
