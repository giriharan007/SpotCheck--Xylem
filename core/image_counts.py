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

import itertools
import os
import re

import cv2
import numpy as np
import pymupdf as fitz

from core.crop_images import (
    MASK_TEXT_IN_CROPS,
    extract_topics_with_positions,
    find_topic_for_rect,
    get_all_image_candidates,
    is_blank_region,
    mask_text_inside_rect,
    survives_text_masking,
)
from core import barcode_qr as Barcode_QR_Check
from core import docscan
from core import margins as PageMargins

TOPIC_CODE = re.compile(r"^\s*(\d+(?:\.\d+)*)")

UNNUMBERED_PREFIX = "#"

# Graphics that sit before the first topic - the cover, the legal notice, the
# contents list - belong to no section. They get their own bucket so they are
# still counted and still compared like for like, rather than being charged to
# topic 1, which is somewhere else entirely in the document.
FRONT_MATTER_KEY = "front"


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


def _croppable_rects(page, code_rects, page_margins, dpi):
    """
    The graphics on one page that the cropper would actually write out.

    Every caller that counts or pairs graphics goes through here, because the
    checks are only comparable if they agree on what a graphic IS. Four filters,
    in the cropper's own order: a barcode is not a graphic, a region painted
    over is not a graphic, and - the two this used to miss - a region that is
    empty once its text is masked, or too small to render, is not one either.

    Missing the last two is what made "Image Counts" and "Images" report
    different totals for the same page. Both are language-sensitive: a label
    that fills its box in English and not in Spanish changes the count on one
    side only, so identical artwork failed the count check.
    """
    kept = []
    for r in get_all_image_candidates(page, margins=page_margins):
        if any(fitz.Rect(cr.x0 - 5, cr.y0 - 5, cr.x1 + 5, cr.y1 + 5).intersects(r)
               for cr in code_rects):
            continue                              # a code, not a graphic
        if is_blank_region(page, r):
            continue                              # painted over or empty
        kept.append(r)

    if not kept:
        return kept

    # The cropper masks the text inside every candidate on the page before it
    # renders any of them, so the same must happen here or the render below
    # would still be looking at the words.
    if MASK_TEXT_IN_CROPS:
        for r in kept:
            mask_text_inside_rect(page, r)

    return [r for r in kept if survives_text_masking(page, r, dpi=dpi)]


_MODEL_CACHE = {}


def record_model(pdf_path, model, margins=None):
    """Store an already-computed image count model (e.g. from the crop extraction pass)."""
    if not pdf_path or not model:
        return
    norm_path = os.path.abspath(pdf_path)
    m_key = PageMargins.describe(margins) if margins else ""
    _MODEL_CACHE[(norm_path, m_key)] = model


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
    norm_path = os.path.abspath(pdf_path) if pdf_path else ""
    m_key = PageMargins.describe(margins) if margins else ""
    if (norm_path, m_key) in _MODEL_CACHE:
        return _MODEL_CACHE[(norm_path, m_key)]

    topics = extract_topics_with_positions(pdf_path)
    keys = _topic_keys(topics)
    by_topic, titles, topic_pages = {}, {}, {}
    for idx, t in enumerate(topics):
        k = keys[idx]
        titles[k] = t.get("title", "")
        if t.get("page"):
            topic_pages[k] = t.get("page")

    index_of = {id(t): i for i, t in enumerate(topics)}
    total = 0

    # The code sweep is the expensive part of this pass, and the crop stage and
    # the barcode check need exactly the same answer. It is taken from the
    # shared document scan so the three of them pay for it once between them.
    scan = docscan.scan(pdf_path)

    with fitz.open(pdf_path) as doc:
        for pno in range(len(doc)):
            page = doc[pno]
            if scan is not None:
                code_rects = scan.code_rects(pno + 1, dpi=dpi)
            else:
                try:
                    codes = Barcode_QR_Check.detect_barcodes_and_qr_codes(page, dpi=dpi)
                except Exception:
                    codes = []
                code_rects = [c["rect"] for c in codes]

            # Exactly the same view of the page as the cropper: this check
            # exists to agree with the crop count, and it cannot do that if one
            # of them treats a ruled table as a graphic and the other does not.
            for r in _croppable_rects(
                    page, code_rects,
                    PageMargins.margins_for_page(margins, pno + 1, len(doc)), dpi):
                total += 1
                if topics:
                    t = find_topic_for_rect(pno + 1, r, topics)
                    if t is None:
                        k = FRONT_MATTER_KEY
                        titles.setdefault(k, "(front matter, before topic 1)")
                    else:
                        k = keys.get(index_of.get(id(t), -1), "?")
                    by_topic[k] = by_topic.get(k, 0) + 1

    return {
        "filename": os.path.basename(pdf_path),
        "has_toc": bool(topics),
        "topic_count": len(topics),
        "by_topic": by_topic,
        "titles": titles,
        "topic_pages": topic_pages,
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
        src_pages = source_model.get("topic_pages", {})
        tgt_pages = target_model.get("topic_pages", {})
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
                "master_page": src_pages.get(key, "-"),
                "target_page": tgt_pages.get(key, "-"),
                "status": "PASS" if ok else f"FAIL (E({s}) vs T({t}))",
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
            "status": "PASS" if passed else f"FAIL (E({src_total}) vs T({tgt_total}))",
        })

    if passed:
        status = f"PASS (E({src_total}) vs T({tgt_total}), {granularity})"
    else:
        detail = ", ".join(mismatches[:6]) if mismatches else "total"
        if len(mismatches) > 6:
            detail += f", +{len(mismatches) - 6} more"
        status = f"FAIL (E({src_total}) vs T({tgt_total}), {granularity}; differs at {detail})"

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
    if key == FRONT_MATTER_KEY:
        return (-1, [0])              # before everything: it is the cover
    if key.startswith(UNNUMBERED_PREFIX):
        try:
            return (0, [int(key[1:])])
        except ValueError:
            return (0, [0])
    try:
        return (1, [int(p) for p in key.split(".")])
    except ValueError:
        return (2, [0])


