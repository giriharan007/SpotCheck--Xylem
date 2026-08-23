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
                              SpotCheck Unified Engine (core/pipeline.py)
                                                   │
                    ┌──────────────────────┬───────┴───────┬──────────────────────┐
                    ▼                      ▼               ▼                      ▼
                  [TOC]            [Barcode & QR]   [Visual Graphics]    [Region Inspector]
            • Section Numbers      • Barcode Counts  • Pure Graphics      • User-drawn regions
            • Order Match          • QR Code Counts  • Text Masking       • Scoped exact match
            • Missing Sections     • Multi-page Scan • Layout Shift       • Visual match mode
                    │                      │               │                      │
                    └──────────────────────┴───────┬───────┴──────────────────────┘
                                                   │
                                  ┌────────────────▼────────────────┐
                                  │ Unified 4-Tab Excel QA Report   │
                                  └─────────────────────────────────┘
```

> **Note** — page 1 and last-page field checks (title, sub-title, manual type, address,
> disclaimer, copyright, footer metadata) are no longer performed by the pipeline.
> These manuals are stylesheet-based, and a fixed-coordinate comparison proved
> unworkable: a footer token such as the date code drifts up to 6.9pt between
> languages because the document number and language code in front of it have
> different glyph widths. Those fields are now verified through the Region
> Inspector's **scoped exact match** instead, which searches for a token anywhere
> inside a parent region rather than at fixed coordinates.

### 1. Table of Contents Numerics (`core/toc.py`)
- **Topic Numerics Extraction**: Extracts section numbering sequences (e.g. `1`, `1.1`, `1.2.1`, `2`, `2.1`) directly from PDF Bookmarks / Outline.
- **Sequence & Missing Topic Detection**: Flags missing, extra, or out-of-order sections across languages without being affected by translated header strings.

### 2. Barcode & QR Code Count Matching (`core/barcode_qr.py`)

> **Detection does not depend on decoding.** After pyzbar and the OpenCV decoders
> run, a structural sweep locates barcode- and QR-shaped regions from the rendered
> pixels — uniform columns with many vertical stripes for a 1D barcode, a square
> region at roughly 50% ink coverage with fine detail on both axes for a QR. This
> works whether a code is drawn as vector paths, a raster image or a pattern fill.
> Without it, a missing pyzbar caused two silent failures at once: the count check
> passed vacuously at `0/0`, and the undetected barcode was passed to the visual
> crop comparison, where its bars legitimately differ per document and it failed at
> ~74% in every language. Counts obtained this way are reported as
> `PASS (Count 1/1 structural)`, and a run that finds nothing with no decoder
> available reports `CHECK (No Detector)` rather than a clean pass.
- **Multi-Engine Decoding**: Decodes 1D barcodes and 2D QR codes via `pyzbar` with OpenCV fallback detectors across all pages.
- **Non-Visual Validation**: Matches absolute barcode and QR counts per document and per page, preventing false visual diffs caused by differing URLs or localized serial numbers.

### 3. Pure Graphic Element Extraction & Visual Comparison (`core/crop_images.py` & `core/compare_crops.py`)

**Crop layout adapts to the document.** When the PDF has a table of contents,
crops are filed under the topic they sit beneath; when it does not, they fall back
to page folders. The crop image and its per-page index are identical either way —
only the folder changes — so a graphic keeps the same name whether or not the
document has an outline, and results stay comparable across documents:

```
with TOC     <pdf>/1.3 User safety/p007_crop_01_image.png
without TOC  <pdf>/page_007/crop_01_image.png
```

`compare_crops.py` reads either layout, recovering the page from the `p###`
filename prefix in the topic layout and from the folder name otherwise. The topic
is carried into the Excel **Images** tab as its own column.

