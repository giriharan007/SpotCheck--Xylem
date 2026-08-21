"""
test_topic_image_extraction.py

Standalone Test: Topic-Wise Image Extraction & Comparison.

Extracts images grouped by TOC topic (not page-wise) from English and
Translated PDFs, then compares image counts per topic code.

Usage:
  python test_topic_image_extraction.py
  python test_topic_image_extraction.py --compare
  python test_topic_image_extraction.py --toc-only
  python test_topic_image_extraction.py --english "path.pdf" --translated "path_tr.pdf" --compare
"""

import os
import sys
import re
import argparse
import pymupdf as fitz
import pdfplumber

try:
    import Barcode_QR_Check
except ImportError:
    import SpotCheck.Barcode_QR_Check as Barcode_QR_Check

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ==============================================================================
# CONFIGURATION
# ==============================================================================
DEFAULT_ENGLISH = r"Input\English\894387_5.0_en-US_2026-04_IOM.Start350.pdf"
DEFAULT_TRANSLATED_DIR = r"Input\Translated"
OUTPUT_DIR = r"Output_Cropped_Images"
DPI = 150
HEADER_MARGIN = 50
FOOTER_MARGIN = 40
MASK_TEXT_IN_CROPS = True
# ==============================================================================


# ---------------------------------------------------------------------------
# TOC / Topic Extraction
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# TOC / Topic Extraction with Spatial Positions
# ---------------------------------------------------------------------------
def extract_topics_with_positions(pdf_path: str) -> list[dict]:
    """
    Extract structured topics from PDF bookmarks (TOC) including exact start_page and top_y coordinates.
    """
    topics = []
    with fitz.open(pdf_path) as doc:
        toc = doc.get_toc(simple=False)
        total_pages = len(doc)

        if not toc:
            print("  [INFO] No TOC bookmarks found. Falling back to page-wise extraction.")
            for p_idx in range(total_pages):
                topics.append({
                    "code": f"Page_{p_idx + 1:03d}",
                    "title": f"Page {p_idx + 1}",
                    "start_page": p_idx + 1,
                    "top_y": 0.0,
                    "lvl": 1,
                    "idx": p_idx,
                })
            return topics

        for idx, item in enumerate(toc):
            lvl, raw_title, start_page, dest = item
            clean_title = raw_title.replace("\xa0", " ").strip()

            m = re.match(r"^\s*(\d+(?:\.\d+)*)", clean_title)
            code = m.group(1) if m else f"UN_{idx}"

            title_text = re.sub(r"^\s*\d+(?:\.\d+)*\s*", "", clean_title).strip()
            if not title_text:
                title_text = clean_title

            page = doc[start_page - 1]
            top_y = 0.0
            if dest and dest.get("to"):
                to_y = dest["to"].y
                if to_y > 0:
                    top_y = max(0.0, page.rect.height - to_y)

            topics.append({
                "code": code,
                "title": title_text,
                "start_page": start_page,
                "top_y": top_y,
                "lvl": lvl,
                "idx": idx,
            })

    return topics


