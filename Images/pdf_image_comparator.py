import os
import re
import csv
import sys
import time
import traceback
import hashlib
# Fix Windows console encoding for Unicode output
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
import cv2
import numpy as np
import pymupdf
from skimage.metrics import structural_similarity
# ============================================================
# CONFIGURATION
# ============================================================
ENGLISH_DIR = (
    r"C:\Xylem Project\spot\input\English"
)
TRANSLATED_DIR = (
    r"C:\Xylem Project\spot\input\Translated"
)
OUTPUT_DIR = (
    r"C:\Xylem Project\spot\output"
)
# Rendering
RENDER_DPI = 150
# Feature matching
ORB_FEATURES = 5000
# Merge nearby vector drawings within this gap (pixels)
VECTOR_MERGE_GAP = 15
# Minimum size for valid images
MIN_IMAGE_SIZE = 10

# Deduplicate repeated PDF image resources / identical extracted graphics.
# This prevents one physical product image from being compared twice when
# the PDF internally references the same image resource more than once.
DEDUP_IMAGES = True
# Minimum area for vector regions
MIN_VECTOR_AREA = 20
# Maximum comparison size (resize large images)
MAX_COMPARE_SIZE = 1800
# ── Thresholds ──
# pHash: Hamming distance
PHASH_SAME_THRESHOLD = 10       # distance <= 10 → SAME
PHASH_MAYBE_THRESHOLD = 20     # distance 11-20 → needs SSIM
# SSIM threshold
SSIM_THRESHOLD = 0.75
# ORB minimum good matches
ORB_MIN_MATCHES = 10
# Combined score threshold
COMBINED_THRESHOLD = 0.60
# Weights for combined score
WEIGHT_PHASH = 0.30
WEIGHT_SSIM = 0.40
WEIGHT_ORB = 0.30
# ============================================================
# PDF PAIRING
# ============================================================
def extract_product_suffix(filename):
    """
    Extract the product suffix from a PDF filename.
    Example:
        894387_5.0_en-US_2026-04_IOM.Start350.pdf
        → IOM.Start350
    """
    name = os.path.splitext(filename)[0]
    # Pattern: id_version_locale_date_PRODUCT
    parts = name.split("_")
    if len(parts) >= 5:
        # Product is everything after the 4th underscore
        product = "_".join(parts[4:])
        return product
    return name
def extract_locale(filename):
    """
    Extract the locale code from a PDF filename.
    Example:
        882539_5.0_da-DK_2026-04_IOM.Start350.pdf
        → da-DK
    """
    name = os.path.splitext(filename)[0]
    parts = name.split("_")
    if len(parts) >= 3:
        return parts[2]
    return "unknown"
def find_pdf_pairs(english_dir, translated_dir):
    """
    Scan English and Translated folders.
    Match PDFs by product suffix.
    Return list of (english_path, translated_path, locale) tuples.
    """
    pairs = []
    # ── Find English PDFs ──
    english_pdfs = {}
    for f in os.listdir(english_dir):
        if not f.lower().endswith(".pdf"):
            continue
        suffix = extract_product_suffix(f)
        english_pdfs[suffix] = os.path.join(
            english_dir,
            f
        )
    # ── Find Translated PDFs and match ──
    for f in os.listdir(translated_dir):
        if not f.lower().endswith(".pdf"):
            continue
        suffix = extract_product_suffix(f)
        locale = extract_locale(f)
        translated_path = os.path.join(
            translated_dir,
            f
        )
        if suffix in english_pdfs:
            pairs.append((
                english_pdfs[suffix],
                translated_path,
                locale
            ))
        else:
            print(
                f"WARNING: No English match for "
                f"{f} (suffix: {suffix})"
            )
    # Sort by locale for consistent ordering
    pairs.sort(key=lambda x: x[2])
    return pairs
# ============================================================
# PAGE RENDERING
# ============================================================
def render_page(page, dpi=RENDER_DPI):
    """Render a PDF page to a BGR OpenCV image."""
    zoom = dpi / 72.0
    matrix = pymupdf.Matrix(zoom, zoom)
    pix = page.get_pixmap(
        matrix=matrix,
        alpha=False
    )
    data = np.frombuffer(
        pix.samples,
        dtype=np.uint8
    )
    image = data.reshape(
        pix.height,
        pix.width,
        pix.n
    )
    if pix.n == 4:
        image = cv2.cvtColor(
            image,
            cv2.COLOR_RGBA2BGR
        )
    else:
        image = cv2.cvtColor(
            image,
            cv2.COLOR_RGB2BGR
        )
    return image