**Ignored margins come from the stylesheet** (`core/margins.py`). Extraction skips
anything lying entirely inside a margin band, measured inwards from each page edge in
points — `header`, `footer`, and now `left` and `right` as well. These were two
constants in the source, measured off one stylesheet; a manual built from a different
one puts its running header lower or its thumb tabs further in, and a fixed number then
either clips real artwork out of the run or lets page furniture in as content. Both
failures are silent. The four values are now set per stylesheet in the Region Inspector's
margin editor, saved into the template, and recorded on the Excel **Overview** sheet so a
report says what it was run with. The count check in `image_counts.py` uses the same
values, because the count and the crop have to agree about what is furniture.
- **Transitive Connected-Component Clustering**: Merges fragmented vector drawing paths (`fitz.get_drawings()`) and raster images into complete diagrams and schematics.
- **Text Masking for Pure Visual Comparison**: Overlays solid white masks across text spans inside crops to isolate graphical logos, icons, and diagrams from translated text.
- **Multi-Page Layout Shift Search**: Searches candidate regions across adjacent pages (priority on same page, expanding to adjacent pages) using normalized cross-correlation template matching (`cv2.matchTemplate`).
- **Visual Match Artifacts**: Generates side-by-side comparison images with similarity percentage scores.

### 4. Symmetric Image Count Check (`core/image_counts.py`)

The crop comparison is one-directional — it crops the master and hunts each crop in
the translation — so it cannot see a graphic the translation *added*, and it can miss
one the translation is *missing*: template matching searches the whole page, so where
a page carries several near-identical hazard icons, deleting one still matches a
sibling at ~99%. Both were reproduced on this manual and both came back 30/30 PASS.

Counting both documents catches both. Granularity follows the document:

- **With a TOC** — counts compared **per topic**, matched on the numeric code
  (`1.1`, `3.2`) rather than the title, since titles are translated but numbering is not.
- **Without a TOC** — **document total** only. Per-page counts are unusable as a
  fallback: reflow legitimately moves a graphic across a page boundary, and the clean
  de-DE copy already differs from the master on pages 11 and 12 while the total matches.

Blank regions are excluded from both counting and cropping — an empty crop matches
anything at 100%, and a graphic painted over would otherwise still count as present.
Across the master and all eleven translations, zero real candidates are blank.

### 5. Unified Multi-Sheet Excel QA Report (`core/pipeline.py`)
Generates a styled, executive-ready Excel workbook (`PDF_Quality_Inspection_Report.xlsx`) with 5 worksheets:

1. **Overview**: Executive dashboard of all sub-check verdicts and overall Master Verdict.
2. **TOC**: Topic numbering list, missing section codes, and section order status.
3. **Barcode & QR**: Barcode and QR code counts and page breakdown tables.
4. **Images**: Crop-by-crop visual comparison with page movement tracking and similarity percentages.
5. **Image Counts**: Per-topic image counts for both documents, or the document total when there is no TOC.

Master Verdict is `PASS` only when TOC, Barcode & QR, Images and Image Counts all pass.

---

## Interactive GUI Frontend (`gui/`)

SpotCheck features a modern desktop graphical interface styled according to **Xylem Corporate Brand Guidelines**:

- **Xylem Primary Palette**:
  - `Xylem Blue` (`#007DA3`) — Dominant primary brand color
  - `Dependable Blue` (`#003E51`) — Headings, cards, and structured elements
  - `Clarity Blue` (`#67DFFF`) — Accent highlights and subtitle styling
  - `Dynamic Green` (`#61D604`) — Action buttons and passing indicators
- **Typography Hierarchy**: **Roboto** (`Roboto Bold`, `Roboto Regular`) with native **Arial** desktop fallback.
- **Three-Tab Layout**: **Inspection** (paths, run control, live console), **Region Inspector** (ROI workbench) and **Review** (in-app comparison and side-by-side page diff) in a single window.
- **Live Streamed Console**: Thread-safe redirection of inspection execution logs to a built-in terminal box.
- **One-Click Post-Action Workflow**: Direct buttons to open the generated Excel QA report or the output folder.

### Region Inspector (`gui/region_dialog.py`)

An interactive ROI workbench that lives as the second tab of the main window:

- **Drag-to-select regions** on the rendered master page, with pan, zoom and page navigation.
- **Automatic sub-region nesting**: a region drawn inside another becomes `Region 1.1`, with consecutive re-indexing on add/delete.
- **Three comparison modes per region**: normal presence & translation check, *Exact Match Required* (100%), and *Don't Compare Text* (masks text and matches graphics only).
- **Layout shift reporting**: locates each region in every translated PDF within a vertical *and* horizontal tolerance (±15pt each by default) and reports the vertical offset in points. Translation changes line width as well as line count, so the search window needs slack on both axes.
- **Defaults**: opens on page 1; vertical tolerance 15pt, horizontal tolerance 15pt, pass threshold 75%.
- **Selected Region Preview**: renders whatever is inside the drawn box as a picture, live, before any check runs. Many regions hold no text at all — a divider rule, the Xylem logo, a hazard icon — and the extracted-text box shows nothing for those; the preview shows the actual mark, and an empty preview tells you the box missed its target.
- All scoring is performed by `core/region_engine.py`, which is fully headless.

