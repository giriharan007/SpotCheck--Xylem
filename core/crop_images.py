import os
import re
import sys
from collections import Counter
import argparse
import cv2
import numpy as np
import pymupdf as fitz  # PyMuPDF (aliased as fitz for API compat)
import pdfplumber

from core import barcode_qr as Barcode_QR_Check
from core import docscan
from core import margins as page_margins

# ==============================================================================
# CONFIGURATION - EDIT YOUR INPUT AND OUTPUT PATHS HERE DIRECTLY
# ==============================================================================
INPUT_PATH = r"Input"                 # Path to a single PDF file OR a directory containing PDFs
OUTPUT_DIR = r"Output_Cropped_Images"    # Directory where cropped element images will be saved
DPI = 150                             # Image resolution DPI (e.g. 150, 300)
MASK_TEXT_IN_CROPS = False             # Mask text characters inside graphic crops to compare pure visual graphics
# ==============================================================================

# Margins are a property of the stylesheet, not of this file: they are set in
# the Region Inspector's margin editor and saved into the template, then passed
# down here as a dict. These two names survive only so the command-line entry
# point and any older caller keep working; core.margins.DEFAULT_MARGINS is the
# single source of truth for their values.
HEADER_MARGIN = page_margins.DEFAULT_MARGINS["header"]   # top band, points
FOOTER_MARGIN = page_margins.DEFAULT_MARGINS["footer"]   # bottom band, points

# ==============================================================================
# TOPIC GROUPING (used when the document has a table of contents)
# ==============================================================================
# Crops are filed under the TOC topic they sit beneath when the PDF has an
# outline, and under their page number when it does not. The image itself and
# its per-page index are identical either way - only the folder changes - so a
# crop keeps the same name whether or not the document happens to have a TOC,
# and results stay comparable across documents.

_TOPIC_SLUG_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def extract_topics_with_positions(pdf_path):
    """
    TOC entries with the page and vertical position each one starts at.

    Falls back to the headings printed on the page when the document carries no
    outline - a translation that came back from DTP without its bookmarks still
    prints "4.7.3", and reading it there keeps the graphic filed under the same
    topic as its master counterpart. Returns [] only when neither is available,
    which is the signal to fall back to page-wise cropping.

    The bookmark is trusted for the PAGE and the printed heading for the
    POSITION on it. A bookmark destination is frequently generic - a default
    Point(72, 36) is common, which converts to near the foot of the page - and
    taking that literally puts the topic's start below everything on its own
    first page. Every graphic there then reads as sitting BEFORE the topic and
    is filed under front matter, which quietly empties the topics the whole
    comparison pairs on.
    """
    topics = []
    heights = {}
    raw_ys = []
    try:
        with fitz.open(pdf_path) as doc:
            toc = doc.get_toc(simple=False)
            for item in toc:
                level, title, page = item[0], item[1], item[2]
                if page is None or page < 1:
                    continue
                top_y = 0.0
                if len(item) > 3 and isinstance(item[3], dict):
                    pt = item[3].get("to")
                    if pt is not None:
                        try:
                            # TOC destinations are bottom-up; convert to top-down.
                            page_h = float(doc[page - 1].rect.height)
                            heights[int(page)] = page_h
                            top_y = page_h - float(pt.y)
                            raw_ys.append(round(float(pt.y), 1))
                        except Exception:
                            top_y = 0.0
                topics.append({
                    "level": level,
                    "title": (title or "").strip(),
                    "start_page": int(page),
                    "top_y": top_y,
                })
    except Exception as e:
        print(f"  [TOC] Could not read outline: {e}")
        return []

    if not topics:
        from core import toc as _toc
        topics = list(_toc.topics_from_text(pdf_path))
    else:
        page_h = next(iter(heights.values()), 0.0)
        if _destinations_are_generic(raw_ys, page_h):
            for t in topics:
                t["top_y"] = 0.0
        _correct_topic_positions(pdf_path, topics, heights)
        _enforce_bookmark_order(topics)

    topics.sort(key=lambda t: (t["start_page"], t["top_y"]))
    return topics


def _enforce_bookmark_order(topics):
    """
    Keep the outline's own sequence, whatever the positions say.

    The outline lists sections in document order, and that order is reliable
    even where the position on the page is not. A topic whose position could
    not be recovered is parked at the top of its page, which is safe only if
    nothing else starts on that page - and on a dense manual something usually
    does. 4.7.6.3 landed at y=0 on the page where 4.7.6.2 starts at y=271, so
    it sorted in FRONT of it, and every graphic between them was filed one
    section out: 26 of the 31 crops in 4.7.6.2 were attributed to 4.7.6.3 in
    one language and not the other, so nothing in either paired.

    Nudging a topic to just after its predecessor rather than trusting a
    fabricated position costs nothing - the sections are still in the right
    order, which is all the pairing needs - and cannot reorder them.
    """
    prev = None
    for t in topics:                      # still in outline order here
        here = (t["start_page"], t["top_y"])
        if prev is not None and here < prev:
            if t["start_page"] < prev[0]:
                t["start_page"] = prev[0]
            t["top_y"] = prev[1] + 0.1
            here = (t["start_page"], t["top_y"])
        prev = here


# When this share of the outline points at the SAME spot on the page, the
# destinations are a default rather than real positions...
_GENERIC_DEST_SHARE = 0.8

# ...but only if that spot is this far down the page. A repeated destination at
# the TOP is what a manual full of sections that start on a fresh page actually
# looks like, and believing it costs nothing.
_GENERIC_DEST_DEPTH = 0.5


def _destinations_are_generic(raw_ys, page_height=0.0):
    """
    True when the outline's destinations are a boilerplate value, not positions.

    Whether a destination can be believed is not something a single entry can
    answer. Judging them one at a time - "anything below three quarters of the
    page must be a default" - threw away positions that were perfectly good,
    because a subsection routinely starts near the foot of a page: 4.7.6.3
    begins at 0.79 of its page and 4.7.5.4 at 0.86. Zeroing those parked them
    at the top of their page ahead of sections that really do start there, and
    every graphic in between was filed one section out.

    Tightening the cutoff cannot work either - a generic Point(72, 36) lands at
    about 0.945, which is nearer 0.86 than any threshold can safely split.

    What actually distinguishes them is repetition. A real outline points at a
    different spot for nearly every entry; a generated one points every entry at
    the same place. In the 3315 manual the commonest destination covers 19% of
    the outline, and its positions agree with the printed headings to within a
    point. A generated outline covers 100%.
    """
    if len(raw_ys) < 2:
        return False
    value, count = Counter(raw_ys).most_common(1)[0]
    if (count / float(len(raw_ys))) < _GENERIC_DEST_SHARE:
        return False

    # Repetition on its own is not enough, and neither is position. Plenty of
    # real sections start at the top of a page, so a destination repeated there
    # is both common and harmless - trusting it files nothing wrongly. The
    # damaging case is a repeated destination LOW on the page, which would put
    # every topic below the content it owns. Only that combination is treated
    # as boilerplate.
    if not page_height:
        return True
    return ((page_height - value) / page_height) > _GENERIC_DEST_DEPTH


def _correct_topic_positions(pdf_path, topics, heights):
    """
    Replace unusable bookmark positions with where the heading is actually printed.

    The printed heading is ground truth for position: "4.6 Install the pump" is
    set at the top of its section whatever the bookmark says. Where the printed
    scan finds the same code on the same page, its y wins; where it does not, an
    implausible position is pulled back to the top of the page rather than left
    at the foot of it.
    """
    from core import toc as _toc

    printed = {}
    try:
        for t in _toc.topics_from_text(pdf_path):
            m = _toc.TOPIC_CODE.match(t.get("title") or "")
            if m:
                printed[(m.group(1), t["start_page"])] = t["top_y"]
    except Exception:
        printed = {}

    for t in topics:
        m = _toc.TOPIC_CODE.match(t.get("title") or "")
        code = m.group(1) if m else None
        hit = printed.get((code, t["start_page"])) if code else None
        if hit is not None:
            t["top_y"] = float(hit)
            continue
        # Nothing else to do: with a real outline the destination stands, and
        # with a generated one it has already been cleared.


# Device names Windows reserves whatever the extension. A topic will almost
# never be called one, but a folder named CON cannot be created at all.
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def topic_slug(topic, max_len=60):
    """
    A filesystem-safe folder name for a topic.

    The trailing strip has to happen AFTER truncation, and the truncation marker
    cannot be "...". That combination produced a folder ending in dots, which is
    the one thing a Windows directory name must not do, and it failed in a way
    that took a moment to see: Win32 strips trailing dots from the LAST
    component of a path but not from intermediate ones. So os.makedirs created
    "…arrangement with" while the subsequent image save opened
    "…arrangement with...\\p033_crop_01_image.png", where the dotted name is no
    longer last and is therefore taken literally. The directory it named did not
    exist, MuPDF returned errno 2, and a 33-page manual aborted mid-run.
    """
    if not topic:
        # Sorts to the top of the output folder, which is where the cover
        # belongs, and says what it holds rather than what it lacks.
        return "_Front matter"
    name = topic.get("title") or "Untitled"
    name = _TOPIC_SLUG_BAD.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()

    if len(name) > max_len:
        name = name[:max_len].rstrip() + "~"     # "~" is legal at the end; "." is not

    # Last, and only now: Windows silently drops trailing dots and spaces from a
    # directory name, so a name that ends in one never matches the path we then
    # try to write into.
    name = name.rstrip(". ")

    if not name:
        return "Untitled"
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = "_" + name
    return name