# ============================================================
# COORDINATE SCALING
# ============================================================
def pdf_to_image_rect(rect, page, image):
    """Scale PDF coordinates to image pixel coordinates."""
    page_width = page.rect.width
    page_height = page.rect.height
    image_height, image_width = image.shape[:2]
    sx = image_width / page_width
    sy = image_height / page_height
    x0 = int(rect[0] * sx)
    y0 = int(rect[1] * sy)
    x1 = int(rect[2] * sx)
    y1 = int(rect[3] * sy)
    return (x0, y0, x1, y1)
# ============================================================
# MERGE NEARBY REGIONS
# ============================================================
def merge_regions(regions, gap=VECTOR_MERGE_GAP):
    """Merge overlapping or nearby bounding boxes."""
    if not regions:
        return []
    regions = [list(r) for r in regions]
    changed = True
    while changed:
        changed = False
        result = []
        while regions:
            current = regions.pop(0)
            merged = False
            for i in range(len(regions)):
                other = regions[i]
                ax0, ay0, ax1, ay1 = current
                bx0, by0, bx1, by1 = other
                close = not (
                    ax1 + gap < bx0
                    or bx1 + gap < ax0
                    or ay1 + gap < by0
                    or by1 + gap < ay0
                )
                if close:
                    current = [
                        min(ax0, bx0),
                        min(ay0, by0),
                        max(ax1, bx1),
                        max(ay1, by1)
                    ]
                    regions.pop(i)
                    regions.insert(0, current)
                    merged = True
                    changed = True
                    break
            if not merged:
                result.append(current)
        regions = result
    return [tuple(r) for r in regions]
# ============================================================
# IMAGE EXTRACTION
# ============================================================
class ExtractedImage:
    """Holds metadata and pixel data for one extracted image."""
    def __init__(
        self,
        image,
        page_number,
        image_index,
        image_type,
        bbox
    ):
        self.image = image              # BGR numpy array
        self.page_number = page_number  # 1-indexed
        self.image_index = image_index  # 1-indexed within page
        self.image_type = image_type    # "raster" or "vector"
        self.bbox = bbox                # (x0, y0, x1, y1) in pixels
def _image_content_hash(image):
    """
    Return a stable hash for image pixel content.

    The hash is used only for deduplication. It intentionally ignores
    the PDF placement/bounding box, because the same image resource can
    be placed more than once in a PDF.
    """
    if image is None or image.size == 0:
        return None

    # Include shape and dtype so differently shaped images cannot collide
    # merely because their raw bytes happen to match.
    hasher = hashlib.sha256()
    hasher.update(str(image.shape).encode("utf-8"))
    hasher.update(str(image.dtype).encode("utf-8"))
    hasher.update(image.tobytes())
    return hasher.hexdigest()


def extract_raster_images(page, rendered, doc, seen_hashes=None):
    """
    Extract individual raster (embedded) images from a page.

    Important:
        A PDF can reference the same embedded image resource multiple
        times. get_images()/get_image_rects() can therefore expose the
        same physical image more than once.

    This function deduplicates by actual decoded pixel content so that
    one product image is returned only once.
    """
    images = []

    if seen_hashes is None:
        seen_hashes = set()

    page_images = page.get_images(full=True)

    for idx, img_info in enumerate(page_images):
        xref = img_info[0]

        # Get all placements of this image resource.
        try:
            rects = page.get_image_rects(img_info)
        except Exception:
            continue

        for rect in rects:
            bbox = pdf_to_image_rect(
                rect, page, rendered
            )

            x0, y0, x1, y1 = bbox
            w = x1 - x0
            h = y1 - y0

            if w < MIN_IMAGE_SIZE or h < MIN_IMAGE_SIZE:
                continue

            cv_image = None

            # Try to extract original embedded image.
            try:
                base_image = doc.extract_image(xref)
                image_bytes = base_image["image"]

                nparr = np.frombuffer(
                    image_bytes,
                    dtype=np.uint8
                )

                cv_image = cv2.imdecode(
                    nparr,
                    cv2.IMREAD_COLOR
                )

                if cv_image is None:
                    raise ValueError(
                        "imdecode returned None"
                    )

            except Exception:
                # Fallback: crop the rendered occurrence.
                y0c = max(0, y0)
                y1c = min(rendered.shape[0], y1)
                x0c = max(0, x0)
                x1c = min(rendered.shape[1], x1)

                if y1c <= y0c or x1c <= x0c:
                    continue

                cv_image = rendered[
                    y0c:y1c,
                    x0c:x1c
                ].copy()

            # ------------------------------------------------
            # DEDUPLICATION
            # ------------------------------------------------
            if DEDUP_IMAGES:
                content_hash = _image_content_hash(cv_image)

                if content_hash in seen_hashes:
                    continue

                seen_hashes.add(content_hash)

            images.append(
                (cv_image, bbox)
            )

    return images