### Stylesheet Templates (`core/templates.py`)

The manuals come from numbered stylesheets (style_10, style_11, ...). Every document
built from one puts its first page, last page, header and footer in the same places, so
the regions worth checking are marked **once per stylesheet**, saved as a named template,
and picked from a dropdown on the Inspection tab thereafter. Choosing one loads its
regions into the Region Inspector automatically; the last used template is remembered
between sessions. Templates are JSON, one file per template, in `templates/` beside the
application (falling back to `~/.spotcheck/templates/`).

**Page scope.** A region is not tied to the page it was drawn on. Each carries a scope —
`This page only`, `First page`, `Last page`, `All pages`, `Odd pages`, `Even pages`,
`Page range`, `Specific pages` — resolved against the document in hand, so `Last page`
means page 22 in a 22-page translation even though it was drawn on page 20 of the master.

**Variant groups.** A mirrored layout puts the same header at the left edge on one page
and the right edge on the next, which no single rectangle can express. Regions sharing a
`variant_group` are alternatives: the page passes if **any** of them matches. The usual
mirrored header is two regions in one group, one scoped `Odd pages` and one `Even pages`.
Results collapse to one verdict per group per page, naming which variant matched.

**Ignored page margins.** Saved with the template alongside the regions, because where
the header, footer and side furniture end is a property of the stylesheet. Four fields
in the Region Inspector — Top, Bottom, Left, Right, in points — with the page itself as
the ruler:

- the ignored area is shaded on the rendered page, live, as the numbers change;
- each boundary is a draggable orange guide, so the value can be set by eye and read off
  rather than guessed — the caption beside it gives points and millimetres;
- every graphic on the page is outlined: green dotted for kept, red dashed and labelled
  `ignored` for one the current setting would throw away;
- the readout under the fields counts them — *page 1: 5 graphics kept, 1 ignored (1 top)*.

Margins the user tunes without saving a template are still remembered in `settings.json`
and restored next launch; loading a template replaces them with its own. A template saved
before margins existed loads with the built-in defaults, which is exactly the behaviour it
was saved under.

### Review (`gui/comparison_gallery.py`)

Every comparison image the engine writes to disk is also browsable in-app, so reviewing
a run no longer means digging through nested output folders:

- **Two sources**: *Region Checks* (from a Region Inspector batch) and *Images* (from a full pipeline inspection).
- **Segregated by verdict**: *All* / *Passed* / *Needs Review*, with live counts.
- **Both sides labelled**: the detail pane names the master PDF and its page, and the translated PDF and its page, plus any vertical shift; the list carries the language tag so rows stay distinguishable across all eleven translations.
- **Keyboard navigation**: `↑` / `↓` step through results and swap the preview, `PgUp` / `PgDn` jump ten, `Home` / `End` go to the ends. The keys work anywhere on the tab, not only when the list has focus, and stand down on the other tabs and inside text fields. `▲` `▼` buttons and an "n of N" readout sit beside the verdict.
- **Click the image** to open it full size in the system viewer.
- **Compare Pages** opens both whole pages side by side — see below.
- **Clear Images** deletes the comparison images from disk after a confirmation that names the folder, the file count and the size. It only ever removes PNGs whose resolved path contains a `Cropped_Comparison` component, so source PDFs, the Excel report and the master crops under `Cropped_Images` cannot be touched; empty sub-folders are pruned and the results list is reset.

Laid out as list-plus-preview rather than a scrolling wall of cards. A full run
produces one comparison per crop per language (11 x 31 = 341 on the Start 350
manual); a card per result built over 10,000 CustomTkinter widgets and pushed Tk
past the point where it paints reliably. The list is a single native
`ttk.Treeview` and exactly one image is decoded at a time, so widget count stays
constant — 299 widgets whether the run produced 3 results or 341.

### Side-by-side page comparison (`core/page_diff.py` & `gui/page_diff_view.py`)

