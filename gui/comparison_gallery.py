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

Sources that feed it:
  - Region checks : results from the Region Inspector's batch run
  - Images        : per-crop results from a full pipeline inspection - a
                    one-directional hunt for each master crop in the
                    translation, which is blind to a graphic that was simply
                    deleted when a near-identical sibling exists elsewhere
                    (see Image Counts)
  - Image Counts  : symmetric per-topic graphic counts - catches exactly what
                    Images cannot, because it never asks "does this graphic
                    exist somewhere", it asks "do both documents hold the
                    same number of graphics in this section"
  - Overlap / Not Translated : the text checks

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
SOURCE_COUNTS = "Image Counts"
SOURCE_MATCHED = "Graphic Match"
SOURCE_TOC = "TOC"
SOURCE_BARCODE = "Barcode"
SOURCE_QR = "QR Code"
SOURCE_OVERLAP = "Overlap"
SOURCE_UNTRANSLATED = "Not Translated"
SOURCE_OVERFLOW = "Margin Overflow"

# One soft background tint per check, so a reviewer scanning a file's detail
# list can see at a glance where one check's rows end and the next begin,
# without reading the Check column on every single row.
SOURCE_BAND_COLOR = {
    SOURCE_REGIONS: "#EAF6FF",
    SOURCE_CROPS: "#F3EAFF",
    SOURCE_COUNTS: "#EAFBF0",
    SOURCE_MATCHED: "#FFF6E0",
    SOURCE_TOC: "#FDEAF3",
    SOURCE_BARCODE: "#EEF0FF",
    SOURCE_QR: "#E8ECFF",
    SOURCE_OVERLAP: "#E9F7F7",
    SOURCE_UNTRANSLATED: "#F5F0E6",
    SOURCE_OVERFLOW: "#FDEEE8",
}
DEFAULT_BAND_COLOR = "#F2F2F2"

# A row that needs review gets this tint regardless of which check it came
# from - that is the signal that actually matters, and it must never blend
# into whichever check's band colour happens to be next to it.
REVIEW_BG = "#FCEAE3"
REVIEW_FG = "#8A3B00"
PASS_FG = "#274E13"


def _row_tag(source, passed):
    """A stable, Tk-safe tag name for one (check, pass/fail) combination."""
    slug = re.sub(r"\W+", "_", source or "other").strip("_").lower()
    return f"row_{slug}_{'pass' if passed else 'review'}"

_TREE_STYLE = "XylemGallery.Treeview"

# The two shapes the results tree switches between. Column id, header text,
# width, anchor - see ComparisonGalleryFrame._set_tree_columns.
FILE_COLUMNS = (
    ("verdict", "Verdict", 80, "center"),
    ("file", "File", 220, "w"),
    ("score", "Score", 90, "center"),
    ("time", "Time", 70, "center"),
)
DETAIL_COLUMNS = (
    ("verdict", "Verdict", 66, "center"),
    ("section", "Check", 96, "w"),
    ("item", "Item", 150, "w"),
    ("mpage", "Master Pg", 74, "center"),
    ("tpage", "Trans Pg", 70, "center"),
    ("score", "Score", 68, "center"),
)

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


def _fmt_duration(seconds):
    """
    Seconds under a minute stay as seconds (5.8s); a minute or more reads as
    minutes and seconds (146.9s -> '2m 27s'). Mirrors core.pipeline.format_duration
    so the tool and the Excel report speak the same way.
    """
    if not isinstance(seconds, (int, float)):
        return ""
    s = float(seconds)
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(int(round(s)), 60)
    return f"{m}m {sec:02d}s"


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


