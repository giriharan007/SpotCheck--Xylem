"""
app_gui.py

Interactive Desktop GUI for SpotCheck PDF Quality & Visual Inspection Engine.
Fully styled according to official Xylem Brand Identity Guidelines:
  - Primary Palette  : Xylem Blue (#007DA3), Dependable Blue (#003E51), Clarity Blue (#67DFFF), Dynamic Green (#61D604)
  - Secondary Palette: Inspired Teal (#20846F), Uplifting Aqua (#29CCBB), Resilient Purple (#6600C5), Vivid Magenta (#D300F2), Radiant Orange (#F96C00)
  - Neutral Palette  : Dark Gray (#555555), Medium Gray (#A8A8A8), Light Gray (#DBDBDB), White (#FFFFFF), Black (#000000)
  - Typography       : Arial (Xylem-approved desktop system font), with Roboto preferred if installed
"""

import os
import sys
import threading
import queue
import traceback

# ──────────────────────────────────────────────────────────────
# Fix 1: Initialize Runtime Logger & Windows DLL Search Paths
# MUST run before any third-party or sub-module imports.
# ──────────────────────────────────────────────────────────────
import logger_config
logger_config.init_logging()


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
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, messagebox
    ctk.set_appearance_mode("Light")
    ctk.set_default_color_theme("blue")
else:
    import tkinter.font as tkfont

import main as spotcheck_engine

# ==============================================================================
# OFFICIAL XYLEM BRAND PALETTE
# ==============================================================================
# Primary Palette
XYLEM_BLUE      = "#007DA3"   # PMS 7704C - Dominant Primary Color
DEPENDABLE_BLUE = "#003E51"   # PMS 3035C - Second Dominant (Headings, Heavy Cards, Strong UI)
CLARITY_BLUE    = "#67DFFF"   # PMS 2197C - Vibrant Energy Accent
DYNAMIC_GREEN   = "#61D604"   # PMS 2287C - Energy, Success & Action Accent

# Secondary Palette (Specialized Functional & Visual Accents)
INSPIRED_TEAL   = "#20846F"   # PMS 569C
UPLIFTING_AQUA  = "#29CCBB"   # PMS 3255C
RESILIENT_PURPLE= "#6600C5"   # PMS 2091C
VIVID_MAGENTA   = "#D300F2"   # PMS Purple C
RADIANT_ORANGE  = "#F96C00"   # PMS 1505C - Warnings / Processing

# Neutral Palette
NEUTRAL_BLACK   = "#000000"
NEUTRAL_DARK_GR = "#555555"   # PMS 425 - Secondary Muted Text
NEUTRAL_MED_GR  = "#A8A8A8"   # PMS Cool Gray 6 - Dividers
NEUTRAL_LIGHT_GR= "#DBDBDB"   # PMS Cool Gray 1 - Borders / Containers
NEUTRAL_WHITE   = "#FFFFFF"

# Functional UI Surface Colors
UI_BG_CANVAS    = "#EEF5FA"   # Soft cool neutral background
UI_CARD_BG      = "#FFFFFF"   # Card surface
UI_CARD_WELL    = "#E2EDF7"   # Secondary container well
UI_BORDER       = "#D0DFEB"   # Soft blue-gray divider border
UI_HOVER_BLUE   = "#0095C2"   # Interactive button hover blue
UI_DARK_HOVER   = "#002834"   # Dependable blue hover

# ──────────────────────────────────────────────────────────────
# Fix 2: Typography — resolve font at runtime
# Xylem standard: Roboto preferred, Arial as desktop system fallback.
# ──────────────────────────────────────────────────────────────
FONT_FAMILY_PREFERRED = "Roboto"
FONT_FAMILY_FALLBACK  = "Arial"


def _resolve_font_family():
    """
    Check if Roboto is installed on this system.
    If yes → use Roboto (Xylem primary typeface).
    If no  → fall back to Arial (Xylem approved desktop system font).
    """
    try:
        probe = tk.Tk()
        probe.withdraw()
        available = set(tkfont.families())
        probe.destroy()
        if FONT_FAMILY_PREFERRED in available:
            print(f"[Typography] Using Roboto (Xylem primary typeface)")
            return FONT_FAMILY_PREFERRED
        else:
            print(f"[Typography] Roboto not installed — using Arial (Xylem desktop system font)")
            return FONT_FAMILY_FALLBACK
    except Exception:
        return FONT_FAMILY_FALLBACK


