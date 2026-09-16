"""
One expensive look at a document, shared by every stage that needs it.

Before this module existed, a single run re-derived the same facts about the
same PDF over and over. Checking eleven translations against one master meant:

    barcode/QR model of the master        x 11   (compare_barcode_qr)
    barcode/QR model of the translation   x 11
    barcode/QR rects again, page by page  x 12   (count_images_by_topic)
    table rects for the master            x  1   (crop_pdf_elements)

On a 92-page A4 manual that is around forty minutes of work, nearly all of it
repeated. The facts do not change between passes: where the barcodes are and
where the tables are depend only on the file. So they are computed once per
document, cached against (path, mtime, size), and handed to whoever asks.

Two properties are cached, both margin-independent:

    codes(page_no)   barcode and QR regions, as PDF-point rects
    tables(page_no)  table regions, as PDF-point rects

Deliberately NOT cached: image candidates. Those depend on the ignored-margin
settings, which change per page and per stylesheet, so caching them would hand
one run's answer to another run's question.

Each pass is computed lazily - a document that is only counted never pays for
table detection - and page-parallel when the document is long enough to make
the process pool worth starting. The pool is a best-effort optimisation: any
failure to start it falls back to the serial path, which is what the code did
before and is always correct.
"""

import os
import sys
import threading
from collections import OrderedDict

import pymupdf as fitz


# Documents held at once. A run touches a master plus its translations; twelve
# is a normal Xylem set, so sixteen keeps a whole run resident without letting
# a long session grow without limit. The entries are small - a few hundred
# rectangles per document, not the page images.
MAX_CACHED_DOCUMENTS = 16

# Below this many pages the process pool costs more to start than it saves.
PARALLEL_MIN_PAGES = 24

# Worker processes. Left at None the pool sizes itself to the machine, keeping
# one core free so the interface stays responsive while a scan runs.
MAX_WORKERS = None

# Set False to force the serial path (useful when debugging, and the automatic
# fallback when a pool cannot be created in a frozen build).
USE_PROCESS_POOL = True


def _worker_count():
    try:
        n = os.cpu_count() or 2
    except Exception:
        n = 2
    if MAX_WORKERS:
        return max(1, min(MAX_WORKERS, n))
    return max(1, min(8, n - 1))


# ── the per-page work, at module level so it can be pickled to a worker ──

# pdfplumber finds a handful of tables PyMuPDF misses - seven across a 23-page
# sample of this manual - and costs 1.2 seconds a page against PyMuPDF's 0.5 to
# do it, which on twelve 92-page documents is twenty minutes. It is off by
# default because crop_images no longer depends on a complete table list: it
# finds ruled regions in the page's own ink, and a table rect is only a hint.
ACCURATE_TABLES = False

# A "table" bigger than this share of the page, or one that runs off the edge,
# is a detection error rather than a table. Both finders produce them on pages
# whose figures use long straight leader lines: page 16 of the 92-page manual
# came back with a table at -54,152 .. 542,787 - wider than the paper - which
# covered the exploded diagram as well as the real parts list underneath it.
# Left in, it caused the diagram to be treated as ruling and cut into pieces.
MAX_TABLE_PAGE_FRACTION = 0.60
MIN_TABLE_SIDE_PT = 20.0


def _plausible_table(rect, page_rect):
    """Reject a 'table' that cannot be one."""
    r = fitz.Rect(rect)
    if r.is_empty or r.width < MIN_TABLE_SIDE_PT or r.height < MIN_TABLE_SIDE_PT:
        return False
    if (r.x0 < page_rect.x0 - 2 or r.y0 < page_rect.y0 - 2
            or r.x1 > page_rect.x1 + 2 or r.y1 > page_rect.y1 + 2):
        return False
    page_area = max(1.0, page_rect.width * page_rect.height)
    return (r.width * r.height) / page_area <= MAX_TABLE_PAGE_FRACTION