# ---------------------------------------------------------------------------
# Image Detection Helpers
# ---------------------------------------------------------------------------
def merge_rects_tight(rect_list, gap=2):
    """
    Tightly merges bounding boxes that intersect or touch (gap <= 2pt),
    or are vertically aligned sub-parts of a single icon.
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
                
                exp = fitz.Rect(cur.x0 - gap, cur.y0 - gap, cur.x1 + gap, cur.y1 + gap)
                should_merge = exp.intersects(rj)
                
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


def get_all_image_candidates(fitz_page, header_margin=HEADER_MARGIN, footer_margin=FOOTER_MARGIN):
    """Get bounding boxes of all raster images, logos, and vector drawing icons/diagrams."""
    page_rect = fitz_page.rect
    raw_elements = []

    # 1. Raster images & logos
    try:
        for img in fitz_page.get_image_info():
            if 'bbox' in img:
                r = fitz.Rect(img['bbox'])
                if r.y1 > header_margin and r.y0 < (page_rect.height - footer_margin):
                    if (r.x0 < 35 and r.width < 40) or (r.x1 > page_rect.width - 35 and r.width < 40):
                        continue
                    raw_elements.append(r)
    except Exception:
        pass

    # 2. Image text blocks (type == 1)
    try:
        text_dict = fitz_page.get_text("dict")
        for b in text_dict.get("blocks", []):
            if b.get("type") == 1:
                r = fitz.Rect(b["bbox"])
                if r.y1 > header_margin and r.y0 < (page_rect.height - footer_margin):
                    if (r.x0 < 35 and r.width < 40) or (r.x1 > page_rect.width - 35 and r.width < 40):
                        continue
                    raw_elements.append(r)
    except Exception:
        pass

    # 3. Vector drawings
    try:
        drawings = fitz_page.get_drawings()
        for d in drawings:
            r = fitz.Rect(d['rect'])
            if r.y1 <= header_margin or r.y0 >= (page_rect.height - footer_margin):
                continue
            # Ignore language thumb tabs / outer side margin indicators
            if (r.x0 < 38 and r.width < 40) or (r.x1 > page_rect.width - 38 and r.width < 40):
                continue
            # Ignore thin divider rules
            if (r.height <= 2.5 and r.width > 20) or (r.width <= 2.5 and r.height > 20):
                continue
            # Ignore full-page backgrounds / huge tint boxes
            if r.width * r.height > (page_rect.width * page_rect.height * 0.4):
                continue
            if r.width > 1 and r.height > 1:
                raw_elements.append(r)
    except Exception:
        pass

    if not raw_elements:
        return []

    merged_rects = merge_rects_tight(raw_elements, gap=2)

    final_candidates = []
    for r in merged_rects:
        if (r.width >= 12 and r.height >= 12) or (r.width * r.height >= 140):
            clamped = fitz.Rect(
                max(0, r.x0), max(0, r.y0),
                min(page_rect.width, r.x1), min(page_rect.height, r.y1)
            )
            if clamped.width > 5 and clamped.height > 5:
                final_candidates.append(clamped)

    final_candidates.sort(key=lambda r: (r.y0, r.x0))
    return final_candidates


def get_table_bboxes(fitz_page, pdfplumber_page=None):
    """Get table bounding boxes."""
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


def mask_text_inside_rect(page, rect):
    """Cover text spans inside rect with white."""
    try:
        text_dict = page.get_text("dict")
        for b in text_dict.get("blocks", []):
            if b.get("type") == 0:
                for line in b.get("lines", []):
                    l_rect = fitz.Rect(line["bbox"])
                    if rect.intersects(l_rect):
                        page.draw_rect(l_rect, color=(1, 1, 1), fill=(1, 1, 1))
    except Exception:
        pass


def detect_barcodes_and_qr_codes(page, dpi=150):
    """Delegate barcode/QR detection."""
    return Barcode_QR_Check.detect_barcodes_and_qr_codes(page, dpi=dpi)


def find_topic_for_image_rect(page_num, rect, topics):
    """
    Find which topic heading an image belongs to based on spatial position (page_num, rect.y0).
    """
    img_pos = (page_num, rect.y0 + rect.height * 0.3)
    best_topic = None
    for t in topics:
        t_pos = (t["start_page"], t["top_y"])
        if t_pos <= img_pos:
            best_topic = t
        else:
            break
    if best_topic is None and topics:
        best_topic = topics[0]
    return best_topic


# ---------------------------------------------------------------------------
# Core: Topic-Wise Image Extraction
# ---------------------------------------------------------------------------
def crop_images_by_topic(pdf_path, output_dir=OUTPUT_DIR, dpi=DPI,
                         header_margin=HEADER_MARGIN, footer_margin=FOOTER_MARGIN,
                         mask_text=MASK_TEXT_IN_CROPS):
    """
    Extract and crop images organized accurately by TOC topic headings.
    Output: output_dir/{pdf_name}/{topic_code}_{title}/crop_*.png
    """
    print(f"\n{'=' * 70}")
    print(f"TOPIC-WISE Image Extraction: {os.path.basename(pdf_path)}")
    print(f"{'=' * 70}")

    topics = extract_topics_with_positions(pdf_path)
    print(f"  Found {len(topics)} topics from TOC bookmarks\n")

    doc = fitz.open(pdf_path)
    pdf_name = os.path.splitext(os.path.basename(pdf_path))[0]

    try:
        plumb_doc = pdfplumber.open(pdf_path)
    except Exception:
        plumb_doc = None

    topic_crops = {t["code"]: [] for t in topics}

    try:
        for page_idx in range(len(doc)):
            page_num = page_idx + 1
            page = doc[page_idx]
            page_rect = page.rect

            plumb_page = (
                plumb_doc.pages[page_idx]
                if plumb_doc and page_idx < len(plumb_doc.pages)
                else None
            )
            table_rects = get_table_bboxes(page, plumb_page)

            detected_codes = detect_barcodes_and_qr_codes(page, dpi=dpi)
            code_rects = [c["rect"] for c in detected_codes]

            image_rects = get_all_image_candidates(
                page, header_margin=header_margin, footer_margin=footer_margin,
            )

            crops_on_page = []
            for img_rect in image_rects:
                if img_rect.y1 <= header_margin or img_rect.y0 >= (page_rect.height - footer_margin):
                    continue

                is_barcode_or_qr = False
                for c_rect in code_rects:
                    padded_c = fitz.Rect(c_rect.x0 - 5, c_rect.y0 - 5, c_rect.x1 + 5, c_rect.y1 + 5)
                    if padded_c.contains(img_rect) or padded_c.intersects(img_rect):
                        intersect = padded_c & img_rect
                        if intersect.width * intersect.height > 0:
                            is_barcode_or_qr = True
                            break
                if is_barcode_or_qr:
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
                if mask_text:
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
                        matched_t = find_topic_for_image_rect(page_num, rect, topics)
                        if matched_t:
                            topic_crops[matched_t["code"]].append({
                                "page_num": page_num,
                                "label": label,
                                "pixmap": crop_pix,
                                "rect": rect,
                            })
    finally:
        if plumb_doc:
            plumb_doc.close()
        doc.close()

    total_crops = 0
    topic_summary = []

    for topic in topics:
        code = topic["code"]
        title = topic["title"]
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title)[:40].strip().rstrip('.')
        folder_name = f"{code}_{safe_title}" if safe_title else code
        topic_out_dir = os.path.join(output_dir, pdf_name, folder_name)

        crops = topic_crops.get(code, [])
        topic_crop_count = len(crops)

        if topic_crop_count > 0:
            os.makedirs(topic_out_dir, exist_ok=True)
            for idx, c_info in enumerate(crops, start=1):
                p_num = c_info["page_num"]
                c_lbl = c_info["label"]
                pix = c_info["pixmap"]
                crop_filename = f"p{p_num:03d}_crop_{idx:02d}_{c_lbl}.png"
                pix.save(os.path.join(topic_out_dir, crop_filename))
                total_crops += 1

            print(f"  Topic {code:8s} | {title[:35]:35s} | Start Pg {topic['start_page']:2d} | {topic_crop_count:2d} image(s) -> {folder_name}")
        else:
            print(f"  Topic {code:8s} | {title[:35]:35s} | Start Pg {topic['start_page']:2d} |  0 images")

        topic_summary.append({
            "code": code,
            "title": title,
            "start_page": topic["start_page"],
            "image_count": topic_crop_count,
        })

    print(f"\n  Total crops saved: {total_crops}")
    print(f"  Output directory : {os.path.join(output_dir, pdf_name)}")
    print(f"{'=' * 70}\n")

    return {
        "pdf": os.path.basename(pdf_path),
        "total_topics": len(topics),
        "total_images": total_crops,
        "topics": topic_summary,
    }


# ---------------------------------------------------------------------------
# Compare Image Counts Between English & Translated (Topic-Wise)
# ---------------------------------------------------------------------------
def compare_image_counts_by_topic(eng_pdf_path, tr_pdf_path, output_dir=OUTPUT_DIR, dpi=DPI):
    """Extract images by topic for both PDFs, then compare counts per topic code."""
    print(f"\n{'=' * 90}")
    print("TOPIC-WISE IMAGE COUNT COMPARISON")
    print(f"{'=' * 90}")
    print(f"  English PDF   : {os.path.basename(eng_pdf_path)}")
    print(f"  Translated PDF: {os.path.basename(tr_pdf_path)}")
    print(f"{'=' * 90}\n")

    eng_result = crop_images_by_topic(eng_pdf_path, output_dir, dpi=dpi)
    tr_result = crop_images_by_topic(tr_pdf_path, output_dir, dpi=dpi)

    eng_by_code = {t["code"]: t for t in eng_result["topics"]}
    tr_by_code = {t["code"]: t for t in tr_result["topics"]}

    all_codes = list(dict.fromkeys(
        [t["code"] for t in eng_result["topics"]] +
        [t["code"] for t in tr_result["topics"]]
    ))

    print(f"\n{'=' * 90}")
    print("IMAGE COUNT COMPARISON BY TOPIC")
    print(f"{'=' * 90}")
    print(f"{'Code':8s} | {'Topic Title':35s} | {'Eng Imgs':>8s} | {'Tr Imgs':>8s} | {'Match':8s}")
    print("-" * 90)

    mismatches = []
    for code in all_codes:
        eng_t = eng_by_code.get(code)
        tr_t = tr_by_code.get(code)
        eng_count = eng_t["image_count"] if eng_t else 0
        tr_count = tr_t["image_count"] if tr_t else 0
        title = (eng_t or tr_t)["title"][:35]

        if eng_count == tr_count:
            status = "[PASS]"
        elif eng_count == 0 and tr_count == 0:
            status = "[ -- ]"
        else:
            status = "[FAIL]"
            mismatches.append(code)

        print(f"{code:8s} | {title:35s} | {eng_count:8d} | {tr_count:8d} | {status}")

    print(f"\n{'=' * 90}")
    overall = "PASS" if not mismatches else "FAIL"
    print(f"Overall Image Count Check: [{overall}]")
    print(f"  English Total   : {eng_result['total_images']} images across {eng_result['total_topics']} topics")
    print(f"  Translated Total: {tr_result['total_images']} images across {tr_result['total_topics']} topics")
    if mismatches:
        print(f"  Mismatched Topics: {', '.join(mismatches)}")
    print(f"{'=' * 90}\n")

    return {
        "english_pdf": eng_result["pdf"],
        "translated_pdf": tr_result["pdf"],
        "eng_total_images": eng_result["total_images"],
        "tr_total_images": tr_result["total_images"],
        "mismatched_topics": mismatches,
        "status": overall,
    }


# ---------------------------------------------------------------------------
# Test Runners
# ---------------------------------------------------------------------------
def test_toc_only(pdf_path):
    """Print the TOC topic structure with start pages."""
    print(f"\n{'=' * 75}")
    print(f"TOC TOPIC STRUCTURE: {os.path.basename(pdf_path)}")
    print(f"{'=' * 75}")

    topics = extract_topics_with_positions(pdf_path)

    print(f"  Total topics found: {len(topics)}\n")
    print(f"  {'#':>3s}  {'Code':8s}  {'Title':40s}  {'Start Page':12s}")
    print(f"  {'-' * 68}")

    for i, t in enumerate(topics, 1):
        print(f"  {i:3d}  {t['code']:8s}  {t['title'][:40]:40s}  Page {t['start_page']:2d}")

    print(f"{'=' * 75}\n")


def test_all_translations(eng_path, tr_dir, output_dir=OUTPUT_DIR):
    """Compare English PDF against all translated PDFs in a directory."""
    tr_files = sorted([
        os.path.join(tr_dir, f)
        for f in os.listdir(tr_dir)
        if f.lower().endswith(".pdf")
    ])

    if not tr_files:
        print(f"No translated PDFs found in: {tr_dir}")
        return

    print(f"\n{'=' * 90}")
    print(f"BATCH TOPIC-WISE IMAGE COMPARISON")
    print(f"{'=' * 90}")
    print(f"  English PDF    : {os.path.basename(eng_path)}")
    print(f"  Translated PDFs: {len(tr_files)} file(s) in {tr_dir}")
    print(f"{'=' * 90}\n")

    summary = []
    for idx, tr_path in enumerate(tr_files, 1):
        print(f"\n[{idx}/{len(tr_files)}] Processing: {os.path.basename(tr_path)}")
        result = compare_image_counts_by_topic(eng_path, tr_path, output_dir)
        summary.append(result)

    print(f"\n{'=' * 90}")
    print(f"BATCH COMPARISON SUMMARY")
    print(f"{'=' * 90}")
    print(f"{'#':>3s}  {'Translated PDF':45s}  {'Eng Imgs':>8s}  {'Tr Imgs':>8s}  {'Mismatches':>10s}  {'Status':8s}")
    print(f"-" * 90)

    for idx, r in enumerate(summary, 1):
        mismatch_count = len(r.get("mismatched_topics", []))
        print(
            f"{idx:3d}  {r['translated_pdf'][:45]:45s}  "
            f"{r['eng_total_images']:8d}  {r['tr_total_images']:8d}  "
            f"{mismatch_count:10d}  [{r['status']}]"
        )

    print(f"{'=' * 90}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Test Topic-Wise Image Extraction & Comparison."
    )
    parser.add_argument("--english", default=DEFAULT_ENGLISH, help="English Master PDF path")
    parser.add_argument("--translated", default=DEFAULT_TRANSLATED_DIR, help="Translated PDF file or directory")
    parser.add_argument("--output", default=OUTPUT_DIR, help=f"Output directory (default: {OUTPUT_DIR})")
    parser.add_argument("--compare", action="store_true", help="Run comparison mode (English vs Translated)")
    parser.add_argument("--toc-only", action="store_true", help="Only print TOC topic structure")

    args = parser.parse_args()

    if args.toc_only:
        test_toc_only(args.english)
        return

    if args.compare:
        if os.path.isfile(args.translated):
            compare_image_counts_by_topic(args.english, args.translated, args.output)
        elif os.path.isdir(args.translated):
            test_all_translations(args.english, args.translated, args.output)
        else:
            print(f"Error: Translated path not found: {args.translated}")
    else:
        crop_images_by_topic(args.english, args.output)


if __name__ == "__main__":
    main()
