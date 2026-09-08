import os
import re
import sys
import argparse
import pymupdf as fitz  # PyMuPDF (aliased as fitz for API compat)
import cv2
import numpy as np

from core import toc as TOC

# ==============================================================================
# CONFIGURATION - EDIT YOUR PATHS HERE DIRECTLY
# ==============================================================================
ENGLISH_CROPS_DIR = r"Output\Cropped_Images"
TRANSLATED_PDF_DIR = r"Input\Translated"
OUTPUT_REPORT_DIR = r"Output\Cropped_Comparison"
SIMILARITY_THRESHOLD = 80.0  # Pass threshold percentage (e.g. 80.0%)
DPI = 150

# Below this, a "best match" is not a match at all - it is the highest number
# the search happened to return, which on a 92-page manual is always some
# unrelated diagram. Reporting "moved to page 59" at 29% told a reviewer to go
# and look at a page the graphic was never on. Anything under this floor is
# reported as NOT FOUND, against the page the graphic was expected on.
MIN_CREDIBLE_MATCH = 55.0

# Coarse-to-fine search. Every candidate page is first checked on a shrunken
# copy, which is ~15x cheaper, and only the few pages that look promising are
# re-checked at full resolution. On the 92-page manual a full-document hunt for
# one crop drops from 5.3s to about 0.4s.
COARSE_SCALE = 0.35
COARSE_KEEP = 3           # pages carried from the coarse pass into the fine one
COARSE_MARGIN = 12.0      # ...plus anything within this many points of the best
# ==============================================================================

def _render_page_masked(page, dpi=DPI):
    """One page rendered with its text painted out, as (bgr, gray)."""
    text_dict = page.get_text("dict")
    for b in text_dict.get("blocks", []):
        if b.get("type") == 0:  # Text block
            for line in b.get("lines", []):
                page.draw_rect(fitz.Rect(line["bbox"]), color=(1, 1, 1), fill=(1, 1, 1))

    pix = page.get_pixmap(dpi=dpi)
    data = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        return cv2.cvtColor(data, cv2.COLOR_RGBA2BGR), cv2.cvtColor(data, cv2.COLOR_RGBA2GRAY)
    if pix.n == 3:
        return cv2.cvtColor(data, cv2.COLOR_RGB2BGR), cv2.cvtColor(data, cv2.COLOR_RGB2GRAY)
    return cv2.cvtColor(data, cv2.COLOR_GRAY2BGR), data


class TranslatedPages:
    """
    The translated document's pages, rendered once and kept only as far as they
    are needed.

    The previous version built two full-colour and two greyscale copies of every
    page up front: on a 92-page A4 manual that is close to a gigabyte of RAM per
    document, which is what made the window stop repainting mid-run. Only the
    greyscale pages are needed to search - a third of the memory - and the
    colour copy of a page is rendered on demand, for the handful of pages that
    actually produce a side-by-side image.

    A shrunken copy of each page is also kept for the coarse pass. It costs
    about an eighth of the greyscale page and saves most of the matching work.
    """

    def __init__(self, pdf_path, dpi=DPI, coarse_scale=COARSE_SCALE, progress=None):
        self.pdf_path = pdf_path
        self.dpi = dpi
        self.coarse_scale = coarse_scale
        self.gray = []
        self.small = []
        self._bgr_cache = {}
        doc = fitz.open(pdf_path)
        try:
            total = len(doc)
            for p_idx in range(total):
                _bgr, gray = _render_page_masked(doc[p_idx], dpi=dpi)
                self.gray.append(gray)
                self.small.append(cv2.resize(gray, None, fx=coarse_scale, fy=coarse_scale,
                                             interpolation=cv2.INTER_AREA))
                if progress:
                    progress(p_idx + 1, total, "rendering")
        finally:
            doc.close()

    def __len__(self):
        return len(self.gray)

    def bgr(self, page_no):
        """The colour page, rendered the first time a match image needs it."""
        hit = self._bgr_cache.get(page_no)
        if hit is None:
            with fitz.open(self.pdf_path) as doc:
                hit, _gray = _render_page_masked(doc[page_no - 1], dpi=self.dpi)
            # Only a few pages are ever drawn; a small cache keeps a page that
            # carries many crops from being re-rendered for each of them.
            if len(self._bgr_cache) > 8:
                self._bgr_cache.clear()
            self._bgr_cache[page_no] = hit
        return hit


def render_translated_pdf_pages_pure_graphics(trans_pdf_path, dpi=DPI):
    """
    Render translated PDF pages with text masked out, isolating pure graphics.

    Retained for the command-line entry point and any external caller; the run
    itself uses TranslatedPages, which does not hold every colour page in RAM.
    """
    pages = TranslatedPages(trans_pdf_path, dpi=dpi)
    return [pages.bgr(i + 1) for i in range(len(pages))], pages.gray

_PAGE_IN_NAME = re.compile(r"^p(\d{3,4})_", re.IGNORECASE)
_PAGE_IN_FOLDER = re.compile(r"^page_(\d+)$", re.IGNORECASE)


