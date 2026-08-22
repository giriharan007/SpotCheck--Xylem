"""
core/region_engine.py

Region (ROI) extraction, similarity scoring and side-by-side comparison engine.

Pure backend: no Tkinter / CustomTkinter dependency. Everything here can be
driven from a script, a test, or the GUI dialog in gui/region_dialog.py.

Provides:
  - get_page_count / render_pdf_page_image : PDF page access & rendering
  - extract_roi_text                       : clipped text extraction for a region
  - compute_region_similarity              : text similarity (exact / sequence / token)
  - compute_visual_graphic_similarity      : text-masked visual graphic matching
  - trim_white_borders                     : whitespace trimming before visual compare
  - is_rect_contained_in_parent            : sub-region nesting test
  - generate_only_compared_image           : side-by-side comparison PNG
  - search_and_verify_region_in_target     : locate & verify one region in one target PDF
  - run_batch_multiple_regions_check       : batch across regions x translated PDFs
"""

import os
import re
import difflib

import pymupdf as fitz  # PyMuPDF (aliased as fitz for API compat)
import cv2
import numpy as np
from PIL import Image, ImageDraw

from core.templates import collapse_variant_results, expand_regions_for_document

# Default output location, used only when the caller does not supply one.
# The GUI always passes an explicit directory derived from the user's chosen
# output folder, so this is a script/CLI fallback.
DEFAULT_CROPS_OUTPUT_DIR = os.path.abspath(
    os.path.join("Output", "Cropped_Comparison", "Region_Inspector")
)


# ==============================================================================
# CORE SPATIAL REGION EXTRACTION & CROPPED IMAGE COMPARISON
# ==============================================================================

def get_page_count(pdf_path: str) -> int:
    """Return total number of pages in a PDF."""
    try:
        with fitz.open(pdf_path) as doc:
            return len(doc)
    except Exception:
        return 0


def render_pdf_page_image(pdf_path: str, page_num: int, zoom: float = 1.0) -> tuple[Image.Image, float, float]:
    """
    Render a specific PDF page to PIL Image at the specified zoom scale.
    Renders cleanly without masking anything so all text and layout remain 100% visible.
    page_num is 1-indexed (or -1 for last page).
    Returns: (PIL.Image, page_width_pt, page_height_pt)
    """
    with fitz.open(pdf_path) as doc:
        total = len(doc)
        if total == 0:
            raise ValueError("PDF has no pages.")
        
        p_idx = (total - 1) if page_num == -1 else max(0, min(total - 1, page_num - 1))
        page = doc[p_idx]
        pw, ph = page.rect.width, page.rect.height
        
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)
        
        mode = "RGBA" if pix.alpha else "RGB"
        img = Image.frombytes(mode, [pix.width, pix.height], pix.samples).convert("RGB")
        return img, pw, ph


def render_region_image(pdf_path, page_num, roi_rect, dpi=150):
    """
    Render just what sits inside a region, as a picture.

    Lets the caller SEE the contents of a bounding box rather than only its
    extracted text — which matters because plenty of regions hold no text at
    all: a divider rule, the Xylem logo, a hazard icon, a table frame. Those
    come back as an empty string from extract_roi_text but are perfectly
    visible here.

    Returns a PIL.Image (RGB), or None if the region is degenerate or the page
    cannot be read.
    """
    try:
        x0, y0, x1, y1 = roi_rect
        if (x1 - x0) < 1 or (y1 - y0) < 1:
            return None
        with fitz.open(pdf_path) as doc:
            total = len(doc)
            if total == 0:
                return None
            p_idx = (total - 1) if page_num == -1 else max(0, min(total - 1, page_num - 1))
            page = doc[p_idx]
            clip = fitz.Rect(roi_rect) & page.rect
            if clip.is_empty or clip.width < 1 or clip.height < 1:
                return None
            pix = page.get_pixmap(dpi=dpi, clip=clip)
            mode = "RGBA" if pix.alpha else "RGB"
            return Image.frombytes(mode, [pix.width, pix.height], pix.samples).convert("RGB")
    except Exception as e:
        print(f"[RegionEngine] Could not render region preview: {e}")
        return None