# Windows refuses a path over 260 characters unless long paths are enabled, and
# a deep output folder plus a long PDF name plus a long topic title gets there
# more easily than it looks.
MAX_PATH_CHARS = 240


def safe_crop_path(directory, filename, page_num):
    """
    A path that can actually be written.

    Shortens the topic folder rather than the filename, because the filename
    carries the page and index that make a crop identifiable, and falls back to
    a plain page folder when even that is not enough.
    """
    full = os.path.join(directory, filename)
    if len(os.path.abspath(full)) <= MAX_PATH_CHARS:
        return full

    parent, leaf = os.path.split(directory)
    room = MAX_PATH_CHARS - len(os.path.abspath(parent)) - len(filename) - 2
    if room >= 12:
        trimmed = leaf[:room].rstrip(". ") or f"page_{page_num:03d}"
        return os.path.join(parent, trimmed, filename)
    return os.path.join(parent, f"page_{page_num:03d}", filename)


def find_topic_for_rect(page_num, rect, topics):
    """
    The last topic that begins at or above this rect, in reading order.

    None for anything that comes BEFORE the first topic - the cover, the legal
    notice, the contents list. That used to be attributed to topic 1, and it was
    not a harmless mislabel: the topic decides where the comparison looks for
    the graphic in the translation. The Xylem logo on the cover was filed under
    "1 Introduction", topic 1 starts on page 6, so the search covered pages 5-7,
    did not find it, widened across the whole document, and matched the SAME
    logo on the back cover - reporting the cover graphic as "moved to page 118"
    at 97%. Front matter is not part of topic 1, and saying so puts the search
    back on page 1 where the graphic actually is.
    """
    if not topics:
        return None
    pos = (page_num, rect.y0 + rect.height * 0.3)
    best = None
    for t in topics:
        if (t["start_page"], t["top_y"]) <= pos:
            best = t
        else:
            break
    return best


# A region that renders essentially empty carries nothing to compare, and a
# blank crop matches anything at 100%. Real content never trips this: across the
# master and all eleven translations of the Start 350 manual, zero candidates
# fall below the threshold. It does catch a graphic that has been painted over,
# which a purely geometric check would still count as present.
BLANK_INK_THRESHOLD = 0.002

# A pixel counts as ink below this grey level. is_blank_region has always used
# this value; naming it lets the ruling test below measure the same ink.
INK_LEVEL = 220


# A rendered crop with less variation than this carries no shape a template
# match could ever find. Pure white measures 0.0; the faintest real hairline on
# a white ground measures well above 1.
MIN_CROP_VARIATION = 1.0


# The cropper renders a little outside each candidate so a stroke on the
# boundary is not clipped, and will not write anything smaller than this once
# that padding is in. Both live here because the count check has to make the
# same two decisions - see survives_text_masking.
CROP_PAD_PT = 2.0
MIN_CROP_SIDE_PT = 5.0


def padded_crop_rect(rect, page_rect, pad=CROP_PAD_PT):
    """The rect the cropper actually renders: padded, and clamped to the page."""
    return fitz.Rect(max(0, rect.x0 - pad),
                     max(0, rect.y0 - pad),
                     min(page_rect.width, rect.x1 + pad),
                     min(page_rect.height, rect.y1 + pad))


def survives_text_masking(page, rect, dpi=DPI, pad=CROP_PAD_PT):
    """
    True when a candidate still holds ink once the text inside it is masked.

    is_blank_region looks at the page BEFORE the text is masked, so a rect
    holding nothing but a caption passes it. The cropper then masks the text,
    renders the rect white and drops it - but the count check had already
    counted it. That gap is why "Images" and "Image Counts" disagreed about how
    many graphics a page holds, and why they disagreed by a different amount in
    every language: how much text sits in a region is precisely what
    translation changes. On a page of terminal-connection labels - PT100, FLS10,
    CT, TH - that is most of the candidates on the page.

    The caller must already have masked the text inside every candidate on the
    page, exactly as crop_pdf_elements does before it renders any of them.
    """
    r = padded_crop_rect(rect, page.rect, pad)
    if r.width <= MIN_CROP_SIDE_PT or r.height <= MIN_CROP_SIDE_PT:
        return False
    try:
        pix = page.get_pixmap(dpi=dpi, clip=r)
        if _is_blank_pixmap(pix):
            return False
        return not _is_ruling_not_artwork(pix, r, float(page.rect.width), dpi)
    except Exception:
        return True          # unreadable: keep it rather than silently drop it


def _is_ruling_not_artwork(pix, rect, page_width, dpi=DPI):
    """
    True when a rule-dominated crop is grid rather than a drawing.

    Rule dominance alone is not enough to decide. Measured on these manuals:

        data plate, Start 350 p9      0.71 rules   - artwork, must be kept
        Ex/FM/CSA plates, 3315 3.7    0.25-0.36    - artwork, never in doubt
        two empty table cells, 1.5.2  0.90         - grid, must go
        leader-line strip, 3.7        0.75         - grid, must go

    So the plates and the grid overlap on that number alone, and an earlier
    attempt to separate them on width let both of the last two through - they
    are narrow. Three things distinguish grid instead: it runs the width of
    the text column, or it is nothing BUT rules, or it is a thin strip. A plate
    is none of those - it is a self-contained box with a logo or a symbol in
    it, which is exactly the ink that keeps it below RULE_PURE.
    """
    if not _is_mostly_ruling(pix, dpi=dpi):
        return False

    w_pt, h_pt = float(rect.width), float(rect.height)
    if page_width and (w_pt / page_width) >= RULING_MIN_WIDTH_FRAC:
        return True                       # as wide as the column: the table
    if min(w_pt, h_pt) < RULING_MIN_SIDE_PT:
        return True                       # a strip of ruling, not a figure
    return _rule_fraction(pix, dpi=dpi) >= RULE_PURE


def _rule_fraction(pix, dpi=DPI):
    """How much of a crop's ink is long straight lines, 0..1."""
    try:
        a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    except Exception:
        return 0.0
    gray = a[:, :, 0] if pix.n == 1 else cv2.cvtColor(a[:, :, :3], cv2.COLOR_RGB2GRAY)
    ink = (gray < INK_LEVEL).astype(np.uint8)
    total = int(ink.sum())
    if not total:
        return 0.0
    min_len_px = max(3, int(round(RULE_MIN_LENGTH_PT * dpi / 72.0)))
    if min_len_px > max(ink.shape):
        return 0.0
    horiz = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                             cv2.getStructuringElement(cv2.MORPH_RECT, (min_len_px, 1)))
    vert = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (1, min_len_px)))
    return int(cv2.bitwise_or(horiz, vert).sum()) / float(total)


def _is_mostly_ruling(pix, dpi=DPI):
    """
    True when a crop is a piece of table grid rather than a picture.

    _strip_rules already empties a ruled region while it is still a mask, so
    a text-only table normally produces no candidate at all. It only strips
    where it is confident the region IS a grid, though, and a fragment that
    the grid finder never framed - a couple of columns off the side of a
    parts table, say - comes through as a candidate and is then compared as
    if it were artwork. Row heights move with the length of the translated
    text, so that fragment mismatches on every correct translation.

    This is the same dominance test, applied once more to the finished crop:
    if what is left after text masking is overwhelmingly long straight lines,
    it is grid. A hazard icon is curves and short strokes and is never close
    to the threshold, and on a crop too small to contain a rule of the
    minimum length nothing is detected at all, so small symbols are untouched.
    """
    try:
        a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    except Exception:
        return False
    gray = a[:, :, 0] if pix.n == 1 else cv2.cvtColor(
        a[:, :, :3], cv2.COLOR_RGB2GRAY)

    ink = (gray < INK_LEVEL).astype(np.uint8)
    total = int(ink.sum())
    if not total:
        return False

    min_len_px = max(3, int(round(RULE_MIN_LENGTH_PT * dpi / 72.0)))
    if min_len_px > max(ink.shape):
        return False          # too small to hold a rule: nothing to judge

    # A rule is not just long, it is THIN. A filled disc or a solid arrow head
    # is full of long horizontal runs and would otherwise read as ruling, so
    # anything that survives an erode is treated as a solid shape and the crop
    # is kept. A 1pt table line is about 2px here and does not survive at all.
    solid = cv2.erode(ink, np.ones((5, 5), np.uint8))
    if int(solid.sum()) / float(total) > SOLID_SHAPE_FRACTION:
        return False

    horiz = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                             cv2.getStructuringElement(cv2.MORPH_RECT, (min_len_px, 1)))
    vert = cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (1, min_len_px)))
    rules = cv2.bitwise_or(horiz, vert)
    return (int(rules.sum()) / float(total)) >= RULE_DOMINANCE