def _tables_on_page(args):
    """Table rects on one page, as plain tuples. Runs in a worker process."""
    path, page_no, accurate = args
    rects = []
    try:
        with fitz.open(path) as doc:
            page = doc[page_no - 1]
            page_rect = page.rect
            try:
                for t in page.find_tables().tables:
                    if _plausible_table(t.bbox, page_rect):
                        rects.append(tuple(fitz.Rect(t.bbox)))
            except Exception:
                pass
        if accurate:
            try:
                import pdfplumber
                with pdfplumber.open(path) as pl:
                    for t in pl.pages[page_no - 1].find_tables():
                        if _plausible_table(t.bbox, page_rect):
                            rects.append(tuple(fitz.Rect(t.bbox)))
            except Exception:
                pass
    except Exception:
        return page_no, []
    return page_no, rects


def _codes_on_page(args):
    """Barcode and QR rects on one page. Runs in a worker process."""
    path, page_no, dpi = args
    try:
        from core import barcode_qr as Barcode_QR_Check
        with fitz.open(path) as doc:
            found = Barcode_QR_Check.detect_barcodes_and_qr_codes(doc[page_no - 1], dpi=dpi)
        return page_no, [
            {
                "type": c.get("type", "BARCODE"),
                "raw_type": c.get("raw_type", ""),
                "data": c.get("data", ""),
                "decoded": bool(c.get("decoded")),
                "rect": tuple(c["rect"]),
                "page_num": page_no,
            }
            for c in found
        ]
    except Exception:
        return page_no, []


def _lower_priority():
    """
    Run this worker below the interface.

    Called once in each worker process. Without it a full-strength scan on a
    four-core laptop leaves nothing for the main thread, and switching tabs
    mid-run stutters or paints half a panel - the window is not hung, it is
    just never scheduled. Dropping the workers one notch costs a few percent of
    throughput and gives the interface back.
    """
    try:
        if sys.platform == "win32":
            import ctypes
            BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS)
        else:
            os.nice(5)
    except Exception:
        pass


_POOL = None
_POOL_SIZE = 0


def _get_pool():
    """
    The run's worker pool, started once and kept.

    A pool was previously created and torn down for every pass of every
    document: on a twelve-document run that is two dozen spawn storms, each one
    re-importing PyMuPDF and OpenCV in every worker while the user is trying to
    use the window. One pool for the whole run removes all of that.
    """
    global _POOL, _POOL_SIZE
    if _POOL is not None:
        return _POOL
    from concurrent.futures import ProcessPoolExecutor
    _POOL_SIZE = _worker_count()
    _POOL = ProcessPoolExecutor(max_workers=_POOL_SIZE, initializer=_lower_priority)
    return _POOL


def shutdown_pool(wait=False):
    """Release the workers. Safe to call when none were ever started."""
    global _POOL
    pool, _POOL = _POOL, None
    if pool is not None:
        try:
            pool.shutdown(wait=wait, cancel_futures=True)
        except TypeError:                      # Python < 3.9
            pool.shutdown(wait=wait)
        except Exception:
            pass


def _map_pages(fn, jobs, progress=None, label=""):
    """
    Run one per-page function over every page, in parallel where that pays.

    Returns {page_no: result}. A pool that will not start, or dies mid-flight,
    falls back to running the same function in this process: slower, never
    wrong. Progress is reported from the consuming side, so it stays on
    whichever thread called in rather than crossing into a worker.
    """
    total = len(jobs)
    out = {}

    use_pool = (USE_PROCESS_POOL and total >= PARALLEL_MIN_PAGES
                and _worker_count() > 1 and not _pool_disabled())
    if use_pool:
        try:
            pool = _get_pool()
            for i, (page_no, res) in enumerate(pool.map(fn, jobs, chunksize=2), start=1):
                out[page_no] = res
                if progress:
                    progress(i, total, label)
            return out
        except Exception as e:
            print(f"  [scan] parallel {label or 'pass'} unavailable ({e}); using single process")
            shutdown_pool()
            _disable_pool()
            out = {}

    for i, job in enumerate(jobs, start=1):
        page_no, res = fn(job)
        out[page_no] = res
        if progress:
            progress(i, total, label)
    return out


_POOL_OFF = False


def _pool_disabled():
    return _POOL_OFF


def _disable_pool():
    """One failure is enough; do not pay the startup cost again this session."""
    global _POOL_OFF
    _POOL_OFF = True