def cards_from_image_counts(count_results, page_lookup=None):
    """
    Normalise image_counts.py's symmetric per-topic counts into gallery rows.

    One row per topic per translation. This exists because Images (the crop
    hunt above) is one-directional and provably blind to a deleted graphic
    that has a near-identical sibling elsewhere in the manual - the hunt for
    the deleted crop still matches the sibling and reports PASS. Counting
    both documents catches it, because it never asks "does this graphic exist
    somewhere", it asks "do both documents hold the same number in this
    section" - which a plain deletion always fails.

    `page_lookup(english_pdf, translated_pdf, topic_code)` -> (eng_page,
    tr_page) is optional. It is called for every row, PASS included, so the
    detail view always shows where a topic lives rather than leaving PASS
    rows blank - the caller is expected to cache the per-document TOC lookup
    it needs so this stays cheap across many topics in the same document
    pair.
    """
    rows = []
    for res in count_results or []:
        eng_name = res.get("english_pdf", "")
        tr_name = res.get("translated_pdf", "")
        for r in res.get("rows", []):
            ok = r.get("status") == "PASS"
            eng_page, tr_page = "-", "-"
            if callable(page_lookup):
                try:
                    eng_page, tr_page = page_lookup(eng_name, tr_name, r.get("topic_code"))
                except Exception:
                    eng_page, tr_page = "-", "-"
            title = r.get("topic_title") or ""
            code = r.get("topic_code") or ""
            label = f"{code} · {title}" if (code and code not in ("-",) and not code.startswith("#")) \
                else (title or "Document total")
            rows.append({
                "source": SOURCE_COUNTS,
                "title": label[:80],
                "eng_name": eng_name,
                "eng_page": eng_page or "-",
                "tr_name": tr_name,
                "tr_page": tr_page or "-",
                "status": r.get("status", ""),
                "passed": ok,
                "score": f"{r.get('target_count', 0)} vs {r.get('master_count', 0)}",
                "detail": ("Same count both sides." if ok else
                          f"Master has {r.get('master_count', 0)} graphic(s) here; "
                          f"the translation has {r.get('target_count', 0)}. A one-directional "
                          f"crop hunt can miss this when a near-identical icon exists "
                          f"elsewhere in the manual - this check counts instead of hunting."),
                "shift": 0.0,
                "image": "",
                "roi_rect": None,
            })
    return rows


def cards_from_matched_images(findings):
    """
    Normalise image_counts.py's content-matched per-topic pairing into
    gallery rows.

    Unlike cards_from_image_counts, this never asks "how many" - it asks "is
    each graphic still the right picture", pairing master and translation
    graphics within a topic by what they look like rather than by reading
    order, so a graphic reflow moved to a different page or column does not
    get compared against the wrong sibling. Each finding already names both
    real page numbers directly (no page_lookup needed, unlike Image Counts),
    because the pairing was built from each document's own detected graphic
    positions, not inferred from a topic's page span.
    """
    rows = []
    for f in findings or []:
        ok = bool(f.get("passed"))
        # The topic title already carries its own number ("1.1 Introduction"),
        # so showing the bare code in front of it too just repeats "1.1"
        # twice - a false impression that this is somehow re-checking section
        # numbering (that is a completely separate check; see cards_from_toc).
        label = f.get("topic_title") or f.get("topic_code") or "Document"
        n = f.get("slot_count", 1)
        rows.append({
            "source": SOURCE_MATCHED,
            "title": f"{label[:70]} — graphic {f.get('slot', 1)} of {n}",
            "eng_name": f.get("english_pdf", ""),
            "eng_page": f.get("master_page", "-"),
            "tr_name": f.get("translated_pdf", ""),
            "tr_page": f.get("target_page", "-"),
            "status": f.get("status", ""),
            "passed": ok,
            "score": f"{f.get('match_pct', 0):.1f}%",
            "detail": (f"This picture in the translation still matches the master's "
                      f"picture at this spot ({f.get('match_pct', 0):.0f}% alike)." if ok else
                      f"The picture the translation has here does not look like the "
                      f"master's picture for this spot ({f.get('match_pct', 0):.0f}% alike) - "
                      f"it may be broken, corrupted, or swapped with another graphic "
                      f"in this section."),
            "shift": 0.0,
            "image": f.get("match_img", ""),
        })
    return rows


