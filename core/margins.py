"""
core/margins.py

The four page margins that decide what image extraction ignores.

Why this is not four constants any more
---------------------------------------
The header and footer bands used to be hardcoded at 50 pt and 40 pt. Those
numbers were measured off one stylesheet. A manual built from a different
stylesheet puts its running header lower or its footer rule higher, and a fixed
50 pt then either clips real artwork out of the extraction or lets the header
logo in as if it were content. Both failures are silent: the crop count simply
comes out different, and nothing says why.

So the margins belong to the stylesheet, not to the source code — they are saved
in the template alongside the marked regions and travel with it.

Semantics
---------
A margin is a band measured inwards from one page edge, in PDF points. An
element is ignored when MOST of it lies inside a band - see MARGIN_COVERAGE.

That threshold is not a nicety. Strict containment was the original rule, and it
let the cover masthead of this very manual straight through: the Flygt logo and
the long rule sweeping out of it cluster into ONE element running from the very
top of the page down to y=82.7, so against a 50 pt header band 60% of it was
furniture and 40% hung below the line - and the whole thing was cropped and
compared as though it were artwork. Raising the band to 85 pt to swallow it
would have deleted a real hazard icon sitting at y=54.6 on page 12.

Measured across the master and all eleven translations, that masthead is the
only element overlapping a 50 pt header band at all; every genuine graphic
starts below the line and overlaps it by 0%. So "more than half of it" separates
the two cleanly and with enormous headroom, where no single band depth could.

    header : band down from the top edge
    footer : band up from the bottom edge
    left   : band in from the left edge      (new)
    right  : band in from the right edge     (new)

Left and right default to 0 — no side band — so a document processed with the
defaults extracts exactly what it extracted before this module existed. The
older narrow-element edge heuristic in crop_images.py still runs underneath and
still removes language thumb tabs; the side margins are an explicit instrument
on top of it, for stylesheets whose side furniture is wider than that heuristic
expects.

Units
-----
Everything here is PDF points (1/72 inch), which is what PyMuPDF returns. The
millimetre helpers exist purely for the interface: nobody knows what 46 pt looks
like, and "16.2 mm" next to a ruler on the rendered page is what makes the
number mean something.
"""

import re

PT_PER_MM = 72.0 / 25.4          # 2.8346
PT_PER_INCH = 72.0

SIDES = ("header", "footer", "left", "right")

# Pages the bands are switched off on, stored beside them in the template as a
# free-text expression ("first, last"). It lives in the same dict rather than a
# parallel setting so that saving, loading and passing margins around carries the
# exemption automatically.
SKIP_KEY = "skip_pages"

NO_MARGINS = {"header": 0.0, "footer": 0.0, "left": 0.0, "right": 0.0}

# The historical values, kept as the defaults so an existing project that has
# never opened the margin editor behaves exactly as it did before.
DEFAULT_MARGINS = {
    "header": 50.0,
    "footer": 40.0,
    "left": 0.0,
    "right": 0.0,
}

SIDE_LABELS = {
    "header": "Header (top)",
    "footer": "Footer (bottom)",
    "left": "Left side",
    "right": "Right side",
}

SHORT_LABELS = {"header": "top", "footer": "bottom", "left": "left", "right": "right"}

# A band wider than this fraction of the page is almost certainly a mistake -
# usually a value typed in millimetres into a field that wants points - and
# would quietly delete most of the document from the extraction.
MAX_FRACTION = 0.45

# How much of an element has to sit inside a band before it counts as page
# furniture. Half is deliberately generous: on these manuals the one element
# that straddles a band is 60% inside it and everything genuine is 0% inside,
# so anything from about 0.2 to 0.6 gives the same answer. Raise it towards 1.0
# to go back to requiring an element to be wholly inside the band.
MARGIN_COVERAGE = 0.5

# How close to the trim an element has to come before it counts as anchored to
# the paper edge. Xylem's thumb tabs stop about 5pt short; the body text block
# on these manuals starts 35-40pt in, and no figure inside it comes near this.
EDGE_TOUCH = 10.0

# ...and how far such an element may run past the inner edge of the side band
# and still be furniture. A tab overhangs its band by a few points; a figure
# that merely grazes the band extends far further into the page than this.
EDGE_REACH_BEYOND = 24.0


def mm(points):
    """Points to millimetres."""
    return float(points) / PT_PER_MM


def pt(millimetres):
    """Millimetres to points."""
    return float(millimetres) * PT_PER_MM


def describe_value(points):
    """'50 pt (17.6 mm)' - the form the editor shows beside each field."""
    return f"{float(points):.0f} pt ({mm(points):.1f} mm)"


