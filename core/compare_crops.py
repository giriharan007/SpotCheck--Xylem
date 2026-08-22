import os
import re
import sys
import argparse
import pymupdf as fitz  # PyMuPDF (aliased as fitz for API compat)
import cv2
import numpy as np

# ==============================================================================
# CONFIGURATION - EDIT YOUR PATHS HERE DIRECTLY
# ==============================================================================
ENGLISH_CROPS_DIR = r"Output\Cropped_Images"
TRANSLATED_PDF_DIR = r"Input\Translated"
OUTPUT_REPORT_DIR = r"Output\Cropped_Comparison"
SIMILARITY_THRESHOLD = 80.0  # Pass threshold percentage (e.g. 80.0%)
DPI = 150
# ==============================================================================

def render_translated_pdf_pages_pure_graphics(trans_pdf_path, dpi=DPI):
    """
    Render translated PDF pages into OpenCV image matrices with text characters masked out,
    isolating pure visual graphics (Flygt logo, vector lines, gray boxes, icons, diagrams, shapes).
    """
    doc = fitz.open(trans_pdf_path)
    bgr_imgs = []
    gray_imgs = []
    try:
        for p_idx in range(len(doc)):
            page = doc[p_idx]
            
            # Mask text spans on page to isolate pure visual graphics
            text_dict = page.get_text("dict")
            for b in text_dict.get("blocks", []):
                if b.get("type") == 0:  # Text block
                    for line in b.get("lines", []):
                        l_rect = fitz.Rect(line["bbox"])
                        page.draw_rect(l_rect, color=(1, 1, 1), fill=(1, 1, 1))

            pix = page.get_pixmap(dpi=dpi)
            data = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            if pix.n == 4:
                img_gray = cv2.cvtColor(data, cv2.COLOR_RGBA2GRAY)
                img_bgr = cv2.cvtColor(data, cv2.COLOR_RGBA2BGR)
            elif pix.n == 3:
                img_gray = cv2.cvtColor(data, cv2.COLOR_RGB2GRAY)
                img_bgr = cv2.cvtColor(data, cv2.COLOR_RGB2BGR)
            else:
                img_gray = data
                img_bgr = cv2.cvtColor(data, cv2.COLOR_GRAY2BGR)
            bgr_imgs.append(img_bgr)
            gray_imgs.append(img_gray)
    finally:
        doc.close()
    return bgr_imgs, gray_imgs

_PAGE_IN_NAME = re.compile(r"^p(\d{3,4})_", re.IGNORECASE)
_PAGE_IN_FOLDER = re.compile(r"^page_(\d+)$", re.IGNORECASE)


def _iter_english_crops(eng_crop_dir):
    """
    Walk the master's crop tree, whichever layout it uses.

    crop_images writes topic folders when the PDF has a TOC and page folders when
    it does not, so the page number is recovered from the filename prefix first
    (topic layout) and from the folder name otherwise. Yields
    (folder, page_number, topic_name, filename), ordered by page then filename so
    the comparison sequence does not depend on the layout.
    """
    rows = []
    for root, _dirs, files in os.walk(eng_crop_dir):
        folder = os.path.basename(root)
        if os.path.abspath(root) == os.path.abspath(eng_crop_dir):
            continue
        m_folder = _PAGE_IN_FOLDER.match(folder)
        for f in files:
            if not f.lower().endswith(".png"):
                continue
            # Barcodes and QR codes are excluded: their contents legitimately
            # differ between documents, so a visual diff is meaningless.
            if any(k in f.lower() for k in ("barcode", "qr", "qrcode")):
                continue
            m_name = _PAGE_IN_NAME.match(f)
            if m_name:
                page, topic = int(m_name.group(1)), folder
            elif m_folder:
                page, topic = int(m_folder.group(1)), ""
            else:
                continue        # unrecognised layout, skip rather than guess
            rows.append((root, page, topic, f))
    rows.sort(key=lambda r: (r[1], r[3]))
    return rows


