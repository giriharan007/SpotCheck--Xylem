"""
core/pipeline.py

Unified entry point for PDF quality & consistency inspection.

Imports and coordinates:
  - toc.py            : Table of Contents topic numerics & missing-section flagging
  - barcode_qr.py     : Multi-page barcode & QR code count & presence validation (non-visual)
  - crop_images.py    : Pure graphic element extraction (excluding text & barcode/QR)
  - compare_crops.py  : Pure visual graphic crop comparison across pages

Page 1 and last-page field checks (title, sub-title, manual type, address,
disclaimer, copyright, footer metadata) are deliberately NOT performed here.
These manuals are stylesheet-based, so those fields are verified through the
Region Inspector's scoped exact match instead - see core/region_engine.py.

Generates:
  - PDF_Quality_Inspection_Report.xlsx : Unified multi-sheet Excel report
    (Overview, TOC, Barcode & QR, Images, Image Counts)
"""

import os
import sys
import argparse
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from core import toc as TOC
from core import barcode_qr as Barcode_QR_Check
from core import crop_images as crop_pdf_images
from core import compare_crops as Compare_cropped_images
from core import image_counts as ImageCounts
from core import margins as PageMargins
from core import docscan as DocScan
from core import metadata as MetaData
from core import region_engine as RegionEngine
from core import text_overlap as TextOverlap
from core import untranslated as Untranslated


# ============================================================
# EXCEL REPORT GENERATORS
# ============================================================

