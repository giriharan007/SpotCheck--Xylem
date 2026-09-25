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

    if not numerics:
        try:
            printed = topics_from_text(pdf_path)
            for t in printed:
                m = TOPIC_CODE.match(t.get("title", ""))
                if m:
                    numerics.append(m.group(1))
        except Exception:
            pass

    return numerics


TOPIC_CODE = re.compile(r"^\s*(\d+(?:\.\d+)*)")


# A numbered heading as it is PRINTED on the page: the code, whitespace, then
# the title. The whitespace matters - it is what separates "4.7.3 Prepare the
# SUBCAB cables" from the list item "3. Check the functionality", whose code is
# followed by a full stop.
_PRINTED_HEADING = re.compile(r"^(\d+(?:\.\d+)*)\s+(\S.*)$")

# A contents listing looks exactly like a run of headings, so a page carrying
# this many numbered-heading lines is taken to BE the contents page and skipped.
# Real body pages in these manuals carry one or two headings; the contents page
# carries dozens.
_CONTENTS_PAGE_HEADINGS = 6

# How much larger than the body text a line must be set to count as a heading.
# Section headings in these manuals run 1.15x body and up; a numbered list item
# is set at body size exactly.
_HEADING_SIZE_RATIO = 1.06

# Deeper than this is a part number or a measurement, not a section.
_MAX_TOPIC_DEPTH = 4

_text_topics_cache = {}


def _page_lines(doc):
    """Every text line in the document as (page_no, y, x, size, text)."""
    for p_idx in range(len(doc)):
        try:
            blocks = doc[p_idx].get_text("dict").get("blocks", [])
        except Exception:
            continue
        for b in blocks:
            if b.get("type") != 0:
                continue
            for line in b.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                text = "".join(s.get("text", "") for s in spans).strip()
                if not text:
                    continue
                # The size of the span carrying the most characters: a heading
                # with a trailing footnote marker is still a heading.
                size = max(spans, key=lambda s: len(s.get("text", "") or ""))\
                    .get("size", 0.0)
                bbox = line.get("bbox") or (0, 0, 0, 0)
                yield p_idx + 1, float(bbox[1]), float(bbox[0]), float(size), text


def _body_size(lines):
    """The document's dominant text size, weighted by how much text is set in it."""
    weight = {}
    for _p, _y, _x, size, text in lines:
        if size > 0:
            weight[round(size, 1)] = weight.get(round(size, 1), 0) + len(text)
    if not weight:
        return 0.0
    return max(weight.items(), key=lambda kv: kv[1])[0]


def _code_key(code):
    return tuple(int(p) for p in code.split("."))


