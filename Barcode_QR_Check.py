"""
Barcode_QR_Check.py

Dedicated Module for Barcode and QR Code Detection & Count Matching.

Avoids visual CV / SSIM image comparison (since barcode data and QR URLs naturally differ
between document versions and languages). Instead, decodes codes via pyzbar (with OpenCV fallback)
and verifies:
  1. Presence of Barcode & QR Code
  2. Total Count matching between Master English PDF and Target Translated PDF
  3. Per-page breakdown of detected codes
"""

import os
import sys
import argparse
import numpy as np
import cv2
import pymupdf  # PyMuPDF

try:
    from pyzbar.pyzbar import decode as pyzbar_decode
    HAS_PYZBAR = True
except ImportError:
    HAS_PYZBAR = False


# -----------------------------------------------------------
# Page-level Barcode & QR Code Detector
# -----------------------------------------------------------

def detect_barcodes_and_qr_codes(page, dpi=200):
    """
    Detect all Barcodes and QR Codes on a PyMuPDF page using pyzbar with OpenCV fallbacks.

    Returns list of dicts:
      [
        {
          "type": "QRCODE" | "BARCODE",
          "raw_type": "QRCODE" | "CODE128" | ...,
          "data": "decoded_string",
          "rect": pymupdf.Rect(x0, y0, x1, y1),  # in PDF point coordinates
          "page_num": page.number + 1
        }, ...
      ]
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
                pdf_rect = pymupdf.Rect(
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
        if res_qr and bool(str(res_qr).strip()) and points is not None and len(points) > 0:
            pts = points[0] if len(points.shape) == 3 else points
            x0 = float(np.min(pts[:, 0])) * scale
            y0 = float(np.min(pts[:, 1])) * scale
            x1 = float(np.max(pts[:, 0])) * scale
            y1 = float(np.max(pts[:, 1])) * scale
            pdf_rect = pymupdf.Rect(max(0, x0 - 4), max(0, y0 - 4), min(page.rect.width, x1 + 4), min(page.rect.height, y1 + 4))

            already_found = any(d["rect"].intersects(pdf_rect) for d in detected if d["type"] == "QRCODE")
            if not already_found:
                detected.append({
                    "type": "QRCODE",
                    "raw_type": "QRCODE",
                    "data": str(res_qr).strip(),
                    "rect": pdf_rect,
                    "page_num": page.number + 1
                })
    except Exception:
        pass

    # 3. Fallback via OpenCV BarcodeDetector
    try:
        bc_detector = cv2.barcode.BarcodeDetector()
        res_bc, _, points = bc_detector.detectAndDecode(img_bgr)
        if res_bc and points is not None and len(points) > 0:
            for i, pts in enumerate(points):
                data_str = res_bc[i] if (isinstance(res_bc, (list, tuple)) and i < len(res_bc) and res_bc[i]) else ""
                if not data_str or not bool(str(data_str).strip()):
                    continue
                x0 = float(np.min(pts[:, 0])) * scale
                y0 = float(np.min(pts[:, 1])) * scale
                x1 = float(np.max(pts[:, 0])) * scale
                y1 = float(np.max(pts[:, 1])) * scale
                pdf_rect = pymupdf.Rect(max(0, x0 - 4), max(0, y0 - 4), min(page.rect.width, x1 + 4), min(page.rect.height, y1 + 4))

                already_found = any(d["rect"].intersects(pdf_rect) for d in detected if d["type"] == "BARCODE")
                if not already_found:
                    detected.append({
                        "type": "BARCODE",
                        "raw_type": "BARCODE",
                        "data": str(data_str).strip(),
                        "rect": pdf_rect,
                        "page_num": page.number + 1
                    })
    except Exception:
        pass

    return detected


# -----------------------------------------------------------
# Document-level Barcode & QR Code Count Extractor
# -----------------------------------------------------------

def extract_barcode_qr_model(pdf_path, dpi=200):
    """
    Extract multi-page Barcode and QR Code model from a PDF file.

    Returns dict containing counts, breakdown, and presence indicators.
    """
    doc = pymupdf.open(pdf_path)
    all_codes = []
    barcode_count = 0
    qr_count = 0
    pages_with_barcode = set()
    pages_with_qr = set()

    try:
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            codes = detect_barcodes_and_qr_codes(page, dpi=dpi)
            for c in codes:
                all_codes.append(c)
                if c["type"] == "BARCODE":
                    barcode_count += 1
                    pages_with_barcode.add(c["page_num"])
                elif c["type"] == "QRCODE":
                    qr_count += 1
                    pages_with_qr.add(c["page_num"])
    finally:
        doc.close()

    return {
        "filename": os.path.basename(pdf_path),
        "pdf_path": pdf_path,
        "total_barcodes": barcode_count,
        "total_qr_codes": qr_count,
        "has_barcode": barcode_count > 0,
        "has_qr": qr_count > 0,
        "pages_with_barcode": sorted(list(pages_with_barcode)),
        "pages_with_qr": sorted(list(pages_with_qr)),
        "all_codes": all_codes,
    }


# -----------------------------------------------------------
# Comparison Logic: Count & Presence Verification
# -----------------------------------------------------------

def compare_barcode_qr_models(source_model, target_model):
    """
    Compare Barcode and QR Code counts and presence between Master English PDF and Translated PDF.
    Does NOT use visual image comparison (CV / SSIM).
    """
    src_bc_count = source_model["total_barcodes"]
    tgt_bc_count = target_model["total_barcodes"]

    src_qr_count = source_model["total_qr_codes"]
    tgt_qr_count = target_model["total_qr_codes"]

    src_pages_bc = source_model.get("pages_with_barcode", [])
    tgt_pages_bc = target_model.get("pages_with_barcode", [])

    src_pages_qr = source_model.get("pages_with_qr", [])
    tgt_pages_qr = target_model.get("pages_with_qr", [])

    # Presence & Count Match Rules
    bc_pass = (tgt_bc_count == src_bc_count)
    qr_pass = (tgt_qr_count == src_qr_count)

    overall_pass = bc_pass and qr_pass

    bc_status = f"PASS (Count {tgt_bc_count}/{src_bc_count})" if bc_pass else f"FAIL (Count {tgt_bc_count}/{src_bc_count})"
    qr_status = f"PASS (Count {tgt_qr_count}/{src_qr_count})" if qr_pass else f"FAIL (Count {tgt_qr_count}/{src_qr_count})"

    return {
        "english_pdf": source_model["filename"],
        "translated_pdf": target_model["filename"],
        "master_barcode_count": src_bc_count,
        "target_barcode_count": tgt_bc_count,
        "master_pages_barcode": src_pages_bc,
        "target_pages_barcode": tgt_pages_bc,
        "barcode_status": bc_status,
        "barcode_pass": bc_pass,
        "master_qr_count": src_qr_count,
        "target_qr_count": tgt_qr_count,
        "master_pages_qr": src_pages_qr,
        "target_pages_qr": tgt_pages_qr,
        "qr_status": qr_status,
        "qr_pass": qr_pass,
        "overall_verdict": "PASS" if overall_pass else "FAIL",
    }


def compare_barcode_qr(source_pdf_path, target_pdf_path, dpi=200):
    """
    Convenience wrapper to extract models and compare Barcode & QR Code counts between two PDFs.
    """
    src_model = extract_barcode_qr_model(source_pdf_path, dpi=dpi)
    tgt_model = extract_barcode_qr_model(target_pdf_path, dpi=dpi)
    return compare_barcode_qr_models(src_model, tgt_model)


# -----------------------------------------------------------
# Command Line Runner
# -----------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Standalone Barcode and QR Code Presence & Count Verification.")
    parser.add_argument("--master", required=True, help="Path to Master English PDF file")
    parser.add_argument("--target", required=True, help="Path to Target Translated PDF file OR directory")
    parser.add_argument("--dpi", type=int, default=200, help="Image resolution DPI (default: 200)")

    args = parser.parse_args()

    if not os.path.isfile(args.master):
        print(f"Error: Master PDF path does not exist: {args.master}")
        sys.exit(1)

    target_files = []
    if os.path.isfile(args.target):
        target_files.append(args.target)
    elif os.path.isdir(args.target):
        for root, _, files in os.walk(args.target):
            for file in files:
                if file.lower().endswith(".pdf"):
                    target_files.append(os.path.join(root, file))
    else:
        print(f"Error: Target path does not exist: {args.target}")
        sys.exit(1)

    print("===========================================================================")
    print("BARCODE & QR CODE COUNT & PAGE VERIFICATION REPORT")
    print("===========================================================================")
    print(f"Master English PDF: {os.path.basename(args.master)}")

    src_model = extract_barcode_qr_model(args.master, dpi=args.dpi)
    print(f"Master Barcodes   : {src_model['total_barcodes']} (Pages: {src_model['pages_with_barcode']})")
    print(f"Master QR Codes   : {src_model['total_qr_codes']} (Pages: {src_model['pages_with_qr']})")
    print("---------------------------------------------------------------------------")

    pass_count = 0
    for idx, tgt_path in enumerate(target_files, start=1):
        res = compare_barcode_qr(args.master, tgt_path, dpi=args.dpi)
        status = res["overall_verdict"]
        if status == "PASS":
            pass_count += 1
        print(f"[{idx:02d}/{len(target_files):02d}] {res['translated_pdf']:<35} | BC: {res['barcode_status']:<18} (Pages: {res['target_pages_barcode']}) | QR: {res['qr_status']:<18} (Pages: {res['target_pages_qr']}) | Verdict: {status}")

    print("===========================================================================")
    print(f"Summary: {pass_count} / {len(target_files)} Translated PDF(s) PASSED Barcode & QR Code Count Matching.")
    print("===========================================================================")


if __name__ == "__main__":
    main()
