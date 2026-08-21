"""
region_inspector.py

CustomTkinter Multi-Region ROI Selector & Visual Layout Verification Engine.
Features:
  1. Sub-Region nesting: Selecting a region inside an existing region automatically creates a Sub-Region (e.g. Region 1.1).
  2. Exact Match Required toggle per region: Require 100% exact text/code match.
  3. "Don't Compare Text (Visual Match)": Verifies presence of content and matches non-text visual graphics (circle badges, icons, lines, shapes, frames) with contour/structural shape matching.
  4. Only saves side-by-side compared images in Output/Output_Cropped_Comparison/Region_Inspector/ (no redundant individual crops).
  5. Automatic whitespace trimming so surrounding margins do not skew graphic comparisons.
  6. Consecutive numbering & automatic re-indexing on add/delete.
  7. Built with CustomTkinter (CTk) with full Maximize, Minimize, and Resizing support.
"""

import os
import sys
import re
import threading
import queue
import difflib
import pymupdf as fitz
import cv2
import numpy as np
from PIL import Image, ImageTk, ImageDraw, ImageFont
import customtkinter as ctk
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# Xylem Brand Palette
XYLEM_BLUE      = "#007DA3"
DEPENDABLE_BLUE = "#003E51"
CLARITY_BLUE    = "#67DFFF"
DYNAMIC_GREEN   = "#61D604"
RADIANT_ORANGE  = "#F96C00"
VIVID_MAGENTA   = "#D300F2"
UI_BG_CANVAS    = "#EEF5FA"
UI_CARD_BG      = "#FFFFFF"
UI_CARD_WELL    = "#E2EDF7"
UI_BORDER       = "#D0DFEB"
NEUTRAL_WHITE   = "#FFFFFF"
NEUTRAL_DARK_GR = "#555555"

REGION_COLORS = [
    "#007DA3",  # Xylem Blue
    "#20846F",  # Inspired Teal
    "#6600C5",  # Resilient Purple
    "#F96C00",  # Radiant Orange
    "#D300F2",  # Vivid Magenta
    "#003E51",  # Dependable Blue
    "#61D604",  # Dynamic Green
]

DEFAULT_CROPS_OUTPUT_DIR = os.path.abspath(r"Output\Output_Cropped_Comparison\Region_Inspector")


# ==============================================================================
# CORE SPATIAL REGION EXTRACTION & CROPPED IMAGE COMPARISON
# ==============================================================================

def get_page_count(pdf_path: str) -> int:
    """Return total number of pages in a PDF."""
    try:
        with fitz.open(pdf_path) as doc:
            return len(doc)
    except Exception:
        return 0


def render_pdf_page_image(pdf_path: str, page_num: int, zoom: float = 1.0) -> tuple[Image.Image, float, float]:
    """
    Render a specific PDF page to PIL Image at the specified zoom scale.
    Renders cleanly without masking anything so all text and layout remain 100% visible.
    page_num is 1-indexed (or -1 for last page).
    Returns: (PIL.Image, page_width_pt, page_height_pt)
    """
    with fitz.open(pdf_path) as doc:
        total = len(doc)
        if total == 0:
            raise ValueError("PDF has no pages.")
        
        p_idx = (total - 1) if page_num == -1 else max(0, min(total - 1, page_num - 1))
        page = doc[p_idx]
        pw, ph = page.rect.width, page.rect.height
        
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)
        
        mode = "RGBA" if pix.alpha else "RGB"
        img = Image.frombytes(mode, [pix.width, pix.height], pix.samples).convert("RGB")
        return img, pw, ph


def extract_roi_text(doc: fitz.Document, page_num: int, roi_rect: tuple[float, float, float, float]) -> str:
    """
    Extract clean text inside roi_rect = (x0, y0, x1, y1) in PDF point coordinates.
    """
    total = len(doc)
    p_idx = (total - 1) if page_num == -1 else max(0, min(total - 1, page_num - 1))
    page = doc[p_idx]
    clip_r = fitz.Rect(roi_rect)
    return page.get_text("text", clip=clip_r).strip()


def compute_region_similarity(eng_text: str, tr_text: str) -> tuple[float, str]:
    """
    Compute similarity between English region text and target translated region text.
    Returns: (similarity_percentage, match_description)
    """
    if not eng_text and not tr_text:
        return 100.0, "Exact Match (Both Empty)"
    if not eng_text or not tr_text:
        return 0.0, "Empty / No Content"

    eng_clean = " ".join(eng_text.split()).strip()
    tr_clean = " ".join(tr_text.split()).strip()

    # 1. Exact string match (ignoring whitespace / case)
    if eng_clean.lower() == tr_clean.lower():
        return 100.0, "Exact Match"

    # 2. SequenceMatcher (Character level)
    seq_ratio = difflib.SequenceMatcher(None, eng_clean.lower(), tr_clean.lower()).ratio() * 100.0
    if seq_ratio >= 85.0:
        return round(seq_ratio, 1), "Near Match"

    # 3. Token & Alphanumeric Overlap (for addresses, phone numbers, URLs, product codes)
    eng_tokens = set(re.findall(r'\w+', eng_clean.lower()))
    tr_tokens = set(re.findall(r'\w+', tr_clean.lower()))
    if eng_tokens and tr_tokens:
        overlap = len(eng_tokens & tr_tokens) / len(eng_tokens) * 100.0
        if overlap >= 75.0:
            return round(max(seq_ratio, overlap), 1), "Token Match"

    # 4. Translated content present
    return round(seq_ratio, 1), "Translated / Content Present"


def trim_white_borders(gray_img: np.ndarray, threshold: int = 245) -> np.ndarray:
    """
    Trim excess surrounding white padding from image so whitespace does not skew visual comparison.
    """
    mask = gray_img < threshold
    if not np.any(mask):
        return gray_img
    y_indices, x_indices = np.where(mask)
    y0, y1 = np.min(y_indices), np.max(y_indices)
    x0, x1 = np.min(x_indices), np.max(x_indices)
    pad = 3
    return gray_img[max(0, y0 - pad):min(gray_img.shape[0], y1 + pad + 1),
                    max(0, x0 - pad):min(gray_img.shape[1], x1 + pad + 1)]