def cards_from_toc_results(toc_results):
    """
    Normalise toc.py's section-numbering comparison into gallery rows.

    This is the ACTUAL table-of-contents check: does the translation's
    section numbering (1, 1.1, 1.2, 2, 2.1 ...) match the master's, in the
    same order - not the graphic-matching check above, which happens to
    reuse those same section numbers only to say where a graphic lives. One
    row per translated document, since this compares the whole outline as
    one sequence rather than judging individual items.
    """
    rows = []
    for r in toc_results or []:
        ok = (r.get("status") == "PASS")
        missing = r.get("missing_topics") or []
        extra = r.get("extra_topics") or []
        detail = r.get("diff_msg", "")
        if ok:
            detail = (f"All {r.get('english_topic_count', 0)} numbered section(s) are "
                      f"present, in the same order, as the master.")
        rows.append({
            "source": SOURCE_TOC,
            "title": "TOC numbering (whole document)",
            "eng_name": r.get("english_pdf", ""),
            "eng_page": "-",
            "tr_name": r.get("translated_pdf", ""),
            "tr_page": "-",
            "status": r.get("status", ""),
            "passed": ok,
            "score": f"{r.get('translated_topic_count', 0)}/{r.get('english_topic_count', 0)}",
            "detail": detail,
            "shift": 0.0,
            "image": "",
        })
    return rows


def _pages_str(pages):
    """A compact page list for a detail line: [] -> 'none'."""
    pages = [p for p in (pages or [])]
    if not pages:
        return "none"
    shown = ", ".join(str(p) for p in pages[:12])
    return shown + (f", +{len(pages) - 12} more" if len(pages) > 12 else "")


def cards_from_barcode(bc_qr_results):
    """
    The BARCODE count check, on its own - one row per translated document, no QR
    code mixed in. A whole-document count check (does the translation carry the
    same number of barcodes, on the same pages, as the master), so one row per
    file, emitted for every file - "0/0, none" is itself the answer a reviewer
    came to confirm.
    """
    rows = []
    for r in bc_qr_results or []:
        m, t = r.get("master_barcode_count", 0), r.get("target_barcode_count", 0)
        status = r.get("barcode_status", "")
        detail = (f"Barcodes — {status}\n"
                  f"    master page(s): {_pages_str(r.get('master_pages_barcode'))}\n"
                  f"    translated page(s): {_pages_str(r.get('target_pages_barcode'))}")
        note = r.get("detection_note", "")
        if note:
            detail += f"\n({note})"
        rows.append({
            "source": SOURCE_BARCODE,
            "title": "Barcodes",
            "eng_name": r.get("english_pdf", ""),
            "eng_page": "-",
            "tr_name": r.get("translated_pdf", ""),
            "tr_page": "-",
            "status": status,
            "passed": _is_pass(status),   # target/master, at a glance in Score
            "score": f"{t}/{m}",
            "detail": detail,
            "shift": 0.0,
            "image": "",
        })
    return rows


def cards_from_qr(bc_qr_results):
    """The QR-CODE count check, on its own - one row per translated document."""
    rows = []
    for r in bc_qr_results or []:
        m, t = r.get("master_qr_count", 0), r.get("target_qr_count", 0)
        status = r.get("qr_status", "")
        detail = (f"QR codes — {status}\n"
                  f"    master page(s): {_pages_str(r.get('master_pages_qr'))}\n"
                  f"    translated page(s): {_pages_str(r.get('target_pages_qr'))}")
        note = r.get("detection_note", "")
        if note:
            detail += f"\n({note})"
        rows.append({
            "source": SOURCE_QR,
            "title": "QR codes",
            "eng_name": r.get("english_pdf", ""),
            "eng_page": "-",
            "tr_name": r.get("translated_pdf", ""),
            "tr_page": "-",
            "status": status,
            "passed": _is_pass(status),
            "score": f"{t}/{m}",
            "detail": detail,
            "shift": 0.0,
            "image": "",
        })
    return rows