def extract_vector_regions(page, rendered, seen_hashes=None):
    """
    Extract individual meaningful vector-graphic regions.

    Vector drawing pieces are merged only when they overlap or are close
    enough to form one graphic. The resulting crops are then deduplicated
    by their rendered pixel content.

    No whole-page vector canvas is created.
    """
    raw_regions = []

    if seen_hashes is None:
        seen_hashes = set()

    drawings = page.get_drawings()

    for drawing in drawings:
        rect = drawing.get("rect")

        if rect is None:
            continue

        image_rect = pdf_to_image_rect(
            rect, page, rendered
        )

        x0, y0, x1, y1 = image_rect
        w = x1 - x0
        h = y1 - y0
        area = w * h

        if w < 3 or h < 3 or area < MIN_VECTOR_AREA:
            continue

        raw_regions.append(image_rect)

    if not raw_regions:
        return []

    merged = merge_regions(
        raw_regions,
        gap=VECTOR_MERGE_GAP
    )

    images = []

    for bbox in merged:
        x0, y0, x1, y1 = bbox

        x0 = max(0, x0)
        y0 = max(0, y0)
        x1 = min(rendered.shape[1], x1)
        y1 = min(rendered.shape[0], y1)

        if x1 <= x0 or y1 <= y0:
            continue

        w = x1 - x0
        h = y1 - y0

        if w < MIN_IMAGE_SIZE or h < MIN_IMAGE_SIZE:
            continue

        crop = rendered[y0:y1, x0:x1].copy()

        if not is_meaningful_graphic(crop):
            continue

        if DEDUP_IMAGES:
            content_hash = _image_content_hash(crop)

            if content_hash in seen_hashes:
                continue

            seen_hashes.add(content_hash)

        images.append(
            (crop, (x0, y0, x1, y1))
        )

    return images


def is_meaningful_graphic(image, threshold=0.05):
    """
    Check if an image region contains meaningful
    graphical content (not just whitespace/text labels).
    Returns True if at least `threshold` fraction of
    pixels are non-white (< 240 in grayscale).
    """
    if image is None or image.size == 0:
        return False
    gray = cv2.cvtColor(
        image, cv2.COLOR_BGR2GRAY
    )
    # Count non-white pixels
    non_white = np.count_nonzero(gray < 240)
    total = gray.size
    ratio = non_white / total
    return ratio >= threshold
def extract_all_images(pdf_path):
    """
    Extract meaningful graphical objects from a PDF.

    Raster images:
        Extract embedded images individually.

    Vector graphics:
        Extract individual vector drawing regions.

    Deduplication:
        A single seen_hashes set is maintained for the entire PDF.
        Therefore, if the exact same image resource/content occurs on
        multiple pages, it is returned only once.

    This function never creates a whole-page vector image.
    """
    doc = pymupdf.open(pdf_path)
    all_images = []

    # One deduplication set for the entire PDF.
    seen_hashes = set()

    try:
        for page_index in range(len(doc)):
            page = doc[page_index]
            page_number = page_index + 1

            rendered = render_page(page)

            # ------------------------------------------------
            # RASTER / EMBEDDED IMAGES
            # ------------------------------------------------
            raster_imgs = extract_raster_images(
                page,
                rendered,
                doc,
                seen_hashes
            )

            for raster_index, (cv_image, bbox) in enumerate(
                raster_imgs,
                start=1
            ):
                all_images.append(
                    ExtractedImage(
                        image=cv_image,
                        page_number=page_number,
                        image_index=raster_index,
                        image_type="raster",
                        bbox=bbox
                    )
                )

            # ------------------------------------------------
            # VECTOR GRAPHICS
            # ------------------------------------------------
            vector_imgs = extract_vector_regions(
                page,
                rendered,
                seen_hashes
            )

            for vector_index, (cv_image, bbox) in enumerate(
                vector_imgs,
                start=1
            ):
                all_images.append(
                    ExtractedImage(
                        image=cv_image,
                        page_number=page_number,
                        image_index=vector_index,
                        image_type="vector",
                        bbox=bbox
                    )
                )

    finally:
        doc.close()

    return all_images


