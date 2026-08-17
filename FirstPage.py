"""
FirstPage.py

Extract and Compare Page 1 Objects between Master English PDF and Translated PDFs.

Checks:
  1. Page Size (Width, Height) - Must be equal
  2. Version (e.g. 5.0) - Must be equal
  3. Document Number Length (e.g. 894387 -> length 6) - Must be equal length
  4. Language Code (e.g. EN, DE, FR, ES, IT) - Verified presence on translated page
  5. Barcode & QR Code Presence Check - Presence check only
  6. Title Presence Check - Highest text size on page 1
  7. Manual Type Check - Identified from master list in English PDF, positional check in translated PDF
"""

import os
import re
import cv2
import numpy as np
import pymupdf

try:
    import Barcode_QR_Check
except ImportError:
    import SpotCheck.Barcode_QR_Check as Barcode_QR_Check


# -----------------------------------------------------------
# Supported ISO 639-1 Language Codes
# -----------------------------------------------------------

LANGUAGE_CODES = {
    "EN", "EG", "BG", "HR","CZ", "DK","NL", "EE","FI", 
    "CA","FR", "DE","GR", "IL","HU", "IS","IE", "IT",
    "KR", "LV","LT", "MT","NO", "PL","BR", "PT","RO",
    "RU","SP", "CN","SK", "SI","LA", "ES","SE", "TR","UA",
    "EL","DA","SV", "NL",
}

# Predefined List of Manual Types
MANUAL_TYPES = [
    "Installation, Operation, and Maintenance",
    "Datasheet/Cutsheet",
    "Quick Start Guide",
    "User Interface",
    "Product specification",
    "Pump curve",
    "Global Product List",
    "Product Sustainability Report",
    "Technical Specification",
    "Basic Repair Kit/Spares Leaflet",
    "Checklist",
    "General Safety Information",
    "Software strings",
    "Installation guide",
    "Mounting Instructions",
    "Service and Repair manual",
    "User Guide",
    "Acrolinx",
    "Others",
]

# Version & Document Number pattern: e.g. "894387_5.0"
VERSION_PATTERN = re.compile(r"(\d+)_(\d+\.\d+)")


# -----------------------------------------------------------
# Page Object Model Extraction
# -----------------------------------------------------------

