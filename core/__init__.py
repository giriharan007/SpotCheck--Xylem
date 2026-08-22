"""
core — SpotCheck backend inspection engine.

Contains no GUI code. Every module here is importable and runnable without
Tkinter, so the inspection logic can be scripted, tested, or driven from the
desktop UI in the `gui` package.

Modules:
  pipeline       : unified orchestration + multi-sheet Excel report generation
  toc            : table of contents bookmark & topic numerics validation
  barcode_qr     : multi-page barcode & QR code count and presence verification
  crop_images    : vector & raster clustering and pure graphic crop extraction
  compare_crops  : cross-page pure graphic template comparison
  image_counts   : symmetric image-count check (per topic, or document total)
  region_engine  : user-defined ROI extraction, scoped exact match, comparison images

Page 1 and last-page field checks were removed: those manuals are stylesheet-based,
so the fields are verified through region_engine's scoped exact match instead.
"""