def _is_blank_pixmap(pix, min_std=MIN_CROP_VARIATION):
    """True when a rendered crop holds no ink worth comparing."""
    try:
        a = np.frombuffer(pix.samples, dtype=np.uint8)
        if a.size == 0:
            return True
        return float(a.std()) < min_std
    except Exception:
        return False        # unreadable: keep it rather than silently drop it


def is_blank_region(page, rect, dpi=110, threshold=BLANK_INK_THRESHOLD):
    """True when almost nothing is drawn inside rect."""
    try:
        r = fitz.Rect(rect) & page.rect
        if r.is_empty or r.width < 2 or r.height < 2:
            return True
        pix = page.get_pixmap(dpi=dpi, clip=r)
        a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        gray = a[:, :, 0] if pix.n == 1 else a[:, :, :3].mean(axis=2)
        return float((gray < 220).mean()) < threshold
    except Exception:
        return False        # if it cannot be measured, keep it


def get_table_bboxes(fitz_page, pdfplumber_page=None):
    """
    Get table bounding boxes using PyMuPDF and pdfplumber.

    Kept for callers that hold their own page objects. The run itself goes
    through core.docscan, which does the same work once per document and in
    parallel: on a 92-page manual this function costs about two seconds a page,
    and it used to be called on every page of every pass.
    """
    table_rects = []
    try:
        tabs = fitz_page.find_tables()
        for t in tabs.tables:
            table_rects.append(fitz.Rect(t.bbox))
    except Exception:
        pass

    try:
        if pdfplumber_page:
            p_tables = pdfplumber_page.find_tables()
            for t in p_tables:
                table_rects.append(fitz.Rect(t.bbox))
    except Exception:
        pass

    return table_rects

def merge_rects_tight(rect_list, gap=4):
    """
    Tightly merges bounding boxes that intersect or touch (gap <= 4pt),
    or are vertically aligned sub-parts of a single icon (such as an icon and its underline bar).
    Does NOT group distinct icons or horizontally separate figures together.
    """
    rects = [fitz.Rect(r) for r in rect_list]
    changed = True
    while changed:
        changed = False
        new_rects = []
        visited = [False] * len(rects)
        for i in range(len(rects)):
            if visited[i]:
                continue
            cur = fitz.Rect(rects[i])
            visited[i] = True
            for j in range(i + 1, len(rects)):
                if visited[j]:
                    continue
                rj = rects[j]
                
                # Check direct intersection / tiny margin
                exp = fitz.Rect(cur.x0 - gap, cur.y0 - gap, cur.x1 + gap, cur.y1 + gap)
                should_merge = exp.intersects(rj)
                
                # Check vertical multi-part alignment of a single icon (e.g. WEEE bin + horizontal bar)
                if not should_merge:
                    x_overlap = min(cur.x1, rj.x1) - max(cur.x0, rj.x0)
                    min_w = min(cur.width, rj.width)
                    if min_w > 0 and (x_overlap / min_w) > 0.5:
                        y_gap = max(0, max(cur.y0, rj.y0) - min(cur.y1, rj.y1))
                        if y_gap <= 10:
                            should_merge = True
                            
                if should_merge:
                    cur.include_rect(rj)
                    visited[j] = True
                    changed = True
            new_rects.append(cur)
        rects = new_rects
    return rects


def merge_figure_components(rect_list, max_gap=16.0, barrier_page=None, scale=None,
                            grid_regions=None, page=None):
    """
    Merges bounding boxes that intersect, overlap, or are aligned sub-components
    of a single technical illustration or diagram (e.g. pump casing, baseplate,
    motor, mounting feet, and straps in Figure 1).
    Does NOT group distant icons or graphics separated by substantial white space,
    nor does it merge across table row dividers, barrier rules, or grid boundaries.
    """
    rects = [fitz.Rect(r) for r in rect_list]
    changed = True

    lines = []
    if page is not None:
        try:
            for d in page.get_drawings():
                r = fitz.Rect(d['rect'])
                if (r.height <= 2.5 and r.width >= 15) or (r.width <= 2.5 and r.height >= 15):
                    lines.append(r)
        except Exception:
            lines = []

    pw = page.rect.width if page is not None else 419.527
    edge_touch = page_margins.EDGE_TOUCH
    max_edge_w = pw * EDGE_ARTIFACT_MAX_WIDTH

    def is_edge(r):
        if r.width > max_edge_w:
            return False
        return r.x0 <= edge_touch or r.x1 >= pw - edge_touch

    def has_barrier(r1, r2):
        # 0. Edge artifacts (thumb tabs / crop marks) must never merge with interior graphics
        if is_edge(r1) or is_edge(r2):
            return True

        # 1. Check raster barrier page if available
        if barrier_page is not None and scale is not None:
            # Vertical gap
            y_top = int(min(r1.y1, r2.y1) * scale)
            y_bot = int(max(r1.y0, r2.y0) * scale)
            x_l = int(max(r1.x0, r2.x0) * scale)
            x_r = int(min(r1.x1, r2.x1) * scale)
            if y_bot > y_top and x_r > x_l:
                if barrier_page[y_top:y_bot, x_l:x_r].any():
                    return True
            # Horizontal gap
            x_l = int(min(r1.x1, r2.x1) * scale)
            x_r = int(max(r1.x0, r2.x0) * scale)
            y_top = int(max(r1.y0, r2.y0) * scale)
            y_bot = int(min(r1.y1, r2.y1) * scale)
            if x_r > x_l and y_bot > y_top:
                if barrier_page[y_top:y_bot, x_l:x_r].any():
                    return True

        # 2. Check vector divider lines
        if lines:
            # Vertical separation
            y_top = min(r1.y1, r2.y1)
            y_bot = max(r1.y0, r2.y0)
            if y_bot > y_top:
                x_l = max(r1.x0, r2.x0)
                x_r = min(r1.x1, r2.x1)
                for l in lines:
                    if l.height <= 2.5:
                        ly = (l.y0 + l.y1) / 2.0
                        if y_top - 1.0 <= ly <= y_bot + 1.0:
                            if l.x0 <= x_l + 10.0 and l.x1 >= x_r - 10.0:
                                return True
            # Horizontal separation
            x_l = min(r1.x1, r2.x1)
            x_r = max(r1.x0, r2.x0)
            if x_r > x_l:
                y_top = max(r1.y0, r2.y0)
                y_bot = min(r1.y1, r2.y1)
                for l in lines:
                    if l.width <= 2.5:
                        lx = (l.x0 + l.x1) / 2.0
                        if x_l - 1.0 <= lx <= x_r + 1.0:
                            if l.y0 <= y_top + 10.0 and l.y1 >= y_bot - 10.0:
                                return True

        # 3. Check table / grid regions
        if grid_regions:
            for g in grid_regions:
                g_rect = fitz.Rect(g)
                if r1 in g_rect and r2 in g_rect:
                    # Inside a table, non-intersecting graphics belong to separate cells/rows
                    return True

        # 4. Standalone small icons vertically stacked in a column
        if (r1.width <= 60 and r1.height <= 60 and r2.width <= 60 and r2.height <= 60
                and abs(r1.width - r2.width) < 10 and abs(r1.x0 - r2.x0) < 10):
            return True

        return False

    while changed:
        changed = False
        out = []
        used = [False] * len(rects)
        for i in range(len(rects)):
            if used[i]:
                continue
            cur = fitz.Rect(rects[i])
            used[i] = True
            for j in range(i + 1, len(rects)):
                if used[j]:
                    continue
                rj = rects[j]
                intersects = cur.intersects(rj)
                h_overlap = min(cur.x1, rj.x1) - max(cur.x0, rj.x0)
                v_overlap = min(cur.y1, rj.y1) - max(cur.y0, rj.y0)

                should_merge = False
                if intersects:
                    should_merge = True
                else:
                    if not has_barrier(cur, rj):
                        if h_overlap > 0 and v_overlap >= -max_gap:
                            # Horizontally overlapping with vertical proximity
                            should_merge = True
                        elif v_overlap > 0 and h_overlap >= -max_gap:
                            # Vertically overlapping with horizontal proximity
                            should_merge = True
                        elif h_overlap >= -2.0 and v_overlap >= -2.0:
                            # Tight corner or diagonal touch
                            should_merge = True

                if should_merge:
                    cur.include_rect(rj)
                    used[j] = True
                    changed = True
            out.append(cur)
        rects = out
    return rects


