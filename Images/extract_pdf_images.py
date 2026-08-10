import os
import sys
import hashlib
import traceback
from dataclasses import dataclass

import cv2
import numpy as np

try:
    import pymupdf
except ImportError:
    import fitz as pymupdf

# ============================================================
# CONFIGURATION
# ============================================================

INPUT_DIR = r"C:\Xylem Project\Spotcheck\Input\set1\Translated"
OUTPUT_DIR = r"C:\Xylem Project\Spotcheck\Input\set1\output"

RENDER_DPI = 600

# Minimum output image size in rendered pixels.
MIN_IMAGE_SIZE = 10

# Vector filtering.
MIN_VECTOR_WIDTH = 3
MIN_VECTOR_HEIGHT = 3
MIN_VECTOR_AREA = 20

# Distance used when joining nearby vector drawing objects.
# Keep this relatively small so unrelated graphics are not merged.
VECTOR_MERGE_GAP = 5

# Very thin lines are usually page separators/borders rather than graphics.
MAX_DECORATIVE_LINE_THICKNESS = 4

# Ignore a vector object if it occupies almost the entire page.
# This helps remove page backgrounds/frames.
MAX_PAGE_COVERAGE = 0.92

# Percentage of dark/non-white pixels required for a vector crop.
MEANINGFUL_GRAPHIC_THRESHOLD = 0.015

# Remove exact duplicate graphics on the same page.
DEDUP_PER_PAGE = True

IMAGE_EXTENSION = ".png"


# ============================================================
# DATA STRUCTURE
# ============================================================

@dataclass
class Graphic:
    image: np.ndarray
    page_number: int
    graphic_index: int
    graphic_type: str
    bbox: tuple
    xref: int = 0


# ============================================================
# CONSOLE
# ============================================================

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ============================================================
# PAGE RENDERING
# ============================================================

def render_page(page, dpi=RENDER_DPI):
    """Render a PDF page to a BGR OpenCV image."""

    zoom = dpi / 72.0

    matrix = pymupdf.Matrix(
        zoom,
        zoom
    )

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
# COORDINATE CONVERSION
# ============================================================

def pdf_rect_to_pixels(rect, page, rendered):
    """Convert a PDF rectangle from points to rendered pixels."""

    page_width = page.rect.width
    page_height = page.rect.height

    image_height, image_width = rendered.shape[:2]

    sx = image_width / page_width
    sy = image_height / page_height

    x0 = int(round(rect.x0 * sx))
    y0 = int(round(rect.y0 * sy))
    x1 = int(round(rect.x1 * sx))
    y1 = int(round(rect.y1 * sy))

    return (
        x0,
        y0,
        x1,
        y1
    )


def clip_bbox(bbox, image):
    """Clip a pixel bbox to the image."""

    x0, y0, x1, y1 = bbox

    height, width = image.shape[:2]

    x0 = max(0, min(width, x0))
    y0 = max(0, min(height, y0))
    x1 = max(0, min(width, x1))
    y1 = max(0, min(height, y1))

    return (
        x0,
        y0,
        x1,
        y1
    )


# ============================================================
# HASHING
# ============================================================

def image_hash(image):
    """Hash actual pixel content."""

    if image is None or image.size == 0:
        return None

    h = hashlib.sha256()

    h.update(
        str(image.shape).encode("utf-8")
    )

    h.update(
        str(image.dtype).encode("utf-8")
    )

    h.update(
        image.tobytes()
    )

    return h.hexdigest()


# ============================================================
# BBOX HELPERS
# ============================================================

def bbox_area(bbox):
    x0, y0, x1, y1 = bbox

    return max(
        0,
        x1 - x0
    ) * max(
        0,
        y1 - y0
    )


def bbox_iou(a, b):
    """Intersection over Union."""

    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b

    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)

    iw = max(
        0,
        ix1 - ix0
    )

    ih = max(
        0,
        iy1 - iy0
    )

    intersection = iw * ih

    if intersection == 0:
        return 0.0

    union = (
        bbox_area(a)
        + bbox_area(b)
        - intersection
    )

    if union <= 0:
        return 0.0

    return intersection / union