# ============================================================
# COMPARISON ENGINE — LAYER 1: PERCEPTUAL HASH
# ============================================================
def compute_phash(image):
    """Compute perceptual hash of an image."""
    hasher = cv2.img_hash.PHash_create()
    # Convert to grayscale if needed
    if len(image.shape) == 3:
        gray = cv2.cvtColor(
            image,
            cv2.COLOR_BGR2GRAY
        )
    else:
        gray = image
    hash_value = hasher.compute(gray)
    return hash_value
def compare_phash(image1, image2):
    """
    Compare two images using perceptual hashing.
    Returns:
        distance: Hamming distance (0 = identical)
        score: Normalized score (1.0 = identical, 0.0 = very different)
    """
    hasher = cv2.img_hash.PHash_create()
    hash1 = compute_phash(image1)
    hash2 = compute_phash(image2)
    distance = int(hasher.compare(hash1, hash2))
    # Normalize: max Hamming distance for pHash is 64 bits
    score = max(
        0.0,
        1.0 - (distance / 64.0)
    )
    return distance, score
# ============================================================
# COMPARISON ENGINE — LAYER 2: SSIM
# ============================================================
def compare_ssim(image1, image2):
    """
    Compare two images using Structural Similarity Index.
    Images are resized to common dimensions.
    Returns:
        score: SSIM score (1.0 = identical)
    """
    gray1 = cv2.cvtColor(
        image1,
        cv2.COLOR_BGR2GRAY
    )
    gray2 = cv2.cvtColor(
        image2,
        cv2.COLOR_BGR2GRAY
    )
    # Resize to common dimensions
    target_h = max(gray1.shape[0], gray2.shape[0])
    target_w = max(gray1.shape[1], gray2.shape[1])
    # Cap size for performance
    if max(target_h, target_w) > MAX_COMPARE_SIZE:
        scale = MAX_COMPARE_SIZE / max(target_h, target_w)
        target_h = int(target_h * scale)
        target_w = int(target_w * scale)
    # Ensure minimum size for SSIM (win_size=7 needs >= 7px)
    target_h = max(target_h, 8)
    target_w = max(target_w, 8)
    gray1 = cv2.resize(
        gray1,
        (target_w, target_h),
        interpolation=cv2.INTER_AREA
    )
    gray2 = cv2.resize(
        gray2,
        (target_w, target_h),
        interpolation=cv2.INTER_AREA
    )
    score = structural_similarity(
        gray1,
        gray2,
        data_range=255
    )
    return float(score)
# ============================================================
# COMPARISON ENGINE — LAYER 3: ORB FEATURE MATCHING
# ============================================================
def compare_orb(image1, image2):
    """
    Compare two images using ORB feature matching.
    Returns:
        score: Normalized match score (0.0 to 1.0)
        match_count: Number of good matches
    """
    gray1 = cv2.cvtColor(
        image1,
        cv2.COLOR_BGR2GRAY
    )
    gray2 = cv2.cvtColor(
        image2,
        cv2.COLOR_BGR2GRAY
    )
    orb = cv2.ORB_create(
        nfeatures=ORB_FEATURES
    )
    kp1, des1 = orb.detectAndCompute(gray1, None)
    kp2, des2 = orb.detectAndCompute(gray2, None)
    if des1 is None or des2 is None:
        return 0.0, 0
    if len(des1) < 5 or len(des2) < 5:
        return 0.0, 0
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    matches = matcher.knnMatch(
        des1, des2, k=2
    )
    good = []
    for pair in matches:
        if len(pair) != 2:
            continue
        m, n = pair
        if m.distance < 0.75 * n.distance:
            good.append(m)
    count = len(good)
    if count == 0:
        score = 0.0
    elif count >= 100:
        score = 1.0
    else:
        score = count / 100.0
    return score, count