def resolve_margins(margins=None, header_margin=None, footer_margin=None, page=None):
    """
    Settle on one margins dict from whichever form the caller supplied.

    `margins` (the stylesheet template's block) wins; the two legacy scalars are
    honoured when it is absent, so the CLI and any older caller still work.
    """
    if margins is not None:
        base = dict(margins)
    else:
        base = dict(page_margins.DEFAULT_MARGINS)
    if header_margin is not None:
        base["header"] = header_margin
    if footer_margin is not None:
        base["footer"] = footer_margin
    if page is not None:
        return page_margins.normalize(base, page.rect.width, page.rect.height)
    return page_margins.normalize(base)


# ==============================================================================
# GROUPING A PAGE'S INK INTO WHOLE FIGURES
# ==============================================================================
# What counts as "one graphic" cannot be decided from the drawing operators. An
# exploded parts diagram is hundreds of separate vector paths with white space
# between them; a table grid is a few dozen long rules. Merging paths that touch
# gave the wrong answer in both directions on this manual:
#
#   page 17  one exploded pump diagram came out as 19 boxes, several of them
#            single table cells, plus two empty boxes in the left margin
#   page 15  five illustrations came out as five WRONG boxes - part 1 was missed
#            entirely, parts 3 and 4 were each cut in half
#
# What a reviewer calls one figure is a connected region of ink once the text is
# taken away. So that is what is measured: the page is rendered, the text is
# painted out, the remaining ink is dilated by the gap that should still count
# as "the same picture", and each connected region becomes one candidate. The
# leader lines of an exploded diagram then hold it together exactly as they do
# for the eye, and five illustrations that share nothing stay five.
#
# It is also cheaper than walking the drawing list: about 0.05s a page against
# 0.18s, because one render replaces thousands of rectangle intersections.

# The resolution the ink is measured at. Enough to resolve a hairline; small
# enough that the morphology is free.
INK_DPI = 100

# White space narrower than this still reads as one picture. Set from the manual:
# the widest internal gap in an exploded diagram that a reviewer treats as one
# figure is around 8pt, and the narrowest gap BETWEEN two illustrations that must
# stay separate is around 20pt.
CLUSTER_GAP_PT = 10.0

# Rules at least this long are table or page furniture, not drawing strokes.
RULE_MIN_LENGTH_PT = 26.0

# An element touching the trim is furniture only if it is also narrow relative
# to the sheet. A twelfth of A4 is 50pt - comfortably more than the 47pt thumb
# tab, comfortably less than any figure, which spans most of the text block.
EDGE_ARTIFACT_MAX_WIDTH = 0.12

# How much of the sheet a run of ruling must span before it counts as page
# furniture. A quarter is comfortably below a separator rule across the text
# column, and far above a single barcode bar.
MIN_FURNITURE_SPAN = 0.25

# A cluster made this much of long straight strokes is ruling, not artwork.
# A table cell outline measures 1.0; a hazard triangle, whose sides are
# diagonal, measures 0; the densest line drawing in the manual measures 0.42.
RULE_ONLY_FRACTION = 0.85

# ...and for a small cluster, where a partial cell border is common. Measured:
# leftover cell fragment 564pt2 at 0.80, cover barcode 2958pt2 at 0.64, hazard
# triangle 1213pt2 at 0.29.
SMALL_RULE_AREA_PT2 = 1000.0
SMALL_RULE_FRACTION = 0.60

# A region is only treated as ruling if this much of its ink is long straight
# lines. A parts table measures around 0.9; an exploded diagram around 0.2.
RULE_DOMINANCE = 0.55

# Ink that survives a 5x5 erode is a solid shape, not ruling. Above this much
# of it, a crop is artwork whatever its long runs suggest - a filled disc is
# nothing but long horizontal runs, and is not a table.
SOLID_SHAPE_FRACTION = 0.25

# A crop this wide is the page's table rather than something sitting inside a
# cell of it. The text column in these manuals is about 0.70 of the page.
RULING_MIN_WIDTH_FRAC = 0.55

# Ink this completely made of straight lines is grid and nothing else. A plate
# or a boxed figure always carries something that is not a rule - a logo, a
# curve, a symbol - and lands well below this even when it reads as heavily
# ruled overall.
RULE_PURE = 0.85

# Narrower than this on its shorter side, a rule-dominated crop is a strip of
# ruling or a leader line, not a figure. Genuine small symbols are not ruled at
# all and never reach this test.
RULING_MIN_SIDE_PT = 24.0

# How many rule crossings make a lattice. Four is the smallest real table - one
# cell has four corners - and it is well above what a drawing produces, where a
# leader line meeting a centreline gives one or two.
MIN_GRID_JUNCTIONS = 4


def _ink_mask(fitz_page, dpi=INK_DPI):
    """
    The page's ink with the text taken out, as a binary image.

    The page object is not touched. Masking by painting white rectangles onto
    the page would work, but it would also change what every later stage sees;
    the text is erased from the rendered pixels instead.
    """
    pix = fitz_page.get_pixmap(dpi=dpi)
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 1:
        gray = a[:, :, 0].copy()
    else:
        gray = cv2.cvtColor(np.ascontiguousarray(a[:, :, :3]), cv2.COLOR_RGB2GRAY)

    scale = dpi / 72.0
    h, w = gray.shape
    try:
        for b in fitz_page.get_text("dict").get("blocks", []):
            if b.get("type") != 0:
                continue                      # type 1 is an image block: keep it
            for line in b.get("lines", []):
                for span in line.get("spans", []):
                    x0, y0, x1, y1 = span["bbox"]
                    # A pixel of bleed each way: glyph outlines round outwards,
                    # and a surviving stem would tie a caption to the figure.
                    gray[max(0, int(y0 * scale) - 1):min(h, int(y1 * scale) + 2),
                         max(0, int(x0 * scale) - 1):min(w, int(x1 * scale) + 2)] = 255
    except Exception:
        pass

    return (gray < 245).astype(np.uint8), scale


def _thin_rules(mask, min_len_px, max_thick_px=None):
    """
    Long straight strokes that are actually thin.

    The thickness test is not fussiness. A horizontal opening asks "is there an
    unbroken run of ink this wide", and a solid black disc 50px across answers
    yes for every row through its middle - so the hazard pictogram on page 11
    was classified as ruling and most of it removed, leaving an 18pt sliver
    where a 50pt icon should have been. A rule is long AND thin; a picture is
    long and thick.
    """
    if max_thick_px is None:
        max_thick_px = max(3, int(round(min_len_px * 0.18)))

    out = np.zeros_like(mask)
    for kernel, axis in (((min_len_px, 1), "h"), ((1, min_len_px), "v")):
        band = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_RECT, kernel))
        if not band.any():
            continue
        count, labels, stats, _c = cv2.connectedComponentsWithStats(band, 8)
        keep = [i for i in range(1, count)
                if (stats[i][3] if axis == "h" else stats[i][2]) <= max_thick_px]
        if keep:
            out = cv2.bitwise_or(out, np.isin(labels, keep).astype(np.uint8))
    return out


# How much of a region's width a horizontal rule must span before it counts as
# a row divider. A table's row rule runs wall to wall; a horizontal stroke
# inside an illustration - the body of the cable in "Prepare the SUBCAB
# cables" - spans only part of its cell.
# A row divider need only span the columns it separates, not the whole table.
ROW_DIVIDER_MIN_SPAN = 0.40

# Long against thick. A table rule measures 25:1 and up even where a glyph
# thickens it; a horizontal stroke inside a drawing measures well under 20:1.
ROW_DIVIDER_ASPECT = 20.0

ROW_DIVIDER_SPAN = 0.7


