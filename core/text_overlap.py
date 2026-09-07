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
            overlap = _intersection_area(ab, bb)
            if overlap < MIN_OVERLAP_PT2:
                continue
            smaller = min(_area(ab), _area(bb)) or 1e-6
            if overlap / smaller >= MIN_OVERLAP_FRACTION:
                hits.append((a, b, overlap))
    return hits


def _ink_rows(page, rect):
    """Dark pixels per scan line down a rectangle of the page."""
    pix = page.get_pixmap(clip=fitz.Rect(rect), dpi=INK_DPI, colorspace=fitz.csGRAY)
    if not pix.width or not pix.height:
        return np.zeros(0, dtype=int)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    return (arr < INK_THRESHOLD).sum(axis=1)


def _glyphs_collide(page, a_bbox, b_bbox):
    """
    The two boxes overlap. Do the letters?

    Returns (collides, explanation).
    """
    x0, x1 = max(a_bbox[0], b_bbox[0]), min(a_bbox[2], b_bbox[2])
    if x1 - x0 <= 0.5:
        return False, "the boxes share no column of the page"

    top = max(a_bbox[1], b_bbox[1]) - BAND_PAD_PT
    bot = min(a_bbox[3], b_bbox[3]) + BAND_PAD_PT
    rows = _ink_rows(page, (x0, top, x1, bot))
    if not rows.size or not rows.max():
        return False, "no ink in the overlap band"

    # Only the gap BETWEEN the ink counts. The band is padded, and a span box
    # runs past the tallest letter in it, so there are always blank lines at the
    # top and bottom - counting those dismissed a page where the two lines were
    # printed straight through each other.
    inked = np.flatnonzero(rows)
    interior = rows[inked[0]:inked[-1] + 1]
    gap_px = _longest_run_of_zeros(interior)
    gap_pt = gap_px / (INK_DPI / 72.0)
    if gap_pt >= MIN_CLEAR_GAP_PT:
        return False, (f"{gap_pt:.2f} pt of white separates the two lines - "
                       f"the boxes overlap, the letters do not")
    return True, ("no white between them (largest gap "
                  f"{gap_pt:.2f} pt) - the ink is printed through itself")


def _longest_run_of_zeros(rows):
    longest = run = 0
    for value in rows:
        run = run + 1 if value == 0 else 0
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
            raw = []
            for a, b, overlap in _candidates(page, clip):
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