def extract_page_model(pdf_path, ref_manual_bbox=None, ref_manual_type=None, padding=15):
    """
    Extract minimal Page Object Model from page 1 of a PDF.

    Parameters:
      pdf_path: str - Path to PDF file
      ref_manual_bbox: tuple (x0, y0, x1, y1) - Bounding box from English Master PDF
      ref_manual_type: str - Identified manual type from English Master PDF
      padding: float - Tolerance in points for extracting text at ref_manual_bbox position

    Returns dict containing page 1 properties, extracted Title, and Manual Type information.
    """
    doc = pymupdf.open(pdf_path)
    page = doc[0]

    # 1. Page Size
    width = round(page.rect.width, 2)
    height = round(page.rect.height, 2)

    # 2. Text Spans Analysis: Font sizes, Title, Document Number, Version, Language Code
    text_dict = page.get_text("dict")
    spans = []
    for block in text_dict.get("blocks", []):
        if "lines" in block:
            for line in block["lines"]:
                for span in line["spans"]:
                    txt = span["text"].strip()
                    if txt:
                        spans.append({
                            "size": span["size"],
                            "text": txt,
                            "bbox": span["bbox"],
                            "flags": span.get("flags", 0)
                        })

    text = page.get_text("text")

    doc_num = None
    doc_len = None
    version = None
    language = None

    match = VERSION_PATTERN.search(text)
    if match:
        doc_num = match.group(1)
        doc_len = len(doc_num)
        version = match.group(2)

    # Detect Language Code: check standalone uppercase spans first to avoid prose words (e.g. Dutch 'en', Spanish 'de')
    for s in spans:
        stxt = s["text"].strip()
        if stxt in LANGUAGE_CODES and stxt.isupper():
            language = stxt
            break

    # Fallback to uppercase standalone words if span match not found
    if not language:
        words = text.split()
        for w in words:
            w_clean = w.strip()
            if w_clean in LANGUAGE_CODES and w_clean.isupper():
                language = w_clean
                break

    # -------------------------------------------------------
    # Title Extraction: Text with highest font size on page 1
    # -------------------------------------------------------
    title = ""
    max_font_size = 0.0
    if spans:
        max_font_size = max(s["size"] for s in spans)
        # Gather all spans with maximum font size (within 0.1pt tolerance)
        title_spans = [s for s in spans if abs(s["size"] - max_font_size) < 0.1]
        # Sort by vertical position (y0), then horizontal position (x0)
        title_spans.sort(key=lambda s: (s["bbox"][1], s["bbox"][0]))
        title = " ".join(s["text"] for s in title_spans).strip()

    has_title = len(title) > 0

    # -------------------------------------------------------
    # Manual Type Detection & Positional Verification
    # -------------------------------------------------------
    manual_type = "Others"
    manual_type_bbox = None
    has_manual_type = False
    translated_manual_text = ""

    # Match English manual types from list
    for mtype in MANUAL_TYPES:
        if mtype == "Others":
            continue
        pattern = re.escape(mtype)
        if re.search(pattern, text, re.IGNORECASE):
            manual_type = mtype
            matching_spans = [s for s in spans if re.search(pattern, s["text"], re.IGNORECASE)]
            if not matching_spans:
                mtype_words = [w for w in mtype.lower().split() if len(w) > 2]
                matching_spans = [s for s in spans if any(w in s["text"].lower() for w in mtype_words)]
            
            if matching_spans:
                x0 = min(s["bbox"][0] for s in matching_spans)
                y0 = min(s["bbox"][1] for s in matching_spans)
                x1 = max(s["bbox"][2] for s in matching_spans)
                y1 = max(s["bbox"][3] for s in matching_spans)
                manual_type_bbox = (x0, y0, x1, y1)
            has_manual_type = True
            break

    # If reference manual_bbox is provided (for Translated PDFs)
    if ref_manual_bbox is not None:
        rx0, ry0, rx1, ry1 = ref_manual_bbox
        padded_rect = pymupdf.Rect(
            max(0, rx0 - padding),
            max(0, ry0 - padding),
            rx1 + padding,
            ry1 + padding
        )
        clip_text = page.get_text("text", clip=padded_rect).strip()
        raw_text = " ".join(clip_text.split())
        clean_text = re.sub(r"\b\d{5,8}[_\s]*\d.*$", "", raw_text).strip()
        translated_manual_text = clean_text if clean_text else raw_text
        has_manual_type = len(translated_manual_text) > 0
        if ref_manual_type:
            manual_type = ref_manual_type

    return {
        "manual_type": manual_type,
        "has_manual_type": has_manual_type,
        "manual_type_bbox": manual_type_bbox,
        "translated_manual_text": translated_manual_text
    }


# -----------------------------------------------------------
# Delegated Barcode & QR Helpers
# -----------------------------------------------------------

def detect_barcodes_and_qr_codes(page, dpi=200):
    """
    Delegate page-level barcode & QR detection to Barcode_QR_Check module.
    """
    return Barcode_QR_Check.detect_barcodes_and_qr_codes(page, dpi=dpi)


def extract_all_pages_barcode_qr(pdf_path, dpi=200):
    """
    Delegate multi-page barcode & QR extraction to Barcode_QR_Check module.
    """
    m = Barcode_QR_Check.extract_barcode_qr_model(pdf_path, dpi=dpi)
    return {
        "has_barcode": m["has_barcode"],
        "has_qr": m["has_qr"],
        "pages_with_barcode": m["pages_with_barcode"],
        "pages_with_qr": m["pages_with_qr"],
        "all_codes": m["all_codes"],
        "total_barcodes": m["total_barcodes"],
        "total_qr_codes": m["total_qr_codes"],
    }


# -----------------------------------------------------------
# Page Object Model Extraction
# -----------------------------------------------------------