def _row_dividers(region_ink, min_len_px, min_span_frac=ROW_DIVIDER_SPAN):
    """
    The horizontal rules that genuinely divide a table's rows, or None.

    Used as a barrier so the clustering dilation cannot bridge two icons in
    adjacent rows, which means it must find table ruling and nothing else. A
    plain horizontal opening does not: it answers yes for every row through the
    middle of any thick horizontal drawing, the same trap _thin_rules was
    written to avoid. The body of the cable in figure 4.7.3 is an 18pt-deep
    black run, so it was read as a row divider, dilated into a barrier and
    erased - which cut the illustration in half and left the crop covering only
    the fanned-out conductors beside it.

    A real divider is therefore required to be BOTH thin, like a rule, and to
    span the region, like a row divider. A drawing's horizontal stroke fails
    one test or the other.
    """
    band = cv2.morphologyEx(region_ink, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (min_len_px, 1)))
    if not band.any():
        return None

    max_thick = max(3, int(round(min_len_px * 0.18)))
    span_px = min_span_frac * region_ink.shape[1]

    # A rule running the FULL width of the table is a row divider whatever its
    # measured thickness. The thickness test exists to keep a thick horizontal
    # stroke inside a drawing from being read as a divider, and such a stroke
    # never spans the whole table - but a real divider does pick up a few
    # pixels where a glyph or an icon happens to sit against it, and rejecting
    # it for that left two rows joined. Those two then clustered together and
    # came back as one graphic: five hazard symbols counted as three.
    full_span_px = 0.95 * region_ink.shape[1]
    full_span_thick = max(max_thick, int(round(min_len_px * 0.5)))

    # A divider does not have to cross the whole table. In the 1.2 symbols
    # table the rules separating the rows run only across the columns they
    # divide - 205px of a 410px region, exactly half - so the 70% span test
    # threw both of them away, the rows they separated merged into one blob,
    # and five hazard symbols came out as three. English hit this and Dutch did
    # not, purely because the shorter English text makes the rows tighter.
    #
    # What actually tells a rule from a stroke inside a drawing is how long it
    # is against how thick: these dividers measure 41 and 26 to one, while the
    # triangle bases rejected alongside them measure 13. Length alone would not
    # do it, and thickness alone already failed - one of the two dividers picks
    # up a few pixels where a glyph sits against it.
    aspect_span_px = ROW_DIVIDER_MIN_SPAN * region_ink.shape[1]

    def _is_divider(st):
        _x, _y, w, h = st[0], st[1], st[2], st[3]
        if w < aspect_span_px:
            return False                       # too short to divide anything
        if w >= span_px and h <= max_thick:
            return True                        # thin and long: plainly a rule
        if w >= full_span_px and h <= full_span_thick:
            return True                        # spans the table: a rule regardless
        return h > 0 and (w / float(h)) >= ROW_DIVIDER_ASPECT

    count, labels, stats, _c = cv2.connectedComponentsWithStats(band, 8)
    keep = [i for i in range(1, count) if _is_divider(stats[i])]
    if not keep:
        return None
    return np.isin(labels, keep).astype(np.uint8)


def _strip_isolated_rules(mask, min_len_px):
    """
    Remove long straight rules that touch nothing else.

    A page separator - the line above and below a DANGER block - is a graphic to
    a clustering algorithm and furniture to a reader. Leaving them in cost two
    things on the Start 350 manual: each pair of rules became a candidate in its
    own right (a 292x6pt "graphic"), and a rule running past a hazard pictogram
    merged with it, stretching a 63pt icon box across the full text column.
    Worse, where the rules fall depends on how long the translated text is, so
    the count differed in every language and the symmetric count check failed on
    all eleven for a reason that had nothing to do with the translations.

    Isolation is the whole test, and it is what makes this safe. A separator
    touches nothing once the text is masked away. A leader line in an exploded
    diagram touches the part it points at - that is its job - so it is attached,
    and attached rules are left exactly where they are. Without that distinction
    this would take an exploded diagram apart, which is the failure the ink
    clustering was written to fix.
    """
    rules = _thin_rules(mask, min_len_px)
    if rules is None or not rules.any():
        return mask

    # Ink that is not part of a long rule.
    #
    # The rule's own anti-aliased fringe has to go with it. A 2pt rule renders
    # as a solid core plus a grey row either side; the core is what the opening
    # finds, and the fringe is not, so it lands in "other" one pixel away and
    # every rule on the page reports itself as touching something. That is
    # exactly what happened first time: fifteen rules, fifteen "attached", and
    # nothing stripped.
    rule_fringe = cv2.dilate(rules, np.ones((5, 5), np.uint8))
    other = cv2.bitwise_and(mask, cv2.bitwise_not(rule_fringe))
    if not other.any():
        return cv2.bitwise_and(mask, cv2.bitwise_not(rules))
    near_other = cv2.dilate(other, np.ones((5, 5), np.uint8))

    count, labels, stats, _c = cv2.connectedComponentsWithStats(rules, 8)
    attached = np.unique(labels[(near_other > 0) & (rules > 0)])
    attached = set(int(v) for v in attached if v != 0)

    # Furniture also has to SPAN something. A barcode is fifty thin vertical
    # strokes, each one long enough to pass the stroke test and touching
    # nothing, so an isolation test alone ate most of the barcode on the cover
    # and left a different handful of fragments in every language - which is
    # the very instability this was meant to remove. A separator rule runs
    # across the text column and a table grid spans its whole table; a single
    # barcode bar spans 5% of the page.
    h, w = mask.shape
    min_span = MIN_FURNITURE_SPAN * max(h, w)
    drop_labels = [i for i in range(1, count)
                   if i not in attached
                   and (stats[i][2] >= min_span or stats[i][3] >= min_span)]
    if not drop_labels:
        return mask

    isolated = np.isin(labels, drop_labels)

    drop = cv2.dilate(isolated.astype(np.uint8), np.ones((3, 3), np.uint8))
    return cv2.bitwise_and(mask, cv2.bitwise_not(drop))


def _grid_regions(mask, min_len_px):
    """
    Where the page carries a ruled grid, found from the ink alone.

    A table is the one thing on a technical page that draws long horizontal AND
    long vertical rules crossing each other repeatedly in a small area. An
    exploded diagram draws long lines too - leader lines, shaft centrelines -
    but they do not form a lattice.

    Deriving this from the ink rather than from a table finder is worth a great
    deal: pdfplumber and PyMuPDF between them cost about two seconds a page on
    this manual, they disagree, and on page 16 pdfplumber returned a "table"
    wider than the sheet that swallowed the diagram. This costs about five
    milliseconds and cannot return a region that has no rules in it.

    Returns a list of (x0, y0, x1, y1) pixel boxes.
    """
    horiz = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                             cv2.getStructuringElement(cv2.MORPH_RECT, (min_len_px, 1)))
    vert = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (1, min_len_px)))
    if not horiz.any() or not vert.any():
        return []

    # Where a long horizontal and a long vertical rule meet. The dilation gives
    # each rule a little width so that a T-junction counts even when the two
    # strokes stop a pixel short of touching.
    reach = np.ones((5, 5), np.uint8)
    crossings = cv2.bitwise_and(cv2.dilate(horiz, reach), cv2.dilate(vert, reach))
    if not crossings.any():
        return []

    # Group by the LATTICE, not by how close the crossings are to each other.
    # Grouping crossings was the first attempt and it does not work: in a
    # two-column table the corners sit 200pt apart, so they came out as four
    # separate clumps of one or two junctions each, none of them recognised as
    # a table and none of them stripped. The rules of a table, though, all touch
    # - that is what makes it a table - so one connected run of ruling is one
    # lattice however wide the cells are.
    lattice = cv2.dilate(cv2.bitwise_or(horiz, vert), np.ones((3, 3), np.uint8))
    count, labels, stats, _c = cv2.connectedComponentsWithStats(lattice, 8)

    regions = []
    for i in range(1, count):
        x, y, w, h, _area = stats[i]
        # Distinct crossings inside this run of ruling: two lines meeting in a
        # drawing give one, a grid gives one per cell corner.
        junctions = cv2.connectedComponentsWithStats(
            cv2.bitwise_and(crossings, (labels == i).astype(np.uint8)), 8)[0] - 1
        if junctions < MIN_GRID_JUNCTIONS:
            continue
        # A lattice smaller than a couple of cells each way is a pair of drawing
        # lines that happen to cross, not a table.
        if w < min_len_px * 1.5 or h < min_len_px * 1.5:
            continue
        regions.append((x, y, x + w, y + h))
    return regions


def _strip_rules(mask, region, min_len_px):
    """
    Erase long straight rules inside one region of the mask.

    Used on table interiors only. A table whose cells hold nothing but text is
    all rules, so this empties it and it produces no candidate - which is right,
    because a table's row heights change with the length of the translated text,
    and cropping the grid would report a difference on every correct
    translation. A table cell that holds a real picture keeps that picture,
    because a drawing is not made of 26pt straight lines.
    """
    x0, y0, x1, y1 = region
    sub = mask[y0:y1, x0:x1]
    if sub.size == 0:
        return
    horiz = cv2.morphologyEx(sub, cv2.MORPH_OPEN,
                             cv2.getStructuringElement(cv2.MORPH_RECT, (min_len_px, 1)))
    vert = cv2.morphologyEx(sub, cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (1, min_len_px)))
    rules = cv2.bitwise_or(horiz, vert)

    # Only strip where the ink really is a grid. A figure misread as a table is
    # mostly curves and short strokes, and cutting its long lines would break
    # one drawing into a dozen pieces - which is the failure this whole rewrite
    # exists to remove, so it must not be reintroduced here by a bad table rect.
    ink = int(sub.sum())
    if ink and (int(rules.sum()) / float(ink)) < RULE_DOMINANCE:
        return

    # Grow the rule by a pixel before removing it, so its anti-aliased edge goes
    # with it rather than being left behind as a hairline ghost.
    rules = cv2.dilate(rules, np.ones((3, 3), np.uint8))
    cleaned = cv2.bitwise_and(sub, cv2.bitwise_not(rules))

    # Where two rules crossed, a few pixels survive both passes. A 2px speck in
    # a cell corner is enough to become a candidate and then a meaningless crop,
    # so an opening clears it. A picture inside a cell is many pixels thick and
    # comes through untouched.
    mask[y0:y1, x0:x1] = cv2.morphologyEx(cleaned, cv2.MORPH_OPEN,
                                          np.ones((3, 3), np.uint8))


