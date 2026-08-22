"""
gui/region_dialog.py

CustomTkinter Multi-Region ROI Selector window.

This is the presentation layer only — every measurement, similarity score and
comparison image is produced by core.region_engine, which has no Tkinter
dependency and can be driven from a script or a test.

Features:
  1. Sub-Region nesting: selecting a region inside an existing region automatically
     creates a Sub-Region (e.g. Region 1.1).
  2. Exact Match Required toggle per region: require 100% exact text/code match.
  3. "Don't Compare Text (Visual Match)": verifies presence of content and matches
     non-text visual graphics (circle badges, icons, lines, shapes, frames).
  4. Saves only side-by-side compared images, into the output folder chosen on the
     main window (no redundant individual crops).
  5. Automatic whitespace trimming so surrounding margins do not skew comparisons.
  6. Consecutive numbering & automatic re-indexing on add/delete.
  7. Full Maximize, Minimize and Resizing support.
"""

import os
import threading

import pymupdf as fitz  # PyMuPDF (aliased as fitz for API compat)
from PIL import Image, ImageTk
import customtkinter as ctk
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from gui import theme
from gui.theme import (
    XYLEM_BLUE,
    DEPENDABLE_BLUE,
    CLARITY_BLUE,
    DYNAMIC_GREEN,
    RADIANT_ORANGE,
    UI_BG_CANVAS,
    UI_CARD_BG,
    UI_CARD_WELL,
    UI_BORDER,
    NEUTRAL_WHITE,
    NEUTRAL_DARK_GR,
    REGION_COLORS,
)

from core import margins as page_margins
from core import templates as templates_store
from core.templates import (
    SCOPE_LABELS, SCOPE_PAGES, SCOPE_RANGE, SCOPE_SINGLE, SCOPE_TYPES,
    default_scope, describe_scope, parse_pages_expression,
)
from core.region_engine import (
    DEFAULT_CROPS_OUTPUT_DIR,
    render_region_image,
    get_page_count,
    render_pdf_page_image,
    extract_roi_text,
    is_rect_contained_in_parent,
    run_batch_multiple_regions_check,
)

# Xylem-branded ttk table style, registered lazily on first widget creation.
_TREE_STYLE = "Xylem.Treeview"

# Inspector defaults
DEFAULT_Y_TOLERANCE = 15
DEFAULT_X_TOLERANCE = 15
DEFAULT_PASS_THRESHOLD = 75

# The tab scrolls, and its two columns are never shorter than this. Chosen to
# be taller than a laptop window on purpose: the space below the fold is what
# gives the results table enough rows to be worth reading.
MIN_CONTENT_HEIGHT = 1100

# One wheel notch moves the page viewer this far, in canvas pixels.
PAGE_WHEEL_STEP = 60

# Margin editor
MARGIN_GRAB_PX = 6                 # how close the pointer must be to grab a guide
MARGIN_BAND_COLOR = "#D0021B"      # the ignored area, shaded
MARGIN_GUIDE_COLOR = "#F5A623"     # the draggable boundary line
MARGIN_KEPT_COLOR = "#1E9E5A"      # a graphic the current margins keep
MARGIN_SIDE_ORDER = ("header", "footer", "left", "right")
MARGIN_FIELD_LABELS = {"header": "Top", "footer": "Bottom",
                       "left": "Left", "right": "Right"}
# Which page edge each guide runs along, for the drag maths.
MARGIN_AXIS = {"header": "y", "footer": "y", "left": "x", "right": "x"}


def _font(size=12, weight="normal", family=None, **kwargs):
    """CustomTkinter font in the resolved Xylem typeface (Roboto, else Arial)."""
    return ctk.CTkFont(
        family=family or theme.resolve_font_family(),
        size=size,
        weight=weight,
        **kwargs,
    )


# ==============================================================================
# CUSTOMTKINTER MULTI-REGION SELECTOR & INSPECTOR DIALOG
# ==============================================================================

