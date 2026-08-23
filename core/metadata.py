"""
core/metadata.py

Document facts: how big the file is, how many pages, what sheet it is set on,
and how many text columns each page runs.

This is the place to add anything else worth knowing about a PDF at a glance.
The interface renders itself from the two column specifications at the bottom of
this file, so a new field needs three things and nothing more:

    1. put the value into the dict built by pdf_metadata() or page_metadata()
    2. add one entry to SUMMARY_COLUMNS (per document) or PAGE_COLUMNS (per page)
    3. nothing else - gui/metadata_tab.py picks it up on the next run

No GUI toolkit is imported here, so everything below can be driven from a script
or a test.

On column detection
-------------------
Column count is the only figure here that has to be inferred rather than read,
and the naive version is wrong in two specific ways that both showed up on the
Start 350 manual immediately:

  - a two-column SPEC TABLE has a vertical gap down the middle of it, and that
    gap reads exactly like a column gutter. Table regions are therefore removed
    before the page is measured.
  - a short right-aligned run of text near the outer edge leaves a sliver of
    whitespace that also reads as a gutter, turning a plainly single-column page
    into a two-column one. A band only counts as a column if it is a reasonable
    share of the text width AND carries several lines of its own.

What remains is a projection: with headers, footers, side margins and tables
out of the way, a real gutter is a vertical strip that no body line writes into,
wide enough to be deliberate, with substantial text on both sides of it.
"""

import os

import pymupdf as fitz

from core import margins as page_margins

# ==============================================================================
# SHEET SIZES
# ==============================================================================
# ISO A series plus the three North American sizes that turn up in practice.
# Millimetres, portrait (short edge first); landscape is detected by swapping.
SHEET_SIZES_MM = [
    ("A0", 841, 1189),
    ("A1", 594, 841),
    ("A2", 420, 594),
    ("A3", 297, 420),
    ("A4", 210, 297),
    ("A5", 148, 210),
    ("A6", 105, 148),
    ("A7", 74, 105),
    ("A8", 52, 74),
    ("Letter", 216, 279),
    ("Legal", 216, 356),
    ("Tabloid", 279, 432),
]

# How far off nominal a page may be and still be called that size. Trim and
# bleed settings routinely shift a page by a millimetre or so.
SHEET_TOLERANCE_MM = 2.5

MM_PER_PT = 25.4 / 72.0

# ==============================================================================
# COLUMN DETECTION
# ==============================================================================
MIN_GUTTER_PT = 9.0        # narrower than this is word spacing, not a gutter
MIN_BODY_LINES = 12        # too little text to call the layout anything
MIN_COL_LINES = 4          # a column nobody wrote four lines in is not a column
MIN_COL_FRAC = 0.12        # ...nor one holding under this share of the page
MIN_COL_WIDTH_FRAC = 0.15  # ...nor one this thin relative to the text width
MAX_STRADDLE_FRAC = 0.18   # headings may cross a gutter; body text may not
BIN_PT = 1.0
NOISE_FRAC = 0.02          # x-bins touched by at most this share count as empty
MAX_REPORTED_COLUMNS = 4   # beyond four, report "4+" rather than a precise count


def human_size(num_bytes):
    """'12.4 MB' - the form a person reads, not 13002342."""
    try:
        n = float(num_bytes)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def classify_sheet(width_pt, height_pt):
    """
    Name the sheet a page is set on.

    Returns (name, orientation, exact) where exact is False when the match is
    within tolerance rather than spot on, and name is a "custom" description
    when nothing in the table fits.
    """
    w_mm = float(width_pt) * MM_PER_PT
    h_mm = float(height_pt) * MM_PER_PT
    orientation = "landscape" if w_mm > h_mm else "portrait"
    short, long_ = min(w_mm, h_mm), max(w_mm, h_mm)

    best, best_err = None, None
    for name, s, l in SHEET_SIZES_MM:
        err = max(abs(short - s), abs(long_ - l))
        if best_err is None or err < best_err:
            best, best_err = name, err

    if best_err is not None and best_err <= SHEET_TOLERANCE_MM:
        return best, orientation, best_err < 0.5
    return f"Custom {short:.0f}×{long_:.0f} mm", orientation, False


def _body_lines(page, margins):
    """
    Text line rectangles inside the live area.

    Everything outside the ignored margins is dropped first. That is not a
    detail: a running header spans the full width and would bridge the gutter of
    a genuinely two-column page, and a language thumb tab down the outer edge
    would invent a column of its own.
    """
    m = page_margins.normalize(margins, page.rect.width, page.rect.height)
    x0_lim, y0_lim, x1_lim, y1_lim = page_margins.content_box(
        page.rect.width, page.rect.height, m)

    out = []
    try:
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:               # 0 = text
                continue
            for line in block.get("lines", []):
                x0, y0, x1, y1 = line["bbox"]
                if y1 <= y0_lim or y0 >= y1_lim:
                    continue
                if x1 <= x0_lim or x0 >= x1_lim:
                    continue
                if x1 - x0 < 1:
                    continue
                out.append((x0, y0, x1, y1))
    except Exception:
        pass
    return out