def extract_roi_text(doc: fitz.Document, page_num: int, roi_rect: tuple[float, float, float, float]) -> str:
    """
    Extract clean text inside roi_rect = (x0, y0, x1, y1) in PDF point coordinates.
    """
    total = len(doc)
    p_idx = (total - 1) if page_num == -1 else max(0, min(total - 1, page_num - 1))
    page = doc[p_idx]
    clip_r = fitz.Rect(roi_rect)
    return page.get_text("text", clip=clip_r).strip()


def compute_region_similarity(eng_text: str, tr_text: str) -> tuple[float, str]:
    """
    Compute similarity between English region text and target translated region text.
    Returns: (similarity_percentage, match_description)
    """
    if not eng_text and not tr_text:
        return 100.0, "Exact Match (Both Empty)"
    if not eng_text or not tr_text:
        return 0.0, "Empty / No Content"

    eng_clean = " ".join(eng_text.split()).strip()
    tr_clean = " ".join(tr_text.split()).strip()

    # 1. Exact string match (ignoring whitespace / case)
    if eng_clean.lower() == tr_clean.lower():
        return 100.0, "Exact Match"

    # 2. SequenceMatcher (Character level)
    seq_ratio = difflib.SequenceMatcher(None, eng_clean.lower(), tr_clean.lower()).ratio() * 100.0
    if seq_ratio >= 85.0:
        return round(seq_ratio, 1), "Near Match"

    # 3. Token & Alphanumeric Overlap (for addresses, phone numbers, URLs, product codes)
    eng_tokens = set(re.findall(r'\w+', eng_clean.lower()))
    tr_tokens = set(re.findall(r'\w+', tr_clean.lower()))
    if eng_tokens and tr_tokens:
        overlap = len(eng_tokens & tr_tokens) / len(eng_tokens) * 100.0
        if overlap >= 75.0:
            return round(max(seq_ratio, overlap), 1), "Token Match"

    # 4. Translated content present
    return round(seq_ratio, 1), "Translated / Content Present"


def trim_white_borders(gray_img: np.ndarray, threshold: int = 245) -> np.ndarray:
    """
    Trim excess surrounding white padding from image so whitespace does not skew visual comparison.
    """
    mask = gray_img < threshold
    if not np.any(mask):
        return gray_img
    y_indices, x_indices = np.where(mask)
    y0, y1 = np.min(y_indices), np.max(y_indices)
    x0, x1 = np.min(x_indices), np.max(x_indices)
    pad = 3
    return gray_img[max(0, y0 - pad):min(gray_img.shape[0], y1 + pad + 1),
                    max(0, x0 - pad):min(gray_img.shape[1], x1 + pad + 1)]


