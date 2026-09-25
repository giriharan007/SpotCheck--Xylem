"""
core/text_overlap.py

Find text that collides with other text, in any of the documents.

WHY THIS IS NOT JUST "DO THE BOXES INTERSECT"
---------------------------------------------
A span's bounding box runs from the ascender to the descender of the font, not
around the ink. Set two lines on tight leading and the boxes overlap while the
letters never come near each other. That is not a hypothetical: scanning the
Start 350 set found exactly one box intersection across all twelve documents -
the Greek copyright block on page 20 - and rendering it at 400 dpi showed three
perfectly clean lines. Greek accents make the box taller than the leading. A
detector that stopped at box intersection would report that page as broken in
every Greek, Cyrillic and Vietnamese manual Xylem ships, and a reviewer who is
told "broken" eleven times for nothing stops reading the column.

So the check is in two tiers:

  1. Box intersection, on the geometry alone. Cheap, and it narrows a page of
     several hundred spans down to a handful of candidates.
  2. Ink. The overlap band is rendered at 300 dpi and the dark pixels are
     counted per scan line. If a run of completely blank lines separates the
     two lines' ink, the boxes overlap and the glyphs do not - dismissed. No
     blank line anywhere in the band means the ink really is on top of itself.

Measured on the Start 350 set:

    all twelve shipped documents      1 candidate      0 real collisions
    one copy with a line overrun      2 candidates     1 real collision

The second row is the case worth having: a caption that runs long in German and
lands on the line below it. That is what this is for.

skip_first_last defaults to True here, but the pipeline calls this with it set
to False: an overrun cover or back-cover layout is a real defect too, and by
request the running inspection now checks every page, first and last
included. The default stays True for any other caller that wants the old
cover/back-matter-is-hand-set behaviour.
"""

import os

import numpy as np
import pymupdf as fitz


# A box intersection smaller than this is kerning or a rounding artefact.
MIN_OVERLAP_PT2 = 1.0

# ...and it has to be a real share of the smaller of the two boxes. Two lines
# grazing each other by a hairline is not what anyone means by overlapping.
MIN_OVERLAP_FRACTION = 0.20

# Tier 2 render. 300 dpi puts about four pixels across the gap between two
# 8 pt lines on normal leading, which is enough to see the gap without making
# the render itself the cost of the check.
INK_DPI = 300
INK_THRESHOLD = 160           # 0-255; below this counts as ink

# How far past the overlap band to look. Without a little margin the band can
# start and end inside the glyphs themselves, and every candidate then looks
# like a collision because there was never room for a blank line.
BAND_PAD_PT = 1.0

# How much white between the two lines' ink means they are merely near each
# other. Two lines on tight leading keep about 1-3 pt of daylight even when
# their boxes overlap; inside one line, the space between a letter and the
# accent above it is a fraction of that. Half a point sits in the gap between
# those two worlds.
MIN_CLEAR_GAP_PT = 0.5


def _spans(page, clip=None):
    """Every non-blank text span on the page, with the line it belongs to."""
    out = []
    kwargs = {"clip": fitz.Rect(clip)} if clip else {}
    for block in page.get_text("dict", **kwargs).get("blocks", []):
        if block.get("type") != 0:          # 0 = text, 1 = image
            continue
        for li, line in enumerate(block.get("lines", [])):
            for span in line.get("spans", []):
                if span.get("text", "").strip():
                    out.append({
                        "text": span["text"],
                        "bbox": tuple(float(v) for v in span["bbox"]),
                        "line": (block.get("number"), li),
                    })
    return out


def _intersection_area(a, b):
    return (max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
            * max(0.0, min(a[3], b[3]) - max(a[1], b[1])))


def _area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _is_normal_typesetting(a, b):
    """
    True when two spans are consecutive lines in the same text block with standard leading.
    Ascender-to-descender font box overlap is standard typography and not a collision.
    """
    if a["line"][0] == b["line"][0]:
        ab, bb = a["bbox"], b["bbox"]
        ha = ab[3] - ab[1]
        hb = bb[3] - bb[1]
        h = min(ha, hb)
        dy = abs(bb[1] - ab[1])
        if dy >= 0.55 * h:
            return True
    return False