def _topic_image_rects(pdf_path, topics, keys, margins=None):
    """
    Every graphic in the document, grouped by topic and kept in READING ORDER
    (top of the page to the bottom, left to right on a tie).

    This is the same walk as count_images_by_topic - same code exclusion, same
    blank-region skip - so a topic's ordered list here always has exactly as
    many entries as its count there. That agreement is what lets order-wise
    pairing run only where it is meaningful (see compare_images_in_order).
    """
    by_topic = {}
    index_of = {id(t): i for i, t in enumerate(topics)}
    scan = docscan.scan(pdf_path)

    with fitz.open(pdf_path) as doc:
        for pno in range(len(doc)):
            page = doc[pno]
            page_num = pno + 1
            if scan is not None:
                code_rects = scan.code_rects(page_num, dpi=150)
            else:
                try:
                    codes = Barcode_QR_Check.detect_barcodes_and_qr_codes(page, dpi=150)
                except Exception:
                    codes = []
                code_rects = [c["rect"] for c in codes]

            candidates = _croppable_rects(
                page, code_rects,
                PageMargins.margins_for_page(margins, page_num, len(doc)), 150)
            # Left-to-right, top-to-bottom on the page, matching how a reviewer's
            # eye actually moves - which is what "1st graphic, 2nd graphic" means.
            candidates = sorted(candidates, key=lambda r: (round(r.y0, 1), round(r.x0, 1)))

            for r in candidates:
                t = find_topic_for_rect(page_num, r, topics)
                k = keys.get(index_of.get(id(t), -1), FRONT_MATTER_KEY) if t is not None \
                    else FRONT_MATTER_KEY
                by_topic.setdefault(k, []).append((page_num, r))

    return by_topic


# Below this, a "best match" is not a match at all - see the identical
# reasoning in compare_crops.py, which is where this floor was originally
# measured for this manual's icon set.
ORDER_MIN_CREDIBLE_MATCH = 55.0
ORDER_PASS_THRESHOLD = 90.0

# The slot is searched a little wider than its own box, not hunted across the
# page: this check is about "is slot #2 still the same graphic", not "where
# did it go" - that question belongs to compare_crops.py, which already
# widens across the whole document when a topic-scoped search comes up empty.
SLOT_SEARCH_PAD_PT = 14.0


