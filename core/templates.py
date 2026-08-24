"""
core/templates.py

Stylesheet templates: a saved set of marked regions, reusable across documents.

The manuals are produced from numbered stylesheets (style_10, style_11, ...).
Every document built from a given stylesheet puts its first page, last page,
header and footer in the same places, so the regions worth checking only have to
be marked once per stylesheet rather than once per document.

Two ideas beyond a plain list of regions:

PAGE SCOPE
    A region is not necessarily tied to the page it was drawn on. A header must
    be checked on every page; a logo only on page 1; a legal block only on the
    last page. Each region therefore carries a scope, resolved against the
    document being checked:

        single | first | last | all | odd | even | range (from-to) | pages (list)

VARIANT GROUPS
    Mirrored layouts put the same header at the left edge on one page and the
    right edge on the next. That is not a defect, so a single fixed rectangle
    cannot express it. Regions sharing a `variant_group` are alternatives: the
    page passes when it matches ANY of them. Scope and variants compose - the
    usual mirrored header is two regions in one group, one scoped odd and one
    scoped even - but a group with overlapping scopes works too, which matters
    because a translation with a different page count can flip which parity a
    given piece of content lands on.

IGNORED MARGINS
    How far in from each edge the page furniture reaches - the running header,
    the footer rule, the language thumb tabs down the side. Image extraction
    skips anything lying entirely inside those bands. Where they fall is a
    property of the stylesheet exactly as the regions are, so they are saved
    here rather than fixed in the source. See core/margins.py.

Storage is one JSON file per template, beside the application when that is
writable and under the user profile otherwise, matching settings.py.
"""

import datetime
import json
import os
import re
import sys

from core import margins as page_margins

TEMPLATE_DIR_NAME = "templates"
APP_DIR_NAME = ".spotcheck"
TEMPLATE_SUFFIX = ".template.json"
SCHEMA_VERSION = 1

SCOPE_SINGLE = "single"
SCOPE_FIRST = "first"
SCOPE_LAST = "last"
SCOPE_ALL = "all"
SCOPE_ODD = "odd"
SCOPE_EVEN = "even"
SCOPE_RANGE = "range"
SCOPE_PAGES = "pages"

# Every scope the loader still understands, so templates saved before the
# absolute-page ones were retired keep working.
SCOPE_TYPES = (SCOPE_SINGLE, SCOPE_FIRST, SCOPE_LAST, SCOPE_ALL,
               SCOPE_ODD, SCOPE_EVEN, SCOPE_RANGE, SCOPE_PAGES)

# What the interface offers. "This page only", "Page range" and "Specific pages"
# were removed deliberately: they pin a region to a page NUMBER, and a page
# number does not survive translation. The Swedish rendering of this manual is
# 18 pages against the master's 20, so from topic 3.3 onwards every page number
# is off by one - a region marked "page 16" would be checked against the wrong
# content in three of the eleven languages, and would look like a defect in the
# translation rather than a mistake in the setup.
#
# What is left is relative to the document rather than to a number, so it means
# the same thing in a 20-page master and an 18-page translation.
SCOPE_TYPES_OFFERED = (SCOPE_FIRST, SCOPE_LAST, SCOPE_ALL, SCOPE_ODD, SCOPE_EVEN)

# Retired, still loadable. A template carrying one of these is flagged in the
# regions table so it can be changed rather than silently mis-checked.
SCOPE_TYPES_LEGACY = (SCOPE_SINGLE, SCOPE_RANGE, SCOPE_PAGES)

SCOPE_LABELS = {
    SCOPE_SINGLE: "This page only",
    SCOPE_FIRST: "First page",
    SCOPE_LAST: "Last page",
    SCOPE_ALL: "All pages",
    SCOPE_ODD: "Odd pages",
    SCOPE_EVEN: "Even pages",
    SCOPE_RANGE: "Page range",
    SCOPE_PAGES: "Specific pages",
}

