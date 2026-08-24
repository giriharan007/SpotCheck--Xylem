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

from core import docscan

try:
    from pyzbar.pyzbar import decode as pyzbar_decode
    HAS_PYZBAR = True
except Exception as e:
    HAS_PYZBAR = False
    print(f"[Warning] pyzbar could not be loaded: {e}")
    print("[Warning] Barcode DECODING is unavailable. Codes will still be located "
          "structurally, but install pyzbar to read their contents:  pip install pyzbar")


# ==============================================================================
# STRUCTURAL (DECODE-FREE) CODE DETECTION
# ==============================================================================
# Locating a code must not depend on being able to decode it. When pyzbar is
# missing the decoders find nothing, and "found nothing" used to be
# indistinguishable from "there is nothing here" - which produced two silent
# failures at once: the barcode count check passed vacuously at 0/0, and the
# undetected barcode was handed to the visual crop comparison, where the bars
# legitimately differ between documents and it failed at ~74% in every language.
#
# These tests look at the rendered pixels, so they work whether a code is drawn
# as vector paths, a raster image or a pattern fill. Validated on the Start 350
# manual: exactly one barcode and one QR found in the master and all eleven
# translations, and no false positives on logos, hazard icons, the product
# photo, the wiring diagram or the page banner.

STRUCTURAL_DPI = 200


def _binarise(page, rect, dpi=STRUCTURAL_DPI):
    pix = page.get_pixmap(dpi=dpi, clip=pymupdf.Rect(rect))
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 1:
        gray = a[:, :, 0].astype(float)
    else:
        gray = 0.299 * a[:, :, 0] + 0.587 * a[:, :, 1] + 0.114 * a[:, :, 2]
    return gray < 128


def code_features(page, rect, dpi=STRUCTURAL_DPI):
    """Shape statistics used to recognise a code without decoding it."""
    b = _binarise(page, rect, dpi=dpi)
    if b.shape[0] < 8 or b.shape[1] < 8:
        return None
    col = b.mean(axis=0)
    return {
        "ink": float(b.mean()),
        # ~0 when every column is entirely ink or entirely blank, as in a 1D barcode
        "col_uniform": float(np.mean(np.minimum(col, 1.0 - col))),
        "x_runs": int(np.count_nonzero(np.diff((col > 0.5).astype(int)))),
        "tx": float((b[:, 1:] != b[:, :-1]).mean()),
        "ty": float((b[1:, :] != b[:-1, :]).mean()),
        "aspect": b.shape[1] / b.shape[0],
    }


def classify_code_shape(f):
    """Return "BARCODE", "QRCODE" or None for a region's shape statistics."""
    if not f:
        return None
    # 1D barcode: uniform columns, many vertical stripes, wide, no vertical detail.
    if (f["col_uniform"] < 0.06 and f["x_runs"] >= 20
            and 0.15 < f["ink"] < 0.85 and f["aspect"] > 1.5 and f["ty"] < 0.10):
        return "BARCODE"
    # QR: square, about half ink by construction, fine detail on both axes.
    # Ink coverage is what separates it from icons and line art, which run 6-25%.
    if (0.75 <= f["aspect"] <= 1.33 and 0.35 <= f["ink"] <= 0.68
            and f["tx"] >= 0.05 and f["ty"] >= 0.05):
        return "QRCODE"
    return None


def _proposal_rects(page, gap=2.0):
    """
    Compact self-contained region proposals.

    Deliberately not shared with crop_images.get_all_image_candidates: that
    module imports this one, and codes are isolated blocks that a simple
    overlap merge finds perfectly well.
    """
    rects = []
    try:
        for d in page.get_drawings():
            r = pymupdf.Rect(d["rect"])
            if r.width > 4 and r.height > 4:
                rects.append(r)
    except Exception:
        pass
    try:
        for info in page.get_image_info():
            r = pymupdf.Rect(info["bbox"]) & page.rect
            if r.width > 4 and r.height > 4:
                rects.append(r)
    except Exception:
        pass

    merged, changed = rects, True
    while changed and merged:
        changed = False
        out, used = [], [False] * len(merged)
        for i, a in enumerate(merged):
            if used[i]:
                continue
            cur = pymupdf.Rect(a)
            used[i] = True
            for j in range(i + 1, len(merged)):
                if used[j]:
                    continue
                grown = pymupdf.Rect(cur.x0 - gap, cur.y0 - gap, cur.x1 + gap, cur.y1 + gap)
                if grown.intersects(merged[j]):
                    cur.include_rect(merged[j])
                    used[j] = True
                    changed = True
            out.append(cur)
        merged = out
    return [r for r in merged if r.width >= 10 and r.height >= 10]