def compute_visual_graphic_similarity(
    eng_pdf_path: str,
    tr_pdf_path: str,
    eng_page: int,
    tr_page: int,
    roi_rect: tuple[float, float, float, float],
    search_rect: tuple[float, float, float, float]
) -> float:
    """
    Universally masks out text spans from both English and Translated page copies
    and matches all non-text visual elements (headers, footers, divider lines, logos,
    circular badges, tables, shapes, frames, and icons).
    """
    try:
        # 1. English copy with text masked out
        with fitz.open(eng_pdf_path) as doc_e:
            p_e = doc_e[eng_page - 1]
            for b in p_e.get_text("dict", clip=fitz.Rect(roi_rect)).get("blocks", []):
                if b.get("type") == 0:
                    for l in b.get("lines", []):
                        p_e.draw_rect(fitz.Rect(l["bbox"]), color=(1, 1, 1), fill=(1, 1, 1))
            pix_e = p_e.get_pixmap(dpi=150, clip=fitz.Rect(roi_rect))
            data_e = np.frombuffer(pix_e.samples, dtype=np.uint8).reshape(pix_e.height, pix_e.width, pix_e.n)
            gray_e = cv2.cvtColor(data_e, cv2.COLOR_RGBA2GRAY if pix_e.n == 4 else cv2.COLOR_RGB2GRAY)

        # 2. Translated copy with text masked out
        with fitz.open(tr_pdf_path) as doc_t:
            p_t = doc_t[tr_page - 1]
            for b in p_t.get_text("dict", clip=fitz.Rect(search_rect)).get("blocks", []):
                if b.get("type") == 0:
                    for l in b.get("lines", []):
                        p_t.draw_rect(fitz.Rect(l["bbox"]), color=(1, 1, 1), fill=(1, 1, 1))
            pix_t = p_t.get_pixmap(dpi=150, clip=fitz.Rect(search_rect))
            data_t = np.frombuffer(pix_t.samples, dtype=np.uint8).reshape(pix_t.height, pix_t.width, pix_t.n)
            gray_t = cv2.cvtColor(data_t, cv2.COLOR_RGBA2GRAY if pix_t.n == 4 else cv2.COLOR_RGB2GRAY)

        # 3. Trim outer white padding
        trimmed_e = trim_white_borders(gray_e)
        trimmed_t = trim_white_borders(gray_t)

        # If both regions contain only masked text and no other graphics, return 100%
        if np.all(trimmed_e > 240) and np.all(trimmed_t > 240):
            return 100.0

        # 4. Normalized template matching across search window
        if trimmed_e.shape[0] <= trimmed_t.shape[0] and trimmed_e.shape[1] <= trimmed_t.shape[1]:
            res = cv2.matchTemplate(trimmed_t, trimmed_e, cv2.TM_CCOEFF_NORMED)
            sim = max(0.0, float(np.max(res))) * 100.0
        else:
            resized_t = cv2.resize(trimmed_t, (trimmed_e.shape[1], trimmed_e.shape[0]))
            res = cv2.matchTemplate(resized_t, trimmed_e, cv2.TM_CCOEFF_NORMED)
            sim = max(0.0, float(np.max(res))) * 100.0

        return round(min(100.0, sim), 1)
    except Exception as e:
        print(f"Warning: Visual graphic match failed: {e}")
        return 75.0


# ==============================================================================
# TOKEN-SNAPPED NEEDLE SEARCH (scoped exact match)
# ==============================================================================
# A sub-region marked "Exact Match" is not checked at fixed coordinates. Its box
# only DEFINES a needle on the master; the needle is then searched inside the
# parent region's scope on the target. This is deliberate: on a stylesheet-based
# manual the frame is stable but the flow inside it is not — a token like the
# date code slides by several points between languages because the document
# number and language code in front of it have different glyph widths.
#
# The needle snaps to token boundaries rather than using the raw rectangular
# clip, so a hand-drawn box a few points off still yields a clean token. Plain
# word splitting is not enough: an underscore-joined run such as
# "894387_5.0_en-US_2026-04_IOM_Start" is a single "word" to the text extractor.

TOKEN_SEPARATORS = set("_ \t\n\r\v\f\u00a0\u2007\u202f")


def extract_tokens(page, clip=None):
    """
    Split the text in `clip` into separator-delimited tokens.

    Returns a list of (text, fitz.Rect), each rect being the union of the
    character boxes that make up that token.
    """
    raw = page.get_text("rawdict", clip=fitz.Rect(clip)) if clip else page.get_text("rawdict")
    tokens, cur, box = [], "", None

    def flush():
        nonlocal cur, box
        if cur:
            tokens.append((cur, box))
        cur, box = "", None

    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    c = ch["c"]
                    if c in TOKEN_SEPARATORS:
                        flush()
                    else:
                        cur += c
                        cb = fitz.Rect(ch["bbox"])
                        box = cb if box is None else (box | cb)
            flush()
    flush()
    return tokens


def needle_from_region(pdf_path, page_num, inner_rect, coverage=0.5):
    """
    Derive the search needle from a region drawn on the master.

    A token joins the needle when at least `coverage` of its area falls inside
    the box, which is what makes the result stable against a few points of
    hand-drawing imprecision.
    """
    R = fitz.Rect(inner_rect)
    with fitz.open(pdf_path) as doc:
        total = len(doc)
        p_idx = (total - 1) if page_num == -1 else max(0, min(total - 1, page_num - 1))
        picked = []
        for text, box in extract_tokens(doc[p_idx]):
            if box is None or box.get_area() <= 0:
                continue
            if ((box & R).get_area() / box.get_area()) >= coverage:
                picked.append(text)
    # extract_tokens already walks blocks -> lines -> spans -> chars in reading
    # order; re-sorting by geometry would misplace glyphs whose baseline differs
    # slightly from their neighbours (the © in "© 2012 – 2026 Xylem Inc.").
    return " ".join(picked)