class RegionInspectorFrame(ctk.CTkFrame):
    """
    Embeddable Multi-Region ROI Selector.

    A plain CTkFrame rather than a window, so it lives as a tab inside the main
    application window. RegionInspectorDialog below wraps it in a Toplevel for
    standalone use (`python -m gui.region_dialog`).

    Construct it with no paths and call load() later — that is how the main
    window uses it, since the user picks the PDFs on the Inspection tab.
    """
    def __init__(self, parent, eng_pdf_path: str = "", tr_target_path: str = "",
                 output_dir: str = None, on_results=None, on_templates_changed=None,
                 on_margins_changed=None):
        super().__init__(parent, fg_color=UI_BG_CANVAS)

        # Optional callback so the host window can publish results elsewhere
        # (the Comparisons gallery tab).
        self._on_results = on_results
        # Lets the host refresh its own template dropdown when one is saved/deleted.
        self._on_templates_changed = on_templates_changed
        # Lets the host remember margins the user tuned without saving a template.
        self._on_margins_changed = on_margins_changed
        self._margin_save_job = None

        self._apply_paths(eng_pdf_path, tr_target_path, output_dir)

        self.current_page = 1  # Open on page 1; navigate or type a page to move
        self.zoom = 1.25
        self.page_width_pt = 420.0
        self.page_height_pt = 595.0

        # Dynamic Region list:
        # [{ "id": int, "label": str, "page_num": int, "is_last_page": bool, "roi_rect": tuple, "eng_text": str,
        #    "parent_id": int|None, "exact_match": bool, "dont_compare_text": bool, "color": str, "is_custom_label": bool }]
        self.regions = []
        self.active_region_id = None

        self.selection_start = None
        self.temp_rect_id = None
        self.tk_image = None
        self.pil_image = None

        self.is_checking = False
        self.check_results = []
        self.preview_tk_img = None
        self._updating_selection = False

        # Ignored page margins. Part of the stylesheet, edited on the canvas,
        # saved into the template - see core/margins.py for what they mean.
        self.margins = dict(page_margins.DEFAULT_MARGINS)
        self._drag_guide = None            # which guide the pointer is holding
        self._margin_scan = {}             # (pdf, page) -> element list, cached
        self._suspend_margin_sync = False  # stops the spinboxes echoing a drag

        theme.apply_treeview_style(_TREE_STYLE)

        self._build_ui()
        self.refresh_template_list()
        self._load_and_render_page()

    # ──────────────────────────────────────────────────────────
    # Document binding
    # ──────────────────────────────────────────────────────────
    def _apply_paths(self, eng_pdf_path, tr_target_path, output_dir):
        """Resolve the master PDF, translated target and output folder."""
        self.eng_pdf_path = os.path.abspath(eng_pdf_path) if eng_pdf_path else ""
        self.tr_target_path = os.path.abspath(tr_target_path) if tr_target_path else ""

        # Honour the output folder chosen on the Inspection tab so the Region
        # Inspector's comparison images land alongside the rest of the run
        # instead of in a hardcoded relative directory.
        if output_dir:
            self.output_crops_dir = os.path.join(
                os.path.abspath(output_dir), "Cropped_Comparison", "Region_Inspector"
            )
        else:
            self.output_crops_dir = DEFAULT_CROPS_OUTPUT_DIR

        self.total_pages = get_page_count(self.eng_pdf_path) if self.eng_pdf_path else 0

    def load(self, eng_pdf_path, tr_target_path=None, output_dir=None):
        """
        Point the inspector at a document and re-render.

        Called by the main window whenever the user opens the Region Inspector
        tab. Switching to a different master PDF clears any regions already
        drawn, since their coordinates belong to the previous document;
        re-loading the same PDF keeps them.
        """
        previous = self.eng_pdf_path
        self._apply_paths(eng_pdf_path, tr_target_path, output_dir)

        if self.eng_pdf_path != previous:
            self.regions = []
            self.active_region_id = None
            self.check_results = []
            self.current_page = 1
            self._margin_scan = {}      # element cache belongs to the old document
            try:
                for row in self.results_tree.get_children():
                    self.results_tree.delete(row)
            except Exception:
                pass

        self.current_page = max(1, min(self.current_page, max(1, self.total_pages)))

        try:
            self.page_spin.configure(to=max(1, self.total_pages))
            self.page_total_lbl.configure(text=f"/ {self.total_pages}")
        except Exception:
            pass

        self._sync_spinbox()
        self._update_source_label()
        self._load_and_render_page()

    def _update_source_label(self):
        """Refresh the strip that says which document is currently loaded."""
        try:
            if not self.eng_pdf_path or self.total_pages == 0:
                txt = "No master PDF loaded"
            else:
                tr = os.path.basename(self.tr_target_path.rstrip("\\/")) if self.tr_target_path else "none selected"
                txt = (f"Master: {os.path.basename(self.eng_pdf_path)}   "
                       f"({self.total_pages} pages)      Translated: {tr}")
            self.source_lbl.configure(text=txt)
        except Exception:
            pass

    def _build_ui(self):
        # 0. The whole tab scrolls.
        #
        # The results table lives at the bottom of the right column and used to
        # get whatever vertical space the window had left over, which on a
        # laptop screen was about two rows. Giving the content a floor taller
        # than a typical window and letting the tab scroll means the table gets
        # room to be read; on a large monitor the content still fits and no
        # scrollbar appears.
        self._scroll = ctk.CTkScrollableFrame(
            self, fg_color=UI_BG_CANVAS, corner_radius=0,
            scrollbar_button_color=XYLEM_BLUE,
            scrollbar_button_hover_color=DEPENDABLE_BLUE)
        self._scroll.pack(fill="both", expand=True)
        body = self._scroll

        # 1. Source strip — which document the inspector is pointed at
        header_frame = ctk.CTkFrame(body, fg_color=DEPENDABLE_BLUE, corner_radius=8)
        header_frame.pack(fill="x", padx=14, pady=(12, 8))
        self._header_frame = header_frame

        self.source_lbl = ctk.CTkLabel(
            header_frame,
            text="No master PDF loaded",
            font=_font(size=12, weight="bold"),
            text_color=NEUTRAL_WHITE,
            anchor="w",
            justify="left"
        )
        self.source_lbl.pack(side="left", padx=16, pady=10)

        sub_lbl = ctk.CTkLabel(
            header_frame,
            text="Draw regions on canvas \u2022 Nested sub-regions \u2022 Exact Match \u2022 Don't Compare Text (Visual Match) \u2022 Compared images only",
            font=_font(size=11),
            text_color=CLARITY_BLUE
        )
        sub_lbl.pack(side="right", padx=16, pady=10)

        # 2. Main Content Split Pane
        # Inside a scrolling parent nothing stretches vertically on its own, so
        # the two columns are given an explicit height and told not to shrink to
        # their contents. _fit_content_height() keeps that in step with the
        # window.
        content_frame = ctk.CTkFrame(body, fg_color="transparent",
                                     height=MIN_CONTENT_HEIGHT)
        content_frame.pack(fill="x", padx=14, pady=(0, 10))
        content_frame.pack_propagate(False)
        self._content_frame = content_frame
        self._content_height = MIN_CONTENT_HEIGHT

        # Left Pane: Page Preview & Canvas
        left_card = ctk.CTkFrame(content_frame, fg_color=UI_CARD_BG, corner_radius=8, border_width=1, border_color=UI_BORDER)
        left_card.pack(side="left", fill="both", expand=True, padx=(0, 6))

        # Left Nav Bar
        nav_bar = ctk.CTkFrame(left_card, fg_color=UI_CARD_WELL, corner_radius=6)
        nav_bar.pack(fill="x", padx=8, pady=6)

        ctk.CTkButton(nav_bar, text="\u00ab First", width=55, height=28, fg_color=XYLEM_BLUE, text_color=NEUTRAL_WHITE, font=_font(size=11, weight="bold"), command=self._goto_first_page).pack(side="left", padx=2)
        ctk.CTkButton(nav_bar, text="\u25c0 Prev", width=55, height=28, fg_color=DEPENDABLE_BLUE, text_color=NEUTRAL_WHITE, font=_font(size=11), command=self._goto_prev_page).pack(side="left", padx=2)

        ctk.CTkLabel(nav_bar, text="Page:", font=_font(size=11, weight="bold"), text_color=DEPENDABLE_BLUE).pack(side="left", padx=(8, 2))
        self.page_spin = tk.Spinbox(nav_bar, from_=1, to=max(1, self.total_pages), width=4, font=theme.get_font(10), command=self._on_spinbox_change)
        self.page_spin.delete(0, "end")
        self.page_spin.insert(0, str(self.current_page))
        self.page_spin.bind("<Return>", lambda e: self._on_spinbox_change())
        self.page_spin.pack(side="left", padx=2)

        self.page_total_lbl = ctk.CTkLabel(nav_bar, text=f"/ {self.total_pages}", font=_font(size=11), text_color=DEPENDABLE_BLUE)
        self.page_total_lbl.pack(side="left", padx=(2, 8))

        ctk.CTkButton(nav_bar, text="Next \u25b6", width=55, height=28, fg_color=DEPENDABLE_BLUE, text_color=NEUTRAL_WHITE, font=_font(size=11), command=self._goto_next_page).pack(side="left", padx=2)
        ctk.CTkButton(nav_bar, text="Last \u00bb", width=55, height=28, fg_color=XYLEM_BLUE, text_color=NEUTRAL_WHITE, font=_font(size=11, weight="bold"), command=self._goto_last_page).pack(side="left", padx=2)

        # Zoom Controls
        ctk.CTkButton(nav_bar, text="Zoom -", width=55, height=26, fg_color=UI_CARD_BG, text_color=DEPENDABLE_BLUE, border_width=1, border_color=UI_BORDER, font=_font(size=10), command=self._zoom_out).pack(side="right", padx=2)
        self.zoom_lbl = ctk.CTkLabel(nav_bar, text="125%", font=_font(size=11, weight="bold"), text_color=DEPENDABLE_BLUE)
        self.zoom_lbl.pack(side="right", padx=2)
        ctk.CTkButton(nav_bar, text="Zoom +", width=55, height=26, fg_color=UI_CARD_BG, text_color=DEPENDABLE_BLUE, border_width=1, border_color=UI_BORDER, font=_font(size=10), command=self._zoom_in).pack(side="right", padx=2)

        # Ignored margin editor, directly above the page it describes
        self._build_margin_bar(left_card)

        # Canvas with Scrollbars
        canvas_container = tk.Frame(left_card, bg=UI_CARD_BG)
        canvas_container.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        self.canvas = tk.Canvas(canvas_container, bg="#333333", cursor="crosshair")
        v_scroll = tk.Scrollbar(canvas_container, orient="vertical", command=self.canvas.yview)
        h_scroll = tk.Scrollbar(canvas_container, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(xscrollcommand=h_scroll.set, yscrollcommand=v_scroll.set)

        v_scroll.pack(side="right", fill="y")
        h_scroll.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.canvas.bind("<Motion>", self._on_canvas_motion)

        # Helper Tip Bar
        tip_frame = ctk.CTkFrame(left_card, fg_color=UI_CARD_WELL, corner_radius=6)
        tip_frame.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(
            tip_frame,
            text=("\U0001f4a1 Drag to draw regions • nested selections become Sub-Regions (Region 1.1) "
                  "• mouse wheel scrolls the page, Ctrl+wheel zooms, Shift+wheel pans"),
            font=_font(size=10, slant="italic"),
            text_color=DEPENDABLE_BLUE
        ).pack(side="left", padx=8, pady=4)

        # Right Pane: Multi-Region Management & Results
        right_card = ctk.CTkFrame(content_frame, fg_color=UI_CARD_BG, corner_radius=8, border_width=1, border_color=UI_BORDER, width=560)
        right_card.pack(side="right", fill="both", expand=True, padx=(6, 0))

        # 1. Multi-Region Management Card
        mgmt_header = ctk.CTkFrame(right_card, fg_color="transparent")
        mgmt_header.pack(fill="x", padx=10, pady=(8, 4))
        ctk.CTkLabel(mgmt_header, text="Defined Region Selections", font=_font(size=12, weight="bold"), text_color=DEPENDABLE_BLUE).pack(side="left")

        tmpl_bar = ctk.CTkFrame(right_card, fg_color=UI_CARD_WELL, corner_radius=6)
        tmpl_bar.pack(fill="x", padx=10, pady=(6, 2))
        ctk.CTkLabel(tmpl_bar, text="Stylesheet Template:", font=_font(size=10, weight="bold"),
                     text_color=DEPENDABLE_BLUE).pack(side="left", padx=(10, 4), pady=6)
        self.template_var = tk.StringVar(value="")
        self.template_menu = ctk.CTkOptionMenu(
            tmpl_bar, variable=self.template_var, values=["(none)"], width=190, height=26,
            font=_font(size=10), fg_color=UI_CARD_BG, button_color=XYLEM_BLUE,
            text_color=DEPENDABLE_BLUE)
        self.template_menu.pack(side="left", padx=4, pady=6)
        ctk.CTkButton(tmpl_bar, text="Load", width=58, height=26, fg_color=XYLEM_BLUE,
                      text_color=NEUTRAL_WHITE, font=_font(size=10, weight="bold"),
                      command=self._on_load_template).pack(side="left", padx=3, pady=6)
        ctk.CTkButton(tmpl_bar, text="Save As...", width=82, height=26, fg_color=DEPENDABLE_BLUE,
                      text_color=NEUTRAL_WHITE, font=_font(size=10, weight="bold"),
                      command=self._on_save_template).pack(side="left", padx=3, pady=6)
        ctk.CTkButton(tmpl_bar, text="Delete", width=62, height=26, fg_color=UI_CARD_BG,
                      text_color=DEPENDABLE_BLUE, border_width=1, border_color=UI_BORDER,
                      font=_font(size=10), command=self._on_delete_template).pack(side="left", padx=3, pady=6)

        mgmt_btn_bar = ctk.CTkFrame(right_card, fg_color="transparent")
        mgmt_btn_bar.pack(fill="x", padx=10, pady=(0, 4))

        ctk.CTkButton(mgmt_btn_bar, text="➕ Add Selection", width=95, height=26, fg_color=XYLEM_BLUE, text_color=NEUTRAL_WHITE, font=_font(size=10, weight="bold"), command=self._on_btn_add_region).pack(side="left", padx=2)
        ctk.CTkButton(mgmt_btn_bar, text="🗑️ Delete Selection", width=95, height=26, fg_color=UI_CARD_WELL, text_color=DEPENDABLE_BLUE, font=_font(size=10), command=self._on_btn_delete_region).pack(side="left", padx=2)
        ctk.CTkButton(mgmt_btn_bar, text="🧹 Clear All", width=75, height=26, fg_color=UI_CARD_WELL, text_color=DEPENDABLE_BLUE, font=_font(size=10), command=self._on_btn_clear_regions).pack(side="left", padx=2)

        # Region Treeview (List of defined regions)
        r_table_frame = tk.Frame(right_card, bg=UI_CARD_BG)
        r_table_frame.pack(fill="x", padx=10, pady=2)

        r_cols = ("label", "page", "type", "coords")
        self.region_tree = ttk.Treeview(r_table_frame, columns=r_cols, show="headings", height=6, style=_TREE_STYLE)
        self.region_tree.heading("label", text="Custom Label / Name")
        self.region_tree.heading("page", text="Applies To")
        self.region_tree.heading("type", text="Match Type")
        self.region_tree.heading("coords", text="Coordinates (x0, y0, x1, y1)")

        self.region_tree.column("label", width=140, anchor="w")
        self.region_tree.column("page", width=160, anchor="w")
        self.region_tree.column("type", width=115, anchor="center")
        self.region_tree.column("coords", width=170, anchor="w")

        r_scroll = tk.Scrollbar(r_table_frame, orient="vertical", command=self.region_tree.yview)
        self.region_tree.configure(yscrollcommand=r_scroll.set)
        r_scroll.pack(side="right", fill="y")
        self.region_tree.pack(side="left", fill="both", expand=True)

        self.region_tree.bind("<<TreeviewSelect>>", self._on_region_tree_select)

        # 2. Active Region Editor, Exact Match & Don't Compare Text Toggles
        editor_card = ctk.CTkFrame(right_card, fg_color=UI_CARD_WELL, corner_radius=6)
        editor_card.pack(fill="x", padx=10, pady=6)

        edit_row = ctk.CTkFrame(editor_card, fg_color="transparent")
        edit_row.pack(fill="x", padx=8, pady=(6, 2))

        ctk.CTkLabel(edit_row, text="Label:", font=_font(size=11, weight="bold"), text_color=DEPENDABLE_BLUE).pack(side="left")
        self.label_var = tk.StringVar()
        self.label_entry = ctk.CTkEntry(edit_row, textvariable=self.label_var, font=_font(size=11, weight="bold"), width=130, height=28)
        self.label_entry.pack(side="left", padx=4)
        self.label_entry.bind("<KeyRelease>", self._on_label_entry_change)

        self.exact_match_var = ctk.BooleanVar(value=False)
        self.exact_match_chk = ctk.CTkCheckBox(
            edit_row,
            text="Exact Match (100%)",
            variable=self.exact_match_var,
            font=_font(size=10, weight="bold"),
            text_color=DEPENDABLE_BLUE,
            command=self._on_exact_match_toggle
        )
        self.exact_match_chk.pack(side="left", padx=4)

        self.scope_only_var = ctk.BooleanVar(value=False)
        self.scope_only_chk = ctk.CTkCheckBox(
            edit_row,
            text="Don't Compare (Scope Only)",
            variable=self.scope_only_var,
            font=_font(size=10, weight="bold"),
            text_color=DEPENDABLE_BLUE,
            command=self._on_scope_only_toggle
        )
        self.scope_only_chk.pack(side="left", padx=4)

        self.dont_compare_text_var = ctk.BooleanVar(value=False)
        self.dont_compare_text_chk = ctk.CTkCheckBox(
            edit_row,
            text="Don't Compare Text (Visual Match)",
            variable=self.dont_compare_text_var,
            font=_font(size=10, weight="bold"),
            text_color=DEPENDABLE_BLUE,
            command=self._on_dont_compare_text_toggle
        )
        self.dont_compare_text_chk.pack(side="left", padx=4)

        # Page scope and variant grouping. A header is not tied to the page it
        # was drawn on, and a mirrored layout needs more than one accepted
        # rectangle, so both live per-region rather than per-run.
        scope_row = ctk.CTkFrame(editor_card, fg_color="transparent")
        scope_row.pack(fill="x", padx=8, pady=(0, 4))

        ctk.CTkLabel(scope_row, text="Applies to:", font=_font(size=10, weight="bold"),
                     text_color=DEPENDABLE_BLUE).pack(side="left")
        self.scope_var = tk.StringVar(value=SCOPE_LABELS[SCOPE_SINGLE])
        self.scope_menu = ctk.CTkOptionMenu(
            scope_row, variable=self.scope_var,
            values=[SCOPE_LABELS[t] for t in SCOPE_TYPES],
            width=140, height=26, font=_font(size=10),
            fg_color=UI_CARD_BG, button_color=XYLEM_BLUE,
            text_color=DEPENDABLE_BLUE, command=self._on_scope_change)
        self.scope_menu.pack(side="left", padx=4)

        self.scope_pages_var = tk.StringVar()
        self.scope_pages_entry = ctk.CTkEntry(
            scope_row, textvariable=self.scope_pages_var, width=110, height=26,
            font=_font(size=10), placeholder_text="e.g. 3-8 or 1,5,9")
        self.scope_pages_entry.pack(side="left", padx=4)
        self.scope_pages_entry.bind("<KeyRelease>", lambda _e: self._on_scope_change())

        ctk.CTkLabel(scope_row, text="Variant group:", font=_font(size=10, weight="bold"),
                     text_color=DEPENDABLE_BLUE).pack(side="left", padx=(14, 2))
        self.variant_var = tk.StringVar()
        self.variant_entry = ctk.CTkEntry(
            scope_row, textvariable=self.variant_var, width=130, height=26,
            font=_font(size=10), placeholder_text="e.g. Page Header")
        self.variant_entry.pack(side="left", padx=4)
        self.variant_entry.bind("<KeyRelease>", lambda _e: self._on_variant_change())

        # On its own line rather than trailing the row: the row is already five
        # controls wide, and an explanation that gets clipped explains nothing.
        ctk.CTkLabel(editor_card,
                     text="Page range / Specific pages use the box beside the menu (3-8 or 1,5,9) "
                          "• regions sharing a variant group pass if ANY of them matches",
                     font=_font(size=9), text_color=NEUTRAL_DARK_GR,
                     anchor="w", justify="left").pack(fill="x", padx=10, pady=(0, 4))

        content_hdr = ctk.CTkFrame(editor_card, fg_color="transparent")
        content_hdr.pack(fill="x", padx=8, pady=(2, 1))
        ctk.CTkLabel(content_hdr, text="Extracted English Master Content:", font=_font(size=10, weight="bold"), text_color=DEPENDABLE_BLUE).pack(side="left")
        self.coords_lbl = ctk.CTkLabel(content_hdr, text="No region selected", font=_font(family="Consolas", size=9), text_color=XYLEM_BLUE)
        self.coords_lbl.pack(side="right")
        self.eng_text_box = ctk.CTkTextbox(editor_card, height=40, font=_font(size=10), fg_color=UI_CARD_BG, text_color=DEPENDABLE_BLUE)
        self.eng_text_box.pack(fill="x", padx=8, pady=(0, 6))

        # Plenty of regions contain no text at all - a divider rule, the Xylem
        # logo, a hazard icon. The text box shows nothing useful for those, so
        # render what is actually inside the box.
        prev_hdr = ctk.CTkFrame(editor_card, fg_color="transparent")
        prev_hdr.pack(fill="x", padx=8, pady=(0, 1))
        ctk.CTkLabel(prev_hdr, text="Selected Region Preview:", font=_font(size=10, weight="bold"),
                     text_color=DEPENDABLE_BLUE).pack(side="left")
        self.preview_size_lbl = ctk.CTkLabel(prev_hdr, text="", font=_font(family="Consolas", size=9),
                                             text_color=XYLEM_BLUE)
        self.preview_size_lbl.pack(side="right")

        # tk.Label, not CTkLabel: CTkLabel.configure() re-applies the currently
        # bound image, which throws once the previous PhotoImage is released.
        self.region_preview_lbl = tk.Label(
            editor_card, bd=1, relief="solid", bg=NEUTRAL_WHITE,
            highlightthickness=0, text="[Drag on the page to select an area]",
            fg=NEUTRAL_DARK_GR, font=theme.get_font(9), height=4)
        self.region_preview_lbl.pack(fill="x", padx=8, pady=(0, 8))

        # 3. Parameters Card (Tolerance & Pass Threshold)
        params_card = ctk.CTkFrame(right_card, fg_color="transparent")
        params_card.pack(fill="x", padx=10, pady=2)

        ctk.CTkLabel(params_card, text="Vertical Tolerance (\u00b1 \u0394y pt):", font=_font(size=10),
                     text_color=DEPENDABLE_BLUE).pack(side="left")
        self.tol_spin = tk.Spinbox(params_card, from_=0, to=150, width=5, font=theme.get_font(9))
        self.tol_spin.delete(0, "end")
        self.tol_spin.insert(0, str(DEFAULT_Y_TOLERANCE))
        self.tol_spin.pack(side="left", padx=6)

        # Translation changes line width as well as line count, so the search
        # window needs slack on both axes, not just vertically.
        ctk.CTkLabel(params_card, text="Horizontal Tolerance (\u00b1 \u0394x pt):", font=_font(size=10),
                     text_color=DEPENDABLE_BLUE).pack(side="left", padx=(10, 0))
        self.xtol_spin = tk.Spinbox(params_card, from_=0, to=150, width=5, font=theme.get_font(9))
        self.xtol_spin.delete(0, "end")
        self.xtol_spin.insert(0, str(DEFAULT_X_TOLERANCE))
        self.xtol_spin.pack(side="left", padx=6)

        ctk.CTkLabel(params_card, text="Pass Threshold (%):", font=_font(size=10),
                     text_color=DEPENDABLE_BLUE).pack(side="left", padx=(10, 0))
        self.thresh_spin = tk.Spinbox(params_card, from_=40, to=100, width=5, font=theme.get_font(9))
        self.thresh_spin.delete(0, "end")
        self.thresh_spin.insert(0, str(DEFAULT_PASS_THRESHOLD))
        self.thresh_spin.pack(side="left", padx=6)

        # 4. Action Buttons (Run & Open Folder)
        action_box = ctk.CTkFrame(right_card, fg_color="transparent")
        action_box.pack(fill="x", padx=10, pady=4)

        self.run_btn = ctk.CTkButton(
            action_box,
            text="\u25b6  Check All Selections Across Translated PDFs",
            font=_font(size=11, weight="bold"),
            fg_color=DYNAMIC_GREEN,
            text_color=DEPENDABLE_BLUE,
            height=34,
            command=self._start_batch_check
        )
        self.run_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self.open_crops_btn = ctk.CTkButton(
            action_box,
            text="\U0001f4c2 Open Cropped Comparison Folder",
            font=_font(size=10, weight="bold"),
            fg_color=XYLEM_BLUE,
            text_color=NEUTRAL_WHITE,
            height=34,
            command=self._open_crops_folder
        )
        self.open_crops_btn.pack(side="right")

        self.progress_bar = ctk.CTkProgressBar(right_card, height=8, progress_color=XYLEM_BLUE)
        self.progress_bar.set(0.0)
        self.progress_bar.pack(fill="x", padx=10, pady=(2, 2))

        self.status_lbl = ctk.CTkLabel(right_card, text="Ready \u2022 Draw regions on page or edit labels above", font=_font(size=10, weight="bold"), text_color=XYLEM_BLUE)
        self.status_lbl.pack(anchor="w", padx=10, pady=(0, 2))

        # 5. Results Table & Cropped Comparison Preview
        results_card = ctk.CTkFrame(right_card, fg_color=UI_CARD_BG, corner_radius=6, border_width=1, border_color=UI_BORDER)
        results_card.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        table_frame = tk.Frame(results_card, bg=UI_CARD_BG)
        table_frame.pack(fill="both", expand=True, padx=4, pady=4)

        columns = ("region_label", "tr_name", "page", "similarity", "shift", "status")
        self.results_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=10, style=_TREE_STYLE)
        self.results_tree.heading("region_label", text="Region Label")
        self.results_tree.heading("tr_name", text="Translated PDF Name")
        self.results_tree.heading("page", text="Page")
        self.results_tree.heading("similarity", text="Similarity / Found")
        self.results_tree.heading("shift", text="Shift (pt)")
        self.results_tree.heading("status", text="Status")

        self.results_tree.column("region_label", width=110, anchor="w")
        self.results_tree.column("tr_name", width=175, anchor="w")
        self.results_tree.column("page", width=45, anchor="center")
        self.results_tree.column("similarity", width=130, anchor="center")
        self.results_tree.column("shift", width=65, anchor="center")
        self.results_tree.column("status", width=120, anchor="center")

        res_scroll = tk.Scrollbar(table_frame, orient="vertical", command=self.results_tree.yview)
        self.results_tree.configure(yscrollcommand=res_scroll.set)
        res_scroll.pack(side="right", fill="y")
        self.results_tree.pack(side="left", fill="both", expand=True)

        self.results_tree.tag_configure("pass", background="#D9EAD3", foreground="#274E13")
        self.results_tree.tag_configure("check", background="#FFF2CC", foreground="#7F6000")
        self.results_tree.tag_configure("fail", background="#FCE5CD", foreground="#783F04")

        self.results_tree.bind("<<TreeviewSelect>>", self._on_results_tree_select)

        # Selected row preview (Text & Visual Crop Preview)
        preview_container = ctk.CTkFrame(results_card, fg_color="transparent")
        preview_container.pack(fill="x", padx=4, pady=(2, 4))

        # Text preview
        txt_prev_frame = ctk.CTkFrame(preview_container, fg_color="transparent")
        txt_prev_frame.pack(side="left", fill="both", expand=True, padx=(0, 4))
        ctk.CTkLabel(txt_prev_frame, text="Matched Translated Text:", font=_font(size=9, weight="bold"), text_color=DEPENDABLE_BLUE).pack(anchor="w")
        self.tr_text_box = ctk.CTkTextbox(txt_prev_frame, height=45, font=_font(size=10), fg_color=UI_CARD_WELL, text_color=DEPENDABLE_BLUE)
        self.tr_text_box.pack(fill="both", expand=True)

        # Visual Image Crop Comparison Thumbnail
        img_prev_frame = ctk.CTkFrame(preview_container, fg_color="transparent", width=180)
        img_prev_frame.pack(side="right", padx=(4, 0))
        ctk.CTkLabel(img_prev_frame, text="Visual Crop Comparison:", font=_font(size=9, weight="bold"), text_color=DEPENDABLE_BLUE).pack(anchor="w")
        self.crop_thumb_lbl = tk.Label(img_prev_frame, text="[Select row to preview crop]", font=theme.get_font(8), bg=UI_CARD_WELL, width=28, height=3)
        self.crop_thumb_lbl.pack(fill="both", expand=True)
        self.crop_thumb_lbl.bind("<Button-1>", lambda e: self._open_selected_crop_image())

        self._wire_scrolling()

    # ──────────────────────────────────────────────────────────
    # Scrolling
    #
    # CTkScrollableFrame catches the wheel with bind_all, which would mean the
    # whole tab slides whenever the pointer is anywhere inside it - including
    # over the page viewer, where the wheel should turn the page instead. Tk
    # runs the widget's own bindings before the all-level one, so a widget
    # binding that returns "break" claims the wheel for that widget. That is
    # how the page viewer and the two tables keep theirs.
    # ──────────────────────────────────────────────────────────
    def _wire_scrolling(self):
        self._fit_content_height()
        try:
            # Keep the columns as tall as the window when the window is the
            # taller of the two. add="+" so CTk's own handler still runs.
            self._scroll._parent_canvas.bind(
                "<Configure>", lambda _e: self._fit_content_height(), add="+")
        except Exception as e:
            print(f"[Layout] Could not track the tab height: {e}")

        # The page viewer scrolls the page, not the tab.
        self.canvas.configure(xscrollincrement=1, yscrollincrement=1)
        self._bind_wheel(self.canvas, self._on_page_wheel)

        # Each table scrolls itself while it has anywhere to go, and hands the
        # wheel back to the tab once it is at the end - which is what makes a
        # short list inside a long page feel right rather than sticky.
        for tree in (self.region_tree, self.results_tree):
            self._bind_wheel(tree, lambda e, t=tree: self._on_tree_wheel(t, e))

    def _bind_wheel(self, widget, handler):
        """Bind the wheel across platforms: Windows/macOS deltas and X11 buttons."""
        for seq in ("<MouseWheel>", "<Shift-MouseWheel>", "<Control-MouseWheel>",
                    "<Button-4>", "<Button-5>"):
            try:
                widget.bind(seq, handler, add="+")
            except Exception:
                pass

    @staticmethod
    def _wheel_direction(event):
        """-1 for a scroll up, +1 for a scroll down, on any platform."""
        if getattr(event, "num", 0) in (4, 5):        # X11
            return -1 if event.num == 4 else 1
        delta = getattr(event, "delta", 0)
        if delta == 0:
            return 0
        if abs(delta) >= 120:                          # Windows, multiples of 120
            return -int(delta / 120)
        return -int(delta)                             # macOS, small deltas

    def _fit_content_height(self):
        """Grow the columns to fill a tall window; never below the floor."""
        try:
            avail = self._scroll._parent_canvas.winfo_height()
            head = self._header_frame.winfo_height() or 60
            want = max(MIN_CONTENT_HEIGHT, avail - head - 30)
            if abs(want - self._content_height) > 4:
                self._content_height = want
                self._content_frame.configure(height=want)
        except Exception:
            pass

    def _on_page_wheel(self, event):
        """Wheel over the page viewer: turn the page. Ctrl zooms, Shift pans."""
        step = self._wheel_direction(event)
        if not step:
            return None
        shift = bool(event.state & 0x0001)
        ctrl = bool(event.state & 0x0004)

        if ctrl:
            self._zoom_out() if step > 0 else self._zoom_in()
            return "break"

        view = self.canvas.xview() if shift else self.canvas.yview()
        if view == (0.0, 1.0):
            return None            # nothing to scroll here; let the tab take it
        if shift:
            self.canvas.xview_scroll(step * PAGE_WHEEL_STEP, "units")
        else:
            self.canvas.yview_scroll(step * PAGE_WHEEL_STEP, "units")
        return "break"

    def _on_tree_wheel(self, tree, event):
        step = self._wheel_direction(event)
        if not step:
            return None
        try:
            first, last = tree.yview()
        except Exception:
            return None
        if (first, last) == (0.0, 1.0):
            return None            # fully visible; the tab scrolls instead
        if (step < 0 and first <= 0.0) or (step > 0 and last >= 1.0):
            return None            # already at the end; pass the wheel on
        tree.yview_scroll(step, "units")
        return "break"

    # ──────────────────────────────────────────────────────────
    # Ignored page margins
    #
    # The header and footer bands used to be two constants in crop_images.py,
    # measured off one stylesheet. Every other stylesheet needs different ones,
    # and nobody can guess "46 pt" from looking at a page. So the numbers are
    # editable, they are shown on the page itself as shaded bands with a
    # draggable edge, and the panel says how many graphics on this page the
    # current setting would throw away.
    # ──────────────────────────────────────────────────────────
    def _build_margin_bar(self, parent):
        bar = ctk.CTkFrame(parent, fg_color=UI_CARD_WELL, corner_radius=6)
        bar.pack(fill="x", padx=8, pady=(0, 6))

        row = ctk.CTkFrame(bar, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=(6, 0))

        ctk.CTkLabel(row, text="Ignored margins:", font=_font(size=11, weight="bold"),
                     text_color=DEPENDABLE_BLUE).pack(side="left")

        self.margin_spins = {}
        for side in MARGIN_SIDE_ORDER:
            ctk.CTkLabel(row, text=MARGIN_FIELD_LABELS[side], font=_font(size=10),
                         text_color=DEPENDABLE_BLUE).pack(side="left", padx=(8, 2))
            sp = tk.Spinbox(row, from_=0, to=400, increment=1, width=5,
                            font=theme.get_font(9),
                            command=lambda s=side: self._on_margin_spin(s))
            sp.delete(0, "end")
            sp.insert(0, f"{self.margins[side]:g}")
            sp.bind("<KeyRelease>", lambda _e, s=side: self._on_margin_spin(s))
            sp.bind("<Return>", lambda _e, s=side: self._on_margin_spin(s))
            sp.bind("<FocusOut>", lambda _e, s=side: self._on_margin_spin(s))
            sp.pack(side="left")
            self.margin_spins[side] = sp

        ctk.CTkLabel(row, text="pt", font=_font(size=10),
                     text_color=NEUTRAL_DARK_GR).pack(side="left", padx=(3, 0))

        ctk.CTkButton(row, text="Reset", width=52, height=24, fg_color=UI_CARD_BG,
                      text_color=DEPENDABLE_BLUE, border_width=1, border_color=UI_BORDER,
                      font=_font(size=10), command=self._reset_margins).pack(side="right", padx=2)

        self.show_margins_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(row, text="Show on page", variable=self.show_margins_var,
                        font=_font(size=10, weight="bold"), text_color=DEPENDABLE_BLUE,
                        checkbox_width=16, checkbox_height=16,
                        command=self._on_show_margins_toggle).pack(side="right", padx=6)

        self.margin_hint_lbl = ctk.CTkLabel(
            bar, text="", font=_font(size=9), text_color=NEUTRAL_DARK_GR,
            anchor="w", justify="left")
        self.margin_hint_lbl.pack(fill="x", padx=10, pady=(1, 6))

    def get_margins(self):
        """The margins currently set, for the run and for saving."""
        return page_margins.normalize(self.margins,
                                      self.page_width_pt, self.page_height_pt)

    def set_margins(self, margins, redraw=True):
        """Adopt a margins dict (from a template, or remembered settings)."""
        self.margins = page_margins.normalize(margins)
        self._sync_margin_spins()
        if redraw:
            self._refresh_margin_overlay()

    def _reset_margins(self):
        self.set_margins(page_margins.DEFAULT_MARGINS)

    def _on_show_margins_toggle(self):
        self._refresh_margin_overlay()

    def _sync_margin_spins(self):
        """Push the model back into the four fields without re-triggering them."""
        self._suspend_margin_sync = True
        try:
            for side, sp in self.margin_spins.items():
                want = f"{self.margins[side]:g}"
                if sp.get() != want:
                    sp.delete(0, "end")
                    sp.insert(0, want)
        except Exception:
            pass
        finally:
            self._suspend_margin_sync = False

    def _on_margin_spin(self, side):
        """A field was typed in or stepped."""
        if self._suspend_margin_sync:
            return
        try:
            raw = self.margin_spins[side].get().strip()
        except Exception:
            return                      # widget going away during teardown
        if raw == "":
            return                      # mid-edit; wait for a real value
        try:
            value = float(raw)
        except ValueError:
            return
        self.margins = page_margins.normalize(
            {**self.margins, side: value}, self.page_width_pt, self.page_height_pt)
        self._refresh_margin_overlay()
        self._schedule_margin_save()

    def _schedule_margin_save(self):
        """
        Tell the host the margins moved, once the user stops moving them.

        Typing "120" into a field is three keystrokes and three valid values;
        writing settings.json on each would be three files' worth of churn for
        one decision.
        """
        if not callable(self._on_margins_changed):
            return
        try:
            if self._margin_save_job:
                self.after_cancel(self._margin_save_job)
            self._margin_save_job = self.after(700, self._fire_margin_save)
        except Exception:
            pass

    def _fire_margin_save(self):
        self._margin_save_job = None
        try:
            self._on_margins_changed(self.get_margins())
        except Exception as e:
            print(f"[Margins] Could not remember the margins: {e}")

    # ── canvas overlay ────────────────────────────────────────
    def _page_to_canvas(self, value, axis):
        return 10 + value * self.zoom

    def _canvas_to_page(self, value, axis):
        return max(0.0, (value - 10) / self.zoom)

    def _guide_positions(self):
        """Canvas coordinate of each guide line, by side."""
        m = self.get_margins()
        return {
            "header": self._page_to_canvas(m["header"], "y"),
            "footer": self._page_to_canvas(self.page_height_pt - m["footer"], "y"),
            "left": self._page_to_canvas(m["left"], "x"),
            "right": self._page_to_canvas(self.page_width_pt - m["right"], "x"),
        }

    def _margin_elements(self):
        """
        Every graphic on this page, before any margin is applied.

        Scanned once per page and cached: the point of the preview is that it
        keeps up while a guide is being dragged, and re-parsing the page
        content on every mouse-move would not.
        """
        key = (self.eng_pdf_path, self.current_page)
        if key in self._margin_scan:
            return self._margin_scan[key]

        found = []
        try:
            from core.crop_images import get_all_image_candidates
            zero = page_margins.from_values(0, 0, 0, 0)
            with fitz.open(self.eng_pdf_path) as doc:
                page = doc[self.current_page - 1]
                for rect, reason in get_all_image_candidates(
                        page, margins=zero, include_ignored=True):
                    # "too small" is sub-pixel noise the cropper would never
                    # have written anyway; showing it would only add clutter.
                    if reason == "too small":
                        continue
                    found.append(((rect.x0, rect.y0, rect.x1, rect.y1),
                                  reason == "edge artifact"))
        except Exception as e:
            print(f"[Margins] Could not scan page {self.current_page}: {e}")

        self._margin_scan[key] = found
        return found

    def _refresh_margin_overlay(self):
        """Redraw the bands and update the readout. Cheap enough for a drag."""
        self._draw_margin_overlay()
        self._update_margin_hint()

    def _draw_margin_overlay(self):
        self.canvas.delete("margin")
        if not self.show_margins_var.get() or not self.eng_pdf_path or self.total_pages == 0:
            return

        m = self.get_margins()
        pw, ph = self.page_width_pt, self.page_height_pt

        # 1. The ignored bands. tk.Canvas has no alpha, so a stipple pattern is
        #    what lets the page show through the shading.
        for _side, (bx0, by0, bx1, by1) in page_margins.band_boxes(pw, ph, m).items():
            self.canvas.create_rectangle(
                self._page_to_canvas(bx0, "x"), self._page_to_canvas(by0, "y"),
                self._page_to_canvas(bx1, "x"), self._page_to_canvas(by1, "y"),
                fill=MARGIN_BAND_COLOR, stipple="gray12", outline="", tags="margin")

        # 2. What the current numbers would do to this page's graphics
        for rect, was_edge_artifact in self._margin_elements():
            side = page_margins.which_margin(rect, pw, ph, m)
            if side is None and was_edge_artifact:
                continue          # dropped by the thumb-tab rule, not by a margin
            cx0 = self._page_to_canvas(rect[0], "x")
            cy0 = self._page_to_canvas(rect[1], "y")
            cx1 = self._page_to_canvas(rect[2], "x")
            cy1 = self._page_to_canvas(rect[3], "y")
            if side is None:
                self.canvas.create_rectangle(
                    cx0, cy0, cx1, cy1, outline=MARGIN_KEPT_COLOR, width=1,
                    dash=(1, 3), tags="margin")
            else:
                self.canvas.create_rectangle(
                    cx0, cy0, cx1, cy1, outline=MARGIN_BAND_COLOR, width=2,
                    dash=(3, 2), tags="margin")
                self.canvas.create_text(
                    cx1 + 3, cy0, anchor="nw", text="ignored",
                    font=theme.get_font(7, "bold"), fill=MARGIN_BAND_COLOR, tags="margin")

        # 3. The draggable boundary of each band, with its value beside it
        guides = self._guide_positions()
        img_w = 10 + pw * self.zoom
        img_h = 10 + ph * self.zoom
        for side, pos in guides.items():
            if m[side] <= 0:
                continue
            caption = f"{MARGIN_FIELD_LABELS[side]} {page_margins.describe_value(m[side])}"
            band_px = m[side] * self.zoom
            # The caption belongs inside the band it measures, where it covers
            # only furniture. A band too thin to hold it spills outward instead.
            roomy = band_px >= 15

            if MARGIN_AXIS[side] == "y":
                self.canvas.create_line(10, pos, img_w, pos, fill=MARGIN_GUIDE_COLOR,
                                        width=2, tags=("margin", f"guide_{side}"))
                self.canvas.create_rectangle(img_w / 2 - 14, pos - 3, img_w / 2 + 14, pos + 3,
                                             fill=MARGIN_GUIDE_COLOR, outline="",
                                             tags=("margin", f"guide_{side}"))
                inward = 1 if side == "header" else -1
                offset = -4 * inward if roomy else 4 * inward
                self._margin_caption(
                    14, pos + offset, caption,
                    anchor="sw" if (side == "header") == roomy else "nw")
            else:
                self.canvas.create_line(pos, 10, pos, img_h, fill=MARGIN_GUIDE_COLOR,
                                        width=2, tags=("margin", f"guide_{side}"))
                self.canvas.create_rectangle(pos - 3, img_h / 2 - 14, pos + 3, img_h / 2 + 14,
                                             fill=MARGIN_GUIDE_COLOR, outline="",
                                             tags=("margin", f"guide_{side}"))
                # Rotated, so a side caption never collides with the top and
                # bottom ones no matter how narrow the page is on screen.
                inward = 1 if side == "left" else -1
                offset = -5 * inward if roomy else 5 * inward
                self._margin_caption(
                    pos + offset, img_h * 0.72, caption, angle=90,
                    anchor="sw" if (side == "left") == roomy else "nw")

        # Redrawn mid-drag, the overlay would otherwise land on top of the
        # region boxes. Keep it sandwiched: above the page, below the regions.
        try:
            if self.canvas.find_withtag("roi_box"):
                self.canvas.tag_lower("margin", "roi_box")
            else:
                self.canvas.tag_raise("margin", "page_img")
        except Exception:
            pass

    def _margin_caption(self, x, y, text, anchor="nw", angle=0):
        """
        A margin measurement, on a small opaque chip.

        Without the chip the figure lands on top of whatever artwork happens to
        be under it — on this manual, squarely on the logo — and the one number
        the panel exists to communicate becomes the hardest thing to read.
        """
        item = self.canvas.create_text(
            x, y, text=text, anchor=anchor, angle=angle,
            font=theme.get_font(8, "bold"), fill=MARGIN_BAND_COLOR, tags="margin")
        try:
            bx0, by0, bx1, by1 = self.canvas.bbox(item)
            chip = self.canvas.create_rectangle(
                bx0 - 2, by0 - 1, bx1 + 2, by1 + 1,
                fill=NEUTRAL_WHITE, outline="", tags="margin")
            self.canvas.tag_lower(chip, item)
        except Exception:
            pass
        return item

    def _update_margin_hint(self):
        """The line under the fields: millimetres, and the cost on this page."""
        m = self.get_margins()
        sizes = " · ".join(
            f"{MARGIN_FIELD_LABELS[s]} {page_margins.describe_value(m[s])}"
            for s in MARGIN_SIDE_ORDER)

        if not self.eng_pdf_path or self.total_pages == 0:
            self.margin_hint_lbl.configure(text=sizes)
            return

        pw, ph = self.page_width_pt, self.page_height_pt
        kept, dropped = 0, {}
        for rect, was_edge_artifact in self._margin_elements():
            side = page_margins.which_margin(rect, pw, ph, m)
            if side:
                dropped[side] = dropped.get(side, 0) + 1
            elif not was_edge_artifact:
                kept += 1

        if dropped:
            detail = ", ".join(f"{n} {MARGIN_FIELD_LABELS[s].lower()}"
                               for s, n in sorted(dropped.items()))
            effect = (f"page {self.current_page}: {kept} graphic(s) kept, "
                      f"{sum(dropped.values())} ignored ({detail})")
        else:
            effect = f"page {self.current_page}: {kept} graphic(s) kept, none ignored"

        self.margin_hint_lbl.configure(
            text=f"{sizes}\nDrag the orange guides on the page to set these · {effect}")

    # ── dragging a guide ──────────────────────────────────────
    def _guide_at(self, cx, cy):
        """Which guide, if any, the pointer is close enough to grab."""
        if not self.show_margins_var.get() or self.total_pages == 0:
            return None
        m = self.get_margins()
        for side, pos in self._guide_positions().items():
            if m[side] <= 0:
                continue
            near = cy if MARGIN_AXIS[side] == "y" else cx
            if abs(near - pos) <= MARGIN_GRAB_PX:
                return side
        return None

    def _on_canvas_motion(self, event):
        """Turn the cursor into a resize arrow over a guide, so it reads as draggable."""
        if self._drag_guide or self.selection_start:
            return
        side = self._guide_at(self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))
        want = "crosshair"
        if side:
            want = "sb_v_double_arrow" if MARGIN_AXIS[side] == "y" else "sb_h_double_arrow"
        # Reconfiguring on every mouse-move is a Tk round trip per pixel; only
        # touch the widget when the cursor actually has to change.
        if want != getattr(self, "_canvas_cursor", None):
            self._canvas_cursor = want
            self.canvas.configure(cursor=want)

    def _drag_guide_to(self, cx, cy):
        """Set the held margin from a pointer position."""
        side = self._drag_guide
        if MARGIN_AXIS[side] == "y":
            page_pos = self._canvas_to_page(cy, "y")
            value = page_pos if side == "header" else self.page_height_pt - page_pos
        else:
            page_pos = self._canvas_to_page(cx, "x")
            value = page_pos if side == "left" else self.page_width_pt - page_pos
        self.margins = page_margins.normalize(
            {**self.margins, side: max(0.0, value)},
            self.page_width_pt, self.page_height_pt)
        self._sync_margin_spins()
        self._refresh_margin_overlay()

    # ──────────────────────────────────────────────────────────
    # Multi-Region & Sub-Region Management with Consecutive Numbering
    # ──────────────────────────────────────────────────────────
    def _find_enclosing_parent(self, roi_rect: tuple, page_num: int):
        """Find if roi_rect is contained inside an existing top-level region on page_num."""
        for r in self.regions:
            if r["page_num"] == page_num and r.get("parent_id") is None:
                if is_rect_contained_in_parent(roi_rect, r["roi_rect"]):
                    return r
        return None

    def _reindex_and_renumber_regions(self):
        """
        Re-index and consecutively renumber all regions and sub-regions:
        Top-level: Region 1, Region 2, Region 3...
        Sub-regions: Region 1.1, Region 1.2, Region 2.1...
        """
        top_counter = 1
        id_counter = 1

        top_level_regions = [r for r in self.regions if r.get("parent_id") is None]
        for top_r in top_level_regions:
            old_id = top_r["id"]
            new_id = id_counter
            id_counter += 1
            top_r["id"] = new_id

            top_num_str = f"Region {top_counter}"
            if not top_r.get("is_custom_label", False) or top_r["label"].startswith("Region "):
                top_r["label"] = top_num_str

            sub_counter = 1
            sub_regions = [r for r in self.regions if r.get("parent_id") == old_id]
            for sub_r in sub_regions:
                sub_r["parent_id"] = new_id
                sub_r["id"] = id_counter
                id_counter += 1

                sub_num_str = f"Region {top_counter}.{sub_counter}"
                if not sub_r.get("is_custom_label", False) or sub_r["label"].startswith("Region "):
                    sub_r["label"] = sub_num_str
                sub_counter += 1

            top_counter += 1

        ordered = []
        for top_r in top_level_regions:
            ordered.append(top_r)
            subs = [r for r in self.regions if r.get("parent_id") == top_r["id"]]
            ordered.extend(subs)

        self.regions = ordered

    def _add_new_region(self, label: str = None, roi_rect: tuple = None, page_num: int = None):
        p_num = page_num if page_num is not None else self.current_page
        is_last = (p_num == self.total_pages)

        if not roi_rect:
            roi_rect = (35.0, 460.0, 385.0, 565.0)

        parent = self._find_enclosing_parent(roi_rect, p_num)
        parent_id = parent["id"] if parent else None

        new_region = {
            "id": len(self.regions) + 1,
            "label": label or "",
            "page_num": p_num,
            "is_last_page": is_last,
            "roi_rect": roi_rect,
            "eng_text": "",
            "parent_id": parent_id,
            "exact_match": False,
            "dont_compare_text": False,
            "scope_only": False,
            "page_scope": default_scope(p_num, is_last),
            "variant_group": None,
            "color": REGION_COLORS[len(self.regions) % len(REGION_COLORS)],
            "is_custom_label": bool(label)
        }
        self.regions.append(new_region)
        self._reindex_and_renumber_regions()

        self.active_region_id = new_region["id"]
        return new_region

    def _get_active_region(self) -> dict:
        for r in self.regions:
            if r["id"] == self.active_region_id:
                return r
        return self.regions[0] if self.regions else None

    # ──────────────────────────────────────────────────────────
    # Selected-region image preview
    # ──────────────────────────────────────────────────────────
    PREVIEW_MAX_W = 620
    PREVIEW_MAX_H = 96

    def _set_region_preview_image(self, photo, placeholder=""):
        """Swap the preview image, repointing the widget before releasing the old one."""
        previous = getattr(self, "_region_preview_photo", None)
        try:
            if photo is None:
                # tk.Label reads `height` as TEXT LINES when showing text...
                self.region_preview_lbl.configure(image="", text=placeholder, height=4)
                self.region_preview_lbl.image = None
            else:
                # ...and as PIXELS when showing an image. Leaving the text-line
                # value in place clips the picture to a few pixels tall.
                self.region_preview_lbl.configure(
                    image=photo, text="", height=photo.height() + 6)
                self.region_preview_lbl.image = photo
        except tk.TclError as e:
            print(f"[RegionInspector] preview swap failed: {e}")
            photo = None
        self._region_preview_photo = photo
        del previous

    def _update_region_preview(self, region=None):
        """Render whatever sits inside the active region and show it."""
        if region is None:
            region = self._get_active_region()

        if not region or not self.eng_pdf_path:
            self.preview_size_lbl.configure(text="")
            self._set_region_preview_image(None, "[Drag on the page to select an area]")
            return

        rect = region["roi_rect"]
        w_pt, h_pt = rect[2] - rect[0], rect[3] - rect[1]
        self.preview_size_lbl.configure(text=f"{w_pt:.1f} x {h_pt:.1f} pt")

        img = render_region_image(self.eng_pdf_path, region["page_num"], rect, dpi=150)
        if img is None:
            self._set_region_preview_image(None, "[region too small to render]")
            return

        w, h = img.size
        scale = min(self.PREVIEW_MAX_W / max(1, w), self.PREVIEW_MAX_H / max(1, h))
        # Small marks (a thin rule, a tiny icon) are enlarged a little so they
        # are actually legible, but never blown up past 2x.
        scale = min(scale, 2.0)
        if abs(scale - 1.0) > 0.01:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

        try:
            self._set_region_preview_image(ImageTk.PhotoImage(img, master=self.region_preview_lbl))
        except Exception as e:
            self._set_region_preview_image(None, f"[preview failed: {e}]")

    def _describe_master_content(self, r: dict) -> str:
        """
        What the master region contributes to the check.

        For a scoped sub-region the useful information is not the raw clipped
        text but the token-snapped needle that will be searched for inside the
        parent region.
        """
        raw = r.get("eng_text", "") or ""
        if not (r.get("exact_match") and r.get("parent_id")):
            return raw
        try:
            from core.region_engine import needle_from_region
            needle = needle_from_region(self.eng_pdf_path, r["page_num"], r["roi_rect"])
            parent = next((x for x in self.regions if x["id"] == r["parent_id"]), None)
            scope = parent["label"] if parent else "parent region"
            if needle:
                return f"SCOPED EXACT  ->  search for {needle!r}  anywhere inside \"{scope}\""
        except Exception:
            pass
        return raw

    def _get_match_type_string(self, r: dict) -> str:
        if r.get("scope_only", False):
            return "Scope Only (not compared)"
        if r.get("dont_compare_text", False):
            return "Visual (No Text)"
        elif r.get("exact_match", False):
            # A sub-region's exact match is scoped to its parent: the box defines
            # the needle, the parent defines where to look for it.
            if r.get("parent_id"):
                parent = next((x for x in self.regions if x["id"] == r["parent_id"]), None)
                return f"Exact in {parent['label']}" if parent else "Exact in Scope"
            return "Exact (100%)"
        else:
            return "Layout/Presence"

    def _sync_regions_table(self):
        self._updating_selection = True
        try:
            for item in self.region_tree.get_children():
                self.region_tree.delete(item)

            for r in self.regions:
                x0, y0, x1, y1 = r["roi_rect"]
                coords_str = f"[{x0:.1f}, {y0:.1f}, {x1:.1f}, {y1:.1f}]"
                type_str = self._get_match_type_string(r)
                
                display_label = f"  ↳ {r['label']}" if r.get("parent_id") is not None else r["label"]

                self.region_tree.insert(
                    "",
                    "end",
                    iid=str(r["id"]),
                    values=(display_label, self._page_cell(r), type_str, coords_str)
                )

            if self.active_region_id and self.region_tree.exists(str(self.active_region_id)):
                self.region_tree.selection_set(str(self.active_region_id))

            active_r = self._get_active_region()
            if active_r:
                self.label_var.set(active_r["label"])
                self.exact_match_var.set(active_r.get("exact_match", False))
                self.dont_compare_text_var.set(active_r.get("dont_compare_text", False))
                self.scope_only_var.set(active_r.get("scope_only", False))
                self._sync_scope_controls(active_r)
                x0, y0, x1, y1 = active_r["roi_rect"]
                self.coords_lbl.configure(text=f"x: {x0:.1f} \u2192 {x1:.1f}, y: {y0:.1f} \u2192 {y1:.1f}")
                self.eng_text_box.delete("1.0", "end")
                self.eng_text_box.insert("1.0", self._describe_master_content(active_r))
                self._update_region_preview(active_r)
            else:
                self.label_var.set("")
                self.exact_match_var.set(False)
                self.dont_compare_text_var.set(False)
                self.scope_only_var.set(False)
                self._sync_scope_controls(None)
                self.coords_lbl.configure(text="No region selected")
                self.eng_text_box.delete("1.0", "end")
                self.eng_text_box.insert("1.0", "[Drag on the page to select an area]")
                self._update_region_preview(None)
        finally:
            self._updating_selection = False

    def _on_btn_add_region(self):
        self._add_new_region(
            roi_rect=(40.0, 100.0, 380.0, 200.0)
        )
        self._load_and_render_page()

    def _on_btn_delete_region(self):
        if not self.regions:
            return
        active_r = self._get_active_region()
        if active_r:
            del_id = active_r["id"]
            self.regions = [r for r in self.regions if r["id"] != del_id and r.get("parent_id") != del_id]
            self._reindex_and_renumber_regions()
            self.active_region_id = self.regions[0]["id"] if self.regions else None
            self._load_and_render_page()

    def _on_btn_clear_regions(self):
        if self.regions and messagebox.askyesno("Confirm Clear", "Clear all custom regions?"):
            self.regions = []
            self.active_region_id = None
            self._load_and_render_page()

    def _on_region_tree_select(self, event):
        if getattr(self, "_updating_selection", False):
            return
        sel = self.region_tree.selection()
        if not sel:
            return
        r_id = int(sel[0])
        if self.active_region_id == r_id:
            return
        self.active_region_id = r_id
        active_r = self._get_active_region()
        if active_r:
            if active_r["page_num"] != self.current_page:
                self.current_page = active_r["page_num"]
                self._sync_spinbox()
                self._load_and_render_page()
            else:
                self.label_var.set(active_r["label"])
                self.exact_match_var.set(active_r.get("exact_match", False))
                self.dont_compare_text_var.set(active_r.get("dont_compare_text", False))
                self.scope_only_var.set(active_r.get("scope_only", False))
                self._sync_scope_controls(active_r)
                x0, y0, x1, y1 = active_r["roi_rect"]
                self.coords_lbl.configure(text=f"x: {x0:.1f} \u2192 {x1:.1f}, y: {y0:.1f} \u2192 {y1:.1f}")
                self.eng_text_box.delete("1.0", "end")
                self.eng_text_box.insert("1.0", self._describe_master_content(active_r))
                self._update_region_preview(active_r)
                self._draw_all_rois_on_canvas()

    def _on_label_entry_change(self, event):
        active_r = self._get_active_region()
        if active_r:
            new_label = self.label_var.get().strip()
            if new_label:
                active_r["label"] = new_label
                active_r["is_custom_label"] = True
                if self.region_tree.exists(str(active_r["id"])):
                    x0, y0, x1, y1 = active_r["roi_rect"]
                    type_str = self._get_match_type_string(active_r)
                    display_lbl = f"  ↳ {new_label}" if active_r.get("parent_id") is not None else new_label
                    self.region_tree.item(str(active_r["id"]), values=(display_lbl, self._page_cell(active_r), type_str, f"[{x0:.1f}, {y0:.1f}, {x1:.1f}, {y1:.1f}]"))
                self._draw_all_rois_on_canvas()

    # ──────────────────────────────────────────────────────────
    # Page scope, variant groups and stylesheet templates
    # ──────────────────────────────────────────────────────────
    _LABEL_TO_SCOPE = {v: k for k, v in SCOPE_LABELS.items()}

    def _on_scope_change(self, _value=None):
        """Store the chosen page scope on the active region."""
        active_r = self._get_active_region()
        stype = self._LABEL_TO_SCOPE.get(self.scope_var.get(), SCOPE_SINGLE)

        needs_pages = stype in (SCOPE_RANGE, SCOPE_PAGES)
        try:
            self.scope_pages_entry.configure(state="normal" if needs_pages else "disabled")
        except Exception:
            pass
        if not active_r:
            return

        if stype == SCOPE_RANGE:
            pages = parse_pages_expression(self.scope_pages_var.get(), self.total_pages)
            scope = ({"type": SCOPE_RANGE, "from": pages[0], "to": pages[-1]} if pages
                     else {"type": SCOPE_RANGE, "from": 1, "to": max(1, self.total_pages)})
        elif stype == SCOPE_PAGES:
            scope = {"type": SCOPE_PAGES,
                     "pages": parse_pages_expression(self.scope_pages_var.get(), self.total_pages)}
        elif stype == SCOPE_SINGLE:
            scope = {"type": SCOPE_SINGLE, "page": active_r["page_num"]}
        else:
            scope = {"type": stype}

        active_r["page_scope"] = scope
        active_r["is_last_page"] = (stype == "last") or active_r.get("is_last_page", False)
        self._refresh_region_row(active_r)

    def _on_variant_change(self):
        active_r = self._get_active_region()
        if active_r:
            active_r["variant_group"] = (self.variant_var.get() or "").strip() or None
            self._refresh_region_row(active_r)

    def _sync_scope_controls(self, r):
        """Reflect a region's scope and variant group back into the controls."""
        scope = (r or {}).get("page_scope") or default_scope((r or {}).get("page_num"))
        stype = scope.get("type", SCOPE_SINGLE)
        self.scope_var.set(SCOPE_LABELS.get(stype, SCOPE_LABELS[SCOPE_SINGLE]))
        if stype == SCOPE_RANGE:
            self.scope_pages_var.set(f"{scope.get('from', 1)}-{scope.get('to', 1)}")
        elif stype == SCOPE_PAGES:
            self.scope_pages_var.set(", ".join(str(p) for p in scope.get("pages", [])))
        else:
            self.scope_pages_var.set("")
        try:
            self.scope_pages_entry.configure(
                state="normal" if stype in (SCOPE_RANGE, SCOPE_PAGES) else "disabled")
        except Exception:
            pass
        self.variant_var.set((r or {}).get("variant_group") or "")

    def _refresh_region_row(self, r):
        """Redraw one row of the regions table after an edit."""
        try:
            if self.region_tree.exists(str(r["id"])):
                x0, y0, x1, y1 = r["roi_rect"]
                lbl = f"  ↳ {r['label']}" if r.get("parent_id") is not None else r["label"]
                self.region_tree.item(str(r["id"]), values=(
                    lbl, self._page_cell(r), self._get_match_type_string(r),
                    f"[{x0:.1f}, {y0:.1f}, {x1:.1f}, {y1:.1f}]"))
        except Exception:
            pass

    def _page_cell(self, r):
        """The Page column shows the scope, not a bare page number."""
        txt = describe_scope(r.get("page_scope") or default_scope(r.get("page_num")))
        grp = r.get("variant_group")
        return f"{txt}  [{grp}]" if grp else txt

    def refresh_template_list(self, select=None):
        names = templates_store.list_templates()
        self.template_menu.configure(values=names or ["(none)"])
        if select and select in names:
            self.template_var.set(select)
        elif names and self.template_var.get() not in names:
            self.template_var.set(names[0])
        elif not names:
            self.template_var.set("(none)")
        return names

    def apply_template(self, name_or_data, announce=True):
        """Replace the current regions with those of a stored template."""
        data = (name_or_data if isinstance(name_or_data, dict)
                else templates_store.load_template(name_or_data))
        if not data:
            if announce:
                messagebox.showerror("Template Not Found",
                                     f"Could not read template: {name_or_data}")
            return False
        self.regions = []
        for src in data.get("regions", []):
            r = dict(src)
            r.setdefault("eng_text", "")
            r["is_custom_label"] = True
            r["color"] = REGION_COLORS[len(self.regions) % len(REGION_COLORS)]
            self.regions.append(r)
        # The margins are as much a part of the stylesheet as the regions are;
        # loading one without the other would silently change what the run
        # extracts. Older templates have none and keep the defaults.
        self.set_margins(data.get("margins"), redraw=False)
        self._reindex_and_renumber_regions()
        self.active_region_id = self.regions[0]["id"] if self.regions else None
        self.check_results = []
        try:
            for row in self.results_tree.get_children():
                self.results_tree.delete(row)
        except Exception:
            pass
        self._load_and_render_page()
        self.template_var.set(data.get("name", ""))
        print(f"[Template] Loaded '{data.get('name')}' with {len(self.regions)} region(s), "
              f"margins {page_margins.describe(self.margins)}")
        return True

    def _on_load_template(self):
        name = self.template_var.get().strip()
        if not name or name == "(none)":
            messagebox.showinfo("No Template", "There are no saved templates yet.\n\n"
                                "Mark the regions you need, then use 'Save As...'.")
            return
        if self.regions and not messagebox.askyesno(
                "Replace Regions?",
                f"Loading '{name}' replaces the {len(self.regions)} region(s) "
                f"currently defined.\n\nContinue?"):
            return
        self.apply_template(name)

    def _on_save_template(self):
        if not self.regions and page_margins.is_default(self.margins):
            messagebox.showinfo(
                "Nothing to Save",
                "Mark at least one region, or set the page margins for this "
                "stylesheet, before saving a template.")
            return
        suggested = self.template_var.get() if self.template_var.get() != "(none)" else ""
        name = simpledialog.askstring(
            "Save Stylesheet Template",
            "Template name (e.g. 10_stylesheet_template):",
            initialvalue=suggested, parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        if name in templates_store.list_templates() and not messagebox.askyesno(
                "Overwrite?", f"A template named '{name}' already exists.\n\nOverwrite it?"):
            return
        margins = self.get_margins()
        path = templates_store.save_template(
            name, self.regions, source_pdf=self.eng_pdf_path, margins=margins,
            page_size=(self.page_width_pt, self.page_height_pt))
        if path:
            self.refresh_template_list(select=name)
            if callable(self._on_templates_changed):
                try:
                    self._on_templates_changed(name)
                except Exception:
                    pass
            messagebox.showinfo(
                "Template Saved",
                f"Saved {len(self.regions)} region(s) as '{name}'.\n\n"
                f"Ignored margins: {page_margins.describe(margins)}\n\n{path}")
        else:
            messagebox.showerror("Save Failed",
                                 "The template could not be written. See the log for details.")

    def _on_delete_template(self):
        name = self.template_var.get().strip()
        if not name or name == "(none)":
            return
        if not messagebox.askyesno(
                "Delete Template?",
                f"Permanently delete the template '{name}'?\n\n"
                "The regions currently on screen are not affected."):
            return
        if templates_store.delete_template(name):
            self.refresh_template_list()
            if callable(self._on_templates_changed):
                try:
                    self._on_templates_changed(None)
                except Exception:
                    pass
        else:
            messagebox.showerror("Delete Failed", f"Could not delete '{name}'.")

    def _on_scope_only_toggle(self):
        """
        Mark a region as a container only.

        A region drawn purely to scope its sub-regions should not be scored
        itself — otherwise a block full of translated prose reports CHECK and
        drags down the pass rate while telling you nothing. Scope-only regions
        are skipped by the batch run but still act as the search scope for any
        sub-regions nested inside them.
        """
        active_r = self._get_active_region()
        if active_r:
            is_checked = self.scope_only_var.get()
            active_r["scope_only"] = is_checked
            if is_checked:
                active_r["exact_match"] = False
                active_r["dont_compare_text"] = False
                self.exact_match_var.set(False)
                self.dont_compare_text_var.set(False)

            if self.region_tree.exists(str(active_r["id"])):
                x0, y0, x1, y1 = active_r["roi_rect"]
                type_str = self._get_match_type_string(active_r)
                display_lbl = f"  \u21b3 {active_r['label']}" if active_r.get("parent_id") is not None else active_r["label"]
                self.region_tree.item(str(active_r["id"]), values=(display_lbl, self._page_cell(active_r), type_str, f"[{x0:.1f}, {y0:.1f}, {x1:.1f}, {y1:.1f}]"))
            self._draw_all_rois_on_canvas()

    def _on_exact_match_toggle(self):
        active_r = self._get_active_region()
        if active_r:
            is_checked = self.exact_match_var.get()
            active_r["exact_match"] = is_checked
            if is_checked:
                active_r["dont_compare_text"] = False
                active_r["scope_only"] = False
                self.dont_compare_text_var.set(False)
                self.scope_only_var.set(False)

            if self.region_tree.exists(str(active_r["id"])):
                x0, y0, x1, y1 = active_r["roi_rect"]
                type_str = self._get_match_type_string(active_r)
                display_lbl = f"  ↳ {active_r['label']}" if active_r.get("parent_id") is not None else active_r["label"]
                self.region_tree.item(str(active_r["id"]), values=(display_lbl, self._page_cell(active_r), type_str, f"[{x0:.1f}, {y0:.1f}, {x1:.1f}, {y1:.1f}]"))
            self._draw_all_rois_on_canvas()

    def _on_dont_compare_text_toggle(self):
        active_r = self._get_active_region()
        if active_r:
            is_checked = self.dont_compare_text_var.get()
            active_r["dont_compare_text"] = is_checked
            if is_checked:
                active_r["exact_match"] = False
                active_r["scope_only"] = False
                self.exact_match_var.set(False)
                self.scope_only_var.set(False)

            if self.region_tree.exists(str(active_r["id"])):
                x0, y0, x1, y1 = active_r["roi_rect"]
                type_str = self._get_match_type_string(active_r)
                display_lbl = f"  ↳ {active_r['label']}" if active_r.get("parent_id") is not None else active_r["label"]
                self.region_tree.item(str(active_r["id"]), values=(display_lbl, self._page_cell(active_r), type_str, f"[{x0:.1f}, {y0:.1f}, {x1:.1f}, {y1:.1f}]"))
            self._draw_all_rois_on_canvas()

    # ──────────────────────────────────────────────────────────
    # Page Navigation & Zoom
    # ──────────────────────────────────────────────────────────
    def _goto_first_page(self):
        self.current_page = 1
        self._sync_spinbox()
        self._load_and_render_page()

    def _goto_prev_page(self):
        if self.current_page > 1:
            self.current_page -= 1
            self._sync_spinbox()
            self._load_and_render_page()

    def _goto_next_page(self):
        if self.current_page < self.total_pages:
            self.current_page += 1
            self._sync_spinbox()
            self._load_and_render_page()

    def _goto_last_page(self):
        self.current_page = self.total_pages
        self._sync_spinbox()
        self._load_and_render_page()

    def _on_spinbox_change(self):
        try:
            val = int(self.page_spin.get())
            if 1 <= val <= self.total_pages:
                self.current_page = val
                self._load_and_render_page()
        except ValueError:
            pass

    def _sync_spinbox(self):
        self.page_spin.delete(0, "end")
        self.page_spin.insert(0, str(self.current_page))

    def _zoom_in(self):
        self.zoom = min(2.5, self.zoom + 0.25)
        self.zoom_lbl.configure(text=f"{int(self.zoom * 100)}%")
        self._load_and_render_page()

    def _zoom_out(self):
        self.zoom = max(0.5, self.zoom - 0.25)
        self.zoom_lbl.configure(text=f"{int(self.zoom * 100)}%")
        self._load_and_render_page()

    # ──────────────────────────────────────────────────────────
    # Rendering & Multi-ROI Canvas Drawing
    # ──────────────────────────────────────────────────────────
    def _load_and_render_page(self):
        # Built before the user has chosen anything: show guidance, not an error.
        if not self.eng_pdf_path or self.total_pages == 0:
            try:
                self.canvas.delete("all")
                self.canvas.create_text(
                    24, 24, anchor="nw",
                    text=("No master PDF loaded.\n\n"
                          "Select an English Master PDF on the Inspection tab,\n"
                          "then click 'Region Inspector' to load it here."),
                    fill=NEUTRAL_DARK_GR, font=theme.get_font(11)
                )
                self._update_margin_hint()
            except Exception:
                pass
            return

        try:
            img, pw, ph = render_pdf_page_image(self.eng_pdf_path, self.current_page, zoom=self.zoom)
            self.pil_image = img
            self.page_width_pt = pw
            self.page_height_pt = ph
            self.tk_image = ImageTk.PhotoImage(img, master=self)

            self.canvas.delete("all")
            self.canvas.create_image(10, 10, anchor="nw", image=self.tk_image, tags="page_img")
            self.canvas.config(scrollregion=(0, 0, img.width + 30, img.height + 30))

            with fitz.open(self.eng_pdf_path) as doc_eng:
                for r in self.regions:
                    if r["page_num"] == self.current_page:
                        r["eng_text"] = extract_roi_text(doc_eng, self.current_page, r["roi_rect"])

            self._refresh_margin_overlay()
            self._draw_all_rois_on_canvas()
            self._sync_regions_table()
        except Exception as e:
            messagebox.showerror("Render Error", f"Failed to render page {self.current_page}:\n{e}")

    def _draw_all_rois_on_canvas(self):
        self.canvas.delete("roi_box")
        self.canvas.delete("roi_badge")

        for r in self.regions:
            if r["page_num"] != self.current_page:
                continue

            x0, y0, x1, y1 = r["roi_rect"]
            cx0 = 10 + x0 * self.zoom
            cy0 = 10 + y0 * self.zoom
            cx1 = 10 + x1 * self.zoom
            cy1 = 10 + y1 * self.zoom

            is_active = (r["id"] == self.active_region_id)
            is_sub = (r.get("parent_id") is not None)

            box_width = 3 if is_active else 2
            dash_pattern = () if (is_active and not is_sub) else ((4, 2) if is_sub else ())
            outline_color = DYNAMIC_GREEN if is_active else r["color"]

            self.canvas.create_rectangle(
                cx0, cy0, cx1, cy1,
                outline=outline_color,
                width=box_width,
                dash=dash_pattern,
                tags=("roi_box", f"roi_{r['id']}")
            )

            # Badge tag
            mode_tag = " [VISUAL]" if r.get("dont_compare_text", False) else (" [EXACT]" if r.get("exact_match", False) else "")
            badge_text = f" {r['label']}{mode_tag} "
            badge_y = max(12, cy0 - 10)
            self.canvas.create_rectangle(
                cx0, badge_y - 8, cx0 + len(badge_text) * 7 + 4, badge_y + 8,
                fill=outline_color,
                outline="",
                tags=("roi_badge", f"badge_{r['id']}")
            )
            self.canvas.create_text(
                cx0 + 4, badge_y,
                text=badge_text,
                anchor="w",
                font=theme.get_font(8, "bold"),
                fill=NEUTRAL_WHITE if is_active else "#FFFFFF",
                tags=("roi_badge", f"badge_txt_{r['id']}")
            )

    def _on_canvas_press(self, event):
        canvas_x = self.canvas.canvasx(event.x)
        canvas_y = self.canvas.canvasy(event.y)

        # A press that lands on a margin guide moves that guide instead of
        # starting a new region, so the same drag gesture serves both without
        # a mode switch the user has to remember.
        side = self._guide_at(canvas_x, canvas_y)
        if side:
            self._drag_guide = side
            self.selection_start = None
            return

        self.selection_start = (canvas_x, canvas_y)

    def _on_canvas_drag(self, event):
        if self._drag_guide:
            self._drag_guide_to(self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))
            return
        if not self.selection_start:
            return
        cur_x = self.canvas.canvasx(event.x)
        cur_y = self.canvas.canvasy(event.y)

        x0 = min(self.selection_start[0], cur_x)
        y0 = min(self.selection_start[1], cur_y)
        x1 = max(self.selection_start[0], cur_x)
        y1 = max(self.selection_start[1], cur_y)

        if self.temp_rect_id:
            self.canvas.coords(self.temp_rect_id, x0, y0, x1, y1)
        else:
            self.temp_rect_id = self.canvas.create_rectangle(
                x0, y0, x1, y1,
                outline=DYNAMIC_GREEN,
                width=2,
                dash=(4, 2),
                tags="temp_rect"
            )

    def _on_canvas_release(self, event):
        if self._drag_guide:
            self._drag_guide_to(self.canvas.canvasx(event.x), self.canvas.canvasy(event.y))
            self._drag_guide = None
            self.status_lbl.configure(
                text=f"Ignored margins • {page_margins.describe(self.get_margins())} "
                     f"• saved with the template")
            self._schedule_margin_save()
            return
        if not self.selection_start:
            return
        cur_x = self.canvas.canvasx(event.x)
        cur_y = self.canvas.canvasy(event.y)

        cx0 = min(self.selection_start[0], cur_x)
        cy0 = min(self.selection_start[1], cur_y)
        cx1 = max(self.selection_start[0], cur_x)
        cy1 = max(self.selection_start[1], cur_y)
        self.selection_start = None

        if self.temp_rect_id:
            self.canvas.delete(self.temp_rect_id)
            self.temp_rect_id = None

        if (cx1 - cx0) < 12 or (cy1 - cy0) < 12:
            return

        x0 = max(0.0, (cx0 - 10) / self.zoom)
        y0 = max(0.0, (cy0 - 10) / self.zoom)
        x1 = min(self.page_width_pt, (cx1 - 10) / self.zoom)
        y1 = min(self.page_height_pt, (cy1 - 10) / self.zoom)
        new_roi = (round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1))

        self._add_new_region(
            roi_rect=new_roi,
            page_num=self.current_page
        )

        self._load_and_render_page()

    # ──────────────────────────────────────────────────────────
    # Check All Regions Across Translated PDFs & Generate Crops
    # ──────────────────────────────────────────────────────────
    def _start_batch_check(self):
        if self.is_checking:
            return

        if not self.eng_pdf_path or not os.path.exists(self.eng_pdf_path):
            messagebox.showerror(
                "No Master PDF",
                "No English Master PDF is loaded.\n\n"
                "Select one on the Inspection tab, then click 'Region Inspector'."
            )
            return
        if not self.tr_target_path or not os.path.exists(self.tr_target_path):
            messagebox.showerror(
                "No Translated Target",
                "No translated PDF folder is loaded.\n\n"
                "Select one on the Inspection tab, then click 'Region Inspector'."
            )
            return
        if not self.regions:
            messagebox.showinfo("Notice", "Please draw or add at least one region to check.")
            return

        try:
            tolerance = float(self.tol_spin.get())
            x_tolerance = float(self.xtol_spin.get())
            threshold = float(self.thresh_spin.get())
        except ValueError:
            messagebox.showerror("Invalid Input",
                                 "Please enter valid numeric values for the tolerances and threshold.")
            return

        self.is_checking = True
        self.run_btn.configure(state="disabled", text="⏳ Checking & Cropping Regions...")
        self.status_lbl.configure(text="Processing translations and generating comparison crops...", text_color=RADIANT_ORANGE)
        self.progress_bar.set(0.0)

        for item in self.results_tree.get_children():
            self.results_tree.delete(item)
        self.check_results = []
        self.tr_text_box.delete("1.0", "end")
        self.crop_thumb_lbl.config(image="", text="[Processing crops...]")

        t = threading.Thread(
            target=self._run_batch_check_thread,
            args=(tolerance, x_tolerance, threshold),
            daemon=True
        )
        t.start()

    def _run_batch_check_thread(self, tolerance, x_tolerance, threshold):
        def progress_cb(current, total, reg_lbl, filename):
            pct = current / total
            self.after(0, lambda: self.progress_bar.set(pct))
            self.after(0, lambda: self.status_lbl.configure(text=f"Cropping & Checking [{reg_lbl}]: {filename} ({current}/{total})"))

        results = run_batch_multiple_regions_check(
            eng_pdf_path=self.eng_pdf_path,
            tr_target=self.tr_target_path,
            regions=self.regions,
            y_tolerance=tolerance,
            x_tolerance=x_tolerance,
            similarity_threshold=threshold,
            output_crops_dir=self.output_crops_dir,
            progress_callback=progress_cb
        )

        self.after(0, self._on_batch_check_complete, results)

    def _on_batch_check_complete(self, results):
        self.is_checking = False
        self.run_btn.configure(state="normal", text="\u25b6  Check All Selections Across Translated PDFs")
        self.progress_bar.set(1.0)
        self.check_results = results

        if callable(self._on_results):
            try:
                self._on_results(results)
            except Exception as e:
                print(f"[WARN] results callback failed: {e}")

        pass_count = 0
        for r in results:
            stat = r["status"]
            tag = "pass" if "PASS" in stat else ("check" if "CHECK" in stat else "fail")
            if "PASS" in stat:
                pass_count += 1

            shift_str = f"{r['shift_y']:+.1f}" if r["shift_y"] != 0.0 else "0.0"
            # For a scoped exact match a percentage is meaningless — the useful
            # number is how many times the needle was found.
            if r.get("scoped"):
                score_str = f"found {r.get('needle_count', 0)}x"
            else:
                score_str = f"{r['similarity']:.1f}%"
            self.results_tree.insert(
                "",
                "end",
                values=(
                    r["region_label"],
                    r["tr_name"],
                    f"Pg {r.get('target_page', '-')}",
                    score_str,
                    shift_str,
                    stat
                ),
                tags=(tag,)
            )

        total = len(results)
        n_scope = sum(1 for x in self.regions if x.get("scope_only", False))
        scope_note = f"   ({n_scope} scope-only region{'s' if n_scope != 1 else ''} not compared)" if n_scope else ""
        self.status_lbl.configure(
            text=(f"\u25cf Multi-Region Verification & Crops Complete: "
                  f"{pass_count} / {total} Passed ({round(pass_count/max(1,total)*100, 1)}%){scope_note}"),
            text_color=DYNAMIC_GREEN if pass_count == total else RADIANT_ORANGE
        )

    def _on_results_tree_select(self, event):
        selected = self.results_tree.selection()
        if not selected:
            return
        item = selected[0]
        idx = self.results_tree.index(item)
        if 0 <= idx < len(self.check_results):
            r = self.check_results[idx]
            tr_content = r.get("tr_text", "")
            self.tr_text_box.delete("1.0", "end")
            self.tr_text_box.insert("1.0", tr_content if tr_content else "[Empty / Non-Text Graphics]")

            comp_img_path = r.get("comparison_img_path", "")
            if comp_img_path and os.path.exists(comp_img_path):
                try:
                    c_img = Image.open(comp_img_path)
                    c_img.thumbnail((200, 70))
                    self.preview_tk_img = ImageTk.PhotoImage(c_img, master=self)
                    self.crop_thumb_lbl.config(image=self.preview_tk_img, text="")
                except Exception:
                    self.crop_thumb_lbl.config(image="", text="[Click to Open Crop Image]")
            else:
                self.crop_thumb_lbl.config(image="", text="[No Crop Image Available]")

    def _open_selected_crop_image(self):
        selected = self.results_tree.selection()
        if not selected:
            return
        item = selected[0]
        idx = self.results_tree.index(item)
        if 0 <= idx < len(self.check_results):
            r = self.check_results[idx]
            comp_path = r.get("comparison_img_path", "")
            if comp_path and os.path.exists(comp_path):
                os.startfile(comp_path)
            else:
                self._open_crops_folder()

    def _open_crops_folder(self):
        if self.output_crops_dir and os.path.exists(self.output_crops_dir):
            os.startfile(self.output_crops_dir)
        else:
            messagebox.showinfo("Folder Notice", f"Crops output folder does not exist yet:\n{self.output_crops_dir}\n\nRun the check first to generate crops.")


