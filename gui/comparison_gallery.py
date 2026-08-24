"""
gui/comparison_gallery.py

In-app viewer for the side-by-side comparison images.

The engine writes every comparison to the output folder, which meant the only
way to look at a result was to leave the application and dig through nested
directories. This tab shows the same images inline, split by verdict, each one
labelled with the master and translated file and the page each side came from.

Design note — why master/detail rather than a scrolling wall of cards:
a full run produces one comparison per crop per language (11 x 31 = 341 on the
Start 350 manual, and more on bigger documents). Rendering a card per result
built over 10,000 CustomTkinter widgets, took 15s, and pushed Tk past the point
where it paints reliably: CTk widgets silently stopped rendering across the
whole application while native ttk widgets kept working. So the list is a
single native ttk.Treeview - which handles thousands of rows cheaply - and
exactly one comparison image is decoded at a time, for the selected row. Widget
count is now constant no matter how large the run.

Two sources feed it:
  - Region checks : results from the Region Inspector's batch run
  - Images        : per-crop results from a full pipeline inspection

A result that needs a closer look opens both whole pages side by side through
gui/page_diff_view.py, which is where "what actually differs" gets answered.
"""

import os
import re
import subprocess
import sys

from PIL import Image, ImageTk
import customtkinter as ctk
import tkinter as tk
from tkinter import ttk, messagebox

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
    NEUTRAL_WHITE,
    NEUTRAL_DARK_GR,
)

SOURCE_REGIONS = "Region Checks"
SOURCE_CROPS = "Images"

FILTER_ALL = "All"
FILTER_PASS = "Passed"
FILTER_FAIL = "Needs Review"

_TREE_STYLE = "XylemGallery.Treeview"

# Deleting from disk is irreversible, so "Clear Images" refuses to touch anything
# that is not inside the engine's own comparison output tree. Every comparison
# the engine writes lands under a directory with this name, so requiring it as a
# path component means a malformed or unexpected path is skipped rather than
# deleted. Source PDFs, the Excel report and the master crops are all outside it.
COMPARISON_DIR_NAME = "Cropped_Comparison"
DELETABLE_SUFFIXES = (".png",)

# Upper bound for the decoded preview; the full image opens in the OS viewer.
PREVIEW_MAX_W = 900
PREVIEW_MAX_H = 420


