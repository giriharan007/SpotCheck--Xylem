"""
gui/text_checks_tab.py

The Text Checks tab: two questions the region stylesheet cannot answer.

  Text overlap        Does any text collide with other text? Applies to the
                      English master as much as the translations - an overrun
                      caption is a layout fault, not a translation fault.
  Not translated      Is there English left in a translated manual?

Both live here rather than in the region stylesheet because neither is about a
place on the page. A collision can happen anywhere, and a missed segment is
wherever the translator's eye slipped. The findings are also handed to the
Review tab, which is where a reviewer works through them page by page.

The first and last page of every document are skipped, on purpose: the cover
and the back matter carry addresses, trademarks and a copyright line that are
English by design and set by hand.

The scan runs on a worker thread and reports back through a queue polled on the
main thread. Tk objects must not be touched from a worker - self.after() from
one raises "main thread is not in main loop" - and this tab was written after
that lesson had already been paid for elsewhere in the application.
"""

import os
import queue
import threading

from PIL import Image, ImageTk
import customtkinter as ctk
import tkinter as tk
from tkinter import ttk, messagebox

from gui import theme
from gui.theme import (
    XYLEM_BLUE,
    DEPENDABLE_BLUE,
    DYNAMIC_GREEN,
    UI_BG_CANVAS,
    UI_CARD_BG,
    UI_CARD_WELL,
    UI_BORDER,
    NEUTRAL_DARK_GR,
)
from core import text_overlap, untranslated

CHECK_OVERLAP = "Text Overlap"
CHECK_UNTRANSLATED = "Not Translated"

_TREE_STYLE = "XylemTextChecks.Treeview"

PREVIEW_MAX_W = 720
PREVIEW_MAX_H = 260

EVIDENCE_DIR_NAME = "Text_Checks"