# ==============================================================================
# STANDALONE CLI LAUNCHER
# ==============================================================================

class RegionInspectorDialog(ctk.CTkToplevel):
    """
    Standalone window hosting RegionInspectorFrame.

    Used only when the inspector is opened outside the main application — the
    normal path embeds the frame as a tab instead.
    """
    def __init__(self, parent, eng_pdf_path: str = "", tr_target_path: str = "",
                 output_dir: str = None, on_results=None):
        super().__init__(parent)
        self.title("Xylem SpotCheck - Custom Region Inspector & Layout Comparison")
        self.geometry("1280x880")
        self.minsize(1040, 680)
        self.resizable(True, True)
        self.configure(fg_color=UI_BG_CANVAS)

        try:
            self.lift()
            self.focus_force()
        except Exception:
            pass

        self.inspector = RegionInspectorFrame(self, eng_pdf_path, tr_target_path,
                                              output_dir, on_results=on_results)
        self.inspector.pack(fill="both", expand=True)


def open_region_inspector(parent=None, eng_pdf_path=None, tr_target_path=None, output_dir=None):
    """
    Open the Region Inspector in its own window.

    The main application does NOT use this — it embeds RegionInspectorFrame as a
    tab. This remains for standalone development of the ROI tool:

        python -m gui.region_dialog
    """
    eng_path = eng_pdf_path or ""
    tr_path = tr_target_path or ""

    if parent is None:
        root = ctk.CTk()
        root.withdraw()
        dlg = RegionInspectorDialog(root, eng_path, tr_path, output_dir=output_dir)
        dlg.protocol("WM_DELETE_WINDOW", root.destroy)
        root.mainloop()
        return dlg

    dlg = RegionInspectorDialog(parent, eng_path, tr_path, output_dir=output_dir)
    try:
        dlg.lift()
        dlg.focus_force()
    except Exception:
        pass
    return dlg


if __name__ == "__main__":
    open_region_inspector()