def normalize(margins=None, page_width=None, page_height=None):
    """
    A complete, sane margins dict.

    Accepts None, a partial dict, or a dict with junk in it, and always returns
    all four sides as floats. Values are clamped to zero at the bottom and, when
    the page size is known, to MAX_FRACTION of that dimension at the top, so a
    slip of the keyboard cannot blank out the extraction.
    """
    out = dict(DEFAULT_MARGINS)
    skip = (margins or {}).get(SKIP_KEY) if isinstance(margins, dict) else None
    if skip:
        out[SKIP_KEY] = skip
    for side in SIDES:
        raw = (margins or {}).get(side)
        if raw is None:
            continue
        try:
            out[side] = max(0.0, float(raw))
        except (TypeError, ValueError):
            continue

    if page_height:
        cap = float(page_height) * MAX_FRACTION
        out["header"] = min(out["header"], cap)
        out["footer"] = min(out["footer"], cap)
    if page_width:
        cap = float(page_width) * MAX_FRACTION
        out["left"] = min(out["left"], cap)
        out["right"] = min(out["right"], cap)
    return out


# Which page, and which of its bands. The value stored under SKIP_KEY is
#
#     {"first": ["header", "footer"], "last": ["header"]}
#
# rather than a list of pages, because "skip the margins on the cover" and
# "skip only the header on the back page" are different instructions and the
# stylesheets need both. Keys are "first", "last", or a 1-based page number as
# a string; values are any subset of SIDES.
SKIP_FIRST = "first"
SKIP_LAST = "last"


def normalize_skip(skip, total_pages=None):
    """
    A clean {where: set(sides)} map from whatever was stored.

    A plain string is accepted and means "all four bands on those pages" - that
    is what the earlier `first, last, 3-5` expression meant, so templates saved
    with it keep working and simply gain the finer control when re-saved.
    """
    out = {}
    if not skip:
        return out

    if isinstance(skip, str):
        for page in _parse_page_expression(skip, total_pages):
            out[page] = set(SIDES)
        return out

    if isinstance(skip, dict):
        for where, sides in skip.items():
            key = str(where).strip().lower()
            if key in (SKIP_FIRST, SKIP_LAST):
                pass
            elif key.isdigit():
                key = int(key)
            else:
                continue
            if sides is True:
                chosen = set(SIDES)
            elif isinstance(sides, str):
                chosen = {sides} & set(SIDES)
            else:
                chosen = {str(x).lower() for x in (sides or [])} & set(SIDES)
            if chosen:
                out[key] = chosen
    return out


def _parse_page_expression(text, total_pages=None):
    """Legacy `first, last, 3-5` expression to a set of page numbers."""
    out = set()
    for chunk in re.split(r"[,;]+", str(text or "")):
        chunk = chunk.strip().lower()
        if not chunk:
            continue
        if chunk in ("first", "1st", "cover"):
            out.add(SKIP_FIRST)
            continue
        if chunk in ("last", "back"):
            out.add(SKIP_LAST)
            continue
        m = re.match(r"^(\d+)\s*[-–]\s*(\d+)$", chunk)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            out.update(range(a, b + 1))
        elif chunk.isdigit():
            out.add(int(chunk))
    return out


def sides_skipped_on(margins, page_no, total_pages=None):
    """Which bands are switched off on this particular page."""
    skip = normalize_skip((margins or {}).get(SKIP_KEY) if isinstance(margins, dict) else None,
                          total_pages)
    if not skip or page_no is None:
        return set()

    off = set()
    page_no = int(page_no)
    if page_no == 1:
        off |= skip.get(SKIP_FIRST, set())
    if total_pages and page_no == int(total_pages):
        off |= skip.get(SKIP_LAST, set())
    off |= skip.get(page_no, set())
    return off


def describe_skip(skip, total_pages=None):
    """'first page: top, bottom off' - the editor's one-line readout."""
    m = normalize_skip(skip, total_pages)
    if not m:
        return "margins apply to every page"
    order = [SKIP_FIRST, SKIP_LAST] + sorted(k for k in m if isinstance(k, int))
    parts = []
    for where in order:
        if where not in m:
            continue
        sides = m[where]
        label = {SKIP_FIRST: "first page", SKIP_LAST: "last page"}.get(where, f"page {where}")
        if sides == set(SIDES):
            parts.append(f"{label}: all margins off")
        else:
            names = ", ".join(SHORT_LABELS[s] for s in SIDES if s in sides)
            parts.append(f"{label}: {names} off")
    return "  ·  ".join(parts)


def margins_for_page(margins, page_no=None, total_pages=None):
    """
    The margins in force on one page, with the exempted bands zeroed.

    Only the chosen bands are switched off, not all of them: a cover whose
    header is deliberately artwork still wants its footer band honoured.
    Resolving it here rather than threading a page number through every geometry
    function keeps the exemption in one place.
    """
    m = normalize(margins)
    off = sides_skipped_on(margins, page_no, total_pages)
    for side in off:
        m[side] = 0.0
    return m