# ============================================================
# 3-LAYER COMPARISON PIPELINE
# ============================================================
def compare_images(image1, image2):
    """
    Run the 3-layer comparison pipeline on two images.
    Returns a dict with all scores and final verdict.
    """
    # Resize for comparison
    img1 = resize_for_comparison(image1)
    img2 = resize_for_comparison(image2)
    # ── Layer 1: pHash ──
    phash_distance, phash_score = compare_phash(
        img1, img2
    )
    # Quick exit: nearly identical
    if phash_distance <= PHASH_SAME_THRESHOLD:
        return {
            "phash_distance": phash_distance,
            "phash_score": phash_score,
            "ssim": 1.0,
            "orb_score": 1.0,
            "orb_matches": 0,
            "combined_score": 1.0,
            "verdict": "SAME",
            "decided_by": "pHash"
        }
    # ── Layer 2: SSIM ──
    ssim_score = compare_ssim(img1, img2)
    # If pHash is borderline and SSIM is high → SAME
    if (
        phash_distance <= PHASH_MAYBE_THRESHOLD
        and ssim_score >= SSIM_THRESHOLD
    ):
        combined = (
            WEIGHT_PHASH * phash_score
            + WEIGHT_SSIM * ssim_score
            + WEIGHT_ORB * 1.0
        )
        return {
            "phash_distance": phash_distance,
            "phash_score": phash_score,
            "ssim": ssim_score,
            "orb_score": 1.0,
            "orb_matches": 0,
            "combined_score": combined,
            "verdict": "SAME",
            "decided_by": "pHash+SSIM"
        }
    # ── Layer 3: ORB ──
    orb_score, orb_matches = compare_orb(
        img1, img2
    )
    # ── Combined decision ──
    combined = (
        WEIGHT_PHASH * phash_score
        + WEIGHT_SSIM * ssim_score
        + WEIGHT_ORB * orb_score
    )
    if combined >= COMBINED_THRESHOLD:
        verdict = "SAME"
    elif orb_score >= 0.80:
        # Strong ORB match overrides
        verdict = "SAME"
    else:
        verdict = "DIFFERENT"
    return {
        "phash_distance": phash_distance,
        "phash_score": phash_score,
        "ssim": ssim_score,
        "orb_score": orb_score,
        "orb_matches": orb_matches,
        "combined_score": combined,
        "verdict": verdict,
        "decided_by": "combined"
    }
def resize_for_comparison(image, max_size=MAX_COMPARE_SIZE):
    """Resize an image if it exceeds max_size."""
    h, w = image.shape[:2]
    largest = max(h, w)
    if largest <= max_size:
        return image
    scale = max_size / float(largest)
    new_w = int(w * scale)
    new_h = int(h * scale)
    return cv2.resize(
        image,
        (new_w, new_h),
        interpolation=cv2.INTER_AREA
    )
# ============================================================
# SIDE-BY-SIDE IMAGE OUTPUT
# ============================================================
def create_side_by_side(image1, image2, verdict):
    """
    Create a side-by-side comparison image with
    labels and verdict.
    """
    # Resize both to same height
    h1, w1 = image1.shape[:2]
    h2, w2 = image2.shape[:2]
    target_h = max(h1, h2)
    # Cap height
    if target_h > 600:
        scale = 600.0 / target_h
        target_h = 600
    else:
        scale = 1.0
    new_w1 = int(w1 * (target_h / h1))
    new_w2 = int(w2 * (target_h / h2))
    img1 = cv2.resize(
        image1,
        (new_w1, target_h),
        interpolation=cv2.INTER_AREA
    )
    img2 = cv2.resize(
        image2,
        (new_w2, target_h),
        interpolation=cv2.INTER_AREA
    )
    # Separator
    sep_width = 4
    separator = np.ones(
        (target_h, sep_width, 3),
        dtype=np.uint8
    ) * 128
    # Header bar
    header_height = 40
    total_width = new_w1 + sep_width + new_w2
    if verdict == "SAME":
        header_color = (0, 150, 0)      # Green
    else:
        header_color = (0, 0, 200)      # Red
    header = np.ones(
        (header_height, total_width, 3),
        dtype=np.uint8
    ) * 240
    # Draw verdict text
    cv2.putText(
        header,
        f"ENGLISH",
        (10, 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 0, 0),
        2
    )
    cv2.putText(
        header,
        f"TRANSLATED",
        (new_w1 + sep_width + 10, 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 0, 0),
        2
    )
    verdict_text = f"  [{verdict}]"
    cv2.putText(
        header,
        verdict_text,
        (total_width // 2 - 60, 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        header_color,
        2
    )
    # Combine
    body = np.hstack([img1, separator, img2])
    combined = np.vstack([header, body])
    return combined
# ============================================================
# CSV OUTPUT
# ============================================================
def write_csv(results, output_path):
    """
    Write comparison results to CSV.
    Each row is one image pair.
    """
    fieldnames = [
        "English_PDF",
        "Translated_PDF",
        "Language",
        "Image_Order",
        "Type_EN",
        "Page_EN",
        "Index_EN",
        "Type_TR",
        "Page_TR",
        "Index_TR",
        "pHash_Distance",
        "pHash_Score",
        "SSIM",
        "ORB_Score",
        "ORB_Matches",
        "Combined_Score",
        "Decided_By",
        "Verdict"
    ]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames
        )
        writer.writeheader()
        for row in results:
            writer.writerow(row)
