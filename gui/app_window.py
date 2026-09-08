"""
gui/app_window.py

Main SpotCheck desktop window.

Presentation layer only: the inspection itself is executed by core.pipeline on a
worker thread, and the Region Inspector lives in gui.region_dialog. All colors
and fonts come from gui.theme, so the two windows can no longer drift apart.
"""

import os
import re
import sys
import shutil
import threading
import queue
import traceback

# ──────────────────────────────────────────────────────────────
# Runtime logger & native DLL search paths.
# MUST run before any third-party or sub-module imports.
# ──────────────────────────────────────────────────────────────
import logger_config
logger_config.init_logging()

import settings
from core import templates as templates_store
from core import margins as page_margins


def _ensure_utf8_console():
    """Reconfigure stdout/stderr to UTF-8 if they exist and support it."""
    for stream_name in ('stdout', 'stderr'):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass


_ensure_utf8_console()


try:
    import customtkinter as ctk
    HAS_CTK = True
except ImportError:
    HAS_CTK = False

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

if HAS_CTK:
    ctk.set_appearance_mode("Light")
    ctk.set_default_color_theme("blue")

from core import pipeline as spotcheck_engine

from gui import theme
from gui.theme import (
    XYLEM_BLUE,
    DEPENDABLE_BLUE,
    CLARITY_BLUE,
    DYNAMIC_GREEN,
    INSPIRED_TEAL,
    UPLIFTING_AQUA,
    RESILIENT_PURPLE,
    VIVID_MAGENTA,
    RADIANT_ORANGE,
    NEUTRAL_BLACK,
    NEUTRAL_DARK_GR,
    NEUTRAL_MED_GR,
    NEUTRAL_LIGHT_GR,
    NEUTRAL_WHITE,
    UI_BG_CANVAS,
    UI_CARD_BG,
    UI_CARD_WELL,
    UI_BORDER,
    UI_HOVER_BLUE,
    UI_DARK_HOVER,
)

from gui.theme import FONT_FAMILY_PREFERRED, FONT_FAMILY_FALLBACK

# Resolved once, shared with the Region Inspector.
FONT_FAMILY = theme.resolve_font_family()

# The results folder, created inside the batch folder so a run's report and
# evidence land right beside the PDFs they were measured against.
OUTPUT_SUBDIR_NAME = "SpotCheck_Output"

# Tab labels (also used as CTkTabview keys)
TAB_INSPECTION = "  Inspection  "
TAB_REGION = "  Region Inspector  "
TAB_METADATA = "  Meta Data  "
TAB_COMPARISONS = "  Review  "
TAB_TEXT_CHECKS = "  Text Checks  "


# ──────────────────────────────────────────────────────────────
# Fix 1 (continued): Safe TextRedirector with encoding guard & file logging
# ──────────────────────────────────────────────────────────────
class TextRedirector:
    """
    Redirects stdout/stderr streams to a thread-safe GUI text queue AND
    appends all output to the active SpotCheck log file.
    """
    def __init__(self, text_queue):
        self.text_queue = text_queue

    def write(self, string):
        if string:
            # Ensure we only enqueue clean str objects
            if isinstance(string, bytes):
                string = string.decode('utf-8', errors='replace')
            self.text_queue.put(string)
            # Simultaneously write to the session log file
            log_path = logger_config.get_current_log_path()
            if log_path:
                try:
                    with open(log_path, 'a', encoding='utf-8', errors='replace') as f:
                        f.write(string)
                except Exception:
                    pass

    def flush(self):
        pass

    @property
    def encoding(self):
        return 'utf-8'