def cards_from_overlaps(findings):
    """
    Colliding text, as gallery rows.

    Every one of these is a defect - there is no "passed" overlap - so they all
    carry passed=False and land under Needs Review, which is where a reviewer
    looks first. The master is a legitimate target here: an overrun caption in
    the English original is still an overrun caption.
    """
    rows = []
    for f in findings or []:
        texts = f.get("texts") or [f.get("text_a", ""), f.get("text_b", "")]
        rows.append({
            "source": SOURCE_OVERLAP,
            "title": f"Overlap · page {f.get('page', '?')}",
            # No master side: the finding is about one document, which may be
            # the master itself. "(master) Page —" would be a lie by layout.
            "eng_name": "—",
            "eng_page": "—",
            "tr_name": f.get("document", ""),
            "tr_page": f.get("page", "-"),
            "status": f"{f.get('pairs', 1)} COLLISION(S)",
            "passed": False,
            "score": f"{f.get('overlap_pt2', 0):.0f} pt²",
            "detail": "  ✕  ".join(texts)[:300] + "\n" + (f.get("why", "") or ""),
            "shift": 0.0,
            "image": f.get("image", ""),
            "roi_rect": f.get("rect"),
            # Which document to open, since the row names a file rather than a
            # master/translation pair.
            "self_path": f.get("path", ""),
        })
    return rows


def cards_from_untranslated(findings):
    """English left in a translation, as gallery rows."""
    rows = []
    for f in findings or []:
        rows.append({
            "source": SOURCE_UNTRANSLATED,
            "title": (f.get("text") or "")[:60] or "untranslated",
            "eng_name": "—",
            "eng_page": "—",
            "tr_name": f.get("document", ""),
            "tr_page": f.get("page", "-"),
            "status": (f.get("reason", "") or "").upper(),
            "passed": False,
            "score": f"{f.get('density', 0):.2f}",
            "detail": f.get("text", ""),
            "shift": 0.0,
            "image": f.get("image", ""),
            "roi_rect": f.get("rect"),
            "self_path": f.get("path", ""),
        })
    return rows