def _table_rects(page):
    """Table bounding boxes, whose internal gaps must not read as gutters."""
    rects = []
    try:
        found = page.find_tables()
        for t in found.tables:
            rects.append(fitz.Rect(t.bbox))
    except Exception:
        pass
    return rects


def _outside_tables(lines, tables):
    if not tables:
        return lines
    keep = []
    for (x0, y0, x1, y1) in lines:
        r = fitz.Rect(x0, y0, x1, y1)
        if any(fitz.Rect(t.x0 - 2, t.y0 - 2, t.x1 + 2, t.y1 + 2).intersects(r)
               for t in tables):
            continue
        keep.append((x0, y0, x1, y1))
    return keep


def detect_columns(page, margins=None, exclude_tables=True):
    """
    How many text columns this page runs.

    Returns (count, gutters, note). `gutters` are (x_from, x_to) pairs in points
    and `note` says how the answer was reached, which is what makes a surprising
    number checkable rather than merely surprising.
    """
    lines = _body_lines(page, margins)
    if exclude_tables:
        tables = _table_rects(page)
        lines = _outside_tables(lines, tables)
    else:
        tables = []

    if len(lines) < MIN_BODY_LINES:
        return 1, [], f"{len(lines)} body line(s) - too few to judge"

    left = min(l[0] for l in lines)
    right = max(l[2] for l in lines)
    span = right - left
    if span < 60:
        return 1, [], "text spans too little of the page to judge"

    nbins = max(1, int(span / BIN_PT) + 1)
    cover = [0] * nbins
    for x0, _y0, x1, _y1 in lines:
        a = max(0, int((x0 - left) / BIN_PT))
        b = min(nbins - 1, int((x1 - left) / BIN_PT))
        for i in range(a, b + 1):
            cover[i] += 1

    thresh = int(len(lines) * NOISE_FRAC)
    empty = [c <= thresh for c in cover]

    # Runs of empty bins strictly inside the text span are candidate gutters.
    candidates, i = [], 0
    while i < nbins:
        if not empty[i]:
            i += 1
            continue
        j = i
        while j < nbins and empty[j]:
            j += 1
        if i > 0 and j < nbins and (j - i) * BIN_PT >= MIN_GUTTER_PT:
            candidates.append((left + i * BIN_PT, left + j * BIN_PT))
        i = j

    if not candidates:
        return 1, [], f"{len(lines)} body lines, no gutter over {MIN_GUTTER_PT:.0f}pt"

    # A candidate is only a column boundary when both sides stand on their own.
    edges = [left] + [(a + b) / 2 for a, b in candidates] + [right]
    kept, rejected = [], []
    for k, (ga, gb) in enumerate(candidates):
        lo, hi = edges[k], edges[k + 2]
        mid = (ga + gb) / 2
        l_lines = [ln for ln in lines if ln[0] >= lo - 1 and ln[2] <= mid]
        r_lines = [ln for ln in lines if ln[0] >= mid and ln[2] <= hi + 1]
        straddle = sum(1 for ln in lines if ln[0] < mid < ln[2])

        why = None
        if min(len(l_lines), len(r_lines)) < MIN_COL_LINES:
            why = f"only {min(len(l_lines), len(r_lines))} line(s) on one side"
        elif min(len(l_lines), len(r_lines)) / len(lines) < MIN_COL_FRAC:
            why = "one side holds too little of the text"
        elif min(mid - lo, hi - mid) / span < MIN_COL_WIDTH_FRAC:
            why = "one side is too narrow to be a column"
        elif straddle / len(lines) > MAX_STRADDLE_FRAC:
            why = f"{straddle} line(s) cross it"

        if why:
            rejected.append((ga, gb, why))
        else:
            kept.append((ga, gb))

    count = len(kept) + 1
    note = f"{len(lines)} body lines"
    if tables:
        note += f", {len(tables)} table(s) excluded"
    if kept:
        note += ", gutters at " + ", ".join(f"{a:.0f}-{b:.0f}pt" for a, b in kept)
    if rejected:
        note += "; ignored " + "; ".join(f"{a:.0f}-{b:.0f}pt ({w})"
                                         for a, b, w in rejected[:3])
    return count, kept, note


def columns_label(count):
    """'2 column' / '4+ column', for a table cell."""
    if count is None:
        return "-"
    if count >= MAX_REPORTED_COLUMNS:
        return f"{MAX_REPORTED_COLUMNS}+ column" if count > MAX_REPORTED_COLUMNS \
            else f"{count} column"
    return f"{count} column"


# ==============================================================================
# PER PAGE / PER DOCUMENT
# ==============================================================================

def page_metadata(page, page_no, margins=None, exclude_tables=True):
    """Everything worth knowing about one page."""
    w, h = page.rect.width, page.rect.height
    sheet, orientation, exact = classify_sheet(w, h)
    cols, gutters, note = detect_columns(page, margins, exclude_tables)
    return {
        "page": page_no,
        "sheet": sheet,
        "sheet_exact": exact,
        "orientation": orientation,
        "width_pt": round(w, 1),
        "height_pt": round(h, 1),
        "size_mm": f"{w * MM_PER_PT:.0f} × {h * MM_PER_PT:.0f} mm",
        "rotation": page.rotation,
        "columns": cols,
        "columns_label": columns_label(cols),
        "gutters": gutters,
        "note": note,
    }