def ink_clusters(fitz_page, table_rects=None, gap_pt=CLUSTER_GAP_PT, dpi=INK_DPI,
                 return_grids=False):
    """
    The page's graphics as whole figures: one rect per connected region of ink.

    `table_rects` is an optional hint from a table finder; the ruled regions are
    found in the ink either way. With return_grids the ruled regions come back
    too, as PDF-point rects, which is what tells the caller whether a crop came
    out of a table.
    """
    mask, scale = _ink_mask(fitz_page, dpi=dpi)
    rule_px = max(3, int(round(RULE_MIN_LENGTH_PT * scale)))

    # Where the ruled regions are, measured before anything is removed. These
    # are used to LABEL a crop as coming out of a table; they are no longer used
    # to decide what to remove.
    regions = list(_grid_regions(mask, rule_px))
    for t in (table_rects or []):
        r = fitz.Rect(t) & fitz_page.rect
        if r.is_empty:
            continue
        regions.append((max(0, int(r.x0 * scale) - 2), max(0, int(r.y0 * scale) - 2),
                        min(mask.shape[1], int(r.x1 * scale) + 2),
                        min(mask.shape[0], int(r.y1 * scale) + 2)))

    # One rule handles all ruling: a long thin stroke that touches nothing else
    # is furniture and goes. That covers a table grid (its rules touch only each
    # other), a separator above a hazard block, and the line under a running
    # header, without a region test.
    #
    # Stripping by region was the earlier design and it took the page-11 hazard
    # pictogram apart: the rules above and below it made the area read as a
    # lattice, the whole region was stripped, and a 61pt black disc came out as
    # an 18pt sliver of the white arrow inside it. Asking about each stroke
    # rather than each area cannot make that mistake.
    ink_before = mask.copy()
    mask = _strip_isolated_rules(mask, rule_px)

    k = max(3, int(round(gap_pt * scale)) | 1)          # odd, so it is centred

    # Inside a table, icons in adjacent rows can sit close enough that the
    # dilation bridges them into one cluster - the hazard symbols on a 1.2
    # "Safety terminology and symbols" page, where the row gap is smaller than
    # the clustering kernel. The row divider between them is used to keep the
    # rows apart, found from the ORIGINAL ink because by this point the rule
    # itself has been taken out as furniture.
    #
    # The cut is THIN, and it is made on the DILATED mask, not on the ink.
    # Widening it to the clustering kernel and subtracting it from the ink -
    # which is what this did - eats the icons it is supposed to be separating:
    # in a 40pt row a 34pt triangle came out 17pt tall, and the bottom rows
    # merged into the table ruling and were then dropped as ruling, so five
    # symbols were counted as three. Nothing needs to be removed from the ink
    # at all; a full-width line cut through the grown mask separates the rows
    # on its own, and leaves every icon whole.
    barrier_page = np.zeros_like(mask)
    for rx0, ry0, rx1, ry1 in regions:
        sub = ink_before[ry0:ry1, rx0:rx1]
        if sub.size == 0:
            continue
        horiz = _row_dividers(sub, rule_px)
        if horiz is None:
            continue
        # Just wide enough to cover the rule and its anti-aliased edge, so a
        # cluster cannot span it and no icon loses more than a pixel.
        thin = cv2.dilate(horiz, np.ones((3, 1), np.uint8))
        region_slice = mask[ry0:ry1, rx0:rx1]
        mask[ry0:ry1, rx0:rx1] = cv2.bitwise_and(region_slice,
                                                 cv2.bitwise_not(thin))
        np.maximum(barrier_page[ry0:ry1, rx0:rx1], thin,
                   out=barrier_page[ry0:ry1, rx0:rx1])

    grown = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    if barrier_page.any():
        grown = cv2.bitwise_and(grown, cv2.bitwise_not(barrier_page))
    count, _labels, stats, _cent = cv2.connectedComponentsWithStats(grown, 8)

    pad = (k - 1) / 2.0                                  # undo the dilation
    out = []
    for i in range(1, count):
        x, y, w, h, _area = stats[i]
        out.append(fitz.Rect((x + pad) / scale, (y + pad) / scale,
                             (x + w - pad) / scale, (y + h - pad) / scale))

    # Two regions can be separate ink and still sit one inside the other - an
    # inset detail drawn in the white space of a larger diagram, for instance.
    # Cropping both means comparing the same picture twice and reporting it
    # twice, so the enclosed one goes.
    # Anything that is only ruling is not a graphic.
    #
    # Stripping handles a rule that stands alone, but a table's own rules touch
    # each other, and where the lattice breaks into pieces some of them are too
    # short to look like furniture and cluster into a "graphic" - an empty cell
    # of the Condition/Action table on page 14 came out as a 102x53pt crop in
    # English and had no counterpart in any translation, which is what made
    # topic 3.5 fail in all eleven languages. Asking what a cluster is MADE OF
    # settles it: a cell outline is entirely straight strokes, a hazard triangle
    # and a pump drawing are not.
    rules_all = _thin_rules(ink_before, rule_px)
    def rule_only(rect):
        x0 = max(0, int(rect.x0 * scale)); y0 = max(0, int(rect.y0 * scale))
        x1 = min(ink_before.shape[1], int(rect.x1 * scale) + 1)
        y1 = min(ink_before.shape[0], int(rect.y1 * scale) + 1)
        ink = int(ink_before[y0:y1, x0:x1].sum())
        if ink <= 0:
            return True
        fraction = int(rules_all[y0:y1, x0:x1].sum()) / float(ink)
        if fraction >= RULE_ONLY_FRACTION:
            return True
        # A SMALL cluster does not have to be pure ruling to be a leftover
        # piece of a table. Two thresholds rather than one because the cover
        # barcode measures 0.64 - it is fifty short vertical strokes - and must
        # not be caught; it is five times the area of the biggest fragment.
        return (rect.width * rect.height) < SMALL_RULE_AREA_PT2 and fraction >= SMALL_RULE_FRACTION

    out = [r for r in out if not rule_only(r)]

    out.sort(key=lambda r: -(r.width * r.height))
    kept = []
    for r in out:
        area = max(1e-6, r.width * r.height)
        if any(((r & big).get_area() / area) > 0.85 for big in kept):
            continue
        kept.append(r)

    regions_pt = [fitz.Rect(x0 / scale, y0 / scale, x1 / scale, y1 / scale)
                  for x0, y0, x1, y1 in regions]
    kept = merge_figure_components(kept, barrier_page=barrier_page, scale=scale,
                                   grid_regions=regions_pt, page=fitz_page)

    if return_grids:
        return kept, regions_pt
    return kept


