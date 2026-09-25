"""
gui — SpotCheck desktop interface (CustomTkinter).

Presentation layer only. No inspection logic lives here; every check is executed
by the `core` package.

Modules:
  theme                : Xylem palette, typography resolver and ttk table styling,
                         shared by every window
  app_window           : main application window (path pickers, live log, run control)
  region_marking_tab   : Region Marking ROI selector, embedded as a tab
  Review_tab           : Review tab — pass/fail review of comparison images
  Side_by_Side_preview : Side-by-side page viewer (master vs. translation)
"""