def cards_from_overflows(findings):
    """
    Text running past the left/right margin, as gallery rows.

    One document, not a master/translation pair - the overrun is a fault in
    whichever file it is in, which may be the master - so like the other text
    checks it names that one document and carries self_path so the page view
    opens it directly with the offending line highlighted.
    """
    rows = []
    for f in findings or []:
        side = f.get("side", "right")
        over = f.get("over_pt", 0)
        rows.append({
            "source": SOURCE_OVERFLOW,
            "title": (f.get("text") or "")[:60] or "margin overflow",
            "eng_name": "—",
            "eng_page": "—",
            "tr_name": f.get("document", ""),
            "tr_page": f.get("page", "-"),
            "status": f"PAST {side.upper()} MARGIN",
            "passed": False,
            "score": f"{over:.0f} pt",
            "detail": (f"This line runs {over:.0f} pt past the {side} margin — the "
                       f"text is longer than the live area and spills into the "
                       f"margin.\n{f.get('text', '')}"),
            "shift": 0.0,
            "image": f.get("image", ""),
            "roi_rect": f.get("rect"),
            "self_path": f.get("path", ""),
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
        # Wall-clock seconds per translated file, filled in from the run so the
        # file list can show how long each document took, plus the end-to-end
        # total (which also covers the shared stages).
        self._timings = {}
        self._total_seconds = None
        self._preview_photo = None      # exactly one image alive at a time
        # Two-level view: a per-file summary list first (name, verdict, score),
        # and only on a click does it drill into that one file's findings
        # across every check. "_mode" is which of the two the tree is
        # currently showing; "_detail_file" is which file the drill-in is for.
        self._mode = "files"
        self._detail_file = None
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

        list_head = ctk.CTkFrame(list_card, fg_color="transparent")
        list_head.pack(fill="x", padx=10, pady=(8, 2))
        # Packed only while a file is drilled into - see _refresh_list.
        self.back_btn = ctk.CTkButton(
            list_head, text="← All files", width=88, height=22,
            fg_color=UI_CARD_WELL, text_color=DEPENDABLE_BLUE,
            font=self._f(9, "bold"), command=self._show_file_list)
        self.list_title = ctk.CTkLabel(list_head, text="Results", font=self._f(11, "bold"),
                                       text_color=DEPENDABLE_BLUE)
        self.list_title.pack(side="left")

        holder = tk.Frame(list_card, bg=UI_CARD_BG)
        holder.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.tree = ttk.Treeview(holder, columns=[c[0] for c in FILE_COLUMNS],
                                 show="headings", style=_TREE_STYLE)
        self._set_tree_columns(FILE_COLUMNS)
        sb = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("pass", foreground=PASS_FG)
        self.tree.tag_configure("fail", foreground=REVIEW_FG)
        # One tag per (check, pass/fail) combination, pre-configured so a
        # detail row's colour is set with a single unambiguous tag rather
        # than layering two tags and hoping ttk resolves the conflict the
        # way we want. A PASS row gets its check's band colour; a row that
        # needs review gets the warning tint instead, overriding the band -
        # that is the one thing that must stand out regardless of which
        # check it belongs to.
        all_sources = list(SOURCE_BAND_COLOR) + ["other"]
        for src in all_sources:
            band = SOURCE_BAND_COLOR.get(src, DEFAULT_BAND_COLOR)
            self.tree.tag_configure(_row_tag(src, True), background=band, foreground=PASS_FG)
            self.tree.tag_configure(_row_tag(src, False), background=REVIEW_BG, foreground=REVIEW_FG)
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
        self._refresh_list()

    def load_crop_details(self, details):
        self._replace(SOURCE_CROPS, cards_from_crop_details(details))
        self._refresh_list()

    def load_image_counts(self, count_results):
        """
        Publish image_counts.py's symmetric per-topic counts.

        A page lookup is wired in here (not in the pure cards_from_ function)
        because it needs self._resolve_paths to turn the reported file names
        back into real paths, then core.toc to find where every topic - PASS
        rows included, so the detail view always shows a page number - lives
        in each document. The TOC spans for a given document pair are
        extracted once and cached here rather than once per topic row.
        """
        span_cache = {}

        def _spans_for(master_pdf, trans_pdf):
            key = (master_pdf, trans_pdf)
            if key not in span_cache:
                from core import toc as TOC
                span_cache[key] = (TOC.topic_page_spans(master_pdf),
                                    TOC.topic_page_spans(trans_pdf))
            return span_cache[key]

        def _page_lookup(eng_name, tr_name, topic_code):
            if not callable(self._resolve_paths):
                return "-", "-"
            master_pdf, trans_pdf = self._resolve_paths(
                {"eng_name": eng_name, "tr_name": tr_name})
            if not master_pdf or not trans_pdf:
                return "-", "-"
            try:
                eng_spans, tr_spans = _spans_for(master_pdf, trans_pdf)
            except Exception:
                return "-", "-"
            eng_page = eng_spans.get(topic_code, (None,))[0] or "-"
            tr_page = tr_spans.get(topic_code, (None,))[0] or "-"
            return eng_page, tr_page

        self._replace(SOURCE_COUNTS, cards_from_image_counts(count_results, _page_lookup))
        self._refresh_list()

    def load_matched_images(self, matched_findings):
        """
        Publish image_counts.py's content-matched pairing (broken or swapped
        graphics that a count agreeing on both sides would miss).
        """
        self._replace(SOURCE_MATCHED, cards_from_matched_images(matched_findings))
        self._refresh_list()

    def load_toc_results(self, toc_results):
        """
        Publish toc.py's section-numbering comparison - the actual table-of-
        contents check, distinct from Graphic Match above even though both
        happen to talk about section numbers.
        """
        self._replace(SOURCE_TOC, cards_from_toc_results(toc_results))
        self._refresh_list()

    def load_barcode_qr(self, bc_qr_results):
        """
        Publish barcode_qr.py's checks as TWO separate checks - Barcode and QR
        Code, each its own row per file - so a barcode dropped in a translation
        and a QR code dropped are seen and reviewed independently, neither mixed
        into the other or into any other check.
        """
        self._replace(SOURCE_BARCODE, cards_from_barcode(bc_qr_results))
        self._replace(SOURCE_QR, cards_from_qr(bc_qr_results))
        self._refresh_list()

    def load_timings(self, timing_rows, total_seconds=None):
        """
        Record how long each translated PDF took, so the file list can show it.

        Accepts the pipeline's timing_rows (list of {filename, role, seconds})
        and the end-to-end total; the master row is kept in the map too so its
        time is available, and total_seconds is shown once, in the list header.
        """
        self._timings = {}
        for r in timing_rows or []:
            name = r.get("filename")
            if name:
                self._timings[name] = r.get("seconds")
        self._total_seconds = total_seconds
        self._refresh_list()

    def load_text_checks(self, overlaps, untranslated_rows):
        """Both text checks at once - they are produced by one scan."""
        self._replace(SOURCE_OVERLAP, cards_from_overlaps(overlaps))
        self._replace(SOURCE_UNTRANSLATED, cards_from_untranslated(untranslated_rows))
        self._refresh_list()

    def load_overflows(self, overflow_rows):
        """Text that runs past the left/right margin, into the Review breakdown."""
        self._replace(SOURCE_OVERFLOW, cards_from_overflows(overflow_rows))
        self._refresh_list()

    def _replace(self, source, rows):
        self._all_rows = [r for r in self._all_rows if r["source"] != source] + rows
        # A fresh batch of results always lands on the file summary first,
        # never mid-drill-down into whatever file happened to be open before.
        self._mode = "files"
        self._detail_file = None

    def _file_summaries(self):
        """
        One entry per translated document: does EVERYTHING about it pass -
        across Region Checks, Images, Image Counts, Overlap and Not
        Translated at once - and what the tally is.
        """
        agg = {}
        for r in self._all_rows:
            name = r.get("tr_name") or ""
            if not name:
                continue
            p, t = agg.get(name, (0, 0))
            agg[name] = (p + (1 if r["passed"] else 0), t + 1)

        summaries = []
        for name in sorted(agg):
            p, t = agg[name]
            summaries.append({
                "name": name,
                "passed": p,
                "total": t,
                "verdict": "PASS" if p == t else "REVIEW",
                "score_label": f"{(p / t * 100 if t else 0):.0f}%",
                "time_label": _fmt_duration(self._timings.get(name)),
            })
        return summaries

    def _set_tree_columns(self, columns):
        cids = [c[0] for c in columns]
        self.tree.configure(columns=cids)
        for cid, text, w, anchor in columns:
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=w, anchor=anchor, stretch=(cid in ("file", "item")))

    def _set_compare_enabled(self, enabled):
        try:
            self.compare_btn.configure(state="normal" if enabled else "disabled")
        except Exception:
            pass

    def _show_file_list(self):
        """The back arrow: leave a file's detail view for the summary list."""
        self._mode = "files"
        self._detail_file = None
        self._refresh_list()

    def _drill_into(self, name):
        """A file was clicked in the summary list: show everything about it."""
        self._mode = "detail"
        self._detail_file = name
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

        # A text-check row names ONE document - the manual the collision or the
        # English leftover is in - and that document may be the master itself.
        # Put it on the right, where the flagged page and its highlight belong,
        # and the English original on the left to read the passage against.
        own = row.get("self_path") or ""
        if own and os.path.isfile(own):
            trans_pdf = own
            if not master_pdf or os.path.abspath(master_pdf) == os.path.abspath(own):
                master_pdf = own

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

        # A text-check row (overlap / untranslated text) names ONE real page -
        # the translated one, where the finding was actually measured - and no
        # master page of its own ("—"). Its roi_rect is in that page's own
        # coordinates too, so it belongs on the "trans" pane, not "master".
        is_self_row = bool(row.get("self_path"))
        focus_side = "trans" if is_self_row else "master"

        if is_self_row:
            tr_page = _page(row.get("tr_page"), 1)
            # The master page is not reported at all here, so it is mapped
            # from the known translated page rather than reused as the same
            # page number - which drifts as soon as the two documents'
            # pagination differs, and is what put the wrong sentence under
            # the highlight on the master side.
            try:
                from core import toc as TOC
                eng_page = TOC.page_mapper(trans_pdf, master_pdf)(tr_page)
            except Exception:
                eng_page = tr_page
        else:
            # "—" for a row with no master page at all. Open the same number
            # on the left so the two sides start off aligned.
            eng_page = _page(row.get("eng_page"), _page(row.get("tr_page")))

            # Where to look in the translation when the result itself does not
            # say - a graphic reported NOT FOUND, for instance. Falling back to
            # the same page number opens two pages that reflow has long since
            # separated, so the topic-aligned page is used instead.
            fallback = eng_page
            try:
                from core import toc as TOC
                fallback = TOC.page_mapper(master_pdf, trans_pdf)(eng_page)
            except Exception:
                pass
            tr_page = _page(row.get("tr_page"), fallback)

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
            trans_pdf, tr_page,
            focus_rect=row.get("roi_rect"),
            focus_side=focus_side,
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
        self.tree.delete(*self.tree.get_children())
        if self._mode == "files":
            self._render_file_list()
        else:
            self._render_file_detail()

    def _render_file_list(self):
        """The landing view: one row per translated document, name and score."""
        self._set_tree_columns(FILE_COLUMNS)
        self.list_title.configure(text="Results")
        self.back_btn.pack_forget()
        self._set_compare_enabled(False)

        summaries = self._file_summaries()
        self._visible_rows = summaries
        n_pass = sum(1 for s in summaries if s["verdict"] == "PASS")
        n_fail = len(summaries) - n_pass
        self.count_lbl.configure(
            text=f"{n_pass} passed   ·   {n_fail} need review   ·   {len(summaries)} file(s)")
        total_note = (f"   ·   {_fmt_duration(self._total_seconds)} total"
                      if isinstance(self._total_seconds, (int, float)) else "")
        self.summary_lbl.configure(
            text=(f"{len(self._all_rows)} result(s) across {len(summaries)} file(s)"
                  f"{total_note}"
                  if self._all_rows else "Nothing to review yet"))

        for i, s in enumerate(summaries):
            self.tree.insert(
                "", "end", iid=str(i),
                values=(s["verdict"], s["name"], s["score_label"],
                        s.get("time_label", "")),
                tags=("pass" if s["verdict"] == "PASS" else "fail",))

        # Nothing auto-selected here on purpose: landing on this view and
        # immediately jumping into the first file's detail would mean this
        # summary is never actually seen, which defeats the point of it.
        self._clear_detail(
            "Click a file above to see its results." if summaries else
            "Run a Region Inspector check, or a full inspection, to see results here.")

    def _render_file_detail(self):
        """Drilled into one file: every finding about it, from every check."""
        self._set_tree_columns(DETAIL_COLUMNS)
        self.list_title.configure(text=self._detail_file or "Results")
        self.back_btn.pack(side="left", padx=(0, 8))
        self._set_compare_enabled(True)

        rows = [r for r in self._all_rows if (r.get("tr_name") or "") == self._detail_file]
        self._visible_rows = rows
        n_pass = sum(1 for r in rows if r["passed"])
        n_fail = len(rows) - n_pass
        self.count_lbl.configure(
            text=f"{n_pass} passed   ·   {n_fail} need review   ·   {len(rows)} total")
        self.summary_lbl.configure(
            text=f"{len(rows)} result(s) for {self._detail_file}")

        for i, r in enumerate(rows):
            self.tree.insert(
                "", "end", iid=str(i),
                values=("PASS" if r["passed"] else "REVIEW",
                        r["source"],
                        r["title"],
                        r["eng_page"],
                        r["tr_page"],
                        r["score"]),
                tags=(_row_tag(r["source"], r["passed"]),))

        if rows:
            self._select_index(0)
            try:
                self.tree.focus_set()
            except Exception:
                pass
        else:
            self._clear_detail("No results for this file.")

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
        if self._mode == "files":
            # A row here is a file summary, not a finding - selecting one
            # (click, or arrow-key navigation landing on it) means "open it".
            self._drill_into(row["name"])
            return
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