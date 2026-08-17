import os
import sys
import argparse
import fitz  # PyMuPDF
import pdfplumber

# ==============================================================================
# CONFIGURATION - EDIT YOUR INPUT AND OUTPUT PATHS HERE DIRECTLY
# ==============================================================================
INPUT_PATH = r"Input"                 # Path to a single PDF file OR a directory containing PDFs
OUTPUT_DIR = r"Output_Cropped_Images"    # Directory where cropped element images will be saved
DPI = 150                             # Image resolution DPI (e.g. 150, 300)
HEADER_MARGIN = 50                    # Ignore elements in top header margin (in points)
FOOTER_MARGIN = 40                    # Ignore elements in bottom footer margin (in points)
MASK_TEXT_IN_CROPS = True             # Mask text characters inside graphic crops to compare pure visual graphics
# ==============================================================================

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

def get_all_image_candidates(fitz_page, header_margin=HEADER_MARGIN, footer_margin=FOOTER_MARGIN):
    """
    Get bounding boxes of all raster images, logos, and vector drawing icons/diagrams/figures.
    """
    page_rect = fitz_page.rect
    raw_images = []

    # 1. Raster images & logos
    try:
        for img in fitz_page.get_image_info():
            if 'bbox' in img:
                r = fitz.Rect(img['bbox'])
                if r.y1 > header_margin and r.y0 < (page_rect.height - footer_margin):
                    raw_images.append(r)
    except Exception:
        pass

    # 2. Image text blocks
    try:
        text_dict = fitz_page.get_text("dict")
        for b in text_dict.get("blocks", []):
            if b.get("type") == 1:
                r = fitz.Rect(b["bbox"])
                if r.y1 > header_margin and r.y0 < (page_rect.height - footer_margin):
                    raw_images.append(r)
    except Exception:
        pass

    # 3. Vector drawings, schematics, hazard icons, figures, & lines
    try:
        drawings = fitz_page.get_drawings()
        valid = []
        for d in drawings:
            r = fitz.Rect(d['rect'])
            if r.y1 <= header_margin or r.y0 >= (page_rect.height - footer_margin):
                continue
            if r.width > (page_rect.width * 0.85) and r.height < 6:
                continue
            if r.height > (page_rect.height * 0.85) and r.width < 6:
                continue
            if r.width > 3 and r.height > 3:
                valid.append(r)

        if valid:
            clusters = []
            for r in valid:
                added = False
                for c in clusters:
                    if c.intersects(r) or (c + (-5, -5, 5, 5)).intersects(r):
                        c.include_rect(r)
                        added = True
                        break
                if not added:
                    clusters.append(fitz.Rect(r))
            for c in clusters:
                if c.width > 8 and c.height > 8:
                    raw_images.append(c)
    except Exception:
        pass

    # Merge overlapping image bounding boxes
    merged_images = []
    for r in raw_images:
        added = False
        for m in merged_images:
            if m.intersects(r) or (m + (-3, -3, 3, 3)).intersects(r):
                m.include_rect(r)
                added = True
                break
        if not added:
            merged_images.append(fitz.Rect(r))

    return merged_images

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

import fitz  # PyMuPDF
import cv2
import numpy as np

try:
    from pyzbar.pyzbar import decode as pyzbar_decode
    HAS_PYZBAR = True
except ImportError:
    HAS_PYZBAR = False


def detect_barcodes_and_qr_codes(page, dpi=150):
    """
    Detect all Barcodes and QR Codes on a PyMuPDF page using pyzbar with OpenCV fallbacks.
    Returns list of dicts with bounding boxes (pymupdf.Rect) in PDF point coordinates.
    """
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

    detected = []
    scale = 72.0 / float(dpi)

    # 1. Primary detection via pyzbar
    if HAS_PYZBAR:
        try:
            objs = pyzbar_decode(img_gray)
            for obj in objs:
                r = obj.rect
                if r.width < 5 or r.height < 5:
                    continue  # Ignore zero-size or tiny noise false positives

                x0 = r.left * scale
                y0 = r.top * scale
                x1 = (r.left + r.width) * scale
                y1 = (r.top + r.height) * scale

                pad = 4
                pdf_rect = fitz.Rect(
                    max(0, x0 - pad),
                    max(0, y0 - pad),
                    min(page.rect.width, x1 + pad),
                    min(page.rect.height, y1 + pad)
                )

                b_data = obj.data.decode('utf-8', errors='ignore') if isinstance(obj.data, bytes) else str(obj.data)
                code_kind = "QRCODE" if str(obj.type).upper() == "QRCODE" else "BARCODE"

                detected.append({
                    "type": code_kind,
                    "raw_type": str(obj.type),
                    "data": b_data,
                    "rect": pdf_rect,
                    "page_num": page.number + 1
                })
        except Exception:
            pass

    # 2. Fallback via OpenCV QRCodeDetector
    try:
        qr_detector = cv2.QRCodeDetector()
        res_qr, points, _ = qr_detector.detectAndDecode(img_bgr)
        if (res_qr or points is not None) and len(points) > 0:
            pts = points[0] if len(points.shape) == 3 else points
            x0 = float(np.min(pts[:, 0])) * scale
            y0 = float(np.min(pts[:, 1])) * scale
            x1 = float(np.max(pts[:, 0])) * scale
            y1 = float(np.max(pts[:, 1])) * scale
            pdf_rect = fitz.Rect(max(0, x0 - 4), max(0, y0 - 4), min(page.rect.width, x1 + 4), min(page.rect.height, y1 + 4))

            already_found = any(d["rect"].intersects(pdf_rect) for d in detected if d["type"] == "QRCODE")
            if not already_found:
                detected.append({
                    "type": "QRCODE",
                    "raw_type": "QRCODE",
                    "data": res_qr if res_qr else "QR Code Present",
                    "rect": pdf_rect,
                    "page_num": page.number + 1
                })
    except Exception:
        pass

    # 3. Fallback via OpenCV BarcodeDetector
    try:
        bc_detector = cv2.barcode.BarcodeDetector()
        res_bc, _, points = bc_detector.detectAndDecode(img_bgr)
        if points is not None and len(points) > 0:
            for i, pts in enumerate(points):
                x0 = float(np.min(pts[:, 0])) * scale
                y0 = float(np.min(pts[:, 1])) * scale
                x1 = float(np.max(pts[:, 0])) * scale
                y1 = float(np.max(pts[:, 1])) * scale
                pdf_rect = fitz.Rect(max(0, x0 - 4), max(0, y0 - 4), min(page.rect.width, x1 + 4), min(page.rect.height, y1 + 4))

                already_found = any(d["rect"].intersects(pdf_rect) for d in detected if d["type"] == "BARCODE")
                if not already_found:
                    data_str = res_bc[i] if (isinstance(res_bc, (list, tuple)) and i < len(res_bc) and res_bc[i]) else "Barcode Present"
                    detected.append({
                        "type": "BARCODE",
                        "raw_type": "BARCODE",
                        "data": data_str,
                        "rect": pdf_rect,
                        "page_num": page.number + 1
                    })
    except Exception:
        pass

    return detected