def detect_code_like_regions(page, dpi=STRUCTURAL_DPI):
    """Locate barcode- and QR-shaped regions on a page without decoding them."""
    found = []
    for r in _proposal_rects(page):
        clipped = r & page.rect
        if clipped.is_empty or clipped.width < 10 or clipped.height < 10:
            continue
        try:
            kind = classify_code_shape(code_features(page, clipped, dpi=dpi))
        except Exception:
            kind = None
        if kind:
            found.append({
                "type": kind,
                "raw_type": f"{kind}(structural)",
                "data": "",
                "rect": clipped,
                "page_num": page.number + 1,
                "decoded": False,
            })
    return found


# -----------------------------------------------------------
# Page-level Barcode & QR Code Detector
# -----------------------------------------------------------

# One resolution for every code sweep in the run. The detectors were called at
# 200 dpi from the document-level extractor and 150 from the crop stage, which
# meant two sweeps of the same page could not share a result. Measured on the
# 92-page A4 manual, 72, 100 and 150 dpi all locate exactly the same codes, so
# the shared value is the cheaper one and nothing is lost.
SCAN_DPI = 150


def detect_barcodes_and_qr_codes(page, dpi=SCAN_DPI):
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
                    "page_num": page.number + 1,
                    "decoded": True,
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
                    "page_num": page.number + 1,
                    "decoded": True,
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
                        "page_num": page.number + 1,
                        "decoded": True,
                    })
    except Exception:
        pass

    # 4. Structural sweep: catches codes no decoder could read. Anything already
    #    decoded wins, so this only ever ADDS regions the decoders missed.
    try:
        for cand in detect_code_like_regions(page):
            if not any(d["rect"].intersects(cand["rect"]) for d in detected):
                detected.append(cand)
    except Exception as e:
        print(f"[Warning] Structural code detection failed on page {page.number + 1}: {e}")

    return detected


# -----------------------------------------------------------
# Document-level Barcode & QR Code Count Extractor
# -----------------------------------------------------------

def extract_barcode_qr_model(pdf_path, dpi=SCAN_DPI, progress=None):
    """
    Extract multi-page Barcode and QR Code model from a PDF file.

    Returns dict containing counts, breakdown, and presence indicators.

    The page sweep goes through core.docscan, so a document scanned here is not
    scanned again by the crop or count stage - and the master, which used to be
    re-swept once per translation, is swept once per run. Detection itself is
    unchanged; only who pays for it is.
    """
    all_codes = []
    barcode_count = 0
    qr_count = 0
    decoded_count = 0
    pages_with_barcode = set()
    pages_with_qr = set()

    scan = docscan.scan(pdf_path)
    if scan is not None:
        codes_iter = scan.all_codes(dpi=dpi, progress=progress)
    else:
        codes_iter = []
        with pymupdf.open(pdf_path) as doc:
            for page_idx in range(len(doc)):
                codes_iter.extend(detect_barcodes_and_qr_codes(doc[page_idx], dpi=dpi))

    for c in codes_iter:
        all_codes.append(c)
        if c.get("decoded"):
            decoded_count += 1
        if c["type"] == "BARCODE":
            barcode_count += 1
            pages_with_barcode.add(c["page_num"])
        elif c["type"] == "QRCODE":
            qr_count += 1
            pages_with_qr.add(c["page_num"])

    return {
        "filename": os.path.basename(pdf_path),
        "pdf_path": pdf_path,
        "total_barcodes": barcode_count,
        "total_qr_codes": qr_count,
        "has_barcode": barcode_count > 0,
        "has_qr": qr_count > 0,
        "decoded_count": decoded_count,
        "structural_count": len(all_codes) - decoded_count,
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

    # Say how the codes were found. A count derived without a working decoder is
    # still a valid count, but the reader should know the contents were never
    # read - and a 0/0 with no decoder at all must never look like a clean pass.
    used_structural = bool(source_model.get("structural_count") or target_model.get("structural_count"))
    note = " structural" if used_structural else ""

    no_codes_at_all = (src_bc_count + tgt_bc_count + src_qr_count + tgt_qr_count) == 0
    if no_codes_at_all and not HAS_PYZBAR:
        bc_status = "CHECK (No Detector)"
        qr_status = "CHECK (No Detector)"
        overall_pass = False
        detection_note = ("pyzbar unavailable and nothing found structurally - "
                          "cannot distinguish 'no codes' from 'not detected'")
    else:
        bc_status = (f"PASS (Count {tgt_bc_count}/{src_bc_count}{note})" if bc_pass
                     else f"FAIL (Count {tgt_bc_count}/{src_bc_count}{note})")
        qr_status = (f"PASS (Count {tgt_qr_count}/{src_qr_count}{note})" if qr_pass
                     else f"FAIL (Count {tgt_qr_count}/{src_qr_count}{note})")
        detection_note = ("located structurally; install pyzbar to verify contents"
                          if used_structural else "decoded")

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
        "detection_note": detection_note,
        "overall_verdict": "PASS" if overall_pass else "FAIL",
    }


def compare_barcode_qr(source_pdf_path, target_pdf_path, dpi=SCAN_DPI):
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