# Resolved at module load time (before any widget creation)
FONT_FAMILY = _resolve_font_family()


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
        self.geometry("1020x760")
        self.minsize(880, 640)

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

        self._build_ui()
        self._check_queue()

    def _get_font(self, size=12, weight="normal", family=None):
        fam = family or FONT_FAMILY
        if HAS_CTK:
            return ctk.CTkFont(family=fam, size=size, weight=weight)
        return (fam, size, weight)

    def _build_ui(self):
        if HAS_CTK:
            # ------------------------------------------------------------------
            # 1. Header Banner (Dependable Blue #003E51 with Clarity Blue Accent)
            # ------------------------------------------------------------------
            header_frame = ctk.CTkFrame(
                self,
                corner_radius=12,
                fg_color=DEPENDABLE_BLUE,
                border_width=1,
                border_color=XYLEM_BLUE
            )
            header_frame.pack(fill="x", padx=18, pady=(16, 12))

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
                self,
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

            default_eng = os.path.abspath(r"Input\English\894387_5.0_en-US_2026-04_IOM.Start350.pdf") if os.path.exists(r"Input\English\894387_5.0_en-US_2026-04_IOM.Start350.pdf") else ""
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

            default_tr = os.path.abspath(r"Input\Translated") if os.path.exists(r"Input\Translated") else ""
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

            self.out_dir_var = ctk.StringVar(value=os.path.abspath(r"Output"))
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
            action_frame = ctk.CTkFrame(self, fg_color="transparent")
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
                state="disabled",
                height=40,
                command=self._open_output_folder
            )
            self.open_folder_btn.pack(side="left", padx=6)

            self.status_lbl = ctk.CTkLabel(
                action_frame,
                text="\u25cf Ready to inspect",
                font=self._get_font(12, "bold"),
                text_color=XYLEM_BLUE
            )
            self.status_lbl.pack(side="right", padx=10)

            # Progress Bar (Xylem Blue)
            self.progress_bar = ctk.CTkProgressBar(
                self,
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
                self,
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
            self.eng_pdf_var = tk.StringVar(value=os.path.abspath(r"Input\English\894387_5.0_en-US_2026-04_IOM.Start350.pdf") if os.path.exists(r"Input\English\894387_5.0_en-US_2026-04_IOM.Start350.pdf") else "")
            self.eng_entry = tk.Entry(config_frame, textvariable=self.eng_pdf_var, font=(FONT_FAMILY_FALLBACK, 10), width=70)
            self.eng_entry.grid(row=0, column=1, sticky="ew", padx=8, pady=5)
            btn_eng_tk = tk.Button(config_frame, text="Browse...", bg=XYLEM_BLUE, fg=NEUTRAL_WHITE, command=self._browse_eng_pdf)
            btn_eng_tk.grid(row=0, column=2, padx=4, pady=5)
            self._browse_buttons.append(btn_eng_tk)

            tk.Label(config_frame, text="Translated Folder:", font=(FONT_FAMILY_FALLBACK, 10, "bold"), fg=DEPENDABLE_BLUE, bg=UI_CARD_BG).grid(row=1, column=0, sticky="w", pady=5)
            self.tr_dir_var = tk.StringVar(value=os.path.abspath(r"Input\Translated") if os.path.exists(r"Input\Translated") else "")
            self.tr_entry = tk.Entry(config_frame, textvariable=self.tr_dir_var, font=(FONT_FAMILY_FALLBACK, 10), width=70)
            self.tr_entry.grid(row=1, column=1, sticky="ew", padx=8, pady=5)
            btn_tr_tk = tk.Button(config_frame, text="Browse...", bg=XYLEM_BLUE, fg=NEUTRAL_WHITE, command=self._browse_tr_dir)
            btn_tr_tk.grid(row=1, column=2, padx=4, pady=5)
            self._browse_buttons.append(btn_tr_tk)

            tk.Label(config_frame, text="Output Directory:", font=(FONT_FAMILY_FALLBACK, 10, "bold"), fg=DEPENDABLE_BLUE, bg=UI_CARD_BG).grid(row=2, column=0, sticky="w", pady=5)
            self.out_dir_var = tk.StringVar(value=os.path.abspath(r"Output"))
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

    def _unlock_inputs(self):
        """Re-enable all entries and browse buttons after inspection completes."""
        for btn in self._browse_buttons:
            btn.configure(state="normal")
        self.eng_entry.configure(state="normal")
        self.tr_entry.configure(state="normal")
        self.out_entry.configure(state="normal")

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

        t = threading.Thread(target=self._run_inspection_thread, args=(eng_pdf, tr_target, out_dir), daemon=True)
        t.start()

    # ──────────────────────────────────────────────────────────
    # Worker Thread (with Fix 5: full traceback on errors)
    # ──────────────────────────────────────────────────────────
    def _run_inspection_thread(self, eng_pdf, tr_target, out_dir):
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        redirector = TextRedirector(self.text_queue)
        sys.stdout = redirector
        sys.stderr = redirector

        try:
            spotcheck_engine.run_quality_inspection(eng_pdf, tr_target, out_dir)
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

    def _open_log_file(self):
        log_path = logger_config.get_current_log_path()
        if not logger_config.open_current_log():
            messagebox.showinfo("Log File", f"Log file path:\n{log_path}")

    def _open_log_folder(self):
        log_dir = logger_config.get_log_dir()
        if not logger_config.open_log_folder():
            messagebox.showinfo("Log Folder", f"Logs folder:\n{log_dir}")


def main():
    _ensure_utf8_console()
    app = SpotCheckApp()
    app.mainloop()


if __name__ == "__main__":
    main()
