import os
import re
import sys
import argparse
import numpy as np
import pymupdf as fitz  # PyMuPDF (aliased as fitz for API compat)
import pdfplumber

from core import barcode_qr as Barcode_QR_Check
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


def topic_slug(topic, max_len=60):
    """A filesystem-safe folder name for a topic."""
    if not topic:
        return "_No topic"
    name = topic.get("title") or "Untitled"
    name = _TOPIC_SLUG_BAD.sub("_", name).strip().rstrip(". ")
    name = re.sub(r"\s+", " ", name)
    if len(name) > max_len:
        name = name[:max_len].rstrip() + "..."
    return name or "Untitled"


def find_topic_for_rect(page_num, rect, topics):
    """The last topic that begins at or above this rect, in reading order."""
    if not topics:
        return None
    pos = (page_num, rect.y0 + rect.height * 0.3)
    best = None
    for t in topics:
        if (t["start_page"], t["top_y"]) <= pos:
            best = t
        else:
            break
    return best or topics[0]


# A region that renders essentially empty carries nothing to compare, and a
# blank crop matches anything at 100%. Real content never trips this: across the
# master and all eleven translations of the Start 350 manual, zero candidates
# fall below the threshold. It does catch a graphic that has been painted over,
# which a purely geometric check would still count as present.
BLANK_INK_THRESHOLD = 0.002


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


def get_all_image_candidates(fitz_page, header_margin=None, footer_margin=None,
                             margins=None, include_ignored=False):
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

    # Kept separate from the margin test: this is the narrow-artifact heuristic
    # that removes language thumb tabs and edge crop marks, and it is not the
    # same rule as "sits inside the side margin". Both run.
    def is_edge_artifact(r, reach=36):
        return ((r.x0 < reach and r.width < 40)
                or (r.x1 > pw - reach and r.width < 40))

    raw_elements = []          # everything drawn, before clustering
    ignored = []               # (rect, reason) - only collected when asked for

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
        return [] if not include_ignored else [(r, s) for r, s in ignored]

    # Tight clustering to assemble vector paths into individual icons without merging separate graphics
    merged_rects = merge_rects_tight(raw_elements, gap=2)

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
                      mask_text_in_crops=MASK_TEXT_IN_CROPS, margins=None):
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

    try:
        plumb_doc = pdfplumber.open(pdf_path)
    except Exception:
        plumb_doc = None

    total_crops = 0

    try:
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            page_rect = page.rect
            page_num = page_idx + 1

            page_out_dir = os.path.join(output_dir, pdf_name, f"page_{page_num:03d}")  # page-wise fallback

            # Get table bounding boxes
            plumb_page = plumb_doc.pages[page_idx] if plumb_doc and page_idx < len(plumb_doc.pages) else None
            table_rects = get_table_bboxes(page, plumb_page)

            # Detect Barcodes and QR Codes on current page to exclude them from cropping
            detected_codes = detect_barcodes_and_qr_codes(page, dpi=dpi)
            code_rects = [c["rect"] for c in detected_codes]

            # Margins can be switched off on named pages (a cover that is
            # deliberately all furniture, say), so they are resolved per page.
            page_margins_here = page_margins.margins_for_page(
                active_margins, page_num, len(doc))
            image_rects = get_all_image_candidates(page, margins=page_margins_here)

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

                        os.makedirs(out_dir_for_crop, exist_ok=True)
                        crop_path = os.path.join(out_dir_for_crop, crop_filename)
                        crop_pix.save(crop_path)
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