class SpotCheckApp(ctk.CTk if HAS_CTK else tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("SpotCheck - Xylem PDF Quality & Visual Inspection Engine")
        self._fit_to_screen()

        if HAS_CTK:
            self.configure(fg_color=UI_BG_CANVAS)
        else:
            self.configure(bg=UI_BG_CANVAS)

        self.text_queue = queue.Queue()
        self.done_queue = queue.Queue()
        self.progress_queue = queue.Queue()
        self.is_running = False
        self.output_excel_path = None
        self.output_dir_path = None

        # Fix 6: Track browse buttons for disable/enable during inspection
        self._browse_buttons = []

        # The report and its evidence are written inside the batch folder by
        # default - one folder holds the master, its translations, and their
        # results - so nothing is scattered. _output_user_set flips true once the
        # user browses to a location of their own, after which we stop moving it;
        # _output_batch_folder remembers the folder we last derived from, so
        # typing a path by hand isn't clobbered while the master is unchanged.
        self._output_user_set = False
        self._output_batch_folder = None

        # Whatever was configured last time, so the user does not re-pick the
        # same three paths on every launch. Stale entries are dropped by
        # load_paths(), so a deleted folder falls back to the built-in default.
        self._remembered = settings.load_paths()
        if self._remembered:
            print(f"[Settings] Restored {len(self._remembered)} path(s) from "
                  f"{settings.get_settings_path()}")

        self.region_inspector = None
        self.metadata_tab = None
        self.comparison_gallery = None
        self.tabview = None
        self.last_run_results = None

        # With CustomTkinter present, the application is a two-tab window:
        # "Inspection" (this file) and "Region Inspector" (gui.region_dialog),
        # both children of the same window rather than separate top-levels.
        # The plain-Tk fallback has no tabs and no inspector, since the
        # inspector itself requires CustomTkinter.
        if HAS_CTK:
            # Colours come from theme.tabview_colors(): the tab strip shares one
            # text colour across selected and unselected tabs, so both fills have
            # to carry it. They used to be white-on-near-white.
            self.tabview = ctk.CTkTabview(
                self,
                fg_color=UI_BG_CANVAS,
                anchor="w",
                **theme.tabview_colors(),
            )
            self.tabview.pack(fill="both", expand=True, padx=10, pady=(8, 10))
            # Left to right in the order the work happens: configure the run,
            # set up the stylesheet, read the results, look at the documents.
            self._tab_inspection = self.tabview.add(TAB_INSPECTION)
            self._tab_region = self.tabview.add(TAB_REGION)
            self._tab_comparisons = self.tabview.add(TAB_COMPARISONS)
            self._tab_metadata = self.tabview.add(TAB_METADATA)
            self._body = self._tab_inspection
        else:
            self._body = self

        self._build_ui()

        if HAS_CTK:
            self._build_region_tab()
            self.text_checks_tab = None
            self._build_metadata_tab()
            self._build_comparisons_tab()
            # Resolve the batch from the remembered master PDF: its folder is the
            # batch, the PDFs beside it the translations. Done before the path
            # watcher so the batch is in place when the inspector and metadata
            # first sync.
            self._sync_batch_from_master()
            self._watch_paths()
            names = self.refresh_template_dropdown(
                select=self._remembered.get("template"))
            if self._remembered.get("template") in names:
                self.after(900, self._on_template_selected)
        else:
            # No inspector tab in the fallback UI, but paths are still remembered.
            for var in (self.eng_pdf_var, self.tr_dir_var, self.out_dir_var):
                var.trace_add("write", lambda *_a: self._persist_paths())

        # Backstop: a path typed and left unsaved is still captured on close.
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._check_queue()

    # Screens this has to work on range from a 1366x768 laptop to a 4K desktop.
    # A hard 1320x900 window opened larger than the screen on the first and
    # wasted most of the second, so the window is sized from the display and the
    # widget scale is nudged down when the screen is genuinely small.
    PREFERRED_SIZE = (1560, 1000)
    ABSOLUTE_MIN = (900, 620)

    def _fit_to_screen(self):
        try:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        except Exception:
            sw, sh = 1366, 768

        want_w, want_h = self.PREFERRED_SIZE
        w = max(self.ABSOLUTE_MIN[0], min(want_w, int(sw * 0.92)))
        h = max(self.ABSOLUTE_MIN[1], min(want_h, int(sh * 0.90)))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(min(self.ABSOLUTE_MIN[0], w), min(self.ABSOLUTE_MIN[1], h))

        # Below roughly 900 usable pixels the fixed row heights stop fitting, so
        # shrink everything a little rather than clipping it.
        if HAS_CTK:
            try:
                if sh < 800:
                    ctk.set_widget_scaling(0.85)
                elif sh < 900:
                    ctk.set_widget_scaling(0.92)
            except Exception:
                pass
        print(f"[Layout] Screen {sw}x{sh} -> window {w}x{h}")

    def _get_font(self, size=12, weight="normal", family=None):
        """Xylem-branded font; CTkFont when CustomTkinter is present, else a Tk tuple."""
        fam = family or theme.resolve_font_family()
        if HAS_CTK:
            return ctk.CTkFont(family=fam, size=size, weight=weight)
        return theme.get_font(size, weight, fam)

    def _build_ui(self):
        if HAS_CTK:
            # ------------------------------------------------------------------
            # 1. Header Banner (Dependable Blue #003E51 with Clarity Blue Accent)
            # ------------------------------------------------------------------
            header_frame = ctk.CTkFrame(
                self._body,
                corner_radius=12,
                fg_color=DEPENDABLE_BLUE,
                border_width=1,
                border_color=XYLEM_BLUE
            )
            header_frame.pack(fill="x", padx=10, pady=(10, 10))

            top_row = ctk.CTkFrame(header_frame, fg_color="transparent")
            top_row.pack(fill="x", padx=20, pady=(14, 2))

            title_lbl = ctk.CTkLabel(
                top_row,
                text="XYLEM  |  SpotCheck",
                font=self._get_font(21, "bold"),
                text_color=NEUTRAL_WHITE
            )
            title_lbl.pack(side="left")

            badge_lbl = ctk.CTkLabel(
                top_row,
                text="PDF Quality & Visual Inspection Engine",
                font=self._get_font(12, "bold"),
                fg_color=XYLEM_BLUE,
                text_color=NEUTRAL_WHITE,
                corner_radius=6,
                padx=10,
                pady=4
            )
            badge_lbl.pack(side="right")

            desc_lbl = ctk.CTkLabel(
                header_frame,
                text="Automated Multi-Language Verification \u2022 First Page Metadata \u2022 TOC Topic Numerics \u2022 Last Page Footers \u2022 Graphic Elements",
                font=self._get_font(11, "normal"),
                text_color=CLARITY_BLUE
            )
            desc_lbl.pack(anchor="w", padx=20, pady=(0, 14))
            header_frame.bind(
                "<Configure>",
                lambda e: desc_lbl.configure(wraplength=max(280, e.width - 40)),
                add="+"
            )

            # ------------------------------------------------------------------
            # 2. Input Configuration Card (White Card with Xylem Blue Accents)
            # ------------------------------------------------------------------
            config_card = ctk.CTkFrame(
                self._body,
                corner_radius=10,
                fg_color=UI_CARD_BG,
                border_width=1,
                border_color=UI_BORDER
            )
            config_card.pack(fill="x", padx=18, pady=6)

            card_title = ctk.CTkLabel(
                config_card,
                text="Inspection Paths Configuration",
                font=self._get_font(13, "bold"),
                text_color=DEPENDABLE_BLUE
            )
            card_title.grid(row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(12, 6))

            # Row 1: the English master, picked as a single FILE. Its own folder
            # is the batch: every OTHER PDF sitting beside it is a translation,
            # discovered automatically. So the user selects one PDF and nothing
            # else. eng_pdf_var is that file; tr_dir_var is derived as its folder
            # (still what the run scans, with the master excluded downstream), so
            # everything further along is untouched.
            ctk.CTkLabel(
                config_card,
                text="English Master PDF:",
                font=self._get_font(12, "bold"),
                text_color=DEPENDABLE_BLUE
            ).grid(row=1, column=0, sticky="w", padx=16, pady=6)

            self.eng_pdf_var = ctk.StringVar(value=self._remembered.get("english_pdf") or "")
            # Derived from the master's folder; no field of its own any more.
            self.tr_dir_var = ctk.StringVar(value="")

            self.eng_entry = ctk.CTkEntry(
                config_card,
                textvariable=self.eng_pdf_var,
                font=self._get_font(11),
                fg_color=UI_CARD_WELL,
                border_color=UI_BORDER,
                text_color=DEPENDABLE_BLUE,
                height=34
            )
            self.eng_entry.grid(row=1, column=1, sticky="ew", padx=(0, 10), pady=6)

            btn_master = ctk.CTkButton(
                config_card,
                text="Browse PDF",
                font=self._get_font(11, "bold"),
                fg_color=XYLEM_BLUE,
                hover_color=UI_HOVER_BLUE,
                text_color=NEUTRAL_WHITE,
                width=110,
                height=34,
                command=self._browse_master_pdf
            )
            btn_master.grid(row=1, column=2, padx=(0, 16), pady=6)
            self._browse_buttons.append(btn_master)

            # Row 2: confirms the batch - how many other PDFs in the master's
            # folder will be checked as translations - before anything runs.
            self.batch_hint_lbl = ctk.CTkLabel(
                config_card,
                text="pick the English PDF — the other PDFs in its folder are the translations",
                font=self._get_font(10), text_color=NEUTRAL_DARK_GR,
                anchor="w", justify="left")
            self.batch_hint_lbl.grid(row=2, column=1, columnspan=2, sticky="w",
                                     padx=(0, 16), pady=(0, 4))

            # Row 3: Output Directory
            ctk.CTkLabel(
                config_card,
                text="Output Directory:",
                font=self._get_font(12, "bold"),
                text_color=DEPENDABLE_BLUE
            ).grid(row=3, column=0, sticky="w", padx=16, pady=(6, 14))

            self.template_var = ctk.StringVar(value=self._remembered.get("template") or "")
            # Left blank on purpose: it fills itself in from the master's folder
            # the moment a master is chosen (SpotCheck_Output inside the batch).
            self.out_dir_var = ctk.StringVar(
                value=self._remembered.get("output_dir") or "")
            self.out_entry = ctk.CTkEntry(
                config_card,
                textvariable=self.out_dir_var,
                font=self._get_font(11),
                fg_color=UI_CARD_WELL,
                border_color=UI_BORDER,
                text_color=DEPENDABLE_BLUE,
                height=34
            )
            self.out_entry.grid(row=3, column=1, sticky="ew", padx=(0, 10), pady=(6, 4))

            btn_out = ctk.CTkButton(
                config_card,
                text="Browse Output",
                font=self._get_font(11, "bold"),
                fg_color=XYLEM_BLUE,
                hover_color=UI_HOVER_BLUE,
                text_color=NEUTRAL_WHITE,
                width=110,
                height=34,
                command=self._browse_out_dir
            )
            btn_out.grid(row=3, column=2, padx=(0, 16), pady=(6, 4))
            self._browse_buttons.append(btn_out)

            # Row 4: says where results land - inside the batch folder by default,
            # right beside the PDFs, unless the user browses somewhere else.
            self.output_hint_lbl = ctk.CTkLabel(
                config_card,
                text=(f"results go in a “{OUTPUT_SUBDIR_NAME}” folder beside the "
                      f"PDFs — Browse Output only to change that"),
                font=self._get_font(10), text_color=NEUTRAL_DARK_GR,
                anchor="w", justify="left")
            self.output_hint_lbl.grid(row=4, column=1, columnspan=2, sticky="w",
                                      padx=(0, 16), pady=(0, 12))

            config_card.columnconfigure(1, weight=1)

            # ------------------------------------------------------------------
            # 3. Action Toolbar & Status Card
            # ------------------------------------------------------------------
            action_frame = ctk.CTkFrame(self._body, fg_color="transparent")
            action_frame.pack(fill="x", padx=18, pady=8)

            self.start_btn = ctk.CTkButton(
                action_frame,
                text="\u25b6  Run Full Inspection",
                font=self._get_font(13, "bold"),
                fg_color=DYNAMIC_GREEN,
                hover_color="#52B603",
                text_color=DEPENDABLE_BLUE,
                height=40,
                width=180,
                command=self._start_inspection
            )
            self.start_btn.pack(side="left", padx=(0, 10))

            self.open_excel_btn = ctk.CTkButton(
                action_frame,
                text="\U0001f4ca Open Excel Report",
                font=self._get_font(12, "bold"),
                fg_color=DEPENDABLE_BLUE,
                hover_color=UI_DARK_HOVER,
                text_color=NEUTRAL_WHITE,
                state="disabled",
                height=40,
                command=self._open_excel_report
            )
            self.open_excel_btn.pack(side="left", padx=6)

            self.open_folder_btn = ctk.CTkButton(
                action_frame,
                text="\U0001f4c1 Open Output Folder",
                font=self._get_font(12, "bold"),
                fg_color=UI_CARD_BG,
                hover_color=UI_CARD_WELL,
                text_color=DEPENDABLE_BLUE,
                border_width=1,
                border_color=UI_BORDER,
                text_color_disabled=theme.TEXT_DISABLED,
                state="disabled",
                height=40,
                command=self._open_output_folder
            )
            self.open_folder_btn.pack(side="left", padx=6)

            ctk.CTkLabel(
                config_card,
                text="Stylesheet Template:",
                font=self._get_font(12, "bold"),
                text_color=DEPENDABLE_BLUE
            ).grid(row=4, column=0, sticky="w", padx=16, pady=(0, 14))

            tmpl_cell = ctk.CTkFrame(config_card, fg_color="transparent")
            tmpl_cell.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(0, 16), pady=(0, 14))
            self.template_menu = ctk.CTkOptionMenu(
                tmpl_cell, variable=self.template_var, values=["(none)"], width=300, height=34,
                font=self._get_font(11), fg_color=UI_CARD_BG, button_color=XYLEM_BLUE,
                text_color=DEPENDABLE_BLUE, command=self._on_template_selected)
            self.template_menu.pack(side="left")
            ctk.CTkLabel(
                tmpl_cell,
                text="regions load into the Region Inspector automatically",
                font=self._get_font(10), text_color=NEUTRAL_DARK_GR
            ).pack(side="left", padx=10)

            self.clear_output_btn = ctk.CTkButton(
                action_frame,
                text="\U0001f9f9 Clear Output Folder",
                font=self._get_font(12, "bold"),
                fg_color=RADIANT_ORANGE,
                hover_color="#C85800",
                text_color=theme.TEXT_ON_ORANGE,
                height=40,
                command=self._clear_output_folder
            )
            self.clear_output_btn.pack(side="left", padx=6)

            self.status_lbl = ctk.CTkLabel(
                action_frame,
                text="\u25cf Ready to inspect",
                font=self._get_font(12, "bold"),
                text_color=theme.TEXT_ON_LIGHT
            )
            self.status_lbl.pack(side="right", padx=10)

            # Progress Bar (Xylem Blue)
            self.progress_bar = ctk.CTkProgressBar(
                self._body,
                progress_color=XYLEM_BLUE,
                fg_color=UI_CARD_WELL,
                height=8
            )
            self.progress_bar.pack(fill="x", padx=18, pady=(0, 8))
            self.progress_bar.set(0)

            # ------------------------------------------------------------------
            # 4. Live Console & Detailed Execution Log
            # ------------------------------------------------------------------
            log_frame = ctk.CTkFrame(
                self._body,
                corner_radius=10,
                fg_color=UI_CARD_BG,
                border_width=1,
                border_color=UI_BORDER
            )
            log_frame.pack(fill="both", expand=True, padx=18, pady=(0, 16))

            log_header = ctk.CTkFrame(log_frame, fg_color="transparent")
            log_header.pack(fill="x", padx=14, pady=(10, 4))

            ctk.CTkLabel(
                log_header,
                text="Execution Console & Inspection Details",
                font=self._get_font(12, "bold"),
                text_color=DEPENDABLE_BLUE
            ).pack(side="left")

            open_log_btn = ctk.CTkButton(
                log_header,
                text="📄 Open Log File",
                font=self._get_font(11, "bold"),
                fg_color=DEPENDABLE_BLUE,
                hover_color=UI_DARK_HOVER,
                text_color=NEUTRAL_WHITE,
                height=26,
                width=115,
                command=self._open_log_file
            )
            open_log_btn.pack(side="right", padx=(6, 0))

            open_log_dir_btn = ctk.CTkButton(
                log_header,
                text="📁 Log Folder",
                font=self._get_font(11, "bold"),
                fg_color=UI_CARD_BG,
                hover_color=UI_CARD_WELL,
                text_color=DEPENDABLE_BLUE,
                border_width=1,
                border_color=UI_BORDER,
                height=26,
                width=95,
                command=self._open_log_folder
            )
            open_log_dir_btn.pack(side="right")

            # Clears what is on screen only - the on-disk log file keeps every
            # line, so nothing is actually lost by tidying the live view.
            clear_console_btn = ctk.CTkButton(
                log_header,
                text="🧹 Clear Console",
                font=self._get_font(11, "bold"),
                fg_color=UI_CARD_BG,
                hover_color=UI_CARD_WELL,
                text_color=DEPENDABLE_BLUE,
                border_width=1,
                border_color=UI_BORDER,
                height=26,
                width=120,
                command=self._clear_console
            )
            clear_console_btn.pack(side="right", padx=(0, 6))

            self.log_textbox = ctk.CTkTextbox(
                log_frame,
                font=ctk.CTkFont(family="Consolas", size=15),
                fg_color=DEPENDABLE_BLUE,
                text_color=NEUTRAL_WHITE,
                border_width=0,
                corner_radius=6
            )
            self.log_textbox.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        else:
            # ==================================================================
            # Native Tkinter Desktop System Fallback (Arial)
            # ==================================================================
            header_frame = tk.Frame(self, bg=DEPENDABLE_BLUE)
            header_frame.pack(fill="x", padx=12, pady=10)

            tk.Label(
                header_frame,
                text="XYLEM  |  SpotCheck - PDF Quality Inspection Engine",
                font=(FONT_FAMILY_FALLBACK, 15, "bold"),
                fg=NEUTRAL_WHITE,
                bg=DEPENDABLE_BLUE
            ).pack(anchor="w", padx=14, pady=10)

            config_frame = tk.LabelFrame(
                self,
                text=" Inspection Paths Configuration ",
                font=(FONT_FAMILY_FALLBACK, 10, "bold"),
                fg=DEPENDABLE_BLUE,
                bg=UI_CARD_BG,
                padx=12,
                pady=10
            )
            config_frame.pack(fill="x", padx=12, pady=6)

            tk.Label(config_frame, text="English Master PDF:", font=(FONT_FAMILY_FALLBACK, 10, "bold"), fg=DEPENDABLE_BLUE, bg=UI_CARD_BG).grid(row=0, column=0, sticky="w", pady=5)
            self.eng_pdf_var = tk.StringVar(value=self._remembered.get("english_pdf") or (os.path.abspath(r"Input\English\894387_5.0_en-US_2026-04_IOM.Start350.pdf") if os.path.exists(r"Input\English\894387_5.0_en-US_2026-04_IOM.Start350.pdf") else ""))
            self.eng_entry = tk.Entry(config_frame, textvariable=self.eng_pdf_var, font=(FONT_FAMILY_FALLBACK, 10), width=70)
            self.eng_entry.grid(row=0, column=1, sticky="ew", padx=8, pady=5)
            btn_eng_tk = tk.Button(config_frame, text="Browse...", bg=XYLEM_BLUE, fg=NEUTRAL_WHITE, command=self._browse_eng_pdf)
            btn_eng_tk.grid(row=0, column=2, padx=4, pady=5)
            self._browse_buttons.append(btn_eng_tk)

            tk.Label(config_frame, text="Translated Folder:", font=(FONT_FAMILY_FALLBACK, 10, "bold"), fg=DEPENDABLE_BLUE, bg=UI_CARD_BG).grid(row=1, column=0, sticky="w", pady=5)
            self.tr_dir_var = tk.StringVar(value=self._remembered.get("translated_dir") or (os.path.abspath(r"Input\Translated") if os.path.exists(r"Input\Translated") else ""))
            self.tr_entry = tk.Entry(config_frame, textvariable=self.tr_dir_var, font=(FONT_FAMILY_FALLBACK, 10), width=70)
            self.tr_entry.grid(row=1, column=1, sticky="ew", padx=8, pady=5)
            btn_tr_tk = tk.Button(config_frame, text="Browse...", bg=XYLEM_BLUE, fg=NEUTRAL_WHITE, command=self._browse_tr_dir)
            btn_tr_tk.grid(row=1, column=2, padx=4, pady=5)
            self._browse_buttons.append(btn_tr_tk)

            tk.Label(config_frame, text="Output Directory:", font=(FONT_FAMILY_FALLBACK, 10, "bold"), fg=DEPENDABLE_BLUE, bg=UI_CARD_BG).grid(row=2, column=0, sticky="w", pady=5)
            self.out_dir_var = tk.StringVar(value=self._remembered.get("output_dir") or os.path.abspath(r"Output"))
            self.out_entry = tk.Entry(config_frame, textvariable=self.out_dir_var, font=(FONT_FAMILY_FALLBACK, 10), width=70)
            self.out_entry.grid(row=2, column=1, sticky="ew", padx=8, pady=5)
            btn_out_tk = tk.Button(config_frame, text="Browse...", bg=XYLEM_BLUE, fg=NEUTRAL_WHITE, command=self._browse_out_dir)
            btn_out_tk.grid(row=2, column=2, padx=4, pady=5)
            self._browse_buttons.append(btn_out_tk)
            config_frame.columnconfigure(1, weight=1)

            btn_frame = tk.Frame(self, bg=UI_BG_CANVAS)
            btn_frame.pack(fill="x", padx=12, pady=8)

            self.start_btn = tk.Button(btn_frame, text="\u25b6 Run Full Inspection", font=(FONT_FAMILY_FALLBACK, 11, "bold"), bg=DYNAMIC_GREEN, fg=DEPENDABLE_BLUE, command=self._start_inspection)
            self.start_btn.pack(side="left", padx=5)

            self.open_excel_btn = tk.Button(btn_frame, text="Open Excel Report", font=(FONT_FAMILY_FALLBACK, 10, "bold"), bg=DEPENDABLE_BLUE, fg=NEUTRAL_WHITE, state="disabled", command=self._open_excel_report)
            self.open_excel_btn.pack(side="left", padx=5)

            self.open_folder_btn = tk.Button(btn_frame, text="Open Output Folder", font=(FONT_FAMILY_FALLBACK, 10), state="disabled", command=self._open_output_folder)
            self.open_folder_btn.pack(side="left", padx=5)

            self.clear_output_btn = tk.Button(btn_frame, text="Clear Output Folder", font=(FONT_FAMILY_FALLBACK, 10, "bold"), bg=RADIANT_ORANGE, fg=NEUTRAL_WHITE, command=self._clear_output_folder)
            self.clear_output_btn.pack(side="left", padx=5)

            self.status_lbl = tk.Label(btn_frame, text="\u25cf Ready", font=(FONT_FAMILY_FALLBACK, 10, "bold"), fg=XYLEM_BLUE, bg=UI_BG_CANVAS)
            self.status_lbl.pack(side="right", padx=10)

            self.progress_bar = ttk.Progressbar(self, mode="indeterminate")
            self.progress_bar.pack(fill="x", padx=12, pady=5)

            log_frame = tk.LabelFrame(self, text=" Execution Console ", font=(FONT_FAMILY_FALLBACK, 10, "bold"), fg=DEPENDABLE_BLUE, bg=UI_CARD_BG, padx=8, pady=8)
            log_frame.pack(fill="both", expand=True, padx=12, pady=10)

            log_bar = tk.Frame(log_frame, bg=UI_CARD_BG)
            log_bar.pack(fill="x", pady=(0, 4))
            tk.Button(log_bar, text="Open Log File", font=(FONT_FAMILY_FALLBACK, 9), bg=DEPENDABLE_BLUE, fg=NEUTRAL_WHITE, command=self._open_log_file).pack(side="right", padx=3)
            tk.Button(log_bar, text="Log Folder", font=(FONT_FAMILY_FALLBACK, 9), bg=UI_CARD_WELL, fg=DEPENDABLE_BLUE, command=self._open_log_folder).pack(side="right", padx=3)
            tk.Button(log_bar, text="Clear Console", font=(FONT_FAMILY_FALLBACK, 9), bg=UI_CARD_WELL, fg=DEPENDABLE_BLUE, command=self._clear_console).pack(side="right", padx=3)

            self.log_textbox = tk.Text(log_frame, font=("Consolas", 14), bg=DEPENDABLE_BLUE, fg=NEUTRAL_WHITE)
            self.log_textbox.pack(fill="both", expand=True)

    # ──────────────────────────────────────────────────────────
    # File / Directory Pickers
    # ──────────────────────────────────────────────────────────
    def _browse_eng_pdf(self):
        """
        Pick the folder holding the master.

        A folder rather than a file, to match the translated side. Exactly one
        PDF must be in it - resolve_master_pdf refuses the ambiguity rather than
        picking one, because a whole run measured against the wrong master would
        look completely normal in the report.
        """
        d = filedialog.askdirectory(title="Select the folder holding the English master PDF")
        if d:
            self.eng_pdf_var.set(os.path.abspath(d))
            self._describe_master()

    def _describe_master(self):
        """Say which PDF the English folder resolves to, before anything runs."""
        if not hasattr(self, "master_lbl"):
            return
        path = (self.eng_pdf_var.get() or "").strip().strip('"').strip("'")
        if not path:
            self.master_lbl.configure(text="")
            return
        pdf, err = spotcheck_engine.resolve_master_pdf(path)
        if pdf:
            self.master_lbl.configure(
                text=f"master: {os.path.basename(pdf)}", text_color=NEUTRAL_DARK_GR)
        else:
            self.master_lbl.configure(text=err.split("\n")[0], text_color=theme.TEXT_ATTENTION)

    def _browse_tr_dir(self):
        d = filedialog.askdirectory(title="Select Translated PDFs Directory")
        if d:
            self.tr_dir_var.set(os.path.abspath(d))

    def _browse_out_dir(self):
        d = filedialog.askdirectory(title="Select Output Directory")
        if d:
            # A deliberate choice: from here on, don't move the output into the
            # batch folder for them. Picking a new master won't re-point it.
            self._output_user_set = True
            self.out_dir_var.set(os.path.abspath(d))

    # ──────────────────────────────────────────────────────────
    # One batch = one folder. The user picks the English master as a
    # single PDF; every OTHER PDF sitting beside it is a translation,
    # discovered automatically. tr_dir_var (what the run scans, master
    # excluded downstream) is derived from the master's own folder, so
    # nothing further along the pipeline has to change.
    # ──────────────────────────────────────────────────────────
    def _browse_master_pdf(self):
        """Pick the English master as one PDF file; its folder becomes the batch."""
        f = filedialog.askopenfilename(
            title="Select the English master PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")])
        if f:
            self.eng_pdf_var.set(os.path.abspath(f))
            self._sync_batch_from_master()

    def _sync_batch_from_master(self):
        """
        Derive the batch folder from the chosen master and report what was found.

        The master is one PDF; its own folder is the batch, and every OTHER PDF
        in that folder is a translation. tr_dir_var is kept as that folder so the
        run (which already excludes the master) needs no change. Idempotent - it
        writes tr_dir_var only when the value truly changes - so the path watcher
        can call it without looping.
        """
        eng = (self.eng_pdf_var.get() or "").strip().strip('"').strip("'")
        folder = ""
        n_tr = 0
        if eng and os.path.isfile(eng):
            folder = os.path.dirname(os.path.abspath(eng))
            try:
                master_abs = os.path.abspath(eng)
                n_tr = sum(1 for f in os.listdir(folder)
                           if f.lower().endswith(".pdf")
                           and os.path.abspath(os.path.join(folder, f)) != master_abs)
            except OSError:
                n_tr = 0
        elif eng and os.path.isdir(eng):
            # A folder pasted or restored from an older settings file: keep its
            # old meaning - the folder is the batch, and one of its PDFs the
            # master - so an upgrade never comes up blank.
            folder = os.path.abspath(eng)
            try:
                n_tr = max(0, sum(1 for f in os.listdir(folder)
                                  if f.lower().endswith(".pdf")) - 1)
            except OSError:
                n_tr = 0

        if hasattr(self, "tr_dir_var") and (self.tr_dir_var.get() or "") != folder:
            self.tr_dir_var.set(folder)

        self._derive_output_from_batch(folder)
        self._update_batch_hint(eng, n_tr)

    def _derive_output_from_batch(self, folder):
        """
        Point the output at a results folder inside the batch, so the report and
        its evidence sit beside the PDFs they came from.

        Two guards keep this from fighting the user:
          - once they browse to an output of their own, _output_user_set is set
            and we never move it again.
          - while the batch folder is unchanged we don't re-derive, so a path
            typed by hand into the output field survives - only a new master
            (a new batch folder) re-points it.
        """
        if not folder or getattr(self, "_output_user_set", False):
            return
        if getattr(self, "_output_batch_folder", None) == folder:
            return
        derived = os.path.join(folder, OUTPUT_SUBDIR_NAME)
        if hasattr(self, "out_dir_var") and (self.out_dir_var.get() or "") != derived:
            self.out_dir_var.set(derived)
        self._output_batch_folder = folder

    def _update_batch_hint(self, eng, n_tr):
        """One line under the picker: what the chosen master resolves to."""
        if not hasattr(self, "batch_hint_lbl"):
            return
        if not eng:
            txt = ("pick the English PDF — the other PDFs in its folder are the "
                   "translations")
            color = NEUTRAL_DARK_GR
        elif not (os.path.isfile(eng) or os.path.isdir(eng)):
            txt = "that path no longer exists — pick the English PDF again"
            color = theme.TEXT_ATTENTION
        elif n_tr <= 0:
            txt = ("no other PDFs sit beside this one — add the translated PDFs "
                   "to its folder")
            color = theme.TEXT_ATTENTION
        else:
            txt = (f"master: {os.path.basename(eng)}  •  "
                   f"{n_tr} translation(s) beside it")
            color = NEUTRAL_DARK_GR
        try:
            self.batch_hint_lbl.configure(text=txt, text_color=color)
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────
    # Log Console
    # ──────────────────────────────────────────────────────────
    # A run prints thousands of lines. Past this the console is scrollback
    # nobody reads, and a Tk text widget holding it repaints slowly enough to
    # be felt everywhere else in the window.
    MAX_LOG_LINES = 4000

    def _clear_console(self):
        """
        Empty the live console view. The on-disk log file is untouched - it
        keeps the full record - so this only tidies what is on screen, and stays
        available during a run (the next line simply appends to the empty view).
        """
        try:
            self.log_textbox.delete("1.0", "end")
        except Exception:
            pass

    def _append_log(self, text):
        """Add one chunk. Prefer _append_log_batch while a run is producing."""
        self._append_log_batch([text])

    def _append_log_batch(self, chunks):
        """
        Write many log messages as one edit.

        Inserting and then calling see() once per message was the single most
        expensive thing on the main thread during a run: two hundred forced
        scroll-and-redraws every tenth of a second. Everything else the
        interface wanted to do - switching tabs, repainting a panel - queued up
        behind it, which is what made the window look broken rather than busy.
        One insert and one see() per tick costs the same as a single message.
        """
        if not chunks:
            return
        try:
            self.log_textbox.insert("end", "".join(chunks))
            # Trim from the front, so the console stays a fixed cost.
            lines = int(self.log_textbox.index("end-1c").split(".")[0])
            if lines > self.MAX_LOG_LINES:
                self.log_textbox.delete("1.0", f"{lines - self.MAX_LOG_LINES}.0")
            self.log_textbox.see("end")
        except Exception:
            pass

    def _check_queue(self):
        # The log can arrive faster than it can be drawn on a long run. Draining
        # a bounded number of messages per tick keeps the window repainting -
        # an unbounded drain is what made the interface stop responding while
        # a 92-page manual scrolled past.
        chunks = []
        for _ in range(400):
            try:
                chunks.append(self.text_queue.get_nowait())
            except queue.Empty:
                break
        self._append_log_batch(chunks)

        latest = None
        while True:
            try:
                latest = self.progress_queue.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            self._show_progress(*latest)

        # Completion is delivered the same way the log is, on the main thread.
        # The worker used to call self.after() directly, which registers a
        # command on the Tk interpreter from the wrong thread and raises
        # "main thread is not in main loop" - the run would finish, the report
        # would be written, and none of the tabs would ever be told.
        while not self.done_queue.empty():
            try:
                self._on_inspection_finished(self.done_queue.get_nowait())
            except queue.Empty:
                break
        self.after(100, self._check_queue)

    def _show_progress(self, fraction, message):
        """
        Draw real progress, on the main thread.

        A run over a dozen 92-page manuals takes minutes. An indeterminate bar
        sliding back and forth for that long is indistinguishable from a hung
        program, which is exactly how it was being read. A fraction and the name
        of the stage answer the only question the user has: is it still working.
        """
        try:
            if fraction is None:
                self.progress_bar.start()
            else:
                self.progress_bar.stop()
                self.progress_bar.set(max(0.0, min(1.0, float(fraction))))
        except Exception:
            pass
        if message:
            try:
                text = f"● {message}"
                if HAS_CTK:
                    self.status_lbl.configure(text=text, text_color=theme.TEXT_ATTENTION)
                else:
                    self.status_lbl.config(text=text, fg=RADIANT_ORANGE)
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────
    # Fix 6: Lock / Unlock all input controls during inspection
    # ──────────────────────────────────────────────────────────
    def _set_input_state(self, state):
        """Enable or disable every path control together (guarded per widget)."""
        for btn in self._browse_buttons:
            try:
                btn.configure(state=state)
            except Exception:
                pass
        # eng_entry is the master-PDF field in the CTk UI and the English field
        # in the plain-Tk fallback; tr_entry exists only in the fallback. Guard
        # each by name so either layout works.
        for name in ("eng_entry", "tr_entry", "out_entry", "clear_output_btn"):
            widget = getattr(self, name, None)
            if widget is not None:
                try:
                    widget.configure(state=state)
                except Exception:
                    pass

    def _lock_inputs(self):
        """Disable all entries and browse buttons to prevent edits during inspection."""
        self._set_input_state("disabled")

    def _unlock_inputs(self):
        """Re-enable all entries and browse buttons after inspection completes."""
        self._set_input_state("normal")

    # ──────────────────────────────────────────────────────────
    # Inspection Launch (with Fix 3 + Fix 4)
    # ──────────────────────────────────────────────────────────
    def _start_inspection(self):
        if self.is_running:
            return

        eng_pdf = self.eng_pdf_var.get().strip().strip('"').strip("'")

        # The master is the only thing the user picks. Validate it first, then
        # derive everything else from where it lives.
        if not eng_pdf:
            messagebox.showerror(
                "Input Required",
                "Please select the English master PDF.\n"
                "Use the 'Browse PDF' button to choose it — the other PDFs in "
                "its folder are checked as translations.")
            return

        master_pdf, master_err = spotcheck_engine.resolve_master_pdf(eng_pdf)
        if master_err:
            messagebox.showerror("English Master", master_err)
            return

        # The master's own folder is the batch, and that is what the run scans.
        tr_target = os.path.dirname(os.path.abspath(master_pdf))
        self.tr_dir_var.set(tr_target)

        # Make sure the output points inside the batch folder even if the user
        # hit Run before the path watcher's derive fired (respects a chosen one).
        self._derive_output_from_batch(tr_target)
        out_dir = self.out_dir_var.get().strip()

        if not out_dir:
            messagebox.showerror("Input Required", "Please specify an Output Directory.\nUse the 'Browse Output' button to select a directory.")
            return

        # Everything in the folder except the master is a translation; make sure
        # there is at least one, otherwise the run has nothing to compare.
        try:
            others = [f for f in os.listdir(tr_target)
                      if f.lower().endswith(".pdf")
                      and os.path.abspath(os.path.join(tr_target, f)) != os.path.abspath(master_pdf)]
        except OSError:
            others = []
        if not others:
            messagebox.showerror(
                "No Translations Found",
                "There are no other PDFs beside the master to check.\n\n"
                "Put the English master and all its translated PDFs in the one "
                "folder, then pick the English master with 'Browse PDF'.")
            return

        # Fix 3: Auto-create output directory if it doesn't exist
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Output Directory Error", f"Cannot create output directory:\n{out_dir}\n\nError: {e}")
            return

        self.is_running = True
        self.start_btn.configure(state="disabled")
        self.open_excel_btn.configure(state="disabled")
        self.open_folder_btn.configure(state="disabled")
        self._lock_inputs()  # Fix 6

        if HAS_CTK:
            self.status_lbl.configure(text="\u25cf Inspecting PDFs...", text_color=theme.TEXT_ATTENTION)
            self.progress_bar.start()
        else:
            self.status_lbl.config(text="\u25cf Inspecting PDFs...", fg=RADIANT_ORANGE)
            self.progress_bar.start(10)

        self._append_log("\n" + "=" * 76 + "\n")
        self._append_log("XYLEM SPOTCHECK INSPECTION INITIATED\n")
        self._append_log(f"Master English Source : {eng_pdf}\n")
        self._append_log(f"Translated Target     : {tr_target}\n")
        self._append_log(f"Output Directory      : {out_dir}\n")
        self._append_log("=" * 76 + "\n")

        self.output_dir_path = os.path.abspath(out_dir)
        self.output_excel_path = os.path.join(self.output_dir_path, "PDF_Quality_Inspection_Report.xlsx")

        # The ignored margins decide what image extraction even sees, so the
        # run has to use the ones the user set in the Region Inspector - the
        # same ones the template was saved with.
        run_margins = self._active_margins()
        run_regions, region_src = self._regions_for_run()
        self._append_log(f"Ignored Margins       : {page_margins.describe(run_margins)}\n")
        self._append_log(f"Stylesheet Regions    : {len(run_regions)} from {region_src}\n")
        self._append_log("Meta Data             : collected for the master and every translation\n")

        t = threading.Thread(target=self._run_inspection_thread,
                             args=(eng_pdf, tr_target, out_dir, run_margins, run_regions),
                             daemon=True)
        t.start()

    # ──────────────────────────────────────────────────────────
    # Worker Thread (with Fix 5: full traceback on errors)
    # ──────────────────────────────────────────────────────────
    def _run_inspection_thread(self, eng_pdf, tr_target, out_dir, run_margins=None,
                               run_regions=None):
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        redirector = TextRedirector(self.text_queue)
        sys.stdout = redirector
        sys.stderr = redirector

        def report(fraction, message):
            # Straight onto a queue: touching a widget from here raises
            # "main thread is not in main loop". The pump draws it.
            self.progress_queue.put((fraction, message))

        try:
            self.last_run_results = spotcheck_engine.run_quality_inspection(
                eng_pdf, tr_target, out_dir, margins=run_margins,
                regions=run_regions or None, progress=report)
            success = True
        except Exception as e:
            # Fix 5: Full traceback in error console for production debugging
            tb_text = traceback.format_exc()
            self.text_queue.put(f"\n{'=' * 76}\n")
            self.text_queue.put(f"[ERROR] Inspection failed with exception:\n")
            self.text_queue.put(f"{tb_text}\n")
            self.text_queue.put(f"{'=' * 76}\n")
            success = False
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            self.done_queue.put(success)

    def _on_inspection_finished(self, success):
        self.is_running = False

        # Publish the crop comparisons into the gallery so they can be reviewed
        # in-app instead of only on disk.
        if success and self.comparison_gallery is not None and self.last_run_results:
            try:
                self.comparison_gallery.load_crop_details(
                    self.last_run_results.get("img_crop_details", []))
            except Exception as e:
                print(f"[WARN] Could not publish crop results to the gallery: {e}")

            # The one-directional crop hunt above cannot see a graphic that was
            # simply deleted when a near-identical sibling exists elsewhere in
            # the manual - it still matches the sibling and reports PASS. The
            # symmetric per-topic count check catches exactly that, and used to
            # be Excel-only; publishing it here is what makes a deleted graphic
            # show up as something to review instead of nothing at all.
            try:
                self.comparison_gallery.load_image_counts(
                    self.last_run_results.get("count_results", []))
            except Exception as e:
                print(f"[WARN] Could not publish image-count results to the gallery: {e}")

            # A count agreeing on both sides does not mean nothing broke - two
            # icons can trade places, or one can render corrupted in place,
            # while the tally and the one-directional hunt both still pass.
            # This pairs graphics by what they look like rather than by
            # reading order, so it stays correct even when translated text
            # reflows a graphic onto a later page or a different column.
            try:
                self.comparison_gallery.load_matched_images(
                    self.last_run_results.get("matched_results", []))
            except Exception as e:
                print(f"[WARN] Could not publish content-matched image results: {e}")

            # The actual table-of-contents check: does the translation's
            # section numbering match the master's, in the same order. Used
            # to be Excel-only like Image Counts was.
            try:
                self.comparison_gallery.load_toc_results(
                    self.last_run_results.get("toc_results", []))
            except Exception as e:
                print(f"[WARN] Could not publish TOC numbering results: {e}")

            # Barcode & QR-code counts, per translated file - a barcode or QR
            # code dropped in a translation. Used to be Excel-only.
            try:
                self.comparison_gallery.load_barcode_qr(
                    self.last_run_results.get("bc_qr_results", []))
            except Exception as e:
                print(f"[WARN] Could not publish barcode/QR results: {e}")

            # How long each document took, shown in the file list plus the
            # end-to-end total in its header.
            try:
                self.comparison_gallery.load_timings(
                    self.last_run_results.get("timing_rows", []),
                    self.last_run_results.get("total_seconds"))
            except Exception as e:
                print(f"[WARN] Could not publish timing to the gallery: {e}")

        # One run now feeds every tab, so the results land where the user will
        # look for them rather than only in the Excel file.
        if success and self.last_run_results:
            regions = self.last_run_results.get("region_results") or []
            if regions and self.comparison_gallery is not None:
                try:
                    self.comparison_gallery.load_region_results(regions)
                except Exception as e:
                    print(f"[WARN] Could not publish region results: {e}")
            # The run does the text checks too, so the tab shows what the run
            # found rather than making the user scan the same documents again.
            overlaps = self.last_run_results.get("overlap_results") or []
            missed = self.last_run_results.get("untranslated_results") or []
            if (overlaps or missed) and self.comparison_gallery is not None:
                try:
                    self.comparison_gallery.load_text_checks(overlaps, missed)
                except Exception as e:
                    print(f"[WARN] Could not publish text checks to the gallery: {e}")

            # Text running past the left/right margin - a geometry fault the
            # count and text checks cannot see, shown under its own check in the
            # per-file Review breakdown.
            overflows = self.last_run_results.get("overflow_results") or []
            if overflows and self.comparison_gallery is not None:
                try:
                    self.comparison_gallery.load_overflows(overflows)
                except Exception as e:
                    print(f"[WARN] Could not publish margin overflows to the gallery: {e}")

            rows = self.last_run_results.get("metadata_rows") or []
            if rows and self.metadata_tab is not None:
                try:
                    self.metadata_tab.show_rows(rows)
                except Exception as e:
                    print(f"[WARN] Could not publish metadata: {e}")

        self.start_btn.configure(state="normal")
        self.open_folder_btn.configure(state="normal")
        self._unlock_inputs()  # Fix 6

        if HAS_CTK:
            self.progress_bar.stop()
            self.progress_bar.set(1.0)
        else:
            self.progress_bar.stop()

        if success and self.output_excel_path and os.path.exists(self.output_excel_path):
            self.open_excel_btn.configure(state="normal")
            if HAS_CTK:
                self.status_lbl.configure(text="\u25cf Inspection Complete (Report Ready)", text_color=DYNAMIC_GREEN)
            else:
                self.status_lbl.config(text="\u25cf Inspection Complete", fg=DYNAMIC_GREEN)
            note = (self.last_run_results or {}).get("region_note") or ""
            messagebox.showinfo(
                "Success",
                f"Inspection Complete!\n\n{note}\n\nExcel Report saved to:\n"
                f"{self.output_excel_path}")
        else:
            if HAS_CTK:
                self.status_lbl.configure(text="\u25cf Inspection Finished with Warnings/Errors", text_color=VIVID_MAGENTA)
            else:
                self.status_lbl.config(text="\u25cf Finished with Issues", fg=VIVID_MAGENTA)

    # ──────────────────────────────────────────────────────────
    # Post-Inspection & Log Actions
    # ──────────────────────────────────────────────────────────
    def _open_excel_report(self):
        if self.output_excel_path and os.path.exists(self.output_excel_path):
            os.startfile(self.output_excel_path)
        else:
            messagebox.showwarning("File Not Found", "Excel report has not been generated yet.")

    def _open_output_folder(self):
        if self.output_dir_path and os.path.exists(self.output_dir_path):
            os.startfile(self.output_dir_path)
        else:
            messagebox.showwarning("Folder Not Found", "Output folder does not exist.")

    # ──────────────────────────────────────────────────────────
    # Clearing the output folder
    # ──────────────────────────────────────────────────────────
    @staticmethod
    def _is_inside(child, parent):
        """True if `child` resolves to somewhere at or under `parent`."""
        try:
            c = os.path.realpath(child)
            pa = os.path.realpath(parent)
            return os.path.commonpath([c, pa]) == pa
        except Exception:
            return False        # different drives, or an unresolvable path

    def _clear_output_folder(self):
        """
        Empty the configured output directory, after an explicit confirmation.

        Refuses outright when the output folder would take the inputs with it -
        if someone points Output at the same folder as their PDFs, clearing it
        would destroy the source documents. That is checked before anything is
        counted, let alone deleted.
        """
        if self.is_running:
            messagebox.showinfo("Inspection Running",
                                "Wait for the current inspection to finish before clearing the output folder.")
            return

        out_dir = self.out_dir_var.get().strip().strip('"').strip("'")
        if not out_dir:
            messagebox.showerror("No Output Directory", "Choose an output directory first.")
            return
        if not os.path.isdir(out_dir):
            messagebox.showerror("Not Found", f"Output directory does not exist:\n{out_dir}")
            return

        real_out = os.path.realpath(out_dir)
        if os.path.dirname(real_out) == real_out:
            messagebox.showerror("Refused",
                                 f"{real_out} is a filesystem root. Refusing to clear it.")
            return

        eng = self.eng_pdf_var.get().strip().strip('"').strip("'")
        tr = self.tr_dir_var.get().strip().strip('"').strip("'")
        clashes = []
        if eng and self._is_inside(eng, real_out):
            clashes.append(f"the English master PDF ({os.path.basename(eng)})")
        if tr and (self._is_inside(tr, real_out) or self._is_inside(real_out, tr)):
            clashes.append("the translated PDFs folder")
        if clashes:
            messagebox.showerror(
                "Refused - Inputs Would Be Deleted",
                "The output folder contains " + " and ".join(clashes) + ".\n\n"
                f"{real_out}\n\nClearing it would delete your source documents. "
                "Point Output at a separate folder first.")
            return

        entries = os.listdir(real_out)
        if not entries:
            messagebox.showinfo("Already Empty", f"The output folder is already empty:\n{real_out}")
            return

        n_files = 0
        total = 0
        for root, _dirs, files in os.walk(real_out):
            for f in files:
                n_files += 1
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        n_dirs = sum(1 for e in entries if os.path.isdir(os.path.join(real_out, e)))

        if not messagebox.askyesno(
            "Clear Output Folder?",
            f"Delete everything inside:\n{real_out}\n\n"
            f"{n_files} file(s) in {n_dirs} sub-folder(s), {total / (1024 * 1024):.1f} MB.\n\n"
            "This includes the Excel report and all cropped and comparison images. "
            "Your PDFs are not touched.\n\nThis cannot be undone.",
            icon="warning", default="no"
        ):
            return

        removed, failed = 0, []
        for name in entries:
            target = os.path.join(real_out, name)
            try:
                if os.path.isdir(target) and not os.path.islink(target):
                    shutil.rmtree(target)
                else:
                    os.remove(target)
                removed += 1
            except OSError as e:
                failed.append(f"{name}: {e}")

        # Anything the app was pointing at at is gone now.
        self.output_excel_path = None
        try:
            self.open_excel_btn.configure(state="disabled")
        except Exception:
            pass
        if self.comparison_gallery is not None:
            try:
                self.comparison_gallery._all_rows = []
                self.comparison_gallery._selected = None
                self.comparison_gallery._refresh_list()
            except Exception:
                pass

        print(f"[Output] Cleared {removed} item(s) from {real_out}")
        msg = f"Removed {removed} item(s) from:\n{real_out}"
        if failed:
            msg += "\n\nCould not remove:\n" + "\n".join(failed[:6])
            if len(failed) > 6:
                msg += f"\n...and {len(failed) - 6} more"
            msg += "\n\nA file may be open in another program (the Excel report, for instance)."
            messagebox.showwarning("Cleared With Errors", msg)
        else:
            messagebox.showinfo("Output Folder Cleared", msg)

    def _on_close(self):
        """Save the configured paths, then close."""
        try:
            self._persist_paths()
        except Exception as e:
            print(f"[Settings] Could not save on close: {e}")
        self.destroy()

    def _open_log_file(self):
        log_path = logger_config.get_current_log_path()
        if not logger_config.open_current_log():
            messagebox.showinfo("Log File", f"Log file path:\n{log_path}")

    def _open_log_folder(self):
        log_dir = logger_config.get_log_dir()
        if not logger_config.open_log_folder():
            messagebox.showinfo("Log Folder", f"Logs folder:\n{log_dir}")

    # ──────────────────────────────────────────────────────────
    # Region Inspector Tab
    # ──────────────────────────────────────────────────────────
    def _build_region_tab(self):
        """
        Embed the Region Inspector as a tab of this window.

        Created empty: the user picks the PDFs on the Inspection tab, and
        _sync_region_inspector() hands the configured paths over via load().
        """
        try:
            from gui.region_dialog import RegionInspectorFrame
            self.region_inspector = RegionInspectorFrame(
                self._tab_region, on_results=self._on_region_results,
                on_templates_changed=self._on_templates_changed,
                on_margins_changed=self._on_margins_changed)
            self.region_inspector.pack(fill="both", expand=True)
            # Restore last session's margins. A template selected a moment
            # later overwrites them with its own, which is the right order.
            if self._remembered.get("margins"):
                self.region_inspector.set_margins(self._remembered["margins"])
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[ERROR] Failed to build Region Inspector tab: {e}\n{tb}")
            self.region_inspector = None
            ctk.CTkLabel(
                self._tab_region,
                text=f"Region Inspector unavailable:\n{e}",
                font=self._get_font(12),
                text_color=VIVID_MAGENTA,
                justify="left",
            ).pack(padx=20, pady=20, anchor="w")

    # ──────────────────────────────────────────────────────────
    # Keep the Region Inspector in step with the Inspection tab
    # ──────────────────────────────────────────────────────────
    def _watch_paths(self):
        """
        Load the configured master into the Region Inspector automatically.

        There is no longer a button to press: whatever is selected on the
        Inspection tab is what the inspector shows. The fields are watched
        rather than only the Browse buttons so a typed or pasted path works
        too, and the reload is debounced so typing does not re-open the PDF on
        every keystroke.
        """
        self._path_reload_job = None
        for var in (self.eng_pdf_var, self.tr_dir_var, self.out_dir_var):
            var.trace_add("write", self._on_path_changed)
        self.after(300, self._sync_region_inspector)

    def _on_path_changed(self, *_args):
        if getattr(self, "_path_reload_job", None) is not None:
            try:
                self.after_cancel(self._path_reload_job)
            except Exception:
                pass
        self._path_reload_job = self.after(400, self._sync_region_inspector)

    def _persist_paths(self):
        """
        Remember whatever is currently configured, so the next launch starts here.

        Each field is stored independently and only when it points at something
        real, so a half-typed path never overwrites a good remembered one.
        """
        eng = self.eng_pdf_var.get().strip().strip('"').strip("'")
        tr = self.tr_dir_var.get().strip().strip('"').strip("'")
        out = self.out_dir_var.get().strip().strip('"').strip("'")
        settings.save_paths(
            # The master may be given either way round - the folder holding it,
            # which is what the picker offers, or the PDF itself, which is what
            # a pasted path and every older settings file look like.
            english_pdf=eng if settings.is_master_path(eng) else None,
            translated_dir=tr if tr and os.path.exists(tr) else None,
            output_dir=out if out and os.path.isdir(out) else None,
        )
        try:
            tmpl = (self.template_var.get() or "").strip()
            if tmpl and tmpl != "(none)":
                settings.save({"template": tmpl})
        except Exception:
            pass

        # Margins are remembered separately from the template, so a user who
        # tunes them without saving a template still finds them next launch.
        try:
            if self.region_inspector is not None:
                settings.save({"margins": page_margins.to_storage(
                    self.region_inspector.get_margins())})
        except Exception:
            pass

    def _sync_region_inspector(self):
        """Point the inspector at the currently configured paths, if usable."""
        self._path_reload_job = None

        # A master path typed or pasted into the field (rather than browsed)
        # still re-derives its batch folder and refreshes the hint. Idempotent -
        # it writes a variable only when the value truly changes - so it cannot
        # loop with the path trace that called us.
        try:
            self._sync_batch_from_master()
        except Exception as e:
            print(f"[WARN] Could not resolve the batch from the master: {e}")

        # Persist first: the output folder is worth remembering even when no
        # master has been chosen yet, and the reload below returns early then.
        self._persist_paths()

        if self.metadata_tab is not None:
            try:
                self.metadata_tab.documents_changed()
            except Exception:
                pass

        if self.region_inspector is None:
            return

        # Never re-open a PDF into the inspector while a run is working. The
        # render happens on the main thread, and doing it while every core is
        # busy scanning is what makes switching tabs mid-run feel like the
        # window has broken. The paths cannot change during a run anyway - the
        # fields are locked - so this only defers a redundant reload.
        if getattr(self, "is_running", False):
            self._path_reload_job = self.after(1500, self._sync_region_inspector)
            return

        eng_path = self.eng_pdf_var.get().strip().strip('"').strip("'")
        eng, _err = (spotcheck_engine.resolve_master_pdf(eng_path)
                     if eng_path else (None, None))
        tr = self.tr_dir_var.get().strip().strip('"').strip("'")
        out = self.out_dir_var.get().strip().strip('"').strip("'")

        if not eng or not os.path.isfile(eng):
            return
        if os.path.abspath(eng) == getattr(self, "_loaded_master", None) and \
           os.path.abspath(tr or "") == getattr(self, "_loaded_target", "") and \
           os.path.abspath(out or "") == getattr(self, "_loaded_out", ""):
            return
        # A new master means the inspector clears and re-applies the selected
        # stylesheet. Forget which template is "already loaded" so that path is
        # not short-circuited: the guard exists to stop redundant reloads of the
        # same template on the same document, not to skip the one reload that
        # matters when the document changes underneath it.
        if os.path.abspath(eng) != getattr(self, "_loaded_master", None):
            self._loaded_template = None

        try:
            self.region_inspector.load(eng_pdf_path=eng, tr_target_path=tr,
                                       output_dir=out or None)
            self._loaded_master = os.path.abspath(eng)
            self._loaded_target = os.path.abspath(tr or "")
            self._loaded_out = os.path.abspath(out or "")
            print(f"[Region Inspector] Loaded {os.path.basename(eng)}")
        except Exception as e:
            print(f"[WARN] Could not load master into the Region Inspector: {e}")

    def _build_text_checks_tab(self):
        """
        Colliding text, and English left in a translation.

        Neither question is about a place on the page, so neither belongs in the
        region stylesheet: a collision happens wherever a line runs long, and a
        missed segment is wherever the translator's eye slipped.
        """
        try:
            from gui.text_checks_tab import TextChecksFrame
            self.text_checks_tab = TextChecksFrame(
                self._tab_text_checks,
                get_documents=self._text_check_documents,
                get_margins=self._active_margins,
                get_output_dir=lambda: (self.out_dir_var.get() or "").strip(),
                on_results=self._on_text_check_results,
            )
            self.text_checks_tab.pack(fill="both", expand=True)
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[ERROR] Failed to build Text Checks tab: {e}\n{tb}")
            self.text_checks_tab = None
            ctk.CTkLabel(
                self._tab_text_checks,
                text=f"Text checks unavailable:\n{e}",
                font=self._get_font(12), text_color=VIVID_MAGENTA, justify="left",
            ).pack(padx=20, pady=20, anchor="w")

    def _text_check_documents(self):
        """(master, [translations]) - the overlap check reads both sides."""
        docs = self._documents_in_scope()
        if not docs:
            return None, []
        return docs[0], docs[1:]

    def _on_text_check_results(self, overlaps, missed):
        """Hand the findings to the Review tab, where they get worked through."""
        if getattr(self, "comparison_gallery", None) is None:
            return
        try:
            self.comparison_gallery.load_text_checks(overlaps, missed)
        except Exception as e:
            print(f"[WARN] Could not show text checks in the Review tab: {e}")

    def _build_metadata_tab(self):
        """Embed the document metadata tab beside the Region Inspector."""
        try:
            from gui.metadata_tab import MetadataFrame
            self.metadata_tab = MetadataFrame(
                self._tab_metadata,
                get_documents=self._documents_in_scope,
                get_margins=self._active_margins,
            )
            self.metadata_tab.pack(fill="both", expand=True)
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[ERROR] Failed to build Meta Data tab: {e}\n{tb}")
            self.metadata_tab = None
            ctk.CTkLabel(
                self._tab_metadata,
                text=f"Metadata view unavailable:\n{e}",
                font=self._get_font(12), text_color=VIVID_MAGENTA, justify="left",
            ).pack(padx=20, pady=20, anchor="w")

    def _documents_in_scope(self):
        """
        Every PDF this run is about: the master first, then the translations.

        Master first is not cosmetic - the metadata tab compares every other row
        against the first one to find the odd document out.
        """
        out = []
        # The English side is a folder now, so the master has to be resolved
        # rather than assumed to be the path itself.
        eng = (self.eng_pdf_var.get() or "").strip().strip('"').strip("'")
        if eng:
            master, _err = spotcheck_engine.resolve_master_pdf(eng)
            if master:
                out.append(os.path.abspath(master))

        target = (self.tr_dir_var.get() or "").strip().strip('"').strip("'")
        if target and os.path.isfile(target) and target.lower().endswith(".pdf"):
            out.append(os.path.abspath(target))
        elif target and os.path.isdir(target):
            for f in sorted(os.listdir(target)):
                if f.lower().endswith(".pdf"):
                    out.append(os.path.join(os.path.abspath(target), f))

        seen, unique = set(), []
        for p in out:
            key = os.path.normcase(p)
            if key not in seen:
                seen.add(key)
                unique.append(p)
        return unique

    def _build_comparisons_tab(self):
        """Embed the comparison-image gallery as a third tab."""
        try:
            from gui.comparison_gallery import ComparisonGalleryFrame
            self.comparison_gallery = ComparisonGalleryFrame(
                self._tab_comparisons,
                is_active=lambda: self.tabview is not None
                and self.tabview.get() == TAB_COMPARISONS,
                resolve_paths=self._resolve_result_pdfs,
                get_margins=self._active_margins,
            )
            self.comparison_gallery.pack(fill="both", expand=True)
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[ERROR] Failed to build Review tab: {e}\n{tb}")
            self.comparison_gallery = None
            ctk.CTkLabel(
                self._tab_comparisons,
                text=f"Comparison gallery unavailable:\n{e}",
                font=self._get_font(12), text_color=VIVID_MAGENTA, justify="left",
            ).pack(padx=20, pady=20, anchor="w")

    def _regions_for_run(self):
        """
        The stylesheet regions this run should check, and where they came from.

        The saved template wins, because a run should be reproducible from its
        name. Regions drawn but not yet saved are the fallback, so experimenting
        does not require saving first. Neither, and the section is skipped.
        """
        insp = self.region_inspector
        if insp is None:
            return [], "the Region Inspector is unavailable"
        tmpl = (self.template_var.get() or "").strip() if hasattr(self, "template_var") else ""
        if tmpl and tmpl != "(none)":
            try:
                data = templates_store.load_template(tmpl)
                if data and data.get("regions"):
                    return data["regions"], f"template '{tmpl}'"
            except Exception as e:
                print(f"[Templates] Could not load '{tmpl}': {e}")
        if insp.regions:
            return list(insp.regions), "the regions currently marked in the Region Inspector"
        return [], "no template selected and no regions marked"

    def _resolve_result_pdfs(self, row):
        """
        Turn a gallery row's file names back into paths on disk.

        The engine reports basenames, because that is what belongs in a report.
        The side-by-side view needs the documents themselves, and this window is
        the only place that knows where the user pointed it.
        """
        eng = (self.eng_pdf_var.get() or "").strip().strip('"').strip("'")
        master, _err = spotcheck_engine.resolve_master_pdf(eng) if eng else (None, None)
        if master and row.get("eng_name") and \
                os.path.basename(master) != row["eng_name"]:
            # The configured master has changed since the run that produced this
            # row. Say nothing and use it anyway - it is the only one we have,
            # and the header names the file being shown.
            pass

        wanted = os.path.basename(row.get("tr_name") or "")
        target = (self.tr_dir_var.get() or "").strip().strip('"').strip("'")
        translated = None
        if wanted and target:
            if os.path.isfile(target) and os.path.basename(target) == wanted:
                translated = target
            elif os.path.isdir(target):
                exact = os.path.join(target, wanted)
                if os.path.isfile(exact):
                    translated = exact
                else:
                    # Rows carry a shortened name for some sources, so fall back
                    # to a unique prefix match rather than giving up.
                    stem = os.path.splitext(wanted)[0]
                    hits = [f for f in os.listdir(target)
                            if f.lower().endswith(".pdf") and f.startswith(stem)]
                    if len(hits) == 1:
                        translated = os.path.join(target, hits[0])
        elif target and os.path.isfile(target):
            translated = target

        return master, translated

    # ──────────────────────────────────────────────────────────
    # Stylesheet templates
    # ──────────────────────────────────────────────────────────
    def _on_margins_changed(self, margins):
        """The Region Inspector's margins were edited; remember them."""
        try:
            settings.save({"margins": page_margins.to_storage(margins)})
        except Exception as e:
            print(f"[Settings] Could not remember the margins: {e}")

    def _active_margins(self):
        """
        The margins a run should use.

        The Region Inspector holds the live value - it is where they are edited
        and it is loaded from the selected template - so it wins. Without it
        (the plain-Tk fallback UI), fall back to the selected template on disk,
        then to what was remembered last session.
        """
        if self.region_inspector is not None:
            try:
                return self.region_inspector.get_margins()
            except Exception:
                pass
        tmpl = (self.template_var.get() or "").strip() if hasattr(self, "template_var") else ""
        if tmpl and tmpl != "(none)":
            try:
                return templates_store.margins_for_template(tmpl)
            except Exception:
                pass
        return page_margins.normalize(self._remembered.get("margins"))

    def refresh_template_dropdown(self, select=None):
        """Reload the template list into the Inspection tab's dropdown."""
        try:
            names = templates_store.list_templates()
            self.template_menu.configure(values=names or ["(none)"])
            if select and select in names:
                self.template_var.set(select)
            elif self.template_var.get() not in names:
                self.template_var.set(names[0] if names else "(none)")
            return names
        except Exception as e:
            print(f"[Templates] Could not refresh the dropdown: {e}")
            return []

    def _on_templates_changed(self, name):
        """The Region Inspector saved or deleted a template."""
        self.refresh_template_dropdown(select=name)
        self._persist_paths()

    def _on_template_selected(self, name=None):
        """Load the chosen template's regions into the Region Inspector."""
        name = (name or self.template_var.get() or "").strip()
        if not name or name == "(none)" or self.region_inspector is None:
            return
        if name == getattr(self, "_loaded_template", None):
            return
        if self.region_inspector.apply_template(name, announce=False):
            self._loaded_template = name
            try:
                self.region_inspector.refresh_template_list(select=name)
            except Exception:
                pass
            self._persist_paths()

    def _on_region_results(self, results):
        """Called by the Region Inspector when a batch check finishes."""
        if self.comparison_gallery is not None:
            try:
                self.comparison_gallery.load_region_results(results)
            except Exception as e:
                print(f"[WARN] Could not publish region results to the gallery: {e}")

def main():
    """Launch the SpotCheck desktop application."""
    _ensure_utf8_console()
    app = SpotCheckApp()
    app.mainloop()