def _candidates(page, clip=None):
    """
    Pairs of spans whose boxes intersect enough to be worth rendering.

    Swept rather than compared pairwise: sorted by top edge, each span is only
    tested against the ones that start before its own bottom edge. A dense page
    has several hundred spans, and the honest O(n^2) version cost about a tenth
    of a second a page - two minutes across a 92-page manual in twelve
    languages, for a check that finds nothing on almost every page.
    """
    spans = sorted(_spans(page, clip), key=lambda s: s["bbox"][1])
    hits = []
    for i, a in enumerate(spans):
        ab = a["bbox"]
        for b in spans[i + 1:]:
            bb = b["bbox"]
            if bb[1] >= ab[3]:              # sorted by top: nothing later can reach back
                break
            if a["line"] == b["line"]:      # same line - that is kerning, not a collision
                continue
            if _is_normal_typesetting(a, b): # regular multi-line typesetting in same block
                continue
            overlap = _intersection_area(ab, bb)
            if overlap < MIN_OVERLAP_PT2:
                continue
            smaller = min(_area(ab), _area(bb)) or 1e-6
            w_ov = max(0.0, min(ab[2], bb[2]) - max(ab[0], bb[0]))
            h_ov = max(0.0, min(ab[3], bb[3]) - max(ab[1], bb[1]))
            # Significant if:
            # - fraction >= MIN_OVERLAP_FRACTION (0.20)
            # - cross-collision with rotated/vertical text (w_ov >= 6.0 and h_ov >= 6.0)
            # - substantial overlap area (>= 25.0 pt2)
            if (overlap / smaller >= MIN_OVERLAP_FRACTION) or (w_ov >= 6.0 and h_ov >= 6.0) or (overlap >= 25.0):
                hits.append((a, b, overlap))
    return hits


def _ink_rows(page, rect):
    """Dark pixels per scan line down a rectangle of the page."""
    pix = page.get_pixmap(clip=fitz.Rect(rect), dpi=INK_DPI, colorspace=fitz.csGRAY)
    if not pix.width or not pix.height:
        return np.zeros(0, dtype=int), 0
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    return (arr < INK_THRESHOLD).sum(axis=1), pix.width


def _glyphs_collide(page, a_bbox, b_bbox):
    """
    The two boxes overlap. Do the letters?

    Returns (collides, explanation).
    """
    x0, x1 = max(a_bbox[0], b_bbox[0]), min(a_bbox[2], b_bbox[2])
    if x1 - x0 <= 0.5:
        return False, "the boxes share no column of the page"

    ov_top = max(a_bbox[1], b_bbox[1])
    ov_bot = min(a_bbox[3], b_bbox[3])
    if ov_bot <= ov_top:
        return False, "the boxes do not overlap vertically"

    ha = a_bbox[3] - a_bbox[1]
    hb = b_bbox[3] - b_bbox[1]
    v_overlap = ov_bot - ov_top
    h_overlap = x1 - x0

    # 1. Direct superimposition on the same line (e.g. topic 7.6):
    # Two lines share >= 50% of vertical height and >= 8pt horizontal span.
    # They occupy the same line, so vertical daylight checking between lines is inapplicable.
    if v_overlap >= 0.50 * min(ha, hb) and h_overlap >= 8.0:
        rows_ov, _ = _ink_rows(page, (x0, ov_top, x1, ov_bot))
        if rows_ov.size and rows_ov.max() > 0:
            return True, f"direct superimposition ({v_overlap:.1f} pt vertical overlap on same line)"

    # 2. Cross collision (horizontal text crossing vertical/rotated text, e.g. topic 8.2):
    is_cross = ((a_bbox[2] - a_bbox[0] > 2.0 * ha and hb > 1.5 * (b_bbox[2] - b_bbox[0])) or
                (b_bbox[2] - b_bbox[0] > 2.0 * hb and ha > 1.5 * (a_bbox[2] - a_bbox[0])))
    if is_cross and h_overlap >= 6.0 and v_overlap >= 6.0:
        rows_ov, _ = _ink_rows(page, (x0, ov_top, x1, ov_bot))
        if rows_ov.size and rows_ov.max() > 0:
            return True, f"cross collision ({h_overlap:.1f}x{v_overlap:.1f} pt intersection with rotated/vertical text)"

    # 3. Stacked lines (one line above another on tight leading):
    # To check if there is daylight BETWEEN line A and line B, the sample should be bounded by
    # the centers of the two lines, never expanding into whitespace beyond them.
    if a_bbox[1] <= b_bbox[1]:
        scan_top = max(a_bbox[1], ov_top - 2.0)
        scan_bot = min(b_bbox[3], ov_bot + 2.0)
    else:
        scan_top = max(b_bbox[1], ov_top - 2.0)
        scan_bot = min(a_bbox[3], ov_bot + 2.0)

    rows, pix_w = _ink_rows(page, (x0, scan_top, x1, scan_bot))
    if not rows.size or not rows.max():
        return False, "no ink in the overlap band"

    inked = np.flatnonzero(rows)
    if len(inked) < 2:
        return False, "negligible ink in overlap band"

    interior = rows[inked[0]:inked[-1] + 1]
    gap_px = _longest_run_of_zeros(interior)
    gap_pt = gap_px / (INK_DPI / 72.0)
    if gap_pt >= MIN_CLEAR_GAP_PT:
        return False, (f"{gap_pt:.2f} pt of white separates the two lines - "
                       f"the boxes overlap, the letters do not")

    # Anti-aliasing fringe test: gap must be in the interior (not edge tails)
    if len(interior) >= 6:
        mid_interior = interior[2:-2]
        low_ink_run = _longest_run_below(mid_interior, threshold=max(2, int(pix_w * 0.03)))
        low_gap_pt = low_ink_run / (INK_DPI / 72.0)
        if low_gap_pt >= 1.0:
            return False, (f"{low_gap_pt:.2f} pt of daylight separates the lines - "
                           f"only antialiasing fringe in between")

    return True, ("no white between them (largest gap "
                  f"{gap_pt:.2f} pt) - the ink is printed through itself")


