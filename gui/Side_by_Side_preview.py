"""
gui/Side_by_Side_preview.py

Side-by-side page viewer: the master page and its translation shown side by side.

Opened from the Review tab ("⇔ Side by Side").
Allows reviewing multi-language translated documents against the master English
source page by page.

Features:
- Completely independent scrolling and page navigation for both PDFs.
- Mouse wheel navigation: hover over either PDF and scroll down (next page) or up (prev page).
- Fast, clean side-by-side rendering with instant in-memory pre-caching.
- Fit to Page (default), Fit Width, and custom zoom levels.
- Smooth click-and-drag panning when zoomed in.
- Optional "Sync scrolling" toggle if user wants linked movement.
- Optional algorithmic diff overlay ("Highlight Differences") on demand.
"""

import os
import queue
import threading
import time

import customtkinter as ctk
import tkinter as tk
from PIL import Image, ImageTk

try:
    import fitz
except ImportError:
    import pymupdf as fitz

from gui import theme
from gui.theme import (
    XYLEM_BLUE,
    DEPENDABLE_BLUE,
    DYNAMIC_GREEN,
    RADIANT_ORANGE,
    UI_BG_CANVAS,
    UI_CARD_BG,
    UI_CARD_WELL,
    UI_BORDER,
    UI_HOVER_BLUE,
    NEUTRAL_WHITE,
    NEUTRAL_DARK_GR,
)
from core import page_diff

# Resampling filter compatible with all Pillow versions
_RESAMPLE = getattr(Image, "Resampling", Image).BILINEAR

FOCUS_COLOR = DYNAMIC_GREEN

PAGE_WHEEL_STEP = 60
WHEEL_COOLDOWN_SEC = 0.35

# Module-level LRU cache for rendered base page images: (pdf_path, page_no, dpi) -> (PIL.Image, (pt_w, pt_h))
_RENDER_CACHE = {}
_MAX_CACHE_ENTRIES = 40


def _get_rendered_page(pdf_path, page_no, dpi=150):
    """Retrieve or render a PDF page as a PIL image with dimensions in points."""
    key = (os.path.abspath(pdf_path), page_no, dpi)
    if key in _RENDER_CACHE:
        return _RENDER_CACHE[key]

    with fitz.open(pdf_path) as doc:
        if not 1 <= page_no <= len(doc):
            raise IndexError(f"{os.path.basename(pdf_path)} has no page {page_no} (total {len(doc)})")
        page = doc[page_no - 1]
        pix = page.get_pixmap(dpi=dpi)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        rect = page.rect
        result = (img, (rect.width, rect.height))

    if len(_RENDER_CACHE) >= _MAX_CACHE_ENTRIES:
        _RENDER_CACHE.pop(next(iter(_RENDER_CACHE)))
    _RENDER_CACHE[key] = result
    return result


def _wheel_step(event):
    """
    One wheel notch as +1 (down/next) or -1 (up/prev), or 0.
    Handles Linux (buttons 4/5), Windows delta (+/-120), and macOS trackpads.
    """
    num = getattr(event, "num", 0)
    if num in (4, 5):
        return 1 if num == 5 else -1
    delta = getattr(event, "delta", 0)
    if not delta:
        return 0
    if abs(delta) >= 120:
        return -int(delta / 120)
    return -1 if delta > 0 else 1


