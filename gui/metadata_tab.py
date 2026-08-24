"""
gui/metadata_tab.py

The Meta Data tab: file size, page count, sheet size and column layout for the
master and every translation, side by side.

This file is only the presentation. Every figure comes from core/metadata.py,
and the two tables build their own columns from SUMMARY_COLUMNS and PAGE_COLUMNS
declared there - so adding a new piece of metadata means editing that module and
nothing here.

The scan runs on a worker thread and reports back through a queue rather than
calling Tk from the thread, which raises "main thread is not in main loop".
"""

import os
import queue
import threading

import customtkinter as ctk
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

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
from core import metadata as meta

_TREE_STYLE = "XylemMeta.Treeview"


class MetadataFrame(ctk.CTkFrame):
    """Document facts for the whole document set, with a per-page breakdown."""

    def __init__(self, parent, get_documents=None, get_margins=None):
        super().__init__(parent, fg_color=UI_BG_CANVAS)

        # Two ways to say which documents to measure. By default the tab follows
        # the Inspection tab, so a run and a scan describe the same set. Point it
        # at its own folder and it becomes a standalone tool for any PDFs at all,
        # which is what it is most useful for between inspections.
        self._get_documents = get_documents
        self._get_margins = get_margins
        self.use_own_folder = ctk.BooleanVar(value=False)
        self.folder_var = tk.StringVar(value="")

        self._rows = []
        self._queue = queue.Queue()
        self._scanning = False

        theme.apply_treeview_style(_TREE_STYLE, row_height=22)
        self._build_ui()
        self._on_source_toggle()

    def _table_rows(self):
        """Requested table height in rows, from the display rather than a guess."""
        try:
            h = self.winfo_screenheight()
        except Exception:
            h = 900
        return max(5, min(12, int((h - 420) / 60)))

    def _f(self, size=11, weight="normal"):
        return ctk.CTkFont(family=theme.resolve_font_family(), size=size, weight=weight)

    # ──────────────────────────────────────────────────────────
    # Layout
    # ──────────────────────────────────────────────────────────
    def _build_ui(self):
        bar = ctk.CTkFrame(self, fg_color=DEPENDABLE_BLUE, corner_radius=8)
        bar.pack(fill="x", padx=14, pady=(12, 8))
        self.summary_lbl = ctk.CTkLabel(
            bar, text="No documents scanned yet", anchor="w", justify="left",
            font=self._f(12, "bold"), text_color=NEUTRAL_WHITE)
        self.summary_lbl.pack(side="left", padx=16, pady=10)
        self.scan_btn = ctk.CTkButton(
            bar, text="⟳  Scan Documents", width=160, height=28,
            fg_color=DYNAMIC_GREEN, hover_color="#4FB003",
            text_color=DEPENDABLE_BLUE, font=self._f(11, "bold"),
            command=self.scan)
        self.scan_btn.pack(side="right", padx=(6, 12), pady=8)

        # ---- where to look ----
        src = ctk.CTkFrame(self, fg_color=UI_CARD_WELL, corner_radius=8)
        src.pack(fill="x", padx=14, pady=(0, 6))
        ctk.CTkCheckBox(src, text="Use my own folder", variable=self.use_own_folder,
                        font=self._f(10, "bold"), text_color=DEPENDABLE_BLUE,
                        checkbox_width=16, checkbox_height=16,
                        fg_color=XYLEM_BLUE, hover_color=UI_HOVER_BLUE,
                        command=self._on_source_toggle).pack(side="left", padx=(12, 8), pady=8)
        self.folder_entry = ctk.CTkEntry(
            src, textvariable=self.folder_var, height=28, font=self._f(10),
            placeholder_text="any folder of PDFs — this tab works on its own")
        self.folder_entry.pack(side="left", fill="x", expand=True, padx=4, pady=8)
        self.browse_btn = ctk.CTkButton(
            src, text="Browse…", width=86, height=28, fg_color=XYLEM_BLUE,
            text_color=NEUTRAL_WHITE, font=self._f(10, "bold"),
            command=self._browse_folder)
        self.browse_btn.pack(side="left", padx=(4, 12), pady=8)
        self.source_lbl = ctk.CTkLabel(src, text="", font=self._f(9),
                                       text_color=NEUTRAL_DARK_GR)
        self.source_lbl.pack(side="right", padx=12, pady=8)

        opts = ctk.CTkFrame(self, fg_color=UI_CARD_WELL, corner_radius=8)
        opts.pack(fill="x", padx=14, pady=(0, 8))
        # Column layout belongs to the stylesheet, not to any one page, so the
        # scan measures a spread of pages rather than all of them and reports a
        # single verdict. Half is enough on every Xylem manual measured; the
        # figure is here because a document with an unusual back section might
        # need more, and because a reviewer should be able to see what the
        # answer rests on.
        ctk.CTkLabel(opts, text="Measure:", font=self._f(10, "bold"),
                     text_color=DEPENDABLE_BLUE).pack(side="left", padx=(12, 4), pady=8)
        self.sample_spin = tk.Spinbox(opts, from_=1, to=100, increment=5, width=5,
                                      font=theme.get_font(9))
        self.sample_spin.delete(0, "end")
        self.sample_spin.insert(0, str(meta.DEFAULT_SAMPLE_PERCENT))
        self.sample_spin.pack(side="left", pady=8)
        ctk.CTkLabel(opts, text="% of pages, spread through the document",
                     font=self._f(9), text_color=NEUTRAL_DARK_GR
                     ).pack(side="left", padx=(6, 0), pady=8)

        self.page_wise = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(opts, text="List every page", variable=self.page_wise,
                        font=self._f(10, "bold"), text_color=DEPENDABLE_BLUE,
                        checkbox_width=16, checkbox_height=16,
                        fg_color=XYLEM_BLUE, hover_color=UI_HOVER_BLUE
                        ).pack(side="left", padx=(16, 8), pady=8)

        # Table detection is the slow part of a scan and the reason a spec table
        # is not mistaken for two columns, so it is on - but it is the first
        # thing to turn off on a very long document.
        self.exclude_tables = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(opts, text="Ignore tables when counting columns",
                        variable=self.exclude_tables, font=self._f(10, "bold"),
                        text_color=DEPENDABLE_BLUE, checkbox_width=16, checkbox_height=16,
                        fg_color=XYLEM_BLUE, hover_color=UI_HOVER_BLUE
                        ).pack(side="left", padx=(20, 10), pady=8)

        self.progress = ctk.CTkProgressBar(opts, height=8, progress_color=XYLEM_BLUE,
                                           width=220)
        self.progress.set(0.0)
        self.progress.pack(side="right", padx=(6, 14), pady=8)
        self.status_lbl = ctk.CTkLabel(opts, text="Ready", font=self._f(10, "bold"),
                                       text_color=theme.TEXT_ON_LIGHT)
        self.status_lbl.pack(side="right", padx=6, pady=8)

        # ---- one row per document ----
        top = ctk.CTkFrame(self, fg_color=UI_CARD_BG, corner_radius=8,
                           border_width=1, border_color=UI_BORDER)
        top.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        ctk.CTkLabel(top, text="Documents", font=self._f(11, "bold"),
                     text_color=DEPENDABLE_BLUE).pack(anchor="w", padx=10, pady=(8, 2))

        rows = self._table_rows()
        self.doc_tree, _ = self._table(top, meta.SUMMARY_COLUMNS, height=rows)
        self.doc_tree.bind("<<TreeviewSelect>>", self._on_doc_select)

        # ---- the selected document, page by page ----
        bottom = ctk.CTkFrame(self, fg_color=UI_CARD_BG, corner_radius=8,
                              border_width=1, border_color=UI_BORDER)
        bottom.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        head = ctk.CTkFrame(bottom, fg_color="transparent")
        head.pack(fill="x", padx=10, pady=(8, 2))
        self.page_hdr = ctk.CTkLabel(head, text="Pages", font=self._f(11, "bold"),
                                     text_color=DEPENDABLE_BLUE)
        self.page_hdr.pack(side="left")
        self.doc_detail = ctk.CTkLabel(head, text="", font=self._f(9),
                                       text_color=NEUTRAL_DARK_GR)
        self.doc_detail.pack(side="right")

        self.page_tree, _ = self._table(bottom, meta.PAGE_COLUMNS, height=rows - 1)

        self.hint_lbl = ctk.CTkLabel(
            self,
            text="Configure the master and translated PDFs on the Inspection tab, "
                 "then press Scan Documents.",
            font=self._f(9), text_color=NEUTRAL_DARK_GR, anchor="w", justify="left")
        self.hint_lbl.pack(fill="x", padx=18, pady=(0, 10))

    def _table(self, parent, spec, height):
        """A ttk table built from a column specification in core.metadata."""
        holder = tk.Frame(parent, bg=UI_CARD_BG)
        holder.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        cols = [key for key, _h, _w, _a in spec]
        tree = ttk.Treeview(holder, columns=cols, show="headings",
                            height=height, style=_TREE_STYLE)
        for key, heading, width, anchor in spec:
            tree.heading(key, text=heading)
            tree.column(key, width=width, anchor=anchor,
                        stretch=(key in ("filename", "note")))
        vsb = ttk.Scrollbar(holder, orient="vertical", command=tree.yview)
        hsb = ttk.Scrollbar(holder, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        tree.pack(side="left", fill="both", expand=True)
        tree.tag_configure("master", background="#E2EDF7")
        tree.tag_configure("problem", background="#FCE5CD", foreground="#783F04")
        return tree, holder

    # ──────────────────────────────────────────────────────────
    # Scanning
    # ──────────────────────────────────────────────────────────
    def scan(self):
        if self._scanning:
            return
        try:
            docs = self.documents()
        except Exception as e:
            messagebox.showerror("Could Not List Documents", str(e))
            return
        if not docs:
            if self.use_own_folder.get():
                messagebox.showinfo(
                    "No PDFs Found",
                    "That folder holds no PDF files.\n\n"
                    f"{self.folder_var.get() or '(no folder chosen)'}")
            else:
                messagebox.showinfo(
                    "No Documents",
                    "Set the English and translated folders on the Inspection tab, "
                    "or tick \u201cUse my own folder\u201d to measure any folder of PDFs.")
            return

        try:
            sample_percent = int(self.sample_spin.get() or meta.DEFAULT_SAMPLE_PERCENT)
        except ValueError:
            sample_percent = meta.DEFAULT_SAMPLE_PERCENT
        sample_percent = max(1, min(100, sample_percent))
        page_wise = bool(self.page_wise.get())
        margins = None
        if callable(self._get_margins):
            try:
                margins = self._get_margins()
            except Exception:
                margins = None

        self._scanning = True
        self.scan_btn.configure(state="disabled", text="Scanning…")
        self.progress.set(0.0)
        self.status_lbl.configure(text=f"0 / {len(docs)}")
        exclude = bool(self.exclude_tables.get())

        def work():
            out = []
            try:
                for i, path in enumerate(docs, start=1):
                    out.append(meta.pdf_metadata(
                        path, margins=margins, exclude_tables=exclude,
                        sample_percent=sample_percent, page_wise=page_wise))
                    self._queue.put(("progress", i, len(docs), os.path.basename(path)))
                self._queue.put(("done", out, None, ""))
            except Exception as e:
                self._queue.put(("done", out, e, ""))

        threading.Thread(target=work, daemon=True).start()
        self.after(80, self._poll)

    def show_rows(self, rows):
        """Display metadata produced elsewhere - by the Inspection tab's run."""
        self._rows = list(rows or [])
        self._fill_documents()
        self.scan_btn.configure(text="\u27f3  Rescan Documents")
        self.status_lbl.configure(text="from the last inspection run",
                                  text_color=theme.TEXT_ON_LIGHT)
        self.progress.set(1.0)

    def documents(self):
        """Whichever set of PDFs this tab is currently pointed at."""
        if self.use_own_folder.get():
            folder = (self.folder_var.get() or "").strip().strip('"').strip("'")
            if not folder or not os.path.isdir(folder):
                return []
            return [os.path.join(folder, f) for f in sorted(os.listdir(folder))
                    if f.lower().endswith(".pdf")]
        if callable(self._get_documents):
            return [p for p in (self._get_documents() or []) if p]
        return []

    def _on_source_toggle(self):
        own = self.use_own_folder.get()
        state = "normal" if own else "disabled"
        try:
            self.folder_entry.configure(state=state)
            self.browse_btn.configure(state=state)
        except Exception:
            pass
        self._describe_source()

    def _browse_folder(self):
        chosen = filedialog.askdirectory(title="Folder of PDFs to measure")
        if chosen:
            self.folder_var.set(chosen)
            self.use_own_folder.set(True)
            self._on_source_toggle()

    def _describe_source(self):
        n = len(self.documents())
        if self.use_own_folder.get():
            where = self.folder_var.get() or "(no folder chosen)"
            self.source_lbl.configure(text=f"{n} PDF(s) in {os.path.basename(where) or where}")
        else:
            self.source_lbl.configure(
                text=f"following the Inspection tab — {n} document(s)")

    def _poll(self):
        try:
            while True:
                msg = self._queue.get_nowait()
                if msg[0] == "progress":
                    _t, done, total, name = msg
                    self.progress.set(done / max(1, total))
                    self.status_lbl.configure(text=f"{done} / {total}  ·  {name[:28]}")
                elif msg[0] == "done":
                    _t, rows, err, _ = msg
                    self._finish(rows, err)
                    return
        except queue.Empty:
            pass
        if self._scanning:
            self.after(80, self._poll)

    def _finish(self, rows, err):
        self._scanning = False
        self.scan_btn.configure(state="normal", text="⟳  Rescan Documents")
        self.progress.set(1.0)
        self._rows = rows or []
        self._fill_documents()
        if err:
            self.status_lbl.configure(text="failed", text_color=theme.TEXT_ATTENTION)
            messagebox.showerror("Scan Failed", str(err))
        else:
            self.status_lbl.configure(text="done", text_color=theme.TEXT_ON_LIGHT)

    # ──────────────────────────────────────────────────────────
    # Rendering
    # ──────────────────────────────────────────────────────────
    def _fill_documents(self):
        self.doc_tree.delete(*self.doc_tree.get_children())
        self.page_tree.delete(*self.page_tree.get_children())

        if not self._rows:
            self.summary_lbl.configure(text="No documents scanned yet")
            return

        # What the reviewer is really looking for here is the odd one out, so
        # anything that disagrees with the master is tinted rather than left to
        # be spotted by reading twelve rows of near-identical numbers.
        master = self._rows[0]
        odd = 0
        for i, r in enumerate(self._rows):
            values = [r.get(key, "-") for key, _h, _w, _a in meta.SUMMARY_COLUMNS]
            tags = []
            if i == 0:
                tags.append("master")
            elif (r.get("sheet") != master.get("sheet")
                  or r.get("columns_label") != master.get("columns_label")
                  or r.get("pages") != master.get("pages")
                  or r.get("error")):
                tags.append("problem")
                odd += 1
            self.doc_tree.insert("", "end", iid=str(i), values=values, tags=tuple(tags))

        total_pages = sum(r.get("pages", 0) for r in self._rows)
        total_bytes = sum(r.get("file_size") or 0 for r in self._rows)
        self.summary_lbl.configure(
            text=(f"{len(self._rows)} document(s)  ·  {total_pages} pages  ·  "
                  f"{meta.human_size(total_bytes)}"
                  + (f"  ·  {odd} differ(s) from the master" if odd else
                     "  ·  all match the master")))
        self.hint_lbl.configure(
            text=("The first row is the English master; a tinted row differs from it "
                  "on sheet size, page count or column layout. Select a row to see "
                  "it page by page."))
        self._select(0)

    def _select(self, index):
        try:
            iid = str(index)
            self.doc_tree.selection_set(iid)
            self.doc_tree.focus(iid)
        except Exception:
            pass

    def _on_doc_select(self, _event=None):
        sel = self.doc_tree.selection()
        if not sel:
            return
        try:
            row = self._rows[int(sel[0])]
        except (ValueError, IndexError):
            return

        note = row.get("sample_note")
        self.page_hdr.configure(
            text=f"Pages — {row['filename']}" + (f"   (measured {note})" if note else ""))
        # doc.metadata["format"] already reads "PDF 1.5"; prefixing it again gave
        # "PDF PDF 1.5".
        bits = [row.get("pdf_version", "-"),
                f"producer: {row.get('producer', '-')[:44]}"]
        if row.get("encrypted"):
            bits.append("ENCRYPTED")
        if row.get("error"):
            bits.append(f"error: {row['error'][:60]}")
        self.doc_detail.configure(text="   ·   ".join(bits))

        self.page_tree.delete(*self.page_tree.get_children())
        dominant = row.get("columns_label")
        if not row.get("page_rows") and not row.get("error"):
            # The per-page detail is off by default. Say so in the table itself,
            # rather than leaving a reviewer looking at an empty panel.
            blank = ["" for _k, _h, _w, _a in meta.PAGE_COLUMNS]
            blank[-1] = ("Tick “List every page” above to see the "
                         "per-page findings behind this verdict.")
            self.page_tree.insert("", "end", values=blank)
            return
        for p in row.get("page_rows", []):
            values = [p.get(key, "-") for key, _h, _w, _a in meta.PAGE_COLUMNS]
            tag = ("problem",) if (dominant and p.get("columns_label") != dominant
                                   and " (+" not in (dominant or "")) else ()
            self.page_tree.insert("", "end", values=values, tags=tag)

    # ──────────────────────────────────────────────────────────
    # Host hooks
    # ──────────────────────────────────────────────────────────
    def documents_changed(self):
        """The configured paths moved; the figures on screen are now stale."""
        self._describe_source()
        if self._rows and not self.use_own_folder.get():
            self.hint_lbl.configure(
                text="The configured documents have changed — press Rescan Documents.")
