import os
import re
import sys
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
MASK_TEXT_IN_CROPS = True             # Mask text characters inside graphic crops to compare pure visual graphics
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

    Returns [] when the document has no outline, which is the signal to fall
    back to page-wise cropping.
    """
    topics = []
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
                            top_y = float(doc[page - 1].rect.height - pt.y)
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
    topics.sort(key=lambda t: (t["start_page"], t["top_y"]))
    return topics


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


# A rendered crop with less variation than this carries no shape a template
# match could ever find. Pure white measures 0.0; the faintest real hairline on
# a white ground measures well above 1.
MIN_CROP_VARIATION = 1.0


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

def merge_rects_tight(rect_list, gap=2):
    """
    Tightly merges bounding boxes that intersect or touch (gap <= 2pt),
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
    # dilation bridges them into one cluster - the DANGER and WARNING triangles
    # on page 6 of the Start 350 manual, where the row gap after rule removal
    # is smaller than the clustering kernel.  Erasing the horizontal row
    # dividers (found from the ORIGINAL ink, before any stripping) acts as a
    # barrier: the dilation cannot cross a line that is no longer there.
    # Only horizontal rules are erased, because they separate rows; vertical
    # column dividers are left alone (icons in the same row but different
    # columns are already far enough apart).
    for rx0, ry0, rx1, ry1 in regions:
        sub = ink_before[ry0:ry1, rx0:rx1]
        if sub.size == 0:
            continue
        horiz = cv2.morphologyEx(sub, cv2.MORPH_OPEN,
                                 cv2.getStructuringElement(cv2.MORPH_RECT, (rule_px, 1)))
        if not horiz.any():
            continue
        # The barrier is slightly thicker than the clustering kernel so that
        # ink on opposite sides of a row divider cannot touch after dilation.
        barrier = cv2.dilate(horiz, np.ones((max(3, k + 2), 1), np.uint8))
        region_slice = mask[ry0:ry1, rx0:rx1]
        mask[ry0:ry1, rx0:rx1] = cv2.bitwise_and(region_slice,
                                                  cv2.bitwise_not(barrier))
    grown = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
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

    if return_grids:
        return kept, [fitz.Rect(x0 / scale, y0 / scale, x1 / scale, y1 / scale)
                      for x0, y0, x1, y1 in regions]
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

    if use_ink_clustering:
        try:
            merged_rects, grids = ink_clusters(fitz_page, table_rects=table_rects,
                                               return_grids=True)
            result = _judge_candidates(merged_rects, pw, ph, dropped_by,
                                       is_edge_artifact, ignored, include_ignored)
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
                               ignored, include_ignored)
    return (result, []) if return_grids else result