def compare_images_in_order(source_pdf_path, target_pdf_path, dpi=150, margins=None,
                            pass_threshold=ORDER_PASS_THRESHOLD, evidence_dir=None):
    """
    Pair up the master's Nth graphic in a topic with the translation's Nth
    graphic in that same topic, and check it is still the same picture.

    Why this exists on top of count_images_by_topic: a count agreeing on both
    sides does not mean nothing moved. Two icons swapped within a topic - the
    warning triangle and the electrical-hazard circle trading places, say -
    leaves the count exactly as it was and the one-directional crop hunt in
    compare_crops.py still finds each icon SOMEWHERE in the topic's page span,
    so both existing checks report PASS. Comparing master slot N against
    translation slot N, in the order they actually appear on the page, is what
    catches a swap that a tally cannot see.

    Deliberately scoped to topics where both documents already agree on the
    count: a topic with a genuine addition or deletion is compare_image_counts'
    job to name, and pairing mismatched-length lists by index would just
    manufacture a false "wrong picture" for every slot after the gap.
    """
    from core.compare_crops import _render_page_masked, create_crop_match_image, SIMILARITY_THRESHOLD

    src_topics = extract_topics_with_positions(source_pdf_path)
    tgt_topics = extract_topics_with_positions(target_pdf_path)
    src_keys = _topic_keys(src_topics)
    tgt_keys = _topic_keys(tgt_topics)
    src_titles = {src_keys[i]: t.get("title", "") for i, t in enumerate(src_topics)}

    src_by_topic = _topic_image_rects(source_pdf_path, src_topics, src_keys, margins)
    tgt_by_topic = _topic_image_rects(target_pdf_path, tgt_topics, tgt_keys, margins)

    findings = []
    src_name = os.path.basename(source_pdf_path)
    tgt_name = os.path.basename(target_pdf_path)

    with fitz.open(source_pdf_path) as src_doc, fitz.open(target_pdf_path) as tgt_doc:
        for key, src_list in src_by_topic.items():
            tgt_list = tgt_by_topic.get(key, [])
            # Different lengths: the count check already names this topic as a
            # mismatch. Pairing by index here would blame the wrong slot for
            # everything after wherever the addition or deletion happened.
            if not src_list or len(src_list) != len(tgt_list):
                continue

            for slot, ((sp, srect), (tp, trect)) in enumerate(zip(src_list, tgt_list), start=1):
                src_page = src_doc[sp - 1]
                tgt_page = tgt_doc[tp - 1]

                crop_gray = _clip_gray_text_masked(src_page, srect, dpi=dpi)
                if crop_gray is None or crop_gray.shape[0] < 8 or crop_gray.shape[1] < 8 \
                        or float(crop_gray.std()) < 1.0:
                    continue     # nothing crisp enough on the master side to compare

                pad = fitz.Rect(trect.x0 - SLOT_SEARCH_PAD_PT, trect.y0 - SLOT_SEARCH_PAD_PT,
                               trect.x1 + SLOT_SEARCH_PAD_PT, trect.y1 + SLOT_SEARCH_PAD_PT) \
                    & tgt_page.rect
                region_gray = _clip_gray_text_masked(tgt_page, pad, dpi=dpi)
                if region_gray is None or region_gray.shape[0] < crop_gray.shape[0] \
                        or region_gray.shape[1] < crop_gray.shape[1]:
                    score, loc = 0.0, (0, 0)
                else:
                    res = cv2.matchTemplate(region_gray, crop_gray, cv2.TM_CCOEFF_NORMED)
                    _min, score, _minloc, loc = cv2.minMaxLoc(res)

                match_pct = score * 100
                found = match_pct >= ORDER_MIN_CREDIBLE_MATCH
                is_match = match_pct >= pass_threshold
                status = ("MATCH (PASS)" if is_match else
                          "CHECK" if found else "DIFFERENT (swap?)")

                # loc is inside the padded search region; add its own top-left
                # in the full page so the evidence image points at the actual
                # page location, not the offset inside the little region.
                _tp_bgr, _g = _render_page_masked(tgt_page, dpi=dpi)
                abs_loc = (int(pad.x0 * dpi / 72.0) + loc[0], int(pad.y0 * dpi / 72.0) + loc[1])
                canvas = create_crop_match_image(
                    crop_gray, _tp_bgr, abs_loc, score, sp, tp,
                    threshold=pass_threshold,
                    status=None if found else "NOT FOUND")

                match_img_path = ""
                if evidence_dir:
                    try:
                        topic_dir = os.path.join(evidence_dir, tgt_name.rsplit(".", 1)[0],
                                                 re.sub(r"[^\w.\-]+", "_", key))
                        os.makedirs(topic_dir, exist_ok=True)
                        match_img_path = os.path.join(topic_dir, f"slot_{slot:02d}.png")
                        from core.compare_crops import imwrite_unicode
                        imwrite_unicode(match_img_path, canvas)
                    except Exception:
                        match_img_path = ""

                findings.append({
                    "english_pdf": src_name,
                    "translated_pdf": tgt_name,
                    "topic_code": key,
                    "topic_title": src_titles.get(key, ""),
                    "slot": slot,
                    "slot_count": len(src_list),
                    "master_page": sp,
                    "target_page": tp,
                    "match_pct": match_pct,
                    "status": status,
                    "passed": is_match,
                    "match_img": match_img_path,
                })

    return findings