class DocScan:
    """The cached facts about one PDF."""

    def __init__(self, path, page_count):
        self.path = path
        self.page_count = page_count
        self._tables = None
        self._codes = None
        self._lock = threading.Lock()

    # ── tables ───────────────────────────────────────────────

    def tables(self, page_no, accurate=ACCURATE_TABLES, progress=None):
        """Table rects on one page, scanning the document on first use."""
        self._ensure_tables(accurate=accurate, progress=progress)
        return [fitz.Rect(r) for r in self._tables.get(page_no, ())]

    def _ensure_tables(self, accurate=ACCURATE_TABLES, progress=None):
        with self._lock:
            if self._tables is not None:
                return
            jobs = [(self.path, p, accurate) for p in range(1, self.page_count + 1)]
            self._tables = _map_pages(_tables_on_page, jobs, progress, "tables")

    # ── barcodes and QR codes ────────────────────────────────

    def codes(self, page_no, dpi=150, progress=None):
        """Barcode and QR entries on one page, in the detector's own shape."""
        self._ensure_codes(dpi=dpi, progress=progress)
        return [dict(c, rect=fitz.Rect(c["rect"])) for c in self._codes.get(page_no, ())]

    def code_rects(self, page_no, dpi=150, progress=None):
        """Just the rectangles, which is all the crop and count stages need."""
        self._ensure_codes(dpi=dpi, progress=progress)
        return [fitz.Rect(c["rect"]) for c in self._codes.get(page_no, ())]

    def all_codes(self, dpi=150, progress=None):
        """Every code in the document, page order, for the barcode check."""
        self._ensure_codes(dpi=dpi, progress=progress)
        out = []
        for page_no in sorted(self._codes):
            for c in self._codes[page_no]:
                out.append(dict(c, rect=fitz.Rect(c["rect"])))
        return out

    def _ensure_codes(self, dpi=150, progress=None):
        with self._lock:
            if self._codes is not None:
                return
            # Barcodes and QR codes are verified and expected only on first and last pages
            target_pnos = {1, self.page_count} if self.page_count > 0 else {1}
            jobs = [(self.path, p, dpi) for p in sorted(target_pnos)]
            scanned = _map_pages(_codes_on_page, jobs, progress, "barcodes")
            self._codes = {p: [] for p in range(1, self.page_count + 1)}
            self._codes.update(scanned)


# ── the registry ─────────────────────────────────────────────

_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _identity(path):
    """A key that changes when the file does, so an edited PDF is re-scanned."""
    st = os.stat(path)
    return (os.path.abspath(path), int(st.st_mtime), int(st.st_size))


def scan(pdf_path):
    """
    The cached scan for this document, created on first request.

    Returns None when the file cannot be opened, so callers can fall back to
    their own per-page work rather than failing.
    """
    try:
        key = _identity(pdf_path)
    except OSError:
        return None

    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return hit

    try:
        with fitz.open(pdf_path) as doc:
            pages = len(doc)
    except Exception:
        return None

    entry = DocScan(os.path.abspath(pdf_path), pages)
    with _CACHE_LOCK:
        _CACHE[key] = entry
        _CACHE.move_to_end(key)
        while len(_CACHE) > MAX_CACHED_DOCUMENTS:
            _CACHE.popitem(last=False)
    return entry


def forget(pdf_path=None):
    """Drop one document, or everything. Called when a run starts afresh."""
    with _CACHE_LOCK:
        if pdf_path is None:
            _CACHE.clear()
            return
        try:
            _CACHE.pop(_identity(pdf_path), None)
        except OSError:
            pass


def prepare(pdf_path, tables=False, codes=True, accurate_tables=ACCURATE_TABLES,
            progress=None):
    """
    Do the scanning now rather than on first use.

    The pipeline calls this at the top of a document so the cost lands in one
    reported step with a progress bar, instead of appearing as an unexplained
    stall in the middle of the first stage that happens to need it.
    """
    entry = scan(pdf_path)
    if entry is None:
        return None
    if codes:
        entry._ensure_codes(progress=progress)
    if tables:
        entry._ensure_tables(accurate=accurate_tables, progress=progress)
    return entry


def support_frozen_build():
    """
    Make the process pool safe inside a PyInstaller executable.

    Without this a frozen build re-runs the whole program in every worker
    instead of the worker function. Call it once, first thing in main().
    """
    try:
        import multiprocessing
        multiprocessing.freeze_support()
        if getattr(sys, "frozen", False):
            multiprocessing.set_start_method("spawn", force=True)
    except Exception:
        _disable_pool()
