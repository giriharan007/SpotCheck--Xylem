"""
core/page_diff.py

Locate what actually differs between a master page and its translation.

The problem with diffing a translated page
------------------------------------------
A naive pixel comparison is useless here. The whole point of the document is
that its prose has been rewritten in another language, so every text block
differs by design, and a raw diff lights up the entire page. Worse, translated
text is a different length, so paragraphs reflow and push everything below them
down - which a raw diff also reports as a difference on every remaining element.

What a reviewer actually wants to know is narrower and answerable:

    does the translation still carry the same GRAPHICS, in the same places?

A missing hazard icon, a dropped rule, a logo that moved, a diagram that never
made it through the DTP round trip. That question survives translation, because
artwork is not translated. So the comparison masks every text span on both pages
before it looks at anything, and reports only on what is left.

Why this matches graphics rather than subtracting pixels
--------------------------------------------------------
Masking the text is necessary but not sufficient. Translated prose is a
different length, so the paragraphs above a figure grow or shrink and push it
down the page. Subtracting one masked render from the other reported 17 to 22
"differences" per page on a de-DE translation that is in fact perfectly correct:
every graphic that had shifted appeared twice, once as a hole where it used to
be and once as a surprise where it now is.

So each graphic on the master is instead hunted for across the whole translated
page by normalised cross-correlation, and only then classified:

    (unchanged)  found at the same place            - not reported
    moved        found, but more than tolerance away - reported, with the shift
    missing      not found anywhere on the page
    extra        a graphic on the translation that nothing on the master claimed

A matched graphic is painted out of the translated page before the next one is
hunted. Without that, a page carrying several identical hazard icons would match
them all to whichever one survived, and a genuine deletion would go unnoticed -
the same trap image_counts.py exists to close.

What counts as a graphic is not decided here. crop_images.get_all_image_candidates
already answers that question for the whole application, and it is tuned on
these manuals: it clusters fragmented vector paths into whole icons, drops thin
rules, full-page tints, language thumb tabs and the ignored margins. Finding
graphics a second way would have meant this view disagreeing with the crop
comparison and the count check about what is even on the page. An earlier draft
did exactly that - segmenting the render itself - and reported five phantom
deletions on a correct page, because the two pages' blobs merged differently.

Nothing here imports a GUI toolkit; gui/page_diff_view.py draws the result.
"""

import os

import cv2
import numpy as np
import pymupdf as fitz
from PIL import Image

# Rendering resolution for the comparison. High enough that a hairline rule
# survives, low enough that two A5 pages render in well under a second.
DEFAULT_DPI = 130

# How far a graphic may move before it counts as a difference. Translation
# reflow routinely nudges artwork by a point or two, and flagging that would
# bury the real findings.
DEFAULT_TOLERANCE_PT = 3.0

# Ignore differences smaller than this. Below roughly a 3x3 pt speck it is
# anti-aliasing along an edge, not a missing graphic.
MIN_DIFF_AREA_PT2 = 12.0

# A pixel counts as ink below this grey level, matching crop_images.
INK_LEVEL = 220

# How alike two graphics must look to be called the same one. compare_crops.py
# passes a crop at 0.80; this is stricter because here a false match hides a
# real defect rather than merely scoring it low.
MATCH_THRESHOLD = 0.86

KIND_MISSING = "missing"
KIND_EXTRA = "extra"
KIND_MOVED = "moved"
KIND_REFLOWED = "reflowed"

KIND_LABELS = {
    KIND_MISSING: "missing from the translation",
    KIND_EXTRA: "extra in the translation",
    KIND_MOVED: "moved",
    KIND_REFLOWED: "moved to an adjacent page",
}

KIND_ORDER = (KIND_MISSING, KIND_EXTRA, KIND_MOVED, KIND_REFLOWED)

# How far either side of the paired page to look before calling a graphic
# missing or extra. Translated text runs a different length, so a warning that
# sat at the foot of one page routinely lands at the head of the next - which
# on a strictly page-to-page comparison shows up twice, once as a deletion here
# and once as an addition there, and both are false.
NEIGHBOUR_PAGES = 1


def _mask_text(page):
    """
    Paint every text line on the page solid white, in memory.

    The document is never saved, so this only affects the render that follows.
    """
    try:
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:          # 0 = text, 1 = image
                continue
            for line in block.get("lines", []):
                page.draw_rect(fitz.Rect(line["bbox"]), color=(1, 1, 1), fill=(1, 1, 1))
    except Exception as e:
        print(f"[PageDiff] Could not mask text: {e}")


