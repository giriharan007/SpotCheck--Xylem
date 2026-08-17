import pdfplumber
from PIL import Image
from pathlib import Path


# ============================================================
# CONFIGURATION
# ============================================================

PDF_PATH = r"C:\Xylem Project\SpotCheck\Input\English\894387_5.0_en-US_2026-04_IOM.Start350.pdf"
OUTPUT_DIR = r"C:\Xylem Project\SpotCheck\Output\Table images\English"

DPI = 100
PADDING = 3

BG_COLOR = (255, 255, 255)


# ============================================================
# DETECTION SETTINGS
# ============================================================

# Minimum dimensions of a box in PDF points
MIN_WIDTH = 80
MIN_HEIGHT = 25

# Minimum amount of text inside a candidate box
MIN_TEXT_LENGTH = 10

# Reject extremely large boxes
MAX_PAGE_AREA_RATIO = 0.70

# Header and Footer exclusion margin padding (in PDF points)
HEADER_MARGIN = 45  # Top 55 pt (header title / horizontal line area)
FOOTER_MARGIN = 50  # Bottom 50 pt (footer copyright / page number area)


# ============================================================
# UTILITY FUNCTIONS
# ============================================================

def is_in_header_or_footer(bbox, page_height, header_margin=HEADER_MARGIN, footer_margin=FOOTER_MARGIN):
    """
    Check if a bounding box falls inside top header or bottom footer space.
    """
    x0, top, x1, bottom = bbox

    # Check top header space
    if top < header_margin and bottom <= (header_margin + 20):
        return True

    # Check bottom footer space
    if bottom > (page_height - footer_margin) and top >= (page_height - footer_margin - 20):
        return True

    return False

def bbox_area(bbox):
    x0, top, x1, bottom = bbox
    return max(0, x1 - x0) * max(0, bottom - top)


def bbox_iou(box1, box2):
    """
    Intersection over Union.
    Used to remove duplicate/overlapping detections.
    """

    x0 = max(box1[0], box2[0])
    top = max(box1[1], box2[1])
    x1 = min(box1[2], box2[2])
    bottom = min(box1[3], box2[3])

    intersection = max(0, x1 - x0) * max(0, bottom - top)

    if intersection == 0:
        return 0

    area1 = bbox_area(box1)
    area2 = bbox_area(box2)

    union = area1 + area2 - intersection

    if union == 0:
        return 0

    return intersection / union


def get_text_in_bbox(page, bbox):
    """
    Extract text that falls inside a bounding box.
    """

    try:
        cropped = page.crop(bbox)
        text = cropped.extract_text()
        return text or ""
    except Exception:
        return ""


# ============================================================
# DETECT RECTANGULAR TEXT BOXES
# ============================================================

def detect_rectangular_text_boxes(page):
    """
    Detect vector rectangles containing meaningful text.

    This is useful for:
        - warning boxes
        - information boxes
        - bordered text boxes
        - simple tables
        - table-like regions

    It is intentionally different from pdfplumber.find_tables().
    """

    page_width = page.width
    page_height = page.height
    page_area = page_width * page_height

    candidates = []

    # --------------------------------------------------------
    # PDF vector rectangles
    # --------------------------------------------------------

    for rect in page.rects:

        x0 = rect["x0"]
        top = rect["top"]
        x1 = rect["x1"]
        bottom = rect["bottom"]

        width = x1 - x0
        height = bottom - top

        # Size filter: Check width
        if width < MIN_WIDTH:
            continue

        # Check for connecting vertical side lines extending downward (e.g. safety boxes)
        v_lines = [
            l for l in page.lines
            if (abs(l["x0"] - x0) < 5 or abs(l["x0"] - x1) < 5 or abs(l["x1"] - x0) < 5 or abs(l["x1"] - x1) < 5)
            and (abs(l["top"] - top) < 5 or abs(l["top"] - bottom) < 5)
            and l["bottom"] > bottom
        ]

        if v_lines:
            bottom = max(l["bottom"] for l in v_lines)
            height = bottom - top

        if height < MIN_HEIGHT:
            continue

        # Reject almost full-page rectangles
        area = width * height

        if area > page_area * MAX_PAGE_AREA_RATIO:
            continue

        # ----------------------------------------------------
        # Extract text inside rectangle
        # ----------------------------------------------------

        bbox = (x0, top, x1, bottom)

        # Skip header and footer space boxes
        if is_in_header_or_footer(bbox, page_height):
            continue

        text = get_text_in_bbox(page, bbox)

        clean_text = " ".join(
            text.split()
        )

        # Require meaningful text
        if len(clean_text) < MIN_TEXT_LENGTH:
            continue

        candidates.append({
            "bbox": bbox,
            "text": clean_text,
            "area": area,
            "type": "rectangle",
        })

    return candidates


# ============================================================
# DETECT PDFPLUMBER TABLES
# ============================================================

def detect_pdf_tables(page):
    """
    Keep the original table detector as an additional method.
    """

    settings = {
        "vertical_strategy": "lines",
        "horizontal_strategy": "lines",

        "edge_min_length": 15,

        "snap_tolerance": 3,
        "join_tolerance": 3,

        "intersection_tolerance": 3,
    }

    tables = page.find_tables( table_settings=settings)

    candidates = []

    for table in tables:

        bbox = table.bbox

        # Skip header and footer space boxes
        if is_in_header_or_footer(bbox, page.height):
            continue

        text = get_text_in_bbox(
            page,
            bbox
        )

        clean_text = " ".join(
            text.split()
        )

        if len(clean_text) < MIN_TEXT_LENGTH:
            continue

        candidates.append({
            "bbox": bbox,
            "text": clean_text,
            "area": bbox_area(bbox),
            "type": "table",
        })

    return candidates


