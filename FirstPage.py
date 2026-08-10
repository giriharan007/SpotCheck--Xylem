"""
FirstPage.py

Extract and Compare Page 1 Objects between Master English PDF and Translated PDFs.

Checks:
  1. Page Size (Width, Height) - Must be equal
  2. Version (e.g. 5.0) - Must be equal
  3. Document Number Length (e.g. 894387 -> length 6) - Must be equal length
  4. Language Code (e.g. EN, DE, FR, ES, IT) - Verified presence on translated page
  5. Barcode & QR Code Presence Check - Presence check only
"""

import os
import re
import cv2
import numpy as np
import pymupdf


# -----------------------------------------------------------
# Supported ISO 639-1 Language Codes
# -----------------------------------------------------------

LANGUAGE_CODES = {
    "EN", "DE", "FR", "ES", "IT", "NL", "PT", "FI",
    "SV", "NO", "DA", "EL", "JA", "KO", "ZH", "PL",
    "RU", "TR", "CS", "HU", "RO", "BG", "HR", "SK",
    "SL", "ET", "LV", "LT", "UK", "AR", "HE", "TH",
    "VI", "ID", "MS", "HI", "BN", "TA", "TE", "MR",
    "GU", "KN", "ML", "PA", "UR",
}

# Version & Document Number pattern: e.g. "894387_5.0"
VERSION_PATTERN = re.compile(r"(\d+)_(\d+\.\d+)")


# -----------------------------------------------------------
# Page Object Model Extraction
# -----------------------------------------------------------

def extract_page_model(pdf_path):
    """
    Extract minimal Page Object Model from page 1 of a PDF.

    Returns dict containing:
      - filename: str
      - page_size: (width, height)
      - document_number: str (e.g. '894387')
      - document_length: int (e.g. 6)
      - version: str (e.g. '5.0')
      - language: str (e.g. 'EN', 'DE', 'FR')
      - has_barcode: bool
      - has_qr: bool
    """
    doc = pymupdf.open(pdf_path)
    page = doc[0]

    # 1. Page Size
    width = round(page.rect.width, 2)
    height = round(page.rect.height, 2)

    # 2. Text Analysis: Document Number, Version, Language Code
    text = page.get_text("text")
    words = [w.strip().upper() for w in text.split()]

    doc_num = None
    doc_len = None
    version = None
    language = None

    match = VERSION_PATTERN.search(text)
    if match:
        doc_num = match.group(1)
        doc_len = len(doc_num)
        version = match.group(2)

    for w in words:
        if w in LANGUAGE_CODES:
            language = w
            break

    # 3. Barcode & QR Code Detection
    pix = page.get_pixmap(dpi=150)
    data = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    img_bgr = cv2.cvtColor(data, cv2.COLOR_RGBA2BGR if pix.n == 4 else cv2.COLOR_RGB2BGR)

    has_qr = False
    qr_data = "Missing"
    try:
        qr_detector = cv2.QRCodeDetector()
        res_qr = qr_detector.detectAndDecode(img_bgr)
        if res_qr[0] or (res_qr[1] is not None and len(res_qr[1]) > 0):
            has_qr = True
            qr_data = res_qr[0] if res_qr[0] else "QR Code Present"
    except Exception:
        pass

    has_barcode = False
    barcode_data = "Missing"
    try:
        bc_detector = cv2.barcode.BarcodeDetector()
        res_bc = bc_detector.detectAndDecode(img_bgr)
        if res_bc[0] or (res_bc[1] is not None and len(res_bc[1]) > 0):
            has_barcode = True
            barcode_data = res_bc[0] if res_bc[0] else "Barcode Present"
    except Exception:
        pass

    # Fallback for Barcode presence: check page embedded images if barcode detector is unavailable
    if not has_barcode and len(page.get_images()) > 0:
        has_barcode = True
        barcode_data = "Barcode Present"

    doc.close()

    return {
        "filename": os.path.basename(pdf_path),
        "pdf_path": pdf_path,
        "page_size": (width, height),
        "document_number": doc_num,
        "document_length": doc_len,
        "version": version,
        "language": language,
        "has_barcode": has_barcode,
        "has_qr": has_qr,
        "qr_data": qr_data,
        "barcode_data": barcode_data,
    }


# -----------------------------------------------------------
# Comparison Logic
# -----------------------------------------------------------

def compare_page_models(source_model, target_model):
    """
    Compare a translated PDF page 1 against source English PDF page 1.
    """
    # 1. Page Size Check
    src_size = source_model["page_size"]
    tgt_size = target_model["page_size"]
    size_pass = src_size == tgt_size

    # 2. Version Check
    src_ver = source_model["version"]
    tgt_ver = target_model["version"]
    ver_pass = (src_ver is not None) and (src_ver == tgt_ver)

    # 3. Document Length Check
    src_len = source_model["document_length"]
    tgt_len = target_model["document_length"]
    doc_len_pass = (src_len is not None) and (src_len == tgt_len)

    # 4. Language Code Check
    tgt_lang = target_model["language"]
    lang_pass = tgt_lang is not None

    # 5. Barcode Check
    bc_pass = source_model["has_barcode"] and target_model["has_barcode"]

    # 6. QR Code Check
    qr_pass = source_model["has_qr"] and target_model["has_qr"]

    # Overall Verdict
    overall_pass = (
        size_pass and ver_pass and doc_len_pass and lang_pass and bc_pass and qr_pass
    )

    return {
        "english_pdf": source_model["filename"],
        "translated_pdf": target_model["filename"],
        "language": tgt_lang if tgt_lang else "MISSING",
        "page_size_val": f"{tgt_size[0]} x {tgt_size[1]}",
        "page_size_status": "PASS" if size_pass else "FAIL",
        "version_val": tgt_ver if tgt_ver else "N/A",
        "version_status": "PASS" if ver_pass else "FAIL",
        "doc_len_val": tgt_len if tgt_len is not None else "N/A",
        "doc_len_status": "PASS" if doc_len_pass else "FAIL",
        "language_status": "PASS" if lang_pass else "FAIL",
        "barcode_status": "PASS" if bc_pass else "FAIL",
        "qr_status": "PASS" if qr_pass else "FAIL",
        "overall_verdict": "PASS" if overall_pass else "FAIL",
    }