def create_crop_match_image(crop_img_gray, target_page_bgr, max_loc, max_val, eng_page, trans_page):
    """
    Generate side-by-side comparison image of English crop vs Matched Translated region.
    """
    h_c, w_c = crop_img_gray.shape
    x0, y0 = max_loc
    x1, y1 = min(target_page_bgr.shape[1], x0 + w_c), min(target_page_bgr.shape[0], y0 + h_c)

    trans_crop_bgr = target_page_bgr[y0:y1, x0:x1].copy()

    score_pct = max_val * 100
    box_color = (0, 180, 0) if score_pct >= SIMILARITY_THRESHOLD else (0, 0, 220)

    crop_img_bgr = cv2.cvtColor(crop_img_gray, cv2.COLOR_GRAY2BGR)

    h_max = max(crop_img_bgr.shape[0], trans_crop_bgr.shape[0], 50)
    w1 = crop_img_bgr.shape[1]
    w2 = trans_crop_bgr.shape[1]

    canvas = np.zeros((h_max + 40, w1 + w2 + 30, 3), dtype=np.uint8)
    canvas.fill(255)  # White canvas

    canvas[35:35 + crop_img_bgr.shape[0], 10:10 + w1] = crop_img_bgr
    canvas[35:35 + trans_crop_bgr.shape[0], 20 + w1:20 + w1 + w2] = trans_crop_bgr

    cv2.rectangle(canvas, (20 + w1, 35), (20 + w1 + w2 - 1, 35 + trans_crop_bgr.shape[0] - 1), box_color, 2)

    font = cv2.FONT_HERSHEY_SIMPLEX
    status_text = "PASS" if score_pct >= SIMILARITY_THRESHOLD else "CHECK"
    cv2.putText(canvas, f"English Graphic (Pg {eng_page})", (10, 22), font, 0.45, (0, 100, 0), 1)
    cv2.putText(canvas, f"Translated (Pg {trans_page}): {score_pct:.1f}% [{status_text}]", (20 + w1, 22), font, 0.45, box_color, 1)

    return canvas