class PageDiffWindow(ctk.CTkToplevel):
    """Two pages side by side with independent scrolling and mouse-wheel page turning."""

    def __init__(self, parent, master_pdf, master_page, trans_pdf, trans_page,
                 focus_rect=None, focus_side="master", margins=None, title_hint=""):
        super().__init__(parent)

        self.master_pdf = master_pdf
        self.trans_pdf = trans_pdf
        self.master_page = int(master_page)
        self.trans_page = int(trans_page)
        self.focus_rect = focus_rect
        self.focus_side = focus_side if focus_side in ("master", "trans") else "master"
        self.margins = margins
        self.title_hint = title_hint

        self.master_pages = page_diff.page_count(master_pdf)
        self.trans_pages = page_diff.page_count(trans_pdf)
        self._trans_offset = self.trans_page - self.master_page

        # Viewing state
        self.fit_mode = "page"           # "page", "width", or "custom"
        self.custom_zoom = 1.0
        self.zooms = {"master": 1.0, "trans": 1.0}

        # Independent scrolling is default! (lock_scroll = False)
        self.lock_scroll = tk.BooleanVar(value=False)
        self.wheel_turns_page = tk.BooleanVar(value=True)
        self.show_diffs = tk.BooleanVar(value=False)
        self.sync_mode = tk.StringVar(value="topic")

        self._active_side = "master"
        self._syncing = False
        self._photos = {}
        self._base_images = {}
        self._pt_sizes = {}
        self._diff_result = None
        self._diff_busy = False
        self._diff_gen = 0
        self._diff_queue = queue.Queue()

        # Wheel rate-limiting tracked PER SIDE
        self._last_wheel_time = {"master": 0.0, "trans": 0.0}
        self._wheel_delta_acc = {"master": 0, "trans": 0}

        # Drag-to-pan state
        self._drag_start = None

        self._update_window_title()

        try:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        except Exception:
            sw, sh = 1366, 768
        w = max(920, min(1600, int(sw * 0.94)))
        h = max(600, min(980, int(sh * 0.90)))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(860, 520)
        self.configure(fg_color=UI_BG_CANVAS)

        self._build_ui()
        self._update_page_label()
        self._bind_events()

        # Initial render after window displays and canvas dimensions are known
        self.after(50, self._initial_load)

    def _f(self, size=11, weight="normal"):
        return ctk.CTkFont(family=theme.resolve_font_family(), size=size, weight=weight)

    def _update_window_title(self):
        m_name = os.path.basename(self.master_pdf)
        t_name = os.path.basename(self.trans_pdf)
        self.title(f"Side by Side  •  {m_name} (p.{self.master_page}) ↔ {t_name} (p.{self.trans_page})")

    def _bind_events(self):
        """Keyboard and wheel bindings for intuitive page flipping and navigation."""
        # Key bindings for document navigation
        for seq, fn in (
            ("<Prior>", lambda _e: self._on_key_step(-1)),
            ("<Next>", lambda _e: self._on_key_step(1)),
            ("<Left>", lambda _e: self._on_key_step(-1)),
            ("<Right>", lambda _e: self._on_key_step(1)),
            ("<Up>", lambda _e: self._on_key_step(-1)),
            ("<Down>", lambda _e: self._on_key_step(1)),
            ("<BackSpace>", lambda _e: self._on_key_step(-1)),
            ("<space>", lambda _e: self._on_key_step(1)),
            ("<Home>", lambda _e: self._go_to_page(1)),
            ("<End>", lambda _e: self._go_to_page(self.master_pages)),
            ("<Escape>", lambda _e: self.destroy()),
        ):
            try:
                self.bind(seq, fn)
            except Exception:
                pass

        # Window-level mouse wheel dispatch routes to side under mouse pointer
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind(seq, self._on_window_wheel)

    def _side_from_event(self, event=None, side=None):
        """Determine whether the event applies to 'master' or 'trans'."""
        if side in ("master", "trans"):
            return side
        try:
            root_x = getattr(event, "x_root", None) if event else None
            if root_x is None:
                root_x = self.winfo_pointerx()
            win_x = self.winfo_rootx()
            win_w = self.winfo_width()
            mid_x = win_x + (win_w / 2)
            return "master" if root_x < mid_x else "trans"
        except Exception:
            return getattr(self, "_active_side", "master")

    def _set_active_side(self, side):
        """Highlight active pane card border and record focus."""
        if side not in ("master", "trans"):
            return
        self._active_side = side
        for s, p in self.panes.items():
            if s == side:
                p["card"].configure(border_color=XYLEM_BLUE, border_width=2)
            else:
                p["card"].configure(border_color=UI_BORDER, border_width=1)

    def _on_key_step(self, delta):
        """Handle keyboard arrows / PageUp / PageDown."""
        if self.lock_scroll.get():
            self._go_page(delta)
        else:
            self._go_page_side(self._active_side, delta)

    # ──────────────────────────────────────────────────────────
    # UI Layout
    # ──────────────────────────────────────────────────────────
    def _build_ui(self):
        # 1. Top Header
        head = ctk.CTkFrame(self, fg_color=DEPENDABLE_BLUE, corner_radius=8)
        head.pack(fill="x", padx=12, pady=(8, 5))

        title_text = "⇔  Side by Side View"
        if self.title_hint:
            title_text += f"  •  {self.title_hint}"
        self.head_lbl = ctk.CTkLabel(
            head, text=title_text, anchor="w",
            font=self._f(12, "bold"), text_color=NEUTRAL_WHITE)
        self.head_lbl.pack(side="left", padx=14, pady=7)

        self.status_lbl = ctk.CTkLabel(
            head, text="Scroll mouse over either PDF to turn pages independently", anchor="e",
            font=self._f(10, "bold"), text_color=DYNAMIC_GREEN)
        self.status_lbl.pack(side="right", padx=14, pady=7)

        # 2. Control Toolbar
        bar = ctk.CTkFrame(self, fg_color=UI_CARD_WELL, corner_radius=8)
        bar.pack(fill="x", padx=12, pady=(0, 6))

        # Page Navigation
        self.prev_page_btn = ctk.CTkButton(
            bar, text="◀ Page", width=68, height=26,
            fg_color=DEPENDABLE_BLUE, hover_color="#003566",
            text_color=NEUTRAL_WHITE, font=self._f(10, "bold"),
            command=lambda: self._on_key_step(-1))
        self.prev_page_btn.pack(side="left", padx=(10, 4), pady=6)

        self.page_entry = ctk.CTkEntry(
            bar, width=44, height=26, font=self._f(10, "bold"),
            justify="center")
        self.page_entry.pack(side="left", padx=2, pady=6)
        self.page_entry.bind("<Return>", self._on_page_entry_submit)

        self.page_total_lbl = ctk.CTkLabel(
            bar, text=f"/ {self.master_pages or 1}", font=self._f(10, "bold"),
            text_color=DEPENDABLE_BLUE)
        self.page_total_lbl.pack(side="left", padx=(2, 4), pady=6)

        self.next_page_btn = ctk.CTkButton(
            bar, text="Page ▶", width=68, height=26,
            fg_color=DEPENDABLE_BLUE, hover_color="#003566",
            text_color=NEUTRAL_WHITE, font=self._f(10, "bold"),
            command=lambda: self._on_key_step(1))
        self.next_page_btn.pack(side="left", padx=(4, 14), pady=6)

        # Wheel flips pages toggle
        self.wheel_chk = ctk.CTkCheckBox(
            bar, text="Wheel flips pages", variable=self.wheel_turns_page,
            font=self._f(10, "bold"), text_color=DEPENDABLE_BLUE,
            checkbox_width=16, checkbox_height=16,
            fg_color=XYLEM_BLUE, hover_color=UI_HOVER_BLUE)
        self.wheel_chk.pack(side="left", padx=6, pady=6)

        # Sync scrolling toggle (Unchecked by default for independent scrolling!)
        self.lock_chk = ctk.CTkCheckBox(
            bar, text="Sync scrolling", variable=self.lock_scroll,
            command=self._on_toggle_sync,
            font=self._f(10, "bold"), text_color=DEPENDABLE_BLUE,
            checkbox_width=16, checkbox_height=16,
            fg_color=XYLEM_BLUE, hover_color=UI_HOVER_BLUE)
        self.lock_chk.pack(side="left", padx=8, pady=6)

        # Zoom & Fit Controls on Right
        ctk.CTkButton(
            bar, text="Zoom +", width=58, height=26, fg_color=UI_CARD_BG,
            text_color=DEPENDABLE_BLUE, border_width=1, border_color=UI_BORDER,
            font=self._f(10), command=lambda: self._zoom(0.15)
        ).pack(side="right", padx=(2, 10), pady=6)

        self.zoom_lbl = ctk.CTkLabel(
            bar, text="Fit", font=self._f(10, "bold"), width=44,
            text_color=DEPENDABLE_BLUE)
        self.zoom_lbl.pack(side="right", padx=2, pady=6)

        ctk.CTkButton(
            bar, text="Zoom −", width=58, height=26, fg_color=UI_CARD_BG,
            text_color=DEPENDABLE_BLUE, border_width=1, border_color=UI_BORDER,
            font=self._f(10), command=lambda: self._zoom(-0.15)
        ).pack(side="right", padx=2, pady=6)

        ctk.CTkButton(
            bar, text="Fit Width", width=68, height=26, fg_color=UI_CARD_BG,
            text_color=DEPENDABLE_BLUE, border_width=1, border_color=UI_BORDER,
            font=self._f(10), command=self._set_fit_width
        ).pack(side="right", padx=3, pady=6)

        self.fit_page_btn = ctk.CTkButton(
            bar, text="Fit Page", width=68, height=26, fg_color=XYLEM_BLUE,
            text_color=NEUTRAL_WHITE, font=self._f(10, "bold"),
            command=self._set_fit_page)
        self.fit_page_btn.pack(side="right", padx=3, pady=6)

        # Optional Diff Highlights toggle
        self.diff_chk = ctk.CTkCheckBox(
            bar, text="Highlight differences", variable=self.show_diffs,
            command=self._on_toggle_diffs,
            font=self._f(10), text_color=NEUTRAL_DARK_GR,
            checkbox_width=16, checkbox_height=16,
            fg_color=RADIANT_ORANGE, hover_color="#D97706")
        self.diff_chk.pack(side="right", padx=(14, 10), pady=6)

        # 3. Two-Pane Split View
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        body.grid_columnconfigure(0, weight=500)
        body.grid_columnconfigure(1, weight=0)
        body.grid_columnconfigure(2, weight=500)
        body.grid_rowconfigure(0, weight=1)

        # Draggable vertical sash between Master and Translated panes
        sash = tk.Frame(body, width=8, bg=UI_CARD_WELL, cursor="sb_h_double_arrow")
        sash.grid(row=0, column=1, sticky="ns", padx=2)
        sash_line = tk.Frame(sash, width=2, bg=UI_BORDER)
        sash_line.pack(expand=True, fill="y", pady=40)

        def _on_sash_enter(e):
            sash.configure(bg=UI_HOVER_BLUE)
            sash_line.configure(bg=XYLEM_BLUE)

        def _on_sash_leave(e):
            sash.configure(bg=UI_CARD_WELL)
            sash_line.configure(bg=UI_BORDER)

        def _on_sash_drag(e):
            try:
                bw = body.winfo_width()
                if bw > 100:
                    rx = e.x_root - body.winfo_rootx()
                    ratio = max(0.15, min(0.85, rx / float(bw)))
                    w0 = int(ratio * 1000)
                    w2 = int((1.0 - ratio) * 1000)
                    body.grid_columnconfigure(0, weight=w0)
                    body.grid_columnconfigure(2, weight=w2)
            except Exception:
                pass

        sash.bind("<Enter>", _on_sash_enter)
        sash.bind("<Leave>", _on_sash_leave)
        sash.bind("<B1-Motion>", _on_sash_drag)

        self.panes = {}
        for col_idx, (side, caption, colour) in ((0, ("master", "MASTER (English)", XYLEM_BLUE)),
                                                 (2, ("trans", "TRANSLATED", DEPENDABLE_BLUE))):
            card = ctk.CTkFrame(body, fg_color=UI_CARD_BG, corner_radius=8,
                                border_width=1, border_color=UI_BORDER)
            card.grid(row=0, column=col_idx, sticky="nsew",
                      padx=((0, 2) if side == "master" else (2, 0)))

            # Pane Header
            cap = ctk.CTkFrame(card, fg_color="transparent")
            cap.pack(fill="x", padx=10, pady=(6, 2))
            ctk.CTkLabel(cap, text=caption, font=self._f(10, "bold"),
                         text_color=colour).pack(side="left")

            # Dedicated independent page buttons on each pane
            nav = ctk.CTkFrame(cap, fg_color="transparent")
            nav.pack(side="left", padx=(10, 0))
            prev_b = ctk.CTkButton(nav, text="◀", width=24, height=20,
                                   fg_color=UI_CARD_WELL, text_color=DEPENDABLE_BLUE,
                                   font=self._f(9, "bold"),
                                   command=lambda sd=side: self._go_page_side(sd, -1))
            prev_b.pack(side="left", padx=1)

            side_lbl = ctk.CTkLabel(nav, text="", font=self._f(9, "bold"),
                                    text_color=DEPENDABLE_BLUE, width=54)
            side_lbl.pack(side="left", padx=2)

            next_b = ctk.CTkButton(nav, text="▶", width=24, height=20,
                                   fg_color=UI_CARD_WELL, text_color=DEPENDABLE_BLUE,
                                   font=self._f(9, "bold"),
                                   command=lambda sd=side: self._go_page_side(sd, 1))
            next_b.pack(side="left", padx=1)

            name_lbl = ctk.CTkLabel(cap, text="", font=self._f(9),
                                    text_color=NEUTRAL_DARK_GR)
            name_lbl.pack(side="right")

            # Pane Canvas
            holder = tk.Frame(card, bg=UI_CARD_BG)
            holder.pack(fill="both", expand=True, padx=6, pady=(0, 6))

            cv = tk.Canvas(holder, bg="#2D2D2D", highlightthickness=0,
                           yscrollincrement=1, xscrollincrement=1)
            hsb = tk.Scrollbar(holder, orient="horizontal",
                               command=lambda *a, sd=side: self._xview(sd, *a))
            vsb = tk.Scrollbar(holder, orient="vertical",
                               command=lambda *a, sd=side: self._yview(sd, *a))
            cv.configure(xscrollcommand=lambda f, l, sd=side: self._on_pane_xscroll(sd, f, l, hsb),
                         yscrollcommand=lambda f, l, sd=side: self._on_pane_yscroll(sd, f, l, vsb))

            hsb.pack(side="bottom", fill="x")
            vsb.pack(side="right", fill="y")
            cv.pack(side="left", fill="both", expand=True)

            # Bind Canvas Events directly to this pane's side
            cv.bind("<Enter>", lambda _e, sd=side: self._set_active_side(sd))
            cv.bind("<MouseWheel>", lambda e, sd=side: self._on_wheel(e, sd))
            cv.bind("<Button-4>", lambda e, sd=side: self._on_wheel(e, sd))
            cv.bind("<Button-5>", lambda e, sd=side: self._on_wheel(e, sd))

            cv.bind("<Control-MouseWheel>", lambda e, sd=side: self._on_zoom_wheel(e, sd))
            cv.bind("<Control-Button-4>", lambda e, sd=side: self._on_zoom_wheel(e, sd))
            cv.bind("<Control-Button-5>", lambda e, sd=side: self._on_zoom_wheel(e, sd))

            # Pan with click-and-drag
            cv.bind("<ButtonPress-1>", lambda e, sd=side: self._on_drag_start(e, sd))
            cv.bind("<B1-Motion>", lambda e, sd=side: self._on_drag_move(e, sd))
            cv.bind("<ButtonRelease-1>", lambda e: self._on_drag_end(e))

            # Double-click to toggle Fit Page / 100%
            cv.bind("<Double-Button-1>", lambda _e: self._toggle_fit_zoom())

            self.panes[side] = {
                "card": card, "canvas": cv, "name": name_lbl, "page_lbl": side_lbl,
                "prev": prev_b, "next": next_b, "vsb": vsb, "hsb": hsb
            }

        # 4. Bottom Information Strip
        foot = ctk.CTkFrame(self, fg_color="transparent")
        foot.pack(fill="x", padx=16, pady=(0, 6))

        self.foot_info = ctk.CTkLabel(
            foot, text="", font=self._f(9), text_color=NEUTRAL_DARK_GR,
            anchor="w", justify="left")
        self.foot_info.pack(side="left")

        self.hint_lbl = ctk.CTkLabel(
            foot, text="Tip: Scroll mouse over left or right PDF to flip pages independently  •  Click & drag to pan",
            font=self._f(9), text_color=NEUTRAL_DARK_GR, anchor="e")
        self.hint_lbl.pack(side="right")

        self.bind("<Configure>", self._on_window_resize)
        self._resize_timer = None

    # ──────────────────────────────────────────────────────────
    # Loading and Rendering
    # ──────────────────────────────────────────────────────────
    def _initial_load(self):
        """Initial render once widget dimensions are established."""
        self._load_and_render_side("master")
        self._load_and_render_side("trans")
        self._set_active_side("master")

    def _load_and_render_side(self, side):
        """Load and display a single side independently."""
        pdf_path = self.master_pdf if side == "master" else self.trans_pdf
        page_no = self.master_page if side == "master" else self.trans_page

        try:
            img, pt = _get_rendered_page(pdf_path, page_no)
            self._base_images[side] = img
            self._pt_sizes[side] = pt
        except Exception as e:
            self.status_lbl.configure(text=f"Error rendering {side}: {e}", text_color=theme.TEXT_ATTENTION)
            return

        # Update pane header
        self.panes[side]["name"].configure(
            text=f"{os.path.basename(pdf_path)}  ·  p.{page_no}"
                 f"{self._topic_suffix(pdf_path, page_no)}")

        self._render_side(side)
        self._update_footer_info()

        # Prefetch adjacent pages for this side in background thread
        threading.Thread(target=self._prefetch_side, args=(side, page_no), daemon=True).start()

    def _load_and_render_pages(self):
        """Load and display both sides."""
        self._load_and_render_side("master")
        self._load_and_render_side("trans")
        if self.show_diffs.get():
            self._recompare()

    def _prefetch_side(self, side, current_page):
        """Pre-cache previous and next pages for a specific document."""
        pdf_path = self.master_pdf if side == "master" else self.trans_pdf
        total_pages = self.master_pages if side == "master" else self.trans_pages
        for p in (current_page + 1, current_page - 1):
            if 1 <= p <= total_pages:
                try:
                    _get_rendered_page(pdf_path, p)
                except Exception:
                    pass

    def _render_side(self, side):
        """Scale and position the page for one side onto its canvas."""
        base_img = self._base_images.get(side)
        if not base_img:
            return

        cv = self.panes[side]["canvas"]
        cw = max(100, cv.winfo_width())
        ch = max(100, cv.winfo_height())

        if self.fit_mode == "page":
            scale = min((cw - 16) / max(1, base_img.width), (ch - 16) / max(1, base_img.height))
            scale = max(0.15, min(scale, 2.0))
            self.zooms[side] = scale
        elif self.fit_mode == "width":
            scale = (cw - 20) / max(1, base_img.width)
            scale = max(0.2, min(scale, 3.0))
            self.zooms[side] = scale
        else:
            scale = max(0.2, min(3.0, self.custom_zoom))
            self.zooms[side] = scale

        cv.delete("all")
        nw = max(1, int(base_img.width * scale))
        nh = max(1, int(base_img.height * scale))

        if scale != 1.0:
            scaled_img = base_img.resize((nw, nh), _RESAMPLE)
        else:
            scaled_img = base_img

        photo = ImageTk.PhotoImage(scaled_img, master=cv)
        self._photos[side] = photo

        pos_x = max(0, (cw - nw) // 2)
        pos_y = max(0, (ch - nh) // 2)

        cv.create_image(pos_x, pos_y, anchor="nw", image=photo, tags="page_img")
        cv.config(scrollregion=(0, 0, max(cw, nw + pos_x * 2), max(ch, nh + pos_y * 2)))
        self._draw_overlays_side(side)

    def _render_view(self):
        """Re-render both sides."""
        self._render_side("master")
        self._render_side("trans")
        avg_zoom = int(self.zooms.get("master", 1.0) * 100)
        self.zoom_lbl.configure(text=f"{avg_zoom}%")
        if self.fit_mode == "page":
            self.fit_page_btn.configure(fg_color=XYLEM_BLUE, text_color=NEUTRAL_WHITE)
        else:
            self.fit_page_btn.configure(fg_color=UI_CARD_BG, text_color=DEPENDABLE_BLUE)

    def _draw_overlays_side(self, side):
        """Draw focus region and optional difference boxes for one side."""
        cv = self.panes[side]["canvas"]
        cv.delete("mark")

        if self.focus_rect:
            other = "trans" if self.focus_side == "master" else "master"
            if side == self.focus_side:
                self._box(self.focus_side, self.focus_rect, FOCUS_COLOR, width=2, label="reviewed region")
            elif side == other and self.focus_side == "master":
                self._box(other, self.focus_rect, FOCUS_COLOR, width=2, dash=(5, 3))

        if self.show_diffs.get() and self._diff_result:
            for d in self._diff_result.get("diffs", []):
                colour = RADIANT_ORANGE
                label = page_diff.KIND_LABELS.get(d["kind"], "")
                if side == "master" and d["rect_master"]:
                    self._box("master", d["rect_master"], colour, width=2, label=label)
                if side == "trans" and d["rect_trans"]:
                    self._box("trans", d["rect_trans"], colour, width=2, label=label)

    def _pt_to_px(self, pt_val, side):
        """Convert points to canvas pixels based on DPI and zoom."""
        base_img = self._base_images.get(side)
        pt_size = self._pt_sizes.get(side)
        if not base_img or not pt_size or not pt_size[0]:
            return pt_val
        px_per_pt = (base_img.width / pt_size[0]) * self.zooms[side]
        return pt_val * px_per_pt

    def _box(self, side, rect_pt, colour, width=2, dash=(), label=""):
        """Draw an annotated bounding box on a pane canvas."""
        cv = self.panes[side]["canvas"]
        cw = cv.winfo_width()
        base_img = self._base_images.get(side)
        if not base_img:
            return
        nw = int(base_img.width * self.zooms[side])
        offset_x = max(0, (cw - nw) // 2)
        offset_y = max(0, (cv.winfo_height() - int(base_img.height * self.zooms[side])) // 2)

        x0 = self._pt_to_px(rect_pt[0], side) + offset_x
        y0 = self._pt_to_px(rect_pt[1], side) + offset_y
        x1 = self._pt_to_px(rect_pt[2], side) + offset_x
        y1 = self._pt_to_px(rect_pt[3], side) + offset_y

        cv.create_rectangle(x0, y0, x1, y1, outline=colour, width=width, dash=dash, tags="mark")
        if label:
            ty = y0 - 9 if y0 > 14 else y1 + 9
            txt = cv.create_text(x0 + 2, ty, anchor="w", text=f" {label} ",
                                 font=self._f(8, "bold"), fill=colour, tags="mark")
            try:
                bx0, by0, bx1, by1 = cv.bbox(txt)
                chip = cv.create_rectangle(bx0, by0 - 1, bx1, by1 + 1,
                                           fill=NEUTRAL_WHITE, outline="", tags="mark")
                cv.tag_lower(chip, txt)
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────
    # Independent Page Navigation & Stepping
    # ──────────────────────────────────────────────────────────
    def _go_page_side(self, side, delta):
        """
        Step ONE pane independently.
        Scrolling over Master changes only Master;
        scrolling over Translated changes only Translated!
        """
        pages = self.master_pages if side == "master" else self.trans_pages
        current = self.master_page if side == "master" else self.trans_page
        if not pages:
            return
        target = current + delta
        if not (1 <= target <= pages):
            return

        self._set_active_side(side)

        if side == "master":
            self.master_page = target
        else:
            self.trans_page = target

        self._trans_offset = self.trans_page - self.master_page
        self.focus_rect = None
        self._diff_result = None

        self._update_window_title()
        self._update_page_label()
        self._load_and_render_side(side)

    def _go_page(self, delta):
        """Step both panes simultaneously (used when Sync scrolling is active)."""
        if not self.master_pages:
            return
        target_m = self.master_page + delta
        if not (1 <= target_m <= self.master_pages):
            return

        self.master_page = target_m

        if self.sync_mode.get() == "topic":
            try:
                from core import toc as TOC
                mapped, _code, _why = TOC.matching_page(
                    self.master_pdf, self.trans_pdf, target_m)
            except Exception:
                mapped = target_m + self._trans_offset
        else:
            mapped = target_m + self._trans_offset

        if self.trans_pages:
            mapped = max(1, min(self.trans_pages, mapped))
        self.trans_page = mapped
        self._trans_offset = self.trans_page - self.master_page

        self.focus_rect = None
        self._diff_result = None

        self._update_window_title()
        self._update_page_label()
        self._load_and_render_pages()

    def _go_to_page(self, page_no):
        """Jump directly to a specific page on active side."""
        side = self._active_side
        pages = self.master_pages if side == "master" else self.trans_pages
        target = max(1, min(pages, int(page_no)))
        current = self.master_page if side == "master" else self.trans_page
        delta = target - current
        if delta != 0:
            if self.lock_scroll.get():
                self._go_page(delta)
            else:
                self._go_page_side(side, delta)

    def _on_page_entry_submit(self, _event=None):
        """Jump to page typed into the entry box."""
        try:
            val = int(self.page_entry.get().strip())
            self._go_to_page(val)
        except ValueError:
            self._update_page_label()

    def _update_page_label(self):
        """Refresh page numbers and button states."""
        active_page = self.master_page if self._active_side == "master" else self.trans_page
        active_total = self.master_pages if self._active_side == "master" else self.trans_pages

        self.page_entry.delete(0, "end")
        self.page_entry.insert(0, str(active_page))
        self.page_total_lbl.configure(text=f"/ {active_total or 1}")

        first = active_page <= 1
        last = active_page >= active_total
        try:
            self.prev_page_btn.configure(state="disabled" if first else "normal")
            self.next_page_btn.configure(state="disabled" if last else "normal")
        except Exception:
            pass

        for side, page, total in (("master", self.master_page, self.master_pages),
                                  ("trans", self.trans_page, self.trans_pages)):
            pane = self.panes.get(side)
            if not pane:
                continue
            try:
                pane["page_lbl"].configure(text=f"{page} / {total}" if total else str(page))
                pane["prev"].configure(state="disabled" if page <= 1 else "normal")
                pane["next"].configure(state="disabled" if total and page >= total else "normal")
            except Exception:
                pass

    def _update_footer_info(self):
        """Display topic mapping or alignment notes in bottom strip."""
        notes = []
        try:
            from core import toc as TOC
            m_code = TOC.topic_at_page(self.master_pdf, self.master_page)
            t_code = TOC.topic_at_page(self.trans_pdf, self.trans_page)
            if m_code and t_code:
                if m_code == t_code:
                    notes.append(f"Section matched: {m_code}")
                else:
                    notes.append(f"Master: {m_code}  |  Trans: {t_code}")
        except Exception:
            pass

        if self._trans_offset != 0:
            notes.append(f"Offset: {self._trans_offset:+d}")

        self.foot_info.configure(text="   ·   ".join(notes) if notes else "Pages aligned")

    @staticmethod
    def _topic_suffix(pdf_path, page_no):
        """Return '  ·  Topic 4.2' if page belongs to an outline topic."""
        try:
            from core import toc as TOC
            code = TOC.topic_at_page(pdf_path, page_no)
            return f"  ·  Topic {code}" if code else ""
        except Exception:
            return ""

    # ──────────────────────────────────────────────────────────
    # Mouse Wheel & Panning (Independent per PDF)
    # ──────────────────────────────────────────────────────────
    def _on_window_wheel(self, event):
        """Route mouse wheel from any point in the window to the side under cursor."""
        side = self._side_from_event(event)
        self._on_wheel(event, side)

    def _on_wheel(self, event, side=None):
        """
        Independent mouse wheel handler.
        Scrolling over Master changes only Master.
        Scrolling over Translated changes only Translated!
        """
        side = self._side_from_event(event, side)
        self._set_active_side(side)

        # Ctrl held -> zoom
        state = getattr(event, "state", 0)
        if state & 0x0004:
            return self._on_zoom_wheel(event, side)

        step = _wheel_step(event)
        if not step:
            return "break"

        cv = self.panes[side]["canvas"]

        # If user disabled wheel page flipping, scroll strictly within the page
        if not self.wheel_turns_page.get():
            if self.lock_scroll.get():
                self._yview_both("scroll", step * PAGE_WHEEL_STEP, "units")
            else:
                cv.yview("scroll", step * PAGE_WHEEL_STEP, "units")
            return "break"

        # Check vertical visibility of the canvas for the active side
        try:
            yview = cv.yview()
        except Exception:
            yview = (0.0, 1.0)

        at_top = (yview[0] <= 0.002)
        at_bottom = (yview[1] >= 0.998)

        # 1. If scrolling down and NOT at the end of the current page, scroll within page
        if step > 0 and not at_bottom:
            if self.lock_scroll.get():
                self._yview_both("scroll", step * PAGE_WHEEL_STEP, "units")
            else:
                cv.yview("scroll", step * PAGE_WHEEL_STEP, "units")
            return "break"

        # 2. If scrolling up and NOT at the top of the current page, scroll within page
        if step < 0 and not at_top:
            if self.lock_scroll.get():
                self._yview_both("scroll", step * PAGE_WHEEL_STEP, "units")
            else:
                cv.yview("scroll", step * PAGE_WHEEL_STEP, "units")
            return "break"

        # 3. Only when at the end of the current page (down) or top of the current page (up):
        # Move to next / previous page, debounced to avoid multi-page skipping
        now = time.time()
        cooldown = self._last_wheel_time.get(side, 0.0)
        if now - cooldown > WHEEL_COOLDOWN_SEC:
            self._last_wheel_time[side] = now
            self._wheel_delta_acc[side] = 0
            if self.lock_scroll.get():
                self._go_page(step)
            else:
                self._go_page_side(side, step)

            # When stepping forward, show top of new page; when stepping back, show bottom
            target_pos = 0.0 if step > 0 else 1.0
            def _set_pos():
                try:
                    if self.lock_scroll.get():
                        self._yview_both("moveto", target_pos)
                    else:
                        cv.yview_moveto(target_pos)
                except Exception:
                    pass
            self.after(50, _set_pos)

        return "break"

    def _on_zoom_wheel(self, event, side):
        """Ctrl + Wheel zooms in/out."""
        step = _wheel_step(event)
        if step:
            self._zoom(-step * 0.15)
        return "break"

    def _on_drag_start(self, event, side):
        """Begin click-and-drag panning on clicked side with native anchor."""
        self._set_active_side(side)
        self._drag_start = (side, event.x, event.y)
        self._drag_last = (event.x, event.y)

        # Set native canvas scan anchor on active side (or both if sync locked)
        panes = list(self.panes.values()) if self.lock_scroll.get() else [self.panes[side]]
        for p in panes:
            try:
                p["canvas"].scan_mark(event.x, event.y)
                p["canvas"].configure(cursor="fleur")
            except Exception:
                pass

    def _on_drag_move(self, event, side):
        """Pan canvas viewport smoothly in 2D with native 1:1 scan_dragto."""
        if not self._drag_start:
            return

        self._drag_last = (event.x, event.y)
        start_side, start_x, start_y = self._drag_start
        total_dx = event.x - start_x
        total_dy = event.y - start_y

        cv = self.panes[side]["canvas"]
        try:
            xview = cv.xview()
            yview = cv.yview()
        except Exception:
            xview, yview = (0.0, 1.0), (0.0, 1.0)

        # Native 1:1 pixel panning across all directions
        panes = list(self.panes.values()) if self.lock_scroll.get() else [self.panes[side]]
        for p in panes:
            try:
                p["canvas"].scan_dragto(event.x, event.y, gain=1)
            except Exception:
                pass

        # Visual feedback for swipe-to-turn ONLY when the page fits completely without scrollbars
        can_scroll = (xview != (0.0, 1.0)) or (yview != (0.0, 1.0))
        if not can_scroll and self.fit_mode == "page":
            if abs(total_dx) >= 80 and abs(total_dx) > abs(total_dy) * 1.5:
                if total_dx > 0:
                    self.status_lbl.configure(
                        text="Release mouse to go to Previous Page ◀", text_color=XYLEM_BLUE)
                else:
                    self.status_lbl.configure(
                        text="Release mouse to go to Next Page ▶", text_color=XYLEM_BLUE)
            else:
                self._on_toggle_sync()

    def _on_drag_end(self, event=None):
        if self._drag_start:
            side, start_x, start_y = self._drag_start
            last_x, last_y = getattr(self, "_drag_last", (start_x, start_y))
            if event:
                last_x, last_y = event.x, event.y

            # Reset cursor on all panes
            for p in self.panes.values():
                try:
                    p["canvas"].configure(cursor="")
                except Exception:
                    pass

            cv = self.panes[side]["canvas"]
            try:
                xview = cv.xview()
                yview = cv.yview()
            except Exception:
                xview, yview = (0.0, 1.0), (0.0, 1.0)

            # ONLY allow swipe page turn when the entire page fits without scrollbars
            # When ZOOMED IN (can_scroll is True): dragging is exclusively for PANNING!
            can_scroll = (xview != (0.0, 1.0)) or (yview != (0.0, 1.0))
            if not can_scroll and self.fit_mode == "page":
                total_dx = last_x - start_x
                total_dy = last_y - start_y
                if abs(total_dx) >= 80 and abs(total_dx) > abs(total_dy) * 1.5:
                    if total_dx > 0:
                        if self.lock_scroll.get():
                            self._go_page(-1)
                        else:
                            self._go_page_side(side, -1)
                    elif total_dx < 0:
                        if self.lock_scroll.get():
                            self._go_page(1)
                        else:
                            self._go_page_side(side, 1)

        self._drag_start = None
        self._drag_last = None
        self._on_toggle_sync()

    def _on_toggle_sync(self):
        """Update status label when sync mode changes."""
        if self.lock_scroll.get():
            self.status_lbl.configure(
                text="Scrolling and paging are linked (synced)", text_color=XYLEM_BLUE)
        else:
            self.status_lbl.configure(
                text="Scroll mouse over either PDF to turn pages independently", text_color=DYNAMIC_GREEN)

    # ──────────────────────────────────────────────────────────
    # Zoom and Fit Modes
    # ──────────────────────────────────────────────────────────
    def _set_fit_page(self):
        self.fit_mode = "page"
        self._render_view()

    def _set_fit_width(self):
        self.fit_mode = "width"
        self._render_view()

    def _zoom(self, delta):
        if self.fit_mode in ("page", "width"):
            current = self.zooms.get("master", 1.0)
        else:
            current = self.custom_zoom
        self.custom_zoom = max(0.2, min(3.0, current + delta))
        self.fit_mode = "custom"
        self._render_view()

    def _toggle_fit_zoom(self):
        """Double-click toggles between Fit Page and 100%."""
        if self.fit_mode == "page":
            self.custom_zoom = 1.0
            self.fit_mode = "custom"
        else:
            self.fit_mode = "page"
        self._render_view()

    def _on_window_resize(self, event):
        """Recalculate layout on window resize when in Fit Page/Width mode."""
        if event.widget == self and self.fit_mode in ("page", "width"):
            if self._resize_timer:
                self.after_cancel(self._resize_timer)
            self._resize_timer = self.after(120, self._render_view)

    # ──────────────────────────────────────────────────────────
    # Scroll Synchronization (when lock_scroll is True)
    # ──────────────────────────────────────────────────────────
    def _xview(self, side, *args):
        self.panes[side]["canvas"].xview(*args)
        if self.lock_scroll.get():
            for other, pane in self.panes.items():
                if other != side:
                    pane["canvas"].xview(*args)

    def _yview_both(self, *args):
        for p in self.panes.values():
            p["canvas"].yview(*args)

    def _yview(self, side, *args):
        self.panes[side]["canvas"].yview(*args)
        if self.lock_scroll.get():
            for other, pane in self.panes.items():
                if other != side:
                    pane["canvas"].yview(*args)

    def _on_pane_yscroll(self, side, first, last, vsb):
        try:
            vsb.set(first, last)
        except Exception:
            return
        if not self.lock_scroll.get() or self._syncing:
            return
        self._syncing = True
        try:
            for other, pane in self.panes.items():
                if other != side:
                    pane["canvas"].yview_moveto(first)
        finally:
            self._syncing = False

    def _on_pane_xscroll(self, side, first, last, hsb):
        try:
            hsb.set(first, last)
        except Exception:
            return
        if not self.lock_scroll.get() or self._syncing:
            return
        self._syncing = True
        try:
            for other, pane in self.panes.items():
                if other != side:
                    pane["canvas"].xview_moveto(first)
        finally:
            self._syncing = False

    # ──────────────────────────────────────────────────────────
    # Optional Algorithmic Differences
    # ──────────────────────────────────────────────────────────
    def _on_toggle_diffs(self):
        if self.show_diffs.get():
            self._recompare()
        else:
            self._diff_result = None
            self.status_lbl.configure(
                text="Scroll mouse over either PDF to turn pages independently", text_color=DYNAMIC_GREEN)
            self._draw_overlays_side("master")
            self._draw_overlays_side("trans")

    def _recompare(self):
        """Run difference comparison only when explicitly enabled."""
        self._diff_gen += 1
        gen = self._diff_gen
        self._diff_busy = True
        self.status_lbl.configure(text="Detecting differences…", text_color=RADIANT_ORANGE)

        def work():
            try:
                res = page_diff.compare_pages(
                    self.master_pdf, self.master_page,
                    self.trans_pdf, self.trans_page,
                    ignore_text=True, margins=self.margins)
                self._diff_queue.put((gen, res, None))
            except Exception as e:
                self._diff_queue.put((gen, None, e))

        threading.Thread(target=work, daemon=True).start()
        self.after(80, self._poll_diff)

    def _poll_diff(self):
        try:
            gen, res, err = self._diff_queue.get_nowait()
        except queue.Empty:
            if self._diff_busy:
                self.after(80, self._poll_diff)
            return
        if gen != self._diff_gen:
            self.after(80, self._poll_diff)
            return

        self._diff_busy = False
        if err is not None or res is None:
            self.status_lbl.configure(text="Could not compare differences", text_color=theme.TEXT_ATTENTION)
            return

        self._diff_result = res
        n = len(res.get("diffs", []))
        self.status_lbl.configure(
            text=f"{n} difference(s) detected" if n else "No visual differences detected",
            text_color=DYNAMIC_GREEN if n == 0 else RADIANT_ORANGE)
        self._draw_overlays_side("master")
        self._draw_overlays_side("trans")


def open_page_diff(parent, master_pdf, master_page, trans_pdf, trans_page,
                   focus_rect=None, focus_side="master", margins=None, title_hint=""):
    """
    Open the Side by Side window, or return None with an error reason.
    Maintains compatibility with callers of open_page_diff.
    """
    for path, what in ((master_pdf, "master"), (trans_pdf, "translated")):
        if not path or not os.path.isfile(path):
            return None, f"The {what} PDF could not be found:\n{path or '(not set)'}"
    try:
        win = PageDiffWindow(parent, master_pdf, master_page, trans_pdf, trans_page,
                             focus_rect=focus_rect, focus_side=focus_side, margins=margins,
                             title_hint=title_hint)
        win.after(120, win.lift)
        return win, None
    except Exception as e:
        return None, str(e)


# Convenient alias
open_side_by_side = open_page_diff
SideBySideWindow = PageDiffWindow