def boxes_close(a, b, gap=VECTOR_MERGE_GAP):
    """Return True when two boxes overlap or are close."""

    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b

    return not (
        ax1 + gap < bx0
        or bx1 + gap < ax0
        or ay1 + gap < by0
        or by1 + gap < ay0
    )


def merge_two_boxes(a, b):
    return (
        min(a[0], b[0]),
        min(a[1], b[1]),
        max(a[2], b[2]),
        max(a[3], b[3])
    )


# ============================================================
# VECTOR DRAWING FILTERS
# ============================================================

def drawing_is_decorative_line(
    drawing,
    pixel_bbox
):
    """
    Reject obvious page separators/borders.

    We deliberately do not reject all thin drawings because warning
    symbols and logos can contain thin lines.
    """

    rect = drawing.get("rect")

    if rect is None:
        return True

    width = abs(rect.width)
    height = abs(rect.height)

    x0, y0, x1, y1 = pixel_bbox

    pixel_width = x1 - x0
    pixel_height = y1 - y0

    if pixel_width <= 0 or pixel_height <= 0:
        return True

    # A long, extremely thin line is usually decoration.
    if (
        pixel_width > 500
        and pixel_height <= MAX_DECORATIVE_LINE_THICKNESS
    ):
        return True

    if (
        pixel_height > 500
        and pixel_width <= MAX_DECORATIVE_LINE_THICKNESS
    ):
        return True

    return False


def is_page_frame_bbox(
    bbox,
    rendered
):
    """
    Reject very large regions that are likely page backgrounds/frames.
    """

    image_height, image_width = rendered.shape[:2]

    area = bbox_area(bbox)
    page_area = image_width * image_height

    if page_area <= 0:
        return True

    coverage = area / page_area

    if coverage >= MAX_PAGE_COVERAGE:
        return True

    return False


def is_meaningful_graphic(
    crop,
    threshold=MEANINGFUL_GRAPHIC_THRESHOLD
):
    """
    Check whether the crop contains actual visible artwork.

    Text is not specifically detected here. Text is not obtained from
    get_drawings(), so normal PDF text does not enter the vector list.
    """

    if crop is None or crop.size == 0:
        return False

    gray = cv2.cvtColor(
        crop,
        cv2.COLOR_BGR2GRAY
    )

    # Count pixels that are meaningfully darker than white.
    non_white = np.count_nonzero(
        gray < 245
    )

    total = gray.size

    if total == 0:
        return False

    ratio = non_white / total

    return ratio >= threshold


# ============================================================
# VECTOR REGION MERGING
# ============================================================

def merge_vector_regions(regions):
    """
    Group vector drawing bounding boxes into graphics.

    The grouping is intentionally conservative.

    A component is merged only when its bounding box overlaps or is
    within VECTOR_MERGE_GAP pixels of another component.

    This allows a logo made from several vector paths to become one
    object without turning an entire page header into one object.
    """

    if not regions:
        return []

    regions = [
        tuple(r)
        for r in regions
    ]

    changed = True

    while changed:

        changed = False
        result = []

        while regions:

            current = regions.pop(0)

            found_merge = True

            while found_merge:

                found_merge = False

                for index, other in enumerate(regions):

                    if boxes_close(
                        current,
                        other,
                        VECTOR_MERGE_GAP
                    ):
                        current = merge_two_boxes(
                            current,
                            other
                        )

                        regions.pop(index)

                        found_merge = True
                        changed = True

                        break

            result.append(current)

        regions = result

    return regions


# ============================================================
# RASTER EXTRACTION
# ============================================================

