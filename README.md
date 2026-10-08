# SpotCheck — Xylem PDF Quality & Visual Inspection Engine

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Proprietary-red.svg)]()
[![Brand](https://img.shields.io/badge/brand-Xylem%20Standard-007DA3.svg)]()

**SpotCheck** is an automated quality assurance and visual inspection engine designed for technical documentation at Xylem. It compares multi-language translated PDF manuals against a Master English Source PDF to ensure 100% layout fidelity, metadata accuracy, legal compliance, and graphical consistency.

> 📘 **Looking for step-by-step instructions?** See the comprehensive [User Guide](file:///c:/Xylem%20Project/SpotCheck/USER_GUIDE.md) for end-user operational guidelines, workflow steps, Review Tab navigation, and Side-by-Side viewer tips.

---

## Key Features & Inspection Modules

```
                                  ┌─────────────────────────────────┐
                                  │   Master English Source PDF     │
                                  └────────────────┬────────────────┘
                                                   │
                              SpotCheck Unified Engine (core/pipeline.py)
                                                   │
         ┌──────────────────┬──────────────┼───────────────┬──────────────────┬─────────────────┐
         ▼                  ▼              ▼               ▼                  ▼                 ▼
       [TOC]           [Hyperlinks]  [Barcode & QR] [Visual Graphics] [Region Inspector] [Text & Margins]
 • Section Numbers   • Web URLs     • Barcodes     • Pure Graphics    • User ROI regions • Text Overlaps
 • Sequence Order    • Internal GoTo• QR Codes     • Text Masking     • Scoped exact     • Untranslated
 • Missing Sections  • 2D Position  • Structural   • Layout Shifts    • Visual match     • Margin Overflows
         │                  │              │               │                  │                 │
         └──────────────────┴──────────────┴───────┬───────┴──────────────────┴─────────────────┘
                                                   │
                                  ┌────────────────▼────────────────┐
                                  │  Unified 10-Sheet Excel QA Report│
                                  │  & Interactive Review Dashboard │
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

### 3. Hyperlink & Cross-Reference Verification (`core/links.py`)
- **Multi-Type Extraction**: Extracts external web URLs (URI), GoTo internal page targets, named destinations, and QR code targets directly using PyMuPDF (`page.get_links()`).
- **URL & Anchor Normalization**: Normalizes schemes (`http`/`https`), trailing slashes, query parameters, casing, and anchor fragments to prevent false discrepancy reports.
- **2D Positional Topic Locator (`extract_topics_with_positions`)**: Evaluates the 2D visual layout of headings and sub-headings across pages. If multiple sub-sections (e.g., `2.1` and `2.2`) share a single page, links are mapped to the exact sub-topic above which they physically reside, eliminating ambiguous or wrong topic attributions in the Review tab.
- **SequenceMatcher Alignment**: Performs fuzzy sequence alignment for internal cross-references to identify missing, extra, or count-mismatched internal links across languages after text reflow.
- **Zero Temporary JSON Files**: Runs 100% in-memory with findings flowing directly into the **Links** Excel worksheet and the interactive **Review** tab.
- **Non-TOC Fallback**: If a document lacks PDF bookmark outlines, link comparisons seamlessly bind to physical page numbers.

### 4. Pure Graphic Element Extraction & Visual Comparison (`core/crop_images.py` & `core/compare_crops.py`)

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

**Topic folder names are Windows-safe.** A long TOC title used to be truncated and given
a `...` suffix, producing a directory ending in dots — the one thing a Windows directory
name must not do. It failed in a way worth recording: Win32 strips trailing dots from the
**last** component of a path but not from intermediate ones, so `os.makedirs` created
`…arrangement with` while the image save then opened
`…arrangement with...\p033_crop_01_image.png`, where the dotted name is no longer last and
is taken literally. MuPDF returned errno 2 and a 33-page manual aborted mid-run. The strip
now happens *after* truncation, the marker is `~`, reserved device names are prefixed, and
the whole path is kept under 240 characters. A crop that still cannot be written falls back
to a plain `page_NNN` folder with a warning rather than ending the run.

**Ignored margins come from the stylesheet** (`core/margins.py`). Extraction skips
anything **most of which** lies inside a margin band, measured inwards from each page
edge in points — `header`, `footer`, and now `left` and `right` as well. These were two
constants in the source, measured off one stylesheet; a manual built from a different
one puts its running header lower or its thumb tabs further in, and a fixed number then
either clips real artwork out of the run or lets page furniture in as content. Both
failures are silent. The four values are now set per stylesheet in the Region Inspector's
margin editor, saved into the template, and recorded on the Excel **Overview** sheet so a
report says what it was run with. The count check in `image_counts.py` uses the same
values, because the count and the crop have to agree about what is furniture.

Two details of that rule were learned the hard way, both from the cover masthead:

*Clusters are judged, not fragments.* The masthead is a logo plus the long rule sweeping
out of it, which cluster into one element running from the top of the page down to
y=82.7. Filtering ran on the raw fragments **before** clustering, so only the pieces
wholly inside the band went — the survivors then merged into a graphic that had never
been asked the question, and the whole masthead was cropped and compared as artwork. It
also left a stray 32×12pt shard of the same masthead as a graphic in its own right,
because removing its neighbours changed what was left to cluster with. Clustering now
happens first and the finished cluster is what gets judged, which is the right question
anyway: the cluster is what becomes a crop.

*"Most of it", not "all of it".* Against a 50pt header band the masthead is 60% furniture
and 40% hangs below the line, so strict containment kept it. Deepening the band to 85pt
to swallow it would have deleted a real hazard icon at y=54.6 on page 12. Measured across
the master and all eleven translations, the masthead is the **only** element that overlaps
a 50pt band at all — every genuine graphic starts below the line and overlaps by 0% — so
`MARGIN_COVERAGE = 0.5` separates them with enormous headroom where no band depth could.
Raise it to 1.0 for the old strict behaviour.

Net effect on the Start 350 set: 30 master crops become 29, the masthead drops out of
every language identically, and all eleven still report 29/29 PASS.
- **Transitive Connected-Component Clustering**: Merges fragmented vector drawing paths (`fitz.get_drawings()`) and raster images into complete diagrams and schematics.
- **Text Masking for Pure Visual Comparison**: Overlays solid white masks across text spans inside crops to isolate graphical logos, icons, and diagrams from translated text.
- **Multi-Page Layout Shift Search**: Searches candidate regions across adjacent pages (priority on same page, expanding to adjacent pages) using normalized cross-correlation template matching (`cv2.matchTemplate`).
- **Visual Match Artifacts**: Generates side-by-side comparison images with similarity percentage scores.

**The comparison follows the TOC, not the page number.** Crops were always filed by
topic when the document has an outline, but the *search* was still "the same page number,
give or take three". A page number does not survive translation: the Swedish rendering of
this manual is 18 pages against the master's 20, and from topic 3.3 onward every page is
off by one. A topic number does survive — 3.2 is 3.2 in every language — so the search is
now scoped to wherever that topic actually landed, matched on the numeric code
(`core/toc.py: topic_page_spans`). Without an outline it falls back to the page window, so
both layouts still follow the same order. The comparison output mirrors the crop layout
too — topic folders in, topic folders out.

Verified on the Swedish copy: master page 20 is found at translated page 18 via topic 4.3,
page 16 at 15 via topic 4.1, and all 29 crops match. A topic-scoped search that finds
nothing widens to the whole document rather than reporting a false deletion.

### 5. Symmetric Image Count Check (`core/image_counts.py`)

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

### 6. Text Quality & Margin Overflow Checks (`core/text_overlap.py`, `core/untranslated.py`, `core/margin_overflow.py`)
- **Text Overlap (`core/text_overlap.py`)**: Detects text spans colliding into each other using physical glyph bounding boxes. Prevents false alarms caused by non-overlapping font bounding rectangles.
- **Untranslated English (`core/untranslated.py`)**: Identifies English sentences and phrases left behind in translated manuals by matching against the English master inventory and assessing English grammatical function-word density.
- **Margin Overflow (`core/margin_overflow.py`)**: Flags localized paragraphs and callouts expanding past stylesheet left and right margins (especially prominent in languages with longer average word lengths like German or Finnish).
- First and last pages are skipped by default to allow for untranslated back-cover copyright notices and addresses.

### 7. Unified 10-Sheet Excel QA Report (`core/pipeline.py`)
Generates a styled, executive-ready Excel workbook (`PDF_Quality_Inspection_Report.xlsx`) with 10 worksheets:

1. **Overview**: Executive dashboard of all sub-check verdicts, defect counts, run timings, and overall Master Verdict.
2. **TOC**: Topic numbering list, missing section codes, and section order status.
3. **Links**: Dedicated hyperlink verification sheet detailing category (URI, GoTo, QR), issue type, target destination, anchor text, master/translated pages, counts, and status.
4. **Barcode & QR**: Barcode and QR code counts, page breakdown tables, and detection method.
5. **Images**: Crop-by-crop visual comparison with page movement tracking, similarity percentages, and match status.
6. **Image Counts**: Symmetric per-topic image counts for both documents, or the document total when there is no TOC.
7. **Meta Data**: Document facts including file size, page count, sheet size (A0–A8/Letter/Legal/Custom), orientation, and column layout.
8. **Stylesheet Result**: User-defined Region Inspector measurements, match types, scores, vertical shifts, and verdicts.
9. **Text Overlap**: Detailed list of colliding text spans, collision area (pt²), and affected pages.
10. **Not Translated**: Detailed inventory of untranslated English lines, function-word density, and evidence type.

Master Verdict is `PASS` only when TOC, Links, Barcode & QR, Images, Image Counts, and Region Inspector all pass with zero text collisions, untranslated lines, or margin overflows.

---

## Interactive GUI Frontend (`gui/`)

SpotCheck features a modern desktop graphical interface styled according to **Xylem Corporate Brand Guidelines**:

- **Xylem Primary Palette**:
  - `Xylem Blue` (`#007DA3`) — Dominant primary brand color
  - `Dependable Blue` (`#003E51`) — Headings, cards, and structured elements
  - `Clarity Blue` (`#67DFFF`) — Accent highlights and subtitle styling
  - `Dynamic Green` (`#61D604`) — Action buttons and passing indicators
- **Typography Hierarchy**: **Roboto** (`Roboto Bold`, `Roboto Regular`) with native **Arial** desktop fallback.
- **One button runs everything.** TOC, barcode/QR, images, symmetric counts, stylesheet regions and metadata all run from the Inspection tab and land in one Excel report plus the tabs that show them. The other tabs are for setting things up and reading results, not for running separate jobs.
- **Four-Tab Layout**: **Inspection** (paths, run control, live console) and **Review** (comparison plus side-by-side page diff) sit together as the run-and-read pair; **Region Inspector** (ROI workbench) and **Meta Data** (file size, page count, sheet size, column layout — also usable standalone on any folder) are the set-up tabs behind them.
- **Fits the screen it is on.** The window is sized from the display rather than a fixed 1320×900, widget scale steps down below 800px of screen height, and the panes are proportional. Checked on 1366×768 and 1920×1080: nothing clipped on either.
- **Live Streamed Console**: Thread-safe redirection of inspection execution logs to a built-in terminal box.
- **One-Click Post-Action Workflow**: Direct buttons to open the generated Excel QA report or the output folder.

### Region Inspector (`gui/region_marking_tab.py`)

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

**Check this region on.** A region is not tied to the page it was drawn on. Each carries a
scope — `First page`, `Last page`, `All pages`, `Odd pages`, `Even pages` — resolved
against the document in hand, so `Last page` means page 18 in an 18-page translation even
though it was drawn on page 20 of the master.

`This page only`, `Page range` and `Specific pages` were **removed**. They pin a region to
a page *number*, and a page number does not survive translation: three of the eleven
languages here are 18 pages, so from topic 3.3 onward a region marked "page 16" would be
checked against the wrong content and would look like a defect in the translation rather
than a mistake in the setup. Templates saved with one still load — the scope is left
untouched and flagged `(retired)` in the table so it can be re-picked deliberately. The
unlabelled box that fed those two options is gone with them.

**Alternatives group** (was "variant group"). A mirrored layout puts the same header at the left edge on one page
and the right edge on the next, which no single rectangle can express. Regions sharing a
`variant_group` are alternatives: the page passes if **any** of them matches. The usual
mirrored header is two regions in one group, one scoped `Odd pages` and one `Even pages`.
Results collapse to one verdict per group per page, naming which variant matched.

**Pages exempt from the margins.** A `skip pages:` box beside the four figures takes any
mix of pages, ranges and the words `first` and `last` — `first, last, 3-5, 9` resolves to
pages 1, 3, 4, 5, 9 and 20 on a 20-page master and to 1, 3, 4, 5, 9 and 18 on an 18-page
translation, because the words resolve per document. Useful where a cover is deliberately all
furniture and you want it extracted anyway. The overlay says so on an exempt page rather
than drawing bands that are not in force.

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

### Meta Data (`core/metadata.py` & `gui/metadata_tab.py`)

One row per document — the English master first, then every translation — carrying
**file size**, **page count**, **sheet size** (A0–A8, plus Letter / Legal / Tabloid and a
`Custom W×H mm` fallback), **orientation**, **dimensions in mm**, **column layout** and
**TOC entry count**. Selecting a row breaks it down page by page.

Rows that disagree with the master on sheet size, page count or column layout are tinted,
because with twelve near-identical rows the odd one out is the only thing worth reading.
On the Start 350 set that immediately surfaces the Finnish, Norwegian and Swedish copies
at 18 pages against the master's 20.

**Column detection** is the only figure here that is inferred rather than read. With the
ignored margins, headers, footers and tables removed, a real gutter is a vertical strip no
body line writes into — wide enough to be deliberate, with substantial text on both sides.
Two specific traps had to be closed first, both of which turned a plainly single-column
page into a two-column one:

- a **two-column spec table** has a gap down the middle that reads exactly like a gutter,
  so table regions come out before the page is measured (this is the slow part of a scan,
  and there is a checkbox to turn it off);
- a **short right-aligned run of text** near the outer edge leaves a sliver of whitespace
  that also reads as a gutter, so a band only counts as a column when it holds at least
  four lines, 12% of the page's text and 15% of the text width, and few lines cross it.

Every page's row carries a **How it was measured** note — line count, tables excluded,
gutters found, candidates rejected and why — so a surprising number is checkable rather
than merely surprising. Verified against synthetic 1-, 2-, 3-, 4- and 5-column pages, a
two-column page carrying a three-wide table, and all twelve real manuals.

**Pages per document** samples the first N pages instead of reading all of them; the page
count and file size stay exact either way. On the twelve-manual set a full scan is ~19s
and a three-page sample is under a second.

**Adding a field.** `core/metadata.py` ends with two lists, `SUMMARY_COLUMNS` and
`PAGE_COLUMNS`. Put a value into the dict `pdf_metadata()` or `page_metadata()` returns,
add one entry to the matching list, and the tab grows a column — `gui/metadata_tab.py`
builds both tables from those specifications and needs no edit.

### Review Tab (`gui/Review_tab.py`)

Every inspection result produced by the engine is browsable in-app within a unified, high-performance Review Tab, eliminating the need to dig through nested output directories:

- **10 Integrated Check Sources**:
  - `TOC`: Section numbering sequence matching and missing topic indicators.
  - `Links`: Hyperlink verification (web URLs, internal GoTo targets, missing/extra links, count mismatches).
  - `Images`: Master-to-translation visual crop comparisons with similarity percentages.
  - `Image Counts`: Symmetric graphic count checks highlighting additions or deletions.
  - `Region Checks`: Stylesheet Region Inspector results (layout shifts, exact tokens).
  - `Barcode` & `QR Code`: Count verification and page locations.
  - `Overlap`: Text span collision detection.
  - `Not Translated`: English lines left behind in translations.
  - `Margin Overflow`: Text expanding into stylesheet margins.
- **Color-Banded Categorization**: Each check type features a distinct soft tint so reviewers can instantly distinguish inspection modules without scanning every row label.
- **Segregated by Verdict**: Filter by *All*, *Passed*, or *Needs Review / Issues* with live issue counters.
- **Master/Detail Architecture**: Uses a native `ttk.Treeview` for fast scrolling across thousands of results paired with on-demand image rendering. Widget count remains strictly constant regardless of manual length.
- **Interactive Review Cards**: Detailed inspection cards display the English Master PDF and page, the Translated PDF and page, topic codes, status badges, and exact link/text details.
- **Keyboard Navigation**: `↑` / `↓` step through results, `PgUp` / `PgDn` jump ten, `Home` / `End` move to the ends.
- **One-Click Side-by-Side Review**: Click **Side by Side** to launch the dual-page visual difference viewer anchored directly to the selected finding's page and topic.
- **Clear Images**: Safely cleans up temporary comparison images from disk with a safety guard that never touches source PDFs or master crops.

### Side-by-Side Dual-Page Viewer (`gui/Side_by_Side_preview.py`)

When an image or link check fails, reviewers need to inspect both pages in complete context. The **Side-by-Side Preview** opens the master page and its translated counterpart side by side:

- **Interactive Dual Canvases**: Displays Master PDF on the left and Translated PDF on the right with synchronized or independent scrolling.
- **Coordinate-Based Page Anchoring**: Resizing or maximizing the window preserves the active viewing location using document-relative point coordinates, preventing unintended auto-scrolling, jumping, or disorientation.
- **Sync-Scroll Loop Guard**: An internal rendering guard prevents cross-canvas scroll feedback loops, ensuring smooth navigation.
- **Fit Width & Fit Page Modes**: Toggle between **Fit Width** (ideal for examining text and small diagrams) and **Fit Page** (ideal for whole-sheet geometry review).
- **Proportional Step Zoom**: Instant zoom controls (`+`, `-`, `100%`) with automatic page centering.
- **Visual Defect Highlights**: Algorithmic differences are drawn directly on the canvases:
  - **Missing**: Graphic present on master but not found in translation.
  - **Extra**: Graphic present in translation that belongs to no master element.
  - **Moved**: Graphic shifted beyond the layout tolerance, annotated with the exact shift in points.
- **Exact Topic & Page Targeting**: When opened from a Review card (e.g. a Missing Link or Image crop), the viewer instantly loads and focuses on the exact master and translated pages corresponding to that topic.
- **Ignore Translated Text Toggle**: Automatically paints translated prose white before comparing artwork, preventing false diffs caused by normal translation reflow.

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
├── USER_GUIDE.md                  # Comprehensive end-user operational guidelines
│
├── core/                          # ── BACKEND: inspection engine (no Tkinter) ──
│   ├── __init__.py
│   ├── pipeline.py                # Unified orchestration & 10-sheet Excel report generator
│   ├── toc.py                     # TOC bookmark & topic numerics validator
│   ├── links.py                   # Hyperlink & cross-reference extraction & 2D position matching
│   ├── barcode_qr.py              # Barcode & QR code count & presence verification
│   ├── docscan.py                 # One page scan per document, cached & page-parallel
│   ├── crop_images.py             # Vector & raster clustering, pure graphic crops
│   ├── compare_crops.py           # Cross-page pure graphic template comparison
│   ├── image_counts.py            # Symmetric image-count check (topic or total)
│   ├── margins.py                 # Ignored page margins: header/footer/left/right bands
│   ├── margin_overflow.py         # Margin overflow check: text spilling past margins
│   ├── metadata.py                # File size, page count, sheet size, column layout
│   ├── page_diff.py               # What differs between a master page and its translation
│   ├── templates.py               # Stylesheet templates: margins, page scope & variants
│   ├── region_engine.py           # ROI extraction, scoped exact match & comparison
│   ├── text_overlap.py            # Overlapping text collision detection
│   └── untranslated.py            # English-left-behind detection
│
├── gui/                           # ── FRONTEND: desktop interface (CustomTkinter) ──
│   ├── __init__.py
│   ├── theme.py                   # Xylem palette, typography & ttk styling (shared)
│   ├── app_window.py              # Main window & tab host: pickers, live log, run control
│   ├── region_marking_tab.py      # Region Inspector ROI editor & stylesheet template manager
│   ├── metadata_tab.py            # Meta Data tab: per-document and per-page facts
│   ├── Review_tab.py              # Review tab: unified 10-source inspection gallery
│   └── Side_by_Side_preview.py    # Side-by-side dual-canvas PDF diff viewer with zoom
│
├── Tables/                        # Standalone table structure comparison utilities
│   ├── Compare_Tables.py
│   └── Table_extraction.py
│
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

## What Counts as One Graphic

A crop is only useful if it is the same picture in both documents, so the
grouping has to match what a reviewer would circle with a pen. Deciding that
from the PDF's drawing operators does not work: an exploded parts diagram is
hundreds of separate vector paths with white space between them, and a table
grid is a few dozen long rules. Merging paths that touch got it wrong in both
directions on the 92-page parts manual:

- page 17 — one exploded pump diagram came out as **19 boxes**, several of them
  single table cells, plus two empty boxes in the left margin
- page 15 — five illustrations came out as five *wrong* boxes: part 1 missed
  entirely, parts 3 and 4 each cut in half
- page 45 — sixteen loose parts on the page, **four** of them found

What a reviewer calls one figure is a connected region of ink once the text is
taken away, so that is what `core/crop_images.py` measures. The page is
rendered, the text is erased from the pixels (the page object is never
modified), the remaining ink is dilated by the gap that still reads as "the same
picture", and each connected region becomes one candidate. The leader lines of
an exploded diagram then hold it together exactly as they do for the eye, and
five illustrations that share no ink stay five.

Ruled regions are found in the same pass — a table is the one thing on a
technical page that draws long horizontal *and* long vertical rules crossing
repeatedly — and their ruling is stripped before grouping. A text table
therefore produces nothing, which is what you want: row heights change with the
length of the translated text, so cropping a grid would report a difference on
every correct translation. A picture inside a table cell survives, because a
drawing is not made of 26pt straight lines; 54 such pictures are kept across
this manual.

The grouping of the ruling matters as much as finding it. Grouping the
*crossings* by proximity was the first attempt and it fails on wide tables: in
the two-column table on page 15 the corners sit 200pt apart, so they came out as
four separate clumps of one or two junctions each, none recognised as a table
and none stripped — which is exactly the symptom of a table still being cropped
as a picture. The rules of a table all touch each other, though, so one
connected run of ruling is one lattice however wide the cells are. That is what
is grouped, and it finds every table in the manual.

**Page furniture is recognised by where it sits, not how wide it is.** The
language thumb tab bleeds to the trim, alternating edges on recto and verso.
The old rule required an edge element to be under 40pt wide; the Xylem tab is
47, so it survived — and then survived the side margin too, because it is wider
than the 35pt band and the coverage test needs half the element inside. It was
being cropped and compared on 84 of the 92 pages. An element that reaches within
10pt of the trim and is narrower than a twelfth of the sheet is now furniture
whatever the margins say, and a side band additionally catches anything anchored
to the edge that pokes into it. Nothing else in the manual matches either test:
the 84 elements dropped are all 46–47 × 15pt, one per page, alternating edges.

**Ruling is not artwork, wherever it is.** One test covers all of it: a long,
thin stroke that touches nothing else is furniture. That catches a table grid
(its rules touch only each other), the separator above a hazard block, and the
line under a running header, with no region test and no table finder. Three
details make it safe, and each was found by it going wrong first:

- *isolation* — a leader line in an exploded diagram touches the part it points
  at, so it is attached and stays. Without this the diagrams came apart again.
- *thickness* — a horizontal opening asks "is there an unbroken run of ink this
  wide", and a solid 50pt disc answers yes. The page-11 hazard pictogram was
  classified as ruling and reduced to an 18pt sliver of the arrow inside it.
- *span* — furniture crosses a quarter of the sheet. A barcode is fifty thin
  strokes touching nothing, and without a span test most of the cover barcode
  was eaten, differently in every language.

A cluster is then checked for what it is MADE of: 85% straight strokes and it is
ruling, or 60% if it is smaller than 1000pt² where a partial cell border is
common. Measured — cell fragment 564pt² at 0.80, cover barcode 2958pt² at 0.64,
hazard triangle 1213pt² at 0.29.

This is what made the symmetric count check usable. Before it, the Start 350 set
failed on **all eleven languages**: separator rules counted as graphics and moved
with the text, so every language disagreed with the master for reasons that had
nothing to do with the translations. After it, nine of eleven pass, and the two
that do not are real content differences — the Greek page 6 carries a
magnetic-field symbol the English does not.

Two guards earn their place:

- A table rect is only ever a **hint**. Table finders return nonsense on figure
  pages — page 16 came back with a "table" wider than the sheet that covered the
  diagram — so implausible rects are dropped, and a region is only stripped when
  most of its ink really is long straight lines.
- A cluster wholly inside another is dropped, so an inset detail drawn in the
  white space of a larger diagram is not compared twice.

Because the ruled regions come from the ink, no table finder runs in the crop
path at all — the label `table_image` comes from the same pass. That removed the
last per-document table scan.

Measured over the whole manual: 517 fragments → **446 whole figures**. The crop
count and the symmetric image count now agree exactly, which they did not
before. On the planted test — three pages deleted from a copy of the master —
the report shows exactly three findings, one per deleted page, and nothing else
across the remaining 89.

## One Stylesheet, Many Documents

A stylesheet is a claim that the same regions apply to every manual built from
it. Three things have to be resolved against the document in hand for that claim
to hold, and none of them can come from the numbers the template happens to
store.

**Which page.** A last-page region drawn on a 20-page manual was saved with
`page_num: 20`, and drawing it on page 20 of a 90-page manual puts it in the
middle of the book. The page is now resolved from the region's scope — `last`
means the last page of *this* document, `first` means page 1 — and the stored
number is only a fallback for a scope that no longer exists.

**Where on the page.** The same stylesheet is issued at A2 through A6, and the
layout is not a scaled copy of itself: the logo block is the same physical size
on every sheet and the footer sits the same distance up from the trim, while the
page around them grows. So a region records **which edges it belongs to** — the
gap to the nearest horizontal and vertical edge, plus its own size — and is
re-placed from those edges on whatever sheet it lands on. Measured on the real
stylesheet, A5 → A3:

| Region | on A5 | on A3 | anchored |
|---|---|---|---|
| Brand logo | 7.2, 12.8 | 7.2, 12.8 | top-left, unchanged |
| Bar code | ends 17.1 from the right | ends 17.1 from the right | right edge |
| Last-page block | 81.7 up from the bottom | 81.7 up from the bottom | bottom edge |

Fractional coordinates were the obvious alternative and are wrong here: they
would blow the logo up four times on A2. Anchoring is a default, not a law —
every box stays draggable and resizable.

**What the user corrected.** Automatic placement gets a box close; the last few
points are a drag. Every box on the page can now be picked up:

- press a corner or edge handle to resize
- press inside the box to move it whole
- press bare page to draw a new region, as before

One gesture, no mode to remember, and the cursor says which of the three is
about to happen. Until this existed the cross-sheet story could not be
finished - every drag created a NEW region, so the only way to correct a
placement was to delete and redraw it.

A box adjusted this way is remembered **for that sheet size** under
`rects_by_sheet` and wins over the anchor next time a document of that size is
opened. Two rules keep the sizes from treading on each other:

- the anchor is only re-derived on the sheet it was originally drawn against,
  so correcting A4 does not move A5
- saving pins the origin sheet's rectangle too, so the first placement is a
  stored fact rather than something re-derived later

Verified on the real stylesheet: a logo box arrives on A4 at its A5 size
(7.2, 12.8, 120.8, 70.4), is stretched and nudged to (27.2, 23.2, 200.8, 111.2),
comes back exactly there on the next A4 document, and the A5 geometry is
untouched. A region whose artwork genuinely is scaled with the sheet can set
`scale_with_page`.

The status line says what had to be adjusted — "stylesheet drawn on A5, this
document is A3 · 1 region moved to this document's pages · 2 boxes re-placed
from their page edges" — so a fit is never silent.

## What a Region Is Checked Against

The text a region must match starts as whatever is clipped out of the master at
that rectangle, and that is a good first guess and a poor final answer. A phone
number picks up a line break in the middle; an address loses a trailing comma;
a two-line block comes back joined. Every translation is then hunted for text
the master does not really say.

The box in the Region Inspector is therefore editable, and what is in it is what
gets checked. There are three states, and the file records all of them:

| In the editor | `master_text` | `expected_text` | At run time |
|---|---|---|---|
| left alone | what the page says | absent | re-read from the master |
| typed over | what the page says | what you typed | exactly what you typed |
| **Match this exact text** ticked | what the page says | the clip, pinned | exactly that, every time |

`master_text` is always written. It is a record, not an instruction, but without
it the file was silent about text and a stylesheet whose regions are only
rectangles is very hard to check by eye and impossible to review in a diff.

The tick matters as much as the typing. Relying on an edit alone meant there was
no way to pin a text that was already correct, and no way to see before saving
whether anything had been pinned at all. The **Text** column in the regions
table now reads `from page` or `saved: Fax: +46-471-24…` for every region.

An edit is stored as `expected_text` alongside the clipped text rather than over
it, so:

- the clip is still there to revert to, via **Use the text from the page**
- an edit that merely reproduces the clip records no override at all, which
  keeps a stylesheet from filling up with redundant text
- `expected_text` is saved into the template JSON and reloaded with it, and only
  appears on regions that actually carry an override

Verified end to end: with no override a region scores PASS at 100.0%; with the
same region overridden to text no manual contains, the same run returns CHECK at
16.1%. The override is what the checker uses, not a display.

A scoped sub-region is the exception — it is matched by a needle searched for
inside its parent, so its box shows that search and stays read-only.

Saving a stylesheet with no regions now asks first. It is a legitimate thing to
do — a stylesheet can be nothing but ignored margins — but a file containing
`"regions": []` checks no text on every manual it is applied to, and silently.

## Where a Graphic Is Looked For

A page number does not survive translation, but a topic number does — 3.2 is 3.2
in every language — so each crop is hunted for in whichever pages that topic
occupies in the translation. Two things have to be right for that to work.

**Front matter belongs to no topic.** The cover, the legal notice and the
contents list come before topic 1, and attributing them to topic 1 is not a
harmless mislabel: the topic decides where the search looks. The Xylem logo on
the cover was filed under "1 Introduction", topic 1 starts on page 6, the search
covered pages 5–7, found nothing, widened across the whole document, and matched
the *same logo on the back cover* — reporting a page 1 graphic as "moved to page
118" at 97%. Those crops now go to a `_Front matter` folder, carry no topic code,
and are searched for around their own page, where they match at 100%.

**Reflow moves pages, so the pairing has to move with it.** A translation runs
longer than its master, so English page 12 is Danish page 13 or 14, and by the
back of the book the drift is several pages. Anything that pairs page N with
page N is comparing two unrelated pages once reflow has set in. `toc.page_mapper`
measures the drift at every topic boundary that both documents share and applies
it to the pages inside — English 40 → Swedish 37 on the test copy — and the
region check, the Review side-by-side and the crop search all use it. Measured on
one region: page-for-page gave CHECK at 69.8% against the wrong page; topic-
aligned gives PASS at 100.0% against the right one. With no usable outline in
either document it falls back to the page number, which is the best guess
available.

**A tie goes to the near copy.** Manuals repeat their logo, their hazard icons
and their connector symbols, so several pages score alike and the coarse pass
cannot separate them. The candidate pages are therefore checked nearest-to-
expected first and the search stops there, so a repeated graphic is reported
where it belongs rather than wherever it scored a hair higher.

Below 55% nothing is reported as a location at all — see the credibility floor
in `core/compare_crops.py`.

## How Long a Run Takes

The two facts that used to dominate a run were both re-derived over and over:
where a document's barcodes are, and where its tables are. Neither changes
between passes, so `core/docscan.py` computes each once per document, caches it
against the file's path, size and modification time, and hands it to every stage
that asks. The master used to be re-swept once per translation — twelve times on
a normal Xylem set.

The same module runs the sweep across several processes on documents long enough
to be worth it (24 pages and up), keeping one core free so the window stays
responsive. One pool serves the whole run rather than one per pass — that was
two dozen spawn storms on a twelve-document run, each re-importing PyMuPDF and
OpenCV in every worker while the user was trying to use the window — and the
workers run one priority notch below the interface, which costs a few percent of
throughput and is the difference between a window that is busy and a window that
looks broken. Output is byte-identical to the single-process path; verified on the
92-page A4 manual, 478 crops, matching SHA-256.

Measured on that manual (master plus one translation, on a two-core machine):

| Stage                       | Before | After (serial) | After (2 workers) |
|-----------------------------|--------|----------------|-------------------|
| Master scan (tables + codes)| —      | 212 s          | 128 s             |
| Cropping the master         | 250 s  | 42 s           | 42 s              |
| Per translation             | ~300 s | 103 s          | 71 s              |
| Whole run                   | ~760 s | 391 s          | **157 s**         |

A machine with six cores divides the scan stages again. The run also reports a
real percentage and the name of the stage it is in, rather than an indeterminate
bar that is indistinguishable from a hang.

Two settings trade accuracy for time, both in plain sight:

- **Meta Data → Measure N% of pages.** Column layout belongs to the stylesheet,
  not to any one page, so the scan measures an evenly spread sample — half the
  document by default — and reports one verdict with the count of pages that
  disagreed. Evenly spread rather than the first N: the front of a manual is a
  cover, a notice and a contents list, none of which run the body layout. Tick
  **List every page** for the per-page detail behind the verdict.
- **Ignore tables when counting columns.** On by default; it is what stops a
  spec table being read as two columns.

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
- **Links**: Exact destination and count matching (`PASS`). Missing links, extra links, or destination mismatches evaluate to `FAIL` with issue count annotations (`FAIL (N)`).
- **Barcode & QR**: Exact count and page match (`PASS (Count N/N)`). Structural-only detections reported as `PASS (Count N/N structural)`. Missing detector reported as `CHECK (No Detector)`.
- **Images**: Pure graphic template similarity ≥ 80.0% (`MATCH (PASS)`).
- **Image Counts**: Symmetric graphic counts match per topic (with TOC) or document total (without TOC) (`PASS`). Additions or deletions flag `FAIL`.
- **Text Quality & Margins**: Any text collision (`FAIL`), untranslated English line (`FAIL`), or text margin overflow (`FAIL`) lowers the document verdict.
- **Master Verdict**: `PASS` only when TOC, Links, Barcode & QR, Images, Image Counts, and Region Inspector all evaluate to `PASS` with zero text/margin defects.

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
