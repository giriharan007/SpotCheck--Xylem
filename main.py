"""
main.py

Unified Entry Point for PDF Quality & Consistency Inspection.

Imports and coordinates:
  - FirstPage.py : Checks Page 1 (Page Size, Version, Doc Length, Language Code, Barcode, QR Code)
  - TOC.py       : Checks Table of Contents Topic Numerics & Flags Missing Sections

Generates:
  - PDF_Quality_Inspection_Report.xlsx  : Multi-sheet report (Master Summary, Extracted Values, First Page, TOC)
  - PDF_Extracted_Values_Report.xlsx    : Dedicated Values Report with QR Code, Bar Code, Language Code, Version, Document Number, TOC numerics
"""

import os
import sys
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

import FirstPage

try:
    import TOC
except ImportError:
    import Toc as TOC


# ============================================================
# EXCEL REPORT GENERATORS
# ============================================================

def generate_extracted_values_excel_report(values_data, output_excel_path):
    """
    Generate a dedicated Excel report containing the ACTUAL EXTRACTED VALUES
    for QR Code, Barcode, Language Code, Version, Document Number, Document Length, and TOC Numerics.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Extracted Values"

    headers = [
        "English Master PDF",
        "Translated PDF",
        "QR Code",
        "Barcode",
        "Language Code",
        "Version",
        "Document Number",
        "Document Length",
        "TOC Topic Numerics",
    ]
    ws.append(headers)

    # Styles
    header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")

    thin_border = Border(
        left=Side(style="thin", color="D3D3D3"),
        right=Side(style="thin", color="D3D3D3"),
        top=Side(style="thin", color="D3D3D3"),
        bottom=Side(style="thin", color="D3D3D3")
    )

    # Header styling
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # Append Data Rows
    for row_idx, item in enumerate(values_data, start=2):
        row_data = [
            item["english_pdf"],
            item["translated_pdf"],
            item["qr_code"],
            item["barcode"],
            item["language_code"],
            item["version"],
            item["document_number"],
            item["document_length"],
            item["toc_numerics"],
        ]
        ws.append(row_data)

        for col_idx in range(1, len(row_data) + 1):
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.border = thin_border
            cell.alignment = Alignment(horizontal="center" if col_idx < 9 else "left", vertical="center")

    # Auto-adjust column widths
    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = openpyxl.utils.get_column_letter(col[0].column)
        # Cap TOC column width for clean display
        if col[0].column == 9:
            ws.column_dimensions[col_letter].width = 50
        else:
            ws.column_dimensions[col_letter].width = max(max_len + 3, 14)

    os.makedirs(os.path.dirname(output_excel_path), exist_ok=True)
    try:
        wb.save(output_excel_path)
        print(f"  [Extracted Values Excel Report Saved]: {os.path.abspath(output_excel_path)}")
    except PermissionError:
        alt_path = output_excel_path.replace(".xlsx", "_new.xlsx")
        wb.save(alt_path)
        print(f"  [Extracted Values Excel Report Saved]: {os.path.abspath(alt_path)} (locked file fallback)")


def generate_unified_excel_report(fp_results, toc_results, values_data, output_excel_path):
    """
    Generate ONE single unified Excel report containing 4 worksheets:
      1. Master Summary          : Executive overview of First Page + TOC checks
      2. Extracted Values        : Mentioned values for QR, Barcode, Lang, Version, Doc Num, TOC
      3. First Page Inspection   : Detailed Page 1 metadata comparison
      4. TOC Numerics Inspection : Detailed TOC topic numerics & missing topic checks
    """
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
    # SHEET 1: MASTER SUMMARY
    # --------------------------------------------------------
    ws_summary = wb.active
    ws_summary.title = "Master Summary"

    headers_summary = [
        "English Master PDF",
        "Translated PDF",
        "Language Code",
        "First Page Check",
        "TOC Numerics Check",
        "Master Verdict",
    ]
    ws_summary.append(headers_summary)

    for i in range(len(fp_results)):
        fp_res = fp_results[i]
        toc_res = toc_results[i]

        fp_status = fp_res["overall_verdict"]
        toc_status = toc_res["status"]
        master_verdict = "PASS" if (fp_status == "PASS" and toc_status == "PASS") else "FAIL"

        row_data = [
            fp_res["english_pdf"],
            fp_res["translated_pdf"],
            fp_res["language"],
            fp_status,
            toc_status,
            master_verdict,
        ]
        ws_summary.append(row_data)

    # --------------------------------------------------------
    # SHEET 2: EXTRACTED VALUES
    # --------------------------------------------------------
    ws_val = wb.create_sheet(title="Extracted Values")
    headers_val = [
        "English Master PDF",
        "Translated PDF",
        "QR Code",
        "Barcode",
        "Language Code",
        "Version",
        "Document Number",
        "Document Length",
        "TOC Topic Numerics",
    ]
    ws_val.append(headers_val)

    for item in values_data:
        row_data = [
            item["english_pdf"],
            item["translated_pdf"],
            item["qr_code"],
            item["barcode"],
            item["language_code"],
            item["version"],
            item["document_number"],
            item["document_length"],
            item["toc_numerics"],
        ]
        ws_val.append(row_data)

    # --------------------------------------------------------
    # SHEET 3: FIRST PAGE INSPECTION
    # --------------------------------------------------------
    ws_fp = wb.create_sheet(title="First Page Inspection")

    headers_fp = [
        "English Master PDF",
        "Translated PDF",
        "Language Code",
        "Page Size",
        "Page Size Check",
        "Version",
        "Version Check",
        "Doc Num Length",
        "Doc Len Check",
        "Language Check",
        "Barcode Check",
        "QR Code Check",
        "First Page Verdict",
    ]
    ws_fp.append(headers_fp)

    for res in fp_results:
        row_data = [
            res["english_pdf"],
            res["translated_pdf"],
            res["language"],
            res["page_size_val"],
            res["page_size_status"],
            res["version_val"],
            res["version_status"],
            res["doc_len_val"],
            res["doc_len_status"],
            res["language_status"],
            res["barcode_status"],
            res["qr_status"],
            res["overall_verdict"],
        ]
        ws_fp.append(row_data)

    # --------------------------------------------------------
    # SHEET 4: TOC NUMERICS INSPECTION
    # --------------------------------------------------------
    ws_toc = wb.create_sheet(title="TOC Numerics Inspection")

    headers_toc = [
        "English Master PDF",
        "Translated PDF",
        "English Topic Count",
        "Translated Topic Count",
        "TOC Sequence Check",
        "Missing Topics / Differences",
    ]
    ws_toc.append(headers_toc)

    for res in toc_results:
        row_data = [
            res["english_pdf"],
            res["translated_pdf"],
            res["english_topic_count"],
            res["translated_topic_count"],
            res["status"],
            res["diff_msg"],
        ]
        ws_toc.append(row_data)

    # Format all sheets with headers and PASS/FAIL fills
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
                    horizontal="left" if (ws.title == "Extracted Values" and col_idx == 9) else "center",
                    vertical="center"
                )

                val = str(cell.value or "")
                if val == "PASS":
                    cell.fill = pass_fill
                    cell.font = pass_font
                elif "FAIL" in val or "Missing:" in val or "Extra:" in val:
                    cell.fill = fail_fill
                    cell.font = fail_font

        # Auto-fit Column Widths
        for col in ws.columns:
            max_len = max(len(str(cell.value or '')) for cell in col)
            col_letter = openpyxl.utils.get_column_letter(col[0].column)
            if ws.title == "Extracted Values" and col[0].column == 9:
                ws.column_dimensions[col_letter].width = 50
            else:
                ws.column_dimensions[col_letter].width = max(max_len + 3, 14)

    os.makedirs(os.path.dirname(output_excel_path), exist_ok=True)
    try:
        wb.save(output_excel_path)
        print(f"  [Unified Excel Inspection Report Saved]: {os.path.abspath(output_excel_path)}")
    except PermissionError:
        alt_path = output_excel_path.replace(".xlsx", "_new.xlsx")
        wb.save(alt_path)
        print(f"  [Unified Excel Inspection Report Saved]: {os.path.abspath(alt_path)} (locked file fallback)")


# ============================================================
# MASTER BATCH RUNNER
# ============================================================

def run_quality_inspection(source_pdf_path, translated_path_or_folder, output_dir):
    """
    Run Unified FirstPage, TOC, and Extracted Values Report across translated PDFs.
    """
    if not os.path.exists(source_pdf_path):
        print(f"ERROR: Source English PDF not found: {source_pdf_path}")
        return

    print("=" * 75)
    print("UNIFIED PDF QUALITY & VALUES INSPECTION (MAIN)")
    print("=" * 75)
    print(f"Master English Source: {os.path.basename(source_pdf_path)}")
    print(f"Translated Target    : {translated_path_or_folder}")
    print(f"Output Directory     : {output_dir}")
    print("-" * 75)

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

    # 2. Extract Master Source Models
    print("Extracting Master Models for Source PDF...")
    source_fp_model = FirstPage.extract_page_model(source_pdf_path)
    source_toc_numerics = TOC.extract_toc_numerics(source_pdf_path)
    print(f"  Source Language : {source_fp_model['language']}")
    print(f"  Source Version  : {source_fp_model['version']}")
    print(f"  Source Doc Num  : {source_fp_model['document_number']} (len {source_fp_model['document_length']})")
    print(f"  Source Size     : {source_fp_model['page_size'][0]} x {source_fp_model['page_size'][1]}")
    print(f"  Source Topics   : {len(source_toc_numerics)} sections ({', '.join(source_toc_numerics[:6])}...)")
    print()

    # 3. Inspect each translated PDF
    print(f"Inspecting {len(translated_files)} Translated PDF(s)...")
    print()

    fp_results = []
    toc_results = []
    values_data = []

    for idx, tr_path in enumerate(translated_files, start=1):
        tr_filename = os.path.basename(tr_path)
        try:
            # FirstPage check
            target_fp_model = FirstPage.extract_page_model(tr_path)
            fp_res = FirstPage.compare_page_models(source_fp_model, target_fp_model)
            fp_results.append(fp_res)

            # TOC check
            target_toc_numerics = TOC.extract_toc_numerics(tr_path)
            toc_res = TOC.compare_toc_numerics(source_toc_numerics, target_toc_numerics)
            toc_res["english_pdf"] = os.path.basename(source_pdf_path)
            toc_res["translated_pdf"] = tr_filename
            toc_results.append(toc_res)

            # Collect Extracted Values for dedicated values report
            values_data.append({
                "english_pdf": os.path.basename(source_pdf_path),
                "translated_pdf": tr_filename,
                "qr_code": target_fp_model.get("qr_data", "Missing"),
                "barcode": target_fp_model.get("barcode_data", "Missing"),
                "language_code": target_fp_model.get("language", "Missing"),
                "version": target_fp_model.get("version", "N/A"),
                "document_number": target_fp_model.get("document_number", "N/A"),
                "document_length": target_fp_model.get("document_length", "N/A"),
                "toc_numerics": ", ".join(target_toc_numerics) if target_toc_numerics else "None",
            })

            master_verdict = "PASS" if (fp_res["overall_verdict"] == "PASS" and toc_res["status"] == "PASS") else "FAIL"

            print(
                f"  [{idx:02d}/{len(translated_files):02d}] {tr_filename[:32]:<32} | "
                f"Lang: {fp_res['language']:<4} | "
                f"FirstPage: {fp_res['overall_verdict']:<4} | "
                f"TOC: {toc_res['status']:<4} | "
                f"Master: {master_verdict}"
            )
        except Exception as e:
            print(f"  [{idx:02d}/{len(translated_files):02d}] {tr_filename} -> ERROR: {str(e)}")

    print()
    print("-" * 75)

    # 4. Generate Reports
    unified_report_path = os.path.join(output_dir, "PDF_Quality_Inspection_Report.xlsx")
    values_report_path = os.path.join(output_dir, "PDF_Extracted_Values_Report.xlsx")

    generate_unified_excel_report(fp_results, toc_results, values_data, unified_report_path)
    generate_extracted_values_excel_report(values_data, values_report_path)

    print("=" * 75)
    print("PDF QUALITY & VALUES INSPECTION COMPLETE")
    print("=" * 75)


# ============================================================
# ENTRY POINT CONFIGURATION
# ============================================================

if __name__ == "__main__":
    if sys.stdout.encoding != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")

    # =======================================================
    # MASTER CONFIGURATION OPTIONS
    # =======================================================

    # 1. Master English Source PDF
    SOURCE_ENGLISH_PDF = (
        r"C:\Xylem Project\spot\input\English\894387_5.0_en-US_2026-04_IOM.Start350.pdf"
    )

    # 2. Translated Target: Path to a SINGLE translated PDF file OR folder containing translated PDFs
    TRANSLATED_TARGET = (
        r"C:\Xylem Project\spot\input\Translated"
    )

    # 3. Output directory for the Excel reports
    OUTPUT_DIRECTORY = (
        r"C:\Xylem Project\spot\output"
    )

    # Execute main inspection
    run_quality_inspection(
        source_pdf_path=SOURCE_ENGLISH_PDF,
        translated_path_or_folder=TRANSLATED_TARGET,
        output_dir=OUTPUT_DIRECTORY
    )