# ============================================================
# CONSOLE REPORTING
# ============================================================
def print_separator(char="=", width=75):
    print(char * width)
def print_image_result(order, img_en, img_tr, result):
    """Print result for a single image pair."""
    print(
        f"  Image #{order:03d}  "
        f"[{img_en.image_type}] "
        f"pg{img_en.page_number}/"
        f"#{img_en.image_index}"
        f"  ↔  "
        f"[{img_tr.image_type}] "
        f"pg{img_tr.page_number}/"
        f"#{img_tr.image_index}"
    )
    print(
        f"    pHash: {result['phash_distance']:2d} "
        f"({result['phash_score']:.3f})  "
        f"SSIM: {result['ssim']:.3f}  "
        f"ORB: {result['orb_score']:.3f} "
        f"({result['orb_matches']} matches)"
    )
    print(
        f"    Combined: {result['combined_score']:.3f}  "
        f"→  {result['verdict']}  "
        f"(by {result['decided_by']})"
    )
    print()
# ============================================================
# IMAGE MATCHING
# ============================================================
def match_images_by_similarity(
    en_images,
    tr_images,
    image_type
):
    """
    Match images by sequential order.
    When counts differ, uses pHash-based best-match
    to realign after a mismatch is detected.
    Returns list of (en_image, tr_image) tuples.
    """
    if not en_images or not tr_images:
        return []
    pairs = []
    # If counts match, use direct sequential matching
    if len(en_images) == len(tr_images):
        for i in range(len(en_images)):
            pairs.append(
                (en_images[i], tr_images[i])
            )
        return pairs
    # Counts differ — use greedy forward matching.
    #
    # Walk through the shorter list and for each image,
    # find the best match in a sliding window of the
    # longer list to handle insertions/deletions.
    shorter = en_images
    longer = tr_images
    en_is_shorter = True
    if len(en_images) > len(tr_images):
        shorter = tr_images
        longer = en_images
        en_is_shorter = False
    long_idx = 0
    window_size = 3  # Look ahead window
    for s_idx in range(len(shorter)):
        short_img = shorter[s_idx]
        # If we've exhausted the longer list, stop
        if long_idx >= len(longer):
            break
        best_dist = 999
        best_j = long_idx
        # Search within window
        end = min(
            long_idx + window_size,
            len(longer)
        )
        for j in range(long_idx, end):
            try:
                dist, _ = compare_phash(
                    short_img.image,
                    longer[j].image
                )
            except Exception:
                dist = 64
            if dist < best_dist:
                best_dist = dist
                best_j = j
        if en_is_shorter:
            pairs.append(
                (short_img, longer[best_j])
            )
        else:
            pairs.append(
                (longer[best_j], short_img)
            )
        long_idx = best_j + 1
    return pairs
