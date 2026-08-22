"""
gui — SpotCheck desktop interface (CustomTkinter).

Presentation layer only. No inspection logic lives here; every check is executed
by the `core` package.

Modules:
  theme         : Xylem palette, typography resolver and ttk table styling,
                  shared by every window
  app_window    : main application window (path pickers, live log, run control)
  region_dialog : Region Inspector ROI selector, embedded as a tab
  comparison_gallery : Comparisons tab — pass/fail review of comparison images
"""
