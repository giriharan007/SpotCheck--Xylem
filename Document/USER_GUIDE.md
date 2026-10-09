# SpotCheck — End-User Operational Guide

Welcome to the **SpotCheck Quality Assurance**. This user guide is written from a documentation reviewer's and quality assurance specialist's perspective. It details how to use SpotCheck to verify multi-language technical manuals against the English Master PDF, navigate findings in the interactive desktop GUI, use the dual-canvas Side-by-Side viewer, and interpret the generated 10-sheet Excel report.

---

## Table of Contents
1. [Overview & Key Capabilities](#1-overview--key-capabilities)
2. [Getting Started & Installation](#2-getting-started--installation)
3. [User Interface Overview](#3-user-interface-overview)
4. [Step-by-Step Inspection Workflow](#4-step-by-step-inspection-workflow)
5. [Navigating the Review Tab](#5-navigating-the-review-tab)
6. [Using the Side-by-Side Dual-Page Viewer](#6-using-the-side-by-side-dual-page-viewer)
7. [Working with the Region Inspector](#7-working-with-the-region-inspector)
8. [Auditing Document Metadata](#8-auditing-document-metadata)
9. [Interpreting the 10-Sheet Excel QA Report](#9-interpreting-the-10-sheet-excel-qa-report)
10. [Bitbucket Location](#10-bitbucket-location)  


---

## 1. Overview & Key Capabilities

When technical manuals are translated and prepared in Desktop Publishing (DTP) software, common defects occur:
- **TOC section numbering** gets dropped, reordered, or misaligned.
- **Hyperlinks and cross-references** point to wrong pages or disappear in translation.
- **Diagrams and safety icons** get deleted, displaced, or duplicated.
- **Barcodes or QR codes** are omitted or localized incorrectly.
- **Text expansions** cause text collisions, overlaps, or margin bleed.
- **Untranslated English phrases** are accidentally left behind in foreign text.

**SpotCheck** automates the detection of all these defects across any number of translated languages simultaneously.
---

## 2. Getting Started & Installation


### Running the Standalone Executable

1. Navigate to `dist\SpotCheck\`.
2. Double-click `SpotCheck.exe`.
3. No Python environment is required.

---

## 3. User Interface Overview

SpotCheck opens in a modern desktop interface

The application is structured into four primary tabs:
1. **Inspection Tab**: The command center. Choose input/output paths, select a stylesheet template, click to run inspections, watch real-time console logs, and open reports.
2. **Review Tab**: The interactive findings gallery. Filter results by status (*Passed* vs *Needs Review*), inspect detailed cards, view comparison images, and launch the Side-by-Side viewer.
3. **Region Inspector Tab**: Interactive ROI (Region of Interest) editor. View master pages, draw boxes around headers, footers, logos, or serial numbers, configure tolerances, and save stylesheet templates.
4. **Meta Data Tab**: High-level and per-page document geometry auditor (page counts, sheet sizes, columns, and gutter measurements).

---

## 4. Step-by-Step Inspection Workflow

```
┌────────────────────────────────────────────────────────────────────────┐
│                        4-Step QA Workflow                              │
│                                                                        │
│   [1. Select Master] ──► [2. Choose Template] ──► [3. Run Scan] ──► [4. Review & Export]
│    (Auto-detects         (e.g., 10_xylem)           (Real-time       (Review Tab &
│     all translations)                                progress %)      10-sheet Excel)
└────────────────────────────────────────────────────────────────────────┘
```

### Step 1: Configure Paths
1. Open the **Inspection** tab.
2. Under **English Master PDF**, click **Browse...** and select your master English PDF.
   > **Pro Tip ("One Folder = One Batch"):** When you select the master PDF, SpotCheck automatically scans its folder and designates all other sibling `.pdf` files as translations. You do not need to configure translation paths manually!
3. Under **Output Directory**, SpotCheck automatically selects a folder named `SpotCheck_Output` inside your batch directory. You can also click **Browse...** to specify a custom folder.
   > **Persistent Settings:** SpotCheck automatically remembers your configured paths between sessions.

### Step 2: Select a Stylesheet Template
- In the **Stylesheet Template** dropdown, select the template matching your manual series (for example, `10_xylem.template.json` or `(none)` if running without predefined region checks).
- Loading a template automatically applies pre-calibrated header/footer margins and inspection zones.

### Step 3: Run the Inspection
- Click the large green button: **▶ Run Full Inspection**.
- **Live Feedback**: The progress bar updates with real-time percentage completion and named phases:
  - *Scanning the master* (caching barcodes and vector tables)
  - *TOC topic validation*
  - *Hyperlink extraction*
  - *Extracting graphics and diagram crops*
  - *Scanning translation manuals*
  - *Cross-page graphic template matching*
  - *Text collision and untranslated phrase detection*
  - *Generating 10-sheet Excel report*
- The built-in console streams detailed progress logs.

### Step 4: Access Your Results
Once the status indicator turns green (**● Ready**), you can:
- Click **Open Excel Report** to view `PDF_Quality_Inspection_Report.xlsx as detailed report.
- Click **Open Output Folder** to explore cropped graphics and comparison evidence PNGs.
- Click over to the **Review** tab to inspect all findings visually inside the app.

---

## 5. Navigating the Review Tab

The **Review Tab** brings all 10 inspection modules together into a unified, responsive interface without requiring you to hunt through folders.

### Verdict Filters & Live Counts
At the top-left of the Review tab, select your filter:
- ** Review **: Shows only items requiring reviewer attention (missing topics, missing links, image mismatches, text overlaps).
- **Passed**: Shows confirmed matches.
- Live pill counters display the exact number of items in each category.

### Color-Coded Check Tints
Each inspection check is highlighted with a soft category tint for instant visual recognition:

| Check Category | Tint Color | What It Verifies |
| :--- | :--- | :--- |
| **TOC** | Soft Pink | Outline topic sequence, missing numbers, hierarchy |
| **Links** | Soft Ice Blue | External URLs, internal GoTo targets, QR links |
| **Images** | Lavender / Purple | Pure graphic template similarity (≥ 80.0%) |
| **Image Counts** | Mint Green | Symmetric graphic counts (detects additions/deletions) |
| **Region Checks** | Sky Blue | Stylesheet region layout shifts and exact tokens |
| **Barcode / QR** | Soft Indigo | Code presence, decodes, and page locations |
| **Overlap** | Teal Tint | Colliding text spans printed over other text |
| **Not Translated** | Warm Beige | English phrases accidentally left behind in translations |
| **Margin Overflow** | Soft Coral | Translated text spilling past stylesheet side margins |

### Review tab-Detail Layout & Keyboard Navigation
- The left/top pane lists items using a lightweight native table.
- Selecting any row instantly renders the corresponding **Comparison Card** and high-resolution preview image.
- **Keyboard Shortcuts**:
  - `↑` / `↓` : Step through rows and update the preview.
  - `PgUp` / `PgDn` : Jump 10 rows.
  - `Home` / `End` : Jump to the top or bottom of the list.
- **View Full Size**: Click on any comparison preview image to open it in your Windows default image viewer.
- **Open Side by Side**: Click the **Side by Side** button on any card to jump directly into the full-page dual viewer.

---

## 6. Using the Side-by-Side Dual-Page Viewer

When a graphic or hyperlink fails, you need to understand the full page context. Clicking **Side by Side** launches the dual-page inspection window.

```
┌─────────────────────────────────┬─────────────────────────────────┐
│     ENGLISH MASTER PDF          │      TRANSLATED PDF             │
│     Page 12 (Topic 2.3)         │      Page 14 (Topic 2.3)        │
│                                 │                                 │
│    ┌──────────────┐             │             [ MISSING ]         │
│    │  Figure 12   │             │       (Marked with red box)     │
│    └──────────────┘             │                                 │
│                                 │                                 │
└─────────────────────────────────┴─────────────────────────────────┘
```

### Viewer Capabilities:
1. **Dual Canvases**: Displays the English Master on the left and the Translated PDF on the right.
2. **Automatic Topic Alignment**: The viewer automatically accounts for DTP page reflow (e.g., if English Topic 2.3 is on Page 12, but German Topic 2.3 reflowed to Page 14, both sides open to their correct respective pages).
3. **Synchronized Scrolling**: Scrolling either canvas smoothly updates both pages in parallel.
4. **Coordinate-Anchored Resizing**: Maximizing or resizing the preview window preserves your exact point coordinates—no accidental jumps to the top of the page.
5. **View Modes**:
   - **Fit Width**: Scales pages to fill canvas width (recommended for reviewing text and small callouts).
   - **Fit Page**: Fits the entire page vertically in the window (recommended for overall geometry and layout balance).
   - **Zoom Controls**: Use `+`, `-`, and `100%` buttons to zoom with automatic centering.
6. **Defect Overlay Boxes**:
   - **Missing (Red Box)**: Graphic present on master but absent on translation.
   - **Extra (Purple Box)**: Graphic added in translation that does not exist in master.
   - **Moved (Orange Box)**: Graphic found, but shifted beyond allowable threshold (annotated with offset in points).
7. **"Ignore Translated Text" Toggle**: Enabled by default. Masks prose in memory so translated words don't trigger false pixel differences, focusing purely on diagrams, layout, and artwork.

---

## 7. Working with the Region Inspector

Use the **Region Inspector** tab to define zones on master pages that must be verified across all translations (e.g., logos, header text, document numbers, or footer copyright lines).

### Drawing & Configuring Regions:
1. Navigate through pages using the `< Prev` and `Next >` buttons.
2. Click and drag on the master page canvas to draw a rectangular region.
3. Select the region from the table to configure its properties:
   - **Region Label**: Name the zone (e.g. `Header Logo`, `Disclaimer`).
   - **Comparison Mode**:
     - *Exact Text Match*: Requires 100% character match (e.g. for model numbers or URLs).
     - *Don't Compare Text (Visual Match)*: Masks text and compares only pixels (ideal for logos, icons, hazard symbols).
     - *Present Only*: Checks for the presence of the region but content can br differ.
     - *Scope Only*: Defines an area but does not check it- acts an container for other regions.
     - *Same pattern*: The format must be match as master, but valuse can be differ (eg. number must be 6 digits in translation).
   - **Page Scope**: Select where this region applies:
     - `First page`: Cover page only.
     - `Last page`: Automatically maps to the last page of each translation regardless of page count.
     - `All pages`: Evaluated across the entire manual.
     - `Odd pages` / `Even pages`: Mirrored layout alternating pages.
   - **Variant Groups**: Group alternating left/right headers so a page passes if either variant matches.

### Setting Ignored Margins:
- Adjust the orange margin guides for **Top**, **Bottom**, **Left**, and **Right** in points.
- Any page furniture (running headers, footers, thumb tabs) falling within these bands is excluded from graphic cropping.
- Click **Save Template** to persist your settings as a JSON file in `templates/`.

---

## 8. Auditing Document Metadata

The **Meta Data** tab provides an instant diagnostic of PDF document properties:
- **Summary Table**:
  - Displays file name, size, total pages, sheet size (e.g. `A4`, `A5`, `Letter`), orientation, exact dimensions in mm, and detected column layout.
  - **Discrepancy Highlighting**: Rows that disagree with the English master in page count, sheet size, or column layout are tinted in attention colors (e.g. identifying Finnish or Swedish copies that run 18 pages vs master's 20 pages).
- **Per-Page Breakdown**: Select any document to inspect page-by-page sheet dimensions, rotation, and line gutter measurements.

---

## 9. Interpreting the 10-Sheet Excel QA Report

The generated Excel workbook (`PDF_Quality_Inspection_Report.xlsx`) is structured for QA sign-off:

| Sheet # | Sheet Name | Key Columns & Information |
| :---: | :--- | :--- |
| **1** | **Overview** | Executive summary: English Master vs Translated PDF, individual verdicts for TOC, Links, Images, Image Counts, Barcode/QR, Text Overlap, Not Translated, Master Verdict, and execution duration. |
| **2** | **TOC** | Master vs Translated topic numeric outlines (`1.1`, `1.2`), missing section codes, reordered sections, and TOC status. |
| **3** | **Links** | Complete hyperlink audit: Link category (URI, GoTo, QR), issue type (`MISSING LINK`, `EXTRA LINK`, `COUNT MISMATCH`), target destination URL, anchor text, master/translated pages, counts, and status. |
| **4** | **Barcode & QR** | Barcode and QR code counts, page locations, detection method (pyzbar vs structural), and match status. |
| **5** | **Images** | Detailed crop-by-crop visual comparison: Crop name, topic, master page, translated page, positional shift (`dx`, `dy`), visual similarity percentage, and match status. |
| **6** | **Image Counts** | Symmetric graphic counts per topic: Master count vs translated count (flags graphics deleted or added during DTP). |
| **7** | **Meta Data** | Document facts: File size, page counts, sheet size classification (ISO A0-A8, Custom), dimensions in mm, and body column counts. |
| **8** | **Stylesheet Result** | Region Inspector results: Region label, page scope, match type, similarity score, vertical shift (pt), and pass/fail status. |
| **9** | **Text Overlap** | Colliding text span pairs, collision area (pt²), page numbers, and failure explanation. |
| **10** | **Not Translated** | English phrases found in translations, page numbers, verbatim vs function-word density evidence, and status. |

---
## 10. Bitbucket Location: https://bitbucket.org/xyleminc/pdf-spot-check/src/main/

*SpotCheck — Automated PDF Quality & Visual Inspection Engine*