def generate_unified_excel_report(
    toc_results,
    bc_qr_results,
    img_results_summary,
    img_crop_details_list,
    count_results,
    output_excel_path,
    run_margins=None,
    metadata_rows=None,
    region_results=None,
    region_note="",
    overlap_results=None,
    untranslated_results=None
):
    """
    Generate ONE single unified Excel report containing 5 worksheets:
      1. Overview    : Executive overview of all sub-check verdicts and Master Verdict
      2. TOC         : Topic numerics, missing topics, and Overall status
      3. Barcode & QR: Barcode & QR code count and presence matching
      4. Images      : Crop-by-crop visual graphic matching
      5. Image Counts: Symmetric per-topic counts - catches a graphic added to or
                       missing from a translation, which one-directional crop
                       matching cannot see
      6. Text Overlap: Text printed through other text, in any document
      7. Not Translated: English left behind in a translation
    """
    overlap_results = overlap_results or []
    untranslated_results = untranslated_results or []

    def _count_for(name):
        """How many of each text finding belong to one document."""
        return (sum(1 for f in overlap_results if f.get("document") == name),
                sum(1 for f in untranslated_results if f.get("document") == name))
    wb = openpyxl.Workbook()

    # Common Styles
    header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")

    pass_fill = PatternFill(start_color="D9EAD3", end_color="D9EAD3", fill_type="solid")
    pass_font = Font(name="Calibri", size=10, bold=True, color="274E13")

    fail_fill = PatternFill(start_color="FCE5CD", end_color="FCE5CD", fill_type="solid")
    fail_font = Font(name="Calibri", size=10, bold=True, color="783F04")

    thin_border = Border(
        left=Side(style="thin", color="D3D3D3"),
        right=Side(style="thin", color="D3D3D3"),
        top=Side(style="thin", color="D3D3D3"),
        bottom=Side(style="thin", color="D3D3D3")
    )

    # --------------------------------------------------------
    # TAB 1: OVERVIEW
    # --------------------------------------------------------
    ws_overview = wb.active
    ws_overview.title = "Overview"

    headers_overview = [
        "English Master PDF",
        "Translated PDF",
        "TOC",
        "Images",
        "Image Counts",
        "Barcode & QR",
        "Text Overlap",
        "Not Translated",
        "Master Verdict",
    ]
    ws_overview.append(headers_overview)

    for i in range(len(toc_results)):
        toc_res = toc_results[i]
        bc_res = bc_qr_results[i]
        img_res = img_results_summary[i]

        toc_status = toc_res["status"]
        bc_status = bc_res["overall_verdict"]
        img_status = img_res["overall_status"]
        cnt_status = count_results[i]["overall_verdict"]

        # Colliding text and untranslated English are defects like any other,
        # so they carry the master verdict down with them. A manual that reads
        # PASS while a sentence of English sits in the middle of it is a report
        # nobody can act on.
        n_overlap, n_untranslated = _count_for(toc_res["translated_pdf"])
        overlap_status = "PASS" if not n_overlap else f"FAIL ({n_overlap})"
        untr_status = "PASS" if not n_untranslated else f"FAIL ({n_untranslated})"

        master_pass = (
            toc_status == "PASS" and
            bc_status == "PASS" and
            img_status == "PASS" and
            cnt_status == "PASS" and
            not n_overlap and
            not n_untranslated
        )
        master_verdict = "PASS" if master_pass else "FAIL"

        row_data = [
            toc_res["english_pdf"],
            toc_res["translated_pdf"],
            toc_status,
            img_status,
            cnt_status,
            bc_status,
            overlap_status,
            untr_status,
            master_verdict,
        ]
        ws_overview.append(row_data)

    # A report read a month later has to say what it was run with. The ignored
    # margins change which graphics were extracted at all, so a count of 77 vs
    # 74 between two runs is only explicable if the setting is recorded here.
    if toc_results:
        master_name = toc_results[0].get("english_pdf", "")
        master_overlaps, _ = _count_for(master_name)
        if master_overlaps:
            ws_overview.append([])
            ws_overview.append([
                master_name, "(the master itself)", "", "", "", "",
                f"FAIL ({master_overlaps})", "n/a", "FAIL",
            ])

    ws_overview.append([])
    ws_overview.append(["Ignored page margins",
                        PageMargins.describe(run_margins),
                        "Elements lying entirely inside these bands are not extracted or counted."])

    # --------------------------------------------------------
    # TAB 2: TOC
    # --------------------------------------------------------
    ws_toc = wb.create_sheet(title="TOC")

    headers_toc = [
        "English Master PDF",
        "Translated PDF",
        "TOC Topic Numerics",
        "Missing TOC Topic Numerics",
        "Overall",
    ]
    ws_toc.append(headers_toc)

    for res in toc_results:
        row_data = [
            res["english_pdf"],
            res["translated_pdf"],
            res.get("target_numerics_str") or "None",
            res["diff_msg"],
            res["status"],
        ]
        ws_toc.append(row_data)

    # --------------------------------------------------------
    # TAB 3: BARCODE & QR (Dedicated Worksheet)
    # --------------------------------------------------------
    ws_bc = wb.create_sheet(title="Barcode & QR")

    headers_bc = [
        "English Master PDF",
        "Translated PDF",
        "Master Barcode Count",
        "Target Barcode Count",
        "Master Barcode Pages",
        "Target Barcode Pages",
        "Barcode Status",
        "Master QR Count",
        "Target QR Count",
        "Master QR Pages",
        "Target QR Pages",
        "QR Status",
        "Detection Method",
        "Overall Status",
    ]
    ws_bc.append(headers_bc)

    for res in bc_qr_results:
        m_bc_pages = ", ".join(map(str, res.get("master_pages_barcode", []))) if res.get("master_pages_barcode") else "None"
        t_bc_pages = ", ".join(map(str, res.get("target_pages_barcode", []))) if res.get("target_pages_barcode") else "None"
        m_qr_pages = ", ".join(map(str, res.get("master_pages_qr", []))) if res.get("master_pages_qr") else "None"
        t_qr_pages = ", ".join(map(str, res.get("target_pages_qr", []))) if res.get("target_pages_qr") else "None"

        row_data = [
            res["english_pdf"],
            res["translated_pdf"],
            res["master_barcode_count"],
            res["target_barcode_count"],
            m_bc_pages,
            t_bc_pages,
            res["barcode_status"],
            res["master_qr_count"],
            res["target_qr_count"],
            m_qr_pages,
            t_qr_pages,
            res["qr_status"],
            res.get("detection_note", ""),
            res["overall_verdict"],
        ]
        ws_bc.append(row_data)

    # --------------------------------------------------------
    # TAB 4: IMAGES (Detailed Crop-by-Crop Worksheet)
    # --------------------------------------------------------
    ws_img = wb.create_sheet(title="Images")

    headers_img = [
        "English Master PDF",
        "Translated PDF",
        "Crop Name",
        "Image Type",
        "Topic",
        "English Page",
        "Matched Target Page",
        "Page Movement",
        "Similarity (%)",
        "Match Status",
    ]
    ws_img.append(headers_img)

    for crop_row in img_crop_details_list:
        row_data = [
            crop_row["english_pdf"],
            crop_row["translated_pdf"],
            crop_row["crop_name"],
            crop_row["image_type"],
            crop_row.get("topic") or "-",
            crop_row["eng_page"],
            crop_row["trans_page"] if crop_row["trans_page"] != -1 else "N/A",
            crop_row["shift_info"],
            f"{crop_row['similarity']:.2f}%",
            crop_row["status"],
        ]
        ws_img.append(row_data)

    # --------------------------------------------------------
    # TAB 5: IMAGE COUNTS (symmetric - both documents counted)
    # --------------------------------------------------------
    ws_cnt = wb.create_sheet(title="Image Counts")
    ws_cnt.append([
        "English Master PDF", "Translated PDF", "Granularity", "Topic Code",
        "Topic Title", "Master Images", "Translated Images", "Status",
    ])
    for res in count_results:
        for row in res["rows"]:
            ws_cnt.append([
                res["english_pdf"], res["translated_pdf"], res["granularity"],
                row["topic_code"], row["topic_title"],
                row["master_count"], row["target_count"], row["status"],
            ])

    # --------------------------------------------------------
    # TAB 6: META DATA
    # --------------------------------------------------------
    ws_meta = wb.create_sheet(title="Meta Data")
    ws_meta.append([h for _k, h, _w, _a in MetaData.SUMMARY_COLUMNS]
                   + ["Columns measured on", "Notes"])
    if metadata_rows:
        master = metadata_rows[0]
        for i, r in enumerate(metadata_rows):
            note = "English master" if i == 0 else ""
            if i and r.get("pages") != master.get("pages"):
                note = f"page count differs from the master ({master.get('pages')})"
            if i and r.get("sheet") != master.get("sheet"):
                note = (note + "; " if note else "") + "sheet size differs from the master"
            if r.get("error"):
                note = (note + "; " if note else "") + r["error"]
            ws_meta.append([r.get(k, "-") for k, _h, _w, _a in MetaData.SUMMARY_COLUMNS]
                           + [r.get("sample_note", "-"), note])
    else:
        ws_meta.append(["(metadata was not collected for this run)"])

    # --------------------------------------------------------
    # TAB 7: STYLESHEET RESULT  (the Region Inspector's checks)
    # --------------------------------------------------------
    ws_style = wb.create_sheet(title="Stylesheet Result")
    ws_style.append([
        "English Master PDF", "Translated PDF", "Region", "Applies To",
        "Variant Group", "Master Page", "Translated Page", "Match Type",
        "Similarity / Found", "Shift (pt)", "Status",
    ])
    if region_results:
        from core.templates import describe_scope
        for r in region_results:
            scoped = r.get("scoped")
            match_type = ("Scoped exact match" if scoped
                          else "Visual (no text)" if r.get("dont_compare_text")
                          else "Exact match" if r.get("exact_required")
                          else "Text + visual")
            score = (f"found {r.get('needle_count')}x" if scoped
                     else f"{r.get('similarity', 0):.1f}%")
            ws_style.append([
                r.get("eng_name", ""), r.get("tr_name", ""),
                r.get("region_label", ""),
                describe_scope(r.get("page_scope")),
                r.get("variant_group") or "-",
                r.get("eng_page", "-"), r.get("target_page", "-"),
                match_type, score,
                f"{r.get('shift_y', 0.0):+.1f}",
                r.get("status", ""),
            ])
    else:
        ws_style.append([region_note or "No stylesheet regions were checked."])

    # --------------------------------------------------------
    # TAB: TEXT OVERLAP
    # --------------------------------------------------------
    # Applies to the master as much as the translations: an overrun caption in
    # the English original is a layout fault, not a translation fault.
    ws_ov = wb.create_sheet(title="Text Overlap")
    ws_ov.append([
        "Manual", "Page", "Colliding Pairs", "Text", "Why", "Area (pt2)", "Status",
    ])
    if overlap_results:
        for f in overlap_results:
            texts = f.get("texts") or [f.get("text_a", ""), f.get("text_b", "")]
            ws_ov.append([
                f.get("document", ""), f.get("page", ""), f.get("pairs", 1),
                "  X  ".join(t for t in texts if t)[:400],
                f.get("why", ""), f.get("overlap_pt2", 0), "FAIL",
            ])
    else:
        ws_ov.append(["No text found printed through other text.",
                      "", "", "", "First and last page are skipped by design.", "", "PASS"])

    # --------------------------------------------------------
    # TAB: NOT TRANSLATED
    # --------------------------------------------------------
    ws_un = wb.create_sheet(title="Not Translated")
    ws_un.append([
        "Manual", "Page", "Evidence", "English Text Found", "English Word Density", "Status",
    ])
    if untranslated_results:
        reasons = {
            "verbatim": "word for word in the English master",
            "dense": "reads as English by its function words",
            "verbatim + dense": "in the master AND reads as English",
        }
        for f in untranslated_results:
            ws_un.append([
                f.get("document", ""), f.get("page", ""),
                reasons.get(f.get("reason", ""), f.get("reason", "")),
                (f.get("text", "") or "")[:400],
                f.get("density", 0), "FAIL",
            ])
    else:
        ws_un.append(["No English text found in any translation.", "",
                      "First and last page are skipped: covers and back matter "
                      "are English by design.", "", "", "PASS"])

    # Format all sheets with headers, borders, and PASS/Present/Equal/Matched fills
    for ws in wb.worksheets:
        # Style Header Row
        for col_idx in range(1, ws.max_column + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        # Style Data Rows
        for row_idx in range(2, ws.max_row + 1):
            for col_idx in range(1, ws.max_column + 1):
                cell = ws.cell(row=row_idx, column=col_idx)
                cell.border = thin_border
                cell.alignment = Alignment(
                    horizontal="left" if (
                        (ws.title in ("TOC", "Images") and col_idx in (3, 4))
                        or (ws.title == "Text Overlap" and col_idx in (4, 5))
                        or (ws.title == "Not Translated" and col_idx in (3, 4))
                    ) else "center",
                    vertical="center"
                )

                val = str(cell.value or "")
                if val.startswith("PASS") or val.startswith("Present") or val.startswith("Equal") or val in ("Matched", "MATCH (PASS)", "Same Page"):
                    cell.fill = pass_fill
                    cell.font = pass_font
                elif val.startswith("FAIL") or val.startswith("Not Present") or val.startswith("Not Equal") or val.startswith("Not Matched") or val in ("CHECK", "MISSING") or "Missing:" in val or "Extra:" in val:
                    cell.fill = fail_fill
                    cell.font = fail_font

        # Auto-fit Column Widths
        for col in ws.columns:
            max_len = max(len(str(cell.value or '')) for cell in col)
            col_letter = openpyxl.utils.get_column_letter(col[0].column)
            if ws.title == "TOC" and col[0].column in (3, 4):
                ws.column_dimensions[col_letter].width = 50
            elif ws.title == "Images" and col[0].column in (1, 2, 3):
                ws.column_dimensions[col_letter].width = 30
            elif ws.title == "Text Overlap" and col[0].column in (4, 5):
                ws.column_dimensions[col_letter].width = 60
            elif ws.title == "Not Translated" and col[0].column in (3, 4):
                ws.column_dimensions[col_letter].width = 60
            else:
                ws.column_dimensions[col_letter].width = max(max_len + 3, 14)

    os.makedirs(os.path.dirname(output_excel_path), exist_ok=True)
    try:
        wb.save(output_excel_path)
        print(f"  [Unified Inspection Excel Report Saved]: {os.path.abspath(output_excel_path)}")
    except PermissionError:
        alt_path = output_excel_path.replace(".xlsx", "_new.xlsx")
        wb.save(alt_path)
        print(f"  [Unified Inspection Excel Report Saved]: {os.path.abspath(alt_path)} (locked file fallback)")


def resolve_master_pdf(path):
    """
    The single master PDF behind an English path, which may be a file or folder.

    Returns (pdf_path, error). A folder holding more than one PDF is an error
    rather than a guess: picking one silently would mean a whole run - crops,
    counts, regions, the report - was measured against a document the user did
    not choose, and nothing downstream would say so.
    """
    if not path:
        return None, "No English master configured."
    if os.path.isfile(path):
        if not path.lower().endswith(".pdf"):
            return None, f"The English master is not a PDF: {os.path.basename(path)}"
        return path, None
    if not os.path.isdir(path):
        return None, f"English path not found: {path}"

    pdfs = sorted(f for f in os.listdir(path) if f.lower().endswith(".pdf"))
    if not pdfs:
        return None, f"No PDF found in the English folder:\n{path}"
    if len(pdfs) > 1:
        listing = "\n".join(f"  - {f}" for f in pdfs[:8])
        more = f"\n  ...and {len(pdfs) - 8} more" if len(pdfs) > 8 else ""
        return None, (f"The English folder has more than one PDF ({len(pdfs)}).\n"
                      f"Leave exactly one master in the folder and run again.\n\n"
                      f"{listing}{more}")
    return os.path.join(path, pdfs[0]), None


# ============================================================
# MASTER BATCH RUNNER
# ============================================================

def run_quality_inspection(source_pdf_path, translated_path_or_folder, output_dir,
                           margins=None, regions=None, collect_metadata=True,
                           progress=None):
    """
    Run unified TOC, Barcode/QR and Images inspection across all translated PDFs.

    `margins` is the ignored-margin block from the selected stylesheet template
    (header / footer / left / right, in points). It governs both the crop step
    and the symmetric count step, which have to agree about what counts as page
    furniture. Omitted, the built-in defaults apply.
    """
    source_pdf_path, err = resolve_master_pdf(source_pdf_path)
    if err:
        print(f"ERROR: {err}")
        return {"error": err}

    active_margins = PageMargins.normalize(margins)

    # A run of a dozen manuals is minutes of work. Every stage reports where it
    # has got to, so the interface can show a moving bar with a name on it
    # instead of a spinner that looks identical to a crash.
    def say(fraction, message):
        if progress:
            try:
                progress(fraction, message)
            except Exception:
                pass

    def stage_progress(base, span, label):
        """A per-page callback that maps into this stage's slice of the bar."""
        def cb(done, total, note=""):
            frac = base + span * (done / float(total or 1))
            say(frac, f"{label} — {note} {done}/{total}" if note else f"{label} {done}/{total}")
        return cb

    say(0.0, "Starting")

    print("=" * 80)
    print("UNIFIED PDF QUALITY & VISUAL INSPECTION ENGINE (MAIN)")
    print("=" * 80)
    print(f"Master English Source: {os.path.basename(source_pdf_path)}")
    print(f"Translated Target    : {translated_path_or_folder}")
    print(f"Output Directory     : {output_dir}")
    print(f"Ignored Margins      : {PageMargins.describe(active_margins)}")
    print("-" * 80)

    # 1. Discover translated PDFs
    translated_files = []
    if os.path.isfile(translated_path_or_folder):
        translated_files.append(translated_path_or_folder)
    elif os.path.isdir(translated_path_or_folder):
        for f in os.listdir(translated_path_or_folder):
            if f.lower().endswith(".pdf"):
                translated_files.append(os.path.join(translated_path_or_folder, f))
    else:
        print(f"ERROR: Invalid translated path or folder: {translated_path_or_folder}")
        return

    if not translated_files:
        print("ERROR: No translated PDF files found to inspect!")
        return

    # 2. Extract Master Source Models & Crop Graphic Elements
    #
    # The master is scanned once here - barcodes, QR codes and tables - and the
    # result is cached for every stage and every translation that follows. It
    # used to be re-scanned by the barcode check on each of the eleven
    # translations, which on a 92-page manual was most of the run.
    print("Extracting Master Models for Source PDF...")
    say(0.01, "Scanning the master")
    DocScan.forget()
    DocScan.prepare(source_pdf_path, tables=False, codes=True,
                    progress=stage_progress(0.01, 0.12, "Scanning the master"))

    source_toc_numerics = TOC.extract_toc_numerics(source_pdf_path)
    source_count_model = ImageCounts.count_images_by_topic(source_pdf_path, margins=active_margins)

    # Crop pure graphic elements from Master English PDF for image comparison
    eng_crops_out_dir = os.path.join(output_dir, "Cropped_Images")
    pdf_name_no_ext = os.path.splitext(os.path.basename(source_pdf_path))[0]
    eng_crops_dir = os.path.join(eng_crops_out_dir, pdf_name_no_ext)

    print(f"Extracting Pure Graphic Crops from Source PDF...")
    crop_pdf_images.crop_pdf_elements(
        source_pdf_path, eng_crops_out_dir, margins=active_margins,
        progress=stage_progress(0.13, 0.07, "Cropping the master"))

    print(f"  Source Topics        : {len(source_toc_numerics)} sections")
    print(f"  Source Images        : {source_count_model['total']} "
          f"({'per-topic' if source_count_model['has_toc'] else 'document total - no TOC'})")
    print()

    # 3. Inspect each translated PDF across all modules
    print(f"Inspecting {len(translated_files)} Translated PDF(s)...")
    print()

    toc_results = []
    bc_qr_results = []
    count_results = []
    img_results_summary = []
    img_crop_details_list = []

    diff_crops_out_dir = os.path.join(output_dir, "Cropped_Comparison")

    # Documents occupy the bar from 20% to 82%; the text checks, the metadata
    # and the report share what is left.
    DOC_BASE, DOC_SPAN = 0.20, 0.62
    doc_slice = DOC_SPAN / max(1, len(translated_files))

    for idx, tr_path in enumerate(translated_files, start=1):
        tr_filename = os.path.basename(tr_path)
        here = DOC_BASE + doc_slice * (idx - 1)
        say(here, f"{tr_filename}  ({idx} of {len(translated_files)})")
        # Scan this translation once, up front, for the three stages below.
        DocScan.prepare(tr_path, tables=False, codes=True,
                        progress=stage_progress(here, doc_slice * 0.35,
                                                f"Scanning {tr_filename}"))

        # 1. TOC check
        try:
            target_toc_numerics = TOC.extract_toc_numerics(tr_path)
            toc_res = TOC.compare_toc_numerics(source_toc_numerics, target_toc_numerics)
            toc_res["english_pdf"] = os.path.basename(source_pdf_path)
            toc_res["translated_pdf"] = tr_filename
        except Exception as e:
            target_toc_numerics = []
            toc_res = {"english_pdf": os.path.basename(source_pdf_path), "translated_pdf": tr_filename,
                       "diff_msg": str(e), "status": "FAIL", "target_numerics_str": ""}
        toc_results.append(toc_res)

        # 2. Barcode & QR Code Count Check
        try:
            bc_res = Barcode_QR_Check.compare_barcode_qr(source_pdf_path, tr_path)
        except Exception as e:
            bc_res = {"english_pdf": os.path.basename(source_pdf_path), "translated_pdf": tr_filename, "master_barcode_count": 0, "target_barcode_count": 0, "master_pages_barcode": [], "target_pages_barcode": [], "barcode_status": "FAIL", "master_qr_count": 0, "target_qr_count": 0, "master_pages_qr": [], "target_pages_qr": [], "qr_status": "FAIL", "overall_verdict": "FAIL"}
        bc_qr_results.append(bc_res)

        # 3. Symmetric Image Count Check (per topic, or document total)
        try:
            cnt_res = ImageCounts.compare_image_counts(
                source_count_model,
                ImageCounts.count_images_by_topic(tr_path, margins=active_margins))
        except Exception as e:
            cnt_res = {"english_pdf": os.path.basename(source_pdf_path),
                       "translated_pdf": tr_filename, "granularity": "unavailable",
                       "master_total": 0, "target_total": 0, "mismatched_topics": [],
                       "rows": [], "overall_verdict": "FAIL", "status": f"FAIL ({e})"}
        count_results.append(cnt_res)

        # 4. Pure Visual Graphic Images Comparison
        try:
            img_res = Compare_cropped_images.compare_english_crops_with_translated_pdf(
                eng_crop_dir=eng_crops_dir,
                trans_pdf_path=tr_path,
                output_dir=diff_crops_out_dir,
                progress=stage_progress(here + doc_slice * 0.45, doc_slice * 0.5,
                                        f"Images: {tr_filename}"),
            )
        except Exception as e:
            img_res = {"trans_name": tr_filename, "total_crops": 0, "matched_crops": 0, "match_pct": 0.0, "overall_status": "FAIL", "crop_details": []}
        img_results_summary.append(img_res)

        # Unpack per-crop details for Images tab
        for cd in img_res.get("crop_details", []):
            crop_name = cd["crop_file"]
            image_type = "table_image" if "table_image" in crop_name.lower() else "normal image"
            img_crop_details_list.append({
                "english_pdf": os.path.basename(source_pdf_path),
                "translated_pdf": tr_filename,
                "crop_name": crop_name,
                "image_type": image_type,
                "topic": cd.get("topic", ""),
                "eng_page": cd["eng_page"],
                "trans_page": cd["trans_page"],
                "shift_info": cd["shift_info"],
                "similarity": cd["match_pct"],
                "status": cd["status"],
                "match_img": cd.get("match_img", ""),
            })

        master_pass = (
            toc_res["status"] == "PASS" and
            bc_res["overall_verdict"] == "PASS" and
            img_res["overall_status"] == "PASS" and
            cnt_res["overall_verdict"] == "PASS"
        )
        master_verdict = "PASS" if master_pass else "FAIL"

        print(
            f"  [{idx:02d}/{len(translated_files):02d}] {tr_filename[:34]:<34} | "
            f"TOC: {toc_res['status']:<4} | "
            f"BC/QR: {bc_res['overall_verdict']:<4} | "
            f"Img: {img_res['overall_status']:<4} | "
            f"Count: {cnt_res['overall_verdict']:<4} | "
            f"Master: {master_verdict}"
        )

    print()
    print("-" * 80)

    # 4. Stylesheet regions, from the template or whatever is on screen.
    #    Skipped rather than failed when there are none: the other four checks
    #    are still worth having, and the report says the section was skipped so
    #    a clean run cannot be mistaken for a complete one.
    region_results = []
    region_note = ""
    if regions:
        print(f"Checking {len(regions)} stylesheet region(s) across the translated set...")
        try:
            region_results = RegionEngine.run_batch_multiple_regions_check(
                eng_pdf_path=source_pdf_path,
                tr_target=translated_path_or_folder,
                regions=regions,
                output_crops_dir=os.path.join(diff_crops_out_dir, "Region_Inspector"),
            ) or []
            passed = sum(1 for r in region_results if r.get("is_match"))
            region_note = f"{passed}/{len(region_results)} region checks passed"
            print(f"  {region_note}")
        except Exception as e:
            region_note = f"stylesheet check failed: {e}"
            print(f"  ERROR: {region_note}")
    else:
        region_note = ("No stylesheet template selected and no regions marked - "
                       "the stylesheet check was skipped.")
        print(region_note)
    print()

    # 5. Text checks: colliding text, and English left in a translation.
    #
    # Neither is about a place on the page, so neither can be a stylesheet
    # region: a collision happens wherever a line runs long, and a missed
    # segment is wherever the translator's eye slipped. Both skip the first and
    # last page - covers and back matter are English by design and set by hand.
    #
    # The overlap scan reads the MASTER as well. An overrun caption in the
    # English original is a layout fault that every translation will inherit,
    # and finding it here is cheaper than finding it eleven times.
    print("Checking for colliding and untranslated text...")
    say(0.82, "Text checks")
    text_evidence_dir = os.path.join(diff_crops_out_dir, "Text_Checks")
    overlap_results, untranslated_results = [], []
    try:
        every_document = [source_pdf_path] + translated_files
        for i, path in enumerate(every_document):
            say(0.82 + 0.04 * (i / float(len(every_document))),
                f"Overlap: {os.path.basename(path)}")
            overlap_results.extend(TextOverlap.find_overlaps(
                path, skip_first_last=True,
                evidence_dir=os.path.join(text_evidence_dir, "overlap")))
        print(f"  {len(overlap_results)} text overlap(s) across "
              f"{len(every_document)} document(s)")
    except Exception as e:
        print(f"  ERROR: the overlap check failed: {e}")

    try:
        inventory = Untranslated.master_inventory(source_pdf_path)
        for i, path in enumerate(translated_files):
            say(0.86 + 0.04 * (i / float(len(translated_files) or 1)),
                f"Not translated: {os.path.basename(path)}")
            untranslated_results.extend(Untranslated.find_untranslated(
                path, inventory, skip_first_last=True,
                evidence_dir=os.path.join(text_evidence_dir, "untranslated")))
        print(f"  {len(untranslated_results)} untranslated line(s) across "
              f"{len(translated_files)} translation(s)")
    except Exception as e:
        print(f"  ERROR: the untranslated-text check failed: {e}")
    print()

    # 6. Document metadata for the master and every translation.
    metadata_rows = []
    if collect_metadata:
        print("Collecting document metadata...")
        say(0.90, "Measuring documents")
        try:
            metadata_rows = MetaData.collect(
                [source_pdf_path] + translated_files, margins=active_margins,
                progress=lambda i, n, name: say(0.90 + 0.07 * (i / float(n or 1)),
                                                f"Measuring {name} ({i}/{n})"))
            print(f"  {len(metadata_rows)} document(s) measured")
        except Exception as e:
            print(f"  ERROR: metadata scan failed: {e}")
        print()

    # 7. Generate ONE single Excel report
    unified_report_path = os.path.join(output_dir, "PDF_Quality_Inspection_Report.xlsx")
    say(0.97, "Writing the report")
    generate_unified_excel_report(
        toc_results,
        bc_qr_results,
        img_results_summary,
        img_crop_details_list,
        count_results,
        unified_report_path,
        run_margins=active_margins,
        metadata_rows=metadata_rows,
        region_results=region_results,
        region_note=region_note,
        overlap_results=overlap_results,
        untranslated_results=untranslated_results,
    )

    # The workers have nothing left to do; hand the cores back before the
    # interface starts drawing results into four tabs.
    DocScan.shutdown_pool()

    print("=" * 80)
    print("UNIFIED QUALITY & VISUAL INSPECTION COMPLETE")
    print("=" * 80)

    # Handed back so the GUI can show the comparison images in-app rather than
    # leaving the user to dig through the output folder.
    return {
        "english_pdf": os.path.basename(source_pdf_path),
        "report_path": unified_report_path,
        "toc_results": toc_results,
        "bc_qr_results": bc_qr_results,
        "img_results_summary": img_results_summary,
        "img_crop_details": img_crop_details_list,
        "count_results": count_results,
        "region_results": region_results,
        "region_note": region_note,
        "metadata_rows": metadata_rows,
        "overlap_results": overlap_results,
        "untranslated_results": untranslated_results,
    }


# ============================================================
# ENTRY POINT CONFIGURATION
# ============================================================

if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="SpotCheck - unified PDF quality & visual inspection engine."
    )
    parser.add_argument(
        "--english", default=os.path.join("Input", "English"),
        help="Master English PDF, or a folder containing exactly one (default: Input/English)")
    parser.add_argument(
        "--translated", default=os.path.join("Input", "Translated"),
        help="Translated PDF file or folder (default: Input/Translated)")
    parser.add_argument(
        "--output", default="Output",
        help="Output directory for the report and comparison images (default: Output)")
    parser.add_argument(
        "--template", default=None,
        help="Saved stylesheet template to take the ignored page margins from")
    args = parser.parse_args()

    run_margins = None
    if args.template:
        from core import templates as templates_store
        tpl = templates_store.load_template(args.template)
        if tpl is None:
            print(f"ERROR: No such stylesheet template: {args.template}")
            sys.exit(1)
        run_margins = tpl.get("margins")

    source_pdf = args.english
    if os.path.isdir(source_pdf):
        pdfs = sorted(f for f in os.listdir(source_pdf) if f.lower().endswith(".pdf"))
        if not pdfs:
            print(f"ERROR: No PDF found in English source folder: {source_pdf}")
            sys.exit(1)
        if len(pdfs) > 1:
            print(f"ERROR: Expected one master PDF in {source_pdf}, found {len(pdfs)}.")
            print("       Pass the master explicitly with --english <file.pdf>.")
            sys.exit(1)
        source_pdf = os.path.join(source_pdf, pdfs[0])

    run_quality_inspection(
        source_pdf_path=source_pdf,
        translated_path_or_folder=args.translated,
        output_dir=args.output,
        margins=run_margins,
    )