def extract_raster_images(
    page,
    rendered,
    doc,
    seen_hashes
):
    """
    Extract visible embedded raster images.

    Important:
        get_image_info() reports displayed image blocks and their
        bounding boxes. This is preferred over blindly extracting
        every image resource returned by get_images().
    """

    results = []

    try:
        image_infos = page.get_image_info(
            xrefs=True
        )
    except Exception:

        # Compatibility fallback for older PyMuPDF versions.
        image_infos = []

        try:
            for info in page.get_images(
                full=True
            ):

                xref = info[0]

                rects = page.get_image_rects(
                    info
                )

                for rect in rects:

                    image_infos.append({
                        "xref": xref,
                        "bbox": (
                            rect.x0,
                            rect.y0,
                            rect.x1,
                            rect.y1
                        )
                    })

        except Exception:
            return results

    page_rect = page.rect

    for info in image_infos:

        xref = int(
            info.get(
                "xref",
                0
            )
        )

        raw_bbox = info.get(
            "bbox"
        )

        if not raw_bbox:
            continue

        try:
            rect = pymupdf.Rect(
                raw_bbox
            )
        except Exception:
            continue

        # ----------------------------------------------------
        # VISIBLE PAGE CHECK
        # ----------------------------------------------------

        intersection = rect & page_rect

        if (
            intersection.is_empty
            or intersection.width <= 0
            or intersection.height <= 0
        ):
            # Completely outside the visible page.
            continue

        bbox = pdf_rect_to_pixels(
            intersection,
            page,
            rendered
        )

        bbox = clip_bbox(
            bbox,
            rendered
        )

        x0, y0, x1, y1 = bbox

        if (
            x1 - x0 < MIN_IMAGE_SIZE
            or y1 - y0 < MIN_IMAGE_SIZE
        ):
            continue

        cv_image = None

        # ----------------------------------------------------
        # ORIGINAL EMBEDDED IMAGE
        # ----------------------------------------------------

        if xref > 0:

            try:

                extracted = doc.extract_image(
                    xref
                )

                raw_bytes = extracted.get(
                    "image"
                )

                if raw_bytes:

                    array = np.frombuffer(
                        raw_bytes,
                        dtype=np.uint8
                    )

                    decoded = cv2.imdecode(
                        array,
                        cv2.IMREAD_COLOR
                    )

                    if decoded is not None:
                        cv_image = decoded

            except Exception:
                cv_image = None

        # ----------------------------------------------------
        # FALLBACK TO VISIBLE RENDERED CROP
        # ----------------------------------------------------

        if cv_image is None:

            cv_image = rendered[
                y0:y1,
                x0:x1
            ].copy()

        if (
            cv_image is None
            or cv_image.size == 0
        ):
            continue

        # ----------------------------------------------------
        # DEDUP SAME PAGE
        # ----------------------------------------------------

        if DEDUP_PER_PAGE:

            content_hash = image_hash(
                cv_image
            )

            # Include xref only as a secondary protection.
            # The pixel hash is what actually removes repeated
            # identical images.
            if content_hash in seen_hashes:
                continue

            seen_hashes.add(
                content_hash
            )

        results.append({
            "image": cv_image,
            "bbox": bbox,
            "xref": xref
        })

    return results


# ============================================================
# VECTOR EXTRACTION
# ============================================================

def extract_vector_images(
    page,
    rendered,
    seen_hashes
):
    """
    Extract vector graphics only.

    Text is ignored because only page.get_drawings() is used.

    The gray rectangle surrounding an EN label is therefore extracted
    if it is a vector drawing, while the actual EN letters are ignored.
    """

    raw_regions = []

    try:
        drawings = page.get_drawings()
    except Exception:
        return []

    for drawing in drawings:

        rect = drawing.get(
            "rect"
        )

        if rect is None:
            continue

        bbox = pdf_rect_to_pixels(
            rect,
            page,
            rendered
        )

        bbox = clip_bbox(
            bbox,
            rendered
        )

        x0, y0, x1, y1 = bbox

        width = x1 - x0
        height = y1 - y0

        if width < MIN_VECTOR_WIDTH:
            continue

        if height < MIN_VECTOR_HEIGHT:
            continue

        if width * height < MIN_VECTOR_AREA:
            continue

        if drawing_is_decorative_line(
            drawing,
            bbox
        ):
            continue

        if is_page_frame_bbox(
            bbox,
            rendered
        ):
            continue

        raw_regions.append(
            bbox
        )

    if not raw_regions:
        return []

    # --------------------------------------------------------
    # MERGE DRAWING COMPONENTS
    # --------------------------------------------------------

    merged = merge_vector_regions(
        raw_regions
    )

    results = []

    for bbox in merged:

        bbox = clip_bbox(
            bbox,
            rendered
        )

        x0, y0, x1, y1 = bbox

        if (
            x1 <= x0
            or y1 <= y0
        ):
            continue

        crop = rendered[
            y0:y1,
            x0:x1
        ].copy()

        if (
            crop is None
            or crop.size == 0
        ):
            continue

        if not is_meaningful_graphic(
            crop
        ):
            continue

        # ----------------------------------------------------
        # REMOVE DUPLICATE VECTOR REGIONS
        # ----------------------------------------------------

        if DEDUP_PER_PAGE:

            content_hash = image_hash(
                crop
            )

            if content_hash in seen_hashes:
                continue

            seen_hashes.add(
                content_hash
            )

        results.append({
            "image": crop,
            "bbox": bbox,
            "xref": 0
        })

    return results