class TextChecksFrame(ctk.CTkFrame):
    """Scan the configured documents for colliding and untranslated text."""

    def __init__(self, parent, get_documents=None, get_margins=None,
                 get_output_dir=None, on_results=None):
        super().__init__(parent, fg_color=UI_BG_CANVAS)

        # Supplied by the host so this tab never reaches into the Inspection
        # tab's widgets: () -> (master_pdf, [translated pdfs])
        self._get_documents = get_documents
        self._get_margins = get_margins
        self._get_output_dir = get_output_dir
        self._on_results = on_results

        self._rows = {CHECK_OVERLAP: [], CHECK_UNTRANSLATED: []}
        self._visible = []
        self._check = CHECK_OVERLAP
        self._preview_photo = None
        self._selected = None
        self._scanning = False
        self._queue = queue.Queue()

        theme.apply_treeview_style(_TREE_STYLE)
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    @staticmethod
    def _f(size=11, weight="normal"):
        return theme.get_font(size, weight)

    def _build_ui(self):
        head = ctk.CTkFrame(self, fg_color=DEPENDABLE_BLUE, corner_radius=8)
        head.pack(fill="x", padx=14, pady=(12, 8))
        ctk.CTkLabel(
            head, text="Text Checks",
            font=self._f(13, "bold"), text_color="#FFFFFF").pack(side="left", padx=14, pady=8)
        self.head_lbl = ctk.CTkLabel(
            head,
            text="Colliding text in any document  ·  English left in a translation",
            font=self._f(10), text_color="#D6E6F2")
        self.head_lbl.pack(side="left", padx=6, pady=8)

        controls = ctk.CTkFrame(self, fg_color=UI_CARD_WELL, corner_radius=8)
        controls.pack(fill="x", padx=14, pady=(0, 8))

        self.scan_btn = ctk.CTkButton(
            controls, text="▶  Scan Documents", width=170, height=30,
            fg_color=DYNAMIC_GREEN, hover_color="#4FB003",
            text_color=DEPENDABLE_BLUE, font=self._f(11, "bold"),
            command=self.start_scan)
        self.scan_btn.pack(side="left", padx=(12, 10), pady=8)

        ctk.CTkLabel(controls, text="Show:", font=self._f(10, "bold"),
                     text_color=DEPENDABLE_BLUE).pack(side="left", padx=(6, 4), pady=8)
        self.check_sel = ctk.CTkSegmentedButton(
            controls, values=[CHECK_OVERLAP, CHECK_UNTRANSLATED], font=self._f(10),
            command=self._on_check_change, **theme.segmented_button_colors())
        self.check_sel.set(CHECK_OVERLAP)
        self.check_sel.pack(side="left", padx=4, pady=8)

        self.count_lbl = ctk.CTkLabel(controls, text="Not scanned yet",
                                      font=self._f(10), text_color=NEUTRAL_DARK_GR)
        self.count_lbl.pack(side="right", padx=14, pady=8)

        self.progress = ctk.CTkProgressBar(self, height=6, progress_color=XYLEM_BLUE)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=14, pady=(0, 6))

        split = ctk.CTkFrame(self, fg_color="transparent")
        split.pack(fill="both", expand=True, padx=14, pady=(0, 12))

        # The evidence panel is packed FIRST, against the bottom, so it keeps
        # its height on a laptop screen. Packed after the list it was the last
        # thing to get space and the picture of the defect - the whole point of
        # the tab - fell off the bottom of the window.
        det = ctk.CTkFrame(split, fg_color=UI_CARD_BG, corner_radius=8,
                           border_width=1, border_color=UI_BORDER,
                           height=PREVIEW_MAX_H + 78)
        det.pack(side="bottom", fill="x", pady=(8, 0))
        det.pack_propagate(False)

        list_card = ctk.CTkFrame(split, fg_color=UI_CARD_BG, corner_radius=8,
                                 border_width=1, border_color=UI_BORDER)
        list_card.pack(side="top", fill="both", expand=True)

        holder = tk.Frame(list_card, bg=UI_CARD_BG)
        holder.pack(fill="both", expand=True, padx=8, pady=8)

        cols = ("doc", "page", "what", "detail")
        self.tree = ttk.Treeview(holder, columns=cols, show="headings", style=_TREE_STYLE)
        for cid, text, width, anchor, stretch in (
            ("doc", "Manual", 190, "w", False),
            ("page", "Page", 60, "center", False),
            ("what", "Finding", 120, "w", False),
            ("detail", "Text", 560, "w", True),
        ):
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=width, anchor=anchor, stretch=stretch)
        sb = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        self.det_title = ctk.CTkLabel(det, text="Select a finding",
                                      font=self._f(11, "bold"), text_color=DEPENDABLE_BLUE,
                                      anchor="w", justify="left")
        self.det_title.pack(anchor="w", padx=12, pady=(8, 0))
        self.det_why = ctk.CTkLabel(det, text="", font=self._f(10),
                                    text_color=NEUTRAL_DARK_GR, anchor="w", justify="left")
        self.det_why.pack(anchor="w", padx=12, pady=(0, 4))
        # tk.Label, not CTkLabel: CTkLabel.configure() re-applies the previously
        # bound image, which raises if that PhotoImage has already been released.
        self.det_image = tk.Label(det, bd=0, bg=UI_CARD_BG)
        self.det_image.pack(anchor="nw", padx=12, pady=(0, 10))

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------
    def _documents(self):
        if not callable(self._get_documents):
            return None, []
        try:
            master, translated = self._get_documents()
        except Exception:
            return None, []
        return master, list(translated or [])

    def _evidence_dir(self):
        base = ""
        if callable(self._get_output_dir):
            try:
                base = self._get_output_dir() or ""
            except Exception:
                base = ""
        if not base:
            base = os.path.join(os.path.expanduser("~"), ".spotcheck")
        return os.path.join(base, "Cropped_Comparison", EVIDENCE_DIR_NAME)

    def start_scan(self):
        if self._scanning:
            return
        master, translated = self._documents()
        if not master and not translated:
            messagebox.showinfo(
                "Nothing to Scan",
                "Set the English master and the translated folder on the "
                "Inspection tab first.")
            return

        margins = None
        if callable(self._get_margins):
            try:
                margins = self._get_margins()
            except Exception:
                margins = None

        self._scanning = True
        self.scan_btn.configure(state="disabled", text="Scanning…")
        self.progress.set(0)
        self.count_lbl.configure(text="Scanning…")

        args = (master, translated, margins, self._evidence_dir())
        threading.Thread(target=self._scan_worker, args=args, daemon=True).start()
        self.after(120, self._drain_queue)

    def _scan_worker(self, master, translated, margins, evidence_dir):
        """Runs OFF the main thread. Talks back only through the queue."""
        try:
            documents = ([master] if master else []) + list(translated)
            done = [0]
            total_docs = max(1, len(documents) + len(translated))

            def tick(_page, _total, name):
                self._queue.put(("progress", done[0] / total_docs, name))

            overlaps = []
            for path in documents:
                overlaps.extend(text_overlap.find_overlaps(
                    path, skip_first_last=True, margins=margins,
                    evidence_dir=os.path.join(evidence_dir, "overlap"),
                    progress=tick))
                done[0] += 1
                self._queue.put(("progress", done[0] / total_docs, os.path.basename(path)))

            inventory = untranslated.master_inventory(master) if master else set()
            missed = []
            for path in translated:
                if master and os.path.abspath(path) == os.path.abspath(master):
                    continue
                missed.extend(untranslated.find_untranslated(
                    path, inventory, skip_first_last=True,
                    evidence_dir=os.path.join(evidence_dir, "untranslated"),
                    progress=tick))
                done[0] += 1
                self._queue.put(("progress", done[0] / total_docs, os.path.basename(path)))

            self._queue.put(("done", overlaps, missed))
        except Exception as e:                       # never leave the button stuck
            self._queue.put(("error", str(e), None))

    def _drain_queue(self):
        try:
            while True:
                kind, a, b = self._queue.get_nowait()
                if kind == "progress":
                    self.progress.set(min(1.0, float(a)))
                    self.count_lbl.configure(text=f"Scanning {b}…")
                elif kind == "done":
                    self._finish(a, b)
                    return
                elif kind == "error":
                    self._scanning = False
                    self.scan_btn.configure(state="normal", text="▶  Scan Documents")
                    self.progress.set(0)
                    self.count_lbl.configure(text="Scan failed")
                    messagebox.showerror("Text Checks Failed", a)
                    return
        except queue.Empty:
            pass
        if self._scanning:
            self.after(120, self._drain_queue)

    def _finish(self, overlaps, missed):
        self._scanning = False
        self.scan_btn.configure(state="normal", text="▶  Scan Documents")
        self.progress.set(1.0)
        self._rows[CHECK_OVERLAP] = overlaps
        self._rows[CHECK_UNTRANSLATED] = missed
        self._refresh()
        print(f"[Text Checks] {len(overlaps)} overlap(s), "
              f"{len(missed)} untranslated line(s)")
        if callable(self._on_results):
            try:
                self._on_results(overlaps, missed)
            except Exception as e:
                print(f"[Text Checks] Could not hand results to the Review tab: {e}")

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------
    def _on_check_change(self, value):
        self._check = value
        self._refresh()

    def _refresh(self):
        rows = self._rows.get(self._check, [])
        self._visible = rows
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(rows):
            if self._check == CHECK_OVERLAP:
                what = f"{r.get('pairs', 1)} collision(s)"
                detail = "  ✕  ".join(r.get("texts") or [])[:220]
            else:
                what = r.get("reason", "")
                detail = r.get("text", "")[:220]
            self.tree.insert("", "end", iid=str(i),
                             values=(r.get("document", ""), r.get("page", ""), what, detail))

        n_ov = len(self._rows[CHECK_OVERLAP])
        n_un = len(self._rows[CHECK_UNTRANSLATED])
        self.count_lbl.configure(
            text=(f"{n_ov} overlap(s)   ·   {n_un} untranslated line(s)"
                  if (n_ov or n_un) else "Nothing found — both checks clean"))
        if rows:
            self.tree.selection_set("0")
            self._show(rows[0])
        else:
            self._clear()

    def _on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        try:
            idx = int(sel[0])
        except ValueError:
            return
        if 0 <= idx < len(self._visible):
            self._show(self._visible[idx])

    def _set_preview(self, photo):
        previous = self._preview_photo
        try:
            self.det_image.configure(image=photo or "")
            self.det_image.image = photo
        except tk.TclError:
            self.det_image.configure(image="")
            self.det_image.image = None
            photo = None
        self._preview_photo = photo
        del previous

    def _clear(self):
        self._selected = None
        self._set_preview(None)
        self.det_title.configure(text="Select a finding")
        self.det_why.configure(
            text=("Nothing to look at. Both checks skip the first and last page: "
                  "covers and back matter are English by design."))

    def _show(self, row):
        self._selected = row
        page = row.get("page", "?")
        self.det_title.configure(text=f"{row.get('document', '')}  ·  page {page}")
        if self._check == CHECK_OVERLAP:
            self.det_why.configure(text=row.get("why", ""))
        else:
            reason = {
                "verbatim": "word for word in the English master — this segment was missed",
                "dense": "reads as English by its function words",
                "verbatim + dense": ("word for word in the English master AND reads as "
                                     "English — certain"),
            }.get(row.get("reason", ""), row.get("reason", ""))
            self.det_why.configure(text=reason)

        path = row.get("image") or ""
        if not path or not os.path.exists(path):
            self._set_preview(None)
            return
        try:
            with Image.open(path) as raw:
                img = raw.convert("RGB")
            w, h = img.size
            scale = min(PREVIEW_MAX_W / max(1, w), PREVIEW_MAX_H / max(1, h), 1.0)
            if scale < 1.0:
                img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                                 Image.LANCZOS)
            self._set_preview(ImageTk.PhotoImage(img, master=self.det_image))
        except Exception as e:
            self._set_preview(None)
            self.det_why.configure(text=f"{self.det_why.cget('text')}   [{e}]")

    def show_results(self, overlaps, missed):
        """
        Display findings the main run already produced.

        The full inspection scans for both of these itself, so opening this tab
        after a run should show that run's findings - scanning the same twelve
        documents a second time to see what the report already says is work
        nobody asked for.
        """
        self._rows[CHECK_OVERLAP] = list(overlaps or [])
        self._rows[CHECK_UNTRANSLATED] = list(missed or [])
        self.progress.set(1.0)
        self._refresh()

    # ------------------------------------------------------------------
    def results(self):
        """(overlaps, untranslated) as last scanned."""
        return self._rows[CHECK_OVERLAP], self._rows[CHECK_UNTRANSLATED]