# ============================================================
# REMOVE DUPLICATE DETECTIONS
# ============================================================

def remove_duplicate_boxes(candidates):

    # Largest boxes first
    candidates = sorted(
        candidates,
        key=lambda x: x["area"],
        reverse=True
    )

    selected = []

    for candidate in candidates:

        bbox = candidate["bbox"]

        duplicate = False

        for existing in selected:

            existing_bbox = existing["bbox"]

            iou = bbox_iou(
                bbox,
                existing_bbox
            )

            if iou > 0.80:

                duplicate = True
                break

        if not duplicate:
            selected.append(candidate)

    return selected


# ============================================================
# REMOVE SMALL NESTED BOXES
# ============================================================

def remove_nested_boxes(candidates):

    result = []

    for candidate in candidates:

        bbox = candidate["bbox"]

        is_nested = False

        for other in candidates:

            if candidate is other:
                continue

            other_bbox = other["bbox"]

            # Candidate completely inside another box
            if (
                bbox[0] >= other_bbox[0]
                and bbox[1] >= other_bbox[1]
                and bbox[2] <= other_bbox[2]
                and bbox[3] <= other_bbox[3]
            ):

                # Keep the larger containing region
                if bbox_area(other_bbox) > bbox_area(bbox) * 1.15:
                    is_nested = True
                    break

        if not is_nested:
            result.append(candidate)

    return result


# ============================================================
# FIND ALL TABLE-LIKE REGIONS
# ============================================================

def find_table_regions(page):

    candidates = []

    # --------------------------------------------------------
    # Method 1:
    # pdfplumber table detection
    # --------------------------------------------------------

    candidates.extend(
        detect_pdf_tables(page)
    )

    # --------------------------------------------------------
    # Method 2:
    # Vector rectangles containing text
    # --------------------------------------------------------

    candidates.extend(detect_rectangular_text_boxes(page))

    # --------------------------------------------------------
    # Remove duplicates
    # --------------------------------------------------------

    candidates = remove_duplicate_boxes(candidates)

    # --------------------------------------------------------
    # Remove small boxes inside larger boxes
    # --------------------------------------------------------

    candidates = remove_nested_boxes(candidates)

    # Sort top-to-bottom, left-to-right
    candidates.sort(
        key=lambda x: (
            x["bbox"][1],
            x["bbox"][0]
        )
    )

    return candidates


# ============================================================
# MASK PDF
# ============================================================

def mask_pdf_tables(pdf_path,out_dir,dpi=100,padding=3,bg_color=(255, 255, 255),):
    out_dir = Path(out_dir)
    out_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    scale = dpi / 72.0

    print("=" * 70)
    print("TABLE / TEXT-BOX EXTRACTION")
    print("=" * 70)

    print(f"PDF    : {pdf_path}")
    print(f"Output : {out_dir}")
    print(f"DPI    : {dpi}")
    print()

    with pdfplumber.open(pdf_path) as pdf:

        total_pages = len(pdf.pages)

        print(
            f"Total pages: {total_pages}"
        )
        print()

        for page_num, page in enumerate(
            pdf.pages,
            start=1
        ):

            # ------------------------------------------------
            # Detect regions
            # ------------------------------------------------

            regions = find_table_regions(
                page
            )

            # ------------------------------------------------
            # Render page
            # ------------------------------------------------

            page_image = (
                page
                .to_image(
                    resolution=dpi
                )
                .original
                .convert("RGB")
            )

            # ------------------------------------------------
            # White canvas
            # ------------------------------------------------

            masked = Image.new(
                "RGB",
                page_image.size,
                bg_color
            )

            # ------------------------------------------------
            # Copy detected regions
            # ------------------------------------------------

            for region in regions:

                x0, top, x1, bottom = (
                    region["bbox"]
                )

                px0 = max(
                    0,
                    int((x0 - padding) * scale)
                )

                ptop = max(
                    0,
                    int((top - padding) * scale)
                )

                px1 = min(
                    page_image.width,
                    int((x1 + padding) * scale)
                )

                pbottom = min(
                    page_image.height,
                    int((bottom + padding) * scale)
                )

                box = (
                    px0,
                    ptop,
                    px1,
                    pbottom
                )

                cropped = page_image.crop(
                    box
                )

                masked.paste(
                    cropped,
                    (px0, ptop)
                )

            # ------------------------------------------------
            # Save
            # ------------------------------------------------

            output_path = (
                out_dir /
                f"page_{page_num:03d}.png"
            )

            try:
                masked.save(
                    output_path
                )
            except (OSError, PermissionError) as e:
                print(
                    f"Warning: Could not save {output_path} (file locked or write error): {e}"
                )

            print(
                f"Page {page_num:03d}: "
                f"{len(regions)} region(s) -> "
                f"{output_path}"
            )

            # Print detection information
            for i, region in enumerate(
                regions,
                start=1
            ):

                bbox = region["bbox"]

                preview = region["text"][:80].encode('ascii', 'ignore').decode('ascii')

                print(
                    f"    Region {i}: "
                    f"{region['type']} "
                    f"{bbox} "
                    f"| {preview}"
                )

    print()
    print("=" * 70)
    print("COMPLETED")
    print("=" * 70)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    mask_pdf_tables(
        pdf_path=PDF_PATH,
        out_dir=OUTPUT_DIR,
        dpi=DPI,
        padding=PADDING,
        bg_color=BG_COLOR,
    )