def is_default(margins):
    """True when these margins are the built-in ones."""
    m = normalize(margins)
    return all(abs(m[s] - DEFAULT_MARGINS[s]) < 0.01 for s in SIDES)


def describe(margins):
    """One-line summary for logs and the Excel report header."""
    m = normalize(margins)
    txt = (f"header {m['header']:.0f} / footer {m['footer']:.0f} / "
           f"left {m['left']:.0f} / right {m['right']:.0f} pt")
    if m.get(SKIP_KEY):
        txt += f"  ({describe_skip(m[SKIP_KEY])})"
    return txt


def content_box(page_width, page_height, margins=None):
    """
    The live area as (x0, y0, x1, y1) in top-down page coordinates.

    Degenerate margins (bands that meet or cross) collapse to an empty box
    rather than an inverted one, so callers can test `x1 > x0` safely.
    """
    m = normalize(margins, page_width, page_height)
    x0 = m["left"]
    y0 = m["header"]
    x1 = max(x0, float(page_width) - m["right"])
    y1 = max(y0, float(page_height) - m["footer"])
    return (x0, y0, x1, y1)


def band_boxes(page_width, page_height, margins=None):
    """
    The four ignored bands, as {side: (x0, y0, x1, y1)}, omitting empty ones.

    Full-width top and bottom bands, side bands spanning only the height left
    between them, so the four never overlap and the shaded overlay in the editor
    has no doubled-up corners.
    """
    m = normalize(margins, page_width, page_height)
    w, h = float(page_width), float(page_height)
    top = m["header"]
    bottom = h - m["footer"]
    boxes = {}
    if m["header"] > 0:
        boxes["header"] = (0.0, 0.0, w, min(top, h))
    if m["footer"] > 0:
        boxes["footer"] = (0.0, max(bottom, 0.0), w, h)
    mid_y0, mid_y1 = min(top, h), max(bottom, 0.0)
    if mid_y1 > mid_y0:
        if m["left"] > 0:
            boxes["left"] = (0.0, mid_y0, min(m["left"], w), mid_y1)
        if m["right"] > 0:
            boxes["right"] = (max(w - m["right"], 0.0), mid_y0, w, mid_y1)
    return boxes


def which_margin(rect, page_width, page_height, margins=None,
                 coverage=MARGIN_COVERAGE):
    """
    The side whose band this rect mostly belongs to, or None if it is kept.

    Returned rather than a bare boolean because the editor labels each dropped
    element with the margin responsible for dropping it — that is what turns a
    number into something the user can act on.

    `coverage` is the share of the element that has to fall inside a band for it
    to count as furniture; coverage=1.0 restores strict containment.
    """
    m = normalize(margins, page_width, page_height)
    x0, y0, x1, y1 = (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    w, h = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)
    pw, ph = float(page_width), float(page_height)

    if m["header"] > 0 and max(0.0, min(y1, m["header"]) - y0) / h >= coverage:
        return "header"
    if m["footer"] > 0 and max(0.0, y1 - (ph - m["footer"])) / h >= coverage:
        return "footer"
    # The side bands ask a second question as well. A language thumb tab is
    # wider than the band it lives in - 47pt of tab against a 35pt margin - so
    # the coverage test alone keeps it, which is not what someone who has just
    # set a side margin expects to happen. What makes it furniture is that it
    # is anchored to the paper edge: a real figure sits inside the text block
    # and never comes within a few points of the trim. So an element that
    # reaches the edge AND pokes into the band is furniture whatever its width,
    # as long as it does not also run deep into the content area.
    left_in = max(0.0, min(x1, m["left"]) - x0)
    if m["left"] > 0:
        if left_in / w >= coverage:
            return "left"
        if (x0 <= EDGE_TOUCH and left_in > 0
                and x1 <= m["left"] + EDGE_REACH_BEYOND):
            return "left"

    right_in = max(0.0, x1 - (pw - m["right"]))
    if m["right"] > 0:
        if right_in / w >= coverage:
            return "right"
        if (x1 >= pw - EDGE_TOUCH and right_in > 0
                and x0 >= pw - m["right"] - EDGE_REACH_BEYOND):
            return "right"
    return None


def in_margin(rect, page_width, page_height, margins=None):
    """True when this rect should be ignored by extraction."""
    return which_margin(rect, page_width, page_height, margins) is not None


def from_values(header=None, footer=None, left=None, right=None):
    """Build a margins dict from four separate values (the editor's fields)."""
    return normalize({"header": header, "footer": footer,
                      "left": left, "right": right})


def to_storage(margins):
    """Rounded, JSON-friendly form for the template file."""
    m = normalize(margins)
    out = {s: round(m[s], 1) for s in SIDES}
    skip = normalize_skip(m.get(SKIP_KEY))
    if skip:
        out[SKIP_KEY] = {str(k): [x for x in SIDES if x in v]
                         for k, v in sorted(skip.items(), key=lambda kv: str(kv[0]))}
    return out