def _judge_candidates(merged_rects, pw, ph, dropped_by, is_edge_artifact,
                      ignored, include_ignored):
    """
    Decide which finished clusters survive: too small, in a margin, an edge
    artifact, or a graphic.

    Shared by both clustering paths, because the question asked of a cluster is
    the same however the cluster was assembled - and it is asked of the FINISHED
    cluster, never of its fragments. Judging fragments is what let the masthead
    through on the cover: only the pieces wholly inside the header band were
    dropped, and what was left merged into a graphic that had never been asked.
    """
    # Filter out tiny standalone noise artifacts (smaller than 12x12 and area < 140 pt^2)
    final_candidates = []
    for r in merged_rects:
        if (r.width >= 12 and r.height >= 12) or (r.width * r.height >= 140):
            clamped = fitz.Rect(
                max(0, r.x0),
                max(0, r.y0),
                min(pw, r.x1),
                min(ph, r.y1)
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

def crop_pdf_elements(pdf_path, output_dir, dpi=DPI, header_margin=None, footer_margin=None,
                      mask_text_in_crops=MASK_TEXT_IN_CROPS, margins=None, progress=None):
    """
    Extract and crop pure inside images from PDF pages.

    Layout depends on the document: when the PDF has a table of contents the
    crops are filed under the topic they belong to, otherwise under their page.
    The crop image and its per-page index are the same either way, so the two
    layouts stay directly comparable:

        with TOC   <pdf>/1.3 User safety/p007_crop_01_image.png
        without    <pdf>/page_007/crop_01_image.png

    Other behaviour:
    - Text characters inside graphic crops are masked with white to isolate pure graphics (logos, lines, boxes, shapes, icons).
    - Images inside tables are labeled as 'table_image' (e.g. crop_01_table_image.png)
    - Standalone images outside tables are labeled as 'image' (e.g. crop_02_image.png)
    - Standard text-only tables are NOT extracted as crops.
    - Barcodes and QR Codes are excluded and NOT cropped as image files.
    """
    print(f"\n==================================================")
    print(f"Processing & Cropping Pure PDF Graphic Images: {pdf_path}")
    print(f"==================================================")
    doc = fitz.open(pdf_path)
    pdf_name = os.path.splitext(os.path.basename(pdf_path))[0]

    active_margins = resolve_margins(margins, header_margin, footer_margin)
    print(f"  Ignored margins: {page_margins.describe(active_margins)}"
          f"{'  (built-in default)' if page_margins.is_default(active_margins) else ''}")

    topics = extract_topics_with_positions(pdf_path)
    by_topic = bool(topics)
    print(f"  Grouping: {'topic-wise (' + str(len(topics)) + ' TOC entries)' if by_topic else 'page-wise (no TOC in this document)'}")
    manifest = []

    # Tables and codes come from the shared document scan: computed once for
    # this file, page-parallel, and reused by the count and barcode stages
    # instead of each of them paying for the same page again.
    # Codes come from the shared document scan. Tables do not: the ruled regions
    # are found in each page's own ink as the graphics are grouped, which is
    # both free and more reliable than a table finder on a page full of
    # engineering drawings.
    scan = docscan.scan(pdf_path)
    plumb_doc = None
    if scan is not None:
        if progress:
            progress(0, len(doc), "scanning pages")
        scan._ensure_codes(dpi=dpi, progress=progress)

    total_crops = 0

    try:
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            page_rect = page.rect
            page_num = page_idx + 1
            if progress:
                progress(page_num, len(doc), "cropping")

            page_out_dir = os.path.join(output_dir, pdf_name, f"page_{page_num:03d}")  # page-wise fallback

            if scan is not None:
                code_rects = scan.code_rects(page_num, dpi=dpi)
            else:
                code_rects = [c["rect"] for c in detect_barcodes_and_qr_codes(page, dpi=dpi)]

            # Margins can be switched off on named pages (a cover that is
            # deliberately all furniture, say), so they are resolved per page.
            page_margins_here = page_margins.margins_for_page(
                active_margins, page_num, len(doc))
            # The ruled regions come back with the candidates: a text table
            # leaves nothing behind once its grid is stripped, and a picture in
            # a cell survives and is labelled as one.
            image_rects, table_rects = get_all_image_candidates(
                page, margins=page_margins_here, return_grids=True)

            crops_on_page = []
            for img_rect in image_rects:
                # Exclude Barcode & QR Code regions from cropping
                is_barcode_or_qr = False
                for c_rect in code_rects:
                    padded_c = fitz.Rect(c_rect.x0 - 5, c_rect.y0 - 5, c_rect.x1 + 5, c_rect.y1 + 5)
                    if padded_c.contains(img_rect) or padded_c.intersects(img_rect):
                        intersect = padded_c & img_rect
                        if intersect.width * intersect.height > 0:
                            is_barcode_or_qr = True
                            break

                if is_barcode_or_qr:
                    print(f"  Page {page_num:3d}: Skipped Barcode/QR Code crop at {img_rect}")
                    continue

                if is_blank_region(page, img_rect):
                    print(f"  Page {page_num:3d}: Skipped blank region at {img_rect}")
                    continue

                in_table = False
                for t_rect in table_rects:
                    padded_t = fitz.Rect(t_rect.x0 - 2, t_rect.y0 - 2, t_rect.x1 + 2, t_rect.y1 + 2)
                    if padded_t.contains(img_rect) or padded_t.intersects(img_rect):
                        in_table = True
                        break

                label = "table_image" if in_table else "image"
                crops_on_page.append((label, img_rect))

            if crops_on_page:
                crops_saved_count = 0
                
                # Mask text characters inside crops if enabled
                if mask_text_in_crops:
                    for _, rect in crops_on_page:
                        mask_text_inside_rect(page, rect)

                for crop_idx, (label, rect) in enumerate(crops_on_page, start=1):
                    x0 = max(0, rect.x0 - 2)
                    y0 = max(0, rect.y0 - 2)
                    x1 = min(page_rect.width, rect.x1 + 2)
                    y1 = min(page_rect.height, rect.y1 + 2)

                    if (x1 - x0) > 5 and (y1 - y0) > 5:
                        r_clamped = fitz.Rect(x0, y0, x1, y1)
                        crop_pix = page.get_pixmap(dpi=dpi, clip=r_clamped)

                        # A region can pass the blank test and still come out
                        # empty, because that test looks at the page BEFORE the
                        # text is masked. A rect holding nothing but a caption
                        # is white by the time it is rendered.
                        #
                        # An all-white crop cannot be compared: normalised
                        # correlation against a constant template returns
                        # essentially a random number, which is how a blank
                        # 51x35 patch from page 16 came back as a 29% "match"
                        # on page 59 of a translation. Dropping it here is both
                        # cheaper and more honest than scoring it later.
                        if _is_blank_pixmap(crop_pix):
                            print(f"  Page {page_num:3d}: Skipped empty crop "
                                  f"(nothing left after text masking) at {rect}")
                            continue

                        # The index stays per-page in both layouts, so the same
                        # graphic keeps the same name with or without a TOC.
                        if by_topic:
                            topic = find_topic_for_rect(page_num, rect, topics)
                            out_dir_for_crop = os.path.join(output_dir, pdf_name, topic_slug(topic))
                            crop_filename = f"p{page_num:03d}_crop_{crop_idx:02d}_{label}.png"
                            topic_title = (topic or {}).get("title", "")
                        else:
                            out_dir_for_crop = page_out_dir
                            crop_filename = f"crop_{crop_idx:02d}_{label}.png"
                            topic_title = ""

                        crop_path = safe_crop_path(out_dir_for_crop, crop_filename,
                                                   page_num)
                        try:
                            os.makedirs(os.path.dirname(crop_path), exist_ok=True)
                            crop_pix.save(crop_path)
                        except Exception as e:
                            # One awkward topic title must not end a 33-page run.
                            # Fall back to the page folder, which is built from
                            # numbers and cannot be malformed, and say so.
                            fallback_dir = os.path.join(output_dir, pdf_name,
                                                        f"page_{page_num:03d}")
                            print(f"  Page {page_num:3d}: could not write into "
                                  f"{os.path.dirname(crop_path)!r} ({e}); "
                                  f"filing under page_{page_num:03d} instead")
                            try:
                                os.makedirs(fallback_dir, exist_ok=True)
                                crop_path = os.path.join(fallback_dir, crop_filename)
                                crop_pix.save(crop_path)
                            except Exception as e2:
                                print(f"  Page {page_num:3d}: SKIPPED {crop_filename} - {e2}")
                                continue
                        manifest.append({
                            "page": page_num, "index": crop_idx, "label": label,
                            "topic": topic_title, "path": crop_path,
                        })
                        total_crops += 1
                        crops_saved_count += 1

                if crops_saved_count > 0:
                    where = "topic folders" if by_topic else page_out_dir
                    print(f"  Page {page_num:3d}: Saved {crops_saved_count:2d} pure graphic crop(s) -> {where}")
    finally:
        if plumb_doc:
            plumb_doc.close()
        doc.close()

    print(f"Saved total of {total_crops} cropped pure graphic files to: {os.path.join(output_dir, pdf_name)}")
    if by_topic:
        used = sorted({m["topic"] for m in manifest if m["topic"]})
        print(f"  Filed under {len(used)} topic folder(s)\n")
    else:
        print()
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