def render_page(pdf_path, page_no, dpi=DEFAULT_DPI, mask_text=False, size=None):
    """
    Render one 1-based page to a PIL image.

    `size` forces an exact pixel size, which is how a translation on a slightly
    different page box is brought into register with the master before they are
    compared.
    """
    with fitz.open(pdf_path) as doc:
        if not 1 <= page_no <= len(doc):
            raise IndexError(f"{os.path.basename(pdf_path)} has no page {page_no} "
                             f"({len(doc)} pages)")
        page = doc[page_no - 1]
        if mask_text:
            _mask_text(page)
        pix = page.get_pixmap(dpi=dpi)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        rect = page.rect
    if size and img.size != size:
        img = img.resize(size, Image.LANCZOS)
    return img, (rect.width, rect.height)


def _ink(img):
    """Boolean mask of everything drawn on an otherwise white page."""
    gray = np.asarray(img.convert("L"))
    return (gray < INK_LEVEL).astype(np.uint8)


def page_count(pdf_path):
    """How many pages a document has, or 0 if it cannot be opened."""
    try:
        with fitz.open(pdf_path) as doc:
            return len(doc)
    except Exception:
        return 0


def page_graphics(pdf_path, page_no, margins=None):
    """
    The graphics on a page, in points, exactly as the cropper sees them.

    One shared definition of "a graphic" across the crop comparison, the count
    check and this view. Blank regions are dropped: a rectangle that has been
    painted over carries nothing to match and would match anything.

    Barcodes and QR codes are dropped too, for the same reason the cropper drops
    them - each language carries its own part number, so the code SHOULD differ
    and flagging it here would be a permanent false alarm on every cover page.
    barcode_qr.py checks those on their own terms.
    """
    from core.crop_images import (MASK_TEXT_IN_CROPS, get_all_image_candidates,
                                  is_blank_region, mask_text_inside_rect,
                                  survives_text_masking)
    from core import barcode_qr as Barcode_QR_Check
    from core import margins as page_margins

    with fitz.open(pdf_path) as doc:
        if not 1 <= page_no <= len(doc):
            return []
        page = doc[page_no - 1]
        try:
            codes = [c["rect"] for c in Barcode_QR_Check.detect_barcodes_and_qr_codes(page)]
        except Exception:
            codes = []

        kept = []
        for r in get_all_image_candidates(
                page, margins=page_margins.margins_for_page(margins, page_no, len(doc))):
            if any(fitz.Rect(c.x0 - 5, c.y0 - 5, c.x1 + 5, c.y1 + 5).intersects(r)
                   for c in codes):
                continue
            if is_blank_region(page, r):
                continue
            kept.append(r)

        # The last two filters the cropper applies: a region that is empty once
        # its text is masked, and a piece of table ruling. Without them this
        # view boxed table grids as graphics and reported a row-height change -
        # which every correct translation has - as a difference. The claim in
        # this docstring that all three checks share one definition of "a
        # graphic" was only true down to here.
        if kept and MASK_TEXT_IN_CROPS:
            for r in kept:
                mask_text_inside_rect(page, r)

        return [(float(r.x0), float(r.y0), float(r.x1), float(r.y1))
                for r in kept if survives_text_masking(page, r)]


def _px(rect_pt, px_per_pt, bounds, pad_px=2):
    """A point rectangle as an integer pixel box (x, y, w, h), clipped."""
    max_w, max_h = bounds
    x0 = max(0, int(rect_pt[0] * px_per_pt) - pad_px)
    y0 = max(0, int(rect_pt[1] * px_per_pt) - pad_px)
    x1 = min(max_w, int(round(rect_pt[2] * px_per_pt)) + pad_px)
    y1 = min(max_h, int(round(rect_pt[3] * px_per_pt)) + pad_px)
    return x0, y0, max(0, x1 - x0), max(0, y1 - y0)


def _find(patch, haystack):
    """
    Best location of `patch` in `haystack`, as (score, x, y).

    Both are 8-bit grey. Returns score -1 when the patch cannot be searched for
    at all - larger than the page, or so nearly blank that it would correlate
    with any empty margin and match everywhere.
    """
    ph, pw = patch.shape[:2]
    hh, hw = haystack.shape[:2]
    if ph < 6 or pw < 6 or ph > hh or pw > hw:
        return -1.0, 0, 0
    if float((patch < INK_LEVEL).mean()) < 0.02:
        return -1.0, 0, 0
    res = cv2.matchTemplate(haystack, patch, cv2.TM_CCOEFF_NORMED)
    _minv, maxv, _minl, maxl = cv2.minMaxLoc(res)
    return float(maxv), int(maxl[0]), int(maxl[1])