def _iter_english_crops(eng_crop_dir):
    """
    Walk the master's crop tree, whichever layout it uses.

    crop_images writes topic folders when the PDF has a TOC and page folders when
    it does not, so the page number is recovered from the filename prefix first
    (topic layout) and from the folder name otherwise. Yields
    (folder, page_number, topic_name, filename), ordered by page then filename so
    the comparison sequence does not depend on the layout.
    """
    rows = []
    for root, _dirs, files in os.walk(eng_crop_dir):
        folder = os.path.basename(root)
        if os.path.abspath(root) == os.path.abspath(eng_crop_dir):
            continue
        m_folder = _PAGE_IN_FOLDER.match(folder)
        for f in files:
            if not f.lower().endswith(".png"):
                continue
            # Barcodes and QR codes are excluded: their contents legitimately
            # differ between documents, so a visual diff is meaningless.
            if any(k in f.lower() for k in ("barcode", "qr", "qrcode")):
                continue
            m_name = _PAGE_IN_NAME.match(f)
            if m_name:
                page, topic = int(m_name.group(1)), folder
            elif m_folder:
                page, topic = int(m_folder.group(1)), ""
            else:
                continue        # unrecognised layout, skip rather than guess
            rows.append((root, page, topic, f))
    rows.sort(key=lambda r: (r[1], r[3]))
    return rows


def imread_unicode(path, flags=cv2.IMREAD_GRAYSCALE):
    """
    Read an image from a path that may contain non-ASCII characters.

    cv2.imread goes through the ANSI file API on Windows and simply returns
    None for a path it cannot represent in the local code page. Every crop is
    filed under its topic title, and a translated title is full of characters
    that qualify - "1.1 Einfuehrung" is fine but "1.1 Einführung" is not,
    nor is "4.1 Vorsichtsmaßnahmen", nor anything at all in Greek. The
    read failed, the crop looked empty, and every graphic in those topics was
    reported NOT FOUND while the ASCII-titled topics beside them passed. Going
    through numpy sidesteps the code page entirely.
    """
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


def imwrite_unicode(path, img):
    """Write an image to a path that may contain non-ASCII characters."""
    ext = os.path.splitext(path)[1] or ".png"
    try:
        ok, buf = cv2.imencode(ext, img)
        if not ok:
            return False
        buf.tofile(path)
        return True
    except Exception as e:
        print(f"  [WARN] could not write {path}: {e}")
        return False