def _open_externally(path):
    """Open a file or folder with the OS default handler, on any platform."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception as e:
        print(f"[Gallery] Could not open {path}: {e}")
        return False


def _is_pass(status):
    return "PASS" in (status or "").upper()


_LANG_TOKEN = re.compile(r"(?:^|[_\-.])([a-z]{2}-[A-Z]{2})(?:$|[_\-.])")


def short_target(name):
    """
    A compact label for the translated document.

    With eleven languages the list has to say which file each row belongs to, but
    the full name is far too long for a column. Xylem's naming carries an ISO
    tag (882543_5.0_de-DE_2026-04_IOM.Start350.pdf), so prefer that; fall back to
    the trimmed stem for anything that does not follow the convention.
    """
    if not name:
        return "-"
    m = _LANG_TOKEN.search(name)
    if m:
        return m.group(1)
    stem = os.path.splitext(os.path.basename(name))[0]
    return stem if len(stem) <= 18 else stem[:17] + "\u2026"


def cards_from_region_results(results):
    """Normalise Region Inspector batch results into gallery rows."""
    rows = []
    for r in results or []:
        rows.append({
            "source": SOURCE_REGIONS,
            "title": r.get("region_label", "Region"),
            "eng_name": r.get("eng_name", ""),
            "eng_page": r.get("eng_page", "-"),
            "tr_name": r.get("tr_name", ""),
            "tr_page": r.get("target_page", "-"),
            "status": r.get("status", ""),
            "passed": bool(r.get("is_match")),
            "score": (f"found {r.get('needle_count')}x" if r.get("scoped")
                      else f"{r.get('similarity', 0):.1f}%"),
            "detail": (f"needle {r.get('needle')!r}" if r.get("scoped")
                       else r.get("match_desc", "")),
            "shift": r.get("shift_y", 0.0) or 0.0,
            "image": r.get("comparison_img_path", ""),
            # Where on the master page this check was looking, so the page view
            # can point straight at it.
            "roi_rect": r.get("roi_rect"),
        })
    return rows


def cards_from_crop_details(details):
    """Normalise pipeline per-crop image results into gallery rows."""
    rows = []
    for d in details or []:
        # -1 is the search saying it never found the graphic. Printing it as a
        # page number puts "page -1" in front of a reviewer; a dash reads as
        # what it is, and the Result column carries the word NOT FOUND.
        tr_page = d.get("trans_page", "-")
        if tr_page in (-1, "-1"):
            tr_page = "—"
        rows.append({
            "source": SOURCE_CROPS,
            "title": d.get("crop_name", "crop"),
            "eng_name": d.get("english_pdf", ""),
            "eng_page": d.get("eng_page", "-"),
            "tr_name": d.get("translated_pdf", ""),
            "tr_page": tr_page,
            "status": d.get("status", ""),
            "passed": _is_pass(d.get("status", "")),
            "score": f"{d.get('similarity', 0):.1f}%",
            "detail": d.get("shift_info", ""),
            "shift": 0.0,
            "image": d.get("match_img", ""),
        })
    return rows


class ComparisonGalleryFrame(ctk.CTkFrame):
    """Verdict-segregated list of comparisons with a single-image preview."""

    def __init__(self, parent, is_active=None, resolve_paths=None, get_margins=None):
        super().__init__(parent, fg_color=UI_BG_CANVAS)

        # The rows carry file NAMES, because that is all the engine reports. To
        # open the documents themselves the host has to say where they live.
        self._resolve_paths = resolve_paths
        self._get_margins = get_margins

        # CTkTabview stacks its tabs rather than unmapping them, so winfo_ismapped()
        # is True even for the hidden ones and cannot tell us whether this tab is on
        # screen. The host supplies a predicate instead; without one we assume active.
        self._is_active = is_active

        self._all_rows = []
        self._visible_rows = []
        self._preview_photo = None      # exactly one image alive at a time
        self._source = SOURCE_REGIONS
        self._filter = FILTER_ALL
        self._selected = None

        theme.apply_treeview_style(_TREE_STYLE, row_height=22)
        self._build_ui()
        self._bind_keys()
        self._refresh_list()

    @staticmethod
    def _screen_width(widget=None):
        try:
            return (widget or tk._default_root).winfo_screenwidth()
        except Exception:
            return 1366

    def _list_width(self):
        """A share of the display, so the list is not 580px on every screen."""
        return max(380, min(700, int(self._screen_width(self) * 0.36)))

    def _on_resize(self, event=None):
        """Keep the list pane at about a third of whatever width we now have."""
        try:
            w = self.winfo_width()
            if w > 400:
                self._list_card.configure(width=max(360, min(720, int(w * 0.38))))
        except Exception:
            pass

    def _f(self, size=11, weight="normal"):
        return ctk.CTkFont(family=theme.resolve_font_family(), size=size, weight=weight)

    # ──────────────────────────────────────────────────────────
    # Layout
    # ──────────────────────────────────────────────────────────
    def _build_ui(self):
        bar = ctk.CTkFrame(self, fg_color=DEPENDABLE_BLUE, corner_radius=8)
        bar.pack(fill="x", padx=14, pady=(12, 8))
        self.summary_lbl = ctk.CTkLabel(
            bar, text="Nothing to review yet", anchor="w", justify="left",
            font=self._f(12, "bold"), text_color=NEUTRAL_WHITE)
        self.summary_lbl.pack(side="left", padx=16, pady=10)
        ctk.CTkButton(bar, text="Open Output Folder", width=140, height=26,
                      fg_color=XYLEM_BLUE, text_color=NEUTRAL_WHITE,
                      font=self._f(10, "bold"), command=self._open_folder
                      ).pack(side="right", padx=(6, 12), pady=8)
        self.clear_btn = ctk.CTkButton(
            bar, text="Clear Images", width=120, height=26,
            fg_color=RADIANT_ORANGE, hover_color="#C85800",
            text_color=theme.TEXT_ON_ORANGE, font=self._f(10, "bold"),
            command=self._clear_images)
        self.clear_btn.pack(side="right", padx=6, pady=8)

        controls = ctk.CTkFrame(self, fg_color=UI_CARD_WELL, corner_radius=8)
        controls.pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkLabel(controls, text="Source:", font=self._f(10, "bold"),
                     text_color=DEPENDABLE_BLUE).pack(side="left", padx=(12, 4), pady=8)
        self.source_sel = ctk.CTkSegmentedButton(
            controls, values=[SOURCE_REGIONS, SOURCE_CROPS], font=self._f(10),
            command=self._on_source_change, **theme.segmented_button_colors())
        self.source_sel.set(SOURCE_REGIONS)
        self.source_sel.pack(side="left", padx=4, pady=8)

        ctk.CTkLabel(controls, text="Show:", font=self._f(10, "bold"),
                     text_color=DEPENDABLE_BLUE).pack(side="left", padx=(18, 4), pady=8)
        self.filter_sel = ctk.CTkSegmentedButton(
            controls, values=[FILTER_ALL, FILTER_PASS, FILTER_FAIL], font=self._f(10),
            command=self._on_filter_change, **theme.segmented_button_colors())
        self.filter_sel.set(FILTER_ALL)
        self.filter_sel.pack(side="left", padx=4, pady=8)

        self.count_lbl = ctk.CTkLabel(controls, text="", font=self._f(10),
                                      text_color=NEUTRAL_DARK_GR)
        self.count_lbl.pack(side="right", padx=14, pady=8)

        split = ctk.CTkFrame(self, fg_color="transparent")
        split.pack(fill="both", expand=True, padx=14, pady=(0, 12))

        # ---- left: the result list (native widget, cheap at any row count) ----
        list_card = ctk.CTkFrame(split, fg_color=UI_CARD_BG, corner_radius=8,
                                 border_width=1, border_color=UI_BORDER,
                                 width=self._list_width())
        list_card.pack(side="left", fill="both", padx=(0, 6))
        list_card.pack_propagate(False)
        self._list_card = list_card
        self.bind("<Configure>", self._on_resize, add="+")

        ctk.CTkLabel(list_card, text="Results", font=self._f(11, "bold"),
                     text_color=DEPENDABLE_BLUE).pack(anchor="w", padx=10, pady=(8, 2))

        holder = tk.Frame(list_card, bg=UI_CARD_BG)
        holder.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        cols = ("verdict", "item", "target", "mpage", "tpage", "score")
        self.tree = ttk.Treeview(holder, columns=cols, show="headings", style=_TREE_STYLE)
        for cid, text, w, anchor in (
            ("verdict", "Verdict", 66, "center"),
            ("item", "Item", 168, "w"),
            ("target", "Translated", 96, "w"),
            ("mpage", "Master Pg", 80, "center"),
            ("tpage", "Trans Pg", 76, "center"),
            ("score", "Score", 72, "center"),
        ):
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=w, anchor=anchor, stretch=(cid == "item"))
        sb = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("pass", foreground="#274E13")
        self.tree.tag_configure("fail", foreground="#8A3B00")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # ---- right: one comparison at a time ----
        det = ctk.CTkFrame(split, fg_color=UI_CARD_BG, corner_radius=8,
                           border_width=1, border_color=UI_BORDER)
        det.pack(side="left", fill="both", expand=True, padx=(6, 0))

        head = ctk.CTkFrame(det, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(10, 2))
        self.det_title = ctk.CTkLabel(head, text="Select a result",
                                      font=self._f(13, "bold"), text_color=DEPENDABLE_BLUE)
        self.det_title.pack(side="left")
        self.det_status = ctk.CTkLabel(head, text="", font=self._f(11, "bold"))
        self.det_status.pack(side="right", padx=(8, 0))

        # The crop comparison answers "does this one graphic match". When it
        # says no, the next question is always "what else is wrong on that
        # page" - which needs both pages, whole, side by side.
        self.compare_btn = ctk.CTkButton(
            head, text="⇔  Compare Pages", width=132, height=26,
            fg_color=DYNAMIC_GREEN, hover_color="#4FB003",
            text_color=DEPENDABLE_BLUE, font=self._f(10, "bold"),
            command=self._open_page_diff)
        self.compare_btn.pack(side="right", padx=(8, 6))

        nav = ctk.CTkFrame(head, fg_color="transparent")
        nav.pack(side="right", padx=(0, 14))
        ctk.CTkButton(nav, text="\u25b2", width=30, height=24, fg_color=UI_CARD_WELL,
                      text_color=DEPENDABLE_BLUE, font=self._f(10, "bold"),
                      command=lambda: self._nav(-1)).pack(side="left", padx=2)
        ctk.CTkButton(nav, text="\u25bc", width=30, height=24, fg_color=UI_CARD_WELL,
                      text_color=DEPENDABLE_BLUE, font=self._f(10, "bold"),
                      command=lambda: self._nav(1)).pack(side="left", padx=2)
        self.pos_lbl = ctk.CTkLabel(nav, text="", font=self._f(10),
                                    text_color=NEUTRAL_DARK_GR)
        self.pos_lbl.pack(side="left", padx=(8, 0))

        meta = ctk.CTkFrame(det, fg_color=UI_CARD_WELL, corner_radius=6)
        meta.pack(fill="x", padx=12, pady=(4, 6))
        lft = ctk.CTkFrame(meta, fg_color="transparent")
        lft.pack(side="left", fill="x", expand=True, padx=10, pady=6)
        ctk.CTkLabel(lft, text="MASTER", font=self._f(9, "bold"),
                     text_color=theme.TEXT_ON_LIGHT, anchor="w").pack(anchor="w")
        self.det_master = ctk.CTkLabel(lft, text="-", font=self._f(10),
                                       text_color=NEUTRAL_DARK_GR, anchor="w", justify="left")
        self.det_master.pack(anchor="w")
        rgt = ctk.CTkFrame(meta, fg_color="transparent")
        rgt.pack(side="left", fill="x", expand=True, padx=10, pady=6)
        ctk.CTkLabel(rgt, text="TRANSLATED", font=self._f(9, "bold"),
                     text_color=DEPENDABLE_BLUE, anchor="w").pack(anchor="w")
        self.det_trans = ctk.CTkLabel(rgt, text="-", font=self._f(10),
                                      text_color=NEUTRAL_DARK_GR, anchor="w", justify="left")
        self.det_trans.pack(anchor="w")

        self.det_detail = ctk.CTkLabel(det, text="", font=self._f(10),
                                       text_color=NEUTRAL_DARK_GR, anchor="w", justify="left")
        self.det_detail.pack(anchor="w", padx=14, pady=(0, 4))

        # A plain tk.Label, deliberately not a CTkLabel. CTkLabel.configure()
        # re-applies every option including the currently bound image, so if the
        # previous PhotoImage has already been released Tk raises
        #   TclError: image "pyimageN" doesn't exist
        # before the new image is ever attached. A tk.Label swaps the image in one
        # step, and the canonical `widget.image = photo` idiom keeps it alive.
        self.det_image = tk.Label(det, bd=0, bg=UI_CARD_BG, cursor="hand2")
        self.det_image.pack(anchor="nw", padx=14, pady=(2, 4))
        self.det_image.bind("<Button-1>", lambda _e: self._open_selected())

        self.det_hint = ctk.CTkLabel(
            det, text="Run a Region Inspector check, or a full inspection, to see comparisons here.",
            font=self._f(10), text_color=NEUTRAL_DARK_GR, anchor="w", justify="left")
        self.det_hint.pack(anchor="w", padx=14, pady=(0, 10))

    # ──────────────────────────────────────────────────────────
    # Clearing comparison images from disk
    # ──────────────────────────────────────────────────────────
    @staticmethod
    def _is_deletable(path):
        """
        True only for a file that is genuinely one of our comparison images.

        Three conditions, all required: it exists as a regular file, it is a PNG,
        and COMPARISON_DIR_NAME appears as a component of its resolved path. The
        last one is what makes this safe - nothing outside the engine's own
        comparison output can satisfy it.
        """
        try:
            real = os.path.realpath(path)
        except Exception:
            return False
        if not os.path.isfile(real):
            return False
        if os.path.islink(path):
            return False
        if not real.lower().endswith(DELETABLE_SUFFIXES):
            return False
        parts = os.path.normpath(real).split(os.sep)
        return COMPARISON_DIR_NAME in parts

    def _collect_image_paths(self):
        """Distinct, on-disk, deletable comparison images across both sources."""
        seen, ok, refused = set(), [], []
        for r in self._all_rows:
            path = r.get("image") or ""
            if not path:
                continue
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            (ok if self._is_deletable(path) else refused).append(real)
        return ok, [p for p in refused if os.path.exists(p)]

    @staticmethod
    def _comparison_roots(paths):
        """The <...>/Cropped_Comparison directories the given files live under."""
        roots = set()
        for p in paths:
            parts = os.path.normpath(p).split(os.sep)
            if COMPARISON_DIR_NAME in parts:
                idx = len(parts) - 1 - parts[::-1].index(COMPARISON_DIR_NAME)
                roots.add(os.sep.join(parts[:idx + 1]) or os.sep)
        return roots

    def _prune_empty_dirs(self, roots):
        """Remove directories left empty by the delete, never the root itself."""
        removed = 0
        for root in roots:
            if not os.path.isdir(root):
                continue
            for cur, dirs, files in os.walk(root, topdown=False):
                if os.path.normpath(cur) == os.path.normpath(root):
                    continue
                try:
                    if not os.listdir(cur):
                        os.rmdir(cur)
                        removed += 1
                except OSError:
                    pass
        return removed

    def _clear_images(self):
        """Delete the comparison images from disk, after an explicit confirmation."""
        deletable, refused = self._collect_image_paths()

        if not deletable:
            messagebox.showinfo(
                "Nothing to Clear",
                "No comparison images from this session were found on disk."
                + (f"\n\n{len(refused)} path(s) were skipped as outside the "
                   f"'{COMPARISON_DIR_NAME}' output folder." if refused else ""))
            return

        roots = self._comparison_roots(deletable)
        where = "\n".join(sorted(roots)) or "(unknown)"
        total_mb = sum(os.path.getsize(p) for p in deletable if os.path.exists(p)) / (1024 * 1024)

        if not messagebox.askyesno(
            "Delete Comparison Images?",
            f"Permanently delete {len(deletable)} comparison image(s) "
            f"({total_mb:.1f} MB) from disk?\n\n"
            f"Location:\n{where}\n\n"
            "Empty sub-folders are removed too, and the results list on this tab "
            "is emptied. Your PDFs and the Excel report are not touched.\n\n"
            "This cannot be undone.",
            icon="warning", default="no"
        ):
            return

        deleted, failed = 0, []
        for path in deletable:
            try:
                os.remove(path)
                deleted += 1
            except OSError as e:
                failed.append(f"{os.path.basename(path)}: {e}")

        pruned = self._prune_empty_dirs(roots)

        # Rows without images are just dead links, so reset the tab.
        self._all_rows = []
        self._selected = None
        self._refresh_list()

        print(f"[Gallery] Cleared {deleted} comparison image(s), removed {pruned} empty folder(s).")
        msg = f"Deleted {deleted} comparison image(s).\nRemoved {pruned} empty folder(s)."
        if refused:
            msg += f"\n\nSkipped {len(refused)} file(s) outside the '{COMPARISON_DIR_NAME}' folder."
        if failed:
            msg += "\n\nCould not delete:\n" + "\n".join(failed[:6])
            if len(failed) > 6:
                msg += f"\n...and {len(failed) - 6} more"
            messagebox.showwarning("Cleared With Errors", msg)
        else:
            messagebox.showinfo("Images Cleared", msg)

    # ──────────────────────────────────────────────────────────
    # Keyboard navigation
    # ──────────────────────────────────────────────────────────
    def _bind_keys(self):
        """
        Arrow keys step through results and swap the preview.

        Bound on the toplevel rather than the list, so they work wherever focus
        happens to be inside this tab. Three guards keep that from misbehaving:
        the handler stands down unless the host says this tab is on screen, it
        ignores keys aimed at a text field, and it stands aside when the list
        itself has focus (ttk.Treeview already moves its own selection, and
        handling it twice would skip every other row).
        """
        top = self.winfo_toplevel()
        for seq, delta in (("<Up>", -1), ("<Down>", 1),
                           ("<Prior>", -10), ("<Next>", 10)):
            top.bind(seq, lambda e, d=delta: self._on_key(d), add="+")
        top.bind("<Home>", lambda e: self._on_key(None, first=True), add="+")
        top.bind("<End>", lambda e: self._on_key(None, last=True), add="+")

    def _on_key(self, delta, first=False, last=False):
        if callable(self._is_active) and not self._is_active():
            return None
        focused = None
        try:
            focused = self.focus_get()
        except Exception:
            pass
        # Never steal keys from a text field, and let the list handle its own.
        if focused is self.tree:
            return None
        if focused is not None and focused.winfo_class() in ("Entry", "Text", "TEntry", "Spinbox"):
            return None
        if not self._visible_rows:
            return None
        if first:
            self._select_index(0)
        elif last:
            self._select_index(len(self._visible_rows) - 1)
        else:
            self._nav(delta)
        return "break"

    def _nav(self, delta):
        """Step the selection by `delta` rows, clamped to the list."""
        if not self._visible_rows:
            return
        cur = self._current_index()
        self._select_index(max(0, min(len(self._visible_rows) - 1, cur + delta)))

    def _current_index(self):
        sel = self.tree.selection()
        if sel:
            try:
                return int(sel[0])
            except ValueError:
                pass
        return 0

    def _select_index(self, idx):
        iid = str(idx)
        if not self.tree.exists(iid):
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.tree.see(iid)
        self._show(self._visible_rows[idx])

    def _update_position(self):
        if self._visible_rows:
            self.pos_lbl.configure(text=f"{self._current_index() + 1} of {len(self._visible_rows)}")
        else:
            self.pos_lbl.configure(text="")

    # ──────────────────────────────────────────────────────────
    # Data
    # ──────────────────────────────────────────────────────────
    def load_region_results(self, results):
        self._replace(SOURCE_REGIONS, cards_from_region_results(results))
        self._source = SOURCE_REGIONS
        self.source_sel.set(SOURCE_REGIONS)
        self._refresh_list()

    def load_crop_details(self, details):
        self._replace(SOURCE_CROPS, cards_from_crop_details(details))
        self._source = SOURCE_CROPS
        self.source_sel.set(SOURCE_CROPS)
        self._refresh_list()

    def _replace(self, source, rows):
        self._all_rows = [r for r in self._all_rows if r["source"] != source] + rows

    def _for_source(self):
        return [r for r in self._all_rows if r["source"] == self._source]

    def _apply_filter(self, rows):
        if self._filter == FILTER_PASS:
            return [r for r in rows if r["passed"]]
        if self._filter == FILTER_FAIL:
            return [r for r in rows if not r["passed"]]
        return rows

    # ──────────────────────────────────────────────────────────
    # Events
    # ──────────────────────────────────────────────────────────
    def _on_source_change(self, value):
        self._source = value
        self._refresh_list()

    def _on_filter_change(self, value):
        self._filter = value
        self._refresh_list()

    def _on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
        except (ValueError, IndexError):
            return
        if 0 <= idx < len(self._visible_rows):
            self._show(self._visible_rows[idx])

    def _open_selected(self):
        if self._selected and self._selected.get("image") and os.path.exists(self._selected["image"]):
            _open_externally(self._selected["image"])

    def _open_page_diff(self):
        """Open the selected result's two pages side by side."""
        row = self._selected
        if not row:
            messagebox.showinfo("Nothing Selected",
                                "Pick a result in the list first.")
            return
        if not callable(self._resolve_paths):
            messagebox.showinfo(
                "Documents Not Available",
                "This view was opened without access to the source PDFs, so the "
                "pages cannot be shown side by side.")
            return

        master_pdf, trans_pdf = self._resolve_paths(row)
        if not master_pdf or not trans_pdf:
            messagebox.showwarning(
                "Documents Not Found",
                "Could not locate both documents for this result.\n\n"
                f"Master: {master_pdf or '(not found)'}\n"
                f"Translated: {trans_pdf or row.get('tr_name') or '(not found)'}\n\n"
                "Check the paths configured on the Inspection tab.")
            return

        def _page(value, default=1):
            try:
                n = int(value)
                return n if n > 0 else default
            except (TypeError, ValueError):
                return default

        eng_page = _page(row.get("eng_page"))

        # Where to look in the translation when the result itself does not say -
        # a graphic reported NOT FOUND, for instance. Falling back to the same
        # page number opens two pages that reflow has long since separated, so
        # the topic-aligned page is used instead and only then the page number.
        fallback = eng_page
        try:
            from core import toc as TOC
            fallback = TOC.page_mapper(master_pdf, trans_pdf)(eng_page)
        except Exception:
            pass

        margins = None
        if callable(self._get_margins):
            try:
                margins = self._get_margins()
            except Exception:
                margins = None

        from gui.page_diff_view import open_page_diff
        _win, err = open_page_diff(
            self.winfo_toplevel(),
            master_pdf, eng_page,
            trans_pdf, _page(row.get("tr_page"), fallback),
            focus_rect=row.get("roi_rect"),
            margins=margins,
            title_hint=f"{row.get('title', '')}  •  {row.get('status', '')}")
        if err:
            messagebox.showerror("Could Not Compare", err)

    def _open_folder(self):
        for r in self._all_rows:
            if r.get("image") and os.path.exists(r["image"]):
                _open_externally(os.path.dirname(os.path.dirname(r["image"])))
                return
        print("[Gallery] No comparison images to locate yet.")

    # ──────────────────────────────────────────────────────────
    # Rendering — constant widget count regardless of result volume
    # ──────────────────────────────────────────────────────────
    def _refresh_list(self):
        rows = self._for_source()
        n_pass = sum(1 for r in rows if r["passed"])
        n_fail = len(rows) - n_pass
        self.count_lbl.configure(
            text=f"{n_pass} passed   ·   {n_fail} need review   ·   {len(rows)} total")
        self.summary_lbl.configure(
            text=(f"{len(self._all_rows)} result(s) to review across all sources"
                  if self._all_rows else "Nothing to review yet"))

        self.tree.delete(*self.tree.get_children())
        self._visible_rows = self._apply_filter(rows)

        for i, r in enumerate(self._visible_rows):
            self.tree.insert(
                "", "end", iid=str(i),
                values=("PASS" if r["passed"] else "REVIEW",
                        r["title"],
                        short_target(r["tr_name"]),
                        r["eng_page"],
                        r["tr_page"],
                        r["score"]),
                tags=("pass" if r["passed"] else "fail",))

        if self._visible_rows:
            self._select_index(0)
            try:
                self.tree.focus_set()
            except Exception:
                pass
        else:
            self._clear_detail(
                "No results for this filter." if rows else
                "Run a Region Inspector check, or a full inspection, to see comparisons here.")

    def _set_preview_image(self, photo):
        """
        Swap the preview image without ever leaving the label pointing at a
        released Tk image. The widget is repointed FIRST, and only then is the
        previous reference dropped.
        """
        previous = self._preview_photo          # keep alive across the swap
        try:
            if photo is None:
                self.det_image.configure(image="")
                self.det_image.image = None
            else:
                self.det_image.configure(image=photo)
                self.det_image.image = photo
        except tk.TclError as e:
            print(f"[Gallery] preview swap failed: {e}")
            try:
                self.det_image.configure(image="")
                self.det_image.image = None
            except Exception:
                pass
            photo = None
        self._preview_photo = photo
        del previous

    def _clear_detail(self, hint):
        self._selected = None
        self._set_preview_image(None)
        self.det_title.configure(text="Select a result")
        self.det_status.configure(text="")
        self.det_master.configure(text="-")
        self.det_trans.configure(text="-")
        self.det_detail.configure(text="")
        self.pos_lbl.configure(text="")
        self.det_hint.configure(text=hint)

    def _show(self, row):
        self._selected = row
        accent = DYNAMIC_GREEN if row["passed"] else RADIANT_ORANGE
        self.det_title.configure(text=row["title"])
        self.det_status.configure(text=row["status"], text_color=accent)
        self.det_master.configure(
            text=f"{row['eng_name'] or '(master)'}\nPage {row['eng_page']}")
        shift = f"   ·   shift {row['shift']:+.1f}pt" if row.get("shift") else ""
        self.det_trans.configure(
            text=f"{row['tr_name'] or '(translated)'}\nPage {row['tr_page']}{shift}")
        self.det_detail.configure(text=row.get("detail", ""))
        self._update_position()

        path = row.get("image") or ""
        if not path or not os.path.exists(path):
            self._set_preview_image(None)
            self.det_hint.configure(text="[comparison image not available on disk]")
            return
        try:
            with Image.open(path) as raw:
                img = raw.convert("RGB")
            w, h = img.size
            scale = min(PREVIEW_MAX_W / max(1, w), PREVIEW_MAX_H / max(1, h), 1.0)
            if scale < 1.0:
                img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                                 Image.LANCZOS)
            self._set_preview_image(ImageTk.PhotoImage(img, master=self.det_image))
            self.det_hint.configure(
                text="\u2191 \u2193 to step through results  ·  PgUp / PgDn to jump 10  ·  click the image to open it full size")
        except Exception as e:
            self._set_preview_image(None)
            self.det_hint.configure(text=f"[could not load image: {e}]")