def get_all_image_candidates(fitz_page, header_margin=None, footer_margin=None,
                             margins=None, include_ignored=False,
                             table_rects=None, use_ink_clustering=True,
                             return_grids=False):
    """
    Bounding boxes of all raster images, logos, and vector icons/diagrams/figures.

    Elements are clustered into whole graphics FIRST, and only then judged
    against the ignored margin bands and the edge-artifact rule. The order
    matters and used to be the other way round, which broke in two ways at once
    on the cover of these manuals:

      - the masthead is a logo plus a long sweeping rule that clusters into one
        element crossing the header line. Its fragments were each judged alone,
        so only the ones wholly inside the band went, and the survivors merged
        into a graphic that had never been asked the question - the whole
        masthead was then cropped and compared as artwork.
      - worse, removing those fragments changed what was left to cluster with,
        so a stray 32x12pt piece of the same masthead came out as a graphic in
        its own right.

    Judging the finished cluster answers both, and is also simply the right
    question: it is the cluster that becomes a crop.

    With include_ignored=True the return value becomes a list of
    (rect, ignored_by) pairs instead of bare rects, where ignored_by names what
    removed the element, or None if it was kept. The margin editor uses that to
    show, live on the page, exactly which graphics the numbers currently being
    typed would throw away.
    """
    page_rect = fitz_page.rect
    m = resolve_margins(margins, header_margin, footer_margin, page=fitz_page)
    pw, ph = page_rect.width, page_rect.height

    def dropped_by(r):
        return page_margins.which_margin((r.x0, r.y0, r.x1, r.y1), pw, ph, m)

    # Kept separate from the margin test: this is the edge-furniture heuristic
    # that removes language thumb tabs and crop marks, and it is not the same
    # rule as "sits inside the side margin". Both run.
    #
    # The test is anchorage to the trim, not a fixed width. A thumb tab bleeds
    # to the paper edge; the body text block on these manuals is inset 35-40pt
    # and nothing inside it comes near. The old test also required the element
    # to be under 40pt wide, which the Xylem tab is not - it measures about 47 -
    # so the tab survived, and then survived the side margin too, and got
    # cropped and compared on every page of every language.
    def is_edge_artifact(r):
        if r.width > pw * EDGE_ARTIFACT_MAX_WIDTH:
            return False
        return (r.x0 <= page_margins.EDGE_TOUCH
                or r.x1 >= pw - page_margins.EDGE_TOUCH)

    ignored = []               # (rect, reason) - only collected when asked for
    content = page_margins.content_box(pw, ph, m)

    if use_ink_clustering:
        try:
            merged_rects, grids = ink_clusters(fitz_page, table_rects=table_rects,
                                               return_grids=True)
            result = _judge_candidates(merged_rects, pw, ph, dropped_by,
                                       is_edge_artifact, ignored, include_ignored,
                                       content=content, page=fitz_page,
                                       grid_regions=grids)
            if return_grids:
                return result, grids
            return result
        except Exception as e:
            # Rendering can fail on a damaged page. The path below is the older
            # one: worse groupings, but it needs nothing but the drawing list.
            print(f"  [candidates] ink clustering failed on page "
                  f"{fitz_page.number + 1} ({e}); using the vector path")

    raw_elements = []          # everything drawn, before clustering

    # 1. Raster images & logos
    try:
        for img in fitz_page.get_image_info():
            if 'bbox' in img:
                raw_elements.append(fitz.Rect(img['bbox']))
    except Exception:
        pass

    # 2. Image text blocks (type == 1)
    try:
        text_dict = fitz_page.get_text("dict")
        for b in text_dict.get("blocks", []):
            if b.get("type") == 1:
                raw_elements.append(fitz.Rect(b["bbox"]))
    except Exception:
        pass

    # 3. Vector drawings, schematics, hazard icons, figures. These three
    #    exclusions are intrinsic to the element - a hairline rule is never a
    #    graphic whatever else is on the page - so they apply before clustering.
    try:
        drawings = fitz_page.get_drawings()
        for d in drawings:
            r = fitz.Rect(d['rect'])
            # Ignore thin horizontal divider rules or vertical lines
            if (r.height <= 2.5 and r.width > 20) or (r.width <= 2.5 and r.height > 20):
                continue
            # Ignore full-page backgrounds / huge tint boxes
            if r.width * r.height > (pw * ph * 0.4):
                continue
            if r.width > 1 and r.height > 1:
                raw_elements.append(r)
    except Exception:
        pass

    if not raw_elements:
        empty = [] if not include_ignored else [(r, s) for r, s in ignored]
        return (empty, []) if return_grids else empty

    # Tight clustering to assemble vector paths into individual icons without merging separate graphics
    merged_rects = merge_rects_tight(raw_elements, gap=2)
    result = _judge_candidates(merged_rects, pw, ph, dropped_by, is_edge_artifact,
                               ignored, include_ignored, content=content,
                               page=fitz_page)
    return (result, []) if return_grids else result


def _judge_candidates(merged_rects, pw, ph, dropped_by, is_edge_artifact,
                      ignored, include_ignored, content=None,
                      page=None, grid_regions=None):
    """
    Decide which finished clusters survive: too small, in a margin, an edge
    artifact, or a graphic.

    Shared by both clustering paths, because the question asked of a cluster is
    the same however the cluster was assembled - and it is asked of the FINISHED
    cluster, never of its fragments. Judging fragments is what let the masthead
    through on the cover: only the pieces wholly inside the header band were
    dropped, and what was left merged into a graphic that had never been asked.

    `content` is the live area once the ignored bands are taken off. A cluster
    is trimmed to it before anything else is decided, because clustering runs
    on the whole page and happily joins a language thumb tab to whatever sits
    beside it. The joined cluster then reaches deep into the text block, so it
    is no longer furniture by any test, and the tab came back as part of a
    graphic however the side margin was set - which is not what someone who has
    just marked that margin expects. Trimming first means a marked band can
    never be part of a graphic, whether or not it managed to merge with one.
    """
    # Filter out tiny standalone noise artifacts (smaller than 12x12 and area < 140 pt^2)
    final_candidates = []
    cx0, cy0, cx1, cy1 = content if content else (0.0, 0.0, float(pw), float(ph))
    for r in merged_rects:
        if (r.width >= 12 and r.height >= 12) or (r.width * r.height >= 140):
            clamped = fitz.Rect(
                max(0, r.x0, cx0),
                max(0, r.y0, cy0),
                min(pw, r.x1, cx1),
                min(ph, r.y1, cy1)
            )
            if clamped.width <= 5 or clamped.height <= 5:
                if include_ignored:
                    ignored.append((clamped, "too small"))
                continue

            # The finished cluster, not its fragments, is what gets judged.
            side = dropped_by(clamped)
            if side is not None:
                if include_ignored:
                    ignored.append((clamped, side))
                continue
            if is_edge_artifact(clamped):
                if include_ignored:
                    ignored.append((clamped, "edge artifact"))
                continue

            final_candidates.append(clamped)
        elif include_ignored:
            ignored.append((r, "too small"))

    final_candidates = merge_figure_components(final_candidates,
                                               grid_regions=grid_regions,
                                               page=page)

    # Sort top to bottom, left to right
    final_candidates.sort(key=lambda r: (r.y0, r.x0))
    if include_ignored:
        return [(r, None) for r in final_candidates] + _merge_ignored(ignored)
    return final_candidates


def _merge_ignored(ignored):
    """
    Tidy the ignored list into something worth drawing on screen.

    A single ruled header band arrives here as dozens of individual vector
    paths. Outlining all of them turns the margin preview into red confetti and
    tells the user nothing, so each reason's rectangles are clustered the same
    way surviving candidates are, and the resulting slivers are dropped.
    """
    out = []
    by_reason = {}
    for r, reason in ignored:
        by_reason.setdefault(reason, []).append(r)
    for reason, rects in by_reason.items():
        for merged in merge_rects_tight(rects, gap=3):
            if merged.width >= 6 and merged.height >= 6:
                out.append((merged, reason))
    out.sort(key=lambda pair: (pair[0].y0, pair[0].x0))
    return out

def mask_text_inside_rect(page, rect):
    """
    Cover all text line spans inside rect with solid white to isolate pure visual graphics.
    """
    try:
        text_dict = page.get_text("dict")
        for b in text_dict.get("blocks", []):
            if b.get("type") == 0:  # Text block
                for line in b.get("lines", []):
                    l_rect = fitz.Rect(line["bbox"])
                    if rect.intersects(l_rect):
                        page.draw_rect(l_rect, color=(1, 1, 1), fill=(1, 1, 1))
    except Exception:
        pass

def detect_barcodes_and_qr_codes(page, dpi=150):
    """
    Delegate barcode and QR code detection to Barcode_QR_Check module.
    """
    return Barcode_QR_Check.detect_barcodes_and_qr_codes(page, dpi=dpi)

def _process_single_page_crop_job(args):
    """Process a single PDF page for pure graphic extraction. Thread-safe."""
    (pdf_path, page_num, total_pages, active_margins, output_dir, pdf_name,
     by_topic, topics, dpi, mask_text_in_crops, code_rects) = args
    page_idx = page_num - 1
    page_out_dir = os.path.join(output_dir, pdf_name, f"page_{page_num:03d}")
    page_manifest = []
    crops_saved_count = 0

    with fitz.open(pdf_path) as doc:
        page = doc[page_idx]
        page_rect = page.rect
        page_margins_here = page_margins.margins_for_page(active_margins, page_num, total_pages)
        image_rects, table_rects = get_all_image_candidates(
            page, margins=page_margins_here, return_grids=True)

        crops_on_page = []
        for img_rect in image_rects:
            is_barcode_or_qr = False
            for c_rect in (code_rects or []):
                padded_c = fitz.Rect(c_rect.x0 - 5, c_rect.y0 - 5, c_rect.x1 + 5, c_rect.y1 + 5)
                if padded_c.contains(img_rect) or padded_c.intersects(img_rect):
                    intersect = padded_c & img_rect
                    if intersect.width * intersect.height > 0:
                        is_barcode_or_qr = True
                        break

            if is_barcode_or_qr or is_blank_region(page, img_rect):
                continue

            in_table = False
            for t_rect in (table_rects or []):
                padded_t = fitz.Rect(t_rect.x0 - 2, t_rect.y0 - 2, t_rect.x1 + 2, t_rect.y1 + 2)
                if padded_t.contains(img_rect) or padded_t.intersects(img_rect):
                    in_table = True
                    break

            label = "table_image" if in_table else "image"
            crops_on_page.append((label, img_rect))

        if crops_on_page:
            if mask_text_in_crops:
                for _, rect in crops_on_page:
                    mask_text_inside_rect(page, rect)

            for crop_idx, (label, rect) in enumerate(crops_on_page, start=1):
                r_clamped = padded_crop_rect(rect, page_rect)
                if r_clamped.width > MIN_CROP_SIDE_PT and r_clamped.height > MIN_CROP_SIDE_PT:
                    crop_pix = page.get_pixmap(dpi=dpi, clip=r_clamped)
                    if not survives_text_masking(page, rect, dpi=dpi):
                        continue

                    if by_topic:
                        topic = find_topic_for_rect(page_num, rect, topics)
                        out_dir_for_crop = os.path.join(output_dir, pdf_name, topic_slug(topic))
                        crop_filename = f"p{page_num:03d}_crop_{crop_idx:02d}_{label}.png"
                        topic_title = (topic or {}).get("title", "")
                    else:
                        topic = None
                        out_dir_for_crop = page_out_dir
                        crop_filename = f"crop_{crop_idx:02d}_{label}.png"
                        topic_title = ""

                    crop_path = safe_crop_path(out_dir_for_crop, crop_filename, page_num)
                    try:
                        os.makedirs(os.path.dirname(crop_path), exist_ok=True)
                        crop_pix.save(crop_path)
                    except Exception:
                        fallback_dir = os.path.join(output_dir, pdf_name, f"page_{page_num:03d}")
                        try:
                            os.makedirs(fallback_dir, exist_ok=True)
                            crop_path = os.path.join(fallback_dir, crop_filename)
                            crop_pix.save(crop_path)
                        except Exception:
                            continue

                    page_manifest.append({
                        "page": page_num, "index": crop_idx, "label": label,
                        "topic": topic_title, "topic_obj": topic if by_topic else None,
                        "path": crop_path,
                    })
                    crops_saved_count += 1

    return page_num, page_manifest, crops_saved_count