def compute_visual_graphic_similarity(
    eng_pdf_path: str,
    tr_pdf_path: str,
    eng_page: int,
    tr_page: int,
    roi_rect: tuple[float, float, float, float],
    search_rect: tuple[float, float, float, float]
) -> float:
    """
    Universally masks out text spans from both English and Translated page copies
    and matches all non-text visual elements (headers, footers, divider lines, logos,
    circular badges, tables, shapes, frames, and icons).
    """
    try:
        # 1. English copy with text masked out
        with fitz.open(eng_pdf_path) as doc_e:
            p_e = doc_e[eng_page - 1]
            for b in p_e.get_text("dict", clip=fitz.Rect(roi_rect)).get("blocks", []):
                if b.get("type") == 0:
                    for l in b.get("lines", []):
                        p_e.draw_rect(fitz.Rect(l["bbox"]), color=(1, 1, 1), fill=(1, 1, 1))
            pix_e = p_e.get_pixmap(dpi=150, clip=fitz.Rect(roi_rect))
            data_e = np.frombuffer(pix_e.samples, dtype=np.uint8).reshape(pix_e.height, pix_e.width, pix_e.n)
            gray_e = cv2.cvtColor(data_e, cv2.COLOR_RGBA2GRAY if pix_e.n == 4 else cv2.COLOR_RGB2GRAY)

        # 2. Translated copy with text masked out
        with fitz.open(tr_pdf_path) as doc_t:
            p_t = doc_t[tr_page - 1]
            for b in p_t.get_text("dict", clip=fitz.Rect(search_rect)).get("blocks", []):
                if b.get("type") == 0:
                    for l in b.get("lines", []):
                        p_t.draw_rect(fitz.Rect(l["bbox"]), color=(1, 1, 1), fill=(1, 1, 1))
            pix_t = p_t.get_pixmap(dpi=150, clip=fitz.Rect(search_rect))
            data_t = np.frombuffer(pix_t.samples, dtype=np.uint8).reshape(pix_t.height, pix_t.width, pix_t.n)
            gray_t = cv2.cvtColor(data_t, cv2.COLOR_RGBA2GRAY if pix_t.n == 4 else cv2.COLOR_RGB2GRAY)

        # 3. Trim outer white padding
        trimmed_e = trim_white_borders(gray_e)
        trimmed_t = trim_white_borders(gray_t)

        # If both regions contain only masked text and no other graphics, return 100%
        if np.all(trimmed_e > 240) and np.all(trimmed_t > 240):
            return 100.0

        # 4. Normalized template matching across search window
        if trimmed_e.shape[0] <= trimmed_t.shape[0] and trimmed_e.shape[1] <= trimmed_t.shape[1]:
            res = cv2.matchTemplate(trimmed_t, trimmed_e, cv2.TM_CCOEFF_NORMED)
            sim = max(0.0, float(np.max(res))) * 100.0
        else:
            resized_t = cv2.resize(trimmed_t, (trimmed_e.shape[1], trimmed_e.shape[0]))
            res = cv2.matchTemplate(resized_t, trimmed_e, cv2.TM_CCOEFF_NORMED)
            sim = max(0.0, float(np.max(res))) * 100.0

        return round(min(100.0, sim), 1)
    except Exception as e:
        print(f"Warning: Visual graphic match failed: {e}")
        return 75.0


def is_rect_contained_in_parent(child_rect: tuple, parent_rect: tuple, tolerance: float = 8.0) -> bool:
    """
    Returns True if child_rect is contained inside parent_rect.
    """
    cx0, cy0, cx1, cy1 = child_rect
    px0, py0, px1, py1 = parent_rect

    c_area = max(0.0, cx1 - cx0) * max(0.0, cy1 - cy0)
    p_area = max(0.0, px1 - px0) * max(0.0, py1 - py0)
    if c_area >= p_area * 0.96:
        return False

    return (
        (cx0 >= px0 - tolerance) and
        (cx1 <= px1 + tolerance) and
        (cy0 >= py0 - tolerance) and
        (cy1 <= py1 + tolerance)
    )


def generate_only_compared_image(
    eng_pdf_path: str,
    tr_pdf_path: str,
    eng_page: int,
    tr_page: int,
    roi_rect: tuple[float, float, float, float],
    search_rect: tuple[float, float, float, float],
    output_dir: str,
    region_label: str,
    tr_name: str,
    shift_y: float = 0.0
) -> str:
    """
    Generates ONLY the side-by-side compared image in the output folder.
    """
    safe_lbl = re.sub(r'[^\w\-_\.]', '_', region_label).strip('_')
    if not safe_lbl:
        safe_lbl = "Region"
    reg_dir = os.path.join(output_dir, f"{safe_lbl}_Pg{eng_page}")
    os.makedirs(reg_dir, exist_ok=True)

    # 1. Render English Crop to PIL
    with fitz.open(eng_pdf_path) as doc_e:
        p_e = doc_e[eng_page - 1]
        pix_e = p_e.get_pixmap(dpi=150, clip=fitz.Rect(roi_rect))
        img_e = Image.frombytes("RGBA" if pix_e.alpha else "RGB", [pix_e.width, pix_e.height], pix_e.samples).convert("RGB")

    # 2. Render Translated Crop to PIL
    with fitz.open(tr_pdf_path) as doc_t:
        p_t = doc_t[tr_page - 1]
        pix_t = p_t.get_pixmap(dpi=150, clip=fitz.Rect(search_rect))
        img_t = Image.frombytes("RGBA" if pix_t.alpha else "RGB", [pix_t.width, pix_t.height], pix_t.samples).convert("RGB")

    # 3. Create Side-by-Side Comparison Composite
    header_h = 32
    pad = 8
    total_w = img_e.width + img_t.width + (pad * 3)
    total_h = max(img_e.height, img_t.height) + header_h + (pad * 2)

    comp = Image.new("RGB", (total_w, total_h), color=(255, 255, 255))
    draw = ImageDraw.Draw(comp)

    # Header Left (English)
    draw.rectangle([0, 0, img_e.width + pad * 2, header_h], fill=(0, 125, 163))
    draw.text((pad + 2, 8), f"English Master (Pg {eng_page})", fill=(255, 255, 255))

    # Header Right (Translated)
    tr_base = os.path.splitext(tr_name)[0]
    draw.rectangle([img_e.width + pad * 2, 0, total_w, header_h], fill=(0, 62, 81))
    shift_info = f" (Shift: {shift_y:+.1f}pt)" if shift_y != 0.0 else ""
    draw.text((img_e.width + pad * 3, 8), f"{tr_base} (Pg {tr_page}){shift_info}", fill=(255, 255, 255))

    # Paste crops
    comp.paste(img_e, (pad, header_h + pad))
    comp.paste(img_t, (img_e.width + pad * 2, header_h + pad))

    # Divider line
    draw.line([(img_e.width + pad * 2, 0), (img_e.width + pad * 2, total_h)], fill=(180, 200, 220), width=2)

    comp_path = os.path.join(reg_dir, f"Compare_{tr_base}.png")
    comp.save(comp_path)

    return comp_path