# Topics rarely carry more than a handful of graphics, so an exact brute-force
# search over every possible pairing is cheap and optimal up to this size.
# Above it, a greedy highest-score-first fallback still terminates instantly -
# it trades the guarantee of the best possible pairing for one that is very
# unlikely to matter at a size this check will realistically ever see.
MAX_ASSIGNMENT_BRUTE_FORCE = 8


def _best_assignment(scores):
    """
    One-to-one pairing of rows to columns that maximises total score.

    `scores` is an n x n list of lists (master graphics x translation
    graphics, in the order each side's own topic-scoped list happens to be
    in - that order carries no meaning here, unlike compare_images_in_order,
    which is exactly the point). Returns a list of (row, col) index pairs.
    """
    n = len(scores)
    if n == 0:
        return []
    if n <= MAX_ASSIGNMENT_BRUTE_FORCE:
        best_perm, best_total = None, -1.0
        for perm in itertools.permutations(range(n)):
            total = sum(scores[i][perm[i]] for i in range(n))
            if total > best_total:
                best_total, best_perm = total, perm
        return list(enumerate(best_perm))

    pairs = sorted(((scores[i][j], i, j) for i in range(n) for j in range(n)),
                   key=lambda t: -t[0])
    used_rows, used_cols, chosen = set(), set(), []
    for _score, i, j in pairs:
        if i in used_rows or j in used_cols:
            continue
        used_rows.add(i)
        used_cols.add(j)
        chosen.append((i, j))
        if len(chosen) == n:
            break
    return chosen


