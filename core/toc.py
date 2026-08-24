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
    Extract ONLY topic numerics (e.g. '1', '1.1', '1.3.1', '2.1') from PDF Bookmarks (TOC).
    """
    doc = pymupdf.open(pdf_path)
    numerics = []

    try:
        toc = doc.get_toc(simple=False)
        for item in toc:
            title = item[1]
            match = re.match(r"^\s*(\d+(?:\.\d+)*)", title)
            if match:
                numerics.append(match.group(1))
    except Exception:
        pass
    finally:
        doc.close()

    return numerics


TOPIC_CODE = re.compile(r"^\s*(\d+(?:\.\d+)*)")


def topic_page_spans(pdf_path):
    """
    Which pages each numbered topic occupies, keyed by its numeric code.

    Returns {"1.3": (7, 9), "2.1": (10, 10), ...} - the page a topic starts on
    through the page before the next topic begins, 1-based and inclusive. An
    empty dict means the document has no usable outline.

    The numeric code is the key rather than the title because titles are
    translated and numbering is not: topic 3.2 is topic 3.2 in Swedish. That is
    what lets a graphic be hunted for in the right part of a translation whose
    pagination has shifted, instead of on "the same page number", which in an
    18-page rendering of a 20-page manual means something else entirely.

    A topic that opens partway down a page is treated as owning that whole page,
    and a topic sharing a page with the next one has a single-page span. Both are
    deliberate: the span is a search scope, so erring wide costs nothing while
    erring narrow loses the match.
    """
    spans = {}
    try:
        with pymupdf.open(pdf_path) as doc:
            total = len(doc)
            entries = []
            for item in doc.get_toc(simple=True) or []:
                title, page = item[1], item[2]
                if page is None or page < 1:
                    continue
                m = TOPIC_CODE.match(title or "")
                if m:
                    entries.append((m.group(1), int(page)))
    except Exception as e:
        print(f"  [TOC] Could not read topic spans from {os.path.basename(pdf_path)}: {e}")
        return {}

    if not entries:
        return {}

    entries.sort(key=lambda e: e[1])
    for i, (code, start) in enumerate(entries):
        # The next topic that begins on a LATER page bounds this one; topics
        # sharing a page do not shorten each other to nothing.
        end = total
        for _next_code, next_start in entries[i + 1:]:
            if next_start > start:
                end = next_start - 1
                break
        lo, hi = min(start, end), max(start, end)
        if code in spans:                      # a code repeated: widen the span
            lo = min(lo, spans[code][0])
            hi = max(hi, spans[code][1])
        spans[code] = (max(1, lo), min(total, hi))
    return spans


def page_mapper(master_pdf_path, target_pdf_path):
    """
    A function from a master page number to the page it became in a translation.

    Page numbers do not survive translation. A Danish rendering of an English
    manual runs longer, so English page 12 is Danish page 13 or 14, and by the
    back of the book the drift is several pages. Anything that pairs page N with
    page N is therefore comparing two unrelated pages once reflow has set in -
    which is what a region check was doing, and why a stylesheet check on page
    12 was shown against a page holding a completely different table.

    Topic numbers DO survive: 3.2 is 3.2 in every language. So the drift is
    measured at each topic boundary and applied to the pages inside it. Where a
    topic exists in both documents the mapping is exact at its first page and
    correct to within the reflow inside that topic - a page or so, which the
    caller's own search window absorbs.

    Falls back to the identity mapping (page N -> page N, clamped) when either
    document has no usable outline, which is the same behaviour as before and
    the best available guess.
    """
    src = topic_page_spans(master_pdf_path)
    dst = topic_page_spans(target_pdf_path)

    try:
        with pymupdf.open(target_pdf_path) as doc:
            target_total = len(doc)
    except Exception:
        target_total = 0

    # (master start page, delta) for every topic both documents have, in order.
    shifts = sorted((src[code][0], dst[code][0] - src[code][0])
                    for code in src.keys() & dst.keys())

    def mapped(page_no):
        try:
            page_no = int(page_no)
        except (TypeError, ValueError):
            return page_no
        delta = 0
        for start, d in shifts:
            if start <= page_no:
                delta = d
            else:
                break
        out = page_no + delta
        if target_total:
            out = max(1, min(target_total, out))
        return max(1, out)

    mapped.shifts = shifts
    mapped.usable = bool(shifts)
    return mapped


def topic_code_of(title):
    """The numeric code at the front of a topic title, or None."""
    m = TOPIC_CODE.match(title or "")
    return m.group(1) if m else None


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