The crop comparison answers "does this one graphic match". When it says no, the next
question is always "what else is wrong on that page" — which needs both pages, whole.
**Compare Pages** on the Review tab opens the master page and its translation with the
differences boxed on both, scrolling as one.

**Why it does not diff pixels.** The prose has been rewritten in another language, so
every text block differs by design and a raw diff lights the page up. Translated text
is also a different length, so paragraphs reflow and push artwork down. Both are
handled before anything is compared:

- every text span on both pages is painted white, in memory, so only artwork is left;
- each master graphic is then hunted for **anywhere** on the translated page by
  normalised cross-correlation, rather than checked in place.

A first attempt subtracted the two masked renders and reported 17–22 differences per
page on a de-DE translation that is entirely correct — each shifted graphic appearing
twice, as a hole and as a surprise. Matching instead brings it to a handful, and the
handful is labelled:

| | |
|---|---|
| **Missing from the translation** | not found anywhere on the page |
| **Extra in the translation** | on the translation, claimed by nothing on the master |
| **Moved** | found, but more than 3 pt away — the shift is printed beside it |

A matched graphic is painted out of the working copy before the next is hunted, so a
page carrying several identical hazard icons cannot match them all to the one survivor
— the same trap `image_counts.py` exists to close. Barcodes and QR codes are excluded,
because each language legitimately carries its own part number.

What counts as a graphic is `crop_images.get_all_image_candidates`, the same detector
the crop comparison and the count check use, with the template's ignored margins
applied — so this view cannot disagree with the rest of the tool about what is on the
page.

Every difference is drawn **twice**: solid on the side it is on, dashed at the same
coordinates on the other, so the eye lands on the same spot in both panes. The region
that was under review is outlined in green. **Previous / Next** step through the
findings and scroll both panes to each one. **Ignore translated text** is on by
default; clearing it compares the text too, which is only useful on a page that was
not supposed to be translated at all.

Verified against a planted defect: erasing one hazard icon from page 7 of the German
copy is reported as `1 missing from the translation` at the right coordinates, with the
two genuine reflow shifts on that page correctly separated out as `moved`.

Cards are fed from results already in memory — `run_quality_inspection` returns its
results, and the Region Inspector publishes its batch through an `on_results` callback.

`RegionInspectorFrame` is a plain `CTkFrame`, so it embeds directly in the window's
tabview. `RegionInspectorDialog` wraps the same frame in a `CTkToplevel` for standalone
development (`python -m gui.region_dialog`); the application itself never uses it.

---

## Project Structure

SpotCheck is split into two packages: `core` holds the inspection engine and imports
no GUI toolkit at all, `gui` holds the desktop interface and contains no inspection
logic. Either half can be worked on, run, or tested without the other.

```
SpotCheck/
├── run_gui.py                     # Application entry point
├── logger_config.py               # Runtime logger & native DLL search paths
├── settings.py                    # Remembers the configured paths between sessions
│
├── core/                          # ── BACKEND: inspection engine (no Tkinter) ──
│   ├── __init__.py
│   ├── pipeline.py                # Unified orchestration & Excel report generator
│   ├── toc.py                     # TOC bookmark & topic numerics validator
│   ├── barcode_qr.py              # Barcode & QR code count & presence verification
│   ├── crop_images.py             # Vector & raster clustering, pure graphic crops
│   ├── compare_crops.py           # Cross-page pure graphic template comparison
│   ├── image_counts.py            # Symmetric image-count check (topic or total)
│   ├── margins.py                 # Ignored page margins: header/footer/left/right bands
│   ├── page_diff.py               # What differs between a master page and its translation
│   ├── templates.py               # Stylesheet templates: margins, page scope & variants
│   └── region_engine.py           # ROI extraction, scoped exact match & comparison
│
├── gui/                           # ── FRONTEND: desktop interface (CustomTkinter) ──
│   ├── __init__.py
│   ├── theme.py                   # Xylem palette, typography & ttk styling (shared)
│   ├── app_window.py              # Main window & tab host: pickers, live log, run control
│   ├── region_dialog.py           # Region Inspector ROI selector (embedded tab)
│   ├── comparison_gallery.py      # Review tab: pass/fail image review
│   └── page_diff_view.py          # Side-by-side page comparison window
│
├── Tables/                        # Standalone table structure comparison utilities
│   ├── Compare_Tables.py
│   └── Table_extraction.py
│
├── test_topic_image_extraction.py # Standalone topic-wise image extraction test
├── build_exe.bat                  # Windows 1-click PyInstaller packaging
├── SpotCheck.spec                 # PyInstaller build specification
├── requirements.txt               # Python package dependencies
├── .gitignore                     # Standard repository exclusion rules
└── README.md                      # Project documentation
```