# ============================================================
# MAIN COMPARISON WORKFLOW
# ============================================================
def compare_pdf_pair(
    english_path,
    translated_path,
    locale,
    output_base_dir,
    all_csv_rows
):
    """
    Compare all images between one English PDF and
    one translated PDF.
    """
    en_name = os.path.basename(english_path)
    tr_name = os.path.basename(translated_path)
    print()
    print_separator()
    print(f"  COMPARING: {locale}")
    print(f"  English   : {en_name}")
    print(f"  Translated: {tr_name}")
    print_separator()
    print()
    # ── Create output directories ──
    lang_dir = os.path.join(
        output_base_dir,
        "per_language",
        locale
    )
    pairs_dir = os.path.join(
        lang_dir,
        "image_pairs"
    )
    diff_dir = os.path.join(
        lang_dir,
        "different_only"
    )
    os.makedirs(pairs_dir, exist_ok=True)
    os.makedirs(diff_dir, exist_ok=True)
    # ── Extract images ──
    print("  Extracting images from English PDF...")
    en_images = extract_all_images(
        english_path
    )
    print(
        f"  Found {len(en_images)} images "
        f"in English PDF"
    )
    print("  Extracting images from Translated PDF...")
    tr_images = extract_all_images(
        translated_path
    )
    print(
        f"  Found {len(tr_images)} images "
        f"in Translated PDF"
    )
    print()
    # ── Separate by type ──
    en_raster = [
        img for img in en_images
        if img.image_type == "raster"
    ]
    en_vector = [
        img for img in en_images
        if img.image_type == "vector"
    ]
    tr_raster = [
        img for img in tr_images
        if img.image_type == "raster"
    ]
    tr_vector = [
        img for img in tr_images
        if img.image_type == "vector"
    ]
    print(
        f"  Raster: {len(en_raster)} EN, "
        f"{len(tr_raster)} TR"
    )
    print(
        f"  Vector: {len(en_vector)} EN, "
        f"{len(tr_vector)} TR"
    )
    print()
    print("  Vector graphics are extracted as individual")
    print("  meaningful regions; whole-page vector canvases")
    print("  are NOT used.")
    print()
    # ── Build matched pairs ──
    # Match raster and vector images separately
    # to prevent cross-type cascade misalignment.
    matched_pairs = []
    # Match raster images by order
    raster_pairs = match_images_by_similarity(
        en_raster, tr_raster, "raster"
    )
    matched_pairs.extend(raster_pairs)
    # Match vector images by order
    vector_pairs = match_images_by_similarity(
        en_vector, tr_vector, "vector"
    )
    matched_pairs.extend(vector_pairs)
    if not matched_pairs:
        print("  No images to compare!")
        return 0, 0, 0
    print(
        f"  Comparing {len(matched_pairs)} "
        f"image pairs..."
    )
    print()
    same_count = 0
    diff_count = 0
    for order_idx, (img_en, img_tr) in enumerate(
        matched_pairs
    ):
        order = order_idx + 1
        # ── Compare ──
        try:
            result = compare_images(
                img_en.image,
                img_tr.image
            )
        except Exception as e:
            print(
                f"  Image #{order:03d}: "
                f"ERROR - {str(e)}"
            )
            result = {
                "phash_distance": -1,
                "phash_score": 0.0,
                "ssim": 0.0,
                "orb_score": 0.0,
                "orb_matches": 0,
                "combined_score": 0.0,
                "verdict": "ERROR",
                "decided_by": "error"
            }
        # ── Print result ──
        print_image_result(
            order, img_en, img_tr, result
        )
        if result["verdict"] == "SAME":
            same_count += 1
        else:
            diff_count += 1
        # ── Save side-by-side ──
        try:
            sbs = create_side_by_side(
                img_en.image,
                img_tr.image,
                result["verdict"]
            )
            sbs_filename = (
                f"img_{order:03d}_"
                f"pg{img_en.page_number}_"
                f"{img_en.image_type}_"
                f"idx{img_en.image_index}"
                f"_side_by_side.png"
            )
            cv2.imwrite(
                os.path.join(
                    pairs_dir,
                    sbs_filename
                ),
                sbs
            )
            # Also save in different_only
            if result["verdict"] != "SAME":
                cv2.imwrite(
                    os.path.join(
                        diff_dir,
                        sbs_filename
                    ),
                    sbs
                )
        except Exception as e:
            print(
                f"    (Could not save side-by-side: "
                f"{str(e)})"
            )
        # ── CSV row ──
        all_csv_rows.append({
            "English_PDF": en_name,
            "Translated_PDF": tr_name,
            "Language": locale,
            "Image_Order": order,
            "Type_EN": img_en.image_type,
            "Page_EN": img_en.page_number,
            "Index_EN": img_en.image_index,
            "Type_TR": img_tr.image_type,
            "Page_TR": img_tr.page_number,
            "Index_TR": img_tr.image_index,
            "pHash_Distance": result["phash_distance"],
            "pHash_Score": f"{result['phash_score']:.4f}",
            "SSIM": f"{result['ssim']:.4f}",
            "ORB_Score": f"{result['orb_score']:.4f}",
            "ORB_Matches": result["orb_matches"],
            "Combined_Score": (
                f"{result['combined_score']:.4f}"
            ),
            "Decided_By": result["decided_by"],
            "Verdict": result["verdict"]
        })
    # ── Per-language summary ──
    paired_count = len(matched_pairs)
    print_separator("-")
    print(
        f"  {locale} SUMMARY: "
        f"{paired_count} compared, "
        f"{same_count} SAME, "
        f"{diff_count} DIFFERENT"
    )
    extra_en_r = max(0, len(en_raster) - len(tr_raster))
    extra_en_v = max(0, len(en_vector) - len(tr_vector))
    extra_tr_r = max(0, len(tr_raster) - len(en_raster))
    extra_tr_v = max(0, len(tr_vector) - len(en_vector))
    if extra_en_r + extra_en_v > 0:
        print(
            f"  + {extra_en_r + extra_en_v} unmatched "
            f"English images "
            f"({extra_en_r} raster, {extra_en_v} vector)"
        )
    if extra_tr_r + extra_tr_v > 0:
        print(
            f"  + {extra_tr_r + extra_tr_v} unmatched "
            f"Translated images "
            f"({extra_tr_r} raster, {extra_tr_v} vector)"
        )
    print()
    return paired_count, same_count, diff_count