# Region fields carried into a template. Anything else (colours, transient text,
# per-run results) is presentation or scratch and is deliberately not persisted.
REGION_FIELDS = (
    "id", "label", "page_num", "is_last_page", "roi_rect", "parent_id",
    "exact_match", "dont_compare_text", "scope_only",
    "page_scope", "variant_group",
    # What the region must say, when the user has corrected it by hand.
    # Absent means "whatever is clipped out of the master at run time", which
    # is the right default; present means the extraction got it wrong - a stray
    # line break in a phone number, a clipped trailing comma - and this is the
    # text every translation is checked against instead.
    "expected_text",
)

_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ==============================================================================
# PAGE SCOPE
# ==============================================================================

def default_scope(page_num=None, is_last=False):
    """
    The scope a freshly drawn region gets.

    Page 1 and the last page are the two a page number CAN express safely, so a
    region drawn there gets that scope; anything else defaults to every page,
    which is the honest answer when the tool cannot know which pages of a
    translation hold the same content.
    """
    if is_last:
        return {"type": SCOPE_LAST}
    if page_num == 1:
        return {"type": SCOPE_FIRST}
    return {"type": SCOPE_ALL}


def describe_scope(scope):
    """Short human-readable form, for the regions table."""
    if not scope:
        return SCOPE_LABELS[SCOPE_SINGLE]
    t = scope.get("type", SCOPE_ALL)
    if t == SCOPE_RANGE:
        return f"Pages {scope.get('from', 1)}-{scope.get('to', 1)}  (retired)"
    if t == SCOPE_PAGES:
        pages = scope.get("pages") or []
        shown = ", ".join(str(p) for p in pages[:6])
        body = f"Pages {shown}{'...' if len(pages) > 6 else ''}" if pages else "Pages (none)"
        return body + "  (retired)"
    if t == SCOPE_SINGLE:
        p = scope.get("page")
        return f"Page {p}  (retired)" if p else SCOPE_LABELS[SCOPE_SINGLE]
    return SCOPE_LABELS.get(t, t)


def is_legacy_scope(scope):
    """True for a scope pinned to page numbers, which no longer travels safely."""
    return (scope or {}).get("type") in SCOPE_TYPES_LEGACY


def resolve_pages(scope, total_pages, anchor_page=None):
    """
    Expand a scope into the concrete 1-based page numbers to check.

    Resolved against the document in hand, so the same template applied to a
    translation with a different page count still means "every page" or "the
    last page" rather than a stale page number.
    """
    if total_pages <= 0:
        return []
    t = (scope or {}).get("type", SCOPE_SINGLE)

    if t == SCOPE_ALL:
        return list(range(1, total_pages + 1))
    if t == SCOPE_ODD:
        return [p for p in range(1, total_pages + 1) if p % 2 == 1]
    if t == SCOPE_EVEN:
        return [p for p in range(1, total_pages + 1) if p % 2 == 0]
    if t == SCOPE_FIRST:
        return [1]
    if t == SCOPE_LAST:
        return [total_pages]
    if t == SCOPE_RANGE:
        lo = max(1, int(scope.get("from", 1)))
        hi = min(total_pages, int(scope.get("to", total_pages)))
        return list(range(lo, hi + 1)) if lo <= hi else []
    if t == SCOPE_PAGES:
        wanted = scope.get("pages") or []
        return sorted({int(p) for p in wanted if 1 <= int(p) <= total_pages})

    page = scope.get("page") if scope else None
    page = page or anchor_page or 1
    if page == -1:
        return [total_pages]
    return [max(1, min(total_pages, int(page)))]


def parse_pages_expression(text, total_pages=None):
    """
    Parse "1, 3, 7-9" into a sorted page list. Returns [] for unusable input.

    Accepts the loose forms a person actually types - stray spaces, semicolons,
    reversed ranges - because this comes straight from a text field.
    """
    if not text:
        return []
    out = set()
    for chunk in re.split(r"[,;]+", str(text)):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"^(\d+)\s*[-–]\s*(\d+)$", chunk)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            out.update(range(a, b + 1))
        elif chunk.isdigit():
            out.add(int(chunk))
    if total_pages:
        out = {p for p in out if 1 <= p <= total_pages}
    return sorted(p for p in out if p >= 1)


