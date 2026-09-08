"""
gui/page_diff_view.py

Side-by-side page comparison: the master page and its translation, with what
differs boxed on both.

Opened from the Review tab when a result needs a closer look. The list there
shows one crop against its match; this shows the whole page either side of it,
which is what answers "yes, but is anything else wrong on this page, and where".

Every difference is drawn twice - solid on the side it is on, dashed at the
matching coordinates on the other side. A graphic missing from the translation
is a solid red box on the master and a dashed red ghost on the translation
showing where it should have been, so the eye lands on the same spot in both
panes instead of hunting. The two panes scroll as one for the same reason.

The comparison itself is core/page_diff.py; nothing here decides what a
difference is.
"""

import os
import queue
import threading

import customtkinter as ctk
import tkinter as tk
from PIL import ImageTk

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

# One colour per verdict, used for the boxes and repeated in the legend so the
# panes need no captions of their own.
DIFF_COLORS = {
    page_diff.KIND_MISSING: "#D0021B",      # gone from the translation
    page_diff.KIND_EXTRA: RADIANT_ORANGE,   # only in the translation
    page_diff.KIND_MOVED: XYLEM_BLUE,       # same graphic, different place
}
FOCUS_COLOR = DYNAMIC_GREEN                 # the region this review is about

LEGEND = [
    (page_diff.KIND_MISSING, "Missing from the translation"),
    (page_diff.KIND_EXTRA, "Extra in the translation"),
    (page_diff.KIND_MOVED, "Moved"),
]

PAGE_WHEEL_STEP = 60