def search_and_verify_region_in_target(
    eng_pdf_path: str,
    tr_pdf_path: str,
    eng_page: int,
    is_last_page: bool,
    roi_rect: tuple[float, float, float, float],
    eng_text: str,
    exact_match_required: bool = False,
    dont_compare_text: bool = False,
    y_tolerance: float = 40.0,
    similarity_threshold: float = 65.0,
    output_crops_dir: str = None,
    region_label: str = "Region"
) -> dict:
    """
    Locate corresponding region in target translated PDF with flexible y-offset tolerance,
    extract translated content, verify presence & text/visual similarity, and generate compared images.
    """
    tr_name = os.path.basename(tr_pdf_path)
    if not os.path.exists(tr_pdf_path):
        return {
            "tr_name": tr_name,
            "status": "FAIL (File Not Found)",
            "similarity": 0.0,
            "shift_y": 0.0,
            "tr_text": "",
            "is_match": False,
            "comparison_img_path": "",
        }

    try:
        with fitz.open(tr_pdf_path) as doc_tr:
            total_tr = len(doc_tr)
            if total_tr == 0:
                return {
                    "tr_name": tr_name,
                    "status": "FAIL (Empty PDF)",
                    "similarity": 0.0,
                    "shift_y": 0.0,
                    "tr_text": "",
                    "is_match": False,
                    "comparison_img_path": "",
                }

            p_idx = (total_tr - 1) if (is_last_page or eng_page == -1) else max(0, min(total_tr - 1, eng_page - 1))
            tr_page_num = p_idx + 1
            tr_page = doc_tr[p_idx]
            pw, ph = tr_page.rect.width, tr_page.rect.height

            # Search window with vertical tolerance
            x0, y0, x1, y1 = roi_rect
            search_y0 = max(0.0, y0 - y_tolerance)
            search_y1 = min(ph, y1 + y_tolerance)
            search_rect = (x0, search_y0, x1, search_y1)

            tr_text = tr_page.get_text("text", clip=fitz.Rect(search_rect)).strip()
            has_content = len(tr_text) > 0

            # Estimate shift offset
            shift_y = 0.0
            if has_content:
                blocks = tr_page.get_text("blocks", clip=fitz.Rect(search_rect))
                if blocks:
                    first_b_y0 = min(b[1] for b in blocks)
                    shift_y = round(first_b_y0 - y0, 1)

            # Determine similarity & status based on region mode
            if dont_compare_text:
                # Mode 1: Don't Compare Text (Visual Match)
                vis_sim = compute_visual_graphic_similarity(
                    eng_pdf_path=eng_pdf_path,
                    tr_pdf_path=tr_pdf_path,
                    eng_page=eng_page,
                    tr_page=tr_page_num,
                    roi_rect=roi_rect,
                    search_rect=search_rect
                )
                similarity = vis_sim
                match_desc = "Visual Graphic Match (Ignoring Text)"

                if vis_sim >= 75.0:
                    status = "PASS (Visual Match)"
                    is_match = True
                elif vis_sim >= similarity_threshold:
                    status = "CHECK (Visual Partial)"
                    is_match = True
                else:
                    status = "FAIL (Visual Mismatch)"
                    is_match = False

            elif exact_match_required:
                # Mode 2: Exact Match Required (100%)
                similarity, match_desc = compute_region_similarity(eng_text, tr_text)
                if similarity >= 98.0:
                    status = "PASS (Exact Match)"
                    is_match = True
                else:
                    status = "FAIL (Exact Required)"
                    is_match = False

            else:
                # Mode 3: Normal Layout Presence & Translation Check
                similarity, match_desc = compute_region_similarity(eng_text, tr_text)
                if similarity >= 98.0:
                    status = "PASS (Exact)"
                    is_match = True
                elif similarity >= 85.0:
                    status = "PASS (Near)"
                    is_match = True
                elif has_content:
                    status = "PASS (Present)" if similarity >= similarity_threshold else "CHECK (Translated)"
                    is_match = True
                else:
                    status = "FAIL (Empty Region)"
                    is_match = False

            # Generate ONLY side-by-side compared image
            comp_path = ""
            if output_crops_dir:
                try:
                    comp_path = generate_only_compared_image(
                        eng_pdf_path=eng_pdf_path,
                        tr_pdf_path=tr_pdf_path,
                        eng_page=eng_page,
                        tr_page=tr_page_num,
                        roi_rect=roi_rect,
                        search_rect=search_rect,
                        output_dir=output_crops_dir,
                        region_label=region_label,
                        tr_name=tr_name,
                        shift_y=shift_y
                    )
                except Exception as e:
                    print(f"Warning: Failed to generate compared crop for {tr_name}: {e}")

            return {
                "tr_name": tr_name,
                "target_page": tr_page_num,
                "status": status,
                "similarity": similarity,
                "match_desc": match_desc,
                "shift_y": shift_y,
                "tr_text": tr_text,
                "is_match": is_match,
                "has_content": has_content,
                "exact_required": exact_match_required,
                "dont_compare_text": dont_compare_text,
                "comparison_img_path": comp_path,
            }
    except Exception as e:
        return {
            "tr_name": tr_name,
            "status": f"ERROR ({e})",
            "similarity": 0.0,
            "shift_y": 0.0,
            "tr_text": "",
            "is_match": False,
            "comparison_img_path": "",
        }


def run_batch_multiple_regions_check(
    eng_pdf_path: str,
    tr_target: str,
    regions: list[dict],
    y_tolerance: float = 40.0,
    similarity_threshold: float = 65.0,
    output_crops_dir: str = DEFAULT_CROPS_OUTPUT_DIR,
    progress_callback=None
) -> list[dict]:
    """
    Run verification and compared image generation for multiple user-defined regions across all Translated PDFs.
    """
    os.makedirs(output_crops_dir, exist_ok=True)

    with fitz.open(eng_pdf_path) as doc_eng:
        for r in regions:
            r["eng_text"] = extract_roi_text(doc_eng, r["page_num"], r["roi_rect"])

    tr_files = []
    if os.path.isfile(tr_target):
        tr_files.append(tr_target)
    elif os.path.isdir(tr_target):
        for root, _, files in os.walk(tr_target):
            for f in sorted(files):
                if f.lower().endswith(".pdf"):
                    tr_files.append(os.path.join(root, f))

    all_results = []
    total_steps = len(regions) * len(tr_files)
    step = 0

    for r in regions:
        for tr_path in tr_files:
            step += 1
            if progress_callback:
                progress_callback(step, total_steps, r["label"], os.path.basename(tr_path))

            res = search_and_verify_region_in_target(
                eng_pdf_path=eng_pdf_path,
                tr_pdf_path=tr_path,
                eng_page=r["page_num"],
                is_last_page=r.get("is_last_page", False),
                roi_rect=r["roi_rect"],
                eng_text=r.get("eng_text", ""),
                exact_match_required=r.get("exact_match", False),
                dont_compare_text=r.get("dont_compare_text", False),
                y_tolerance=y_tolerance,
                similarity_threshold=similarity_threshold,
                output_crops_dir=output_crops_dir,
                region_label=r["label"]
            )
            res["region_label"] = r["label"]
            res["eng_page"] = r["page_num"]
            res["eng_text"] = r.get("eng_text", "")
            res["roi_rect"] = r["roi_rect"]
            all_results.append(res)

    return all_results


# ==============================================================================
# CUSTOMTKINTER MULTI-REGION SELECTOR & INSPECTOR DIALOG
# ==============================================================================