# ==============================================================================
# STORAGE
# ==============================================================================

def _candidate_dirs():
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [os.path.join(base, TEMPLATE_DIR_NAME),
            os.path.join(os.path.expanduser("~"), APP_DIR_NAME, TEMPLATE_DIR_NAME)]


def _writable(directory):
    try:
        os.makedirs(directory, exist_ok=True)
        probe = os.path.join(directory, ".perm_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


_template_dir = None


def get_template_dir():
    """Resolve (and remember) where templates live."""
    global _template_dir
    if _template_dir:
        return _template_dir
    for d in _candidate_dirs():
        if _writable(d):
            _template_dir = d
            return d
    _template_dir = _candidate_dirs()[-1]
    return _template_dir


def safe_name(name):
    """
    A filesystem-safe template name; the display name is stored inside.

    Trailing dots and spaces are stripped AFTER the length cap, not before -
    truncating first and stripping second is what produced a Windows-illegal
    directory name in crop_images, and the same ordering mistake was here.
    """
    n = _BAD_NAME.sub("_", (name or "").strip())
    n = re.sub(r"\s+", " ", n)[:80].rstrip(". ")
    return n or "untitled"


def template_path(name):
    return os.path.join(get_template_dir(), safe_name(name) + TEMPLATE_SUFFIX)


def list_templates():
    """Display names of every stored template, sorted. Never raises."""
    d = get_template_dir()
    names = []
    try:
        for f in os.listdir(d):
            if not f.endswith(TEMPLATE_SUFFIX):
                continue
            try:
                with open(os.path.join(d, f), "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                names.append(data.get("name") or f[:-len(TEMPLATE_SUFFIX)])
            except Exception:
                names.append(f[:-len(TEMPLATE_SUFFIX)])
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"[Templates] Could not list {d}: {e}")
        return []
    return sorted(set(names), key=str.lower)


def _clean_region(r, seq):
    """Keep only persistable fields, and fill in anything missing."""
    out = {k: r.get(k) for k in REGION_FIELDS if k in r}
    out["id"] = r.get("id", seq)
    out["label"] = r.get("label") or f"Region {seq}"
    rect = r.get("roi_rect") or (0, 0, 0, 0)
    out["roi_rect"] = [round(float(v), 2) for v in rect]
    out["exact_match"] = bool(r.get("exact_match"))
    out["dont_compare_text"] = bool(r.get("dont_compare_text"))
    out["scope_only"] = bool(r.get("scope_only"))
    out["is_last_page"] = bool(r.get("is_last_page"))
    out["parent_id"] = r.get("parent_id")
    out["variant_group"] = (r.get("variant_group") or "").strip() or None
    # Only carried when it is really an override; an empty override and no
    # override are different things, so this cannot use `or None`.
    if r.get("expected_text") is None:
        out.pop("expected_text", None)
    else:
        out["expected_text"] = str(r["expected_text"])
    scope = r.get("page_scope")
    if not isinstance(scope, dict) or scope.get("type") not in SCOPE_TYPES:
        scope = default_scope(r.get("page_num"), bool(r.get("is_last_page")))
    out["page_scope"] = scope
    out["page_num"] = r.get("page_num", scope.get("page", 1))
    return out


def save_template(name, regions, source_pdf=None, notes="", margins=None, page_size=None):
    """
    Write a template. Returns its path, or None if it could not be written.

    `page_size` is the (width, height) in points of the document the margins
    were set against. It is recorded but never enforced: a stylesheet used at
    two trim sizes is a real thing, and refusing to load the template would be
    worse than letting the interface point out the difference.
    """
    if not name or not str(name).strip():
        print("[Templates] Refusing to save a template with no name.")
        return None
    payload = {
        "schema": SCHEMA_VERSION,
        "name": str(name).strip(),
        "saved_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "source_pdf": os.path.basename(source_pdf) if source_pdf else "",
        "notes": notes or "",
        "margins": page_margins.to_storage(margins),
        "page_size": [round(float(v), 1) for v in page_size] if page_size else None,
        "regions": [_clean_region(r, i + 1) for i, r in enumerate(regions or [])],
    }
    path = template_path(name)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)      # atomic: a crash cannot leave it truncated
        return path
    except Exception as e:
        print(f"[Templates] Could not write {path}: {e}")
        return None