def count_needle_in_scope(pdf_path, page_num, parent_rect, needle):
    """Count occurrences of the needle's token sequence inside the parent scope."""
    want = needle.split()
    if not want:
        return 0
    with fitz.open(pdf_path) as doc:
        total = len(doc)
        p_idx = (total - 1) if page_num == -1 else max(0, min(total - 1, page_num - 1))
        toks = [t for t, _ in extract_tokens(doc[p_idx], clip=parent_rect)]
    return sum(1 for i in range(len(toks) - len(want) + 1) if toks[i:i + len(want)] == want)


def verify_needle_in_scope(target_pdf_path, target_page, parent_rect, needle):
    """
    Verify a needle appears exactly once inside the parent scope on the target.

      0 occurrences  -> FAIL   (the token is missing)
      1 occurrence   -> PASS
      2 or more      -> CHECK  (ambiguous: the check cannot tell which one it validated)
    """
    count = count_needle_in_scope(target_pdf_path, target_page, parent_rect, needle)
    if count == 1:
        return count, "PASS (Exact in Scope)", True
    if count == 0:
        return count, "FAIL (Not Found in Scope)", False
    return count, f"CHECK (Found {count}x in Scope)", False


def is_rect_contained_in_parent(child_rect: tuple, parent_rect: tuple, tolerance: float = 8.0) -> bool:
    """
    Returns True if child_rect is contained inside parent_rect.
    """
    cx0, cy0, cx1, cy1 = child_rect
    px0, py0, px1, py1 = parent_rect

    c_area = max(0.0, cx1 - cx0) * max(0.0, cy1 - cy0)
    p_area = max(0.0, px1 - px0) * max(0.0, py1 - py0)
    if c_area >= p_area * 0.96:
        return False

    return (
        (cx0 >= px0 - tolerance) and
        (cx1 <= px1 + tolerance) and
        (cy0 >= py0 - tolerance) and
        (cy1 <= py1 + tolerance)
    )


def generate_only_compared_image(
    eng_pdf_path: str,
    tr_pdf_path: str,
    eng_page: int,
    tr_page: int,
    roi_rect: tuple[float, float, float, float],
    search_rect: tuple[float, float, float, float],
    output_dir: str,
    region_label: str,
    tr_name: str,
    shift_y: float = 0.0,
    eng_name: str = ""
) -> str:
    """
    Generates ONLY the side-by-side compared image in the output folder.
    """
    safe_lbl = re.sub(r'[^\w\-_\.]', '_', region_label).strip('_')
    if not safe_lbl:
        safe_lbl = "Region"
    reg_dir = os.path.join(output_dir, f"{safe_lbl}_Pg{eng_page}")
    os.makedirs(reg_dir, exist_ok=True)

    # 1. Render English Crop to PIL
    with fitz.open(eng_pdf_path) as doc_e:
        p_e = doc_e[eng_page - 1]
        pix_e = p_e.get_pixmap(dpi=150, clip=fitz.Rect(roi_rect))
        img_e = Image.frombytes("RGBA" if pix_e.alpha else "RGB", [pix_e.width, pix_e.height], pix_e.samples).convert("RGB")

    # 2. Render Translated Crop to PIL
    with fitz.open(tr_pdf_path) as doc_t:
        p_t = doc_t[tr_page - 1]
        pix_t = p_t.get_pixmap(dpi=150, clip=fitz.Rect(search_rect))
        img_t = Image.frombytes("RGBA" if pix_t.alpha else "RGB", [pix_t.width, pix_t.height], pix_t.samples).convert("RGB")

    # 3. Create Side-by-Side Comparison Composite
    header_h = 32
    pad = 8
    total_w = img_e.width + img_t.width + (pad * 3)
    total_h = max(img_e.height, img_t.height) + header_h + (pad * 2)

    comp = Image.new("RGB", (total_w, total_h), color=(255, 255, 255))
    draw = ImageDraw.Draw(comp)

    # Header Left (English)
    draw.rectangle([0, 0, img_e.width + pad * 2, header_h], fill=(0, 125, 163))
    eng_label = os.path.splitext(eng_name)[0] if eng_name else "English Master"
    draw.text((pad + 2, 8), f"{eng_label} (Pg {eng_page})", fill=(255, 255, 255))

    # Header Right (Translated)
    tr_base = os.path.splitext(tr_name)[0]
    draw.rectangle([img_e.width + pad * 2, 0, total_w, header_h], fill=(0, 62, 81))
    shift_info = f" (Shift: {shift_y:+.1f}pt)" if shift_y != 0.0 else ""
    draw.text((img_e.width + pad * 3, 8), f"{tr_base} (Pg {tr_page}){shift_info}", fill=(255, 255, 255))

    # Paste crops
    comp.paste(img_e, (pad, header_h + pad))
    comp.paste(img_t, (img_e.width + pad * 2, header_h + pad))

    # Divider line
    draw.line([(img_e.width + pad * 2, 0), (img_e.width + pad * 2, total_h)], fill=(180, 200, 220), width=2)

    comp_path = os.path.join(reg_dir, f"Compare_{tr_base}.png")
    comp.save(comp_path)

    return comp_path