def _score_on_page(crop, page_gray):
    """Best template score for one crop on one page, or None if it will not fit."""
    if crop.shape[0] > page_gray.shape[0] or crop.shape[1] > page_gray.shape[1]:
        return None
    res = cv2.matchTemplate(page_gray, crop, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    return max_val, max_loc


def _search(pages, crop, crop_small, candidate_pages, threshold, expect_page=None):
    """
    Hunt one crop across a list of candidate pages, cheapest work first.

    Every candidate is scored on the shrunken copy, which is where nearly all
    the pages are eliminated. Only the few that scored best are re-checked at
    full resolution, and that is the score reported - the coarse pass decides
    WHERE to look, never whether something matches.

    `expect_page` is where the graphic ought to be. It settles ties, and ties
    are common: a manual repeats its logo, its hazard icons and its connector
    symbols, so several pages score alike. Without it the search reports
    whichever copy scored a hair higher, which is how the Xylem logo on the
    cover came back as "moved to page 118" - the back cover carries the same
    logo. Checking the nearest plausible page first and stopping there reports
    the copy that is actually the one in question.

    Returns (score 0-1, page number, location) with page -1 when nothing fit.
    """
    coarse = []
    for page_no in candidate_pages:
        hit = _score_on_page(crop_small, pages.small[page_no - 1])
        if hit is not None:
            coarse.append((hit[0], page_no))
    if not coarse:
        return 0.0, -1, (0, 0)

    coarse.sort(key=lambda c: -c[0])
    cut = coarse[0][0] - (COARSE_MARGIN / 100.0)
    finalists = [p for score, p in coarse[:COARSE_KEEP]]
    finalists += [p for score, p in coarse[COARSE_KEEP:] if score >= cut]

    # Nearest to where it belongs first, so the search stops on the right copy.
    if expect_page:
        finalists.sort(key=lambda p: (abs(p - expect_page), p))

    best_score, best_page, best_loc = 0.0, -1, (0, 0)
    for page_no in finalists:
        hit = _score_on_page(crop, pages.gray[page_no - 1])
        if hit is None:
            continue
        score, loc = hit
        if score > best_score:
            best_score, best_page, best_loc = score, page_no, loc
        if best_score * 100 >= threshold:
            break
    return best_score, best_page, best_loc


def _crop_signature(crop):
    """
    A cheap fingerprint, so a graphic that appears on twenty pages is hunted
    for once. Manuals repeat hazard icons and connector symbols constantly, and
    every repeat used to be a fresh full-document search.
    """
    small = cv2.resize(crop, (16, 16), interpolation=cv2.INTER_AREA)
    return (crop.shape, small.tobytes())


# How far off the master's scale a translation's artwork may be drawn and still
# be recognised. A DTP round trip that places a figure at 99% of its original
# size changes nothing a reviewer would call a difference, but normalised
# correlation on thin-line artwork collapses: 1% out takes identical artwork
# from 100% to 56%, well under the pass mark, and reports it as CHECK. Measured
# on this manual's pump illustrations.
SCALE_SWEEP = tuple(round(0.90 + i * 0.01, 2) for i in range(21))   # 0.90 .. 1.10

# A light blur before the retry. Rendering the same line at a fractionally
# different offset lands it on different pixels, which costs a further ~15
# points on thin strokes; softening both sides removes that without blunting a
# genuine difference, which is a change of shape rather than of phase.
_REFINE_BLUR = (3, 3)


def _refine_scale(pages, crop, page_no, threshold):
    """
    Re-hunt one crop on one page, allowing for artwork drawn at a different size.

    Only ever called when the straight search has already failed, so the cost
    falls on the handful of crops that would otherwise be reported as CHECK,
    never on the ones that matched first time.

    Returns (score, loc, scale) for the best scale tried, or None.
    """
    page = cv2.GaussianBlur(pages.gray[page_no - 1], _REFINE_BLUR, 0)
    base = cv2.GaussianBlur(crop, _REFINE_BLUR, 0)

    best = None
    for f in SCALE_SWEEP:
        t = base if f == 1.0 else cv2.resize(base, None, fx=f, fy=f,
                                             interpolation=cv2.INTER_AREA)
        if t.shape[0] > page.shape[0] or t.shape[1] > page.shape[1]:
            continue
        if t.shape[0] < 8 or t.shape[1] < 8:
            continue
        res = cv2.matchTemplate(page, t, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        if best is None or score > best[0]:
            best = (score, loc, f)
        if score * 100 >= threshold:
            break
    return best


def _locate_crop(pages, crop_img, crop_small, scope_pages, scope_desc, span, topic_code,
                 expect, total_trans_pages, threshold):
    """
    One crop's full hunt: its own scope first, the whole document if that is dry.

    A topic-scoped search that found nothing widens rather than reporting a
    false deletion: an outline entry can be wrong, and a graphic that genuinely
    moved section is a finding worth naming, not one worth hiding behind "not
    found".

    Returns (score, page, loc, scope_desc).
    """
    best_score, best_page, best_loc = _search(
        pages, crop_img, crop_small, scope_pages, threshold, expect_page=expect)

    if span and best_score * 100 < threshold:
        rest = [p for p in range(1, total_trans_pages + 1) if p not in set(scope_pages)]
        w_score, w_page, w_loc = _search(pages, crop_img, crop_small, rest, threshold,
                                         expect_page=expect)
        if w_score > best_score:
            best_score, best_page, best_loc = w_score, w_page, w_loc
            scope_desc = f"topic {topic_code}, then widened"

    # Everything above assumes the artwork is drawn at the same size in both
    # documents. When that fails, the figure being a percent or two out is far
    # more likely than it having actually changed, so ask that question before
    # reporting a difference. Only the losing crops pay for this.
    scale = 1.0
    if best_page != -1 and best_score * 100 < threshold:
        hit = _refine_scale(pages, crop_img, best_page, threshold)
        if hit and hit[0] > best_score:
            best_score, best_loc, scale = hit
            if abs(scale - 1.0) > 0.001:
                scope_desc = f"{scope_desc}, at {scale:.0%} scale"

    return best_score, best_page, best_loc, scope_desc, scale


def create_crop_match_image(crop_img_gray, target_page_bgr, max_loc, max_val, eng_page,
                            trans_page, threshold=SIMILARITY_THRESHOLD, status=None,
                            scale=1.0):
    """
    Generate side-by-side comparison image of English crop vs Matched Translated region.

    `status` overrides the verdict drawn on the image. It carries "NOT FOUND"
    for a search whose best score never reached the credibility floor, where
    the right panel shows the place the graphic was expected rather than
    claiming the unrelated thing found there is a match.
    """
    h_c, w_c = crop_img_gray.shape
    x0, y0 = max_loc
    # A match found at another scale occupies a correspondingly bigger or
    # smaller piece of the translated page; lifting the master's own width
    # would crop the figure in half in the side-by-side.
    h_t, w_t = max(1, int(round(h_c * scale))), max(1, int(round(w_c * scale)))
    x1, y1 = min(target_page_bgr.shape[1], x0 + w_t), min(target_page_bgr.shape[0], y0 + h_t)

    trans_crop_bgr = target_page_bgr[y0:y1, x0:x1].copy()

    score_pct = max_val * 100
    box_color = (0, 180, 0) if score_pct >= threshold else (0, 0, 220)

    crop_img_bgr = cv2.cvtColor(crop_img_gray, cv2.COLOR_GRAY2BGR)

    h_max = max(crop_img_bgr.shape[0], trans_crop_bgr.shape[0], 50)
    w1 = crop_img_bgr.shape[1]
    w2 = trans_crop_bgr.shape[1]

    canvas = np.zeros((h_max + 40, w1 + w2 + 30, 3), dtype=np.uint8)
    canvas.fill(255)  # White canvas

    canvas[35:35 + crop_img_bgr.shape[0], 10:10 + w1] = crop_img_bgr
    canvas[35:35 + trans_crop_bgr.shape[0], 20 + w1:20 + w1 + w2] = trans_crop_bgr

    cv2.rectangle(canvas, (20 + w1, 35), (20 + w1 + w2 - 1, 35 + trans_crop_bgr.shape[0] - 1), box_color, 2)

    font = cv2.FONT_HERSHEY_SIMPLEX
    status_text = status or ("PASS" if score_pct >= threshold else "CHECK")
    right_label = (f"Expected near Pg {trans_page}: [{status_text}]" if status == "NOT FOUND"
                   else f"Translated (Pg {trans_page}): {score_pct:.1f}% [{status_text}]")
    cv2.putText(canvas, f"English Graphic (Pg {eng_page})", (10, 22), font, 0.45, (0, 100, 0), 1)
    cv2.putText(canvas, right_label, (20 + w1, 22), font, 0.45, box_color, 1)

    return canvas

def compare_english_crops_with_translated_pdf(eng_crop_dir, trans_pdf_path, output_dir,
                                              threshold=SIMILARITY_THRESHOLD, dpi=DPI,
                                              progress=None):
    """
    Cross-page comparison: searches for every English cropped image across candidate pages
    in the translated PDF and saves visual match images into Output_Cropped_Comparison/<translated_name>/
    """
    trans_name = os.path.splitext(os.path.basename(trans_pdf_path))[0]
    print(f"\n==========================================================================================")
    print(f"Comparing English Pure Graphic Crops vs Translated PDF: {trans_name}")
    print(f"==========================================================================================")

    pages = TranslatedPages(trans_pdf_path, dpi=dpi, progress=progress)
    total_trans_pages = len(pages)

    diff_out_dir = os.path.join(output_dir, trans_name)
    os.makedirs(diff_out_dir, exist_ok=True)

    # Where each topic lives in THIS translation. Empty when the document has no
    # outline, which is the signal to fall back to searching by page number.
    trans_spans = TOC.topic_page_spans(trans_pdf_path)
    if trans_spans:
        print(f"  Searching topic-wise: {len(trans_spans)} topic(s) located in the translation")
    else:
        print("  No outline in the translation - searching page-wise")

    total_crops_checked = 0
    total_matches_found = 0
    crop_details = []
    seen = {}                      # crop fingerprint -> one shared hunt, for repeated graphics
    queued = []                    # per-crop context, reported once the hunts are settled

    all_crops = _iter_english_crops(eng_crop_dir)
    for crop_idx, (folder_path, eng_page_num, topic_name, crop_file) in enumerate(all_crops, start=1):
        if progress:
            progress(crop_idx, len(all_crops), "comparing")
        crop_path = os.path.join(folder_path, crop_file)
        crop_img = imread_unicode(crop_path)

        if crop_img is None or crop_img.shape[0] < 8 or crop_img.shape[1] < 8:
            continue

        # A blank crop scores meaninglessly against every page - normalised
        # correlation on a constant template is numerically undefined - so it
        # is skipped rather than reported as a spurious match somewhere. The
        # cropper no longer writes these, but an output folder from an earlier
        # version can still hold them.
        if float(crop_img.std()) < 1.0:
            print(f"  Eng Pg {eng_page_num:2d} | {crop_file:24s} -> skipped (blank crop)")
            continue

        # Which pages of the translation to search, and why.
        #
        # A page number does not survive translation: the Swedish rendering of
        # this manual is 18 pages against the master's 20, so "page 12" is not
        # the same content. A TOPIC number does survive - 3.2 is 3.2 in every
        # language - so when both documents have an outline the search is scoped
        # to wherever that topic actually landed. Only without an outline does it
        # fall back to guessing from the page number.
        topic_code = TOC.topic_code_of(topic_name) if topic_name else None
        span = trans_spans.get(topic_code) if topic_code else None
        if span:
            lo, hi = span
            scope_pages = list(range(max(1, lo - 1), min(total_trans_pages, hi + 1) + 1))
            scope_desc = f"topic {topic_code} (pp {lo}-{hi})"
        else:
            scope_pages = list(range(max(1, eng_page_num - 3),
                                     min(total_trans_pages, eng_page_num + 3) + 1))
            scope_desc = "page window"

        # Nearest to the master's own page first, so an unmoved graphic is found
        # immediately and the search stops at the first page that clears the bar.
        scope_pages.sort(key=lambda p: (abs(p - eng_page_num), p))

        crop_small = cv2.resize(crop_img, None, fx=COARSE_SCALE, fy=COARSE_SCALE,
                                interpolation=cv2.INTER_AREA)

        # The same hazard icon can appear on twenty pages. Searching for it once
        # and reusing the answer is what keeps a manual full of repeated symbols
        # from costing twenty full-document hunts.
        sig = (_crop_signature(crop_img), tuple(scope_pages))
        group = seen.get(sig)
        if group is None:
            expect = scope_pages[0] if scope_pages else eng_page_num
            best_score, best_page, best_loc, scope_desc, scale = _locate_crop(
                pages, crop_img, crop_small, scope_pages, scope_desc, span, topic_code,
                expect, total_trans_pages, threshold)
            group = {
                "scope_desc": scope_desc,
                "best_score": best_score, "best_page": best_page, "best_loc": best_loc,
                "scale": scale,
            }
            seen[sig] = group

        queued.append({
            "eng_page_num": eng_page_num, "topic_name": topic_name,
            "crop_file": crop_file, "crop_path": crop_path,
            "scope_pages": scope_pages, "group": group,
        })

    for item in queued:
        eng_page_num = item["eng_page_num"]
        topic_name = item["topic_name"]
        crop_file = item["crop_file"]
        scope_pages = item["scope_pages"]
        group = item["group"]
        best_score = group["best_score"]
        best_page = group["best_page"]
        best_loc = group["best_loc"]
        scope_desc = group["scope_desc"]
        scale = group.get("scale", 1.0)
        crop_img = imread_unicode(item["crop_path"])
        if crop_img is None:
            continue
        total_crops_checked += 1

        match_pct = best_score * 100
        is_match = match_pct >= threshold

        # A score this low is noise, not a location. Reporting it as "moved to
        # page 59" sends a reviewer to a page the graphic was never on; the
        # honest answer is that the search did not find it, and the page worth
        # showing is the one it was expected on.
        found_at_all = match_pct >= MIN_CREDIBLE_MATCH and best_page != -1

        if is_match:
            total_matches_found += 1

        if not found_at_all:
            status = "NOT FOUND"
            expected = scope_pages[0] if scope_pages else eng_page_num
            shift_info = f"Not found (expected near page {expected})"
        else:
            status = "MATCH (PASS)" if is_match else "CHECK"
            shift_info = "Same Page" if best_page == eng_page_num else f"Moved to Page {best_page}"

        # The comparison output mirrors the crop layout: topic folders when the
        # master was filed by topic, page folders when it was not. A reviewer
        # opening the two trees side by side then sees the same shape.
        match_save_path = ""
        show_page = best_page if found_at_all else (scope_pages[0] if scope_pages else -1)
        if show_page != -1:
            out_dir_for_match = os.path.join(
                diff_out_dir, topic_name if topic_name else f"page_{eng_page_num:03d}")
            match_img = create_crop_match_image(
                crop_img, pages.bgr(show_page),
                best_loc if found_at_all else (0, 0),
                best_score, eng_page_num, show_page, threshold=threshold,
                status=None if found_at_all else "NOT FOUND",
                scale=scale if found_at_all else 1.0)
            os.makedirs(out_dir_for_match, exist_ok=True)
            match_save_path = os.path.join(out_dir_for_match, f"match_{crop_file}")
            imwrite_unicode(match_save_path, match_img)

        crop_details.append({
            "eng_page": eng_page_num,
            "topic": topic_name,
            "crop_file": crop_file,
            "trans_page": best_page if found_at_all else -1,
            "shift_info": shift_info,
            "scope": scope_desc,
            "match_pct": match_pct,
            "status": status,
            "match_img": match_save_path,
        })

        print(f"  Eng Pg {eng_page_num:2d} | {crop_file:24s} -> Trans Pg "
              f"{(best_page if found_at_all else -1):2d} "
              f"({shift_info:16s} via {scope_desc:22s}) | Match: {match_pct:6.2f}% | {status}")

    overall_pct = (total_matches_found / total_crops_checked * 100) if total_crops_checked > 0 else 0
    overall_status = "PASS" if overall_pct >= threshold else "CHECK"

    print(f"\nResult for {trans_name}: {total_matches_found} / {total_crops_checked} Pure Graphic Crops Matched ({overall_pct:.2f}%) [{overall_status}]\n")

    return {
        "trans_name": trans_name,
        "total_crops": total_crops_checked,
        "matched_crops": total_matches_found,
        "match_pct": overall_pct,
        "overall_status": overall_status,
        "crop_details": crop_details
    }

def process_all_cropped_comparisons(eng_crop_dir=ENGLISH_CROPS_DIR, trans_dir=TRANSLATED_PDF_DIR, output_dir=OUTPUT_REPORT_DIR, threshold=SIMILARITY_THRESHOLD, dpi=DPI):
    """
    Process image matching for all translated PDFs against English cropped images.
    """
    if not os.path.exists(eng_crop_dir):
        print(f"Error: English crops directory not found: {eng_crop_dir}")
        print("Please run crop_pdf_images.py first to generate English cropped images.")
        return

    trans_files = []
    if os.path.isfile(trans_dir):
        trans_files.append(trans_dir)
    elif os.path.isdir(trans_dir):
        for root, _, files in os.walk(trans_dir):
            for file in files:
                if file.lower().endswith(".pdf"):
                    trans_files.append(os.path.join(root, file))

    if not trans_files:
        print(f"No translated PDFs found in: {trans_dir}")
        return

    print(f"English Source Crops: {eng_crop_dir}")
    print(f"Found {len(trans_files)} translated PDF(s) to compare against English cropped images.\n")

    summary_reports = []
    for trans_pdf_path in trans_files:
        res = compare_english_crops_with_translated_pdf(eng_crop_dir, trans_pdf_path, output_dir, threshold=threshold, dpi=dpi)
        summary_reports.append(res)

    print("==========================================================================================")
    print("PURE GRAPHIC CROPPED IMAGE COMPARISON SUMMARY REPORT")
    print("==========================================================================================")
    print(f"{'Translated PDF Name':48s} | {'Matched / Total Crops':22s} | {'Pure Graphic Match %':18s} | Status")
    print("-" * 102)
    for r in summary_reports:
        counts = f"{r['matched_crops']} / {r['total_crops']} images"
        print(f"{r['trans_name']:48s} | {counts:22s} | {r['match_pct']:17.2f}% | {r['overall_status']}")
    print("==========================================================================================")

def main():
    parser = argparse.ArgumentParser(description="Compare English cropped images against Translated PDFs across pages (handling layout shifts).")
    parser.add_argument("--crops", default=ENGLISH_CROPS_DIR, help=f"Path to English cropped images folder (default: {ENGLISH_CROPS_DIR})")
    parser.add_argument("--translated", default=TRANSLATED_PDF_DIR, help=f"Path to translated PDF file or directory (default: {TRANSLATED_PDF_DIR})")
    parser.add_argument("--output", default=OUTPUT_REPORT_DIR, help=f"Output directory for comparison reports (default: {OUTPUT_REPORT_DIR})")
    parser.add_argument("--threshold", type=float, default=SIMILARITY_THRESHOLD, help=f"Similarity pass threshold % (default: {SIMILARITY_THRESHOLD})")
    parser.add_argument("--dpi", type=int, default=DPI, help=f"DPI resolution (default: {DPI})")

    args = parser.parse_args()
    process_all_cropped_comparisons(eng_crop_dir=args.crops, trans_dir=args.translated, output_dir=args.output, threshold=args.threshold, dpi=args.dpi)

if __name__ == "__main__":
    main()


# ==============================================================================
# CROP-TO-CROP COMPARISON
#
# The older path takes each master crop and hunts for it across the whole
# translated PDF. That asks the translation a question it is badly shaped to
# answer: the search can land on a look-alike elsewhere, it cannot see a
# graphic the translation ADDED, and because it matches a fixed-size template
# against a rendered page it collapses when the artwork is placed at 99% of its
# original size - identical figures scored 56%.
#
# Cropping both documents through the SAME extractor and comparing crop against
# crop removes all three. Both sides are filtered identically, so the counts are
# comparable by construction; pairing happens inside a topic, so 4.6 is only
# ever compared with 4.6; and a pair is scaled to a common size before it is
# scored, so how big the figure was placed stops mattering.
# ==============================================================================

# Two crops are scaled to a common size before scoring, so a figure placed at a
# slightly different size is no longer a difference. Aspect ratio is not
# normalised away: a figure that changed shape HAS changed.
ASPECT_TOLERANCE = 1.35

# Softens the sub-pixel differences left by rendering the same artwork at two
# different offsets, which cost ~15 points on thin-line drawings.
_PAIR_BLUR = (3, 3)


def crop_similarity(a, b):
    """
    How alike two crops are, 0..1, independent of the size they were placed at.

    `b` is scaled onto `a`'s shape before scoring. A pair whose aspect ratios
    disagree by more than ASPECT_TOLERANCE is reported as no match at all
    rather than squashed into agreement.
    """
    if a is None or b is None or a.size == 0 or b.size == 0:
        return 0.0
    ah, aw = a.shape[:2]
    bh, bw = b.shape[:2]
    if ah < 4 or aw < 4 or bh < 4 or bw < 4:
        return 0.0

    ar_a, ar_b = aw / float(ah), bw / float(bh)
    if max(ar_a, ar_b) / max(1e-6, min(ar_a, ar_b)) > ASPECT_TOLERANCE:
        return 0.0

    bb = cv2.resize(b, (aw, ah), interpolation=cv2.INTER_AREA)
    aa = cv2.GaussianBlur(a, _PAIR_BLUR, 0)
    bb = cv2.GaussianBlur(bb, _PAIR_BLUR, 0)

    # A constant crop has no variation for correlation to work on; the cropper
    # drops these, but an older output folder can still hold one.
    if float(aa.std()) < 1e-6 or float(bb.std()) < 1e-6:
        return 1.0 if abs(float(aa.mean()) - float(bb.mean())) < 1.0 else 0.0

    return float(cv2.matchTemplate(aa, bb, cv2.TM_CCOEFF_NORMED)[0][0])


# The fallback comparison, used only on a pair the strict one has already
# failed. Both crops are reduced to this many pixels on their longest side and
# blurred, which compares where the ink IS rather than which pixels it is on.
COARSE_PAIR_SIZE = 48
_COARSE_PAIR_BLUR = (7, 7)


def crop_similarity_coarse(a, b):
    """
    A structural comparison, for artwork whose proportions changed.

    Some figures are drawn to fit their own text. The data plate in topic 2.2
    is the same drawing in every language - same logo, same CE mark, same boxes
    - but its rows are set to the wording inside them, so the German plate is
    149x77pt against the English 149x83. Rescaling one onto the other then puts
    every internal rule several pixels out, and correlation on thin-line
    artwork reads that as a different picture: 35%, against a pass mark of 80.

    Reducing both to a thumbnail and blurring compares the arrangement of the
    ink instead of the placement of individual strokes. On this manual that
    lifts the plate to 89% while unrelated graphics stay near 26%, so it is
    only ever consulted after the strict comparison has already said no.
    """
    if a is None or b is None or a.size == 0 or b.size == 0:
        return 0.0
    ah, aw = a.shape[:2]
    if ah < 4 or aw < 4 or b.shape[0] < 4 or b.shape[1] < 4:
        return 0.0

    bb = cv2.resize(b, (aw, ah), interpolation=cv2.INTER_AREA)
    scale = COARSE_PAIR_SIZE / float(max(ah, aw))
    if scale < 1.0:
        a = cv2.resize(a, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        bb = cv2.resize(bb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    a = cv2.GaussianBlur(a, _COARSE_PAIR_BLUR, 0)
    bb = cv2.GaussianBlur(bb, _COARSE_PAIR_BLUR, 0)
    if float(a.std()) < 1e-6 or float(bb.std()) < 1e-6:
        return 0.0
    return float(cv2.matchTemplate(a, bb, cv2.TM_CCOEFF_NORMED)[0][0])


# A crop must be at least this share of the other's area before one is checked
# for being contained in the other. Without it any small element - a circle, a
# bracket - would be found somewhere inside any large diagram and pass.
CONTAINMENT_MIN_AREA_RATIO = 0.30


def crop_similarity_contained(a, b):
    """
    Is the smaller of two crops simply a tighter framing of the larger?

    Clustering does not always draw the same box around the same artwork in two
    languages. A leader line that reaches out of the figure in one rendering
    and stops short in another, or a neighbouring element that merges on one
    side only, changes where the crop ends - so the pictures match but the
    rectangles do not. Measured on the 3315 manual: the English CSA plate is
    cropped just before its right-hand mounting hole while the German one keeps
    it, and comparing the two rectangles scores 31%.

    Both documents render at the same resolution, so the same artwork occupies
    the same number of pixels in both. That makes the question answerable
    directly - look for the smaller crop inside the larger - and the same three
    plates score 98% that way.

    Guarded by area, because "found somewhere inside" is a weak claim when the
    thing being looked for is tiny.
    """
    if a is None or b is None or a.size == 0 or b.size == 0:
        return 0.0
    big, small = (a, b) if a.size >= b.size else (b, a)
    if small.shape[0] > big.shape[0] or small.shape[1] > big.shape[1]:
        return 0.0                      # not contained in either direction
    if min(small.shape[:2]) < 8:
        return 0.0
    if (small.size / float(big.size)) < CONTAINMENT_MIN_AREA_RATIO:
        return 0.0
    if float(small.std()) < 1e-6 or float(big.std()) < 1e-6:
        return 0.0
    res = cv2.matchTemplate(cv2.GaussianBlur(big, _PAIR_BLUR, 0),
                            cv2.GaussianBlur(small, _PAIR_BLUR, 0),
                            cv2.TM_CCOEFF_NORMED)
    return float(cv2.minMaxLoc(res)[1])


def _group_by_topic(rows):
    """
    Crops keyed by the thing both documents share.

    The topic CODE, because titles are translated and numbering is not -
    "1.2 Safety terminology and symbols" and "1.2 Veiligheidstermen en
    symbolen" are the same section. Without an outline there is no code, and
    the page number is the only handle left.
    """
    out = {}
    for folder, page, topic, fname in rows:
        code = TOC.topic_code_of(topic) if topic else None
        key = f"topic:{code}" if code else f"page:{page}"
        out.setdefault(key, []).append((folder, page, topic, fname))
    return out


def create_pair_image(a_gray, b_gray, eng_page, trans_page, score_pct,
                      threshold=SIMILARITY_THRESHOLD, status=None):
    """Master crop beside its translated counterpart, captioned with the score."""
    box = (0, 180, 0) if score_pct >= threshold else (0, 0, 220)
    a_bgr = cv2.cvtColor(a_gray, cv2.COLOR_GRAY2BGR)
    if b_gray is None:
        b_bgr = np.full((max(40, a_gray.shape[0]), max(60, a_gray.shape[1]), 3),
                        245, np.uint8)
        cv2.putText(b_bgr, "none", (6, b_bgr.shape[0] // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 200), 1)
    else:
        b_bgr = cv2.cvtColor(b_gray, cv2.COLOR_GRAY2BGR)

    h = max(a_bgr.shape[0], b_bgr.shape[0], 50)
    w1, w2 = a_bgr.shape[1], b_bgr.shape[1]
    canvas = np.full((h + 40, w1 + w2 + 30, 3), 255, np.uint8)
    canvas[35:35 + a_bgr.shape[0], 10:10 + w1] = a_bgr
    canvas[35:35 + b_bgr.shape[0], 20 + w1:20 + w1 + w2] = b_bgr
    cv2.rectangle(canvas, (20 + w1, 35), (20 + w1 + w2 - 1, 35 + b_bgr.shape[0] - 1),
                  box, 2)

    f = cv2.FONT_HERSHEY_SIMPLEX
    label = status or ("PASS" if score_pct >= threshold else "CHECK")
    cv2.putText(canvas, f"English Graphic (Pg {eng_page})", (10, 22), f, 0.45,
                (0, 100, 0), 1)
    right = (f"Translated: [{label}]" if b_gray is None
             else f"Translated (Pg {trans_page}): {score_pct:.1f}% [{label}]")
    cv2.putText(canvas, right, (20 + w1, 22), f, 0.45, box, 1)
    return canvas


def _load_crop(folder, fname):
    """One crop as greyscale, or None when it carries nothing to compare."""
    img = imread_unicode(os.path.join(folder, fname))
    if img is None or img.shape[0] < 8 or img.shape[1] < 8:
        return None
    if float(img.std()) < 1.0:          # blank: matches anything at random
        return None
    return img


def compare_crop_sets(eng_crop_dir, tr_crop_dir, output_dir, trans_name=None,
                      threshold=SIMILARITY_THRESHOLD, progress=None):
    """
    Compare the master's crops against the translation's own crops, topic by topic.

    Both documents are cropped by the same extractor, so the two sides are
    filtered identically and their counts mean the same thing. Within a topic -
    4.6 against 4.6, never "the same page number" - every master crop is scored
    against every translation crop and the one-to-one pairing with the best
    total is taken. Nothing depends on where on the page a graphic ended up, so
    reflow moving it to another column or the next page is not a finding.

    A master crop left without a partner is missing from the translation; a
    translation crop left over is one the translation has added.
    """
    trans_name = trans_name or os.path.basename(tr_crop_dir.rstrip("\/"))
    print(f"\n==========================================================================================")
    print(f"Comparing Master Crops vs Translated Crops: {trans_name}")
    print(f"==========================================================================================")

    eng_rows = _iter_english_crops(eng_crop_dir)
    tr_rows = _iter_english_crops(tr_crop_dir)
    eng_groups = _group_by_topic(eng_rows)
    tr_groups = _group_by_topic(tr_rows)

    shared = sum(1 for k in eng_groups if k in tr_groups)
    print(f"  Master crops {len(eng_rows)} in {len(eng_groups)} group(s); "
          f"translation {len(tr_rows)} in {len(tr_groups)}; {shared} shared")

    diff_out_dir = os.path.join(output_dir, trans_name)
    os.makedirs(diff_out_dir, exist_ok=True)

    from core.image_counts import _best_assignment

    crop_details, extras = [], []
    checked = matched = 0
    keys = sorted(set(eng_groups) | set(tr_groups))

    for gi, key in enumerate(keys, start=1):
        if progress:
            progress(gi, len(keys), "comparing")

        e_items, t_items = eng_groups.get(key, []), tr_groups.get(key, [])
        e_loaded = [(f, p, tp, n, _load_crop(f, n)) for f, p, tp, n in e_items]
        t_loaded = [(f, p, tp, n, _load_crop(f, n)) for f, p, tp, n in t_items]
        e_loaded = [x for x in e_loaded if x[4] is not None]
        t_loaded = [x for x in t_loaded if x[4] is not None]

        n, m = len(e_loaded), len(t_loaded)
        size = max(n, m)
        pairing = []
        if size:
            # Square, so every crop on the longer side gets a slot; the padding
            # scores zero and is what a missing or added graphic falls into.
            scores = [[0.0] * size for _ in range(size)]
            for i in range(n):
                for j in range(m):
                    scores[i][j] = crop_similarity(e_loaded[i][4], t_loaded[j][4])
            pairing = _best_assignment(scores)

        taken = set()
        for i, j in pairing:
            if i >= n:
                if j < m:
                    taken.add(j)
                    extras.append(t_loaded[j])
                continue
            folder, page, topic, fname, img = e_loaded[i]
            checked += 1
            if j < m:
                t_folder, t_page, _t_topic, t_fname, t_img = t_loaded[j]
                pct = crop_similarity(img, t_img) * 100
                # Pairing stays on the strict comparison; only the verdict is
                # allowed the coarse one, and only for a pair that has already
                # failed. A figure redrawn to fit its own text is not a
                # difference worth reporting.
                if pct < threshold:
                    pct = max(pct,
                              crop_similarity_coarse(img, t_img) * 100,
                              crop_similarity_contained(img, t_img) * 100)
            else:
                t_page, t_fname, t_img, pct = -1, "", None, 0.0

            found = t_img is not None and pct >= MIN_CREDIBLE_MATCH
            # A pairing that scores nothing is not a pairing. The assignment
            # has to give every crop on the longer side a partner, so a graphic
            # the translation dropped gets handed whatever was left over -
            # usually the one it added. Claiming that leftover would report the
            # deletion and silently swallow the addition, when they are two
            # separate findings.
            if found and j < m:
                taken.add(j)
            is_match = found and pct >= threshold
            if is_match:
                matched += 1

            if not found:
                status, shift = "NOT FOUND", "Not found in this topic"
            else:
                status = "MATCH (PASS)" if is_match else "CHECK"
                shift = ("Same Page" if t_page == page
                         else f"Moved to Page {t_page}")

            out_dir = os.path.join(diff_out_dir, topic if topic else f"page_{page:03d}")
            os.makedirs(out_dir, exist_ok=True)
            save_to = os.path.join(out_dir, f"match_{fname}")
            imwrite_unicode(save_to, create_pair_image(
                img, t_img, page, t_page, pct, threshold=threshold,
                status=None if found else "NOT FOUND"))

            crop_details.append({
                "eng_page": page, "topic": topic, "crop_file": fname,
                "trans_page": t_page if found else -1, "shift_info": shift,
                "scope": key.replace("topic:", "topic ").replace("page:", "page "),
                "match_pct": pct, "status": status, "match_img": save_to,
            })
            print(f"  Eng Pg {page:2d} | {fname:28s} -> Trans Pg "
                  f"{(t_page if found else -1):2d} ({shift:24s} via {key:14s}) | "
                  f"Match: {pct:6.2f}% | {status}")

        for j, item in enumerate(t_loaded):
            if j not in taken:
                extras.append(item)

    for folder, page, topic, fname, _img in extras:
        print(f"  {'':7s} | {fname:28s} -> EXTRA in the translation (Pg {page})")

    pct = (matched / checked * 100) if checked else 0.0
    status = "PASS" if pct >= threshold and not extras else "CHECK"
    print(f"\nResult for {trans_name}: {matched} / {checked} crops matched "
          f"({pct:.2f}%), {len(extras)} extra in the translation [{status}]\n")

    return {
        "trans_name": trans_name,
        "total_crops": checked,
        "matched_crops": matched,
        "match_pct": pct,
        "extra_crops": len(extras),
        "overall_status": status,
        "crop_details": crop_details,
    }
