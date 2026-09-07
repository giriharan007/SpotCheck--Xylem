"""
core/margin_overflow.py

Find body text that runs PAST the left or right margin - the two vertical edges
of the live area.

WHY THIS IS ITS OWN CHECK
-------------------------
A translated line is often longer than the English it replaces. When it will not
wrap it pushes past the right margin into the band beyond the trim, where it
collides with the page edge, a thumb tab, or simply reads as broken. The mirror
case is a mis-set indent that starts a line left of the margin. Neither is a
content fault - the words can be perfectly correct - so the count check, the
text diff and the untranslated-text check all pass it. It is a geometry fault,
and only geometry catches it.

Only the LEFT and RIGHT edges are checked. Those are the "vertical" margins the
user cares about; top and bottom are the header/footer's business and are left
alone here.

WHAT IS DELIBERATELY IGNORED
----------------------------
  - the header and footer bands - running heads and page numbers live there by
    design, so a line the margins class as header/footer furniture is skipped.
  - anything the margin rules already treat as furniture: a language thumb tab
    anchored to the right edge, for instance. which_margin() answers exactly
    that question, so the same rule that keeps the tab out of image extraction
    keeps it out of here - the marked/ignored areas need no second mechanism.
  - the first/last page's exempted bands: margins_for_page() zeroes whatever the
    stylesheet skips, and a side with no band has no margin to overflow.

So the only thing that survives to be reported is real running text crossing a
real, live margin - which is the defect.
"""
import os

import pymupdf as fitz

from core import margins as PageMargins


# Balanced default: ignore hairline crossings from glyph-box padding and
# anti-aliasing, flag anything that clears the margin by more than this. A line
# that overruns for real clears it by far more than two points.
MIN_OVERFLOW_PT = 2.0


def _lines(page, clip=None):
    """Every non-blank text line on the page, as (text, bbox)."""
    out = []
    kwargs = {"clip": fitz.Rect(clip)} if clip else {}
    for block in page.get_text("dict", **kwargs).get("blocks", []):
        if block.get("type") != 0:              # 0 = text, 1 = image
            continue
        for line in block.get("lines", []):
            text = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
            if text:
                out.append((text, tuple(float(v) for v in line["bbox"])))
    return out


def evidence_crop(page, bbox, boundary_x, out_path, pad=6.0, dpi=200):
    """
    A picture of the overrun: the line and the margin line it crosses.

    Rendered wide enough to show BOTH the margin boundary and where the text
    ends past it, so the finding can be judged at a glance rather than by its
    coordinates.
    """
    try:
        x0 = min(bbox[0], boundary_x) - pad
        x1 = max(bbox[2], boundary_x) + pad
        r = fitz.Rect(x0, bbox[1] - pad, x1, bbox[3] + pad) & page.rect
        if r.is_empty:
            return ""
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        page.get_pixmap(clip=r, dpi=dpi).save(out_path)
        return out_path
    except Exception:
        return ""


def find_overflows(pdf_path, margins=None, skip_first_last=False,
                   evidence_dir=None, progress=None):
    """
    Every line in one document that crosses its page's left or right margin.

    Returns a list of dicts:
        document, path, page, text, side, over_pt, boundary_x, rect, image

    `margins` is the stylesheet's ignored-margin block - the same one the run
    extracts with. Without side margins set there is nothing to overflow, so a
    document run with the defaults (no left/right band) reports nothing, which is
    the honest answer rather than flagging every full-width line.
    """
    findings = []
    if not pdf_path or not os.path.isfile(pdf_path):
        return findings

    name = os.path.basename(pdf_path)
    with fitz.open(pdf_path) as doc:
        total = len(doc)
        first = 2 if skip_first_last else 1
        last = total - 1 if skip_first_last else total
        for page in doc:
            page_no = page.number + 1
            if page_no < first or page_no > last:
                continue
            if progress:
                progress(page_no, total, name)

            pw, ph = float(page.rect.width), float(page.rect.height)
            # Per page: the first/last page may switch its side bands off, and a
            # fold-out can differ in size. margins_for_page zeroes exempted bands.
            m = PageMargins.margins_for_page(margins, page_no, total)
            left_lim = m["left"]
            right_lim = pw - m["right"]

            # Nothing to overflow if neither side band is set on this page.
            if m["left"] <= 0 and m["right"] <= 0:
                continue

            for text, bbox in _lines(page):
                # The one ignore rule, shared with image extraction: a line the
                # margins already own - a running head, a footer, an edge-anchored
                # thumb tab - is furniture, not an overrun.
                if PageMargins.which_margin(bbox, pw, ph, m) is not None:
                    continue

                bx0, by0, bx1, by1 = bbox
                over_left = (left_lim - bx0) if m["left"] > 0 else 0.0
                over_right = (bx1 - right_lim) if m["right"] > 0 else 0.0

                # Report the worse side when a line somehow clears both.
                if over_right >= over_left:
                    side, over, boundary = "right", over_right, right_lim
                else:
                    side, over, boundary = "left", over_left, left_lim
                if over <= MIN_OVERFLOW_PT:
                    continue

                image = ""
                if evidence_dir:
                    stem = f"{os.path.splitext(name)[0]}_p{page_no}_{len(findings) + 1}.png"
                    image = evidence_crop(page, bbox, boundary,
                                          os.path.join(evidence_dir, stem))
                findings.append({
                    "document": name,
                    "path": pdf_path,
                    "page": page_no,
                    "text": text.strip(),
                    "side": side,
                    "over_pt": round(float(over), 1),
                    "boundary_x": round(float(boundary), 1),
                    "rect": [round(float(v), 2) for v in bbox],
                    "image": image,
                })
    return findings


def scan_documents(pdf_paths, margins=None, skip_first_last=False,
                   evidence_dir=None, progress=None):
    """Run find_overflows over the master and every translation."""
    all_findings = []
    for path in pdf_paths or []:
        all_findings.extend(find_overflows(
            path, margins=margins, skip_first_last=skip_first_last,
            evidence_dir=evidence_dir, progress=progress))
    return all_findings