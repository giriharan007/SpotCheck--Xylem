# SpotCheck — Xylem PDF Quality & Visual Inspection Engine

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Proprietary-red.svg)]()
[![Brand](https://img.shields.io/badge/brand-Xylem%20Standard-007DA3.svg)]()

**SpotCheck** is an automated quality assurance and visual inspection engine designed for technical documentation at Xylem. It compares multi-language translated PDF manuals against a Master English Source PDF to ensure 100% layout fidelity, metadata accuracy, legal compliance, and graphical consistency.

---

## Key Features & Inspection Modules

```
                                  ┌─────────────────────────────────┐
                                  │   Master English Source PDF     │
                                  └────────────────┬────────────────┘
                                                   │
                                   SpotCheck Unified Engine (main.py)
                                                   │
         ┌───────────────────┬─────────────────────┼────────────────────┬────────────────────┐
         ▼                   ▼                     ▼                    ▼                    ▼
   [First Page]            [TOC]              [Last Page]         [Barcode & QR]       [Visual Graphics]
 • Page Dimensions    • Section Numbers    • Global Address     • Barcode Counts     • Pure Graphics
 • Title & Sub-title  • Order Match        • Disclaimer Match   • QR Code Counts     • Text Masking
 • Manual Type Box    • Missing Sections   • Copyright Notice   • Multi-page Scan    • Layout Shift
 • Doc Num & Version                       • Footer Metadata    • Non-visual Decode  • Crop Similarity
         │                   │                     │                    │                    │
         └───────────────────┴─────────────────────┼────────────────────┴────────────────────┘
                                                   │
                                  ┌────────────────▼────────────────┐
                                  │ Unified 6-Tab Excel QA Report   │
                                  └─────────────────────────────────┘
```

### 1. First Page Object Model (`FirstPage.py`)
- **Page Dimensions**: Validates width and height match the master PDF within sub-point precision.
- **Title & Sub-Title Detection**: Analyzes font hierarchy on Page 1 (1st and 2nd largest text sizes) while filtering out metadata keywords and manual types.
- **Manual Type Positional Verification**: Extracts English manual type keyword (e.g. *Installation, Operation, and Maintenance*) and performs bounding box localized text extraction on translated pages.
- **Document Number & Version Matching**: Validates revision numbers (e.g. `_5.0`, `_4.0`) and document number lengths across hyphenated/underscored formats (e.g. `894387`, `95-27772-0000`).
- **Language Code Verification**: Identifies ISO language codes (e.g. `DA`, `DE`, `ES`, `FI`, `FR`, `IT`, `NL`, `NO`, `PT`, `SV`).

### 2. Table of Contents Numerics (`Toc.py`)
- **Topic Numerics Extraction**: Extracts section numbering sequences (e.g. `1`, `1.1`, `1.2.1`, `2`, `2.1`) directly from PDF Bookmarks / Outline.
- **Sequence & Missing Topic Detection**: Flags missing, extra, or out-of-order sections across languages without being affected by translated header strings.

### 3. Last Page Legal & Metadata Verification (`LastPage.py`)
- **Corporate Address Validation**: Verifies standardized global manufacturing facility and headquarters address.
- **Language-Specific Disclaimer & Copyright**: Uses a multilingual dictionary across 12+ European and global languages with text normalization (removing punctuation, handling Dutch `IJ` ligatures, and normalizing whitespace).
- **Order-Independent Footer Line Parsing**: Tokenizes and validates footer strings (e.g. `882597_5.0_sv-SE_2026-04_IOM_Start 350`) extracting:
  - Document Number
  - Version
  - Localized Language Code (e.g. `sv-SE`, `en-US`)
  - Revision Date (e.g. `2026-04`)
  - Manual Type Code (e.g. `IOM`, `QSG`, `DS`)
  - Product Name (e.g. `Start 350`)

### 4. Barcode & QR Code Count Matching (`Barcode_QR_Check.py`)
- **Multi-Engine Decoding**: Decodes 1D barcodes and 2D QR codes via `pyzbar` with OpenCV fallback detectors across all pages.
- **Non-Visual Validation**: Matches absolute barcode and QR counts per document and per page, preventing false visual diffs caused by differing URLs or localized serial numbers.

### 5. Pure Graphic Element Extraction & Visual Comparison (`crop_pdf_images.py` & `Compare_cropped_images.py`)
- **Transitive Connected-Component Clustering**: Merges fragmented vector drawing paths (`fitz.get_drawings()`) and raster images into complete diagrams and schematics.
- **Text Masking for Pure Visual Comparison**: Overlays solid white masks across text spans inside crops to isolate graphical logos, icons, and diagrams from translated text.
- **Multi-Page Layout Shift Search**: Searches candidate regions across adjacent pages (priority on same page, expanding to adjacent pages) using normalized cross-correlation template matching (`cv2.matchTemplate`).
- **Visual Match Artifacts**: Generates side-by-side comparison images with similarity percentage scores.

### 6. Unified Multi-Sheet Excel QA Report (`main.py`)
Generates a styled, executive-ready Excel workbook (`PDF_Quality_Inspection_Report.xlsx`) with 6 worksheets:
1. **Overview**: Executive dashboard of all sub-check verdicts and overall Master Verdict.
2. **First Page**: Detailed Page 1 metadata, title status, and version comparisons.
3. **TOC**: Topic numbering list, missing section codes, and section order status.
4. **Last Page**: Address, disclaimer, copyright, date code, and footer metadata match status.
5. **Barcode & QR**: Barcode and QR code counts and page breakdown tables.
6. **Images**: Crop-by-crop visual comparison table with page movement tracking and similarity percentages.

---

## Interactive GUI Frontend (`app_gui.py`)

SpotCheck features a modern desktop graphical interface styled according to **Xylem Corporate Brand Guidelines**:

- **Xylem Primary Palette**:
  - `Xylem Blue` (`#007DA3`) — Dominant primary brand color
  - `Dependable Blue` (`#003E51`) — Headings, cards, and structured elements
  - `Clarity Blue` (`#67DFFF`) — Accent highlights and subtitle styling
  - `Dynamic Green` (`#61D604`) — Action buttons and passing indicators
- **Typography Hierarchy**: **Roboto** (`Roboto Bold`, `Roboto Regular`) with native **Arial** desktop fallback.
- **Live Streamed Console**: Thread-safe redirection of inspection execution logs to a built-in terminal box.
- **One-Click Post-Action Workflow**: Direct buttons to open the generated Excel QA report or the output folder.

---

## Project Structure

```
SpotCheck/
├── app_gui.py                 # Xylem-branded interactive desktop GUI frontend
├── main.py                    # Unified master orchestration engine & Excel report generator
├── FirstPage.py               # Page 1 metadata, title hierarchy & manual type inspection
├── Toc.py                     # Table of Contents bookmark & topic numerics validator
├── LastPage.py                # Last page address, disclaimer, copyright & footer line parser
├── Barcode_QR_Check.py        # Multi-page Barcode & QR code count & presence verification
├── crop_pdf_images.py         # Vector & raster clustering and pure graphic crop extraction
├── Compare_cropped_images.py  # Cross-page pure graphic template comparison engine
├── Tables/
│   └── Compare_Tables.py      # Table structure comparison utilities
├── build_exe.bat              # Windows 1-click PyInstaller packaging automation
├── requirements.txt           # Python package dependencies
├── .gitignore                 # Standard repository exclusion rules
└── README.md                  # Project documentation
```

---

## Installation & Setup

### Prerequisites
- **Python 3.10+** (64-bit recommended)
- **Windows 10 / 11**

### 1. Clone the Repository
```powershell
git clone https://github.com/giriharan007/SpotCheck--Xylem.git
cd SpotCheck--Xylem
```

### 2. Install Dependencies
```powershell
pip install -r requirements.txt
```

*(Optional)* If scanning barcodes on Windows, ensure the C++ runtime dependencies for `pyzbar` are available.

---

## Running SpotCheck

### Option A: Launch Interactive GUI Frontend (Recommended)
```powershell
python app_gui.py
```
1. Select the **English Master PDF**.
2. Select the **Translated PDFs Folder**.
3. Choose the **Output Directory**.
4. Click **"Run Full Inspection"**.
5. Once complete, click **"Open Excel Report"** to view results.

### Option B: Run via Command Line
```powershell
python main.py
```

---

## Packaging Standalone Windows `.exe`

To build a portable, standalone Windows application:

1. Double-click `build_exe.bat` or run:
```powershell
.\build_exe.bat
```
2. The batch script will automatically:
   - Verify Python environment and dependencies.
   - Clean previous build caches.
   - Bundle all assets, PyMuPDF binaries, OpenCV runtimes, and CustomTkinter themes using PyInstaller.
   - Produce the executable at `dist\SpotCheck\SpotCheck.exe`.
3. The resulting `dist\SpotCheck\` folder can be distributed to any Windows workstation without requiring Python installed.

---

## Supported Language Codes & Mappings

| Code | Language | Code | Language | Code | Language |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **DA** / `da-DK` | Danish | **FI** / `fi-FI` | Finnish | **NO** / `no-NO` | Norwegian |
| **DE** / `de-DE` | German | **FR** / `fr-FR` | French | **PT** / `pt-PT` | Portuguese |
| **EL** / `el-GR` | Greek | **IT** / `it-IT` | Italian | **SV** / `sv-SE` | Swedish |
| **ES** / `es-ES` | Spanish | **NL** / `nl-NL` | Dutch | **EN** / `en-US` | English |

---

## Quality Status Thresholds

- **First Page**: Page size match (`PASS`), Title match (`Equal (<Title>)`), Sub-title match (`Equal (<Sub-Title>)`), Version (`PASS`), Doc length (`PASS`).
- **TOC**: Topic sequence matching (`PASS`), Missing / Extra topic flagging (`FAIL`).
- **Last Page**: Address matched (`Matched`), Disclaimer text matched (`Matched`), Copyright text matched (`Matched`), Manual type code matched (`PASS`), Date code matched (`PASS`).
- **Barcode & QR**: Exact count and page match (`PASS (Count N/N)`).
- **Pure Graphic Crops**: Image similarity $\ge 80.0\%$ (`MATCH (PASS)`).
- **Master Verdict**: `PASS` only when all 5 subsystems evaluate to `PASS`.