def search_and_verify_region_in_target(
    eng_pdf_path: str,
    tr_pdf_path: str,
    eng_page: int,
    is_last_page: bool,
    roi_rect: tuple[float, float, float, float],
    eng_text: str,
    exact_match_required: bool = False,
    dont_compare_text: bool = False,
    y_tolerance: float = 15.0,
    x_tolerance: float = 15.0,
    similarity_threshold: float = 75.0,
    output_crops_dir: str = None,
    region_label: str = "Region",
    scope_rect: tuple = None,
    needle: str = None
) -> dict:
    """
    Locate corresponding region in target translated PDF with flexible y-offset tolerance,
    extract translated content, verify presence & text/visual similarity, and generate compared images.
    """
    tr_name = os.path.basename(tr_pdf_path)
    if not os.path.exists(tr_pdf_path):
        return {
            "tr_name": tr_name,
            "status": "FAIL (File Not Found)",
            "similarity": 0.0,
            "shift_y": 0.0,
            "tr_text": "",
            "is_match": False,
            "comparison_img_path": "",
        }

    try:
        with fitz.open(tr_pdf_path) as doc_tr:
            total_tr = len(doc_tr)
            if total_tr == 0:
                return {
                    "tr_name": tr_name,
                    "status": "FAIL (Empty PDF)",
                    "similarity": 0.0,
                    "shift_y": 0.0,
                    "tr_text": "",
                    "is_match": False,
                    "comparison_img_path": "",
                }

            p_idx = (total_tr - 1) if (is_last_page or eng_page == -1) else max(0, min(total_tr - 1, eng_page - 1))
            tr_page_num = p_idx + 1
            tr_page = doc_tr[p_idx]
            pw, ph = tr_page.rect.width, tr_page.rect.height

            # Search window, widened on both axes. Translated text reflows
            # vertically, but it also changes width - a longer word pushes the
            # right edge of a block out, and a narrower language code shifts a
            # whole footer line sideways - so the horizontal slack matters too.
            x0, y0, x1, y1 = roi_rect
            search_x0 = max(0.0, x0 - x_tolerance)
            search_x1 = min(pw, x1 + x_tolerance)
            search_y0 = max(0.0, y0 - y_tolerance)
            search_y1 = min(ph, y1 + y_tolerance)
            search_rect = (search_x0, search_y0, search_x1, search_y1)

            tr_text = tr_page.get_text("text", clip=fitz.Rect(search_rect)).strip()
            has_content = len(tr_text) > 0

            # Estimate shift offset
            shift_y = 0.0
            if has_content:
                blocks = tr_page.get_text("blocks", clip=fitz.Rect(search_rect))
                if blocks:
                    first_b_y0 = min(b[1] for b in blocks)
                    shift_y = round(first_b_y0 - y0, 1)

            # Determine similarity & status based on region mode
            needle_count = -1
            if dont_compare_text:
                # Mode 1: Don't Compare Text (Visual Match)
                vis_sim = compute_visual_graphic_similarity(
                    eng_pdf_path=eng_pdf_path,
                    tr_pdf_path=tr_pdf_path,
                    eng_page=eng_page,
                    tr_page=tr_page_num,
                    roi_rect=roi_rect,
                    search_rect=search_rect
                )
                similarity = vis_sim
                match_desc = "Visual Graphic Match (Ignoring Text)"

                if vis_sim >= 75.0:
                    status = "PASS (Visual Match)"
                    is_match = True
                elif vis_sim >= similarity_threshold:
                    status = "CHECK (Visual Partial)"
                    is_match = True
                else:
                    status = "FAIL (Visual Mismatch)"
                    is_match = False

            elif exact_match_required and scope_rect and needle:
                # Mode 2a: Scoped Exact Match (sub-region inside a parent region).
                # The sub-region's box defined the needle on the master; here we
                # only ask whether that exact token sequence appears inside the
                # parent's scope, so drift within the block is irrelevant.
                sx0, sy0, sx1, sy1 = scope_rect
                scope = (max(0.0, sx0 - x_tolerance), max(0.0, sy0 - y_tolerance),
                         min(pw, sx1 + x_tolerance), min(ph, sy1 + y_tolerance))
                needle_count, status, is_match = verify_needle_in_scope(
                    tr_pdf_path, tr_page_num, scope, needle
                )
                similarity = 100.0 if is_match else 0.0
                match_desc = f"Scoped Exact Match ({needle!r} x{needle_count})"

            elif exact_match_required:
                # Mode 2b: Exact Match Required (100%) at the region's own position
                similarity, match_desc = compute_region_similarity(eng_text, tr_text)
                if similarity >= 98.0:
                    status = "PASS (Exact Match)"
                    is_match = True
                else:
                    status = "FAIL (Exact Required)"
                    is_match = False

            else:
                # Mode 3: Normal Layout Presence & Translation Check
                similarity, match_desc = compute_region_similarity(eng_text, tr_text)
                if similarity >= 98.0:
                    status = "PASS (Exact)"
                    is_match = True
                elif similarity >= 85.0:
                    status = "PASS (Near)"
                    is_match = True
                elif has_content:
                    status = "PASS (Present)" if similarity >= similarity_threshold else "CHECK (Translated)"
                    is_match = True
                else:
                    status = "FAIL (Empty Region)"
                    is_match = False

            # Generate ONLY side-by-side compared image
            comp_path = ""
            if output_crops_dir:
                try:
                    comp_path = generate_only_compared_image(
                        eng_pdf_path=eng_pdf_path,
                        tr_pdf_path=tr_pdf_path,
                        eng_page=eng_page,
                        tr_page=tr_page_num,
                        roi_rect=roi_rect,
                        search_rect=search_rect,
                        output_dir=output_crops_dir,
                        region_label=region_label,
                        tr_name=tr_name,
                        shift_y=shift_y,
                        eng_name=os.path.basename(eng_pdf_path)
                    )
                except Exception as e:
                    print(f"Warning: Failed to generate compared crop for {tr_name}: {e}")

            return {
                "tr_name": tr_name,
                "eng_name": os.path.basename(eng_pdf_path),
                "target_page": tr_page_num,
                "status": status,
                "similarity": similarity,
                "match_desc": match_desc,
                "shift_y": shift_y,
                "tr_text": tr_text,
                "is_match": is_match,
                "has_content": has_content,
                "exact_required": exact_match_required,
                "dont_compare_text": dont_compare_text,
                "scoped": bool(exact_match_required and scope_rect and needle),
                "needle": needle or "",
                "needle_count": needle_count,
                "comparison_img_path": comp_path,
            }
    except Exception as e:
        return {
            "tr_name": tr_name,
            "status": f"ERROR ({e})",
            "similarity": 0.0,
            "shift_y": 0.0,
            "tr_text": "",
            "is_match": False,
            "comparison_img_path": "",
        }