def _longest_run_of_zeros(rows):
    longest = run = 0
    for value in rows:
        run = run + 1 if value == 0 else 0
        longest = max(longest, run)
    return longest


def _longest_run_below(rows, threshold=2):
    longest = run = 0
    for value in rows:
        run = run + 1 if value <= threshold else 0
        longest = max(longest, run)
    return longest


def _clip_for_margins(page, margins):
    """The area to scan, once the ignored margins are taken off."""
    if not margins:
        return None
    r = page.rect
    try:
        return (r.x0 + float(margins.get("left", 0) or 0),
                r.y0 + float(margins.get("header", 0) or 0),
                r.x1 - float(margins.get("right", 0) or 0),
                r.y1 - float(margins.get("footer", 0) or 0))
    except (TypeError, ValueError):
        return None


def evidence_crop(page, rect, out_path, pad=6.0, dpi=200):
    """Save a picture of the collision, so the finding can be looked at."""
    try:
        r = fitz.Rect(rect[0] - pad, rect[1] - pad, rect[2] + pad, rect[3] + pad)
        r = r & page.rect
        if r.is_empty:
            return ""
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        page.get_pixmap(clip=r, dpi=dpi).save(out_path)
        return out_path
    except Exception:
        return ""


_artwork_cache = {}


def _graphic_rects(pdf_path, page_no, margins):
    """The drawings on one page, cached per document and page."""
    key = (os.path.abspath(pdf_path), page_no)
    if key not in _artwork_cache:
        try:
            from core import page_diff
            _artwork_cache[key] = [fitz.Rect(*r) for r in
                                   page_diff.page_graphics(pdf_path, page_no, margins)]
        except Exception:
            _artwork_cache[key] = []
    return _artwork_cache[key]


# How far outside a drawing a label still belongs to it. A callout sits just
# clear of the thing it names - the pin labels under a terminal symbol land
# about a line below the ink - so a rule that asked for the label to be INSIDE
# the artwork matched almost none of them.
ARTWORK_LABEL_PAD_PT = 14.0


def _inside_artwork(bbox_a, bbox_b, artwork):
    """
    True when both colliding labels belong to the same drawing.

    A technical illustration is not laid out like prose. Callout numbers sit on
    leader lines, a terminal label sits hard against the terminal it names, and
    two labels on neighbouring pins are as close as the pins are - all of which
    reads as a text collision and none of which is a defect. The check exists to
    catch a translated line overrunning its cell or its neighbour in the body
    copy, and inside a figure there is no such expectation to measure against.

    Both boxes must belong to the SAME graphic, so a line that has overrun out
    of a figure and onto real body text is still reported.
    """
    if not artwork:
        return False
    ra, rb = fitz.Rect(bbox_a), fitz.Rect(bbox_b)
    pad = ARTWORK_LABEL_PAD_PT
    for g in artwork:
        near = fitz.Rect(g.x0 - pad, g.y0 - pad, g.x1 + pad, g.y1 + pad)
        if near.contains(ra) and near.contains(rb):
            return True
    return False


