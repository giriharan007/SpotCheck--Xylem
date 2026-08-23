"""
gui/app_window.py

Main SpotCheck desktop window.

Presentation layer only: the inspection itself is executed by core.pipeline on a
worker thread, and the Region Inspector lives in gui.region_dialog. All colors
and fonts come from gui.theme, so the two windows can no longer drift apart.
"""

import os
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

# Tab labels (also used as CTkTabview keys)
TAB_INSPECTION = "  Inspection  "
TAB_REGION = "  Region Inspector  "
TAB_COMPARISONS = "  Review  "


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
        self.geometry("1320x900")
        self.minsize(1100, 740)

        if HAS_CTK:
            self.configure(fg_color=UI_BG_CANVAS)
        else:
            self.configure(bg=UI_BG_CANVAS)

        self.text_queue = queue.Queue()
        self.is_running = False
        self.output_excel_path = None
        self.output_dir_path = None

        # Fix 6: Track browse buttons for disable/enable during inspection
        self._browse_buttons = []

        # Whatever was configured last time, so the user does not re-pick the
        # same three paths on every launch. Stale entries are dropped by
        # load_paths(), so a deleted folder falls back to the built-in default.
        self._remembered = settings.load_paths()
        if self._remembered:
            print(f"[Settings] Restored {len(self._remembered)} path(s) from "
                  f"{settings.get_settings_path()}")

        self.region_inspector = None
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
            self._tab_inspection = self.tabview.add(TAB_INSPECTION)
            self._tab_region = self.tabview.add(TAB_REGION)
            self._tab_comparisons = self.tabview.add(TAB_COMPARISONS)
            self._body = self._tab_inspection
        else:
            self._body = self

        self._build_ui()

        if HAS_CTK:
            self._build_region_tab()
            self._build_comparisons_tab()
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

            # Row 1: English Master PDF
            ctk.CTkLabel(
                config_card,
                text="English Master PDF:",
                font=self._get_font(12, "bold"),
                text_color=DEPENDABLE_BLUE
            ).grid(row=1, column=0, sticky="w", padx=16, pady=6)

            default_eng = self._remembered.get("english_pdf") or (
                os.path.abspath(r"Input\English\894387_5.0_en-US_2026-04_IOM.Start350.pdf")
                if os.path.exists(r"Input\English\894387_5.0_en-US_2026-04_IOM.Start350.pdf") else "")
            self.eng_pdf_var = ctk.StringVar(value=default_eng)
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

            btn_eng = ctk.CTkButton(
                config_card,
                text="Browse PDF",
                font=self._get_font(11, "bold"),
                fg_color=XYLEM_BLUE,
                hover_color=UI_HOVER_BLUE,
                text_color=NEUTRAL_WHITE,
                width=110,
                height=34,
                command=self._browse_eng_pdf
            )
            btn_eng.grid(row=1, column=2, padx=(0, 16), pady=6)
            self._browse_buttons.append(btn_eng)

            # Row 2: Translated Target Folder
            ctk.CTkLabel(
                config_card,
                text="Translated PDFs Folder:",
                font=self._get_font(12, "bold"),
                text_color=DEPENDABLE_BLUE
            ).grid(row=2, column=0, sticky="w", padx=16, pady=6)

            default_tr = self._remembered.get("translated_dir") or (
                os.path.abspath(r"Input\Translated") if os.path.exists(r"Input\Translated") else "")
            self.tr_dir_var = ctk.StringVar(value=default_tr)
            self.tr_entry = ctk.CTkEntry(
                config_card,
                textvariable=self.tr_dir_var,
                font=self._get_font(11),
                fg_color=UI_CARD_WELL,
                border_color=UI_BORDER,
                text_color=DEPENDABLE_BLUE,
                height=34
            )
            self.tr_entry.grid(row=2, column=1, sticky="ew", padx=(0, 10), pady=6)

            btn_tr = ctk.CTkButton(
                config_card,
                text="Browse Folder",
                font=self._get_font(11, "bold"),
                fg_color=XYLEM_BLUE,
                hover_color=UI_HOVER_BLUE,
                text_color=NEUTRAL_WHITE,
                width=110,
                height=34,
                command=self._browse_tr_dir
            )
            btn_tr.grid(row=2, column=2, padx=(0, 16), pady=6)
            self._browse_buttons.append(btn_tr)

            # Row 3: Output Directory
            ctk.CTkLabel(
                config_card,
                text="Output Directory:",
                font=self._get_font(12, "bold"),
                text_color=DEPENDABLE_BLUE
            ).grid(row=3, column=0, sticky="w", padx=16, pady=(6, 14))

            self.template_var = ctk.StringVar(value=self._remembered.get("template") or "")
            self.out_dir_var = ctk.StringVar(
                value=self._remembered.get("output_dir") or os.path.abspath(r"Output"))
            self.out_entry = ctk.CTkEntry(
                config_card,
                textvariable=self.out_dir_var,
                font=self._get_font(11),
                fg_color=UI_CARD_WELL,
                border_color=UI_BORDER,
                text_color=DEPENDABLE_BLUE,
                height=34
            )
            self.out_entry.grid(row=3, column=1, sticky="ew", padx=(0, 10), pady=(6, 14))

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
            btn_out.grid(row=3, column=2, padx=(0, 16), pady=(6, 14))
            self._browse_buttons.append(btn_out)

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

            self.log_textbox = ctk.CTkTextbox(
                log_frame,
                font=ctk.CTkFont(family="Consolas", size=11),
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

            self.log_textbox = tk.Text(log_frame, font=("Consolas", 10), bg=DEPENDABLE_BLUE, fg=NEUTRAL_WHITE)
            self.log_textbox.pack(fill="both", expand=True)

    # ──────────────────────────────────────────────────────────
    # File / Directory Pickers
    # ──────────────────────────────────────────────────────────
    def _browse_eng_pdf(self):
        f = filedialog.askopenfilename(
            title="Select English Master PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")]
        )
        if f:
            self.eng_pdf_var.set(os.path.abspath(f))

    def _browse_tr_dir(self):
        d = filedialog.askdirectory(title="Select Translated PDFs Directory")
        if d:
            self.tr_dir_var.set(os.path.abspath(d))

    def _browse_out_dir(self):
        d = filedialog.askdirectory(title="Select Output Directory")
        if d:
            self.out_dir_var.set(os.path.abspath(d))

    # ──────────────────────────────────────────────────────────
    # Log Console
    # ──────────────────────────────────────────────────────────
    def _append_log(self, text):
        if HAS_CTK:
            self.log_textbox.insert("end", text)
            self.log_textbox.see("end")
        else:
            self.log_textbox.insert("end", text)
            self.log_textbox.see("end")

    def _check_queue(self):
        while not self.text_queue.empty():
            try:
                msg = self.text_queue.get_nowait()
                self._append_log(msg)
            except queue.Empty:
                break
        self.after(100, self._check_queue)

    # ──────────────────────────────────────────────────────────
    # Fix 6: Lock / Unlock all input controls during inspection
    # ──────────────────────────────────────────────────────────
    def _lock_inputs(self):
        """Disable all entries and browse buttons to prevent edits during inspection."""
        for btn in self._browse_buttons:
            btn.configure(state="disabled")
        self.eng_entry.configure(state="disabled")
        self.tr_entry.configure(state="disabled")
        self.out_entry.configure(state="disabled")
        try:
            self.clear_output_btn.configure(state="disabled")
        except Exception:
            pass

    def _unlock_inputs(self):
        """Re-enable all entries and browse buttons after inspection completes."""
        for btn in self._browse_buttons:
            btn.configure(state="normal")
        self.eng_entry.configure(state="normal")
        self.tr_entry.configure(state="normal")
        self.out_entry.configure(state="normal")
        try:
            self.clear_output_btn.configure(state="normal")
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────
    # Inspection Launch (with Fix 3 + Fix 4)
    # ──────────────────────────────────────────────────────────
    def _start_inspection(self):
        if self.is_running:
            return

        eng_pdf = self.eng_pdf_var.get().strip()
        tr_target = self.tr_dir_var.get().strip()
        out_dir = self.out_dir_var.get().strip()

        # Fix 4: Validate empty/whitespace inputs with clear messages
        if not eng_pdf:
            messagebox.showerror("Input Required", "Please select an English Master PDF file.\nUse the 'Browse PDF' button to select a file.")
            return
        if not tr_target:
            messagebox.showerror("Input Required", "Please select a Translated PDFs folder.\nUse the 'Browse Folder' button to select a directory.")
            return
        if not out_dir:
            messagebox.showerror("Input Required", "Please specify an Output Directory.\nUse the 'Browse Output' button to select a directory.")
            return

        if not os.path.exists(eng_pdf):
            messagebox.showerror("File Not Found", f"English Master PDF does not exist:\n{eng_pdf}\n\nPlease verify the path and try again.")
            return
        if not os.path.exists(tr_target):
            messagebox.showerror("Path Not Found", f"Translated target path does not exist:\n{tr_target}\n\nPlease verify the path and try again.")
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
            self.status_lbl.configure(text="\u25cf Inspecting PDFs...", text_color=RADIANT_ORANGE)
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
        self._append_log(f"Ignored Margins       : {page_margins.describe(run_margins)}\n")

        t = threading.Thread(target=self._run_inspection_thread,
                             args=(eng_pdf, tr_target, out_dir, run_margins), daemon=True)
        t.start()

    # ──────────────────────────────────────────────────────────
    # Worker Thread (with Fix 5: full traceback on errors)
    # ──────────────────────────────────────────────────────────
    def _run_inspection_thread(self, eng_pdf, tr_target, out_dir, run_margins=None):
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        redirector = TextRedirector(self.text_queue)
        sys.stdout = redirector
        sys.stderr = redirector

        try:
            self.last_run_results = spotcheck_engine.run_quality_inspection(
                eng_pdf, tr_target, out_dir, margins=run_margins)
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
            self.after(0, self._on_inspection_finished, success)

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
            messagebox.showinfo("Success", f"Inspection Complete!\nExcel Report saved to:\n{self.output_excel_path}")
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
            english_pdf=eng if eng and os.path.isfile(eng) else None,
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

        # Persist first: the output folder is worth remembering even when no
        # master has been chosen yet, and the reload below returns early then.
        self._persist_paths()

        if self.region_inspector is None:
            return
        eng = self.eng_pdf_var.get().strip().strip('"').strip("'")
        tr = self.tr_dir_var.get().strip().strip('"').strip("'")
        out = self.out_dir_var.get().strip().strip('"').strip("'")

        if not eng or not os.path.isfile(eng):
            return
        if os.path.abspath(eng) == getattr(self, "_loaded_master", None) and \
           os.path.abspath(tr or "") == getattr(self, "_loaded_target", "") and \
           os.path.abspath(out or "") == getattr(self, "_loaded_out", ""):
            return
        try:
            self.region_inspector.load(eng_pdf_path=eng, tr_target_path=tr,
                                       output_dir=out or None)
            self._loaded_master = os.path.abspath(eng)
            self._loaded_target = os.path.abspath(tr or "")
            self._loaded_out = os.path.abspath(out or "")
            print(f"[Region Inspector] Loaded {os.path.basename(eng)}")
        except Exception as e:
            print(f"[WARN] Could not load master into the Region Inspector: {e}")

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

    def _resolve_result_pdfs(self, row):
        """
        Turn a gallery row's file names back into paths on disk.

        The engine reports basenames, because that is what belongs in a report.
        The side-by-side view needs the documents themselves, and this window is
        the only place that knows where the user pointed it.
        """
        eng = (self.eng_pdf_var.get() or "").strip().strip('"').strip("'")
        master = eng if eng and os.path.isfile(eng) else None
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