# ============================================================
# SAVE IMAGE
# ============================================================

def save_image(
    image,
    path
):
    """Save an OpenCV image."""

    os.makedirs(
        os.path.dirname(path),
        exist_ok=True
    )

    success = cv2.imwrite(
        path,
        image
    )

    if not success:
        raise RuntimeError(
            f"Could not write image: {path}"
        )


# ============================================================
# EXTRACT ONE PDF
# ============================================================

def extract_pdf(
    pdf_path,
    output_root
):
    """
    Extract raster and vector graphics from one PDF.
    """

    pdf_name = os.path.splitext(
        os.path.basename(pdf_path)
    )[0]

    pdf_output = os.path.join(
        output_root,
        pdf_name
    )

    raster_output = os.path.join(
        pdf_output,
        "raster"
    )

    vector_output = os.path.join(
        pdf_output,
        "vector"
    )

    os.makedirs(
        raster_output,
        exist_ok=True
    )

    os.makedirs(
        vector_output,
        exist_ok=True
    )

    doc = pymupdf.open(
        pdf_path
    )

    total_pages = len(doc)

    total_raster = 0
    total_vector = 0

    page_summary = []

    try:

        for page_index in range(
            total_pages
        ):

            page = doc[
                page_index
            ]

            page_number = (
                page_index + 1
            )

            print(
                f"    Page "
                f"{page_number}/"
                f"{total_pages}..."
            )

            rendered = render_page(
                page
            )

            # One deduplication set PER PAGE.
            # The same graphic appearing on another page is preserved.
            seen_hashes = set()

            # =================================================
            # RASTER
            # =================================================

            raster_images = extract_raster_images(
                page,
                rendered,
                doc,
                seen_hashes
            )

            page_raster_count = 0

            for item in raster_images:

                page_raster_count += 1
                total_raster += 1

                filename = (
                    f"page_{page_number:03d}_"
                    f"raster_{page_raster_count:03d}"
                    f"{IMAGE_EXTENSION}"
                )

                path = os.path.join(
                    raster_output,
                    filename
                )

                save_image(
                    item["image"],
                    path
                )

            # =================================================
            # VECTOR
            # =================================================

            vector_images = extract_vector_images(
                page,
                rendered,
                seen_hashes
            )

            page_vector_count = 0

            for item in vector_images:

                page_vector_count += 1
                total_vector += 1

                filename = (
                    f"page_{page_number:03d}_"
                    f"vector_{page_vector_count:03d}"
                    f"{IMAGE_EXTENSION}"
                )

                path = os.path.join(
                    vector_output,
                    filename
                )

                save_image(
                    item["image"],
                    path
                )

            page_summary.append({
                "page": page_number,
                "raster": page_raster_count,
                "vector": page_vector_count
            })

            print(
                f"        Raster: "
                f"{page_raster_count}"
            )

            print(
                f"        Vector: "
                f"{page_vector_count}"
            )

    finally:
        doc.close()

    # ========================================================
    # SUMMARY
    # ========================================================

    summary_path = os.path.join(
        pdf_output,
        "extraction_summary.txt"
    )

    with open(
        summary_path,
        "w",
        encoding="utf-8"
    ) as f:

        f.write(
            "PDF GRAPHIC EXTRACTION SUMMARY\n"
        )

        f.write(
            "=" * 70
            + "\n\n"
        )

        f.write(
            f"PDF: "
            f"{os.path.basename(pdf_path)}\n"
        )

        f.write(
            f"Pages: "
            f"{total_pages}\n"
        )

        f.write(
            f"Raster graphics: "
            f"{total_raster}\n"
        )

        f.write(
            f"Vector graphics: "
            f"{total_vector}\n"
        )

        f.write(
            f"Total graphics: "
            f"{total_raster + total_vector}\n"
        )

        f.write(
            "\nPAGE DETAILS\n"
        )

        f.write(
            "-" * 70
            + "\n"
        )

        for row in page_summary:

            f.write(
                f"Page "
                f"{row['page']:03d}: "
                f"Raster="
                f"{row['raster']}, "
                f"Vector="
                f"{row['vector']}\n"
            )

    return {
        "pdf": os.path.basename(pdf_path),
        "pages": total_pages,
        "raster": total_raster,
        "vector": total_vector,
        "total": (
            total_raster
            + total_vector
        )
    }