def find_overlaps(pdf_path, skip_first_last=True, margins=None,
                  evidence_dir=None, progress=None):
    """
    Every place in one document where text collides with text.

    Returns a list of dicts:
        page, text_a, text_b, rect, overlap_pt2, why, image

    `margins` is the ignored-margin block from the stylesheet; pass it to keep
    running heads and page furniture out of the scan.
    """
    findings = []
    if not pdf_path or not os.path.isfile(pdf_path):
        return findings

    name = os.path.basename(pdf_path)
    with fitz.open(pdf_path) as doc:
        total = len(doc)
        first = 1 if not skip_first_last else 2
        last = total if not skip_first_last else total - 1
        for page in doc:
            page_no = page.number + 1
            if page_no < first or page_no > last:
                continue
            if progress:
                progress(page_no, total, name)
            clip = _clip_for_margins(page, margins)
            candidates = _candidates(page, clip)
            if not candidates:
                continue

            # Only now, and only on the few pages that got this far. Working
            # out where the drawings are means ink clustering, grid detection
            # and a barcode sweep, with the document reopened for each page -
            # about a third of a second. Doing that for every page of every
            # translation cost seven minutes a batch to answer a question that
            # almost every page never asks: this check finds no candidate at
            # all on the overwhelming majority of them.
            artwork = _graphic_rects(pdf_path, page_no, margins)
            raw = []
            for a, b, overlap in candidates:
                if _inside_artwork(a["bbox"], b["bbox"], artwork):
                    continue
                collides, why = _glyphs_collide(page, a["bbox"], b["bbox"])
                if collides:
                    raw.append((a, b, overlap, why))

            for group in _merge(raw):
                image = ""
                if evidence_dir:
                    stem = f"{os.path.splitext(name)[0]}_p{page_no}_{len(findings) + 1}.png"
                    image = evidence_crop(page, group["rect"],
                                          os.path.join(evidence_dir, stem))
                findings.append({
                    "document": name,
                    "path": pdf_path,
                    "page": page_no,
                    "text_a": group["texts"][0],
                    "text_b": " / ".join(group["texts"][1:]) or group["texts"][0],
                    "texts": group["texts"],
                    "rect": [round(v, 2) for v in group["rect"]],
                    "overlap_pt2": round(group["overlap"], 2),
                    "pairs": group["pairs"],
                    "why": group["why"],
                    "image": image,
                })
    return findings


def _merge(raw):
    """
    One defect, one finding.

    A single line that overruns across a table row collides with every cell it
    crosses, and reporting that as four findings on one page makes a reviewer
    open the same picture four times. Collisions whose areas touch are the same
    defect and are reported together.
    """
    groups = []
    for a, b, overlap, why in raw:
        rect = [min(a["bbox"][0], b["bbox"][0]), min(a["bbox"][1], b["bbox"][1]),
                max(a["bbox"][2], b["bbox"][2]), max(a["bbox"][3], b["bbox"][3])]
        texts = [a["text"].strip(), b["text"].strip()]
        for g in groups:
            if _intersection_area(g["rect"], rect) > 0:
                g["rect"] = [min(g["rect"][0], rect[0]), min(g["rect"][1], rect[1]),
                             max(g["rect"][2], rect[2]), max(g["rect"][3], rect[3])]
                for t in texts:
                    if t not in g["texts"]:
                        g["texts"].append(t)
                g["overlap"] += overlap
                g["pairs"] += 1
                break
        else:
            groups.append({"rect": rect, "texts": texts, "overlap": overlap,
                           "pairs": 1, "why": why})
    return groups


def scan_documents(pdf_paths, skip_first_last=True, margins=None,
                   evidence_dir=None, progress=None):
    """Run find_overlaps over the master and every translation."""
    all_findings = []
    for path in pdf_paths or []:
        all_findings.extend(find_overlaps(
            path, skip_first_last=skip_first_last, margins=margins,
            evidence_dir=evidence_dir, progress=progress))
    return all_findings