def run_batch_multiple_regions_check(
    eng_pdf_path: str,
    tr_target: str,
    regions: list[dict],
    y_tolerance: float = 15.0,
    x_tolerance: float = 15.0,
    similarity_threshold: float = 75.0,
    output_crops_dir: str = DEFAULT_CROPS_OUTPUT_DIR,
    progress_callback=None
) -> list[dict]:
    """
    Run verification and comparison images for user-defined regions across all
    translated PDFs.

    Each region is first expanded across its page scope: a region scoped to
    "all pages" becomes one check per page, "last" resolves against the document
    actually in hand rather than a page number captured when it was drawn.
    Results are then collapsed by variant group, so a mirrored header that
    matches either its left- or right-aligned alternative counts as one pass.
    """
    os.makedirs(output_crops_dir, exist_ok=True)

    by_id = {r["id"]: r for r in regions if r.get("id") is not None}

    with fitz.open(eng_pdf_path) as doc_eng:
        master_pages = len(doc_eng)

    # Expand scopes against the MASTER: this fixes which master pages each
    # region is checked from. Where it lands in each translation is resolved
    # per target inside search_and_verify_region_in_target.
    regions = expand_regions_for_document(regions, master_pages)

    with fitz.open(eng_pdf_path) as doc_eng:
        for r in regions:
            r["eng_text"] = extract_roi_text(doc_eng, r["page_num"], r["roi_rect"])

    # A sub-region marked "Exact Match" is checked against its parent's scope:
    # its own box only defines the needle. Resolve that pairing up front.
    for r in regions:
        parent = by_id.get(r.get("parent_id")) if r.get("parent_id") else None
        if parent is not None and r.get("exact_match", False):
            r["_scope_rect"] = parent["roi_rect"]
            r["_needle"] = needle_from_region(eng_pdf_path, r["page_num"], r["roi_rect"])
        else:
            r["_scope_rect"] = None
            r["_needle"] = None

    tr_files = []
    if os.path.isfile(tr_target):
        tr_files.append(tr_target)
    elif os.path.isdir(tr_target):
        for root, _, files in os.walk(tr_target):
            for f in sorted(files):
                if f.lower().endswith(".pdf"):
                    tr_files.append(os.path.join(root, f))

    # Regions flagged scope_only exist purely to bound their sub-regions'
    # searches. They still resolve as parents above, but are never verified.
    checkable = [r for r in regions if not r.get("scope_only", False)]

    all_results = []
    total_steps = len(checkable) * len(tr_files)
    step = 0

    for r in checkable:
        for tr_path in tr_files:
            step += 1
            if progress_callback:
                progress_callback(step, total_steps, r["label"], os.path.basename(tr_path))

            res = search_and_verify_region_in_target(
                eng_pdf_path=eng_pdf_path,
                tr_pdf_path=tr_path,
                eng_page=r["page_num"],
                is_last_page=r.get("is_last_page", False),
                roi_rect=r["roi_rect"],
                eng_text=r.get("eng_text", ""),
                exact_match_required=r.get("exact_match", False),
                dont_compare_text=r.get("dont_compare_text", False),
                y_tolerance=y_tolerance,
                x_tolerance=x_tolerance,
                similarity_threshold=similarity_threshold,
                output_crops_dir=output_crops_dir,
                region_label=r["label"],
                scope_rect=r.get("_scope_rect"),
                needle=r.get("_needle")
            )
            res["region_label"] = r["label"]
            res["eng_page"] = r["page_num"]
            res["eng_text"] = r.get("eng_text", "")
            res["roi_rect"] = r["roi_rect"]
            res["variant_group"] = r.get("variant_group")
            res["page_scope"] = r.get("page_scope")
            all_results.append(res)

    # Alternatives in a variant group are OR-ed: one match on a page is enough.
    return collapse_variant_results(all_results)