def load_template(name):
    """Read a template by display name. Returns None when unreadable."""
    path = template_path(name)
    if not os.path.isfile(path):
        for f in os.listdir(get_template_dir()) if os.path.isdir(get_template_dir()) else []:
            if not f.endswith(TEMPLATE_SUFFIX):
                continue
            try:
                with open(os.path.join(get_template_dir(), f), "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if (data.get("name") or "") == name:
                    path = os.path.join(get_template_dir(), f)
                    break
            except Exception:
                continue
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[Templates] Could not read {path}: {e}")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("regions"), list):
        print(f"[Templates] {path} is not a valid template file.")
        return None
    data["regions"] = [_clean_region(r, i + 1) for i, r in enumerate(data["regions"])]
    # Templates saved before margins existed simply have none; they get the
    # built-in defaults, which is exactly the behaviour they were saved under.
    data["margins"] = page_margins.normalize(data.get("margins"))
    return data


def margins_for_template(name):
    """
    The ignored margins a template was saved with, or the defaults.

    Convenience for the run path, which needs the margins but not the regions.
    """
    data = load_template(name) if name else None
    return page_margins.normalize((data or {}).get("margins"))


def delete_template(name):
    path = template_path(name)
    try:
        if os.path.isfile(path):
            os.remove(path)
            return True
    except Exception as e:
        print(f"[Templates] Could not delete {path}: {e}")
    return False


# ==============================================================================
# APPLYING A TEMPLATE
# ==============================================================================

def expand_regions_for_document(regions, total_pages):
    """
    Turn template regions into concrete per-page checks.

    A region scoped to "all pages" becomes one check per page. Variant groups
    are carried through so the caller can collapse them: within a group, on a
    given page, one match is enough.
    """
    expanded = []
    for r in regions:
        pages = resolve_pages(r.get("page_scope"), total_pages, r.get("page_num"))
        for p in pages:
            item = dict(r)
            item["page_num"] = p
            item["is_last_page"] = (p == total_pages)
            item["_scope_type"] = (r.get("page_scope") or {}).get("type", SCOPE_SINGLE)
            item["_instance_of"] = r.get("id")
            expanded.append(item)
    return expanded


def collapse_variant_results(results):
    """
    Reduce variant alternatives to one verdict each.

    Regions in the same variant_group checked on the same page and target are
    alternatives - a mirrored header matches the left-aligned variant OR the
    right-aligned one - so the group passes if any member does. Regions with no
    group pass through untouched.
    """
    grouped, passthrough = {}, []
    for res in results:
        group = res.get("variant_group")
        if not group:
            passthrough.append(res)
            continue
        key = (group, res.get("eng_page"), res.get("tr_name"))
        grouped.setdefault(key, []).append(res)

    collapsed = []
    for (group, page, tr_name), members in grouped.items():
        winner = next((m for m in members if m.get("is_match")), None)
        best = winner or max(members, key=lambda m: m.get("similarity", 0))
        out = dict(best)
        out["region_label"] = group
        out["variant_count"] = len(members)
        out["variant_matched"] = best.get("region_label") if winner else None
        if winner:
            out["status"] = f"PASS (variant: {best.get('region_label')})"
        else:
            out["status"] = f"FAIL (no variant matched, tried {len(members)})"
            out["is_match"] = False
        collapsed.append(out)

    return passthrough + collapsed