def topics_from_text(pdf_path):
    """
    Numbered headings read off the printed page, for a document with no outline.

    Every topic-aware check in this application - which pages a topic spans,
    which topic a graphic belongs to, which translated page a master page
    became - was built on doc.get_toc(), the PDF's bookmarks. A translation
    that came back from DTP without its bookmarks therefore had no topics at
    all, and every one of those checks quietly fell back to "page N is page N".
    Once reflow has pushed the Spanish rendering two topics further on, that
    puts English 1.5.2 against Spanish 1.8 and reports the entire page as
    changed.

    The section NUMBER survives translation even when the bookmarks do not:
    "4.7.3 Prepare the SUBCAB cables" is "4.7.3 Prepare los cables SUBCAB".
    So when there is no outline, the headings are read from the page text
    instead, which restores topic alignment for a document that has lost them.

    Returns the same shape as crop_images.extract_topics_with_positions:
    [{"level", "title", "start_page", "top_y"}], in reading order.
    """
    try:
        key = (os.path.abspath(pdf_path), os.path.getmtime(pdf_path))
    except OSError:
        key = (os.path.abspath(pdf_path), None)
    if key in _text_topics_cache:
        return _text_topics_cache[key]

    try:
        with pymupdf.open(pdf_path) as doc:
            lines = list(_page_lines(doc))
    except Exception as e:
        print(f"  [TOC] Could not read printed headings from "
              f"{os.path.basename(pdf_path)}: {e}")
        return []

    body = _body_size(lines)
    min_size = body * _HEADING_SIZE_RATIO if body else 0.0

    # Candidates, and how many each page carries - a contents page is nothing
    # but candidates, and must not be mistaken for the sections themselves.
    per_page = {}
    for page_no, y, x, size, text in lines:
        m = _PRINTED_HEADING.match(text)
        if not m:
            continue
        code, title = m.group(1), m.group(2)
        if len(code.split(".")) > _MAX_TOPIC_DEPTH:
            continue
        if size < min_size:
            continue
        per_page.setdefault(page_no, []).append((y, x, code, title))

    candidates = []
    for page_no, found in sorted(per_page.items()):
        if len(found) >= _CONTENTS_PAGE_HEADINGS:
            continue                       # the contents listing itself
        for y, x, code, title in found:
            candidates.append((page_no, y, code, title))

    candidates.sort(key=lambda c: (c[0], c[1]))

    # A section number printed inside a cross-reference ("see 4.7.3") or a
    # table cell breaks the ascending order the real headings keep. Taking only
    # codes that advance drops those without needing to know what they were.
    topics, seen, last = [], set(), None
    for page_no, y, code, title in candidates:
        if code in seen:
            continue
        try:
            k = _code_key(code)
        except ValueError:
            continue
        if last is not None and k <= last:
            continue
        seen.add(code)
        last = k
        topics.append({
            "level": len(code.split(".")),
            "title": f"{code} {title}".strip(),
            "start_page": page_no,
            "top_y": y,
        })

    if topics:
        print(f"  [TOC] {os.path.basename(pdf_path)} has no usable outline - "
              f"{len(topics)} topic(s) read from the printed headings")
    _text_topics_cache[key] = topics
    return topics


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

    # No bookmarks is not the same as no topics. A translation that lost its
    # outline in DTP still prints its section numbers, and without this the
    # whole document collapses to one span and every caller falls back to
    # pairing page N with page N.
    if not entries:
        printed = topics_from_text(pdf_path)
        entries = [(TOPIC_CODE.match(t["title"]).group(1), t["start_page"])
                   for t in printed if TOPIC_CODE.match(t["title"])]
        try:
            with pymupdf.open(pdf_path) as doc:
                total = len(doc)
        except Exception:
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


def topic_at_page(pdf_path, page_no):
    """
    The numeric code of the topic that owns a page, or None.

    Where two topics share a page the later one wins, because that is the one
    a reader turning to that page is looking at.
    """
    spans = topic_page_spans(pdf_path)
    if not spans:
        return None
    best, best_start = None, -1
    for code, (lo, hi) in spans.items():
        if lo <= page_no <= hi and lo >= best_start:
            best, best_start = code, lo
    return best


def matching_page(master_pdf_path, target_pdf_path, master_page):
    """
    The page of the translation holding the same TOPIC as this master page.

    Pairing page N with page N is only right until the first paragraph grows.
    By the middle of a manual the Spanish rendering is two pages further on, so
    "the same page number" shows a reviewer two unrelated pages and reports the
    whole spread as changed. Topic 4.6 is topic 4.6 in every language, so the
    topic is what the two documents actually have in common.

    Returns (page, topic_code, why). `why` is "topic" when the pairing came from
    a topic both documents carry, "drift" when it was interpolated from the
    nearest topic boundary, and "same page" when neither document has a usable
    outline and there was nothing better to go on.
    """
    src = topic_page_spans(master_pdf_path)
    dst = topic_page_spans(target_pdf_path)
    code = topic_at_page(master_pdf_path, master_page)

    if code and code in src and code in dst:
        # Where the page sits inside its topic, carried across. Clamped to the
        # topic's extent in the translation so a longer section cannot push the
        # pairing past its end.
        offset = master_page - src[code][0]
        lo, hi = dst[code]
        return max(lo, min(hi, lo + offset)), code, "topic"

    mapped = page_mapper(master_pdf_path, target_pdf_path)
    if mapped.usable:
        return mapped(master_page), code, "drift"
    return master_page, code, "same page"


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