def compare_pages(master_pdf, master_page, trans_pdf, trans_page,
                  dpi=DEFAULT_DPI, tolerance_pt=DEFAULT_TOLERANCE_PT,
                  ignore_text=True, margins=None):
    """
    Compare one master page against one translated page.

    Returns a dict carrying both full-colour renders (for display, text intact)
    and the list of differences found on the text-masked renders:

        {
          "master_image", "trans_image"   : PIL images, same pixel size
          "master_size_pt", "trans_size_pt"
          "px_per_pt"                     : display scale factor
          "diffs"   : [{"kind", "rect_pt", "area_pt2"}], reading order
          "counts"  : {"missing": n, "extra": n}
          "notes"   : [str], anything the reviewer should know about the run
        }

    With ignore_text=False the text is left in, which makes every translated
    paragraph a difference. That is occasionally what you want - checking a page
    that should NOT have been translated, such as a parts table - so it is
    offered, but it is not the default.
    """
    notes = []

    master_img, master_pt = render_page(master_pdf, master_page, dpi=dpi)
    trans_img, trans_pt = render_page(trans_pdf, trans_page, dpi=dpi)

    # Bring the translation into register. Same stylesheet means this is almost
    # always a no-op, but a document at a different trim size would otherwise
    # report every single element as both missing and extra.
    if trans_img.size != master_img.size:
        notes.append(
            f"page sizes differ ({trans_pt[0]:.0f}x{trans_pt[1]:.0f} pt vs "
            f"{master_pt[0]:.0f}x{master_pt[1]:.0f} pt) - the translation was "
            f"scaled to match before comparing")
        trans_img = trans_img.resize(master_img.size, Image.LANCZOS)

    if ignore_text:
        m_cmp, _ = render_page(master_pdf, master_page, dpi=dpi, mask_text=True)
        t_cmp, _ = render_page(trans_pdf, trans_page, dpi=dpi, mask_text=True,
                               size=master_img.size)
    else:
        m_cmp, t_cmp = master_img, trans_img
        notes.append("text is included in this comparison, so translated wording "
                     "counts as a difference")

    px_per_pt = master_img.size[0] / max(1e-6, master_pt[0])
    scale_pt = 1.0 / px_per_pt
    tol_px = max(2, int(round(tolerance_pt * px_per_pt)))

    m_gray = np.asarray(m_cmp.convert("L"))
    t_gray = np.asarray(t_cmp.convert("L"))
    bounds_m = (m_gray.shape[1], m_gray.shape[0])
    bounds_t = (t_gray.shape[1], t_gray.shape[0])

    m_rects = page_graphics(master_pdf, master_page, margins=margins)
    t_rects = page_graphics(trans_pdf, trans_page, margins=margins)

    # Biggest first: a whole diagram should claim its match before one of its
    # own sub-parts gets the chance to.
    m_boxes = sorted((_px(r, px_per_pt, bounds_m) for r in m_rects),
                     key=lambda b: b[2] * b[3], reverse=True)
    t_boxes = [_px(r, px_per_pt, bounds_t) for r in t_rects]

    # Matches are struck out of this working copy so the same translated graphic
    # cannot answer for two different master graphics.
    t_work = t_gray.copy()
    claimed = np.zeros(t_gray.shape[:2], np.uint8)

    def as_pt(box):
        x, y, w, h = box
        return (x * scale_pt, y * scale_pt, (x + w) * scale_pt, (y + h) * scale_pt)

    # Neighbouring pages, rendered only if something actually goes looking for
    # them, and only once each. Both documents are needed: a graphic can flow
    # forward out of the master page, or into the translated one.
    _neighbours = {}

    def neighbour_gray(pdf_path, page_no, total_hint=None):
        key = (pdf_path, page_no)
        if key not in _neighbours:
            try:
                img, _pt = render_page(pdf_path, page_no, dpi=dpi, mask_text=True,
                                       size=master_img.size)
                _neighbours[key] = np.asarray(img.convert("L"))
            except Exception:
                _neighbours[key] = None          # no such page, or unreadable
        return _neighbours[key]

    def found_nearby(patch, pdf_path, centre_page):
        """
        The adjacent page carrying this graphic, or None.

        Nearest page first, so a graphic present on both neighbours is
        reported against the one it most likely flowed to.
        """
        for step in range(1, NEIGHBOUR_PAGES + 1):
            for page_no in (centre_page - step, centre_page + step):
                if page_no < 1:
                    continue
                gray = neighbour_gray(pdf_path, page_no)
                if gray is None:
                    continue
                if patch.shape[0] > gray.shape[0] or patch.shape[1] > gray.shape[1]:
                    continue
                score, _fx, _fy = _find(patch, gray)
                if score >= MATCH_THRESHOLD:
                    return page_no, score
        return None

    diffs = []
    for (x, y, w, h) in m_boxes:
        if w < 4 or h < 4:
            continue
        patch = m_gray[y:y + h, x:x + w]
        score, fx, fy = _find(patch, t_work)
        if score < MATCH_THRESHOLD:
            # Not on this page - but reflow may simply have carried it onto the
            # next or previous one, which is not a deletion and must not be
            # reported as one.
            near = found_nearby(patch, trans_pdf, trans_page)
            if near:
                near_page, near_score = near
                diffs.append({
                    "kind": KIND_REFLOWED,
                    "rect_master": as_pt((x, y, w, h)),
                    "rect_trans": None,
                    "shift_pt": None,
                    "score": near_score,
                    "found_on_page": near_page,
                    "found_side": "trans",
                })
                continue
            diffs.append({
                "kind": KIND_MISSING,
                "rect_master": as_pt((x, y, w, h)),
                "rect_trans": None,
                "shift_pt": None,
                "score": max(0.0, score),
            })
            continue

        t_work[fy:fy + h, fx:fx + w] = 255       # consumed
        claimed[fy:fy + h, fx:fx + w] = 1
        dx, dy = fx - x, fy - y
        if abs(dx) > tol_px or abs(dy) > tol_px:
            diffs.append({
                "kind": KIND_MOVED,
                "rect_master": as_pt((x, y, w, h)),
                "rect_trans": as_pt((fx, fy, w, h)),
                "shift_pt": (dx * scale_pt, dy * scale_pt),
                "score": score,
            })
        # Matched in place: nothing to report.

    # Anything on the translation that no master graphic claimed is an addition -
    # unless the master simply carries it on the page before or after, which is
    # the same reflow seen from the other side.
    for (x, y, w, h) in t_boxes:
        if w < 4 or h < 4:
            continue
        covered = float(claimed[y:y + h, x:x + w].mean())
        if covered >= 0.5:
            continue
        near = found_nearby(t_gray[y:y + h, x:x + w], master_pdf, master_page)
        if near:
            near_page, near_score = near
            diffs.append({
                "kind": KIND_REFLOWED,
                "rect_master": None,
                "rect_trans": as_pt((x, y, w, h)),
                "shift_pt": None,
                "score": near_score,
                "found_on_page": near_page,
                "found_side": "master",
            })
            continue
        diffs.append({
            "kind": KIND_EXTRA,
            "rect_master": None,
            "rect_trans": as_pt((x, y, w, h)),
            "shift_pt": None,
            "score": None,
        })

    def _order(d):
        r = d["rect_master"] or d["rect_trans"]
        return (r[1], r[0])
    diffs.sort(key=_order)

    counts = {k: sum(1 for d in diffs if d["kind"] == k) for k in KIND_ORDER}

    return {
        "master_pdf": master_pdf,
        "trans_pdf": trans_pdf,
        "master_page": master_page,
        "trans_page": trans_page,
        "master_image": master_img,
        "trans_image": trans_img,
        "master_size_pt": master_pt,
        "trans_size_pt": trans_pt,
        "px_per_pt": px_per_pt,
        "dpi": dpi,
        "ignore_text": ignore_text,
        "tolerance_pt": tolerance_pt,
        "graphics_master": len(m_boxes),
        "graphics_trans": len(t_boxes),
        "diffs": diffs,
        "counts": counts,
        "notes": notes,
    }


def summarize(result):
    """One line for the window's status strip."""
    if result is None:
        return "not compared"
    n = len(result["diffs"])
    if n == 0:
        got = result.get("graphics_master", 0)
        return (f"No differences - all {got} graphic(s) matched"
                if result["ignore_text"] else "No differences found")
    c = result["counts"]
    parts = [f"{c[k]} {KIND_LABELS[k]}" for k in KIND_ORDER if c.get(k)]
    return f"{n} difference{'s' if n != 1 else ''} — " + ", ".join(parts)