# ============================================================
# FIND PDF FILES
# ============================================================

def find_pdfs(
    input_dir
):
    """Return all PDF files in the input directory."""

    if not os.path.isdir(
        input_dir
    ):
        return []

    files = []

    for filename in os.listdir(
        input_dir
    ):

        if filename.lower().endswith(
            ".pdf"
        ):

            files.append(
                os.path.join(
                    input_dir,
                    filename
                )
            )

    return sorted(
        files
    )


# ============================================================
# MAIN
# ============================================================

def main():

    print()
    print("=" * 70)
    print("PDF GRAPHIC EXTRACTOR")
    print("=" * 70)
    print()

    print(
        f"Input : {INPUT_DIR}"
    )

    print(
        f"Output: {OUTPUT_DIR}"
    )

    print()

    os.makedirs(
        OUTPUT_DIR,
        exist_ok=True
    )

    pdf_files = find_pdfs(
        INPUT_DIR
    )

    if not pdf_files:

        print(
            "No PDF files found."
        )

        print(
            f"Check INPUT_DIR: "
            f"{INPUT_DIR}"
        )

        return

    print(
        f"Found "
        f"{len(pdf_files)} "
        f"PDF file(s)."
    )

    print()

    results = []

    for index, pdf_path in enumerate(
        pdf_files,
        start=1
    ):

        print("=" * 70)

        print(
            f"[{index}/{len(pdf_files)}] "
            f"{os.path.basename(pdf_path)}"
        )

        print("=" * 70)

        try:

            result = extract_pdf(
                pdf_path,
                OUTPUT_DIR
            )

            results.append(
                result
            )

            print()
            print(
                f"  Raster: "
                f"{result['raster']}"
            )

            print(
                f"  Vector: "
                f"{result['vector']}"
            )

            print(
                f"  Total : "
                f"{result['total']}"
            )

            print()

        except Exception as exc:

            print()
            print(
                "ERROR:"
            )

            print(
                str(exc)
            )

            traceback.print_exc()

            print()

    # ========================================================
    # FINAL SUMMARY
    # ========================================================

    raster_total = sum(
        item["raster"]
        for item in results
    )

    vector_total = sum(
        item["vector"]
        for item in results
    )

    print("=" * 70)
    print("EXTRACTION COMPLETE")
    print("=" * 70)

    print(
        f"PDFs processed : "
        f"{len(results)}"
    )

    print(
        f"Raster images  : "
        f"{raster_total}"
    )

    print(
        f"Vector graphics: "
        f"{vector_total}"
    )

    print(
        f"Total graphics : "
        f"{raster_total + vector_total}"
    )

    print()

    print(
        f"Output folder: "
        f"{OUTPUT_DIR}"
    )

    print()


if __name__ == "__main__":
    main()