def _dominant(values):
    """The most common value, and whether it was unanimous."""
    if not values:
        return None, True
    tally = {}
    for v in values:
        tally[v] = tally.get(v, 0) + 1
    best = max(tally, key=lambda k: (tally[k], k))
    return best, len(tally) == 1


def _mixed_label(values, dominant, unanimous, fmt=str):
    if unanimous or dominant is None:
        return fmt(dominant)
    others = len([v for v in values if v != dominant])
    return f"{fmt(dominant)}  (+{others} other)"


def pdf_metadata(path, margins=None, exclude_tables=True, max_pages=None,
                 progress=None):
    """
    Collect the metadata for one PDF.

    `max_pages` samples the first N pages instead of reading all of them, which
    matters only for very long documents; the page count and file size are always
    exact. `progress(done, total)` is called as pages are analysed.
    """
    row = {
        "path": path,
        "filename": os.path.basename(path),
        "file_size": None,
        "file_size_label": "-",
        "pages": 0,
        "sheet": "-",
        "orientation": "-",
        "size_mm": "-",
        "columns_label": "-",
        "toc_entries": 0,
        "encrypted": False,
        "pdf_version": "-",
        "producer": "-",
        "title": "-",
        "page_rows": [],
        "error": "",
    }

    try:
        row["file_size"] = os.path.getsize(path)
        row["file_size_label"] = human_size(row["file_size"])
    except OSError as e:
        row["error"] = f"could not stat the file: {e}"

    try:
        with fitz.open(path) as doc:
            row["pages"] = len(doc)
            row["encrypted"] = bool(doc.is_encrypted)
            try:
                row["toc_entries"] = len(doc.get_toc(simple=True) or [])
            except Exception:
                row["toc_entries"] = 0
            info = doc.metadata or {}
            row["producer"] = (info.get("producer") or "-").strip() or "-"
            row["title"] = (info.get("title") or "-").strip() or "-"
            row["pdf_version"] = (info.get("format") or "-").strip() or "-"

            last = len(doc) if not max_pages else min(len(doc), max_pages)
            for i in range(last):
                row["page_rows"].append(
                    page_metadata(doc[i], i + 1, margins, exclude_tables))
                if progress:
                    progress(i + 1, last)
    except Exception as e:
        row["error"] = (row["error"] + "; " if row["error"] else "") + str(e)
        return row

    pages = row["page_rows"]
    if pages:
        sheets = [p["sheet"] for p in pages]
        dom_sheet, uni_sheet = _dominant(sheets)
        row["sheet"] = _mixed_label(sheets, dom_sheet, uni_sheet)

        orients = [p["orientation"] for p in pages]
        dom_o, uni_o = _dominant(orients)
        row["orientation"] = _mixed_label(orients, dom_o, uni_o)

        row["size_mm"] = next((p["size_mm"] for p in pages
                               if p["sheet"] == dom_sheet), "-")

        cols = [p["columns"] for p in pages]
        dom_c, uni_c = _dominant(cols)
        row["columns_label"] = _mixed_label(cols, dom_c, uni_c, columns_label)
        row["column_tally"] = {c: cols.count(c) for c in sorted(set(cols))}

    return row


def collect(paths, margins=None, exclude_tables=True, max_pages=None,
            progress=None):
    """
    Metadata for a list of PDFs, in the order given.

    `progress(index, total, filename)` fires as each document is finished, so a
    scan of a dozen manuals can report where it has got to.
    """
    rows = []
    total = len(paths)
    for i, p in enumerate(paths, start=1):
        rows.append(pdf_metadata(p, margins=margins, exclude_tables=exclude_tables,
                                 max_pages=max_pages))
        if progress:
            progress(i, total, os.path.basename(p))
    return rows


# ==============================================================================
# WHAT THE INTERFACE SHOWS
# ==============================================================================
# (key, heading, width, anchor). Add a row here and the tab grows a column.

SUMMARY_COLUMNS = [
    ("filename", "PDF", 300, "w"),
    ("file_size_label", "File Size", 90, "center"),
    ("pages", "Pages", 60, "center"),
    ("sheet", "Sheet", 130, "center"),
    ("orientation", "Orientation", 100, "center"),
    ("size_mm", "Dimensions", 120, "center"),
    ("columns_label", "Columns", 130, "center"),
    ("toc_entries", "TOC Entries", 90, "center"),
]

PAGE_COLUMNS = [
    ("page", "Page", 55, "center"),
    ("sheet", "Sheet", 90, "center"),
    ("orientation", "Orientation", 95, "center"),
    ("size_mm", "Dimensions", 115, "center"),
    ("columns_label", "Columns", 95, "center"),
    ("note", "How it was measured", 460, "w"),
]
