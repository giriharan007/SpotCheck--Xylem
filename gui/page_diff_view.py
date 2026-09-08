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
    page_diff.KIND_REFLOWED: "#8E8E93",     # same graphic, page either side
}
FOCUS_COLOR = DYNAMIC_GREEN                 # the region this review is about

LEGEND = [
    (page_diff.KIND_MISSING, "Missing from the translation"),
    (page_diff.KIND_EXTRA, "Extra in the translation"),
    (page_diff.KIND_MOVED, "Moved"),
    (page_diff.KIND_REFLOWED, "Moved to an adjacent page"),
]

# Pixels of page per wheel notch. The canvases are built with a 1px scroll
# increment, so this is a real distance rather than a fraction of the window.
PAGE_WHEEL_STEP = 60


def _wheel_step(event):
    """
    One wheel notch as +1 (down/away) or -1 (up/towards), or 0.

    X11 reports the wheel as buttons 4 and 5; Windows and macOS report a delta
    on the event, in multiples of 120 on Windows and in small numbers on a
    trackpad. All three shapes reduce to the same notch here.
    """
    num = getattr(event, "num", 0)
    if num in (4, 5):
        return -1 if num == 4 else 1
    delta = getattr(event, "delta", 0)
    if not delta:
        return 0
    if abs(delta) >= 120:
        return -int(delta / 120)
    return -1 if delta > 0 else 1


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

        # How far the pages can be stepped. Taken from the documents rather
        # than from the caller, which only ever names one pair.
        self.master_pages = page_diff.page_count(master_pdf)
        self.trans_pages = page_diff.page_count(trans_pdf)

        self.result = None
        # One zoom per pane. They start locked together and the toolbar moves
        # both, but ctrl+wheel over a pane zooms only that pane, so a small
        # detail on one page can be enlarged against the other at 100%.
        self.zooms = {"master": 1.0, "trans": 1.0}
        self.ignore_text = tk.BooleanVar(value=True)
        self._photos = {}                       # keep PhotoImages alive
        self._current = -1                      # index of the highlighted diff
        self._busy = False
        # Every comparison is stamped, and a result is only accepted if its
        # stamp is still the current one. Without it a page step had to wait
        # for the comparison it interrupted: the second click of a double
        # click was swallowed and the view stayed where it was.
        self._gen = 0
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
        self._update_page_label()
        self._bind_keys()
        self.after(60, self._recompare)

    def _bind_keys(self):
        """
        Keyboard for the two kinds of stepping this window does.

        PageUp/PageDown move through the document; the arrow keys move through
        the findings on the page being shown. Bound on the window rather than
        on a canvas so they work wherever the focus happens to be.
        """
        for seq, fn in (("<Prior>", lambda _e: self._go_page(-1)),
                        ("<Next>", lambda _e: self._go_page(1)),
                        ("<Control-Left>", lambda _e: self._go_page(-1)),
                        ("<Control-Right>", lambda _e: self._go_page(1)),
                        ("<Up>", lambda _e: self._step(-1)),
                        ("<Down>", lambda _e: self._step(1)),
                        ("<Escape>", lambda _e: self.destroy())):
            try:
                self.bind(seq, fn)
            except Exception:
                pass

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

        # Page navigation, kept visually apart from the difference stepper
        # beside it: one moves through the document, the other through the
        # findings on the page it is showing.
        self.prev_page_btn = ctk.CTkButton(
            bar, text="◀◀ Page", width=76, height=26,
            fg_color=DEPENDABLE_BLUE, text_color=NEUTRAL_WHITE,
            font=self._f(10, "bold"),
            command=lambda: self._go_page(-1))
        self.prev_page_btn.pack(side="left", padx=(12, 3), pady=7)
        self.page_lbl = ctk.CTkLabel(bar, text="", font=self._f(10, "bold"),
                                     text_color=DEPENDABLE_BLUE)
        self.page_lbl.pack(side="left", padx=2, pady=7)
        self.next_page_btn = ctk.CTkButton(
            bar, text="Page ▶▶", width=76, height=26,
            fg_color=DEPENDABLE_BLUE, text_color=NEUTRAL_WHITE,
            font=self._f(10, "bold"),
            command=lambda: self._go_page(1))
        self.next_page_btn.pack(side="left", padx=(3, 14), pady=7)

        ctk.CTkButton(bar, text="◀ Previous", width=86, height=26,
                      fg_color=XYLEM_BLUE, text_color=NEUTRAL_WHITE,
                      font=self._f(10, "bold"),
                      command=lambda: self._step(-1)).pack(side="left", padx=(0, 3), pady=7)
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

            # This pane's own paging. The toolbar pair moves the two together
            # and keeps them on one topic, which is the right default; these
            # break that deliberately, for the times when the pairing itself is
            # what needs checking - reflow has put the counterpart a page out,
            # and the only way to see it is to move one side alone.
            nav = ctk.CTkFrame(cap, fg_color="transparent")
            nav.pack(side="left", padx=(10, 0))
            prev_b = ctk.CTkButton(nav, text="◀", width=26, height=22,
                                   fg_color=UI_CARD_WELL, text_color=DEPENDABLE_BLUE,
                                   font=self._f(10, "bold"),
                                   command=lambda sd=side: self._go_page_side(sd, -1))
            prev_b.pack(side="left", padx=1)
            side_lbl = ctk.CTkLabel(nav, text="", font=self._f(9, "bold"),
                                    text_color=DEPENDABLE_BLUE, width=52)
            side_lbl.pack(side="left", padx=2)
            next_b = ctk.CTkButton(nav, text="▶", width=26, height=22,
                                   fg_color=UI_CARD_WELL, text_color=DEPENDABLE_BLUE,
                                   font=self._f(10, "bold"),
                                   command=lambda sd=side: self._go_page_side(sd, 1))
            next_b.pack(side="left", padx=1)

            name_lbl = ctk.CTkLabel(cap, text="", font=self._f(9),
                                    text_color=NEUTRAL_DARK_GR)
            name_lbl.pack(side="right")

            holder = tk.Frame(card, bg=UI_CARD_BG)
            holder.pack(fill="both", expand=True, padx=8, pady=(0, 8))
            # yscrollincrement makes "units" mean PIXELS. Left at Tk's default
            # of 0 a unit is a tenth of the window, so one wheel notch of
            # PAGE_WHEEL_STEP units jumped six screens - which is what made
            # this pane feel like it was teleporting rather than scrolling.
            cv = tk.Canvas(holder, bg="#3A3A3A", highlightthickness=0,
                           yscrollincrement=1, xscrollincrement=1)
            hsb = tk.Scrollbar(holder, orient="horizontal", command=cv.xview)
            cv.configure(xscrollcommand=hsb.set,
                         yscrollcommand=self._on_pane_yscroll)
            hsb.pack(side="bottom", fill="x")
            cv.pack(side="left", fill="both", expand=True)
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                cv.bind(seq, self._on_wheel)
            # Ctrl+wheel zooms, the way every document viewer does it. Bound
            # per pane so the pane under the pointer is the one that changes.
            for seq in ("<Control-MouseWheel>", "<Control-Button-4>",
                        "<Control-Button-5>"):
                cv.bind(seq, lambda e, sd=side: self._on_zoom_wheel(e, sd))
            cv.bind("<Shift-MouseWheel>",
                    lambda e, c=cv: (c.xview("scroll",
                                             -_wheel_step(e) * PAGE_WHEEL_STEP,
                                             "units"), "break")[1])
            self.panes[side] = {"canvas": cv, "name": name_lbl, "page_lbl": side_lbl,
                                "prev": prev_b, "next": next_b}

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
        step = _wheel_step(event)
        if step:
            self._yview_both("scroll", step * PAGE_WHEEL_STEP, "units")
        return "break"

    def _on_zoom_wheel(self, event, side):
        """Ctrl+wheel over one pane: zoom that pane about the pointer."""
        step = _wheel_step(event)
        if step:
            self._zoom(-step * 0.1, side=side, anchor=(event.x, event.y))
        return "break"

    # ──────────────────────────────────────────────────────────
    # Comparing
    # ──────────────────────────────────────────────────────────
    def _go_page(self, delta):
        """
        Step both panes to the next or previous page.

        The master page moves by one; the translated page is then looked up by
        TOPIC rather than moved alongside it. Stepping both by one would drift
        apart the moment a section runs to a different length - which is the
        whole reason these two documents cannot be compared page-for-page - and
        after a few steps the panes would be showing unrelated sections.
        """
        if not self.master_pages:
            return
        target = self.master_page + delta
        if not 1 <= target <= self.master_pages:
            return

        self.master_page = target
        try:
            from core import toc as TOC
            mapped, _code, _why = TOC.matching_page(
                self.master_pdf, self.trans_pdf, target)
        except Exception:
            mapped = target
        if self.trans_pages:
            mapped = max(1, min(self.trans_pages, mapped))
        self.trans_page = mapped

        # The reviewed region belonged to the page we have just left; drawing it
        # here would box whatever happens to sit at those coordinates now.
        self.focus_rect = None
        self._current = -1

        self.title(f"Master ↔ Translated  •  "
                   f"page {self.master_page} vs {self.trans_page}")
        self._update_page_label()
        self._recompare()

    def _go_page_side(self, side, delta):
        """
        Step ONE pane, leaving the other where it is.

        No topic lookup here, deliberately: the point of moving a single pane
        is to look at a page the topic mapping would not have chosen.
        """
        pages = self.master_pages if side == "master" else self.trans_pages
        current = self.master_page if side == "master" else self.trans_page
        if not pages:
            return
        target = current + delta
        if not 1 <= target <= pages:
            return

        if side == "master":
            self.master_page = target
        else:
            self.trans_page = target

        self.focus_rect = None
        self._current = -1
        self.title(f"Master ↔ Translated  •  "
                   f"page {self.master_page} vs {self.trans_page}")
        self._update_page_label()
        self._recompare()

    def _update_page_label(self):
        if self.master_pages:
            self.page_lbl.configure(
                text=f"{self.master_page} / {self.master_pages}")
        else:
            self.page_lbl.configure(text=str(self.master_page))
        first, last = self.master_page <= 1, self.master_page >= self.master_pages
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
                pane["page_lbl"].configure(
                    text=f"{page} / {total}" if total else str(page))
                pane["prev"].configure(state="disabled" if page <= 1 else "normal")
                pane["next"].configure(
                    state="disabled" if total and page >= total else "normal")
            except Exception:
                pass

    def _recompare(self):
        self._gen += 1
        gen = self._gen
        self._busy = True
        self.summary_lbl.configure(text="Comparing…", text_color=DYNAMIC_GREEN)
        ignore = bool(self.ignore_text.get())
        master_page, trans_page = self.master_page, self.trans_page

        # The worker must not touch Tk at all - not even self.after(), which
        # registers a command on the interpreter and raises "main thread is not
        # in main loop" when called from anywhere else. It drops the result in a
        # queue and the main thread polls, the same shape app_window uses for
        # the inspection run.
        def work():
            try:
                res = page_diff.compare_pages(
                    self.master_pdf, master_page,
                    self.trans_pdf, trans_page,
                    ignore_text=ignore, margins=self.margins)
                self._queue.put((gen, res, None))
            except Exception as e:
                self._queue.put((gen, None, e))

        threading.Thread(target=work, daemon=True).start()
        self.after(80, self._poll)

    def _poll(self):
        try:
            gen, res, err = self._queue.get_nowait()
        except queue.Empty:
            if self._busy:
                self.after(80, self._poll)
            return
        if gen != self._gen:
            # A later page step has already superseded this one; its own
            # result is still on its way.
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
        # The section each page belongs to, so the pairing can be checked at a
        # glance. Two panes showing the same topic code is the whole basis on
        # which these pages are comparable; two different codes means reflow
        # has moved something and every "difference" below is suspect.
        self.panes["master"]["name"].configure(
            text=f"{os.path.basename(self.master_pdf)}  ·  page {self.master_page}"
                 f"{self._topic_suffix(self.master_pdf, self.master_page)}")
        self.panes["trans"]["name"].configure(
            text=f"{os.path.basename(self.trans_pdf)}  ·  page {self.trans_page}"
                 f"{self._topic_suffix(self.trans_pdf, self.trans_page)}")

        notes = list(result["notes"])
        if result["counts"].get(page_diff.KIND_MOVED):
            notes.append("A graphic reported as moved is usually translation reflow, "
                         "not a defect - the shift is shown beside it.")
        if result["counts"].get(page_diff.KIND_REFLOWED):
            notes.append("A graphic marked as being on an adjacent page was carried "
                         "there by reflow - it is present, not missing.")
        self.note_lbl.configure(text="  ".join(notes))
        self._render()
        self._update_pos()

    # ──────────────────────────────────────────────────────────
    # Drawing
    # ──────────────────────────────────────────────────────────
    def _zoom(self, delta, side=None, anchor=None):
        """
        Change zoom, on one pane or on both.

        With an anchor - the pointer, for ctrl+wheel - the point under the
        cursor is kept still, so zooming walks INTO the detail being looked at
        instead of drifting away from it.
        """
        sides = (side,) if side else tuple(self.zooms)
        keep = {}
        for sd in sides:
            cv = self.panes[sd]["canvas"]
            before = self.zooms[sd]
            after = max(0.4, min(3.0, before + delta))
            if after == before:
                continue
            if anchor is not None:
                # Where the pointer sits on the page, in unzoomed pixels.
                doc_x = (cv.canvasx(anchor[0])) / before
                doc_y = (cv.canvasy(anchor[1])) / before
                keep[sd] = (doc_x, doc_y, anchor)
            self.zooms[sd] = after

        self._update_zoom_label()
        self._render()

        for sd, (doc_x, doc_y, (px, py)) in keep.items():
            cv = self.panes[sd]["canvas"]
            cv.xview_moveto(0); cv.yview_moveto(0)
            cv.xview("scroll", int(doc_x * self.zooms[sd] - px), "units")
            cv.yview("scroll", int(doc_y * self.zooms[sd] - py), "units")

    @staticmethod
    def _topic_suffix(pdf_path, page_no):
        """'  ·  4.6' for a page inside a numbered section, else nothing."""
        try:
            from core import toc as TOC
            code = TOC.topic_at_page(pdf_path, page_no)
            return f"  ·  {code}" if code else ""
        except Exception:
            return ""

    def _update_zoom_label(self):
        m, t = self.zooms["master"], self.zooms["trans"]
        self.zoom_lbl.configure(
            text=f"{int(m * 100)}%" if m == t
            else f"{int(m * 100)}% / {int(t * 100)}%")

    def _render(self):
        if not self.result:
            return
        r = self.result
        for side, img_key in (("master", "master_image"), ("trans", "trans_image")):
            cv = self.panes[side]["canvas"]
            cv.delete("all")
            img = r[img_key]
            z = self.zooms[side]
            if z != 1.0:
                img = img.resize((max(1, int(img.width * z)),
                                  max(1, int(img.height * z))))
            photo = ImageTk.PhotoImage(img, master=cv)
            self._photos[side] = photo          # a live reference per pane
            cv.create_image(0, 0, anchor="nw", image=photo)
            cv.config(scrollregion=(0, 0, img.width, img.height))
        self._draw_boxes()

    def _pt_to_px(self, value, side="master"):
        return value * self.result["px_per_pt"] * self.zooms[side]

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
            elif d["kind"] == page_diff.KIND_REFLOWED and d.get("found_on_page"):
                where = "translation" if d.get("found_side") == "trans" else "master"
                label = f"on page {d['found_on_page']} of the {where}"

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
        x0, y0, x1, y1 = (self._pt_to_px(v, side) for v in rect_pt)
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