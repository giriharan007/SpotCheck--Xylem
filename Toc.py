"""
TOC.py

Extract and Compare Table of Contents (TOC) Topic Numerics between
Master English PDF and Translated PDFs.

Checks:
  - Topic Numerics Only (e.g. 1, 1.1, 1.2, 1.3, 1.3.1, 2, 2.1, 3, 3.1 ...)
  - Page numbers and translated topic text titles are ignored.
"""

import os
import re
import pymupdf


def extract_toc_numerics(pdf_path):
    """
    Extract ONLY topic numerics (e.g. '1', '1.1', '1.3.1', '2.1') from a PDF.

    First attempts to extract from PDF Bookmarks (TOC).
    If bookmarks are empty, falls back to parsing page text section headers.
    """
    doc = pymupdf.open(pdf_path)
    numerics = []

    # 1. Extract from PDF Bookmarks / Table of Contents
    try:
        toc = doc.get_toc(simple=False)
        for item in toc:
            title = item[1]
            match = re.match(r"^\s*(\d+(?:\.\d+)*)", title)
            if match:
                numerics.append(match.group(1))
    except Exception:
        pass

    # 2. Fallback: Parse page text if PDF bookmark TOC is empty
    if not numerics:
        for page in doc:
            text = page.get_text("text")
            for line in text.splitlines():
                match = re.match(r"^\s*(\d+(?:\.\d+)+)\s+[A-Za-z\u00C0-\u024F]", line)
                if match:
                    num = match.group(1)
                    if num not in numerics:
                        numerics.append(num)

    doc.close()
    return numerics


def compare_toc_numerics(source_numerics, target_numerics):
    """
    Compare topic numerics list of translated PDF against source English PDF.

    Pass condition:
      - The numeric sequences must match exactly in order and values.
    """
    is_match = (source_numerics == target_numerics)

    missing_in_target = [num for num in source_numerics if num not in target_numerics]
    extra_in_target = [num for num in target_numerics if num not in source_numerics]

    if is_match:
        status = "PASS"
        diff_msg = "PASS"
    else:
        status = "FAIL"
        msg_parts = []
        if missing_in_target:
            msg_parts.append(f"Missing: {', '.join(missing_in_target)}")
        if extra_in_target:
            msg_parts.append(f"Extra: {', '.join(extra_in_target)}")
        if not msg_parts:
            msg_parts.append("Order Mismatch")
        diff_msg = " | ".join(msg_parts)

    return {
        "status": status,
        "english_topic_count": len(source_numerics),
        "translated_topic_count": len(target_numerics),
        "diff_msg": diff_msg,
        "missing_topics": missing_in_target,
        "extra_topics": extra_in_target,
        "source_numerics_str": ", ".join(source_numerics),
        "target_numerics_str": ", ".join(target_numerics),
    }