def compare_english_crops_with_translated_pdf(eng_crop_dir, trans_pdf_path, output_dir, threshold=SIMILARITY_THRESHOLD, dpi=DPI):
    """
    Cross-page comparison: searches for every English cropped image across candidate pages
    in the translated PDF and saves visual match images into Output_Cropped_Comparison/<translated_name>/
    """
    trans_name = os.path.splitext(os.path.basename(trans_pdf_path))[0]
    print(f"\n==========================================================================================")
    print(f"Comparing English Pure Graphic Crops vs Translated PDF: {trans_name}")
    print(f"==========================================================================================")

    trans_bgr_imgs, trans_gray_imgs = render_translated_pdf_pages_pure_graphics(trans_pdf_path, dpi=dpi)
    total_trans_pages = len(trans_gray_imgs)

    diff_out_dir = os.path.join(output_dir, trans_name)
    os.makedirs(diff_out_dir, exist_ok=True)

    total_crops_checked = 0
    total_matches_found = 0
    crop_details = []

    for folder_path, eng_page_num, topic_name, crop_file in _iter_english_crops(eng_crop_dir):
        page_out_dir = os.path.join(diff_out_dir, f"page_{eng_page_num:03d}")
        crop_path = os.path.join(folder_path, crop_file)
        crop_img = cv2.imread(crop_path, cv2.IMREAD_GRAYSCALE)

        if crop_img is None or crop_img.shape[0] < 8 or crop_img.shape[1] < 8:
            continue

        total_crops_checked += 1

        # Priority 1: Check Same Page first (eng_page_num - 1)
        same_p_idx = eng_page_num - 1
        best_score = 0.0
        best_page = -1
        best_loc = (0, 0)

        if 0 <= same_p_idx < total_trans_pages:
            target_gray = trans_gray_imgs[same_p_idx]
            if crop_img.shape[0] <= target_gray.shape[0] and crop_img.shape[1] <= target_gray.shape[1]:
                res = cv2.matchTemplate(target_gray, crop_img, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                best_score = max_val
                best_page = eng_page_num
                best_loc = max_loc

        # Priority 2: Only if same-page score is below threshold, search adjacent pages sorted by distance
        if best_score * 100 < threshold:
            search_start = max(0, eng_page_num - 4)
            search_end = min(total_trans_pages, eng_page_num + 3)
            all_search_pages = list(range(search_start, search_end))
            all_search_pages.sort(key=lambda p_idx: abs(p_idx - same_p_idx))

            for search_p_idx in all_search_pages:
                if search_p_idx == same_p_idx:
                    continue
                target_gray = trans_gray_imgs[search_p_idx]
                if crop_img.shape[0] > target_gray.shape[0] or crop_img.shape[1] > target_gray.shape[1]:
                    continue

                res = cv2.matchTemplate(target_gray, crop_img, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)

                if max_val > best_score:
                    best_score = max_val
                    best_page = search_p_idx + 1
                    best_loc = max_loc

        match_pct = best_score * 100
        is_match = match_pct >= threshold

        if is_match:
            total_matches_found += 1

        status = "MATCH (PASS)" if is_match else "CHECK"
        shift_info = "Same Page" if best_page == eng_page_num else f"Moved to Page {best_page}"
        if best_page == -1:
            shift_info = "Not Found"

        # Save visual match diff image
        match_save_path = ""
        if best_page != -1:
            target_bgr = trans_bgr_imgs[best_page - 1]
            match_img = create_crop_match_image(crop_img, target_bgr, best_loc, best_score, eng_page_num, best_page)
            os.makedirs(page_out_dir, exist_ok=True)
            match_filename = f"match_{crop_file}"
            match_save_path = os.path.join(page_out_dir, match_filename)
            cv2.imwrite(match_save_path, match_img)

        crop_details.append({
            "eng_page": eng_page_num,
            "topic": topic_name,
            "crop_file": crop_file,
            "trans_page": best_page,
            "shift_info": shift_info,
            "match_pct": match_pct,
            "status": status,
            "match_img": match_save_path,
        })

        print(f"  Eng Pg {eng_page_num:2d} | {crop_file:24s} -> Trans Pg {best_page:2d} ({shift_info:16s}) | Match: {match_pct:6.2f}% | {status}")

    overall_pct = (total_matches_found / total_crops_checked * 100) if total_crops_checked > 0 else 0
    overall_status = "PASS" if overall_pct >= threshold else "CHECK"

    print(f"\nResult for {trans_name}: {total_matches_found} / {total_crops_checked} Pure Graphic Crops Matched ({overall_pct:.2f}%) [{overall_status}]\n")

    return {
        "trans_name": trans_name,
        "total_crops": total_crops_checked,
        "matched_crops": total_matches_found,
        "match_pct": overall_pct,
        "overall_status": overall_status,
        "crop_details": crop_details
    }

def process_all_cropped_comparisons(eng_crop_dir=ENGLISH_CROPS_DIR, trans_dir=TRANSLATED_PDF_DIR, output_dir=OUTPUT_REPORT_DIR, threshold=SIMILARITY_THRESHOLD, dpi=DPI):
    """
    Process image matching for all translated PDFs against English cropped images.
    """
    if not os.path.exists(eng_crop_dir):
        print(f"Error: English crops directory not found: {eng_crop_dir}")
        print("Please run crop_pdf_images.py first to generate English cropped images.")
        return

    trans_files = []
    if os.path.isfile(trans_dir):
        trans_files.append(trans_dir)
    elif os.path.isdir(trans_dir):
        for root, _, files in os.walk(trans_dir):
            for file in files:
                if file.lower().endswith(".pdf"):
                    trans_files.append(os.path.join(root, file))

    if not trans_files:
        print(f"No translated PDFs found in: {trans_dir}")
        return

    print(f"English Source Crops: {eng_crop_dir}")
    print(f"Found {len(trans_files)} translated PDF(s) to compare against English cropped images.\n")

    summary_reports = []
    for trans_pdf_path in trans_files:
        res = compare_english_crops_with_translated_pdf(eng_crop_dir, trans_pdf_path, output_dir, threshold=threshold, dpi=dpi)
        summary_reports.append(res)

    print("==========================================================================================")
    print("PURE GRAPHIC CROPPED IMAGE COMPARISON SUMMARY REPORT")
    print("==========================================================================================")
    print(f"{'Translated PDF Name':48s} | {'Matched / Total Crops':22s} | {'Pure Graphic Match %':18s} | Status")
    print("-" * 102)
    for r in summary_reports:
        counts = f"{r['matched_crops']} / {r['total_crops']} images"
        print(f"{r['trans_name']:48s} | {counts:22s} | {r['match_pct']:17.2f}% | {r['overall_status']}")
    print("==========================================================================================")

def main():
    parser = argparse.ArgumentParser(description="Compare English cropped images against Translated PDFs across pages (handling layout shifts).")
    parser.add_argument("--crops", default=ENGLISH_CROPS_DIR, help=f"Path to English cropped images folder (default: {ENGLISH_CROPS_DIR})")
    parser.add_argument("--translated", default=TRANSLATED_PDF_DIR, help=f"Path to translated PDF file or directory (default: {TRANSLATED_PDF_DIR})")
    parser.add_argument("--output", default=OUTPUT_REPORT_DIR, help=f"Output directory for comparison reports (default: {OUTPUT_REPORT_DIR})")
    parser.add_argument("--threshold", type=float, default=SIMILARITY_THRESHOLD, help=f"Similarity pass threshold % (default: {SIMILARITY_THRESHOLD})")
    parser.add_argument("--dpi", type=int, default=DPI, help=f"DPI resolution (default: {DPI})")

    args = parser.parse_args()
    process_all_cropped_comparisons(eng_crop_dir=args.crops, trans_dir=args.translated, output_dir=args.output, threshold=args.threshold, dpi=args.dpi)

if __name__ == "__main__":
    main()