def extract_page_model(pdf_path, ref_manual_bbox=None, ref_manual_type=None, padding=15):
    """
    Extract minimal Page Object Model from page 1 of a PDF and multi-page Barcode/QR model across all pages.

    Parameters:
      pdf_path: str - Path to PDF file
      ref_manual_bbox: tuple (x0, y0, x1, y1) - Bounding box from English Master PDF
      ref_manual_type: str - Identified manual type from English Master PDF
      padding: float - Tolerance in points for extracting text at ref_manual_bbox position

    Returns dict containing page 1 properties, extracted Title, and Manual Type & Barcode/QR information.
    """
    doc = pymupdf.open(pdf_path)
    page = doc[0]

    # 1. Page Size
    width = round(page.rect.width, 2)
    height = round(page.rect.height, 2)

    # 2. Text Spans Analysis: Font sizes, Title, Document Number, Version, Language Code
    text_dict = page.get_text("dict")
    spans = []
    for block in text_dict.get("blocks", []):
        if "lines" in block:
            for line in block["lines"]:
                for span in line["spans"]:
                    txt = span["text"].strip()
                    if txt:
                        spans.append({
                            "size": span["size"],
                            "text": txt,
                            "bbox": span["bbox"],
                            "flags": span.get("flags", 0)
                        })

    text = page.get_text("text")

    doc_num = None
    doc_len = None
    version = None
    language = None

    match = VERSION_PATTERN.search(text)
    if match:
        doc_num = match.group(1)
        doc_len = len(doc_num)
        version = match.group(2)

    # Detect Language Code: check standalone uppercase spans first to avoid prose words (e.g. Dutch 'en', Spanish 'de')
    for s in spans:
        stxt = s["text"].strip()
        if stxt in LANGUAGE_CODES and stxt.isupper():
            language = stxt
            break

    # Fallback to uppercase standalone words if span match not found
    if not language:
        words = text.split()
        for w in words:
            w_clean = w.strip()
            if w_clean in LANGUAGE_CODES and w_clean.isupper():
                language = w_clean
                break

    # -------------------------------------------------------
    # Title Extraction: Text with highest font size on page 1
    # -------------------------------------------------------
    title = ""
    max_font_size = 0.0
    if spans:
        max_font_size = max(s["size"] for s in spans)
        # Gather all spans with maximum font size (within 0.1pt tolerance)
        title_spans = [s for s in spans if abs(s["size"] - max_font_size) < 0.1]
        # Sort by vertical position (y0), then horizontal position (x0)
        title_spans.sort(key=lambda s: (s["bbox"][1], s["bbox"][0]))
        title = " ".join(s["text"] for s in title_spans).strip()

    has_title = len(title) > 0

    # -------------------------------------------------------
    # Manual Type Detection & Positional Verification
    # -------------------------------------------------------
    manual_type = "Others"
    manual_type_bbox = None
    has_manual_type = False
    translated_manual_text = ""

    # Match English manual types from list
    for mtype in MANUAL_TYPES:
        if mtype == "Others":
            continue
        pattern = re.escape(mtype)
        if re.search(pattern, text, re.IGNORECASE):
            manual_type = mtype
            matching_spans = [s for s in spans if re.search(pattern, s["text"], re.IGNORECASE)]
            if not matching_spans:
                mtype_words = [w for w in mtype.lower().split() if len(w) > 2]
                matching_spans = [s for s in spans if any(w in s["text"].lower() for w in mtype_words)]
            
            if matching_spans:
                x0 = min(s["bbox"][0] for s in matching_spans)
                y0 = min(s["bbox"][1] for s in matching_spans)
                x1 = max(s["bbox"][2] for s in matching_spans)
                y1 = max(s["bbox"][3] for s in matching_spans)
                manual_type_bbox = (x0, y0, x1, y1)
            has_manual_type = True
            break

    # If reference manual_bbox is provided (for Translated PDFs)
    if ref_manual_bbox is not None:
        rx0, ry0, rx1, ry1 = ref_manual_bbox
        padded_rect = pymupdf.Rect(
            max(0, rx0 - padding),
            max(0, ry0 - padding),
            rx1 + padding,
            ry1 + padding
        )
        clip_text = page.get_text("text", clip=padded_rect).strip()
        raw_text = " ".join(clip_text.split())
        clean_text = re.sub(r"\b\d{5,8}[_\s]*\d.*$", "", raw_text).strip()
        translated_manual_text = clean_text if clean_text else raw_text
        has_manual_type = len(translated_manual_text) > 0
        if ref_manual_type:
            manual_type = ref_manual_type

    # 3. Barcode & QR Code Detection (Page 1 & All Pages)
    p1_codes = detect_barcodes_and_qr_codes(page, dpi=150)
    has_qr = any(c["type"] == "QRCODE" for c in p1_codes)
    qr_data = next((c["data"] for c in p1_codes if c["type"] == "QRCODE"), "Missing")

    has_barcode = any(c["type"] == "BARCODE" for c in p1_codes)
    barcode_data = next((c["data"] for c in p1_codes if c["type"] == "BARCODE"), "Missing")

    # Fallback for Page 1 Barcode presence: check page embedded images if detector is unavailable
    if not has_barcode and len(page.get_images()) > 0:
        has_barcode = True
        barcode_data = "Barcode Present"

    doc.close()

    # Multi-page Barcode & QR Code check across ALL pages
    doc_code_info = extract_all_pages_barcode_qr(pdf_path, dpi=150)

    return {
        "filename": os.path.basename(pdf_path),
        "pdf_path": pdf_path,
        "page_size": (width, height),
        "document_number": doc_num,
        "document_length": doc_len,
        "version": version,
        "language": language,
        "title": title,
        "has_title": has_title,
        "max_font_size": max_font_size,
        "manual_type": manual_type,
        "manual_type_bbox": manual_type_bbox,
        "has_manual_type": has_manual_type,
        "translated_manual_text": translated_manual_text,
        "has_barcode": has_barcode or doc_code_info["has_barcode"],
        "has_qr": has_qr or doc_code_info["has_qr"],
        "qr_data": qr_data,
        "barcode_data": barcode_data,
        "doc_has_barcode": doc_code_info["has_barcode"],
        "doc_has_qr": doc_code_info["has_qr"],
        "pages_with_barcode": doc_code_info["pages_with_barcode"],
        "pages_with_qr": doc_code_info["pages_with_qr"],
        "all_codes": doc_code_info["all_codes"],
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
    language_status = f"Present ({tgt_lang})" if lang_pass else "Not Present"

    # 5. Barcode Check
    bc_pass = source_model["has_barcode"] and target_model["has_barcode"]

    # 6. QR Code Check
    qr_pass = source_model["has_qr"] and target_model["has_qr"]

    # 7. Title Check (Must be equal between Master English PDF and Translated PDF)
    src_title = source_model["title"].strip()
    tgt_title = target_model["title"].strip()
    title_pass = bool(tgt_title) and (src_title.lower() == tgt_title.lower())
    title_status = "Equal" if title_pass else "Not Equal"

    # 8. Manual Type Check
    manual_type_pass = target_model["has_manual_type"]

    # Overall Verdict
    overall_pass = (
        size_pass and ver_pass and doc_len_pass and lang_pass and bc_pass and qr_pass and title_pass and manual_type_pass
    )

    version_display = f"PASS ({tgt_ver})" if ver_pass else f"FAIL ({tgt_ver if tgt_ver else 'N/A'})"
    doc_len_display = f"PASS (Len {tgt_len})" if doc_len_pass else f"FAIL (Len {tgt_len if tgt_len is not None else 'N/A'})"

    return {
        "english_pdf": source_model["filename"],
        "translated_pdf": target_model["filename"],
        "language": tgt_lang if tgt_lang else "MISSING",
        "title_val": target_model["title"] if target_model["title"] else "MISSING",
        "title_status": title_status,
        "manual_type_val": target_model["manual_type"],
        "manual_type_status": "Present" if manual_type_pass else "Not Present",
        "page_size_val": f"{tgt_size[0]} x {tgt_size[1]}",
        "page_size_status": "PASS" if size_pass else "FAIL",
        "version_val": tgt_ver if tgt_ver else "N/A",
        "version_status": "PASS" if ver_pass else "FAIL",
        "version_display": version_display,
        "doc_len_val": tgt_len if tgt_len is not None else "N/A",
        "doc_len_status": "PASS" if doc_len_pass else "FAIL",
        "doc_len_display": doc_len_display,
        "language_status": language_status,
        "barcode_status": "Present" if bc_pass and target_model["has_barcode"] else "Not Present",
        "qr_status": "Present" if qr_pass and target_model["has_qr"] else "Not Present",
        "overall_verdict": "PASS" if overall_pass else "FAIL",
    }