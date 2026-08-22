"""
core/image_counts.py

Symmetric image-count verification.

Why this exists as its own check: the visual crop comparison in compare_crops.py
is one-directional. It crops the MASTER and hunts each crop in the translation,
which means it cannot see a graphic that only the translation has, and — less
obviously — it can miss a graphic the translation is MISSING. Template matching
searches the whole page, so when a page carries several near-identical hazard
icons, deleting one still matches a sibling at ~99%. Both failure modes were
reproduced on the Start 350 manual: an added graphic and a fully erased one each
came back 30/30 PASS.

Counting both documents catches both, because it never asks "is this graphic
somewhere over there" — it asks "do the two documents hold the same number of
graphics in the same place".

Granularity follows the document:
  - With a table of contents, counts are compared PER TOPIC, matched on the
    numeric code (1.1, 3.2.1) rather than the title, since titles are translated
    but numbering is not.
  - Without one, only the document total is compared. Per-page counts are not
    usable as a fallback: translation reflow legitimately moves a graphic from
    one page to the next, and the clean de-DE copy of this very manual already
    differs from the master on pages 11 and 12 while the total is identical.
"""

import os
import re

import pymupdf as fitz

from core.crop_images import (
    extract_topics_with_positions,
    find_topic_for_rect,
    get_all_image_candidates,
    is_blank_region,
)
from core import barcode_qr as Barcode_QR_Check

TOPIC_CODE = re.compile(r"^\s*(\d+(?:\.\d+)*)")

UNNUMBERED_PREFIX = "#"


def _topic_keys(topics):
    """
    A stable identity for each topic that survives translation.

    Numbered topics key on their code. Front matter such as "About this manual"
    carries no number, so those key on their order among the unnumbered ones —
    the structure is the same in every language even though the wording is not.
    """
    keys, seq = {}, 0
    for idx, t in enumerate(topics):
        m = TOPIC_CODE.match(t.get("title") or "")
        if m:
            keys[idx] = m.group(1)
        else:
            seq += 1
            keys[idx] = f"{UNNUMBERED_PREFIX}{seq}"
    return keys


def count_images_by_topic(pdf_path, dpi=150, margins=None):
    """
    Count croppable graphics per topic.

    Counts exactly what the cropper would write: barcode and QR regions are
    excluded, because their contents legitimately differ between documents and
    they are verified separately by barcode_qr.py.

    `margins` must be the same margins the cropper is run with — the count and
    the crop have to agree about what is page furniture, or the count check
    starts reporting differences that only exist between two of our own passes.
    """
    topics = extract_topics_with_positions(pdf_path)
    keys = _topic_keys(topics)
    by_topic, titles = {}, {}
    for idx, t in enumerate(topics):
        titles[keys[idx]] = t.get("title", "")

    index_of = {id(t): i for i, t in enumerate(topics)}
    total = 0

    with fitz.open(pdf_path) as doc:
        for pno in range(len(doc)):
            page = doc[pno]
            try:
                codes = Barcode_QR_Check.detect_barcodes_and_qr_codes(page, dpi=dpi)
            except Exception:
                codes = []
            code_rects = [c["rect"] for c in codes]

            for r in get_all_image_candidates(page, margins=margins):
                if any(fitz.Rect(cr.x0 - 5, cr.y0 - 5, cr.x1 + 5, cr.y1 + 5).intersects(r)
                       for cr in code_rects):
                    continue                      # a code, not a graphic
                if is_blank_region(page, r):
                    continue                      # painted over or empty
                total += 1
                if topics:
                    t = find_topic_for_rect(pno + 1, r, topics)
                    k = keys.get(index_of.get(id(t), -1), "?")
                    by_topic[k] = by_topic.get(k, 0) + 1

    return {
        "filename": os.path.basename(pdf_path),
        "has_toc": bool(topics),
        "topic_count": len(topics),
        "by_topic": by_topic,
        "titles": titles,
        "total": total,
    }


def compare_image_counts(source_model, target_model):
    """
    Compare two count models.

    Per topic when both documents have a TOC, document total otherwise.
    """
    src_total = source_model["total"]
    tgt_total = target_model["total"]
    both_toc = source_model["has_toc"] and target_model["has_toc"]

    rows, mismatches = [], []

    if both_toc:
        src_t, tgt_t = source_model["by_topic"], target_model["by_topic"]
        for key in sorted(set(src_t) | set(tgt_t), key=_sort_key):
            s, t = src_t.get(key, 0), tgt_t.get(key, 0)
            ok = (s == t)
            if not ok:
                mismatches.append(key)
            rows.append({
                "topic_code": key,
                "topic_title": source_model["titles"].get(key)
                               or target_model["titles"].get(key, ""),
                "master_count": s,
                "target_count": t,
                "status": "PASS" if ok else f"FAIL ({t} vs {s})",
            })
        granularity = "per topic"
        passed = not mismatches and src_total == tgt_total
        if src_total != tgt_total and not mismatches:
            mismatches.append("total")
    else:
        granularity = "document total"
        passed = (src_total == tgt_total)
        rows.append({
            "topic_code": "-",
            "topic_title": "(no table of contents - document total only)",
            "master_count": src_total,
            "target_count": tgt_total,
            "status": "PASS" if passed else f"FAIL ({tgt_total} vs {src_total})",
        })

    if passed:
        status = f"PASS ({tgt_total}/{src_total}, {granularity})"
    else:
        detail = ", ".join(mismatches[:6]) if mismatches else "total"
        if len(mismatches) > 6:
            detail += f", +{len(mismatches) - 6} more"
        status = f"FAIL ({tgt_total}/{src_total}, {granularity}; differs at {detail})"

    return {
        "english_pdf": source_model["filename"],
        "translated_pdf": target_model["filename"],
        "granularity": granularity,
        "master_total": src_total,
        "target_total": tgt_total,
        "mismatched_topics": mismatches,
        "rows": rows,
        "overall_verdict": "PASS" if passed else "FAIL",
        "status": status,
    }


def _sort_key(key):
    """Order topic codes numerically (1.2 before 1.10), unnumbered ones first."""
    if key.startswith(UNNUMBERED_PREFIX):
        try:
            return (0, [int(key[1:])])
        except ValueError:
            return (0, [0])
    try:
        return (1, [int(p) for p in key.split(".")])
    except ValueError:
        return (2, [0])


def compare_image_counts_for(source_pdf_path, target_pdf_path, dpi=150, margins=None):
    """Convenience wrapper: build both models and compare them."""
    return compare_image_counts(
        count_images_by_topic(source_pdf_path, dpi=dpi, margins=margins),
        count_images_by_topic(target_pdf_path, dpi=dpi, margins=margins),
    )