def crop_pdf_elements(pdf_path, output_dir, dpi=DPI, header_margin=None, footer_margin=None,
                      mask_text_in_crops=MASK_TEXT_IN_CROPS, margins=None, progress=None):
    """
    Extract and crop pure inside images from PDF pages.
    """
    print(f"\n==================================================")
    print(f"Processing & Cropping Pure PDF Graphic Images: {pdf_path}")
    print(f"==================================================")
    doc = fitz.open(pdf_path)
    pdf_name = os.path.splitext(os.path.basename(pdf_path))[0]
    total_pages = len(doc)

    active_margins = resolve_margins(margins, header_margin, footer_margin)
    print(f"  Ignored margins: {page_margins.describe(active_margins)}"
          f"{'  (built-in default)' if page_margins.is_default(active_margins) else ''}")

    topics = extract_topics_with_positions(pdf_path)
    by_topic = bool(topics)
    print(f"  Grouping: {'topic-wise (' + str(len(topics)) + ' TOC entries)' if by_topic else 'page-wise (no TOC in this document)'}")
    manifest = []

    scan = docscan.scan(pdf_path)
    if scan is not None:
        if progress:
            progress(0, total_pages, "scanning pages")
        scan._ensure_codes(dpi=dpi, progress=progress)

    total_crops = 0

    try:
        workers = min(4, max(1, (os.cpu_count() or 2) - 1)) if total_pages >= 8 else 1
        jobs = []
        for p_idx in range(total_pages):
            p_no = p_idx + 1
            if scan is not None:
                c_rects = scan.code_rects(p_no, dpi=dpi)
            else:
                c_rects = [c["rect"] for c in detect_barcodes_and_qr_codes(doc[p_idx], dpi=dpi)]
            jobs.append((pdf_path, p_no, total_pages, active_margins, output_dir,
                         pdf_name, by_topic, topics, dpi, mask_text_in_crops, c_rects))

        if workers > 1:
            import concurrent.futures
            completed = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_process_single_page_crop_job, job): job[1] for job in jobs}
                for fut in concurrent.futures.as_completed(futures):
                    completed += 1
                    if progress:
                        progress(completed, total_pages, "cropping")
                    try:
                        p_num, p_man, p_count = fut.result()
                        manifest.extend(p_man)
                        total_crops += p_count
                        if p_count > 0:
                            where = "topic folders" if by_topic else "page folder"
                            print(f"  Page {p_num:3d}: Saved {p_count:2d} pure graphic crop(s) -> {where}")
                    except Exception as e:
                        print(f"  [WARN] Cropping failed for page: {e}")
            manifest.sort(key=lambda m: (m["page"], m["index"]))
        else:
            for p_idx, job in enumerate(jobs):
                p_num = p_idx + 1
                if progress:
                    progress(p_num, total_pages, "cropping")
                p_num, p_man, p_count = _process_single_page_crop_job(job)
                manifest.extend(p_man)
                total_crops += p_count
                if p_count > 0:
                    where = "topic folders" if by_topic else "page folder"
                    print(f"  Page {p_num:3d}: Saved {p_count:2d} pure graphic crop(s) -> {where}")
    finally:
        doc.close()

    print(f"Saved total of {total_crops} cropped pure graphic files to: {os.path.join(output_dir, pdf_name)}")
    if by_topic:
        used = sorted({m["topic"] for m in manifest if m["topic"]})
        print(f"  Filed under {len(used)} topic folder(s)\n")
    else:
        print()

    try:
        from core import image_counts
        keys = image_counts._topic_keys(topics) if topics else {}
        index_of = {id(t): i for i, t in enumerate(topics)} if topics else {}
        by_topic_dict, titles, topic_pages = {}, {}, {}
        for idx, t in enumerate(topics or []):
            k = keys[idx]
            titles[k] = t.get("title", "")
            if t.get("page"):
                topic_pages[k] = t.get("page")

        for m in manifest:
            if topics:
                t = m.get("topic_obj")
                if t is None:
                    k = image_counts.FRONT_MATTER_KEY
                    titles.setdefault(k, "(front matter, before topic 1)")
                else:
                    k = keys.get(index_of.get(id(t), -1), "?")
                by_topic_dict[k] = by_topic_dict.get(k, 0) + 1

        count_model = {
            "filename": os.path.basename(pdf_path),
            "has_toc": bool(topics),
            "topic_count": len(topics or []),
            "by_topic": by_topic_dict,
            "titles": titles,
            "topic_pages": topic_pages,
            "total": total_crops,
        }
        image_counts.record_model(pdf_path, count_model, margins=active_margins)
    except Exception as e:
        print(f"  [WARN] Could not cache image count model from crops: {e}")

    return total_crops

def process_input_dir(input_dir, output_dir, dpi=DPI, header_margin=None, footer_margin=None,
                      margins=None):
    """
    Recursively process all PDFs in input_dir.
    """
    pdf_files = []
    for root, _, files in os.walk(input_dir):
        for file in files:
            if file.lower().endswith('.pdf'):
                pdf_files.append(os.path.join(root, file))

    if not pdf_files:
        print(f"No PDF files found in: {input_dir}")
        return

    print(f"Found {len(pdf_files)} PDF file(s) in {input_dir}")
    total = 0
    for pdf_path in pdf_files:
        total += crop_pdf_elements(pdf_path, output_dir, dpi=dpi, header_margin=header_margin,
                                   footer_margin=footer_margin, margins=margins)
    print(f"Finished cropping graphic files. Total crops saved: {total}")

def main():
    parser = argparse.ArgumentParser(description="Crop inside pure graphic images from PDF pages (masking text inside crops).")
    parser.add_argument("--input", default=INPUT_PATH, help=f"Input PDF file or directory (default: {INPUT_PATH})")
    parser.add_argument("--output", default=OUTPUT_DIR, help=f"Output directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dpi", type=int, default=DPI, help=f"DPI resolution (default: {DPI})")
    parser.add_argument("--header", type=float, default=HEADER_MARGIN, help=f"Top margin to ignore, pt (default: {HEADER_MARGIN:g})")
    parser.add_argument("--footer", type=float, default=FOOTER_MARGIN, help=f"Bottom margin to ignore, pt (default: {FOOTER_MARGIN:g})")
    parser.add_argument("--left", type=float, default=page_margins.DEFAULT_MARGINS["left"], help="Left margin to ignore, pt (default: 0)")
    parser.add_argument("--right", type=float, default=page_margins.DEFAULT_MARGINS["right"], help="Right margin to ignore, pt (default: 0)")
    parser.add_argument("--template", default=None, help="Take the margins from this saved stylesheet template instead")

    args = parser.parse_args()

    if args.template:
        from core import templates as templates_store
        data = templates_store.load_template(args.template) or {}
        chosen = page_margins.normalize(data.get("margins"))
        print(f"[Margins] From template '{args.template}': {page_margins.describe(chosen)}")
    else:
        chosen = page_margins.from_values(args.header, args.footer, args.left, args.right)

    if os.path.isfile(args.input):
        crop_pdf_elements(args.input, args.output, dpi=args.dpi, margins=chosen)
    elif os.path.isdir(args.input):
        process_input_dir(args.input, args.output, dpi=args.dpi, margins=chosen)
    else:
        print(f"Error: Input path does not exist: {args.input}")

if __name__ == "__main__":
    main()