def compare_images_matched(source_pdf_path, target_pdf_path, dpi=150, margins=None,
                           pass_threshold=ORDER_PASS_THRESHOLD, evidence_dir=None):
    """
    Pair each topic's master graphics against its translation graphics by
    CONTENT rather than by reading-order position, and check every pair is
    still the same picture.

    This supersedes compare_images_in_order, which paired master slot N
    against translation slot N in a flat top-to-bottom reading-order sort.
    That assumption breaks on a multi-column layout: translated text growing
    can push a graphic from the bottom of one column to the top of the next
    column on a LATER page, which changes its position in that flat sort
    without changing what it is - and a genuine swap between two icons has
    the same symptom in reverse, a changed order with an unchanged count.
    Either way, position-based pairing risks comparing the wrong two graphics
    and reporting a false problem, or accidentally comparing the right ones
    for the wrong reason.

    Position never enters into this version. Every master graphic in a topic
    is scored against every translation graphic in that SAME topic (the same
    locality compare_image_counts and compare_crops already rely on), and the
    one-to-one pairing across the whole topic that maximises total similarity
    is what gets reported. A broken or swapped icon still shows up as the
    pairing with the worst score, wherever reflow put it - because nothing
    here ever asked where it was, only what it looks like.

    Deliberately scoped to topics where both documents already agree on the
    count, for the same reason as compare_images_in_order: a topic with a
    genuine addition or deletion is compare_image_counts' job to name, and
    forcing an n x n assignment across lists of different lengths would blame
    some innocent graphic for a gap that belongs to a different check.

    Topic identity only means anything when BOTH documents have a usable
    outline. find_topic_for_rect() files every graphic under "front matter"
    when a document has none at all, so if only one side lost its bookmarks -
    a translation pipeline stripping them while the master keeps its own,
    say - the master's graphics would be split across many numbered topic
    keys while the translation's are all lumped under one "front matter"
    key. Every numbered topic would then find nothing on the other side to
    pair against and get silently skipped, which is worse than the document-
    total fallback compare_image_counts makes in the same situation - that
    one still runs, just coarser. Falling back to one whole-document bucket
    on both sides costs nothing extra here, because content-based pairing
    never depended on topic identity or reading order to begin with.
    """
    from core.compare_crops import _render_page_masked, create_crop_match_image

    src_topics = extract_topics_with_positions(source_pdf_path)
    tgt_topics = extract_topics_with_positions(target_pdf_path)
    both_toc = bool(src_topics) and bool(tgt_topics)

    if both_toc:
        src_keys = _topic_keys(src_topics)
        tgt_keys = _topic_keys(tgt_topics)
        src_titles = {src_keys[i]: t.get("title", "") for i, t in enumerate(src_topics)}
        src_by_topic = _topic_image_rects(source_pdf_path, src_topics, src_keys, margins)
        tgt_by_topic = _topic_image_rects(target_pdf_path, tgt_topics, tgt_keys, margins)
    else:
        src_titles = {FRONT_MATTER_KEY: "(no bookmarks - whole document)"}
        src_by_topic = _topic_image_rects(source_pdf_path, [], {}, margins)
        tgt_by_topic = _topic_image_rects(target_pdf_path, [], {}, margins)

    findings = []
    src_name = os.path.basename(source_pdf_path)
    tgt_name = os.path.basename(target_pdf_path)

    with fitz.open(source_pdf_path) as src_doc, fitz.open(target_pdf_path) as tgt_doc:
        for key, src_list in src_by_topic.items():
            tgt_list = tgt_by_topic.get(key, [])
            if not src_list or len(src_list) != len(tgt_list):
                continue     # compare_image_counts already names a count mismatch here

            n = len(src_list)

            # One crop per master graphic, rendered once and reused across
            # every column of its row in the score matrix below. Text is painted
            # out so the score judges the drawing, not the language of the labels
            # printed inside the crop.
            src_crops = []
            for sp, srect in src_list:
                crop_gray = _clip_gray_text_masked(src_doc[sp - 1], srect, dpi=dpi)
                if crop_gray is None or crop_gray.shape[0] < 8 or crop_gray.shape[1] < 8 \
                        or float(crop_gray.std()) < 1.0:
                    crop_gray = None   # nothing crisp enough to compare
                src_crops.append(crop_gray)

            # One padded search region per translation graphic - padded, not
            # searched across the page, because the graphic's own position in
            # the translation is already known from tgt_by_topic. The pad only
            # absorbs the small placement jitter reflow causes within a
            # graphic's own slot, the same margin compare_images_in_order used.
            # Text is masked here too, on the same rule as the master crop, so
            # the two sides are compared like for like.
            tgt_regions = []
            for tp, trect in tgt_list:
                tgt_page = tgt_doc[tp - 1]
                pad = fitz.Rect(trect.x0 - SLOT_SEARCH_PAD_PT, trect.y0 - SLOT_SEARCH_PAD_PT,
                               trect.x1 + SLOT_SEARCH_PAD_PT, trect.y1 + SLOT_SEARCH_PAD_PT) \
                    & tgt_page.rect
                region_gray = _clip_gray_text_masked(tgt_page, pad, dpi=dpi)
                tgt_regions.append((region_gray, pad))

            scores = [[0.0] * n for _ in range(n)]
            locs = [[(0, 0)] * n for _ in range(n)]
            for i, crop_gray in enumerate(src_crops):
                for j, (region_gray, _pad) in enumerate(tgt_regions):
                    if crop_gray is None or region_gray is None \
                            or region_gray.shape[0] < crop_gray.shape[0] \
                            or region_gray.shape[1] < crop_gray.shape[1]:
                        continue
                    res = cv2.matchTemplate(region_gray, crop_gray, cv2.TM_CCOEFF_NORMED)
                    _min, score, _minloc, loc = cv2.minMaxLoc(res)
                    scores[i][j] = max(0.0, score)
                    locs[i][j] = loc

            for i, j in _best_assignment(scores):
                if src_crops[i] is None:
                    continue    # nothing crisp enough on the master side to judge
                sp, _srect = src_list[i]
                tp, _trect = tgt_list[j]
                region_gray, pad = tgt_regions[j]
                score = scores[i][j]
                loc = locs[i][j]

                match_pct = score * 100
                found = match_pct >= ORDER_MIN_CREDIBLE_MATCH
                is_match = match_pct >= pass_threshold
                status = ("MATCH (PASS)" if is_match else
                          "CHECK" if found else "DIFFERENT (broken or swapped?)")

                tgt_page = tgt_doc[tp - 1]
                _tp_bgr, _g = _render_page_masked(tgt_page, dpi=dpi)
                abs_loc = (int(pad.x0 * dpi / 72.0) + loc[0], int(pad.y0 * dpi / 72.0) + loc[1])
                canvas = create_crop_match_image(
                    src_crops[i], _tp_bgr, abs_loc, score, sp, tp,
                    threshold=pass_threshold,
                    status=None if found else "NOT FOUND")

                match_img_path = ""
                if evidence_dir:
                    try:
                        topic_dir = os.path.join(evidence_dir, tgt_name.rsplit(".", 1)[0],
                                                 re.sub(r"[^\w.\-]+", "_", key))
                        os.makedirs(topic_dir, exist_ok=True)
                        match_img_path = os.path.join(topic_dir, f"item_{i + 1:02d}.png")
                        from core.compare_crops import imwrite_unicode
                        imwrite_unicode(match_img_path, canvas)
                    except Exception:
                        match_img_path = ""

                findings.append({
                    "english_pdf": src_name,
                    "translated_pdf": tgt_name,
                    "topic_code": key,
                    "topic_title": src_titles.get(key, ""),
                    "slot": i + 1,
                    "slot_count": n,
                    "master_page": sp,
                    "target_page": tp,
                    "match_pct": match_pct,
                    "status": status,
                    "passed": is_match,
                    "match_img": match_img_path,
                })

    return findings