def crop_pdf_elements(pdf_path, output_dir, dpi=DPI, header_margin=HEADER_MARGIN, footer_margin=FOOTER_MARGIN, mask_text_in_crops=MASK_TEXT_IN_CROPS):
    """
    Extract and crop pure inside images from PDF pages:
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

    try:
        plumb_doc = pdfplumber.open(pdf_path)
    except Exception:
        plumb_doc = None

    total_crops = 0

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_rect = page.rect
        page_num = page_idx + 1

        page_out_dir = os.path.join(output_dir, pdf_name, f"page_{page_num:03d}")

        # Get table bounding boxes
        plumb_page = plumb_doc.pages[page_idx] if plumb_doc and page_idx < len(plumb_doc.pages) else None
        table_rects = get_table_bboxes(page, plumb_page)

        # Detect Barcodes and QR Codes on current page to exclude them from cropping
        detected_codes = detect_barcodes_and_qr_codes(page, dpi=dpi)
        code_rects = [c["rect"] for c in detected_codes]

        # Get all image/icon/diagram candidates
        image_rects = get_all_image_candidates(page, header_margin=header_margin, footer_margin=footer_margin)

        crops_on_page = []
        for img_rect in image_rects:
            if img_rect.y1 <= header_margin or img_rect.y0 >= (page_rect.height - footer_margin):
                continue

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

                    os.makedirs(page_out_dir, exist_ok=True)
                    crop_filename = f"crop_{crop_idx:02d}_{label}.png"
                    crop_path = os.path.join(page_out_dir, crop_filename)
                    crop_pix.save(crop_path)
                    total_crops += 1
                    crops_saved_count += 1

            if crops_saved_count > 0:
                print(f"  Page {page_num:3d}: Saved {crops_saved_count:2d} pure graphic crop(s) -> {page_out_dir}")

    if plumb_doc:
        plumb_doc.close()
    doc.close()

    print(f"Saved total of {total_crops} cropped pure graphic files to: {os.path.join(output_dir, pdf_name)}\n")
    return total_crops

def process_input_dir(input_dir, output_dir, dpi=DPI, header_margin=HEADER_MARGIN, footer_margin=FOOTER_MARGIN):
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
        total += crop_pdf_elements(pdf_path, output_dir, dpi=dpi, header_margin=header_margin, footer_margin=footer_margin)
    print(f"Finished cropping graphic files. Total crops saved: {total}")

def main():
    parser = argparse.ArgumentParser(description="Crop inside pure graphic images from PDF pages (masking text inside crops).")
    parser.add_argument("--input", default=INPUT_PATH, help=f"Input PDF file or directory (default: {INPUT_PATH})")
    parser.add_argument("--output", default=OUTPUT_DIR, help=f"Output directory (default: {OUTPUT_DIR})")
    parser.add_argument("--dpi", type=int, default=DPI, help=f"DPI resolution (default: {DPI})")
    parser.add_argument("--header", type=int, default=HEADER_MARGIN, help=f"Header margin in pt (default: {HEADER_MARGIN})")
    parser.add_argument("--footer", type=int, default=FOOTER_MARGIN, help=f"Footer margin in pt (default: {FOOTER_MARGIN})")

    args = parser.parse_args()

    if os.path.isfile(args.input):
        crop_pdf_elements(args.input, args.output, dpi=args.dpi, header_margin=args.header, footer_margin=args.footer)
    elif os.path.isdir(args.input):
        process_input_dir(args.input, args.output, dpi=args.dpi, header_margin=args.header, footer_margin=args.footer)
    else:
        print(f"Error: Input path does not exist: {args.input}")

if __name__ == "__main__":
    main()