### Why the split

| Concern | Lives in | Notes |
| :--- | :--- | :--- |
| Inspection logic | `core/` | Importable headless — scriptable, testable, no display required |
| Windows & widgets | `gui/` | One window, two tabs. The Region Inspector is a `CTkFrame` embedded in the main window's tabview, not a separate window |
| Brand palette & fonts | `gui/theme.py` | Single source of truth, imported by both windows |

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
python run_gui.py
```
The three paths are **remembered between sessions**. Configure them once and the next
launch starts with the same selection; change any of them and the new value becomes the
default. They are stored in `settings.json` beside the application, falling back to
`~/.spotcheck/` when that location is read-only (a copy installed under Program Files,
for instance). A remembered path whose file or folder no longer exists is dropped rather
than pre-filled, so a stale entry cannot fail at Run time.

1. Select the **English Master PDF**.
2. Select the **Translated PDFs Folder**.
3. Choose the **Output Directory**.
4. Click **"Run Full Inspection"**.
5. Once complete, click **"Open Excel Report"** to view results.

Comparison images appear on the **Review** tab once a run finishes, split into
Passed and Needs Review.

The Region Inspector loads the configured master **automatically** — there is no button
to press. Whatever is selected above is what the inspector shows; typed and pasted paths
work too, and the reload is debounced so typing does not re-open the PDF on every keystroke.

**"Clear Output Folder"** empties the configured output directory after a confirmation
naming the file count and size. It refuses outright if the output folder contains the
English master or the translated folder, since clearing it would destroy the source PDFs. Comparison images are written to `<output directory>\Cropped_Comparison\Region_Inspector`.
Loading a different master PDF clears any regions already drawn, since their coordinates
belong to the previous document.

### Option B: Run the engine from the command line

Because the engine is a package, run it with `-m` from the repository root:

```powershell
python -m core.pipeline
python -m core.pipeline --english "Input\English\894387_5.0_en-US_2026-04_IOM.Start350.pdf" ^
                        --translated "Input\Translated" ^
                        --output "Output"
```

Defaults are `Input/English`, `Input/Translated` and `Output`, all relative to the
repository root. Individual subsystems remain runnable on their own, for example:

```powershell
python -m core.barcode_qr --master "<master.pdf>" --target "Input\Translated"
python -m gui.region_dialog
```

To run with a stylesheet's saved margins instead of the defaults, name the template —
`core.crop_images` takes the same option, or the four values directly:

```powershell
python -m core.pipeline --template "10_stylesheet_template"
python -m core.crop_images --input "<master.pdf>" --header 58 --footer 44 --left 30 --right 30
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

- **TOC**: Topic sequence matching (`PASS`), missing / extra topic flagging (`FAIL`).
- **Barcode & QR**: Exact count and page match (`PASS (Count N/N)`).
- **Images**: Image similarity ≥ 80.0% (`MATCH (PASS)`).
- **Master Verdict**: `PASS` only when TOC, Barcode & QR, and Images all evaluate to `PASS`.

### Region Inspector verdicts

- **Layout/Presence**: `PASS (Exact)` ≥98%, `PASS (Near)` ≥85%, `PASS (Present)` above the pass threshold, else `CHECK (Translated)`.
- **Visual Match (no text)**: `PASS (Visual Match)` ≥75%, `CHECK (Visual Partial)` above threshold, else `FAIL`.
- **Scoped Exact Match** (sub-region inside a parent): the sub-region's box defines a
  token-snapped needle on the master, which is then searched inside the parent's scope
  on each translation — position within the block is irrelevant.
  - `0` occurrences → `FAIL (Not Found in Scope)`
  - `1` occurrence → `PASS (Exact in Scope)`
  - `2+` occurrences → `CHECK (Found Nx in Scope)` — ambiguous, surfaced to the user
- **Scope Only**: a container region, skipped by the run but still bounding its sub-regions.