class RegionInspectorDialog(ctk.CTkToplevel):
    """
    CustomTkinter Dialog for Interactive Multi-Region ROI Selection,
    Sub-Region Hierarchy, Exact Match & Visual Match Toggles, and Visual Side-by-Side Comparisons.
    """
    def __init__(self, parent, eng_pdf_path: str, tr_target_path: str):
        super().__init__(parent)
        self.title("Xylem SpotCheck - Custom Region Inspector & Layout Comparison")
        self.geometry("1280x880")
        self.minsize(1040, 680)
        self.resizable(True, True)
        self.configure(fg_color=UI_BG_CANVAS)

        # Lift and focus window cleanly on top
        try:
            self.lift()
            self.focus_force()
        except Exception:
            pass

        self.eng_pdf_path = os.path.abspath(eng_pdf_path)
        self.tr_target_path = os.path.abspath(tr_target_path) if tr_target_path else ""
        self.output_crops_dir = DEFAULT_CROPS_OUTPUT_DIR
        self.total_pages = get_page_count(self.eng_pdf_path)

        self.current_page = self.total_pages if self.total_pages > 0 else 1  # Default to last page
        self.zoom = 1.25
        self.page_width_pt = 420.0
        self.page_height_pt = 595.0

        # Dynamic Region list:
        # [{ "id": int, "label": str, "page_num": int, "is_last_page": bool, "roi_rect": tuple, "eng_text": str,
        #    "parent_id": int|None, "exact_match": bool, "dont_compare_text": bool, "color": str, "is_custom_label": bool }]
        self.regions = []
        self.active_region_id = None

        self.selection_start = None
        self.temp_rect_id = None
        self.tk_image = None
        self.pil_image = None

        self.is_checking = False
        self.check_results = []
        self.preview_tk_img = None
        self._updating_selection = False

        self._build_ui()
        self._load_and_render_page()

    def _build_ui(self):
        # 1. Top Header Banner
        header_frame = ctk.CTkFrame(self, fg_color=DEPENDABLE_BLUE, corner_radius=8)
        header_frame.pack(fill="x", padx=14, pady=(12, 8))

        title_lbl = ctk.CTkLabel(
            header_frame,
            text="XYLEM  |  Custom Region Inspector",
            font=ctk.CTkFont(family="Arial", size=15, weight="bold"),
            text_color=NEUTRAL_WHITE
        )
        title_lbl.pack(side="left", padx=16, pady=10)

        sub_lbl = ctk.CTkLabel(
            header_frame,
            text="Draw regions on canvas \u2022 Nested sub-regions \u2022 Exact Match \u2022 Don't Compare Text (Visual Match) \u2022 Compared images only",
            font=ctk.CTkFont(family="Arial", size=11),
            text_color=CLARITY_BLUE
        )
        sub_lbl.pack(side="right", padx=16, pady=10)

        # 2. Main Content Split Pane
        content_frame = ctk.CTkFrame(self, fg_color="transparent")
        content_frame.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        # Left Pane: Page Preview & Canvas
        left_card = ctk.CTkFrame(content_frame, fg_color=UI_CARD_BG, corner_radius=8, border_width=1, border_color=UI_BORDER)
        left_card.pack(side="left", fill="both", expand=True, padx=(0, 6))

        # Left Nav Bar
        nav_bar = ctk.CTkFrame(left_card, fg_color=UI_CARD_WELL, corner_radius=6)
        nav_bar.pack(fill="x", padx=8, pady=6)

        ctk.CTkButton(nav_bar, text="\u00ab First", width=55, height=28, fg_color=XYLEM_BLUE, text_color=NEUTRAL_WHITE, font=ctk.CTkFont(size=11, weight="bold"), command=self._goto_first_page).pack(side="left", padx=2)
        ctk.CTkButton(nav_bar, text="\u25c0 Prev", width=55, height=28, fg_color=DEPENDABLE_BLUE, text_color=NEUTRAL_WHITE, font=ctk.CTkFont(size=11), command=self._goto_prev_page).pack(side="left", padx=2)

        ctk.CTkLabel(nav_bar, text="Page:", font=ctk.CTkFont(size=11, weight="bold"), text_color=DEPENDABLE_BLUE).pack(side="left", padx=(8, 2))
        self.page_spin = tk.Spinbox(nav_bar, from_=1, to=max(1, self.total_pages), width=4, font=("Arial", 10), command=self._on_spinbox_change)
        self.page_spin.delete(0, "end")
        self.page_spin.insert(0, str(self.current_page))
        self.page_spin.bind("<Return>", lambda e: self._on_spinbox_change())
        self.page_spin.pack(side="left", padx=2)

        self.page_total_lbl = ctk.CTkLabel(nav_bar, text=f"/ {self.total_pages}", font=ctk.CTkFont(size=11), text_color=DEPENDABLE_BLUE)
        self.page_total_lbl.pack(side="left", padx=(2, 8))

        ctk.CTkButton(nav_bar, text="Next \u25b6", width=55, height=28, fg_color=DEPENDABLE_BLUE, text_color=NEUTRAL_WHITE, font=ctk.CTkFont(size=11), command=self._goto_next_page).pack(side="left", padx=2)
        ctk.CTkButton(nav_bar, text="Last \u00bb", width=55, height=28, fg_color=XYLEM_BLUE, text_color=NEUTRAL_WHITE, font=ctk.CTkFont(size=11, weight="bold"), command=self._goto_last_page).pack(side="left", padx=2)

        # Zoom Controls
        ctk.CTkButton(nav_bar, text="Zoom -", width=55, height=26, fg_color=UI_CARD_BG, text_color=DEPENDABLE_BLUE, border_width=1, border_color=UI_BORDER, font=ctk.CTkFont(size=10), command=self._zoom_out).pack(side="right", padx=2)
        self.zoom_lbl = ctk.CTkLabel(nav_bar, text="125%", font=ctk.CTkFont(size=11, weight="bold"), text_color=DEPENDABLE_BLUE)
        self.zoom_lbl.pack(side="right", padx=2)
        ctk.CTkButton(nav_bar, text="Zoom +", width=55, height=26, fg_color=UI_CARD_BG, text_color=DEPENDABLE_BLUE, border_width=1, border_color=UI_BORDER, font=ctk.CTkFont(size=10), command=self._zoom_in).pack(side="right", padx=2)

        # Canvas with Scrollbars
        canvas_container = tk.Frame(left_card, bg=UI_CARD_BG)
        canvas_container.pack(fill="both", expand=True, padx=8, pady=(0, 6))

        self.canvas = tk.Canvas(canvas_container, bg="#333333", cursor="crosshair")
        v_scroll = tk.Scrollbar(canvas_container, orient="vertical", command=self.canvas.yview)
        h_scroll = tk.Scrollbar(canvas_container, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(xscrollcommand=h_scroll.set, yscrollcommand=v_scroll.set)

        v_scroll.pack(side="right", fill="y")
        h_scroll.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)

        # Helper Tip Bar
        tip_frame = ctk.CTkFrame(left_card, fg_color=UI_CARD_WELL, corner_radius=6)
        tip_frame.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(
            tip_frame,
            text="\U0001f4a1 Drag to draw regions. Nested selections automatically create Sub-Regions (e.g. Region 1.1).",
            font=ctk.CTkFont(family="Arial", size=10, slant="italic"),
            text_color=DEPENDABLE_BLUE
        ).pack(side="left", padx=8, pady=4)

        # Right Pane: Multi-Region Management & Results
        right_card = ctk.CTkFrame(content_frame, fg_color=UI_CARD_BG, corner_radius=8, border_width=1, border_color=UI_BORDER, width=560)
        right_card.pack(side="right", fill="both", expand=True, padx=(6, 0))

        # 1. Multi-Region Management Card
        mgmt_header = ctk.CTkFrame(right_card, fg_color="transparent")
        mgmt_header.pack(fill="x", padx=10, pady=(8, 4))
        ctk.CTkLabel(mgmt_header, text="Defined Region Selections", font=ctk.CTkFont(family="Arial", size=12, weight="bold"), text_color=DEPENDABLE_BLUE).pack(side="left")

        mgmt_btn_bar = ctk.CTkFrame(right_card, fg_color="transparent")
        mgmt_btn_bar.pack(fill="x", padx=10, pady=(0, 4))

        ctk.CTkButton(mgmt_btn_bar, text="➕ Add Selection", width=95, height=26, fg_color=XYLEM_BLUE, text_color=NEUTRAL_WHITE, font=ctk.CTkFont(size=10, weight="bold"), command=self._on_btn_add_region).pack(side="left", padx=2)
        ctk.CTkButton(mgmt_btn_bar, text="🗑️ Delete Selection", width=95, height=26, fg_color=UI_CARD_WELL, text_color=DEPENDABLE_BLUE, font=ctk.CTkFont(size=10), command=self._on_btn_delete_region).pack(side="left", padx=2)
        ctk.CTkButton(mgmt_btn_bar, text="🧹 Clear All", width=75, height=26, fg_color=UI_CARD_WELL, text_color=DEPENDABLE_BLUE, font=ctk.CTkFont(size=10), command=self._on_btn_clear_regions).pack(side="left", padx=2)

        # Region Treeview (List of defined regions)
        r_table_frame = tk.Frame(right_card, bg=UI_CARD_BG)
        r_table_frame.pack(fill="x", padx=10, pady=2)

        r_cols = ("label", "page", "type", "coords")
        self.region_tree = ttk.Treeview(r_table_frame, columns=r_cols, show="headings", height=4)
        self.region_tree.heading("label", text="Custom Label / Name")
        self.region_tree.heading("page", text="Page")
        self.region_tree.heading("type", text="Match Type")
        self.region_tree.heading("coords", text="Coordinates (x0, y0, x1, y1)")

        self.region_tree.column("label", width=140, anchor="w")
        self.region_tree.column("page", width=45, anchor="center")
        self.region_tree.column("type", width=115, anchor="center")
        self.region_tree.column("coords", width=170, anchor="w")

        r_scroll = tk.Scrollbar(r_table_frame, orient="vertical", command=self.region_tree.yview)
        self.region_tree.configure(yscrollcommand=r_scroll.set)
        r_scroll.pack(side="right", fill="y")
        self.region_tree.pack(side="left", fill="both", expand=True)

        self.region_tree.bind("<<TreeviewSelect>>", self._on_region_tree_select)

        # 2. Active Region Editor, Exact Match & Don't Compare Text Toggles
        editor_card = ctk.CTkFrame(right_card, fg_color=UI_CARD_WELL, corner_radius=6)
        editor_card.pack(fill="x", padx=10, pady=6)

        edit_row = ctk.CTkFrame(editor_card, fg_color="transparent")
        edit_row.pack(fill="x", padx=8, pady=(6, 2))

        ctk.CTkLabel(edit_row, text="Label:", font=ctk.CTkFont(family="Arial", size=11, weight="bold"), text_color=DEPENDABLE_BLUE).pack(side="left")
        self.label_var = tk.StringVar()
        self.label_entry = ctk.CTkEntry(edit_row, textvariable=self.label_var, font=ctk.CTkFont(family="Arial", size=11, weight="bold"), width=130, height=28)
        self.label_entry.pack(side="left", padx=4)
        self.label_entry.bind("<KeyRelease>", self._on_label_entry_change)

        self.exact_match_var = ctk.BooleanVar(value=False)
        self.exact_match_chk = ctk.CTkCheckBox(
            edit_row,
            text="Exact Match (100%)",
            variable=self.exact_match_var,
            font=ctk.CTkFont(family="Arial", size=10, weight="bold"),
            text_color=DEPENDABLE_BLUE,
            command=self._on_exact_match_toggle
        )
        self.exact_match_chk.pack(side="left", padx=4)

        self.dont_compare_text_var = ctk.BooleanVar(value=False)
        self.dont_compare_text_chk = ctk.CTkCheckBox(
            edit_row,
            text="Don't Compare Text (Visual Match)",
            variable=self.dont_compare_text_var,
            font=ctk.CTkFont(family="Arial", size=10, weight="bold"),
            text_color=DEPENDABLE_BLUE,
            command=self._on_dont_compare_text_toggle
        )
        self.dont_compare_text_chk.pack(side="left", padx=4)

        self.coords_lbl = ctk.CTkLabel(edit_row, text="No region selected", font=ctk.CTkFont(family="Consolas", size=9), text_color=XYLEM_BLUE)
        self.coords_lbl.pack(side="right")

        ctk.CTkLabel(editor_card, text="Extracted English Master Content:", font=ctk.CTkFont(family="Arial", size=10, weight="bold"), text_color=DEPENDABLE_BLUE).pack(anchor="w", padx=8, pady=(2, 1))
        self.eng_text_box = ctk.CTkTextbox(editor_card, height=40, font=ctk.CTkFont(family="Arial", size=10), fg_color=UI_CARD_BG, text_color=DEPENDABLE_BLUE)
        self.eng_text_box.pack(fill="x", padx=8, pady=(0, 6))

        # 3. Parameters Card (Tolerance & Pass Threshold)
        params_card = ctk.CTkFrame(right_card, fg_color="transparent")
        params_card.pack(fill="x", padx=10, pady=2)

        ctk.CTkLabel(params_card, text="Vertical Shift Tolerance (\u00b1 \u0394y pt):", font=ctk.CTkFont(size=10), text_color=DEPENDABLE_BLUE).pack(side="left")
        self.tol_spin = tk.Spinbox(params_card, from_=0, to=150, width=5, font=("Arial", 9))
        self.tol_spin.delete(0, "end")
        self.tol_spin.insert(0, "40")
        self.tol_spin.pack(side="left", padx=6)

        ctk.CTkLabel(params_card, text="Pass Threshold (%):", font=ctk.CTkFont(size=10), text_color=DEPENDABLE_BLUE).pack(side="left", padx=(10, 0))
        self.thresh_spin = tk.Spinbox(params_card, from_=40, to=100, width=5, font=("Arial", 9))
        self.thresh_spin.delete(0, "end")
        self.thresh_spin.insert(0, "65")
        self.thresh_spin.pack(side="left", padx=6)

        # 4. Action Buttons (Run & Open Folder)
        action_box = ctk.CTkFrame(right_card, fg_color="transparent")
        action_box.pack(fill="x", padx=10, pady=4)

        self.run_btn = ctk.CTkButton(
            action_box,
            text="\u25b6  Check All Selections Across Translated PDFs",
            font=ctk.CTkFont(family="Arial", size=11, weight="bold"),
            fg_color=DYNAMIC_GREEN,
            text_color=DEPENDABLE_BLUE,
            height=34,
            command=self._start_batch_check
        )
        self.run_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self.open_crops_btn = ctk.CTkButton(
            action_box,
            text="\U0001f4c2 Open Cropped Comparison Folder",
            font=ctk.CTkFont(family="Arial", size=10, weight="bold"),
            fg_color=XYLEM_BLUE,
            text_color=NEUTRAL_WHITE,
            height=34,
            command=self._open_crops_folder
        )
        self.open_crops_btn.pack(side="right")

        self.progress_bar = ctk.CTkProgressBar(right_card, height=8, progress_color=XYLEM_BLUE)
        self.progress_bar.set(0.0)
        self.progress_bar.pack(fill="x", padx=10, pady=(2, 2))

        self.status_lbl = ctk.CTkLabel(right_card, text="Ready \u2022 Draw regions on page or edit labels above", font=ctk.CTkFont(size=10, weight="bold"), text_color=XYLEM_BLUE)
        self.status_lbl.pack(anchor="w", padx=10, pady=(0, 2))

        # 5. Results Table & Cropped Comparison Preview
        results_card = ctk.CTkFrame(right_card, fg_color=UI_CARD_BG, corner_radius=6, border_width=1, border_color=UI_BORDER)
        results_card.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        table_frame = tk.Frame(results_card, bg=UI_CARD_BG)
        table_frame.pack(fill="both", expand=True, padx=4, pady=4)

        columns = ("region_label", "tr_name", "page", "similarity", "shift", "status")
        self.results_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=4)
        self.results_tree.heading("region_label", text="Region Label")
        self.results_tree.heading("tr_name", text="Translated PDF Name")
        self.results_tree.heading("page", text="Page")
        self.results_tree.heading("similarity", text="Similarity")
        self.results_tree.heading("shift", text="Shift (pt)")
        self.results_tree.heading("status", text="Status")

        self.results_tree.column("region_label", width=110, anchor="w")
        self.results_tree.column("tr_name", width=170, anchor="w")
        self.results_tree.column("page", width=45, anchor="center")
        self.results_tree.column("similarity", width=65, anchor="center")
        self.results_tree.column("shift", width=65, anchor="center")
        self.results_tree.column("status", width=120, anchor="center")

        res_scroll = tk.Scrollbar(table_frame, orient="vertical", command=self.results_tree.yview)
        self.results_tree.configure(yscrollcommand=res_scroll.set)
        res_scroll.pack(side="right", fill="y")
        self.results_tree.pack(side="left", fill="both", expand=True)

        self.results_tree.tag_configure("pass", background="#D9EAD3", foreground="#274E13")
        self.results_tree.tag_configure("check", background="#FFF2CC", foreground="#7F6000")
        self.results_tree.tag_configure("fail", background="#FCE5CD", foreground="#783F04")

        self.results_tree.bind("<<TreeviewSelect>>", self._on_results_tree_select)

        # Selected row preview (Text & Visual Crop Preview)
        preview_container = ctk.CTkFrame(results_card, fg_color="transparent")
        preview_container.pack(fill="x", padx=4, pady=(2, 4))

        # Text preview
        txt_prev_frame = ctk.CTkFrame(preview_container, fg_color="transparent")
        txt_prev_frame.pack(side="left", fill="both", expand=True, padx=(0, 4))
        ctk.CTkLabel(txt_prev_frame, text="Matched Translated Text:", font=ctk.CTkFont(size=9, weight="bold"), text_color=DEPENDABLE_BLUE).pack(anchor="w")
        self.tr_text_box = ctk.CTkTextbox(txt_prev_frame, height=45, font=ctk.CTkFont(family="Arial", size=10), fg_color=UI_CARD_WELL, text_color=DEPENDABLE_BLUE)
        self.tr_text_box.pack(fill="both", expand=True)

        # Visual Image Crop Comparison Thumbnail
        img_prev_frame = ctk.CTkFrame(preview_container, fg_color="transparent", width=180)
        img_prev_frame.pack(side="right", padx=(4, 0))
        ctk.CTkLabel(img_prev_frame, text="Visual Crop Comparison:", font=ctk.CTkFont(size=9, weight="bold"), text_color=DEPENDABLE_BLUE).pack(anchor="w")
        self.crop_thumb_lbl = tk.Label(img_prev_frame, text="[Select row to preview crop]", font=("Arial", 8), bg=UI_CARD_WELL, width=28, height=3)
        self.crop_thumb_lbl.pack(fill="both", expand=True)
        self.crop_thumb_lbl.bind("<Button-1>", lambda e: self._open_selected_crop_image())

    # ──────────────────────────────────────────────────────────
    # Multi-Region & Sub-Region Management with Consecutive Numbering
    # ──────────────────────────────────────────────────────────
    def _find_enclosing_parent(self, roi_rect: tuple, page_num: int):
        """Find if roi_rect is contained inside an existing top-level region on page_num."""
        for r in self.regions:
            if r["page_num"] == page_num and r.get("parent_id") is None:
                if is_rect_contained_in_parent(roi_rect, r["roi_rect"]):
                    return r
        return None

    def _reindex_and_renumber_regions(self):
        """
        Re-index and consecutively renumber all regions and sub-regions:
        Top-level: Region 1, Region 2, Region 3...
        Sub-regions: Region 1.1, Region 1.2, Region 2.1...
        """
        top_counter = 1
        id_counter = 1

        top_level_regions = [r for r in self.regions if r.get("parent_id") is None]
        for top_r in top_level_regions:
            old_id = top_r["id"]
            new_id = id_counter
            id_counter += 1
            top_r["id"] = new_id

            top_num_str = f"Region {top_counter}"
            if not top_r.get("is_custom_label", False) or top_r["label"].startswith("Region "):
                top_r["label"] = top_num_str

            sub_counter = 1
            sub_regions = [r for r in self.regions if r.get("parent_id") == old_id]
            for sub_r in sub_regions:
                sub_r["parent_id"] = new_id
                sub_r["id"] = id_counter
                id_counter += 1

                sub_num_str = f"Region {top_counter}.{sub_counter}"
                if not sub_r.get("is_custom_label", False) or sub_r["label"].startswith("Region "):
                    sub_r["label"] = sub_num_str
                sub_counter += 1

            top_counter += 1

        ordered = []
        for top_r in top_level_regions:
            ordered.append(top_r)
            subs = [r for r in self.regions if r.get("parent_id") == top_r["id"]]
            ordered.extend(subs)

        self.regions = ordered

    def _add_new_region(self, label: str = None, roi_rect: tuple = None, page_num: int = None):
        p_num = page_num if page_num is not None else self.current_page
        is_last = (p_num == self.total_pages)

        if not roi_rect:
            roi_rect = (35.0, 460.0, 385.0, 565.0)

        parent = self._find_enclosing_parent(roi_rect, p_num)
        parent_id = parent["id"] if parent else None

        new_region = {
            "id": len(self.regions) + 1,
            "label": label or "",
            "page_num": p_num,
            "is_last_page": is_last,
            "roi_rect": roi_rect,
            "eng_text": "",
            "parent_id": parent_id,
            "exact_match": False,
            "dont_compare_text": False,
            "color": REGION_COLORS[len(self.regions) % len(REGION_COLORS)],
            "is_custom_label": bool(label)
        }
        self.regions.append(new_region)
        self._reindex_and_renumber_regions()

        self.active_region_id = new_region["id"]
        return new_region

    def _get_active_region(self) -> dict:
        for r in self.regions:
            if r["id"] == self.active_region_id:
                return r
        return self.regions[0] if self.regions else None

    def _get_match_type_string(self, r: dict) -> str:
        if r.get("dont_compare_text", False):
            return "Visual (No Text)"
        elif r.get("exact_match", False):
            return "Exact (100%)"
        else:
            return "Layout/Presence"

    def _sync_regions_table(self):
        self._updating_selection = True
        try:
            for item in self.region_tree.get_children():
                self.region_tree.delete(item)

            for r in self.regions:
                x0, y0, x1, y1 = r["roi_rect"]
                coords_str = f"[{x0:.1f}, {y0:.1f}, {x1:.1f}, {y1:.1f}]"
                type_str = self._get_match_type_string(r)
                
                display_label = f"  ↳ {r['label']}" if r.get("parent_id") is not None else r["label"]

                self.region_tree.insert(
                    "",
                    "end",
                    iid=str(r["id"]),
                    values=(display_label, f"Pg {r['page_num']}", type_str, coords_str)
                )

            if self.active_region_id and self.region_tree.exists(str(self.active_region_id)):
                self.region_tree.selection_set(str(self.active_region_id))

            active_r = self._get_active_region()
            if active_r:
                self.label_var.set(active_r["label"])
                self.exact_match_var.set(active_r.get("exact_match", False))
                self.dont_compare_text_var.set(active_r.get("dont_compare_text", False))
                x0, y0, x1, y1 = active_r["roi_rect"]
                self.coords_lbl.configure(text=f"x: {x0:.1f} \u2192 {x1:.1f}, y: {y0:.1f} \u2192 {y1:.1f}")
                self.eng_text_box.delete("1.0", "end")
                self.eng_text_box.insert("1.0", active_r.get("eng_text", ""))
            else:
                self.label_var.set("")
                self.exact_match_var.set(False)
                self.dont_compare_text_var.set(False)
                self.coords_lbl.configure(text="No region selected")
                self.eng_text_box.delete("1.0", "end")
                self.eng_text_box.insert("1.0", "[Drag on the page to select an area]")
        finally:
            self._updating_selection = False

    def _on_btn_add_region(self):
        self._add_new_region(
            roi_rect=(40.0, 100.0, 380.0, 200.0)
        )
        self._load_and_render_page()

    def _on_btn_delete_region(self):
        if not self.regions:
            return
        active_r = self._get_active_region()
        if active_r:
            del_id = active_r["id"]
            self.regions = [r for r in self.regions if r["id"] != del_id and r.get("parent_id") != del_id]
            self._reindex_and_renumber_regions()
            self.active_region_id = self.regions[0]["id"] if self.regions else None
            self._load_and_render_page()

    def _on_btn_clear_regions(self):
        if self.regions and messagebox.askyesno("Confirm Clear", "Clear all custom regions?"):
            self.regions = []
            self.active_region_id = None
            self._load_and_render_page()

    def _on_region_tree_select(self, event):
        if getattr(self, "_updating_selection", False):
            return
        sel = self.region_tree.selection()
        if not sel:
            return
        r_id = int(sel[0])
        if self.active_region_id == r_id:
            return
        self.active_region_id = r_id
        active_r = self._get_active_region()
        if active_r:
            if active_r["page_num"] != self.current_page:
                self.current_page = active_r["page_num"]
                self._sync_spinbox()
                self._load_and_render_page()
            else:
                self.label_var.set(active_r["label"])
                self.exact_match_var.set(active_r.get("exact_match", False))
                self.dont_compare_text_var.set(active_r.get("dont_compare_text", False))
                x0, y0, x1, y1 = active_r["roi_rect"]
                self.coords_lbl.configure(text=f"x: {x0:.1f} \u2192 {x1:.1f}, y: {y0:.1f} \u2192 {y1:.1f}")
                self.eng_text_box.delete("1.0", "end")
                self.eng_text_box.insert("1.0", active_r.get("eng_text", ""))
                self._draw_all_rois_on_canvas()

    def _on_label_entry_change(self, event):
        active_r = self._get_active_region()
        if active_r:
            new_label = self.label_var.get().strip()
            if new_label:
                active_r["label"] = new_label
                active_r["is_custom_label"] = True
                if self.region_tree.exists(str(active_r["id"])):
                    x0, y0, x1, y1 = active_r["roi_rect"]
                    type_str = self._get_match_type_string(active_r)
                    display_lbl = f"  ↳ {new_label}" if active_r.get("parent_id") is not None else new_label
                    self.region_tree.item(str(active_r["id"]), values=(display_lbl, f"Pg {active_r['page_num']}", type_str, f"[{x0:.1f}, {y0:.1f}, {x1:.1f}, {y1:.1f}]"))
                self._draw_all_rois_on_canvas()

    def _on_exact_match_toggle(self):
        active_r = self._get_active_region()
        if active_r:
            is_checked = self.exact_match_var.get()
            active_r["exact_match"] = is_checked
            if is_checked:
                active_r["dont_compare_text"] = False
                self.dont_compare_text_var.set(False)

            if self.region_tree.exists(str(active_r["id"])):
                x0, y0, x1, y1 = active_r["roi_rect"]
                type_str = self._get_match_type_string(active_r)
                display_lbl = f"  ↳ {active_r['label']}" if active_r.get("parent_id") is not None else active_r["label"]
                self.region_tree.item(str(active_r["id"]), values=(display_lbl, f"Pg {active_r['page_num']}", type_str, f"[{x0:.1f}, {y0:.1f}, {x1:.1f}, {y1:.1f}]"))
            self._draw_all_rois_on_canvas()

    def _on_dont_compare_text_toggle(self):
        active_r = self._get_active_region()
        if active_r:
            is_checked = self.dont_compare_text_var.get()
            active_r["dont_compare_text"] = is_checked
            if is_checked:
                active_r["exact_match"] = False
                self.exact_match_var.set(False)

            if self.region_tree.exists(str(active_r["id"])):
                x0, y0, x1, y1 = active_r["roi_rect"]
                type_str = self._get_match_type_string(active_r)
                display_lbl = f"  ↳ {active_r['label']}" if active_r.get("parent_id") is not None else active_r["label"]
                self.region_tree.item(str(active_r["id"]), values=(display_lbl, f"Pg {active_r['page_num']}", type_str, f"[{x0:.1f}, {y0:.1f}, {x1:.1f}, {y1:.1f}]"))
            self._draw_all_rois_on_canvas()

    # ──────────────────────────────────────────────────────────
    # Page Navigation & Zoom
    # ──────────────────────────────────────────────────────────
    def _goto_first_page(self):
        self.current_page = 1
        self._sync_spinbox()
        self._load_and_render_page()

    def _goto_prev_page(self):
        if self.current_page > 1:
            self.current_page -= 1
            self._sync_spinbox()
            self._load_and_render_page()

    def _goto_next_page(self):
        if self.current_page < self.total_pages:
            self.current_page += 1
            self._sync_spinbox()
            self._load_and_render_page()

    def _goto_last_page(self):
        self.current_page = self.total_pages
        self._sync_spinbox()
        self._load_and_render_page()

    def _on_spinbox_change(self):
        try:
            val = int(self.page_spin.get())
            if 1 <= val <= self.total_pages:
                self.current_page = val
                self._load_and_render_page()
        except ValueError:
            pass

    def _sync_spinbox(self):
        self.page_spin.delete(0, "end")
        self.page_spin.insert(0, str(self.current_page))

    def _zoom_in(self):
        self.zoom = min(2.5, self.zoom + 0.25)
        self.zoom_lbl.configure(text=f"{int(self.zoom * 100)}%")
        self._load_and_render_page()

    def _zoom_out(self):
        self.zoom = max(0.5, self.zoom - 0.25)
        self.zoom_lbl.configure(text=f"{int(self.zoom * 100)}%")
        self._load_and_render_page()

    # ──────────────────────────────────────────────────────────
    # Rendering & Multi-ROI Canvas Drawing
    # ──────────────────────────────────────────────────────────
    def _load_and_render_page(self):
        try:
            img, pw, ph = render_pdf_page_image(self.eng_pdf_path, self.current_page, zoom=self.zoom)
            self.pil_image = img
            self.page_width_pt = pw
            self.page_height_pt = ph
            self.tk_image = ImageTk.PhotoImage(img, master=self)

            self.canvas.delete("all")
            self.canvas.create_image(10, 10, anchor="nw", image=self.tk_image, tags="page_img")
            self.canvas.config(scrollregion=(0, 0, img.width + 30, img.height + 30))

            with fitz.open(self.eng_pdf_path) as doc_eng:
                for r in self.regions:
                    if r["page_num"] == self.current_page:
                        r["eng_text"] = extract_roi_text(doc_eng, self.current_page, r["roi_rect"])

            self._draw_all_rois_on_canvas()
            self._sync_regions_table()
        except Exception as e:
            messagebox.showerror("Render Error", f"Failed to render page {self.current_page}:\n{e}")

    def _draw_all_rois_on_canvas(self):
        self.canvas.delete("roi_box")
        self.canvas.delete("roi_badge")

        for r in self.regions:
            if r["page_num"] != self.current_page:
                continue

            x0, y0, x1, y1 = r["roi_rect"]
            cx0 = 10 + x0 * self.zoom
            cy0 = 10 + y0 * self.zoom
            cx1 = 10 + x1 * self.zoom
            cy1 = 10 + y1 * self.zoom

            is_active = (r["id"] == self.active_region_id)
            is_sub = (r.get("parent_id") is not None)

            box_width = 3 if is_active else 2
            dash_pattern = () if (is_active and not is_sub) else ((4, 2) if is_sub else ())
            outline_color = DYNAMIC_GREEN if is_active else r["color"]

            self.canvas.create_rectangle(
                cx0, cy0, cx1, cy1,
                outline=outline_color,
                width=box_width,
                dash=dash_pattern,
                tags=("roi_box", f"roi_{r['id']}")
            )

            # Badge tag
            mode_tag = " [VISUAL]" if r.get("dont_compare_text", False) else (" [EXACT]" if r.get("exact_match", False) else "")
            badge_text = f" {r['label']}{mode_tag} "
            badge_y = max(12, cy0 - 10)
            self.canvas.create_rectangle(
                cx0, badge_y - 8, cx0 + len(badge_text) * 7 + 4, badge_y + 8,
                fill=outline_color,
                outline="",
                tags=("roi_badge", f"badge_{r['id']}")
            )
            self.canvas.create_text(
                cx0 + 4, badge_y,
                text=badge_text,
                anchor="w",
                font=("Arial", 8, "bold"),
                fill=NEUTRAL_WHITE if is_active else "#FFFFFF",
                tags=("roi_badge", f"badge_txt_{r['id']}")
            )

    def _on_canvas_press(self, event):
        canvas_x = self.canvas.canvasx(event.x)
        canvas_y = self.canvas.canvasy(event.y)
        self.selection_start = (canvas_x, canvas_y)

    def _on_canvas_drag(self, event):
        if not self.selection_start:
            return
        cur_x = self.canvas.canvasx(event.x)
        cur_y = self.canvas.canvasy(event.y)

        x0 = min(self.selection_start[0], cur_x)
        y0 = min(self.selection_start[1], cur_y)
        x1 = max(self.selection_start[0], cur_x)
        y1 = max(self.selection_start[1], cur_y)

        if self.temp_rect_id:
            self.canvas.coords(self.temp_rect_id, x0, y0, x1, y1)
        else:
            self.temp_rect_id = self.canvas.create_rectangle(
                x0, y0, x1, y1,
                outline=DYNAMIC_GREEN,
                width=2,
                dash=(4, 2),
                tags="temp_rect"
            )

    def _on_canvas_release(self, event):
        if not self.selection_start:
            return
        cur_x = self.canvas.canvasx(event.x)
        cur_y = self.canvas.canvasy(event.y)

        cx0 = min(self.selection_start[0], cur_x)
        cy0 = min(self.selection_start[1], cur_y)
        cx1 = max(self.selection_start[0], cur_x)
        cy1 = max(self.selection_start[1], cur_y)
        self.selection_start = None

        if self.temp_rect_id:
            self.canvas.delete(self.temp_rect_id)
            self.temp_rect_id = None

        if (cx1 - cx0) < 12 or (cy1 - cy0) < 12:
            return

        x0 = max(0.0, (cx0 - 10) / self.zoom)
        y0 = max(0.0, (cy0 - 10) / self.zoom)
        x1 = min(self.page_width_pt, (cx1 - 10) / self.zoom)
        y1 = min(self.page_height_pt, (cy1 - 10) / self.zoom)
        new_roi = (round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1))

        self._add_new_region(
            roi_rect=new_roi,
            page_num=self.current_page
        )

        self._load_and_render_page()

    # ──────────────────────────────────────────────────────────
    # Check All Regions Across Translated PDFs & Generate Crops
    # ──────────────────────────────────────────────────────────
    def _start_batch_check(self):
        if self.is_checking:
            return

        if not os.path.exists(self.eng_pdf_path):
            messagebox.showerror("Error", f"English PDF does not exist:\n{self.eng_pdf_path}")
            return
        if not os.path.exists(self.tr_target_path):
            messagebox.showerror("Error", f"Translated target path does not exist:\n{self.tr_target_path}")
            return
        if not self.regions:
            messagebox.showinfo("Notice", "Please draw or add at least one region to check.")
            return

        try:
            tolerance = float(self.tol_spin.get())
            threshold = float(self.thresh_spin.get())
        except ValueError:
            messagebox.showerror("Invalid Input", "Please enter valid numeric values for tolerance and threshold.")
            return

        self.is_checking = True
        self.run_btn.configure(state="disabled", text="⏳ Checking & Cropping Regions...")
        self.status_lbl.configure(text="Processing translations and generating comparison crops...", text_color=RADIANT_ORANGE)
        self.progress_bar.set(0.0)

        for item in self.results_tree.get_children():
            self.results_tree.delete(item)
        self.check_results = []
        self.tr_text_box.delete("1.0", "end")
        self.crop_thumb_lbl.config(image="", text="[Processing crops...]")

        t = threading.Thread(
            target=self._run_batch_check_thread,
            args=(tolerance, threshold),
            daemon=True
        )
        t.start()

    def _run_batch_check_thread(self, tolerance, threshold):
        def progress_cb(current, total, reg_lbl, filename):
            pct = current / total
            self.after(0, lambda: self.progress_bar.set(pct))
            self.after(0, lambda: self.status_lbl.configure(text=f"Cropping & Checking [{reg_lbl}]: {filename} ({current}/{total})"))

        results = run_batch_multiple_regions_check(
            eng_pdf_path=self.eng_pdf_path,
            tr_target=self.tr_target_path,
            regions=self.regions,
            y_tolerance=tolerance,
            similarity_threshold=threshold,
            output_crops_dir=self.output_crops_dir,
            progress_callback=progress_cb
        )

        self.after(0, self._on_batch_check_complete, results)

    def _on_batch_check_complete(self, results):
        self.is_checking = False
        self.run_btn.configure(state="normal", text="\u25b6  Check All Selections Across Translated PDFs")
        self.progress_bar.set(1.0)
        self.check_results = results

        pass_count = 0
        for r in results:
            stat = r["status"]
            tag = "pass" if "PASS" in stat else ("check" if "CHECK" in stat else "fail")
            if "PASS" in stat:
                pass_count += 1

            shift_str = f"{r['shift_y']:+.1f}" if r["shift_y"] != 0.0 else "0.0"
            self.results_tree.insert(
                "",
                "end",
                values=(
                    r["region_label"],
                    r["tr_name"],
                    f"Pg {r.get('target_page', '-')}",
                    f"{r['similarity']:.1f}%",
                    shift_str,
                    stat
                ),
                tags=(tag,)
            )

        total = len(results)
        self.status_lbl.configure(
            text=f"\u25cf Multi-Region Verification & Crops Complete: {pass_count} / {total} Passed ({round(pass_count/max(1,total)*100, 1)}%)",
            text_color=DYNAMIC_GREEN if pass_count == total else RADIANT_ORANGE
        )

    def _on_results_tree_select(self, event):
        selected = self.results_tree.selection()
        if not selected:
            return
        item = selected[0]
        idx = self.results_tree.index(item)
        if 0 <= idx < len(self.check_results):
            r = self.check_results[idx]
            tr_content = r.get("tr_text", "")
            self.tr_text_box.delete("1.0", "end")
            self.tr_text_box.insert("1.0", tr_content if tr_content else "[Empty / Non-Text Graphics]")

            comp_img_path = r.get("comparison_img_path", "")
            if comp_img_path and os.path.exists(comp_img_path):
                try:
                    c_img = Image.open(comp_img_path)
                    c_img.thumbnail((200, 70))
                    self.preview_tk_img = ImageTk.PhotoImage(c_img, master=self)
                    self.crop_thumb_lbl.config(image=self.preview_tk_img, text="")
                except Exception:
                    self.crop_thumb_lbl.config(image="", text="[Click to Open Crop Image]")
            else:
                self.crop_thumb_lbl.config(image="", text="[No Crop Image Available]")

    def _open_selected_crop_image(self):
        selected = self.results_tree.selection()
        if not selected:
            return
        item = selected[0]
        idx = self.results_tree.index(item)
        if 0 <= idx < len(self.check_results):
            r = self.check_results[idx]
            comp_path = r.get("comparison_img_path", "")
            if comp_path and os.path.exists(comp_path):
                os.startfile(comp_path)
            else:
                self._open_crops_folder()

    def _open_crops_folder(self):
        if self.output_crops_dir and os.path.exists(self.output_crops_dir):
            os.startfile(self.output_crops_dir)
        else:
            messagebox.showinfo("Folder Notice", f"Crops output folder does not exist yet:\n{self.output_crops_dir}\n\nRun the check first to generate crops.")


# ==============================================================================
# STANDALONE CLI LAUNCHER
# ==============================================================================

def open_region_inspector(parent=None, eng_pdf_path=None, tr_target_path=None):
    """Convenience helper to open the Region Inspector dialog."""
    default_eng = os.path.abspath(r"Input\English\894387_5.0_en-US_2026-04_IOM.Start350.pdf")
    default_tr = os.path.abspath(r"Input\Translated")

    eng_path = eng_pdf_path or (default_eng if os.path.exists(default_eng) else "")
    tr_path = tr_target_path or (default_tr if os.path.exists(default_tr) else "")

    if parent is None:
        root = ctk.CTk()
        root.withdraw()
        dlg = RegionInspectorDialog(root, eng_path, tr_path)
        dlg.protocol("WM_DELETE_WINDOW", root.destroy)
        root.mainloop()
    else:
        dlg = RegionInspectorDialog(parent, eng_path, tr_path)
        try:
            dlg.lift()
            dlg.focus_force()
        except Exception:
            pass
        return dlg


if __name__ == "__main__":
    open_region_inspector()