def _pixmap_to_gray(pix):
    try:
        data = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n >= 3:
            return cv2.cvtColor(data[:, :, :3], cv2.COLOR_RGB2GRAY)
        return data.reshape(pix.height, pix.width)
    except Exception:
        return None


def _clip_gray_text_masked(page, clip, dpi=150):
    """
    One clipped region rendered to grayscale, with its TEXT painted out.

    This is the fix for a graphic that is identical in both documents scoring a
    false CHECK. The graphic-match score is meant to compare the DRAWING - the
    schematic's lines, the hazard triangle - not the words printed around it.
    But a crop's bounding box legitimately encloses text: the L1/L2/L3 tags on a
    wiring diagram, the "WARNING:" beside a triangle. That text is different in
    every language, so leaving it in dragged an unchanged drawing down to 80-88%
    and flagged it for review purely for being translated.

    Only type-0 text spans are whited out, and only in the RENDERED pixels - the
    page object is never mutated, so the same page can be rendered again for
    another crop or for the evidence image without carrying these edits. Vector
    art, rules and raster/image blocks (type 1) are left untouched, because those
    are exactly what the score should be judging.
    """
    try:
        clip = fitz.Rect(clip) & page.rect
        if clip.is_empty or clip.width < 2 or clip.height < 2:
            return None
        pix = page.get_pixmap(clip=clip, dpi=dpi)
        gray = _pixmap_to_gray(pix)
        if gray is None:
            return None
        scale = dpi / 72.0
        h, w = gray.shape
        for b in page.get_text("dict", clip=clip).get("blocks", []):
            if b.get("type") != 0:                 # keep image blocks (type 1)
                continue
            for line in b.get("lines", []):
                for span in line.get("spans", []):
                    x0, y0, x1, y1 = span["bbox"]
                    # Pixels are measured from the clip's own top-left, and a
                    # pixel of bleed each way takes the glyph's anti-aliased edge
                    # with it so no stem survives to tie the caption to the art.
                    px0 = max(0, int((x0 - clip.x0) * scale) - 1)
                    py0 = max(0, int((y0 - clip.y0) * scale) - 1)
                    px1 = min(w, int((x1 - clip.x0) * scale) + 2)
                    py1 = min(h, int((y1 - clip.y0) * scale) + 2)
                    if px1 > px0 and py1 > py0:
                        gray[py0:py1, px0:px1] = 255
        return gray
    except Exception:
        return None


def compare_image_counts_for(source_pdf_path, target_pdf_path, dpi=150, margins=None):
    """Convenience wrapper: build both models and compare them."""
    return compare_image_counts(
        count_images_by_topic(source_pdf_path, dpi=dpi, margins=margins),
        count_images_by_topic(target_pdf_path, dpi=dpi, margins=margins),
    )