class PageDiffWindow(ctk.CTkToplevel):
    """Two pages, one scroll, differences boxed on both."""

    def __init__(self, parent, master_pdf, master_page, trans_pdf, trans_page,
                 focus_rect=None, focus_side="master", margins=None, title_hint=""):
        super().__init__(parent)

        self.master_pdf = master_pdf
        self.trans_pdf = trans_pdf
        self.master_page = int(master_page)
        self.trans_page = int(trans_page)
        self.focus_rect = focus_rect            # the reviewed region, in points
        # Which pane focus_rect was actually measured on. A stylesheet region
        # check measures on the master page, so "master" is the historical
        # default - but an untranslated-text or text-overlap finding is
        # measured on the TRANSLATED page only, and drawing that rect on the
        # master page lands it wherever those same raw coordinates happen to
        # fall there, which is rarely the same sentence.
        self.focus_side = focus_side if focus_side in ("master", "trans") else "master"
        self.margins = margins
        self.title_hint = title_hint

        self.result = None
        self.zoom = 1.0
        self.ignore_text = tk.BooleanVar(value=True)
        self._photos = {}                       # keep PhotoImages alive
        self._current = -1                      # index of the highlighted diff
        self._busy = False
        self._queue = queue.Queue()

        self.title(f"Master ↔ Translated  •  "
                   f"page {self.master_page} vs {self.trans_page}")
        try:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        except Exception:
            sw, sh = 1366, 768
        w = max(850, min(1500, int(sw * 0.94)))
        h = max(560, min(940, int(sh * 0.88)))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(min(850, w), min(540, h))
        self.configure(fg_color=UI_BG_CANVAS)

        self._build_ui()
        self.after(60, self._recompare)

    def _f(self, size=11, weight="normal"):
        return ctk.CTkFont(family=theme.resolve_font_family(), size=size, weight=weight)

    # ──────────────────────────────────────────────────────────
    # Layout
    # ──────────────────────────────────────────────────────────
    def _build_ui(self):
        head = ctk.CTkFrame(self, fg_color=DEPENDABLE_BLUE, corner_radius=8)
        head.pack(fill="x", padx=12, pady=(10, 6))
        self.head_lbl = ctk.CTkLabel(
            head, text=self.title_hint or "Page comparison", anchor="w",
            font=self._f(12, "bold"), text_color=NEUTRAL_WHITE)
        self.head_lbl.pack(side="left", padx=14, pady=9)
        self.summary_lbl = ctk.CTkLabel(
            head, text="Comparing…", anchor="e",
            font=self._f(11, "bold"), text_color=DYNAMIC_GREEN)
        self.summary_lbl.pack(side="right", padx=14, pady=9)

        bar = ctk.CTkFrame(self, fg_color=UI_CARD_WELL, corner_radius=8)
        bar.pack(fill="x", padx=12, pady=(0, 6))

        ctk.CTkButton(bar, text="◀ Previous", width=86, height=26,
                      fg_color=XYLEM_BLUE, text_color=NEUTRAL_WHITE,
                      font=self._f(10, "bold"),
                      command=lambda: self._step(-1)).pack(side="left", padx=(12, 3), pady=7)
        ctk.CTkButton(bar, text="Next ▶", width=76, height=26,
                      fg_color=XYLEM_BLUE, text_color=NEUTRAL_WHITE,
                      font=self._f(10, "bold"),
                      command=lambda: self._step(1)).pack(side="left", padx=3, pady=7)
        self.pos_lbl = ctk.CTkLabel(bar, text="", font=self._f(10, "bold"),
                                    text_color=DEPENDABLE_BLUE)
        self.pos_lbl.pack(side="left", padx=(8, 0), pady=7)

        ctk.CTkButton(bar, text="Zoom +", width=64, height=26, fg_color=UI_CARD_BG,
                      text_color=DEPENDABLE_BLUE, border_width=1, border_color=UI_BORDER,
                      font=self._f(10), command=lambda: self._zoom(0.25)
                      ).pack(side="right", padx=(3, 12), pady=7)
        self.zoom_lbl = ctk.CTkLabel(bar, text="100%", font=self._f(10, "bold"),
                                     text_color=DEPENDABLE_BLUE)
        self.zoom_lbl.pack(side="right", padx=4, pady=7)
        ctk.CTkButton(bar, text="Zoom −", width=64, height=26, fg_color=UI_CARD_BG,
                      text_color=DEPENDABLE_BLUE, border_width=1, border_color=UI_BORDER,
                      font=self._f(10), command=lambda: self._zoom(-0.25)
                      ).pack(side="right", padx=3, pady=7)

        # Off by default and labelled plainly: with it cleared, every translated
        # paragraph becomes a difference, which is right only when the page was
        # not supposed to be translated at all.
        ctk.CTkCheckBox(bar, text="Ignore translated text", variable=self.ignore_text,
                        font=self._f(10, "bold"), text_color=DEPENDABLE_BLUE,
                        checkbox_width=16, checkbox_height=16,
                        fg_color=XYLEM_BLUE, hover_color=UI_HOVER_BLUE,
                        command=self._recompare).pack(side="right", padx=(14, 10), pady=7)

        legend = ctk.CTkFrame(bar, fg_color="transparent")
        legend.pack(side="left", padx=(20, 0), pady=7)
        for kind, label in LEGEND:
            chip = ctk.CTkFrame(legend, fg_color="transparent")
            chip.pack(side="left", padx=6)
            tk.Frame(chip, bg=DIFF_COLORS[kind], width=13, height=13,
                     highlightthickness=0).pack(side="left", padx=(0, 4))
            ctk.CTkLabel(chip, text=label, font=self._f(9),
                         text_color=DEPENDABLE_BLUE).pack(side="left")

        # ---- the two pages ----
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        body.grid_columnconfigure(0, weight=1, uniform="diff_split")
        body.grid_columnconfigure(1, weight=1, uniform="diff_split")
        body.grid_rowconfigure(0, weight=1)

        self.panes = {}
        for col_idx, (side, caption, colour) in enumerate((("master", "MASTER", XYLEM_BLUE),
                                                          ("trans", "TRANSLATED", DEPENDABLE_BLUE))):
            card = ctk.CTkFrame(body, fg_color=UI_CARD_BG, corner_radius=8,
                                border_width=1, border_color=UI_BORDER)
            card.grid(row=0, column=col_idx, sticky="nsew",
                      padx=((0, 5) if side == "master" else (5, 0)))
            cap = ctk.CTkFrame(card, fg_color="transparent")
            cap.pack(fill="x", padx=10, pady=(7, 2))
            ctk.CTkLabel(cap, text=caption, font=self._f(10, "bold"),
                         text_color=colour).pack(side="left")
            name_lbl = ctk.CTkLabel(cap, text="", font=self._f(9),
                                    text_color=NEUTRAL_DARK_GR)
            name_lbl.pack(side="right")

            holder = tk.Frame(card, bg=UI_CARD_BG)
            holder.pack(fill="both", expand=True, padx=8, pady=(0, 8))
            cv = tk.Canvas(holder, bg="#3A3A3A", highlightthickness=0)
            hsb = tk.Scrollbar(holder, orient="horizontal", command=cv.xview)
            cv.configure(xscrollcommand=hsb.set,
                         yscrollcommand=self._on_pane_yscroll)
            hsb.pack(side="bottom", fill="x")
            cv.pack(side="left", fill="both", expand=True)
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                cv.bind(seq, self._on_wheel)
            self.panes[side] = {"canvas": cv, "name": name_lbl}

        # One scrollbar drives both pages. Scrolling them independently would
        # defeat the point: the eye has to land on the same place twice.
        self.vsb = tk.Scrollbar(body, orient="vertical", command=self._yview_both)
        self.vsb.grid(row=0, column=2, sticky="ns", padx=(4, 0))

        self.note_lbl = ctk.CTkLabel(
            self, text="", font=self._f(9), text_color=NEUTRAL_DARK_GR,
            anchor="w", justify="left")
        self.note_lbl.pack(fill="x", padx=16, pady=(0, 8))

    # ──────────────────────────────────────────────────────────
    # Synced scrolling
    # ──────────────────────────────────────────────────────────
    def _yview_both(self, *args):
        for p in self.panes.values():
            p["canvas"].yview(*args)

    def _on_pane_yscroll(self, first, last):
        """One pane moved; move the scrollbar, and let the other pane follow."""
        self.vsb.set(first, last)

    def _on_wheel(self, event):
        num = getattr(event, "num", 0)
        delta = getattr(event, "delta", 0)
        if num in (4, 5):
            step = -1 if num == 4 else 1
        elif delta:
            step = -int(delta / 120) if abs(delta) >= 120 else -int(delta)
        else:
            return "break"
        self._yview_both("scroll", step * PAGE_WHEEL_STEP, "units")
        return "break"

    # ──────────────────────────────────────────────────────────
    # Comparing
    # ──────────────────────────────────────────────────────────
    def _recompare(self):
        if self._busy:
            return
        self._busy = True
        self.summary_lbl.configure(text="Comparing…", text_color=DYNAMIC_GREEN)
        ignore = bool(self.ignore_text.get())

        # The worker must not touch Tk at all - not even self.after(), which
        # registers a command on the interpreter and raises "main thread is not
        # in main loop" when called from anywhere else. It drops the result in a
        # queue and the main thread polls, the same shape app_window uses for
        # the inspection run.
        def work():
            try:
                res = page_diff.compare_pages(
                    self.master_pdf, self.master_page,
                    self.trans_pdf, self.trans_page,
                    ignore_text=ignore, margins=self.margins)
                self._queue.put((res, None))
            except Exception as e:
                self._queue.put((None, e))

        threading.Thread(target=work, daemon=True).start()
        self.after(80, self._poll)

    def _poll(self):
        try:
            res, err = self._queue.get_nowait()
        except queue.Empty:
            if self._busy:
                self.after(80, self._poll)
            return
        self._done(res, err)

    def _done(self, result, err):
        self._busy = False
        if err is not None or result is None:
            self.summary_lbl.configure(text=f"Could not compare: {err}",
                                       text_color=theme.TEXT_ATTENTION)
            self.note_lbl.configure(text="")
            return

        self.result = result
        self._current = -1
        n = len(result["diffs"])
        self.summary_lbl.configure(
            text=page_diff.summarize(result),
            text_color=DYNAMIC_GREEN if n == 0 else RADIANT_ORANGE)
        self.panes["master"]["name"].configure(
            text=f"{os.path.basename(self.master_pdf)}  ·  page {self.master_page}")
        self.panes["trans"]["name"].configure(
            text=f"{os.path.basename(self.trans_pdf)}  ·  page {self.trans_page}")

        notes = list(result["notes"])
        if result["counts"].get(page_diff.KIND_MOVED):
            notes.append("A graphic reported as moved is usually translation reflow, "
                         "not a defect - the shift is shown beside it.")
        if result["counts"].get(page_diff.KIND_MISSING):
            notes.append("A graphic missing here and extra on the next page has "
                         "reflowed across the page break.")
        self.note_lbl.configure(text="  ".join(notes))
        self._render()
        self._update_pos()

    # ──────────────────────────────────────────────────────────
    # Drawing
    # ──────────────────────────────────────────────────────────
    def _zoom(self, delta):
        self.zoom = max(0.4, min(3.0, self.zoom + delta))
        self.zoom_lbl.configure(text=f"{int(self.zoom * 100)}%")
        self._render()

    def _render(self):
        if not self.result:
            return
        r = self.result
        for side, img_key in (("master", "master_image"), ("trans", "trans_image")):
            cv = self.panes[side]["canvas"]
            cv.delete("all")
            img = r[img_key]
            if self.zoom != 1.0:
                img = img.resize((max(1, int(img.width * self.zoom)),
                                  max(1, int(img.height * self.zoom))))
            photo = ImageTk.PhotoImage(img, master=cv)
            self._photos[side] = photo          # a live reference per pane
            cv.create_image(0, 0, anchor="nw", image=photo)
            cv.config(scrollregion=(0, 0, img.width, img.height))
        self._draw_boxes()

    def _pt_to_px(self, value):
        return value * self.result["px_per_pt"] * self.zoom

    def _draw_boxes(self):
        if not self.result:
            return
        for cv in (p["canvas"] for p in self.panes.values()):
            cv.delete("mark")

        # The region this review is actually about, so it is findable at a
        # glance. Solid + labelled on the side it was actually measured on;
        # a dashed echo at the same raw coordinates on the other side is only
        # meaningful when both pages share a layout, which a stylesheet
        # region does (same template, both languages) and a free-floating
        # text-check finding does not - so the echo is master-only.
        if self.focus_rect:
            other = "trans" if self.focus_side == "master" else "master"
            self._box(self.focus_side, self.focus_rect, FOCUS_COLOR, width=2,
                      label="reviewed here")
            if self.focus_side == "master":
                self._box(other, self.focus_rect, FOCUS_COLOR, width=2, dash=(5, 3))

        for i, d in enumerate(self.result["diffs"]):
            colour = DIFF_COLORS.get(d["kind"], RADIANT_ORANGE)
            active = (i == self._current)
            w = 4 if active else 2
            label = page_diff.KIND_LABELS[d["kind"]]
            if d["kind"] == page_diff.KIND_MOVED and d.get("shift_pt"):
                dx, dy = d["shift_pt"]
                label = f"moved {dx:+.0f},{dy:+.0f} pt"

            # Solid where it is; dashed at the same coordinates on the other
            # side, which is what makes the pair findable without hunting.
            if d["rect_master"]:
                self._box("master", d["rect_master"], colour, w,
                          dash=() if d["kind"] != page_diff.KIND_EXTRA else (4, 3),
                          label=label if active else "")
            elif d["rect_trans"]:
                self._box("master", d["rect_trans"], colour, w, dash=(4, 3))

            if d["rect_trans"]:
                self._box("trans", d["rect_trans"], colour, w,
                          dash=() if d["kind"] != page_diff.KIND_MISSING else (4, 3),
                          label=label if active else "")
            elif d["rect_master"]:
                self._box("trans", d["rect_master"], colour, w, dash=(4, 3))

    def _box(self, side, rect_pt, colour, width=2, dash=(), label=""):
        cv = self.panes[side]["canvas"]
        x0, y0, x1, y1 = (self._pt_to_px(v) for v in rect_pt)
        cv.create_rectangle(x0, y0, x1, y1, outline=colour, width=width,
                            dash=dash, tags="mark")
        if label:
            ty = y0 - 9 if y0 > 14 else y1 + 9
            txt = cv.create_text(x0 + 2, ty, anchor="w", text=f" {label} ",
                                 font=theme.get_font(8, "bold"),
                                 fill=colour, tags="mark")
            try:
                bx0, by0, bx1, by1 = cv.bbox(txt)
                chip = cv.create_rectangle(bx0, by0 - 1, bx1, by1 + 1,
                                           fill=NEUTRAL_WHITE, outline="", tags="mark")
                cv.tag_lower(chip, txt)
            except Exception:
                pass

    # ──────────────────────────────────────────────────────────
    # Stepping through the findings
    # ──────────────────────────────────────────────────────────
    def _step(self, delta):
        if not self.result or not self.result["diffs"]:
            return
        n = len(self.result["diffs"])
        self._current = (self._current + delta) % n
        self._draw_boxes()
        self._scroll_to(self.result["diffs"][self._current])
        self._update_pos()

    def _scroll_to(self, diff):
        rect = diff["rect_master"] or diff["rect_trans"]
        top_px = self._pt_to_px(rect[1])
        cv = self.panes["master"]["canvas"]
        total = max(1, int(cv.cget("scrollregion").split()[3]))
        # Put the finding a third of the way down rather than hard at the top,
        # so its surroundings are visible too.
        target = max(0.0, (top_px - cv.winfo_height() / 3) / total)
        self._yview_both("moveto", min(1.0, target))

    def _update_pos(self):
        n = len(self.result["diffs"]) if self.result else 0
        if not n:
            self.pos_lbl.configure(text="no differences")
        elif self._current < 0:
            self.pos_lbl.configure(text=f"{n} to review")
        else:
            self.pos_lbl.configure(text=f"{self._current + 1} of {n}")


def open_page_diff(parent, master_pdf, master_page, trans_pdf, trans_page,
                   focus_rect=None, focus_side="master", margins=None, title_hint=""):
    """Open the comparison window, or return None with a reason if it cannot."""
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