# ============================================================
# MAIN
# ============================================================
def main():
    start_time = time.time()
    print()
    print_separator("=")
    print("  PDF IMAGE-BY-IMAGE COMPARATOR")
    print_separator("=")
    print()
    print(f"  English folder   : {ENGLISH_DIR}")
    print(f"  Translated folder: {TRANSLATED_DIR}")
    print(f"  Output folder    : {OUTPUT_DIR}")
    print()
    # ── Create output directory ──
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    # ── Find PDF pairs ──
    print("  Scanning for PDF pairs...")
    pairs = find_pdf_pairs(
        ENGLISH_DIR,
        TRANSLATED_DIR
    )
    if not pairs:
        print("  ERROR: No PDF pairs found!")
        return
    print(
        f"  Found {len(pairs)} "
        f"translated PDF(s) to compare"
    )
    for en, tr, loc in pairs:
        print(
            f"    {loc}: "
            f"{os.path.basename(tr)}"
        )
    print()
    # ── Process each pair ──
    all_csv_rows = []
    total_compared = 0
    total_same = 0
    total_different = 0
    for english_path, translated_path, locale in pairs:
        try:
            compared, same, different = (
                compare_pdf_pair(
                    english_path,
                    translated_path,
                    locale,
                    OUTPUT_DIR,
                    all_csv_rows
                )
            )
            total_compared += compared
            total_same += same
            total_different += different
        except Exception as e:
            print()
            print(
                f"  ERROR processing {locale}: "
                f"{str(e)}"
            )
            traceback.print_exc()
            print()
    # ── Write CSV ──
    csv_path = os.path.join(
        OUTPUT_DIR,
        "comparison_results.csv"
    )
    write_csv(all_csv_rows, csv_path)
    print(f"  CSV saved: {csv_path}")
    # ── Write summary ──
    elapsed = time.time() - start_time
    summary_path = os.path.join(
        OUTPUT_DIR,
        "summary.txt"
    )
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("PDF IMAGE COMPARISON SUMMARY\n")
        f.write("=" * 50 + "\n\n")
        f.write(
            f"English folder   : {ENGLISH_DIR}\n"
        )
        f.write(
            f"Translated folder: {TRANSLATED_DIR}\n"
        )
        f.write(
            f"Languages        : {len(pairs)}\n"
        )
        f.write(
            f"Total compared   : {total_compared}\n"
        )
        f.write(
            f"SAME             : {total_same}\n"
        )
        f.write(
            f"DIFFERENT        : {total_different}\n"
        )
        f.write(
            f"Elapsed          : {elapsed:.1f}s\n"
        )
    # ── Grand total ──
    print()
    print_separator("=")
    print("  GRAND TOTAL")
    print_separator("=")
    print()
    print(
        f"  Languages compared : {len(pairs)}"
    )
    print(
        f"  Image pairs checked: {total_compared}"
    )
    print(
        f"  SAME               : {total_same}"
    )
    print(
        f"  DIFFERENT          : {total_different}"
    )
    print(
        f"  Elapsed time       : {elapsed:.1f}s"
    )
    print()
    print(f"  Output: {OUTPUT_DIR}")
    print(f"  CSV   : {csv_path}")
    print()
    print_separator("=")
# ============================================================
# ENTRY POINT
# ============================================================
if __name__ == "__main__":
    main()